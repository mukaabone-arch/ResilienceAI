"""Read models: My Workspace, Command Centre, timeline, communications (Screens 01, 04, 06, 08)."""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select

from .. import authz
from ..db import t
from ..domain import dependencies as dep
from ..domain.enums import ActivityStatus as A
from ..domain.enums import Role
from ..security import Ctx
from .common import event, now


def my_workspace(ctx: Ctx) -> dict:
    a, s, e, d, ack, c, evd = (t(x) for x in ("test_activities", "test_stakeholders", "test_events", "test_dependencies",
                                               "test_acknowledgements", "test_communications", "test_evidence"))
    me, n = ctx.me.user_id, now()
    my_stake = select(s.c.id).where(and_(s.c.user_id == me, s.c.is_active))
    events = [dict(r) for r in ctx.conn.execute(
        select(e.c.id, e.c.event_code, e.c.title, e.c.lifecycle_status, e.c.planned_start_at, e.c.planned_end_at, e.c.timezone)
        .where(and_(e.c.lifecycle_status.notin_(["Closed", "Cancelled"]), or_(
            exists(select(s.c.id).where(and_(s.c.event_id == e.c.id, s.c.user_id == me, s.c.is_active))),
            e.c.test_manager_user_id == me, e.c.owner_user_id == me)))
        .order_by(e.c.planned_start_at)).mappings()]
    acts = [dict(r) for r in ctx.conn.execute(
        select(a, e.c.event_code, e.c.title.label("event_title"), e.c.lifecycle_status.label("event_status"))
        .join(e, e.c.id == a.c.event_id)
        .where(and_(a.c.stakeholder_id.in_(my_stake), e.c.lifecycle_status.notin_(["Closed", "Cancelled"])))
        .order_by(a.c.due_at.nulls_last())).mappings()]
    # Dependency state per event
    for eid in {x["event_id"] for x in acts}:
        nodes = [dep.ActivityNode(r.id, A(r.status), r.due_at) for r in ctx.conn.execute(
            select(a.c.id, a.c.status, a.c.due_at).where(a.c.event_id == eid))]
        edges = [dep.Edge(r.predecessor_activity_id, r.successor_activity_id, r.is_mandatory)
                 for r in ctx.conn.execute(select(d).where(d.c.event_id == eid))]
        states = dep.propagate(nodes, edges, n)
        for x in acts:
            if x["event_id"] == eid:
                x["dependency_state"] = states[x["id"]].state
    accepted = {r[0] for r in ctx.conn.execute(select(evd.c.activity_id).where(and_(
        evd.c.activity_id.in_([x["id"] for x in acts] or [None]), evd.c.review_status.in_(["Pending", "Accepted"]))))}
    open_ = [x for x in acts if x["status"] != "Completed"]
    end_of_day = n.replace(hour=23, minute=59, second=59)
    pending_acks = [dict(r) for r in ctx.conn.execute(
        select(ack.c.id, ack.c.due_at, ack.c.stakeholder_id, c.c.subject, c.c.event_id)
        .join(c, c.c.id == ack.c.communication_id)
        .where(and_(ack.c.recipient_user_id == me, ack.c.status == "Pending"))).mappings()]
    inbox = [dict(r) for r in ctx.conn.execute(
        select(c.c.id, c.c.subject, c.c.communication_type, c.c.event_id, c.c.created_at)
        .join(t("notification_jobs"), t("notification_jobs").c.communication_id == c.c.id)
        .where(and_(t("notification_jobs").c.recipient_user_id == me, t("notification_jobs").c.channel == "Portal"))
        .order_by(c.c.created_at.desc()).limit(20)).mappings()]
    reviews = [dict(r) for r in ctx.conn.execute(
        select(a.c.id, a.c.activity_code, a.c.title, a.c.event_id).where(and_(
            a.c.reviewer_user_id == me, a.c.status == "Submitted"))).mappings()]
    return {
        "upcoming_tests": events,
        "assigned_activities": open_,
        "due_today": [x for x in open_ if x["due_at"] and n <= x["due_at"] <= end_of_day],
        "overdue": [x for x in open_ if x["due_at"] and x["due_at"] < n],
        "blocked_by_dependencies": [x for x in open_ if x.get("dependency_state") in ("blocked", "waiting")],
        "pending_acknowledgements": pending_acks,
        "evidence_awaiting_submission": [x for x in open_ if x["evidence_required"] and x["id"] not in accepted],
        "reviews_awaiting_me": reviews,
        "notifications": inbox,
        "generated_at": n,
    }


def portfolio(ctx: Ctx, programme_id: UUID | None = None, horizon_days: int = 30) -> dict:
    authz.require_role(ctx, *authz.PORTFOLIO_VIEWERS)
    e, a, i, s, ack = (t(x) for x in ("test_events", "test_activities", "test_issues", "test_stakeholders", "test_acknowledgements"))
    n = now()
    scope = e.c.programme_id == programme_id if programme_id else e.c.id.isnot(None)
    by_status = dict(ctx.conn.execute(select(e.c.lifecycle_status, func.count()).where(scope).group_by(e.c.lifecycle_status)).all())
    delayed = [dict(r) for r in ctx.conn.execute(select(e.c.id, e.c.event_code, e.c.title, e.c.planned_start_at, e.c.lifecycle_status)
              .where(and_(scope, e.c.planned_start_at < n, e.c.lifecycle_status.in_(["Draft", "Planned", "Mobilising", "Readiness", "Ready"])))).mappings()]
    upcoming = [dict(r) for r in ctx.conn.execute(select(e.c.id, e.c.event_code, e.c.title, e.c.planned_start_at, e.c.lifecycle_status, e.c.criticality)
               .where(and_(scope, e.c.planned_start_at.between(n, n + timedelta(days=horizon_days)),
                           e.c.lifecycle_status.notin_(["Cancelled", "Closed"]))).order_by(e.c.planned_start_at)).mappings()]
    r = t("test_readiness_items")
    at_risk = [dict(x) for x in ctx.conn.execute(
        select(e.c.id, e.c.event_code, e.c.title, func.count(r.c.id).label("unmet_mandatory"))
        .join(r, r.c.event_id == e.c.id)
        .where(and_(scope, r.c.is_mandatory, r.c.status != "Ready",
                    e.c.planned_start_at.between(n, n + timedelta(days=horizon_days))))
        .group_by(e.c.id, e.c.event_code, e.c.title)).mappings()]
    overdue = ctx.conn.execute(select(func.count()).select_from(a.join(e, e.c.id == a.c.event_id)).where(and_(
        scope, a.c.due_at < n, a.c.status != "Completed", e.c.lifecycle_status.notin_(["Closed", "Cancelled"])))).scalar()
    ack_stats = ctx.conn.execute(select(
        func.count().filter(ack.c.status == "Acknowledged"), func.count().filter(ack.c.status == "Pending"),
        func.count().filter(and_(ack.c.status == "Pending", ack.c.due_at < n)))
        .select_from(ack.join(s, s.c.id == ack.c.stakeholder_id).join(e, e.c.id == s.c.event_id)).where(scope)).one()
    findings = dict(ctx.conn.execute(select(i.c.severity, func.count()).select_from(i.join(e, e.c.id == i.c.event_id))
                                     .where(and_(scope, i.c.issue_type == "Finding", i.c.status != "Closed")).group_by(i.c.severity)).all())
    workload = [dict(x) for x in ctx.conn.execute(
        select(t("users").c.display_name, func.count(a.c.id).label("open_activities"))
        .select_from(a.join(s, s.c.id == a.c.stakeholder_id).join(t("users"), t("users").c.id == s.c.user_id).join(e, e.c.id == a.c.event_id))
        .where(and_(scope, a.c.status != "Completed")).group_by(t("users").c.display_name)
        .order_by(func.count(a.c.id).desc()).limit(10)).mappings()]
    closed = by_status.get("Closed", 0)
    planned_total = sum(v for k, v in by_status.items() if k != "Cancelled")
    return {
        "by_status": by_status, "completion": {"closed": closed, "planned": planned_total,
                                               "percent": round(100 * closed / planned_total, 1) if planned_total else 0.0},
        "delayed": delayed, "upcoming": upcoming, "at_risk": at_risk, "overdue_activities": overdue,
        "acknowledgements": {"acknowledged": ack_stats[0], "pending": ack_stats[1], "overdue": ack_stats[2]},
        "open_findings_by_severity": findings, "workload": workload,
        "data_as_of": n,   # §6: data freshness is displayed
    }


def timeline(ctx: Ctx, event_id: UUID, limit: int = 500) -> list[dict]:
    event(ctx, event_id)
    lg, au, u = t("test_execution_logs"), t("test_event_audit"), t("users")
    names = {r.id: r.display_name for r in ctx.conn.execute(select(u.c.id, u.c.display_name))}
    items = [{"kind": "log", "type": r["log_type"], "message": r["message"], "source": r["source"],
              "actor": names.get(r["actor_user_id"]), "occurred_at": r["occurred_at"], "recorded_at": r["recorded_at"],
              "activity_id": r["activity_id"]}
             for r in ctx.conn.execute(select(lg).where(lg.c.event_id == event_id)).mappings()]
    if not ctx.me.is_external:
        items += [{"kind": "audit", "type": r["action"], "entity": r["entity_type"], "entity_id": r["entity_id"],
                   "actor": names.get(r["actor_user_id"]) or "system", "occurred_at": r["occurred_at"], "seq": r["seq"]}
                  for r in ctx.conn.execute(select(au).where(au.c.event_id == event_id)).mappings()]
    return sorted(items, key=lambda x: x["occurred_at"])[-limit:]


def communications(ctx: Ctx, event_id: UUID) -> list[dict]:
    ev = event(ctx, event_id)
    if not ctx.me.has(Role.AUDITOR, Role.RISK):
        authz.require_manage(ctx, ev)
    c, nj, ack = t("test_communications"), t("notification_jobs"), t("test_acknowledgements")
    out = []
    for r in ctx.conn.execute(select(c).where(c.c.event_id == event_id).order_by(c.c.created_at)).mappings():
        jobs = dict(ctx.conn.execute(select(nj.c.delivery_status, func.count()).where(nj.c.communication_id == r["id"])
                                     .group_by(nj.c.delivery_status)).all())
        acks = dict(ctx.conn.execute(select(ack.c.status, func.count()).where(ack.c.communication_id == r["id"])
                                     .group_by(ack.c.status)).all())
        out.append({**dict(r), "delivery": jobs, "acknowledgements": acks})
    return out
