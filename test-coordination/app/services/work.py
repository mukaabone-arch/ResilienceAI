"""Activities, dependencies, readiness, evidence, issues, remediation, measurements,
approvals and live coordination (Epics 4, 5, 7, 8)."""
from __future__ import annotations

import re
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, insert, select, update

from .. import authz
from ..db import t
from ..domain import activity_workflow as aw
from ..domain import dependencies as dep
from ..domain import measurements as msr
from ..domain.enums import ActivityStatus as A
from ..domain.enums import ApprovalType as AT
from ..domain.enums import LifecycleStatus as S
from ..domain.enums import ReadinessStatus as RS
from ..domain.enums import ReviewStatus as R
from ..domain.enums import Role
from ..errors import Conflict, DomainError, Forbidden, Unprocessable
from ..security import Ctx
from . import audit, notifications
from .common import check_version, child_of_event, event, log, next_seq, now, participant_ids, row
from .events import closure_blockers, readiness_gate, supersede_approvals, valid_approval

WORKABLE = {S.MOBILISING, S.READINESS, S.READY, S.LIVE, S.PAUSED, S.CLOSURE}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _require_workable(ev: dict) -> None:
    if S(ev["lifecycle_status"]) not in WORKABLE:
        raise Conflict(f"Activities cannot be updated while the event is {ev['lifecycle_status']}")


def _stakeholder_user(ctx: Ctx, stakeholder_id: UUID | None) -> tuple[UUID | None, UUID | None]:
    if not stakeholder_id:
        return None, None
    s = t("test_stakeholders")
    r = ctx.conn.execute(select(s.c.user_id, s.c.alternate_user_id).where(s.c.id == stakeholder_id)).first()
    return (r.user_id, r.alternate_user_id) if r else (None, None)


def _is_activity_owner(ctx: Ctx, act: dict) -> bool:
    return ctx.me.user_id in _stakeholder_user(ctx, act["stakeholder_id"])


def _assert_stakeholder_of(ctx: Ctx, event_id: UUID, stakeholder_id: UUID) -> None:
    st = row(ctx, "test_stakeholders", stakeholder_id)
    if st["event_id"] != event_id or not st["is_active"]:
        raise Unprocessable("Stakeholder must be an active participant of this event")


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------
def create_activity(ctx: Ctx, event_id: UUID, data: dict) -> dict:
    ev = event(ctx, event_id)
    authz.require_manage(ctx, ev)
    if S(ev["lifecycle_status"]) in (S.CLOSED, S.CANCELLED):
        raise Conflict("Event is closed")
    if data.get("stakeholder_id"):
        _assert_stakeholder_of(ctx, event_id, data["stakeholder_id"])
    a = t("test_activities")
    if ctx.conn.execute(select(a.c.id).where(and_(a.c.event_id == event_id, a.c.activity_code == data["activity_code"]))).first():
        raise Conflict(f"Activity code {data['activity_code']} already exists in this event")
    # Review is required iff a reviewer is designated. review_status only gates once Submitted.
    if data.get("review_required") and not data.get("reviewer_user_id"):
        raise Unprocessable("reviewer_user_id is required when review_required is true")
    if data.get("reviewer_user_id") and data.get("stakeholder_id"):
        if data["reviewer_user_id"] in _stakeholder_user(ctx, data["stakeholder_id"]):
            raise Unprocessable("The reviewer cannot be the activity owner")
    review = R.PENDING.value if data.get("reviewer_user_id") else R.NOT_REQUIRED.value
    fields = {k: data.get(k) for k in ("stakeholder_id", "activity_code", "title", "description", "category",
                                       "planned_start_at", "due_at", "reviewer_user_id")}
    r = dict(ctx.conn.execute(insert(a).values(
        tenant_id=ctx.me.tenant_id, event_id=event_id, priority=data.get("priority", "Medium"),
        is_mandatory=data.get("is_mandatory", True), evidence_required=data.get("evidence_required", False),
        review_status=review, **fields).returning(a)).mappings().one())
    audit.record(ctx, "activity.created", "Activity", r["id"], event_id=event_id, new=r)
    return r


def list_activities(ctx: Ctx, event_id: UUID) -> list[dict]:
    ev = event(ctx, event_id)
    a, d, s, u = t("test_activities"), t("test_dependencies"), t("test_stakeholders"), t("users")
    acts = [dict(r) for r in ctx.conn.execute(
        select(a, s.c.user_id.label("owner_user_id"), u.c.display_name.label("owner_name"))
        .outerjoin(s, s.c.id == a.c.stakeholder_id).outerjoin(u, u.c.id == s.c.user_id)
        .where(a.c.event_id == event_id).order_by(a.c.activity_code)).mappings()]
    edges = [dep.Edge(r.predecessor_activity_id, r.successor_activity_id, r.is_mandatory)
             for r in ctx.conn.execute(select(d).where(d.c.event_id == event_id))]
    states = dep.propagate([dep.ActivityNode(x["id"], A(x["status"]), x["due_at"]) for x in acts], edges, now())
    for x in acts:
        st = states[x["id"]]
        x["dependency_state"], x["blocking_activities"] = st.state, st.blocking_activities
        x["is_overdue"] = bool(x["due_at"] and x["due_at"] < now() and x["status"] != "Completed")
    if ctx.me.is_external:  # vendors see only their own assignments
        acts = [x for x in acts if x["owner_user_id"] == ctx.me.user_id]
    _ = ev
    return acts


def _graph(ctx: Ctx, event_id: UUID) -> tuple[list[dep.ActivityNode], list[dep.Edge]]:
    a, d = t("test_activities"), t("test_dependencies")
    nodes = [dep.ActivityNode(r.id, A(r.status), r.due_at) for r in ctx.conn.execute(
        select(a.c.id, a.c.status, a.c.due_at).where(a.c.event_id == event_id))]
    edges = [dep.Edge(r.predecessor_activity_id, r.successor_activity_id, r.is_mandatory)
             for r in ctx.conn.execute(select(d).where(d.c.event_id == event_id))]
    return nodes, edges


def update_activity(ctx: Ctx, activity_id: UUID, data: dict) -> dict:
    act, ev = child_of_event(ctx, "test_activities", activity_id, for_update=True)
    check_version(act["version"], data.get("expected_version"))
    a = t("test_activities")
    values: dict = {}
    events_to_log: list[tuple[str, str]] = []

    if "stakeholder_id" in data and data["stakeholder_id"] != act["stakeholder_id"]:
        authz.require_manage(ctx, ev)
        _assert_stakeholder_of(ctx, ev["id"], data["stakeholder_id"])
        values["stakeholder_id"] = data["stakeholder_id"]
        ctx.conn.execute(insert(t("activity_assignment_history")).values(
            tenant_id=ctx.me.tenant_id, activity_id=activity_id, from_stakeholder_id=act["stakeholder_id"],
            to_stakeholder_id=data["stakeholder_id"], reason=data.get("comment"), changed_by=ctx.me.user_id))
    if "due_at" in data and data["due_at"] != act["due_at"]:
        authz.require_manage(ctx, ev)
        values["due_at"] = data["due_at"]

    if data.get("status") and data["status"] != act["status"]:
        if not (_is_activity_owner(ctx, act) or authz.can_manage_event(ctx, ev)):
            raise Forbidden("Only the activity owner, their alternate or the test manager can update status")
        _require_workable(ev)
        requested = A(data["status"])
        if requested == A.IN_PROGRESS and act["status"] == A.NOT_STARTED.value:
            nodes, edges = _graph(ctx, ev["id"])
            waiting = dep.can_start(activity_id, nodes, edges)
            if waiting:
                codes = [c for (c,) in ctx.conn.execute(select(a.c.activity_code).where(a.c.id.in_(waiting)))]
                raise Conflict("Mandatory prerequisites are not complete", [f"Waiting on {c}" for c in codes])
        if requested == A.SUBMITTED and act["evidence_required"]:
            ev_t = t("test_evidence")
            has = ctx.conn.execute(select(ev_t.c.id).where(and_(
                ev_t.c.activity_id == activity_id, ev_t.c.review_status.in_(["Pending", "Accepted"])))).first()
            if not has:
                raise Conflict("Evidence must be uploaded before submitting this activity")
        # A rejected review re-enters the review cycle on the next submit.
        review_in = R.PENDING if act["reviewer_user_id"] else R.NOT_REQUIRED
        try:
            new_status, new_review = aw.owner_update(A(act["status"]), requested, review_in)
        except aw.ActivityTransitionError as e:
            raise Conflict(str(e)) from e
        values.update(status=new_status.value, review_status=new_review.value)
        if new_status == A.COMPLETED:
            values["completed_at"] = now()
        if new_status == A.BLOCKED:
            if not data.get("blocked_reason"):
                raise Unprocessable("blocked_reason is required when reporting a blocker")
            values["blocked_reason"] = data["blocked_reason"]
            events_to_log.append(("Blocker", f"{act['activity_code']} blocked: {data['blocked_reason']}"))
        elif act["status"] == A.BLOCKED.value:
            values["blocked_reason"] = None
            events_to_log.append(("Status", f"{act['activity_code']} unblocked"))
        else:
            events_to_log.append(("Status", f"{act['activity_code']} → {new_status.value}"))

    if not values:
        return act
    new = dict(ctx.conn.execute(update(a).where(a.c.id == activity_id).values(
        **values, version=act["version"] + 1, updated_at=now()).returning(a)).mappings().one())
    o, n = audit.diff(act, new)
    audit.record(ctx, "activity.updated", "Activity", activity_id, event_id=ev["id"], old=o, new={**n, "comment": data.get("comment")})
    for kind, msg in events_to_log:
        log(ctx, ev["id"], kind, msg + (f". {data['comment']}" if data.get("comment") else ""), activity_id=activity_id)
    if new["status"] == A.BLOCKED.value:
        _notify_downstream_blocked(ctx, ev, new)
    if new["status"] == A.SUBMITTED.value and new["reviewer_user_id"]:
        notifications.enqueue(ctx, "approval_requested", event=ev, activity_id=activity_id, recipients=[new["reviewer_user_id"]],
                              subject=f"[{ev['event_code']}] Review requested: {new['activity_code']} {new['title']}",
                              body="An activity has been submitted for your review.",
                              dedupe=f"review:{activity_id}:v{new['version']}")
    return new


def _notify_downstream_blocked(ctx: Ctx, ev: dict, act: dict) -> None:
    nodes, edges = _graph(ctx, ev["id"])
    affected = dep.downstream([e for e in edges if e.mandatory], act["id"])
    if not affected:
        return
    a, s = t("test_activities"), t("test_stakeholders")
    owners = [r[0] for r in ctx.conn.execute(select(s.c.user_id).join(a, a.c.stakeholder_id == s.c.id).where(a.c.id.in_(affected)))]
    notifications.enqueue(ctx, "dependency_blocked", event=ev, activity_id=act["id"],
                          recipients=owners + [ev["test_manager_user_id"]],
                          subject=f"[{ev['event_code']}] Prerequisite blocked: {act['activity_code']} {act['title']}",
                          body=f"{len(affected)} downstream activit{'y is' if len(affected) == 1 else 'ies are'} affected. "
                               f"Reason: {act['blocked_reason']}",
                          dedupe=f"blocked:{act['id']}:v{act['version']}")


def review_activity(ctx: Ctx, activity_id: UUID, accept: bool, comment: str | None) -> dict:
    act, ev = child_of_event(ctx, "test_activities", activity_id, for_update=True)
    if ctx.me.user_id != act["reviewer_user_id"]:
        raise Forbidden("Only the designated reviewer can review this activity")
    if _is_activity_owner(ctx, act):
        raise Forbidden("Owners cannot review their own activity")
    if not accept and not comment:
        raise Unprocessable("A comment is required when rejecting")
    try:
        st, rv = aw.review(A(act["status"]), R(act["review_status"]), accept)
    except aw.ActivityTransitionError as e:
        raise Conflict(str(e)) from e
    a = t("test_activities")
    new = dict(ctx.conn.execute(update(a).where(a.c.id == activity_id).values(
        status=st.value, review_status=rv.value, completed_at=now() if st == A.COMPLETED else None,
        version=act["version"] + 1, updated_at=now()).returning(a)).mappings().one())
    audit.record(ctx, "activity.reviewed", "Activity", activity_id, event_id=ev["id"],
                 new={"accepted": accept, "comment": comment, "status": st.value})
    log(ctx, ev["id"], "Status", f"{act['activity_code']} review {'accepted' if accept else 'rejected'}"
        + (f": {comment}" if comment else ""), activity_id=activity_id)
    return new


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
def add_dependency(ctx: Ctx, event_id: UUID, data: dict) -> dict:
    ev = event(ctx, event_id)
    authz.require_manage(ctx, ev)
    a, d = t("test_activities"), t("test_dependencies")
    pred, succ = data["predecessor_activity_id"], data["successor_activity_id"]
    owners = {r.id: r.event_id for r in ctx.conn.execute(select(a.c.id, a.c.event_id).where(a.c.id.in_([pred, succ])))}
    if owners.get(pred) != event_id or owners.get(succ) != event_id:
        raise Unprocessable("Both activities must belong to this event")
    _, edges = _graph(ctx, event_id)
    try:
        dep.validate_new_edge(edges, dep.Edge(pred, succ, data.get("is_mandatory", True)),
                              data.get("dependency_type", "finish-to-start"))
    except dep.DependencyError as e:
        raise Unprocessable(str(e)) from e
    r = dict(ctx.conn.execute(insert(d).values(
        tenant_id=ctx.me.tenant_id, event_id=event_id, predecessor_activity_id=pred, successor_activity_id=succ,
        dependency_type=data.get("dependency_type", "finish-to-start"), is_mandatory=data.get("is_mandatory", True)
    ).returning(d)).mappings().one())
    audit.record(ctx, "dependency.added", "Dependency", r["id"], event_id=event_id, new=r)
    return r


def dependency_map(ctx: Ctx, event_id: UUID) -> dict:
    acts = list_activities(ctx, event_id)
    d = t("test_dependencies")
    edges = [dict(r) for r in ctx.conn.execute(select(d).where(d.c.event_id == event_id)).mappings()]
    summary: dict[str, int] = {}
    for x in acts:
        summary[x["dependency_state"]] = summary.get(x["dependency_state"], 0) + 1
    return {"nodes": [{k: x[k] for k in ("id", "activity_code", "title", "status", "dependency_state",
                                         "blocking_activities", "owner_name", "due_at", "is_overdue")} for x in acts],
            "edges": edges, "summary": summary}


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
def create_readiness_item(ctx: Ctx, event_id: UUID, data: dict) -> dict:
    ev = event(ctx, event_id)
    authz.require_manage(ctx, ev)
    if S(ev["lifecycle_status"]) not in (S.DRAFT, S.PLANNED, S.MOBILISING, S.READINESS):
        raise Conflict("Readiness items can only be added before the event is Ready")
    r = t("test_readiness_items")
    out = dict(ctx.conn.execute(insert(r).values(
        tenant_id=ctx.me.tenant_id, event_id=event_id,
        **{k: data.get(k) for k in ("title", "description", "owner_user_id", "activity_id")},
        is_mandatory=data.get("is_mandatory", True), evidence_required=data.get("evidence_required", False)
    ).returning(r)).mappings().one())
    audit.record(ctx, "readiness.item_added", "ReadinessItem", out["id"], event_id=event_id, new=out)
    return out


def update_readiness_item(ctx: Ctx, item_id: UUID, data: dict) -> dict:
    item, ev = child_of_event(ctx, "test_readiness_items", item_id, for_update=True)
    check_version(item["version"], data.get("expected_version"))
    if not (ctx.me.user_id == item["owner_user_id"] or authz.can_manage_event(ctx, ev)):
        raise Forbidden("Only the readiness owner or test manager can update this item")
    authz.require_writer(ctx)
    if S(ev["lifecycle_status"]) in (S.CLOSED, S.CANCELLED, S.CLOSURE):
        raise Conflict("Readiness is closed for this event")
    r = t("test_readiness_items")
    values = {"status": data["status"], "version": item["version"] + 1, "updated_at": now()}
    if "owner_user_id" in data:
        authz.require_manage(ctx, ev)
        values["owner_user_id"] = data["owner_user_id"]
    if data["status"] == RS.READY.value:
        values.update(confirmed_at=now(), confirmed_by=ctx.me.user_id)
    new = dict(ctx.conn.execute(update(r).where(r.c.id == item_id).values(**values).returning(r)).mappings().one())
    audit.record(ctx, "readiness.updated", "ReadinessItem", item_id, event_id=ev["id"],
                 old={"status": item["status"]}, new={"status": new["status"], "comment": data.get("comment")})
    log(ctx, ev["id"], "Blocker" if new["status"] in ("Blocked", "Failed") else "Status",
        f"Readiness '{new['title']}' → {new['status']}" + (f". {data['comment']}" if data.get("comment") else ""))
    if item["status"] == RS.READY.value and new["status"] != RS.READY.value:
        _readiness_regressed(ctx, ev, f"Readiness item '{new['title']}' is no longer Ready")
    return new


def _readiness_regressed(ctx: Ctx, ev: dict, why: str) -> None:
    """A Go decision is only valid while mandatory readiness holds."""
    ev = row(ctx, "test_events", ev["id"], for_update=True)
    if ev["lifecycle_status"] != S.READY.value:
        return
    gate = readiness_gate(ctx, ev)
    if gate.passed:
        return
    supersede_approvals(ctx, ev["id"], [AT.GO_NO_GO])
    e = t("test_events")
    ctx.conn.execute(update(e).where(e.c.id == ev["id"]).values(
        lifecycle_status=S.READINESS.value, status_reason=why, version=ev["version"] + 1, updated_at=now()))
    audit.record(ctx, "lifecycle.readiness_regressed", "Event", ev["id"], event_id=ev["id"],
                 old={"lifecycle_status": "Ready"}, new={"lifecycle_status": "Readiness", "reason": why}, actor=ctx.me.user_id)
    log(ctx, ev["id"], "Lifecycle", f"Ready → Readiness (system rule): {why}. Go decision invalidated.", source="System")


def readiness_summary(ctx: Ctx, event_id: UUID) -> dict:
    ev = event(ctx, event_id)
    r, ap, i = t("test_readiness_items"), t("test_approvals"), t("test_issues")
    items = [dict(x) for x in ctx.conn.execute(select(r).where(r.c.event_id == event_id).order_by(r.c.title)).mappings()]
    gate = readiness_gate(ctx, ev)
    deps = dependency_map(ctx, event_id)
    approvals = [dict(x) for x in ctx.conn.execute(select(ap).where(and_(
        ap.c.event_id == event_id, ap.c.approval_type.in_([AT.GO_NO_GO.value, AT.READINESS_EXCEPTION.value])))
        .order_by(ap.c.decided_at.desc())).mappings()]
    blockers = [dict(x) for x in ctx.conn.execute(select(i).where(and_(
        i.c.event_id == event_id, i.c.issue_type == "Blocker", i.c.status.in_(["Open", "In Progress"])))).mappings()]
    return {"gate": gate.__dict__, "items": items, "dependency_summary": deps["summary"],
            "blocked_activities": [n for n in deps["nodes"] if n["dependency_state"] == "blocked"],
            "overdue_activities": [n for n in deps["nodes"] if n["is_overdue"]],
            "critical_blockers": blockers, "decisions": approvals,
            "go_decision_valid": valid_approval(ctx, event_id, AT.GO_NO_GO) is not None,
            "note": "Readiness percentage is a progress indicator; the gate is decided by mandatory items only."}


# ---------------------------------------------------------------------------
# Evidence (RA-057/058). Files go to encrypted object storage via a presigned
# upload; this API records metadata and integrity checksum only.
# ---------------------------------------------------------------------------
def check_can_submit_evidence(ctx: Ctx, *, activity_id: UUID | None = None, readiness_item_id: UUID | None = None,
                              supersedes_id: UUID | None = None) -> dict:
    """Every rule for submitting evidence here, checked before anything is stored (an upload to a retention-locked
    store can't be taken back). Returns the event."""
    if activity_id:
        target, ev = child_of_event(ctx, "test_activities", activity_id)
        allowed = _is_activity_owner(ctx, target)
    else:
        target, ev = child_of_event(ctx, "test_readiness_items", readiness_item_id)  # type: ignore[arg-type]
        allowed = ctx.me.user_id == target["owner_user_id"]
    if not (allowed or authz.can_manage_event(ctx, ev)):
        raise Forbidden("Only the owner or test manager can submit evidence here")
    if S(ev["lifecycle_status"]) in (S.CLOSED, S.CANCELLED, S.DRAFT):
        raise Conflict(f"Evidence cannot be submitted while the event is {ev['lifecycle_status']}")
    if supersedes_id:
        prev = row(ctx, "test_evidence", supersedes_id)
        if prev["event_id"] != ev["id"] or prev["review_status"] != "Rejected":
            raise Unprocessable("Only rejected evidence from this event can be superseded")
    return ev


def submit_evidence(ctx: Ctx, *, activity_id: UUID | None = None, readiness_item_id: UUID | None = None,
                    data: dict) -> dict:
    ev = check_can_submit_evidence(ctx, activity_id=activity_id, readiness_item_id=readiness_item_id,
                                   supersedes_id=data.get("supersedes_id"))
    if data.get("checksum") and not SHA256.match(data["checksum"]):
        raise Unprocessable("checksum must be a lowercase hex SHA-256 digest")
    evd = t("test_evidence")
    code = f"{ev['event_code']}-EV-{next_seq(ctx, 'EV-' + ev['event_code']):03d}"
    out = dict(ctx.conn.execute(insert(evd).values(
        tenant_id=ctx.me.tenant_id, event_id=ev["id"], activity_id=activity_id, readiness_item_id=readiness_item_id,
        evidence_code=code, submitted_by=ctx.me.user_id,
        **{k: data.get(k) for k in ("evidence_type", "storage_reference", "filename", "checksum", "supersedes_id")},
        **{k: data[k] for k in ("storage_backend", "size_bytes", "content_type", "scan_status", "scan_engine",
                                "scan_detail", "scanned_at", "encryption", "retain_until") if k in data}
    ).returning(evd)).mappings().one())
    audit.record(ctx, "evidence.submitted", "Evidence", out["id"], event_id=ev["id"], new=out)
    return out


def review_evidence(ctx: Ctx, evidence_id: UUID, accept: bool, comment: str | None) -> dict:
    item, ev = child_of_event(ctx, "test_evidence", evidence_id, for_update=True)
    reviewer_ok = authz.can_manage_event(ctx, ev) or ctx.me.has(Role.RISK)
    if item["activity_id"]:
        act = row(ctx, "test_activities", item["activity_id"])
        reviewer_ok = reviewer_ok or ctx.me.user_id == act["reviewer_user_id"]
    if not reviewer_ok:
        raise Forbidden("Not authorised to review evidence for this event")
    if item["submitted_by"] == ctx.me.user_id:
        raise Forbidden("You cannot review evidence you submitted")
    if item["review_status"] != "Pending":
        raise Conflict(f"Evidence already {item['review_status']}")
    if not accept and not comment:
        raise Unprocessable("A comment is required when rejecting evidence")
    if accept and item["scan_status"] in ("Pending", "Infected"):
        raise Conflict("This file can't be accepted: " + ("it is still waiting for its malware scan."
                       if item["scan_status"] == "Pending" else "it was quarantined by the malware scan."))
    evd = t("test_evidence")
    new = dict(ctx.conn.execute(update(evd).where(evd.c.id == evidence_id).values(
        review_status="Accepted" if accept else "Rejected", review_comment=comment,
        reviewed_by=ctx.me.user_id, reviewed_at=now()).returning(evd)).mappings().one())
    audit.record(ctx, "evidence.reviewed", "Evidence", evidence_id, event_id=ev["id"],
                 new={"review_status": new["review_status"], "comment": comment})
    if not accept:
        notifications.enqueue(ctx, "approval_requested", event=ev, activity_id=item["activity_id"],
                              recipients=[item["submitted_by"]],
                              subject=f"[{ev['event_code']}] Evidence {item['evidence_code']} rejected",
                              body=f"Feedback: {comment}. Please resubmit.", dedupe=f"ev-rejected:{evidence_id}")
    return new


# ---------------------------------------------------------------------------
# Issues and remediation (RA-053, RA-060/061)
# ---------------------------------------------------------------------------
def create_issue(ctx: Ctx, event_id: UUID, data: dict) -> dict:
    ev = event(ctx, event_id)
    authz.require_writer(ctx)
    if S(ev["lifecycle_status"]) in (S.CLOSED, S.CANCELLED):
        raise Conflict("Event is closed")
    i = t("test_issues")
    code = f"{ev['event_code']}-{'F' if data['issue_type'] == 'Finding' else 'I'}{next_seq(ctx, 'ISS-' + ev['event_code']):03d}"
    out = dict(ctx.conn.execute(insert(i).values(
        tenant_id=ctx.me.tenant_id, event_id=event_id, issue_code=code, created_by=ctx.me.user_id,
        **{k: data.get(k) for k in ("activity_id", "title", "description", "severity", "issue_type",
                                    "root_cause", "owner_user_id", "due_at")}).returning(i)).mappings().one())
    audit.record(ctx, "issue.created", "Issue", out["id"], event_id=event_id, new=out)
    log(ctx, event_id, "Blocker" if out["issue_type"] == "Blocker" else "Status",
        f"{out['issue_type']} {code} raised ({out['severity']}): {out['title']}", activity_id=out["activity_id"])
    if out["severity"] == "Critical":
        notifications.enqueue(ctx, "critical_issue", event=ev, recipients=[ev["test_manager_user_id"], ev["owner_user_id"]],
                              subject=f"[{ev['event_code']}] CRITICAL {out['issue_type']}: {out['title']}",
                              body=out.get("description") or "", dedupe=f"issue:{out['id']}")
    return out


def update_issue(ctx: Ctx, issue_id: UUID, data: dict) -> dict:
    iss, ev = child_of_event(ctx, "test_issues", issue_id, for_update=True)
    if not (authz.can_manage_event(ctx, ev) or ctx.me.user_id == iss["owner_user_id"]):
        raise Forbidden("Only the issue owner or test manager can update this issue")
    if data.get("status") == "Closed" and not authz.can_manage_event(ctx, ev):
        raise Forbidden("Only the test manager can close an issue")
    i = t("test_issues")
    values = {k: data[k] for k in ("status", "owner_user_id", "due_at", "root_cause", "severity") if k in data}
    if values.get("status") in ("Resolved", "Closed") and iss["issue_type"] == "Blocker" and not data.get("comment"):
        raise Unprocessable("Say how the blocker was resolved")
    if not values:
        return iss
    new = dict(ctx.conn.execute(update(i).where(i.c.id == issue_id).values(**values).returning(i)).mappings().one())
    o, n = audit.diff(iss, new)
    audit.record(ctx, "issue.updated", "Issue", issue_id, event_id=ev["id"], old=o, new=n)
    if "status" in n:
        log(ctx, ev["id"], "Status", f"{iss['issue_type']} {iss['issue_code']} → {new['status']}"
            + (f". {data['comment']}" if data.get("comment") else ""), activity_id=iss["activity_id"])
    return new


def create_remediation(ctx: Ctx, issue_id: UUID, data: dict) -> dict:
    iss, ev = child_of_event(ctx, "test_issues", issue_id)
    if not (authz.can_manage_event(ctx, ev) or ctx.me.user_id == iss["owner_user_id"]):
        raise Forbidden("Not authorised to add remediation for this issue")
    ra = t("test_remediation_actions")
    code = f"{iss['issue_code']}-R{next_seq(ctx, 'RA-' + iss['issue_code']):02d}"
    out = dict(ctx.conn.execute(insert(ra).values(
        tenant_id=ctx.me.tenant_id, issue_id=issue_id, action_code=code,
        **{k: data[k] for k in ("title", "owner_user_id", "due_at")}).returning(ra)).mappings().one())
    audit.record(ctx, "remediation.created", "RemediationAction", out["id"], event_id=ev["id"], new=out)
    return out


def update_remediation(ctx: Ctx, action_id: UUID, data: dict) -> dict:
    act = row(ctx, "test_remediation_actions", action_id, for_update=True)
    iss, ev = child_of_event(ctx, "test_issues", act["issue_id"])
    ra = t("test_remediation_actions")
    status = data["status"]
    if status == "Accepted":
        if not (authz.can_manage_event(ctx, ev) or ctx.me.has(Role.RISK)):
            raise Forbidden("Only the test manager or risk/compliance can accept remediation")
        if ctx.me.user_id == act["owner_user_id"]:
            raise Forbidden("Owners cannot accept their own remediation")
        if act["status"] != "Submitted":
            raise Conflict("Remediation must be Submitted before acceptance")
        evid = act["resolution_evidence_id"]
        if not evid or row(ctx, "test_evidence", evid)["review_status"] != "Accepted":
            raise Conflict("Accepted resolution evidence is required")
        values = {"status": status, "accepted_by": ctx.me.user_id, "accepted_at": now()}
    else:
        if ctx.me.user_id != act["owner_user_id"] and not authz.can_manage_event(ctx, ev):
            raise Forbidden("Only the remediation owner can update progress")
        values = {"status": status}
        if data.get("resolution_evidence_id"):
            evrow = row(ctx, "test_evidence", data["resolution_evidence_id"])
            if evrow["event_id"] != ev["id"]:
                raise Unprocessable("Evidence must belong to the same event")
            values["resolution_evidence_id"] = data["resolution_evidence_id"]
    new = dict(ctx.conn.execute(update(ra).where(ra.c.id == action_id).values(**values).returning(ra)).mappings().one())
    audit.record(ctx, "remediation.updated", "RemediationAction", action_id, event_id=ev["id"],
                 old={"status": act["status"]}, new={"status": status})
    _ = iss
    return new


# ---------------------------------------------------------------------------
# Measurements (RA-059)
# ---------------------------------------------------------------------------
def record_measurement(ctx: Ctx, event_id: UUID, data: dict) -> dict:
    ev = event(ctx, event_id)
    authz.require_writer(ctx)
    if ctx.me.is_external and not authz.is_stakeholder(ctx, event_id):
        raise Forbidden("Not a participant")
    if S(ev["lifecycle_status"]) not in (S.LIVE, S.PAUSED, S.CLOSURE):
        raise Conflict("Measurements are recorded during or after the live test")
    computed: dict = {"target_canonical": None, "actual_canonical": None, "target_met": None}
    if data["measurement_type"] != "Other":
        try:
            met, tc, ac = msr.evaluate(data["measurement_type"], Decimal(str(data["target_value"])), data["target_unit"],
                                       Decimal(str(data["actual_value"])), data["unit"])
        except msr.MeasurementError as e:
            raise Unprocessable(str(e)) from e
        computed = {"target_canonical": tc, "actual_canonical": ac, "target_met": met}
    m = t("test_measurements")
    out = dict(ctx.conn.execute(insert(m).values(
        tenant_id=ctx.me.tenant_id, event_id=event_id, recorded_by=ctx.me.user_id, source=data.get("source", "Manual"),
        **{k: data.get(k) for k in ("measurement_type", "target_value", "target_unit", "actual_value", "unit", "evidence_id")},
        **computed).returning(m)).mappings().one())
    audit.record(ctx, "measurement.recorded", "Measurement", out["id"], event_id=event_id, new=out)
    return out


def validate_measurement(ctx: Ctx, measurement_id: UUID) -> dict:
    m_row, ev = child_of_event(ctx, "test_measurements", measurement_id, for_update=True)
    if not (ctx.me.has(Role.RISK) or authz.can_manage_event(ctx, ev)):
        raise Forbidden("Only risk/compliance or the test manager can validate measurements")
    if m_row["recorded_by"] == ctx.me.user_id:
        raise Forbidden("A measurement must be validated by someone other than the recorder")
    if m_row["measurement_type"] == "Other" or m_row["target_met"] is None:
        result = "Met"  # 'Other' is validated as recorded; no automated comparison
    else:
        result = "Met" if m_row["target_met"] else "Not Met"
    m = t("test_measurements")
    new = dict(ctx.conn.execute(update(m).where(m.c.id == measurement_id).values(
        result_status=result, validated_by=ctx.me.user_id, validated_at=now()).returning(m)).mappings().one())
    audit.record(ctx, "measurement.validated", "Measurement", measurement_id, event_id=ev["id"], new={"result_status": result})
    return new


# ---------------------------------------------------------------------------
# Approvals (RA-039, RA-063)
# ---------------------------------------------------------------------------
def record_approval(ctx: Ctx, event_id: UUID, data: dict) -> dict:
    ev = event(ctx, event_id, for_update=True)
    kind = AT(data["approval_type"])
    if kind in (AT.PLAN, AT.BLACKOUT_OVERRIDE):
        raise Unprocessable(f"{kind} decisions are recorded through their workflow action, not directly")
    authz.require_approver(ctx, kind, ev)
    decision = data["decision"]
    if decision == "Conditional" and not data.get("conditions"):
        raise Unprocessable("Conditions are required for a conditional decision")
    if decision in ("Rejected",) and not data.get("comments"):
        raise Unprocessable("Comments are required when rejecting")
    st = S(ev["lifecycle_status"])
    positive = decision in ("Approved", "Conditional")
    snapshot = None
    subject_type = None

    if kind == AT.GO_NO_GO:
        if st != S.READY:
            raise Conflict("Go/no-go is decided when the event is Ready")
        gate = readiness_gate(ctx, ev)
        snapshot = {"passed": gate.passed, "percent_ready": gate.percent_ready, "unmet": gate.unmet, "excepted": gate.excepted}
        if positive and not gate.passed:
            raise DomainError("Go decision blocked: mandatory readiness conditions are unmet", gate.unmet, status_code=409)
    elif kind == AT.READINESS_EXCEPTION:
        if st not in (S.MOBILISING, S.READINESS):
            raise Conflict("Readiness exceptions are granted before the event is Ready")
        item = row(ctx, "test_readiness_items", data.get("subject_id"))
        if item["event_id"] != event_id or not item["is_mandatory"]:
            raise Unprocessable("Exception must reference a mandatory readiness item of this event")
        if not data.get("comments"):
            raise Unprocessable("A justification is required for a readiness exception")
        subject_type = "ReadinessItem"
    elif kind == AT.RESUME:
        if st != S.PAUSED:
            raise Conflict("Resume is approved while the event is Paused")
    elif kind in (AT.BUSINESS, AT.RISK_REVIEW):
        if st != S.CLOSURE:
            raise Conflict(f"{kind} sign-off happens during Closure")
        if not ev["outcome"]:
            raise Conflict("The test manager must record the outcome first")
    elif kind == AT.CLOSURE:
        if st != S.CLOSURE:
            raise Conflict("Closure approval happens during Closure")
        if positive:
            blockers = closure_blockers(ctx, ev, ignore_closure_approval=True)
            if blockers:
                raise DomainError("Closure approval blocked: checklist incomplete", blockers, status_code=409)

    if kind != AT.READINESS_EXCEPTION:
        supersede_approvals(ctx, event_id, [kind])  # latest decision of a type wins
    ap = t("test_approvals")
    out = dict(ctx.conn.execute(insert(ap).values(
        tenant_id=ctx.me.tenant_id, event_id=event_id, approval_type=kind.value, approver_user_id=ctx.me.user_id,
        decision=decision, conditions=data.get("conditions"), comments=data.get("comments"),
        subject_type=subject_type, subject_id=data.get("subject_id") if subject_type else None,
        event_version=ev["version"], readiness_snapshot=snapshot, policy_version=authz.POLICY_VERSION,
    ).returning(ap)).mappings().one())
    audit.record(ctx, "approval.recorded", "Approval", out["id"], event_id=event_id, new=out)
    if kind == AT.GO_NO_GO:
        from . import decisions
        rec = decisions.seal(ctx, ev, out, data.get("present_user_ids"),
                             [dict(d) for d in data.get("dissents") or []])
        out = {**out, "decision_record_id": rec["id"], "decision_record_sha256": rec["content_sha256"]}
    label = {"Go/No-go": "Go/no-go", "ReadinessException": "Readiness exception", "RiskReview": "Risk review",
             "Business": "Business sign-off", "Closure": "Closure approval", "Resume": "Resume"}.get(kind.value, kind.value)
    log(ctx, event_id, "Decision", f"{label}: {decision.lower()} by {ctx.me.display_name}"
        + (f". {data['comments']}" if data.get("comments") else "")
        + (f" (conditions: {data['conditions']})" if data.get("conditions") else ""))
    return out


def request_approval(ctx: Ctx, event_id: UUID, approval_type: str, approver_ids: list[UUID]) -> dict:
    ev = event(ctx, event_id)
    authz.require_manage(ctx, ev)
    cid = notifications.enqueue(ctx, "approval_requested", event=ev, recipients=approver_ids,
                                subject=f"[{ev['event_code']}] {approval_type} decision requested: {ev['title']}",
                                body="Please review the readiness/closure summary and record your decision.",
                                dedupe=f"approval-req:{event_id}:{approval_type}:v{ev['version']}")
    audit.record(ctx, "approval.requested", "Event", event_id, event_id=event_id,
                 new={"type": approval_type, "approvers": [str(a) for a in approver_ids]})
    return {"communication_id": cid, "requested": len(approver_ids)}


# ---------------------------------------------------------------------------
# Live coordination (Epic 7)
# ---------------------------------------------------------------------------
def announce(ctx: Ctx, event_id: UUID, message: str) -> dict:
    ev = event(ctx, event_id)
    authz.require_manage(ctx, ev)
    log(ctx, event_id, "Announcement", message)
    cid = notifications.enqueue(ctx, "announcement", event=ev, recipients=participant_ids(ctx, ev),
                                subject=f"[{ev['event_code']}] Announcement", body=message,
                                dedupe=f"ann:{event_id}:{ctx.correlation_id}", trigger_type="Manual")
    return {"communication_id": cid}


def add_log_entry(ctx: Ctx, event_id: UUID, data: dict) -> dict:
    ev = event(ctx, event_id)
    authz.require_writer(ctx)
    if data["log_type"] == "Decision" and not authz.can_manage_event(ctx, ev):
        raise Forbidden("Only the test manager can record decisions in the log")
    if data["log_type"] not in ("Status", "Decision", "Milestone"):
        raise Unprocessable("Use the dedicated endpoints for announcements, blockers and lifecycle changes")
    if S(ev["lifecycle_status"]) not in (S.READY, S.LIVE, S.PAUSED, S.CLOSURE):
        raise Conflict("Execution log entries are recorded around the live window")
    log(ctx, event_id, data["log_type"], data["message"], activity_id=data.get("activity_id"),
        occurred_at=data.get("occurred_at"), external_reference=data.get("external_reference"))
    return {"ok": True}


def update_milestone(ctx: Ctx, milestone_id: UUID, data: dict) -> dict:
    ms_row, ev = child_of_event(ctx, "test_milestones", milestone_id, for_update=True)
    if not (authz.can_manage_event(ctx, ev) or ctx.me.user_id == ms_row["owner_user_id"]):
        raise Forbidden("Only the milestone owner or test manager can update it")
    ms = t("test_milestones")
    values = {"status": data["status"]}
    if data["status"] == "Completed":
        values["actual_at"] = data.get("actual_at") or now()
    new = dict(ctx.conn.execute(update(ms).where(ms.c.id == milestone_id).values(**values).returning(ms)).mappings().one())
    audit.record(ctx, "milestone.updated", "Milestone", milestone_id, event_id=ev["id"], old={"status": ms_row["status"]}, new=values)
    log(ctx, ev["id"], "Milestone", f"Milestone '{new['title']}' → {new['status']}", occurred_at=values.get("actual_at"))
    return new
