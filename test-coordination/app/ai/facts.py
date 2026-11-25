"""Grounding: the facts each assistant feature may use, gathered with the requesting person's
permissions (so RLS and event access apply), each tagged with the record it came from."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, select

from ..db import t
from ..domain import dependencies as dep
from ..domain import forecast as fc
from ..domain.enums import ActivityStatus as A
from ..security import Ctx
from ..services import work
from ..services.common import now


def _names(ctx: Ctx) -> dict:
    u = t("users")
    return {r.id: r.display_name for r in ctx.conn.execute(select(u.c.id, u.c.display_name))}


def _age(since: datetime | None, at: datetime) -> str:
    if not since:
        return ""
    h = max((at - since).total_seconds() / 3600, 0)
    if h < 1:
        return "under an hour"
    return f"{round(h)} h" if h < 48 else f"{round(h / 24)} days"


def readiness_facts(ctx: Ctx, ev: dict) -> dict:
    n, names = now(), _names(ctx)
    rd = work.readiness_summary(ctx, ev["id"])
    a, d = t("test_activities"), t("test_dependencies")
    acts = {r["id"]: dict(r) for r in ctx.conn.execute(select(a).where(a.c.event_id == ev["id"])).mappings()}
    s = t("test_stakeholders")
    owner_of = {r.id: r.user_id for r in ctx.conn.execute(select(s.c.id, s.c.user_id).where(s.c.event_id == ev["id"]))}
    edges = [dep.Edge(r.predecessor_activity_id, r.successor_activity_id, r.is_mandatory)
             for r in ctx.conn.execute(select(d).where(d.c.event_id == ev["id"]))]
    mand_edges = [e for e in edges if e.mandatory]
    sources: list[dict] = [{"type": "event", "id": str(ev["id"]), "label": ev["event_code"]}]

    def owner(act):
        return names.get(owner_of.get(act["stakeholder_id"])) if act.get("stakeholder_id") else None

    blocked = []
    for x in acts.values():
        if x["status"] == A.BLOCKED.value:
            held = sorted(acts[i]["activity_code"] for i in dep.downstream(mand_edges, x["id"]) if i in acts)
            blocked.append({"code": x["activity_code"], "title": x["title"], "reason": x["blocked_reason"],
                            "owner": owner(x), "blocked_for": _age(x["updated_at"], n), "holds_up": held, "id": str(x["id"])})
            sources.append({"type": "activity", "id": str(x["id"]), "label": x["activity_code"]})
    overdue = []
    for x in acts.values():
        if x["due_at"] and x["due_at"] < n and x["status"] not in ("Completed", "Submitted", "Blocked"):
            overdue.append({"code": x["activity_code"], "title": x["title"], "owner": owner(x),
                            "late_by": _age(x["due_at"], n), "id": str(x["id"])})
            sources.append({"type": "activity", "id": str(x["id"]), "label": x["activity_code"]})
    unmet_ids = set(rd["gate"].get("unmet_item_ids") or [])
    evd = t("test_evidence")
    pending_ev = {r.readiness_item_id for r in ctx.conn.execute(select(evd.c.readiness_item_id).where(and_(
        evd.c.event_id == ev["id"], evd.c.review_status == "Pending", evd.c.readiness_item_id.isnot(None))))}
    unmet = []
    for it in rd["items"]:
        if str(it["id"]) in {str(x) for x in unmet_ids}:
            linked = acts.get(it["activity_id"]) if it["activity_id"] else None
            unmet.append({"title": it["title"], "status": it["status"], "owner": names.get(it["owner_user_id"]),
                          "evidence_required": it["evidence_required"], "evidence_pending": it["id"] in pending_ev,
                          "linked_activity": linked["activity_code"] if linked else None, "id": str(it["id"])})
            sources.append({"type": "readiness_item", "id": str(it["id"]), "label": it["title"]})
    issues = []
    for b in rd["critical_blockers"]:
        issues.append({"code": b["issue_code"], "title": b["title"], "severity": b["severity"],
                       "owner": names.get(b["owner_user_id"]), "open_for": _age(b["created_at"], n), "id": str(b["id"])})
        sources.append({"type": "issue", "id": str(b["id"]), "label": b["issue_code"]})
    ack, c = t("test_acknowledgements"), t("test_communications")
    pending_acks = sorted({names.get(r.recipient_user_id, "") for r in ctx.conn.execute(
        select(ack.c.recipient_user_id).join(c, c.c.id == ack.c.communication_id).where(and_(
            c.c.event_id == ev["id"], ack.c.status == "Pending")))} - {""})
    ev_pending = ctx.conn.execute(select(evd.c.id).where(and_(evd.c.event_id == ev["id"], evd.c.review_status == "Pending"))).all()

    # forecast inputs
    ap = t("test_approvals")
    excepted = {r.subject_id for r in ctx.conn.execute(select(ap.c.subject_id).where(and_(
        ap.c.event_id == ev["id"], ap.c.approval_type == "ReadinessException",
        ap.c.decision.in_(["Approved", "Conditional"]), ap.c.superseded_at.is_(None))))}
    f_acts = [fc.Act(x["id"], x["activity_code"], x["title"], x["status"], x["is_mandatory"], x["due_at"],
                     x["planned_start_at"], x["updated_at"], owner(x)) for x in acts.values()]
    f_conds = [fc.Cond(it["id"], it["title"], it["status"], it["is_mandatory"], it["activity_id"],
                       it["evidence_required"] and it["id"] in pending_ev, it["id"] in excepted)
               for it in rd["items"]]
    forecast = fc.project(n, ev["planned_start_at"], ev["lifecycle_status"], f_acts,
                          [(e.predecessor, e.successor, e.mandatory) for e in edges], f_conds)
    tm = names.get(ev["test_manager_user_id"])
    return {"event": {"code": ev["event_code"], "title": ev["title"], "lifecycle": ev["lifecycle_status"],
                      "criticality": ev["criticality"], "planned_start_at": ev["planned_start_at"], "timezone": ev["timezone"],
                      "test_manager": tm, "owner": names.get(ev["owner_user_id"])},
            "gate": {"passed": rd["gate"]["passed"], "met": rd["gate"]["mandatory_ready"] + len(rd["gate"]["excepted"]),
                     "total": rd["gate"]["mandatory_total"], "excepted": rd["gate"]["excepted"]},
            "go_decision_valid": rd["go_decision_valid"], "blocked": blocked, "overdue": overdue, "unmet": unmet,
            "issues": issues, "pending_acks": pending_acks, "evidence_pending": len(ev_pending),
            "forecast": forecast, "now": n, "sources": _dedupe(sources)}


def live_facts(ctx: Ctx, ev: dict) -> dict:
    n, names = now(), _names(ctx)
    a, ms, lg, i, m = (t(x) for x in ("test_activities", "test_milestones", "test_execution_logs", "test_issues", "test_measurements"))
    s = t("test_stakeholders")
    owner_of = {r.id: names.get(r.user_id) for r in ctx.conn.execute(select(s.c.id, s.c.user_id).where(s.c.event_id == ev["id"]))}
    acts = [dict(r) for r in ctx.conn.execute(select(a).where(a.c.event_id == ev["id"]).order_by(a.c.activity_code)).mappings()]
    miles = [dict(r) for r in ctx.conn.execute(select(ms).where(ms.c.event_id == ev["id"]).order_by(ms.c.sequence_number)).mappings()]
    logs = [dict(r) for r in ctx.conn.execute(select(lg).where(lg.c.event_id == ev["id"]).order_by(lg.c.occurred_at.desc()).limit(8)).mappings()]
    open_issues = [dict(r) for r in ctx.conn.execute(select(i).where(and_(
        i.c.event_id == ev["id"], i.c.status.in_(["Open", "In Progress"]), i.c.issue_type.in_(["Blocker", "Incident"])))).mappings()]
    meas = [dict(r) for r in ctx.conn.execute(select(m).where(m.c.event_id == ev["id"])).mappings()]
    sources = [{"type": "event", "id": str(ev["id"]), "label": ev["event_code"]}]
    sources += [{"type": "issue", "id": str(x["id"]), "label": x["issue_code"]} for x in open_issues]
    sources += [{"type": "milestone", "id": str(x["id"]), "label": x["title"]} for x in miles if x["status"] != "Completed"][:2]
    for x in acts:
        x["owner"] = owner_of.get(x["stakeholder_id"])
    return {"event": {"code": ev["event_code"], "title": ev["title"], "lifecycle": ev["lifecycle_status"],
                      "planned_start_at": ev["planned_start_at"], "planned_end_at": ev["planned_end_at"],
                      "actual_start_at": ev["actual_start_at"], "timezone": ev["timezone"], "status_reason": ev["status_reason"]},
            "activities": acts, "milestones": miles, "logs": [{**x, "actor": names.get(x["actor_user_id"])} for x in logs],
            "issues": open_issues, "measurements": meas, "now": n, "sources": _dedupe(sources)}


def people_for_messages(ctx: Ctx, ev: dict) -> dict:
    """Recipients the drafts may address: stakeholders, owner, test manager, approvers, team leads."""
    u, s, tm_ = t("users"), t("test_stakeholders"), t("teams")
    users = {r.id: dict(r) for r in ctx.conn.execute(select(u.c.id, u.c.display_name, u.c.roles, u.c.is_external)).mappings()}
    stake = [dict(r) for r in ctx.conn.execute(select(s).where(and_(s.c.event_id == ev["id"], s.c.is_active))).mappings()]
    leads = {r.id: r.lead_user_id for r in ctx.conn.execute(select(tm_.c.id, tm_.c.lead_user_id))}
    return {"users": users, "stakeholders": stake, "team_leads": leads}


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for x in items:
        k = (x["type"], x["id"])
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out

