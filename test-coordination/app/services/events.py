"""Programmes, templates, test events, scheduling and lifecycle (Epics 2, 3, 5, 8)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, exists, func, insert, or_, select, true, update

from .. import authz
from ..db import t
from ..domain import dependencies as dep
from ..domain import lifecycle as lc
from ..domain import readiness as rd
from ..domain.enums import ApprovalType as AT
from ..domain.enums import LifecycleStatus as S
from ..domain.enums import Outcome, Role
from ..errors import Conflict, DomainError, Forbidden, NotFound, Unprocessable
from ..security import Ctx
from . import audit, notifications
from .common import active_stakeholders, check_version, event, log, next_seq, now, participant_ids, row

CATEGORY_PREFIX = {"Infrastructure": "DR", "Application": "DR", "Database": "DR", "Cyber": "CY", "Business": "BC"}
LOG_TEXT = {
    "submit": "Plan submitted for approval", "approve_plan": "Plan approved; mobilisation started",
    "return_to_draft": "Plan sent back to draft", "begin_readiness": "Readiness tracking started",
    "mark_ready": "Marked ready for the go decision", "return_to_mobilising": "Returned to mobilising",
    "start": "Live coordination started", "pause": "Coordination paused", "resume": "Coordination resumed",
    "complete": "Coordination completed; closure started", "abort": "Test stopped (outcome: aborted)",
    "close": "Test formally closed", "cancel": "Test cancelled",
}
EDITABLE_FIELDS = ("title", "description", "objectives", "business_impact", "criticality", "test_manager_user_id",
                   "rto_target_minutes", "rpo_target_minutes")


# ---------------------------------------------------------------------------
# Programmes and templates
# ---------------------------------------------------------------------------
def create_programme(ctx: Ctx, data: dict) -> dict:
    authz.require_role(ctx, Role.PROGRAMME_MANAGER, Role.ADMIN)
    p = t("test_programmes")
    if data["end_date"] < data["start_date"]:
        raise Unprocessable("end_date must be on or after start_date")
    exists_ = ctx.conn.execute(select(p.c.id).where(p.c.programme_code == data["programme_code"])).first()
    if exists_:
        raise Conflict(f"Programme code {data['programme_code']} already exists")
    r = ctx.conn.execute(insert(p).values(tenant_id=ctx.me.tenant_id, owner_user_id=data.get("owner_user_id") or ctx.me.user_id,
                                          **{k: data[k] for k in ("name", "programme_code", "financial_year", "start_date", "end_date")},
                                          status=data.get("status", "Active")).returning(p)).mappings().one()
    audit.record(ctx, "programme.created", "Programme", r["id"], new=dict(r))
    return dict(r)


def get_programme(ctx: Ctx, pid: UUID) -> dict:
    p = row(ctx, "test_programmes", pid)
    e = t("test_events")
    counts = dict(ctx.conn.execute(select(e.c.lifecycle_status, func.count()).where(e.c.programme_id == pid)
                                   .group_by(e.c.lifecycle_status)).all())
    return {**p, "event_counts": counts}


def create_template(ctx: Ctx, data: dict) -> dict:
    authz.require_role(ctx, Role.PROGRAMME_MANAGER, Role.TEST_MANAGER)
    d = data.get("definition", {})
    codes = [a["code"] for a in d.get("activities", [])]
    if len(codes) != len(set(codes)):
        raise Unprocessable("Template activity codes must be unique")
    edges: list[dep.Edge] = []
    ids = {c: UUID(int=i + 1) for i, c in enumerate(codes)}
    for pred, succ, *rest in d.get("dependencies", []):
        if pred not in ids or succ not in ids:
            raise Unprocessable(f"Template dependency references unknown activity {pred}->{succ}")
        e = dep.Edge(ids[pred], ids[succ], bool(rest[0]) if rest else True)
        try:
            dep.validate_new_edge(edges, e)
        except dep.DependencyError as ex:
            raise Unprocessable(f"Template dependency {pred}->{succ}: {ex}") from ex
        edges.append(e)
    tt = t("test_templates")
    version = (ctx.conn.execute(select(func.max(tt.c.version)).where(tt.c.template_key == data["template_key"])).scalar() or 0) + 1
    if version > 1:  # new version supersedes old for new events; existing events keep their template_id
        ctx.conn.execute(update(tt).where(tt.c.template_key == data["template_key"]).values(is_active=False))
    r = ctx.conn.execute(insert(tt).values(
        tenant_id=ctx.me.tenant_id, version=version, created_by=ctx.me.user_id, is_active=True,
        **{k: data.get(k) for k in ("template_key", "name", "category", "description", "default_duration")},
        definition=data.get("definition", {})
    ).returning(tt)).mappings().one()
    audit.record(ctx, "template.created", "Template", r["id"], new={"key": r["template_key"], "version": version})
    return dict(r)


# ---------------------------------------------------------------------------
# Scheduling and conflicts (RA-016)
# ---------------------------------------------------------------------------
def schedule_conflicts(ctx: Ctx, start: datetime, end: datetime, *, exclude_event: UUID | None = None,
                       manager: UUID | None = None, service_ids: list[UUID] | None = None) -> dict[str, list[dict]]:
    b, e, sc = t("blackout_periods"), t("test_events"), t("test_scope_items")
    blackouts = [dict(r) for r in ctx.conn.execute(
        select(b.c.id, b.c.name, b.c.starts_at, b.c.ends_at).where(and_(b.c.starts_at < end, b.c.ends_at > start))).mappings()]
    overlap = and_(e.c.planned_start_at < end, e.c.planned_end_at > start,
                   e.c.lifecycle_status.notin_([S.CANCELLED.value, S.CLOSED.value]))
    if exclude_event:
        overlap = and_(overlap, e.c.id != exclude_event)
    service_clash = []
    if exclude_event or service_ids:
        mine = (select(sc.c.business_service_id).where(and_(sc.c.event_id == exclude_event, sc.c.business_service_id.isnot(None)))
                if exclude_event else None)
        svc_filter = sc.c.business_service_id.in_(mine) if mine is not None else sc.c.business_service_id.in_(service_ids or [])
        if mine is not None and service_ids:
            svc_filter = or_(svc_filter, sc.c.business_service_id.in_(service_ids))
        service_clash = [dict(r) for r in ctx.conn.execute(
            select(e.c.id, e.c.event_code, e.c.title, e.c.planned_start_at, e.c.planned_end_at).where(and_(
                overlap, exists(select(sc.c.id).where(and_(sc.c.event_id == e.c.id, svc_filter)))))).mappings()]
    manager_clash = []
    if manager:
        manager_clash = [dict(r) for r in ctx.conn.execute(
            select(e.c.id, e.c.event_code, e.c.title).where(and_(overlap, e.c.test_manager_user_id == manager))).mappings()]
    return {"blackouts": blackouts, "shared_service_overlaps": service_clash, "test_manager_overlaps": manager_clash}


def _blackout_gate(ctx: Ctx, conflicts: dict, override_reason: str | None) -> bool:
    """Return True if an override is being used. Blackouts require authorised approval + reason."""
    if not conflicts["blackouts"]:
        return False
    if not override_reason:
        raise Conflict("Schedule falls within a blackout period",
                       [f"{b['name']} ({b['starts_at']} – {b['ends_at']})" for b in conflicts["blackouts"]])
    if not ctx.me.has(Role.PROGRAMME_MANAGER):
        raise Forbidden("Blackout override requires a DR programme manager")
    return True


def _record_blackout_override(ctx: Ctx, ev: dict, reason: str, conflicts: dict) -> None:
    ctx.conn.execute(insert(t("test_approvals")).values(
        tenant_id=ctx.me.tenant_id, event_id=ev["id"], approval_type=AT.BLACKOUT_OVERRIDE.value,
        approver_user_id=ctx.me.user_id, decision="Approved", comments=reason, event_version=ev["version"],
        readiness_snapshot={"blackouts": [str(b["id"]) for b in conflicts["blackouts"]]},
        policy_version=authz.POLICY_VERSION))
    audit.record(ctx, "schedule.blackout_override", "Event", ev["id"], event_id=ev["id"], new={"reason": reason})


def _validate_tz(tz: str) -> None:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise Unprocessable(f"Unknown timezone '{tz}'") from e


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def create_event(ctx: Ctx, data: dict, *, series: tuple | None = None) -> dict:
    authz.require_role(ctx, Role.PROGRAMME_MANAGER, Role.TEST_MANAGER)
    prog = row(ctx, "test_programmes", data["programme_id"])
    if prog["status"] == "Closed":
        raise Conflict("Programme is closed")
    start, end = data["planned_start_at"], data["planned_end_at"]
    if end <= start:
        raise Unprocessable("planned_end_at must be after planned_start_at")
    _validate_tz(data.get("timezone", "UTC"))
    conflicts = schedule_conflicts(ctx, start, end, manager=data.get("test_manager_user_id"),
                                   service_ids=data.get("business_service_ids") or None)
    override = _blackout_gate(ctx, conflicts, data.get("blackout_override_reason"))

    tpl = None
    if data.get("template_id"):
        tpl = row(ctx, "test_templates", data["template_id"])
        if not tpl["is_active"]:
            raise Conflict("Template version is no longer active")

    tenant = ctx.conn.execute(select(t("tenants").c.code)).scalar_one()
    year = start.astimezone(ZoneInfo(data.get("timezone", "UTC"))).year
    prefix = CATEGORY_PREFIX[data["category"]]
    code = f"{tenant}-{prefix}-{year}-{next_seq(ctx, f'{prefix}-{year}'):03d}"

    e = t("test_events")
    fields = {k: data.get(k) for k in ("programme_id", "template_id", "title", "description", "objectives",
                                       "business_impact", "category", "criticality", "planned_start_at",
                                       "planned_end_at", "test_manager_user_id", "rto_target_minutes",
                                       "rpo_target_minutes")}
    ev = dict(ctx.conn.execute(insert(e).values(
        tenant_id=ctx.me.tenant_id, event_code=code, timezone=data.get("timezone", "UTC"),
        series_id=series[0] if series else None, series_index=series[1] if series else None,
        owner_user_id=data.get("owner_user_id") or ctx.me.user_id, **fields).returning(e)).mappings().one())
    audit.record(ctx, "event.created", "Event", ev["id"], event_id=ev["id"], new=ev)
    if override:
        _record_blackout_override(ctx, ev, data["blackout_override_reason"], conflicts)
    if tpl:
        _instantiate_template(ctx, ev, tpl)
    for svc in data.get("business_service_ids", []):
        add_scope_item(ctx, ev["id"], {"business_service_id": svc})
    notifications.enqueue(ctx, "announcement", event=ev, recipients=[ev["owner_user_id"]],
                          subject=f"[{code}] Test created: {ev['title']}", body="A new test has been registered.",
                          dedupe=f"{ev['id']}:created")
    return {**ev, "conflicts": conflicts}


def _instantiate_template(ctx: Ctx, ev: dict, tpl: dict) -> None:
    d = tpl["definition"] or {}
    a, r, m, dp = t("test_activities"), t("test_readiness_items"), t("test_milestones"), t("test_dependencies")
    start = ev["planned_start_at"]
    by_code: dict[str, UUID] = {}
    for spec in d.get("activities", []):
        due = start + timedelta(hours=spec.get("due_offset_hours", 0))
        by_code[spec["code"]] = ctx.conn.execute(insert(a).values(
            tenant_id=ctx.me.tenant_id, event_id=ev["id"], activity_code=spec["code"], title=spec["title"],
            description=spec.get("description"), category=spec.get("category"),
            priority=spec.get("priority", "Medium"), is_mandatory=spec.get("is_mandatory", True),
            evidence_required=spec.get("evidence_required", False), due_at=due,
        ).returning(a.c.id)).scalar_one()
    for pred, succ, *rest in d.get("dependencies", []):
        ctx.conn.execute(insert(dp).values(tenant_id=ctx.me.tenant_id, event_id=ev["id"],
                                           predecessor_activity_id=by_code[pred], successor_activity_id=by_code[succ],
                                           is_mandatory=bool(rest[0]) if rest else True))
    for spec in d.get("readiness_items", []):
        ctx.conn.execute(insert(r).values(
            tenant_id=ctx.me.tenant_id, event_id=ev["id"], title=spec["title"], description=spec.get("description"),
            is_mandatory=spec.get("is_mandatory", True), evidence_required=spec.get("evidence_required", False),
            activity_id=by_code.get(spec.get("activity_code", ""))))
    for i, spec in enumerate(d.get("milestones", []), start=1):
        ctx.conn.execute(insert(m).values(tenant_id=ctx.me.tenant_id, event_id=ev["id"], title=spec["title"],
                                          sequence_number=i,
                                          planned_at=start + timedelta(minutes=spec.get("offset_minutes", 0))))
    audit.record(ctx, "event.template_applied", "Event", ev["id"], event_id=ev["id"],
                 new={"template_id": tpl["id"], "template_version": tpl["version"]})


def list_events(ctx: Ctx, *, programme_id: UUID | None = None, status: str | None = None,
                start_from: datetime | None = None, start_to: datetime | None = None,
                category: str | None = None, criticality: str | None = None, q: str | None = None,
                limit: int = 200, offset: int = 0) -> list[dict]:
    e, s = t("test_events"), t("test_stakeholders")
    conds = []
    is_stake = exists(select(s.c.id).where(and_(s.c.event_id == e.c.id, s.c.user_id == ctx.me.user_id, s.c.is_active)))
    if ctx.me.is_external:
        conds.append(is_stake)
    elif not ctx.me.has(*authz.PORTFOLIO_VIEWERS):
        conds.append(or_(e.c.owner_user_id == ctx.me.user_id, e.c.test_manager_user_id == ctx.me.user_id, is_stake))
    for col, val in ((e.c.programme_id, programme_id), (e.c.lifecycle_status, status),
                     (e.c.category, category), (e.c.criticality, criticality)):
        if val:
            conds.append(col == val)
    if start_from:
        conds.append(e.c.planned_end_at >= start_from)
    if start_to:
        conds.append(e.c.planned_start_at <= start_to)
    if q:
        conds.append(or_(e.c.title.ilike(f"%{q}%"), e.c.event_code.ilike(f"%{q}%")))
    cols = [c for c in e.c if c.name != "closure_snapshot"]
    return [dict(r) for r in ctx.conn.execute(select(*cols).where(and_(true(), *conds)).order_by(e.c.planned_start_at)
                                              .limit(min(limit, 1000)).offset(offset)).mappings()]


def get_event_overview(ctx: Ctx, event_id: UUID) -> dict:
    ev = event(ctx, event_id)
    gate = readiness_gate(ctx, ev)
    a, i, ms = t("test_activities"), t("test_issues"), t("test_milestones")
    act = dict(ctx.conn.execute(select(a.c.status, func.count()).where(a.c.event_id == event_id).group_by(a.c.status)).all())
    blockers = ctx.conn.execute(select(func.count()).where(and_(
        i.c.event_id == event_id, i.c.issue_type == "Blocker", i.c.status.in_(["Open", "In Progress"])))).scalar()
    upcoming = [dict(r) for r in ctx.conn.execute(select(ms).where(and_(ms.c.event_id == event_id, ms.c.status == "Pending"))
                                                  .order_by(ms.c.sequence_number).limit(5)).mappings()]
    ev.pop("closure_snapshot", None)
    series = None
    if ev.get("series_id"):
        from .series import siblings
        st = t("test_event_series")
        meta = ctx.conn.execute(select(st.c.title, st.c.frequency).where(st.c.id == ev["series_id"])).mappings().first()
        series = {**dict(meta), "occurrences": siblings(ctx, ev["series_id"])} if meta else None
    return {**ev, "series": series, "allowed_actions": lc.allowed_actions(S(ev["lifecycle_status"])),
            "can_manage": authz.can_manage_event(ctx, ev),
            "readiness": gate.__dict__, "activity_counts": act, "open_blockers": blockers,
            "go_decision_valid": valid_approval(ctx, event_id, AT.GO_NO_GO) is not None,
            "stakeholder_count": len(active_stakeholders(ctx, event_id)),
            "upcoming_milestones": upcoming, "generated_at": now()}


def update_event(ctx: Ctx, event_id: UUID, data: dict) -> dict:
    ev = event(ctx, event_id, for_update=True)
    authz.require_manage(ctx, ev)
    check_version(ev["version"], data.get("expected_version"))
    status = S(ev["lifecycle_status"])
    if status in lc.TERMINAL:
        raise Conflict(f"Event is {status}; it can no longer be edited")
    changes = {k: data[k] for k in EDITABLE_FIELDS if k in data and data[k] != ev[k]}
    resched = any(k in data and data[k] is not None and data[k] != ev[k]
                  for k in ("planned_start_at", "planned_end_at", "timezone"))
    if resched:
        if status not in lc.RESCHEDULABLE:
            raise Conflict(f"Cannot reschedule an event in state {status}")
        start = data.get("planned_start_at") or ev["planned_start_at"]
        end = data.get("planned_end_at") or ev["planned_end_at"]
        tz = data.get("timezone") or ev["timezone"]
        if end <= start:
            raise Unprocessable("planned_end_at must be after planned_start_at")
        _validate_tz(tz)
        conflicts = schedule_conflicts(ctx, start, end, exclude_event=event_id,
                                       manager=changes.get("test_manager_user_id", ev["test_manager_user_id"]))
        override = _blackout_gate(ctx, conflicts, data.get("blackout_override_reason"))
        if not data.get("reschedule_reason"):
            raise Unprocessable("reschedule_reason is required when changing the schedule")
        changes.update(planned_start_at=start, planned_end_at=end, timezone=tz)
    if not changes:
        return ev
    if status == S.READY and resched:
        changes["lifecycle_status"] = S.READINESS.value
    e = t("test_events")
    new = dict(ctx.conn.execute(update(e).where(e.c.id == event_id).values(
        **changes, version=ev["version"] + 1, updated_at=now()).returning(e)).mappings().one())
    old_v, new_v = audit.diff(ev, new)
    audit.record(ctx, "event.rescheduled" if resched else "event.updated", "Event", event_id,
                 event_id=event_id, old=old_v, new={**new_v, "reason": data.get("reschedule_reason")})
    if resched:
        if override:
            _record_blackout_override(ctx, new, data["blackout_override_reason"], conflicts)
        n = supersede_approvals(ctx, event_id, [AT.GO_NO_GO])
        log(ctx, event_id, "Lifecycle", f"Rescheduled. Reason: {data['reschedule_reason']}."
            + (" (Go decision invalidated; back to Readiness)" if n or status == S.READY else ""))
        # Only affected stakeholders are notified (§7 business rule).
        notifications.enqueue(ctx, "schedule_changed", event=new, recipients=participant_ids(ctx, new),
                              subject=f"[{new['event_code']}] Schedule changed: {new['title']}",
                              body=f"New window {new['planned_start_at']} – {new['planned_end_at']} ({new['timezone']}). "
                                   f"Reason: {data['reschedule_reason']}",
                              dedupe=f"{event_id}:v{new['version']}")
        new["conflicts"] = conflicts
    return new


def add_scope_item(ctx: Ctx, event_id: UUID, data: dict) -> dict:
    ev = event(ctx, event_id)
    authz.require_manage(ctx, ev)
    name = data.get("name")
    if data.get("business_service_id"):
        svc = row(ctx, "business_services", data["business_service_id"])
        name = name or svc["name"]
    if not name:
        raise Unprocessable("Scope item needs a name or business_service_id")
    sc = t("test_scope_items")
    r = dict(ctx.conn.execute(insert(sc).values(tenant_id=ctx.me.tenant_id, event_id=event_id, name=name,
                                                business_service_id=data.get("business_service_id"),
                                                item_type=data.get("item_type", "BusinessService")).returning(sc)).mappings().one())
    audit.record(ctx, "scope.added", "ScopeItem", r["id"], event_id=event_id, new=r)
    return r


# ---------------------------------------------------------------------------
# Readiness gate, approvals validity, closure checks
# ---------------------------------------------------------------------------
def readiness_gate(ctx: Ctx, ev: dict) -> rd.GateResult:
    r, ev_t, ap = t("test_readiness_items"), t("test_evidence"), t("test_approvals")
    items = ctx.conn.execute(select(r).where(r.c.event_id == ev["id"])).mappings().all()
    accepted = {x[0] for x in ctx.conn.execute(select(ev_t.c.readiness_item_id).where(and_(
        ev_t.c.event_id == ev["id"], ev_t.c.review_status == "Accepted", ev_t.c.readiness_item_id.isnot(None))))}
    excepted = {x[0] for x in ctx.conn.execute(select(ap.c.subject_id).where(and_(
        ap.c.event_id == ev["id"], ap.c.approval_type == AT.READINESS_EXCEPTION.value,
        ap.c.decision.in_(["Approved", "Conditional"]), ap.c.superseded_at.is_(None))))}
    return rd.evaluate([rd.ReadinessItemFacts(i["id"], i["title"], i["status"], i["is_mandatory"],
                                              i["evidence_required"], i["id"] in accepted, i["id"] in excepted)
                        for i in items])


def valid_approval(ctx: Ctx, event_id: UUID, kind: AT) -> dict | None:
    ap = t("test_approvals")
    r = ctx.conn.execute(select(ap).where(and_(
        ap.c.event_id == event_id, ap.c.approval_type == kind.value, ap.c.superseded_at.is_(None)))
        .order_by(ap.c.decided_at.desc()).limit(1)).mappings().first()
    return dict(r) if r and r["decision"] in ("Approved", "Conditional") else None


def supersede_approvals(ctx: Ctx, event_id: UUID, kinds: list[AT]) -> int:
    ap = t("test_approvals")
    res = ctx.conn.execute(update(ap).where(and_(
        ap.c.event_id == event_id, ap.c.approval_type.in_([k.value for k in kinds]), ap.c.superseded_at.is_(None)))
        .values(superseded_at=now()))
    if res.rowcount:
        audit.record(ctx, "approvals.superseded", "Event", event_id, event_id=event_id,
                     new={"types": [k.value for k in kinds], "count": res.rowcount})
    return res.rowcount


def closure_blockers(ctx: Ctx, ev: dict, *, ignore_closure_approval: bool = False) -> list[str]:
    a, evd, ms, i, ra = (t(x) for x in ("test_activities", "test_evidence", "test_measurements",
                                        "test_issues", "test_remediation_actions"))
    out: list[str] = []
    eid = ev["id"]
    if not ev["outcome"]:
        out.append("Outcome has not been recorded")
    acts = ctx.conn.execute(select(a).where(a.c.event_id == eid)).mappings().all()
    accepted_for = {x[0] for x in ctx.conn.execute(select(evd.c.activity_id).where(and_(
        evd.c.event_id == eid, evd.c.review_status == "Accepted", evd.c.activity_id.isnot(None))))}
    aborted = ev["outcome"] == Outcome.ABORTED.value
    for x in acts:
        if x["is_mandatory"] and x["status"] != "Completed" and not aborted:
            out.append(f"Mandatory activity {x['activity_code']} is {x['status']}")
        if x["status"] == "Submitted":
            out.append(f"Activity {x['activity_code']} awaits review")
        if x["evidence_required"] and x["status"] == "Completed" and x["id"] not in accepted_for:
            out.append(f"Mandatory evidence missing for activity {x['activity_code']}")
    pending = ctx.conn.execute(select(func.count()).where(and_(evd.c.event_id == eid, evd.c.review_status == "Pending"))).scalar()
    if pending:
        out.append(f"{pending} evidence item(s) awaiting review")
    unval = ctx.conn.execute(select(func.count()).where(and_(ms.c.event_id == eid, ms.c.result_status == "Not Validated"))).scalar()
    if unval:
        out.append(f"{unval} measurement(s) not validated")
    for x in ctx.conn.execute(select(i).where(and_(i.c.event_id == eid))).mappings():
        if x["issue_type"] in ("Blocker", "Incident") and x["status"] in ("Open", "In Progress"):
            out.append(f"{x['issue_type']} {x['issue_code']} is still {x['status']}")
        if x["issue_type"] == "Finding" and x["status"] != "Closed":
            has_action = ctx.conn.execute(select(ra.c.id).where(ra.c.issue_id == x["id"])).first()
            if not (x["owner_user_id"] and x["due_at"]) and not has_action:
                out.append(f"Finding {x['issue_code']} has no owner/due date or remediation action")
    needed = [AT.BUSINESS] + ([AT.RISK_REVIEW] if ev["criticality"] in ("Critical", "High") else [])
    if not ignore_closure_approval:
        needed.append(AT.CLOSURE)
    for k in needed:
        if not valid_approval(ctx, eid, k):
            out.append(f"{k} approval not yet recorded")
    return out


def gather_facts(ctx: Ctx, ev: dict) -> tuple[lc.EventFacts, rd.GateResult]:
    gate = readiness_gate(ctx, ev)
    f = lc.EventFacts(
        active_stakeholder_count=len(active_stakeholders(ctx, ev["id"])),
        has_test_manager=ev["test_manager_user_id"] is not None,
        readiness_gate_passed=gate.passed, readiness_unmet=gate.unmet,
        has_valid_go_approval=valid_approval(ctx, ev["id"], AT.GO_NO_GO) is not None,
        has_valid_resume_approval=valid_approval(ctx, ev["id"], AT.RESUME) is not None,
    )
    if ev["lifecycle_status"] == S.CLOSURE.value:
        f.closure_blockers = closure_blockers(ctx, ev)
    return f, gate


# ---------------------------------------------------------------------------
# Lifecycle transitions (RA-024)
# ---------------------------------------------------------------------------
def transition(ctx: Ctx, event_id: UUID, action: str, *, reason: str | None = None,
               expected_version: int | None = None) -> dict:
    ev = event(ctx, event_id, for_update=True)
    check_version(ev["version"], expected_version)
    tr = lc.TRANSITIONS.get(action)
    if tr is None:
        raise Unprocessable(f"Unknown action '{action}'")
    authz.require_capability(ctx, tr.capability, ev)
    authz.require_writer(ctx)
    authz.require_human(ctx, "change a test's stage")
    facts, gate = gather_facts(ctx, ev)
    try:
        lc.check_transition(S(ev["lifecycle_status"]), action, facts, reason)
    except lc.TransitionError as e:
        raise DomainError(str(e), e.details, status_code=409) from e

    values: dict[str, Any] = {"lifecycle_status": tr.target.value, "version": ev["version"] + 1, "updated_at": now()}
    if reason:
        values["status_reason"] = reason
    outcome = lc.outcome_for(action, None)
    if outcome:
        values["outcome"] = outcome.value
    if action == "start":
        values["actual_start_at"] = now()
    if action in ("complete", "abort"):
        values["actual_end_at"] = now()

    e = t("test_events")
    new = dict(ctx.conn.execute(update(e).where(e.c.id == event_id).values(**values).returning(e)).mappings().one())

    # Approval hygiene
    if action in ("approve_plan", "return_to_draft"):
        ctx.conn.execute(insert(t("test_approvals")).values(
            tenant_id=ctx.me.tenant_id, event_id=event_id, approval_type=AT.PLAN.value, approver_user_id=ctx.me.user_id,
            decision="Approved" if action == "approve_plan" else "Rejected", comments=reason,
            event_version=ev["version"], policy_version=authz.POLICY_VERSION))
    if action in ("pause", "abort", "cancel"):
        from .automation import cancel_for_event
        cancel_for_event(ctx, event_id, f"test {LOG_TEXT.get(action, action).lower()}")
    if action == "pause":
        supersede_approvals(ctx, event_id, [AT.RESUME])
    if action == "resume":
        supersede_approvals(ctx, event_id, [AT.RESUME])           # consumed
    if action == "return_to_mobilising":
        supersede_approvals(ctx, event_id, [AT.GO_NO_GO, AT.BUSINESS, AT.RISK_REVIEW, AT.CLOSURE])
    if action == "close":
        snap = jsonable_encoder(build_snapshot(ctx, new))
        new = dict(ctx.conn.execute(update(e).where(e.c.id == event_id).values(closure_snapshot=snap)
                                    .returning(e)).mappings().one())

    audit.record(ctx, f"lifecycle.{action}", "Event", event_id, event_id=event_id,
                 old={"lifecycle_status": ev["lifecycle_status"]},
                 new={"lifecycle_status": new["lifecycle_status"], "reason": reason, "outcome": new["outcome"],
                      "readiness": {"passed": gate.passed, "unmet": gate.unmet, "excepted": gate.excepted}})
    log(ctx, event_id, "Lifecycle" if action not in ("pause", "abort", "resume") else "Decision",
        LOG_TEXT.get(action, action) + (f". Reason: {reason}" if reason else ""))

    trig = {"start": "test_started", "close": "test_closed"}.get(action)
    if trig or action in ("pause", "abort", "resume", "cancel"):
        notifications.enqueue(ctx, trig or "announcement", event=new, recipients=participant_ids(ctx, new),
                              subject=f"[{new['event_code']}] {new['title']}: {new['lifecycle_status']}",
                              body=f"{tr.description or action}." + (f" Reason: {reason}" if reason else ""),
                              dedupe=f"{event_id}:{action}:v{new['version']}")
    if action != "close":
        new.pop("closure_snapshot", None)
    return new


def set_outcome(ctx: Ctx, event_id: UUID, outcome: str, summary: str | None, expected_version: int | None) -> dict:
    ev = event(ctx, event_id, for_update=True)
    authz.require_manage(ctx, ev)
    check_version(ev["version"], expected_version)
    if ev["lifecycle_status"] != S.CLOSURE.value:
        raise Conflict("Outcome is prepared during Closure")
    if ev["outcome"] == Outcome.ABORTED.value and outcome != Outcome.ABORTED.value:
        raise Conflict("An aborted test keeps outcome Aborted")
    if outcome == Outcome.CANCELLED.value:
        raise Unprocessable("Use the cancel action for cancelled tests")
    e = t("test_events")
    new = dict(ctx.conn.execute(update(e).where(e.c.id == event_id).values(
        outcome=outcome, outcome_summary=summary, version=ev["version"] + 1, updated_at=now()).returning(e)).mappings().one())
    if ev["outcome"] and ev["outcome"] != outcome:   # sign-offs were against a different outcome
        supersede_approvals(ctx, event_id, [AT.BUSINESS, AT.RISK_REVIEW, AT.CLOSURE])
    audit.record(ctx, "event.outcome_set", "Event", event_id, event_id=event_id,
                 old={"outcome": ev["outcome"]}, new={"outcome": outcome, "summary": summary})
    return new


def build_snapshot(ctx: Ctx, ev: dict) -> dict:
    """Freeze scope, schedule, participants, decisions, results and approvals at closure (§15)."""
    def rows(table, *conds, order=None):
        tb = t(table)
        q = select(tb).where(and_(*[c(tb) for c in conds]))
        if order:
            q = q.order_by(order(tb))
        return [dict(r) for r in ctx.conn.execute(q).mappings()]
    eid = ev["id"]
    by_event = lambda tb: tb.c.event_id == eid  # noqa: E731
    u = t("users")
    names = {r.id: r.display_name for r in ctx.conn.execute(select(u.c.id, u.c.display_name))}
    stake = rows("test_stakeholders", by_event)
    return {
        "generated_at": now(),
        "event": {k: ev[k] for k in ("event_code", "title", "category", "criticality", "outcome", "outcome_summary",
                                     "planned_start_at", "planned_end_at", "actual_start_at", "actual_end_at", "timezone")},
        "owner": names.get(ev["owner_user_id"]), "test_manager": names.get(ev["test_manager_user_id"]),
        "scope": [s["name"] for s in rows("test_scope_items", by_event)],
        "participants": [{"name": names.get(s["user_id"]), "role": s["event_role"], "status": s["assignment_status"],
                          "active": s["is_active"], "acknowledged_at": s["acknowledged_at"]} for s in stake],
        "activities": [{"code": a["activity_code"], "title": a["title"], "status": a["status"],
                        "completed_at": a["completed_at"], "review": a["review_status"]}
                       for a in rows("test_activities", by_event, order=lambda tb: tb.c.activity_code)],
        "readiness": [{"title": r["title"], "status": r["status"], "mandatory": r["is_mandatory"]}
                      for r in rows("test_readiness_items", by_event)],
        "decisions": [{"type": a["approval_type"], "decision": a["decision"], "by": names.get(a["approver_user_id"]),
                       "at": a["decided_at"], "superseded": a["superseded_at"] is not None, "comments": a["comments"]}
                      for a in rows("test_approvals", by_event, order=lambda tb: tb.c.decided_at)],
        "measurements": [{"type": m["measurement_type"], "target": f"{m['target_value']} {m['target_unit']}",
                          "actual": f"{m['actual_value']} {m['unit']}", "result": m["result_status"]}
                         for m in rows("test_measurements", by_event)],
        "issues": [{"code": i["issue_code"], "type": i["issue_type"], "severity": i["severity"], "status": i["status"],
                    "title": i["title"]} for i in rows("test_issues", by_event)],
        "evidence_count": len(rows("test_evidence", by_event)),
    }


def closure_report(ctx: Ctx, event_id: UUID) -> dict:
    ev = event(ctx, event_id)
    if ev["closure_snapshot"]:
        return {"status": "final", **ev["closure_snapshot"]}
    if ev["lifecycle_status"] != S.CLOSURE.value:
        raise NotFound("Closure report is available once the event reaches Closure")
    return {"status": "draft", "blockers": closure_blockers(ctx, ev), **build_snapshot(ctx, ev)}
