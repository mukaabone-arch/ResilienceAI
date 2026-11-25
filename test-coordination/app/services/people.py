"""Stakeholders, acknowledgements, ownership continuity and escalations (Epic 4, RA-047/048)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, insert, select, update

from .. import authz
from ..db import t, tenant_transaction
from ..domain.lifecycle import TERMINAL
from ..domain.enums import LifecycleStatus as S
from ..errors import Conflict, Forbidden, NotFound, Unprocessable
from ..security import Ctx, Principal
from . import audit, notifications
from .common import event, now, row


def _assert_open(ev: dict) -> None:
    if S(ev["lifecycle_status"]) in TERMINAL:
        raise Conflict(f"Event is {ev['lifecycle_status']}")


def add_stakeholders(ctx: Ctx, event_id: UUID, data: dict) -> list[dict]:
    """Add one user, or every member of a team (team-based assignment, RA-026)."""
    ev = event(ctx, event_id)
    authz.require_manage(ctx, ev)
    _assert_open(ev)
    users: list[UUID] = []
    if data.get("user_id"):
        users.append(data["user_id"])
    if data.get("team_id"):
        tm = t("team_members")
        row(ctx, "teams", data["team_id"])
        users += [r[0] for r in ctx.conn.execute(select(tm.c.user_id).where(tm.c.team_id == data["team_id"]))]
    if not users:
        raise Unprocessable("Provide user_id or team_id")
    s, u = t("test_stakeholders"), t("users")
    created = []
    for uid in dict.fromkeys(users):
        usr = ctx.conn.execute(select(u).where(u.c.id == uid)).mappings().first()
        if not usr or not usr["is_active"]:
            raise NotFound(f"User {uid} not found or inactive")
        dup = ctx.conn.execute(select(s.c.id).where(and_(s.c.event_id == event_id, s.c.user_id == uid, s.c.is_active))).first()
        if dup:
            if data.get("user_id") == uid:
                raise Conflict(f"{usr['display_name']} is already an active stakeholder")
            continue  # team expansion skips existing members
        st = dict(ctx.conn.execute(insert(s).values(
            tenant_id=ctx.me.tenant_id, event_id=event_id, user_id=uid, team_id=data.get("team_id"),
            event_role=data["event_role"], responsibility=data.get("responsibility"),
            is_primary=data.get("is_primary", True), alternate_user_id=data.get("alternate_user_id"),
        ).returning(s)).mappings().one())
        audit.record(ctx, "stakeholder.assigned", "Stakeholder", st["id"], event_id=event_id, new=st)
        notifications.enqueue(
            ctx, "stakeholder_assigned", event=ev, recipients=[uid], requires_ack=True,
            stakeholder_by_user={uid: st["id"]},
            subject=f"[{ev['event_code']}] You have been assigned to {ev['title']} as {st['event_role']}",
            body=f"Please acknowledge your assignment. Window: {ev['planned_start_at']} – {ev['planned_end_at']} ({ev['timezone']}).",
            dedupe=f"assign:{st['id']}")
        created.append({**st, "display_name": usr["display_name"]})
    return created


def list_stakeholders(ctx: Ctx, event_id: UUID, include_history: bool = False) -> list[dict]:
    ev = event(ctx, event_id)
    s, u = t("test_stakeholders"), t("users")
    q = select(s, u.c.display_name, u.c.email, u.c.department, u.c.designation, u.c.is_external).join(u, u.c.id == s.c.user_id) \
        .where(s.c.event_id == event_id)
    if not include_history:
        q = q.where(s.c.is_active)
    out = [dict(r) for r in ctx.conn.execute(q.order_by(s.c.assigned_at)).mappings()]
    if ctx.me.is_external:  # vendors see names/roles but not contact details of bank staff
        out = [{k: v for k, v in r.items() if k not in ("email",)} for r in out]
    _ = ev
    return out


def respond_to_assignment(ctx: Ctx, stakeholder_id: UUID, accept: bool, comment: str | None) -> dict:
    st = row(ctx, "test_stakeholders", stakeholder_id, for_update=True)
    if st["user_id"] != ctx.me.user_id:
        raise Forbidden("Only the assigned person can respond to an assignment")
    if not st["is_active"]:
        raise Conflict("Assignment is no longer active")
    ev = event(ctx, st["event_id"])
    s, ack, c = t("test_stakeholders"), t("test_acknowledgements"), t("test_communications")
    status = "Accepted" if accept else "Declined"
    new = dict(ctx.conn.execute(update(s).where(s.c.id == stakeholder_id).values(
        assignment_status=status, acknowledged_at=now()).returning(s)).mappings().one())
    ctx.conn.execute(update(ack).where(and_(ack.c.stakeholder_id == stakeholder_id, ack.c.status == "Pending")).values(
        status="Acknowledged" if accept else "Declined", responded_at=now(), response_comment=comment))
    audit.record(ctx, f"stakeholder.{status.lower()}", "Stakeholder", stakeholder_id, event_id=ev["id"],
                 old={"assignment_status": st["assignment_status"]}, new={"assignment_status": status, "comment": comment})
    if not accept:
        notifications.enqueue(ctx, "ack_sla_exceeded", event=ev, recipients=[ev["test_manager_user_id"], ev["owner_user_id"]],
                              subject=f"[{ev['event_code']}] {ctx.me.display_name} declined their assignment",
                              body=comment or "No reason given. Consider nominating an alternate.",
                              dedupe=f"declined:{stakeholder_id}")
    _ = c
    return new


def acknowledge(ctx: Ctx, ack_id: UUID, accept: bool, comment: str | None) -> dict:
    a = t("test_acknowledgements")
    r = row(ctx, "test_acknowledgements", ack_id, for_update=True)
    if r["recipient_user_id"] != ctx.me.user_id:
        raise Forbidden("Not your acknowledgement")
    if r["stakeholder_id"]:
        respond_to_assignment(ctx, r["stakeholder_id"], accept, comment)
        return row(ctx, "test_acknowledgements", ack_id)
    if r["status"] != "Pending":
        raise Conflict(f"Already {r['status']}")
    return dict(ctx.conn.execute(update(a).where(a.c.id == ack_id).values(
        status="Acknowledged" if accept else "Declined", responded_at=now(), response_comment=comment)
        .returning(a)).mappings().one())


def replace_stakeholder(ctx: Ctx, stakeholder_id: UUID, new_user_id: UUID, reason: str) -> dict:
    """Ownership continuity: move a participant's open activities to a replacement, keeping history."""
    old = row(ctx, "test_stakeholders", stakeholder_id, for_update=True)
    ev = event(ctx, old["event_id"])
    authz.require_manage(ctx, ev)
    _assert_open(ev)
    if not old["is_active"]:
        raise Conflict("Stakeholder is already inactive")
    if not reason:
        raise Unprocessable("reason is required")
    s, a, h = t("test_stakeholders"), t("test_activities"), t("activity_assignment_history")
    existing = ctx.conn.execute(select(s).where(and_(s.c.event_id == ev["id"], s.c.user_id == new_user_id, s.c.is_active))).mappings().first()
    if existing:
        new = dict(existing)
    else:
        new = add_stakeholders(ctx, ev["id"], {"user_id": new_user_id, "event_role": old["event_role"],
                                               "responsibility": old["responsibility"], "team_id": old["team_id"]})[0]
    ctx.conn.execute(update(s).where(s.c.id == stakeholder_id).values(
        is_active=False, assignment_status="Reassigned", replaced_by_id=new["id"], ended_at=now()))
    moved = ctx.conn.execute(select(a.c.id).where(and_(a.c.stakeholder_id == stakeholder_id, a.c.status != "Completed"))).all()
    for (aid,) in moved:
        ctx.conn.execute(update(a).where(a.c.id == aid).values(stakeholder_id=new["id"], version=a.c.version + 1, updated_at=now()))
        ctx.conn.execute(insert(h).values(tenant_id=ctx.me.tenant_id, activity_id=aid, from_stakeholder_id=stakeholder_id,
                                          to_stakeholder_id=new["id"], reason=reason, changed_by=ctx.me.user_id))
    audit.record(ctx, "stakeholder.replaced", "Stakeholder", stakeholder_id, event_id=ev["id"],
                 new={"replacement": new["id"], "reason": reason, "activities_moved": len(moved)})
    return {"replacement": new, "activities_moved": len(moved)}


# ---------------------------------------------------------------------------
# Escalations (scheduled job; runs per tenant as the system actor)
# ---------------------------------------------------------------------------
def _system_ctx(conn, tenant_id: UUID) -> Ctx:
    from uuid import uuid4
    return Ctx(conn=conn, correlation_id=uuid4(),
               me=Principal(tenant_id=tenant_id, user_id=None, display_name="system",  # type: ignore[arg-type]
                            roles=frozenset(), is_external=False))


def run_escalations(tenant_id: UUID, at: datetime | None = None) -> dict:
    at = at or now()
    out = {"ack_escalations": 0, "overdue_escalations": 0}
    with tenant_transaction(tenant_id) as conn:
        ctx = _system_ctx(conn, tenant_id)
        ack, s, tm, e, a = (t(x) for x in ("test_acknowledgements", "test_stakeholders", "teams", "test_events", "test_activities"))
        overdue_acks = conn.execute(select(ack, s.c.event_id, s.c.team_id).join(s, s.c.id == ack.c.stakeholder_id).where(and_(
            ack.c.status == "Pending", ack.c.due_at < at, s.c.is_active))).mappings().all()
        for r in overdue_acks:
            ev = dict(conn.execute(select(e).where(e.c.id == r["event_id"])).mappings().one())
            if S(ev["lifecycle_status"]) in TERMINAL:
                continue
            lead = conn.execute(select(tm.c.lead_user_id).where(tm.c.id == r["team_id"])).scalar() if r["team_id"] else None
            if notifications.enqueue(ctx, "ack_sla_exceeded", event=ev, recipients=[lead or ev["test_manager_user_id"]],
                                     subject=f"[{ev['event_code']}] Assignment not acknowledged within SLA",
                                     body="A stakeholder has not acknowledged their assignment.",
                                     dedupe=f"ack-sla:{r['id']}", trigger_type="Scheduled", actor=None):
                out["ack_escalations"] += 1
        overdue = conn.execute(select(a, s.c.user_id).outerjoin(s, s.c.id == a.c.stakeholder_id).where(and_(
            a.c.due_at < at, a.c.status.notin_(["Completed", "Submitted"])))).mappings().all()
        for r in overdue:
            ev = dict(conn.execute(select(e).where(e.c.id == r["event_id"])).mappings().one())
            if ev["lifecycle_status"] in (S.CLOSED.value, S.CANCELLED.value, S.DRAFT.value, S.PLANNED.value):
                continue
            if notifications.enqueue(ctx, "activity_overdue", event=ev, activity_id=r["id"],
                                     recipients=[r["user_id"], ev["test_manager_user_id"]],
                                     subject=f"[{ev['event_code']}] Overdue: {r['activity_code']} {r['title']}",
                                     body=f"Was due {r['due_at']}.", dedupe=f"overdue:{r['id']}:{r['due_at'].isoformat()}",
                                     trigger_type="Scheduled", actor=None):
                out["overdue_escalations"] += 1
    return out
