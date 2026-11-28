"""Runbook automation with Ansible Automation Platform (AAP).

ResilienceAI never runs commands. A test manager links an activity to a job template in the bank's AAP, with fixed
variables. When the activity can start, its owner (or the test manager) presses Run: that person's approval is
recorded, and ResilienceAI asks AAP to launch the template. From then on it follows the job, so nobody has to type
updates:

    job pending / running   -> activity In Progress
    job successful          -> the job's output is stored as evidence (scanned, pre-checked) and the activity is
                               submitted for review, or completed when it has no reviewer
    job failed / error      -> activity Blocked, with the failed task, host and message, and a link to the job;
                               the owner, the test manager and everyone downstream are told
    job canceled            -> activity Blocked ("cancelled in Ansible")

AAP tells ResilienceAI when a job changes (a webhook "doorbell"); ResilienceAI then asks AAP for the job's actual state
rather than trusting the message. The worker also checks every running job each minute, so a lost notification only
delays an update. Pausing, aborting or cancelling the test cancels its running jobs.

Every link, launch, status change and cancellation is in the audit trail, attributed to the person who approved it or
to the integration's own person record ("Bank AAP (automation)").
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, insert, select, text, update

from .. import authz
from ..db import engine, t, tenant_transaction
from ..domain import dependencies as dep
from ..domain.enums import ActivityStatus as A
from ..domain.enums import LifecycleStatus as S
from ..errors import Conflict, Forbidden, NotFound, Unprocessable
from ..security import Ctx, Principal
from . import aap, audit, notifications
from .common import child_of_event, log, now, row

logger = logging.getLogger("resilienceai.automation")

ACTIVE = ("Launching", "Pending", "Running")
STATUS = {"new": "Pending", "pending": "Pending", "waiting": "Pending", "running": "Running",
          "successful": "Successful", "failed": "Failed", "error": "Error", "canceled": "Canceled"}
FINISHED = ("Successful", "Failed", "Error", "Canceled", "Lost")
MAX_VARS = 8 * 1024
LAUNCH_GRACE = timedelta(minutes=2)


def _client(integ: dict) -> aap.AAP:
    return aap.AAP(integ["base_url"], integ["token_env"])


def _integration(ctx: Ctx, integration_id: UUID) -> dict:
    i = t("automation_integrations")
    r = ctx.conn.execute(select(i).where(i.c.id == integration_id)).mappings().first()
    if r is None or r["disabled_at"]:
        raise NotFound("That automation integration isn't available")
    return dict(r)


def integrations(ctx: Ctx) -> list[dict]:
    if ctx.me.is_external:
        raise Forbidden("Automation isn't available to external users")
    i = t("automation_integrations")
    return [{"id": r.id, "name": r.name, "kind": r.kind}
            for r in ctx.conn.execute(select(i.c.id, i.c.name, i.c.kind).where(i.c.disabled_at.is_(None)).order_by(i.c.name))]


def job_templates(ctx: Ctx, integration_id: UUID, search: str = "") -> list[dict]:
    if ctx.me.is_external:
        raise Forbidden("Automation isn't available to external users")
    try:
        return _client(_integration(ctx, integration_id)).job_templates(search)
    except aap.AAPError as e:
        raise Conflict(e.message) from e


# ---------------------------------------------------------------------------
# Linking an activity to a job template
# ---------------------------------------------------------------------------
def _runbook(ctx: Ctx, activity_id: UUID) -> dict | None:
    rb = t("activity_runbooks")
    r = ctx.conn.execute(select(rb).where(rb.c.activity_id == activity_id)).mappings().first()
    return dict(r) if r else None


def _active_run(ctx: Ctx, activity_id: UUID) -> dict | None:
    ru = t("automation_runs")
    r = ctx.conn.execute(select(ru).where(and_(ru.c.activity_id == activity_id, ru.c.status.in_(ACTIVE)))).mappings().first()
    return dict(r) if r else None


def link(ctx: Ctx, activity_id: UUID, *, integration_id: UUID, job_template_id: int, extra_vars: dict | None,
         timeout_minutes: int = 120, launch_mode: str = "approve") -> dict:
    act, ev = child_of_event(ctx, "test_activities", activity_id, for_update=True)
    authz.require_manage(ctx, ev)
    if S(ev["lifecycle_status"]) in (S.CLOSURE, S.CLOSED, S.CANCELLED):
        raise Conflict("This test is closing or closed; runbooks can't be linked now")
    if launch_mode != "approve":
        raise Conflict("Launching without a person's approval needs an autonomy policy approved by the CISO. "
                       "Until then each run is approved by the activity owner or the test manager.")
    if not act["stakeholder_id"]:
        raise Unprocessable("Assign the activity to a person first: someone stays accountable for what the runbook does")
    if _active_run(ctx, activity_id):
        raise Conflict("A run is in progress for this activity; wait for it to finish or cancel it first")
    extra_vars = extra_vars or {}
    if not isinstance(extra_vars, dict) or len(json.dumps(extra_vars)) > MAX_VARS:
        raise Unprocessable("Variables must be a JSON object of at most 8 KB")
    if any(k.startswith("resilienceai_") for k in extra_vars):
        raise Unprocessable("Variables starting with resilienceai_ are set by ResilienceAI itself")
    integ = _integration(ctx, integration_id)
    try:
        tpl = _client(integ).job_template(job_template_id)
    except aap.AAPError as e:
        raise Conflict(f"Couldn't check the job template: {e.message}") from e
    if extra_vars and not tpl["ask_variables_on_launch"]:
        raise Unprocessable(f"Job template '{tpl['name']}' doesn't accept variables at launch. Turn on 'Prompt on launch' "
                            "for variables in AAP, or link it without variables.")
    rb = t("activity_runbooks")
    values = dict(integration_id=integration_id, job_template_id=tpl["id"], job_template_name=tpl["name"][:250],
                  extra_vars=extra_vars, timeout_minutes=timeout_minutes, launch_mode="approve",
                  linked_by=ctx.me.user_id, linked_at=now())
    old = _runbook(ctx, activity_id)
    if old:
        new = dict(ctx.conn.execute(update(rb).where(rb.c.id == old["id"]).values(**values, version=old["version"] + 1)
                                    .returning(rb)).mappings().one())
    else:
        new = dict(ctx.conn.execute(insert(rb).values(tenant_id=ctx.me.tenant_id, activity_id=activity_id, **values)
                                    .returning(rb)).mappings().one())
    audit.record(ctx, "automation.linked", "Activity", activity_id, event_id=ev["id"],
                 old={"job_template": old["job_template_name"]} if old else None,
                 new={"integration": integ["name"], "job_template_id": tpl["id"], "job_template": tpl["name"],
                      "extra_vars": extra_vars, "timeout_minutes": timeout_minutes})
    log(ctx, ev["id"], "Status", f"{act['activity_code']} linked to Ansible job template '{tpl['name']}' in {integ['name']}",
        activity_id=activity_id)
    return new


def unlink(ctx: Ctx, activity_id: UUID) -> dict:
    act, ev = child_of_event(ctx, "test_activities", activity_id, for_update=True)
    authz.require_manage(ctx, ev)
    rb = _runbook(ctx, activity_id)
    if not rb:
        raise NotFound("This activity isn't linked to a runbook")
    if _active_run(ctx, activity_id):
        raise Conflict("A run is in progress for this activity; cancel it first")
    ru = t("automation_runs")
    if ctx.conn.execute(select(ru.c.id).where(ru.c.runbook_id == rb["id"]).limit(1)).first():
        raise Conflict("This runbook has run already, so its link is kept as part of the record. Link a different "
                       "template to replace it.")
    ctx.conn.execute(t("activity_runbooks").delete().where(t("activity_runbooks").c.id == rb["id"]))
    audit.record(ctx, "automation.unlinked", "Activity", activity_id, event_id=ev["id"], old={"job_template": rb["job_template_name"]})
    return {"unlinked": True}


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def _graph_waiting(ctx: Ctx, ev: dict, activity_id: UUID) -> list[str]:
    from .work import _graph
    nodes, edges = _graph(ctx, ev["id"])
    waiting = dep.can_start(activity_id, nodes, edges)
    if not waiting:
        return []
    a = t("test_activities")
    return [c for (c,) in ctx.conn.execute(select(a.c.activity_code).where(a.c.id.in_(waiting)))]


def run(ctx: Ctx, activity_id: UUID) -> dict:
    """A person approves and launches the linked job template. Returns the run (status Error if AAP refused)."""
    from .work import _is_activity_owner, update_activity
    act, ev = child_of_event(ctx, "test_activities", activity_id, for_update=True)
    if not (_is_activity_owner(ctx, act) or authz.can_manage_event(ctx, ev)):
        raise Forbidden("Only the activity owner or the test manager can approve a run")
    if ctx.me.is_external:
        raise Forbidden("External users can't launch runbooks")
    rb = _runbook(ctx, activity_id)
    if not rb:
        raise NotFound("This activity isn't linked to a runbook")
    if S(ev["lifecycle_status"]) not in (S.MOBILISING, S.READINESS, S.READY, S.LIVE):
        raise Conflict(f"Runbooks can run while a test is being prepared or is live, not while it is {ev['lifecycle_status']}")
    if act["status"] in (A.SUBMITTED.value, A.COMPLETED.value):
        raise Conflict(f"{act['activity_code']} is already {act['status'].lower()}")
    if _active_run(ctx, activity_id):
        raise Conflict("This runbook is already running")
    waiting = _graph_waiting(ctx, ev, activity_id) if act["status"] == A.NOT_STARTED.value else []
    if waiting:
        raise Conflict("Mandatory prerequisites are not complete", [f"Waiting on {c}" for c in waiting])
    integ = _integration(ctx, rb["integration_id"])
    ru = t("automation_runs")
    attempt = (ctx.conn.execute(select(ru.c.attempt).where(ru.c.activity_id == activity_id)
                                .order_by(ru.c.attempt.desc()).limit(1)).scalar() or 0) + 1
    run_id = uuid4()
    variables = {**rb["extra_vars"], "resilienceai_run": str(run_id), "resilienceai_event": ev["event_code"],
                 "resilienceai_activity": act["activity_code"]}
    rec = dict(ctx.conn.execute(insert(ru).values(
        id=run_id, tenant_id=ctx.me.tenant_id, event_id=ev["id"], activity_id=activity_id, runbook_id=rb["id"],
        attempt=attempt, job_template_id=rb["job_template_id"], extra_vars=variables, approved_by=ctx.me.user_id)
        .returning(ru)).mappings().one())
    client = None
    try:
        client = _client(integ)
        job_id = client.launch(rb["job_template_id"], variables)
    except aap.AAPError as e:
        # An unclear answer (timeout, lost connection) may still have started the job: the worker looks for it.
        status = "Launching" if e.status is None else "Error"
        rec = dict(ctx.conn.execute(update(ru).where(ru.c.id == run_id).values(
            status=status, failure_summary=e.message, finished_at=None if status == "Launching" else now())
            .returning(ru)).mappings().one())
        audit.record(ctx, "automation.launch_failed", "Activity", activity_id, event_id=ev["id"],
                     new={"run": str(run_id), "job_template": rb["job_template_name"], "error": e.message})
        log(ctx, ev["id"], "Blocker", f"{act['activity_code']}: Ansible didn't start '{rb['job_template_name']}': {e.message}",
            activity_id=activity_id)
        return {**rec, "message": e.message}
    rec = dict(ctx.conn.execute(update(ru).where(ru.c.id == run_id).values(
        external_job_id=job_id, external_url=client.job_url(job_id), status="Pending").returning(ru)).mappings().one())
    audit.record(ctx, "automation.launched", "Activity", activity_id, event_id=ev["id"],
                 new={"run": str(run_id), "attempt": attempt, "integration": integ["name"], "job_template": rb["job_template_name"],
                      "job": job_id, "extra_vars": variables, "approved_by": ctx.me.display_name})
    if act["status"] in (A.NOT_STARTED.value, A.BLOCKED.value):
        update_activity(ctx, activity_id, {"status": A.IN_PROGRESS.value,
                                           "comment": f"Ansible job {job_id} launched ('{rb['job_template_name']}')"})
    else:
        log(ctx, ev["id"], "Status", f"{act['activity_code']}: Ansible job {job_id} launched ('{rb['job_template_name']}')",
            activity_id=activity_id)
    return rec


def cancel(ctx: Ctx, run_id: UUID, why: str = "cancelled by a person") -> dict:
    from .work import _is_activity_owner
    rec, ev = child_of_event(ctx, "automation_runs", run_id, for_update=True)
    act = row(ctx, "test_activities", rec["activity_id"])
    if not (_is_activity_owner(ctx, act) or authz.can_manage_event(ctx, ev)):
        raise Forbidden("Only the activity owner or the test manager can cancel a run")
    if rec["status"] not in ACTIVE:
        raise Conflict(f"This run has already finished ({rec['status'].lower()})")
    return _cancel(ctx, rec, ev, why)


def _cancel(ctx: Ctx, rec: dict, ev: dict, why: str) -> dict:
    rb = row(ctx, "activity_runbooks", rec["runbook_id"])
    integ = _integration(ctx, rb["integration_id"])
    ru = t("automation_runs")
    problem = None
    if rec["external_job_id"]:
        try:
            _client(integ).cancel(rec["external_job_id"])
        except aap.AAPError as e:
            problem = e.message
    new = dict(ctx.conn.execute(update(ru).where(ru.c.id == rec["id"]).values(
        cancel_requested_by=ctx.me.user_id, failure_summary=problem or rec["failure_summary"]).returning(ru)).mappings().one())
    audit.record(ctx, "automation.cancel_requested", "Activity", rec["activity_id"], event_id=ev["id"],
                 new={"run": str(rec["id"]), "job": rec["external_job_id"], "why": why, "problem": problem})
    log(ctx, ev["id"], "Decision", f"Ansible job {rec['external_job_id'] or '(not started)'} cancellation requested: {why}"
        + (f" (Ansible said: {problem})" if problem else ""), activity_id=rec["activity_id"])
    return {**new, "message": problem}


def cancel_for_event(ctx: Ctx, event_id: UUID, why: str) -> int:
    """Pause, abort or cancel of the test: stop every running job (the kill switch)."""
    ru = t("automation_runs")
    runs = [dict(r) for r in ctx.conn.execute(select(ru).where(and_(ru.c.event_id == event_id, ru.c.status.in_(ACTIVE)))).mappings()]
    if not runs:
        return 0
    ev = row(ctx, "test_events", event_id)
    for r in runs:
        _cancel(ctx, r, ev, why)
    return len(runs)


# ---------------------------------------------------------------------------
# Following a job (worker, webhook)
# ---------------------------------------------------------------------------
def _automation_ctx(conn, tenant_id: UUID, integ: dict) -> Ctx:
    u = t("users")
    name = conn.execute(select(u.c.display_name).where(u.c.id == integ["user_id"])).scalar()
    return Ctx(conn=conn, correlation_id=uuid4(), me=Principal(
        tenant_id=tenant_id, user_id=integ["user_id"], display_name=name or integ["name"], roles=frozenset(),
        is_external=False, is_automation=True))


def _iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def sync(conn, tenant_id: UUID, rec: dict) -> dict:
    """Bring one run up to date with AAP, and the activity with it."""
    rb = dict(conn.execute(select(t("activity_runbooks")).where(t("activity_runbooks").c.id == rec["runbook_id"])).mappings().one())
    integ = dict(conn.execute(select(t("automation_integrations")).where(t("automation_integrations").c.id == rb["integration_id"]))
                 .mappings().one())
    ctx = _automation_ctx(conn, tenant_id, integ)
    ru = t("automation_runs")
    try:
        client = _client(integ)
        if rec["external_job_id"] is None:                 # the launch answer was lost: find the job by its marker
            found = client.find_job(str(rec["id"]))
            if found is None:
                if now() - rec["launched_at"] > LAUNCH_GRACE:
                    return _finish(ctx, rec, "Lost", None, "Ansible never confirmed the launch and no job carries this run's "
                                                            "marker; nothing ran. Press Run again.")
                return rec
            rec = dict(conn.execute(update(ru).where(ru.c.id == rec["id"]).values(
                external_job_id=found, external_url=client.job_url(found), status="Pending").returning(ru)).mappings().one())
        job = client.job(rec["external_job_id"])
    except aap.AAPError as e:
        conn.execute(update(ru).where(ru.c.id == rec["id"]).values(last_checked_at=now()))
        logger.warning("couldn't check Ansible job for run %s: %s", rec["id"], e.message)
        return rec
    status = STATUS.get(job.status, rec["status"])
    values = {"last_checked_at": now(), "started_at": rec["started_at"] or _iso(job.started)}
    if status in ("Pending", "Running"):
        rec = dict(conn.execute(update(ru).where(ru.c.id == rec["id"]).values(status=status, **values).returning(ru)).mappings().one())
        _check_timeout(ctx, rec, rb)
        return rec
    summary = client.failure_summary(job.id) if status in ("Failed", "Error") else None
    output = None
    if status == "Successful":
        try:
            output = client.stdout(job.id)
        except aap.AAPError as e:
            logger.warning("couldn't fetch output of Ansible job %s: %s", job.id, e.message)
    conn.execute(update(ru).where(ru.c.id == rec["id"]).values(**values, finished_at=_iso(job.finished) or now()))
    rec = {**rec, **values}
    return _finish(ctx, rec, status, output, summary, elapsed=job.elapsed)


def _finish(ctx: Ctx, rec: dict, status: str, output: bytes | None, summary: str | None, elapsed: float | None = None) -> dict:
    from . import evidence
    from .work import update_activity
    conn, ru = ctx.conn, t("automation_runs")
    ev = row(ctx, "test_events", rec["event_id"])
    act = row(ctx, "test_activities", rec["activity_id"])
    rb = row(ctx, "activity_runbooks", rec["runbook_id"])
    job = rec["external_job_id"]
    took = f" in {round(elapsed / 60)} min" if elapsed and elapsed >= 60 else f" in {round(elapsed)} s" if elapsed else ""
    values: dict = {"status": status, "finished_at": rec.get("finished_at") or now(), "failure_summary": summary}
    if status == "Successful" and output is not None:
        try:
            ev_item = evidence.upload(ctx, activity_id=act["id"], readiness_item_id=None,
                                      filename=f"ansible-job-{job}-{rb['job_template_name'][:60]}.log".replace("/", "-"),
                                      data=output or b"(no output)", content_type="text/plain", evidence_type="Log",
                                      client_sha256=None, supersedes_id=None)
            values["evidence_id"] = ev_item["id"]
        except Exception as e:  # noqa: BLE001 - the run still finished; the output can be fetched from AAP
            logger.warning("couldn't store output of Ansible job %s as evidence: %s", job, e)
    new = dict(conn.execute(update(ru).where(ru.c.id == rec["id"]).values(**values).returning(ru)).mappings().one())
    audit.record(ctx, f"automation.{status.lower()}", "Activity", act["id"], event_id=ev["id"],
                 new={"run": str(rec["id"]), "job": job, "job_template": rb["job_template_name"], "summary": summary,
                      "evidence": str(values.get("evidence_id")) if values.get("evidence_id") else None})
    workable = S(ev["lifecycle_status"]) not in (S.CLOSURE, S.CLOSED, S.CANCELLED, S.DRAFT, S.PLANNED)
    try:
        if status == "Successful" and workable:
            if act["status"] == A.BLOCKED.value:
                act = update_activity(ctx, act["id"], {"status": A.IN_PROGRESS.value})
            if act["status"] == A.IN_PROGRESS.value:
                update_activity(ctx, act["id"], {"status": A.SUBMITTED.value,
                                                 "comment": f"Ansible job {job} succeeded{took}; its output is attached as evidence"})
        elif status != "Successful" and workable and act["status"] in (A.IN_PROGRESS.value, A.NOT_STARTED.value):
            why = {"Canceled": "cancelled in Ansible", "Lost": "the Ansible launch wasn't confirmed"}.get(status, "failed")
            reason = f"Ansible job {job or ''} {why}".replace("  ", " ") + (f": {summary}" if summary else "")
            update_activity(ctx, act["id"], {"status": A.BLOCKED.value, "blocked_reason": reason[:1000]})
    except Exception as e:  # noqa: BLE001 - e.g. someone moved the activity by hand meanwhile; the run is still recorded
        logger.warning("couldn't update activity %s after Ansible job %s: %s", act["activity_code"], job, e)
        log(ctx, ev["id"], "Status", f"{act['activity_code']}: Ansible job {job} {status.lower()}{took}", activity_id=act["id"])
    if status != "Successful":
        owner = ctx.conn.execute(select(t("test_stakeholders").c.user_id).where(
            t("test_stakeholders").c.id == act["stakeholder_id"])).scalar() if act["stakeholder_id"] else None
        notifications.enqueue(ctx, "critical_issue", event=ev, activity_id=act["id"],
                              recipients=[owner, ev["test_manager_user_id"]], actor=None,
                              subject=f"[{ev['event_code']}] {act['activity_code']}: Ansible job {job or ''} {status.lower()}",
                              body=(summary or "See the job in Ansible Automation Platform.")
                                   + (f" Job: {new['external_url']}" if new.get("external_url") else ""),
                              dedupe=f"run:{rec['id']}:{status}")
    return new


def _check_timeout(ctx: Ctx, rec: dict, rb: dict) -> None:
    if rec["timeout_notified"] or now() - rec["launched_at"] < timedelta(minutes=rb["timeout_minutes"]):
        return
    ev = row(ctx, "test_events", rec["event_id"])
    act = row(ctx, "test_activities", rec["activity_id"])
    owner = ctx.conn.execute(select(t("test_stakeholders").c.user_id).where(
        t("test_stakeholders").c.id == act["stakeholder_id"])).scalar() if act["stakeholder_id"] else None
    ctx.conn.execute(update(t("automation_runs")).where(t("automation_runs").c.id == rec["id"]).values(timeout_notified=True))
    notifications.enqueue(ctx, "critical_issue", event=ev, activity_id=act["id"], recipients=[owner, ev["test_manager_user_id"]],
                          actor=None, subject=f"[{ev['event_code']}] {act['activity_code']}: Ansible job {rec['external_job_id']} "
                                              f"still running after {rb['timeout_minutes']} min",
                          body="It hasn't been stopped. Check it in Ansible and cancel it from ResilienceAI if it's stuck.",
                          dedupe=f"run-timeout:{rec['id']}")
    log(ctx, ev["id"], "Blocker", f"{act['activity_code']}: Ansible job {rec['external_job_id']} has run longer than its "
        f"{rb['timeout_minutes']}-minute limit", activity_id=act["id"])


def sync_active(tenant_id: UUID) -> dict:
    """Worker job: check every unfinished run."""
    out = {"checked": 0, "finished": 0}
    with tenant_transaction(tenant_id) as conn:
        ru = t("automation_runs")
        runs = [dict(r) for r in conn.execute(select(ru).where(ru.c.status.in_(ACTIVE)).order_by(ru.c.launched_at)
                                              .with_for_update(skip_locked=True)).mappings()]
        for r in runs:
            new = sync(conn, tenant_id, r)
            out["checked"] += 1
            out["finished"] += new["status"] in FINISHED
    return out


def webhook(integration_id: UUID, token: str, body: dict) -> dict:
    """AAP job notification: a doorbell. We only learn which job changed, then ask AAP for its real state."""
    with engine().connect() as c:
        tenant_id = c.execute(text("SELECT tenant_for_integration(:i)"), {"i": integration_id}).scalar()
    if tenant_id is None:
        raise NotFound("Unknown integration")
    with tenant_transaction(tenant_id) as conn:
        integ = dict(conn.execute(select(t("automation_integrations")).where(
            t("automation_integrations").c.id == integration_id)).mappings().one())
        if not hmac.compare_digest(hashlib.sha256((token or "").encode()).hexdigest(), integ["webhook_secret_hash"]):
            raise Forbidden("Wrong webhook secret")
        job_id = body.get("id") if isinstance(body.get("id"), int) else (body.get("job") or {}).get("id") \
            if isinstance(body.get("job"), dict) else body.get("job_id")
        if not isinstance(job_id, int):
            raise Unprocessable("The notification doesn't say which job it is about")
        ru, rb = t("automation_runs"), t("activity_runbooks")
        rec = conn.execute(select(ru).join(rb, rb.c.id == ru.c.runbook_id).where(and_(
            rb.c.integration_id == integration_id, ru.c.external_job_id == job_id)).with_for_update()).mappings().first()
        if rec is None:
            return {"tracked": False}               # a job ResilienceAI didn't launch: ignore it
        rec = dict(rec)
        if rec["status"] in FINISHED:
            return {"tracked": True, "status": rec["status"]}
        new = sync(conn, tenant_id, rec)
        return {"tracked": True, "status": new["status"]}


# ---------------------------------------------------------------------------
# For the activities list
# ---------------------------------------------------------------------------
def for_activities(ctx: Ctx, activity_ids: list[UUID]) -> dict[UUID, dict]:
    if not activity_ids:
        return {}
    rb, ru, i, u = t("activity_runbooks"), t("automation_runs"), t("automation_integrations"), t("users")
    out: dict[UUID, dict] = {}
    for r in ctx.conn.execute(select(rb, i.c.name.label("integration")).join(i, i.c.id == rb.c.integration_id)
                              .where(rb.c.activity_id.in_(activity_ids))).mappings():
        out[r["activity_id"]] = {"id": r["id"], "integration": r["integration"], "integration_id": r["integration_id"],
                                 "job_template_id": r["job_template_id"], "job_template": r["job_template_name"],
                                 "extra_vars": r["extra_vars"], "timeout_minutes": r["timeout_minutes"],
                                 "launch_mode": r["launch_mode"], "last_run": None, "runs": 0}
    if out:
        for r in ctx.conn.execute(select(ru, u.c.display_name.label("approved_by_name")).join(u, u.c.id == ru.c.approved_by)
                                  .where(ru.c.activity_id.in_(list(out))).order_by(ru.c.activity_id, ru.c.attempt.desc())).mappings():
            o = out[r["activity_id"]]
            o["runs"] += 1
            if o["last_run"] is None:
                o["last_run"] = {k: r[k] for k in ("id", "attempt", "status", "external_job_id", "external_url", "launched_at",
                                                   "started_at", "finished_at", "failure_summary", "evidence_id",
                                                   "approved_by_name")}
    return out


def _utc(dt):
    return dt.astimezone(timezone.utc) if dt else None
