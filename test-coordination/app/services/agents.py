"""Governed access for the bank's own AI agents.

The bank builds and runs its agents (for example a "DBA agent"); ResilienceAI decides what they may do here.

- **Identity.** Each agent has its own person record ("DBA Agent (agent)"), a stated purpose, a named accountable
  person, an expiring key (shown once, stored as a hash) and a set of scopes. Agents are created only by the
  operations script. It joins a test as a stakeholder and is assigned activities like anyone else.
- **Scopes.** Deny by default: an agent can call only the endpoints its scopes allow, listed in ROUTES below.
- **Never decides.** An agent can't approve, review, accept evidence, change a test's stage, link or launch a runbook.
  It may *ask* for its activity's runbook to run; its accountable person or the test manager approves or declines.
- **Accountability.** Its blockers and run requests notify its accountable person. Every call (allowed or refused) is
  in the agent action log; changes are in the audit trail under the agent's name.
- **Off switch.** Administrators, risk and the accountable person can suspend an agent at once; its key stops
  working. Keys expire, and AGENT_RATE_PER_MIN limits how fast an agent can call.
"""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, insert, select, text, update

from .. import authz, config
from ..db import engine, t, tenant_transaction
from ..domain.enums import Role
from ..errors import Conflict, DomainError, Forbidden, NotFound, Unprocessable
from ..security import Ctx
from . import audit, notifications
from .common import child_of_event, log, now, row

PREFIX = "ag."
SCOPES = ("read", "acknowledge", "update_status", "report_blocker", "post_update", "attach_evidence", "request_run")
REQUEST_MINUTES = 30
_ID = r"[0-9a-fA-F-]{36}"
# (method, path pattern, scope). Anything not listed is refused for agents.
ROUTES: list[tuple[str, re.Pattern, str]] = [(m, re.compile(f"^/api/v1{p}$"), s) for m, p, s in [
    ("GET", "/me", "read"),
    ("GET", "/me/workspace", "read"),
    ("GET", f"/test-events/{_ID}", "read"),
    ("GET", f"/test-events/{_ID}/(activities|dependencies|readiness|timeline|evidence)", "read"),
    ("POST", f"/test-stakeholders/{_ID}/respond", "acknowledge"),
    ("PATCH", f"/test-activities/{_ID}", "update_status"),            # blockers additionally need report_blocker
    ("POST", f"/test-activities/{_ID}/updates", "post_update"),
    ("POST", f"/test-activities/{_ID}/evidence/upload", "attach_evidence"),
    ("POST", f"/test-activities/{_ID}/runbook/run-requests", "request_run"),
]]


class AgentError(DomainError):
    pass


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def required_scope(method: str, path: str) -> str | None:
    for m, pat, scope in ROUTES:
        if m == method and pat.match(path):
            return scope
    return None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def _tenant(key: str) -> UUID | None:
    with engine().connect() as c:
        return c.execute(text("SELECT agent_tenant(:h)"), {"h": key_hash(key)}).scalar()


def resolve(key: str) -> tuple[UUID, str, dict]:
    """(organisation, the agent's subject, the agent) for a valid key; raises AgentError(401/403/429) otherwise."""
    tid = _tenant(key)
    if tid is None:
        raise AgentError("Unknown agent key", status_code=401)
    with tenant_transaction(tid) as conn:
        ag, u = t("agent_identities"), t("users")
        a = conn.execute(select(ag).where(ag.c.key_hash == key_hash(key))).mappings().one()
        if a["suspended_at"]:
            raise AgentError(f"This agent is suspended: {a['suspended_reason'] or 'no reason given'}", status_code=403)
        if a["expires_at"] < now():
            raise AgentError("This agent's key has expired; ask operations to rotate it", status_code=401)
        person = conn.execute(select(u.c.is_active).where(u.c.id == a["accountable_user_id"])).scalar()
        if not person:
            raise AgentError("This agent's accountable person is no longer active, so it can't act", status_code=403)
        aa = t("agent_actions")
        recent = conn.execute(select(func.count()).where(and_(aa.c.agent_id == a["id"],
                                                             aa.c.at > now() - timedelta(minutes=1)))).scalar()
        if recent >= config.settings.agent_rate_per_min:
            raise AgentError(f"Too many calls: at most {config.settings.agent_rate_per_min} a minute", status_code=429)
        conn.execute(update(ag).where(ag.c.id == a["id"]).values(last_used_at=now()))
        subject = conn.execute(select(u.c.idp_subject).where(u.c.id == a["user_id"])).scalar()
        return tid, subject, dict(a)


def record_call(key: str, method: str, path: str, status_code: int) -> None:
    """Middleware: every call an agent makes, allowed or not, goes into the agent action log."""
    tid = _tenant(key)
    if tid is None:
        return
    outcome = "done" if status_code < 400 else "refused" if status_code in (401, 403, 404, 409, 422, 429) else "failed"
    with tenant_transaction(tid) as conn:
        ag = t("agent_identities")
        aid = conn.execute(select(ag.c.id).where(ag.c.key_hash == key_hash(key))).scalar()
        conn.execute(insert(t("agent_actions")).values(tenant_id=tid, agent_id=aid, method=method, path=path[:300],
                                                      status_code=status_code, outcome=outcome))


def check_call(ctx: Ctx, method: str, path: str) -> None:
    scope = required_scope(method, path)
    if scope is None:
        raise Forbidden("Agents can't do this. They can read their tests, update their own activities and ask for "
                        "runs; people approve and decide.")
    if scope not in ctx.me.agent_scopes:
        raise Forbidden(f"This agent doesn't have the '{scope}' permission")


# ---------------------------------------------------------------------------
# Run requests: the agent asks, a person decides
# ---------------------------------------------------------------------------
def _agent(ctx: Ctx, agent_id: UUID) -> dict:
    return row(ctx, "agent_identities", agent_id)


def accountable_for(ctx: Ctx, act: dict) -> bool:
    """Is the current person accountable for the agent that owns this activity?"""
    if not act.get("stakeholder_id") or ctx.me.is_agent:
        return False
    s, ag = t("test_stakeholders"), t("agent_identities")
    return ctx.conn.execute(select(ag.c.id).join(s, s.c.user_id == ag.c.user_id).where(and_(
        s.c.id == act["stakeholder_id"], ag.c.accountable_user_id == ctx.me.user_id))).first() is not None


def request_run(ctx: Ctx, activity_id: UUID, reason: str) -> dict:
    from .automation import _active_run, _runbook
    from .work import _is_activity_owner
    if not ctx.me.is_agent:
        raise Forbidden("People run runbooks directly; requests are for agents")
    act, ev = child_of_event(ctx, "test_activities", activity_id, for_update=True)
    if not _is_activity_owner(ctx, act):
        raise Forbidden("An agent can ask only for its own activities' runbooks")
    if len((reason or "").strip()) < 10:
        raise Unprocessable("Say why the runbook should run now (what you checked and what you expect it to do)")
    rb = _runbook(ctx, activity_id)
    if not rb:
        raise NotFound("This activity isn't linked to a runbook")
    if _active_run(ctx, activity_id):
        raise Conflict("The runbook is already running")
    _expire(ctx)
    rr = t("automation_run_requests")
    if ctx.conn.execute(select(rr.c.id).where(and_(rr.c.activity_id == activity_id, rr.c.status == "Pending"))).first():
        raise Conflict("A request for this runbook is already waiting for a decision")
    from .automation import _launch, preflight
    from .autonomy import permits
    preflight(ctx, act, ev)                     # it couldn't run now anyway: say why rather than ask a person
    agent = _agent(ctx, ctx.me.agent_id)
    rec = dict(ctx.conn.execute(insert(rr).values(tenant_id=ctx.me.tenant_id, event_id=ev["id"], activity_id=activity_id,
                                                  agent_id=agent["id"], reason=reason.strip()[:2000]).returning(rr)).mappings().one())
    pol, rule, why = permits(ctx.conn, kind="agent_request", integration_id=rb["integration_id"],
                             job_template_id=rb["job_template_id"], event=ev, agent_id=agent["id"])
    if pol is not None:                         # the CISO-approved policy covers it: no one needs to press Approve
        run_rec = _launch(ctx, act, ev, rb, approved_by=None, policy=pol, rule=rule,
                          note=f"asked by {agent['name']}: {rec['reason']}", also_tell=[agent["accountable_user_id"]])
        rec = dict(ctx.conn.execute(update(rr).where(rr.c.id == rec["id"]).values(
            status="AutoApproved", policy_id=pol["id"], run_id=run_rec["id"]).returning(rr)).mappings().one())
        audit.record(ctx, "agent.run_auto_approved", "Activity", activity_id, event_id=ev["id"],
                     new={"request": str(rec["id"]), "policy": pol["version"], "rule": rule["name"], "run": str(run_rec["id"])})
        return {**rec, "run": run_rec, "policy": {"version": pol["version"], "rule": rule["name"]}}
    audit.record(ctx, "agent.run_requested", "Activity", activity_id, event_id=ev["id"],
                 new={"request": str(rec["id"]), "job_template": rb["job_template_name"], "reason": rec["reason"]})
    log(ctx, ev["id"], "Status", f"{agent['name']} asks to run '{rb['job_template_name']}' for {act['activity_code']}: "
        f"{rec['reason']}", activity_id=activity_id)
    notifications.enqueue(ctx, "approval_requested", event=ev, activity_id=activity_id,
                          recipients=[agent["accountable_user_id"], ev["test_manager_user_id"]],
                          subject=f"[{ev['event_code']}] {agent['name']} asks to run '{rb['job_template_name']}' for {act['activity_code']}",
                          body=f"Reason given: {rec['reason']}. Approve or decline it on the test's Activities tab. "
                               f"The request expires after {REQUEST_MINUTES} minutes. ({why}.)",
                          dedupe=f"run-request:{rec['id']}")
    return {**rec, "needs_approval_because": why}


def decide(ctx: Ctx, request_id: UUID, approve: bool, note: str | None) -> dict:
    from .automation import run
    if ctx.me.is_agent:
        raise Forbidden("Agents can't approve runs")
    rec, ev = child_of_event(ctx, "automation_run_requests", request_id, for_update=True)
    act = row(ctx, "test_activities", rec["activity_id"])
    if not (authz.can_manage_event(ctx, ev) or accountable_for(ctx, act)):
        raise Forbidden("Only the agent's accountable person or the test manager can decide this")
    _expire(ctx)
    rec = row(ctx, "automation_run_requests", request_id)
    if rec["status"] != "Pending":
        raise Conflict(f"This request is already {rec['status'].lower()}")
    if not approve and not (note or "").strip():
        raise Unprocessable("Say why you're declining, so the agent (and its owner) know what to do instead")
    rr = t("automation_run_requests")
    agent = _agent(ctx, rec["agent_id"])
    run_rec = None
    if approve:
        run_rec = run(ctx, rec["activity_id"])           # the person's approval is what launches it
    new = dict(ctx.conn.execute(update(rr).where(rr.c.id == request_id).values(
        status="Approved" if approve else "Declined", decided_by=ctx.me.user_id, decided_at=now(),
        decision_note=(note or "").strip() or None, run_id=run_rec["id"] if run_rec else None).returning(rr)).mappings().one())
    audit.record(ctx, "agent.run_approved" if approve else "agent.run_declined", "Activity", rec["activity_id"],
                 event_id=ev["id"], new={"request": str(request_id), "agent": agent["name"], "note": note,
                                         "run": str(run_rec["id"]) if run_rec else None})
    if not approve:
        log(ctx, ev["id"], "Decision", f"{ctx.me.display_name} declined {agent['name']}'s request to run the runbook for "
            f"{act['activity_code']}: {note}", activity_id=act["id"])
    return {**new, "run": run_rec}


def _expire(ctx: Ctx) -> None:
    rr = t("automation_run_requests")
    ctx.conn.execute(update(rr).where(and_(rr.c.status == "Pending",
                                           rr.c.requested_at < now() - timedelta(minutes=REQUEST_MINUTES))).values(status="Expired"))


def pending_for(ctx: Ctx, activity_ids: list[UUID]) -> dict[UUID, dict]:
    if not activity_ids:
        return {}
    _expire(ctx)
    rr, ag = t("automation_run_requests"), t("agent_identities")
    return {r["activity_id"]: {"id": r["id"], "agent": r["agent"], "reason": r["reason"], "requested_at": r["requested_at"],
                               "accountable_user_id": r["accountable_user_id"]}
            for r in ctx.conn.execute(select(rr, ag.c.name.label("agent"), ag.c.accountable_user_id).join(ag, ag.c.id == rr.c.agent_id)
                                      .where(and_(rr.c.activity_id.in_(activity_ids), rr.c.status == "Pending"))).mappings()}


# ---------------------------------------------------------------------------
# Oversight
# ---------------------------------------------------------------------------
OVERSIGHT = (Role.ADMIN, Role.RISK, Role.AUDITOR)


def overview(ctx: Ctx) -> dict:
    ag, u, aa = t("agent_identities"), t("users"), t("agent_actions")
    acc = u.alias("acc")
    q = select(ag, acc.c.display_name.label("accountable"), u.c.display_name.label("person")) \
        .join(acc, acc.c.id == ag.c.accountable_user_id).join(u, u.c.id == ag.c.user_id)
    if not ctx.me.has(*OVERSIGHT):
        q = q.where(ag.c.accountable_user_id == ctx.me.user_id)
    agents = [dict(r) for r in ctx.conn.execute(q.order_by(ag.c.name)).mappings()]
    if not agents and not ctx.me.has(*OVERSIGHT):
        raise Forbidden("Only administrators, risk, audit and an agent's accountable person can see agents")
    since = now() - timedelta(hours=24)
    counts = {(r.agent_id, r.outcome): r.n for r in ctx.conn.execute(
        select(aa.c.agent_id, aa.c.outcome, func.count().label("n")).where(aa.c.at > since).group_by(aa.c.agent_id, aa.c.outcome))}
    for a in agents:
        a.pop("key_hash", None)
        a["calls_24h"] = sum(n for (aid, _), n in counts.items() if aid == a["id"])
        a["refused_24h"] = counts.get((a["id"], "refused"), 0)
        a["expired"] = a["expires_at"] < now()
        a["can_suspend"] = not a["suspended_at"] and (ctx.me.has(Role.ADMIN, Role.RISK) or a["accountable_user_id"] == ctx.me.user_id)
    ids = [a["id"] for a in agents]
    recent = [dict(r) for r in ctx.conn.execute(
        select(aa, ag.c.name.label("agent")).join(ag, ag.c.id == aa.c.agent_id).where(aa.c.agent_id.in_(ids))
        .order_by(aa.c.at.desc()).limit(40)).mappings()] if ids else []
    return {"agents": agents, "recent": recent}


def suspend(ctx: Ctx, agent_id: UUID, reason: str) -> dict:
    a = _agent(ctx, agent_id)
    if not (ctx.me.has(Role.ADMIN, Role.RISK) or a["accountable_user_id"] == ctx.me.user_id) or ctx.me.is_agent:
        raise Forbidden("Only administrators, risk or the agent's accountable person can suspend it")
    if a["suspended_at"]:
        raise Conflict("This agent is already suspended")
    if not (reason or "").strip():
        raise Unprocessable("Say why the agent is being suspended")
    ag = t("agent_identities")
    ctx.conn.execute(update(ag).where(ag.c.id == agent_id).values(suspended_at=now(), suspended_by=ctx.me.user_id,
                                                                  suspended_reason=reason.strip()))
    rr = t("automation_run_requests")
    ctx.conn.execute(update(rr).where(and_(rr.c.agent_id == agent_id, rr.c.status == "Pending")).values(status="Expired"))
    audit.record(ctx, "agent.suspended", "Agent", agent_id, new={"agent": a["name"], "reason": reason})
    return {"suspended": True}
