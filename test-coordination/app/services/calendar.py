"""Annual test calendar (Screen 02, RA-012/016): visible events in a window with conflict flags, plus blackouts."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, insert, select

from .. import authz
from ..db import t
from ..domain.enums import Role
from ..errors import Unprocessable
from ..security import Ctx
from . import audit
from .events import list_events

INACTIVE = {"Cancelled", "Closed"}


def calendar(ctx: Ctx, start: datetime, end: datetime, *, category: str | None = None,
             criticality: str | None = None, programme_id: UUID | None = None) -> dict:
    if end <= start:
        raise Unprocessable("end must be after start")
    if (end - start).days > 400:
        raise Unprocessable("A calendar window can cover at most 400 days")
    evs = list_events(ctx, start_from=start, start_to=end, category=category, criticality=criticality,
                      programme_id=programme_id, limit=1000)
    ids = [e["id"] for e in evs]
    sc, s, u = t("test_scope_items"), t("test_stakeholders"), t("users")
    services: dict[UUID, set[UUID]] = defaultdict(set)
    names: dict[UUID, list[str]] = defaultdict(list)
    for r in ctx.conn.execute(select(sc.c.event_id, sc.c.business_service_id, sc.c.name).where(sc.c.event_id.in_(ids or [None]))):
        if r.business_service_id:
            services[r.event_id].add(r.business_service_id)
        names[r.event_id].append(r.name)
    teams = defaultdict(int)
    for r in ctx.conn.execute(select(s.c.event_id).where(and_(s.c.event_id.in_(ids or [None]), s.c.is_active))):
        teams[r.event_id] += 1
    people = {r.id: r.display_name for r in ctx.conn.execute(select(u.c.id, u.c.display_name))}

    b = t("blackout_periods")
    blackouts = [dict(r) for r in ctx.conn.execute(
        select(b).where(and_(b.c.starts_at < end, b.c.ends_at > start)).order_by(b.c.starts_at)).mappings()]

    out = []
    for e in evs:
        flags = []
        if e["lifecycle_status"] not in INACTIVE:
            for bo in blackouts:
                if bo["starts_at"] < e["planned_end_at"] and bo["ends_at"] > e["planned_start_at"]:
                    flags.append({"kind": "blackout", "with": bo["name"]})
            for o in evs:
                if o["id"] == e["id"] or o["lifecycle_status"] in INACTIVE:
                    continue
                if not (o["planned_start_at"] < e["planned_end_at"] and o["planned_end_at"] > e["planned_start_at"]):
                    continue
                if services[e["id"]] & services[o["id"]]:
                    flags.append({"kind": "shared_service", "with": o["event_code"], "title": o["title"], "event_id": o["id"]})
                if e["test_manager_user_id"] and e["test_manager_user_id"] == o["test_manager_user_id"]:
                    flags.append({"kind": "same_test_manager", "with": o["event_code"], "title": o["title"], "event_id": o["id"]})
        out.append({k: e[k] for k in ("id", "event_code", "title", "category", "criticality", "lifecycle_status",
                                       "outcome", "planned_start_at", "planned_end_at", "timezone", "programme_id")}
                   | {"test_manager": people.get(e["test_manager_user_id"]), "owner": people.get(e["owner_user_id"]),
                      "scope": names[e["id"]], "participants": teams[e["id"]], "conflicts": flags})
    return {"start": start, "end": end, "events": out, "blackouts": blackouts}


def list_blackouts(ctx: Ctx) -> list[dict]:
    b = t("blackout_periods")
    return [dict(r) for r in ctx.conn.execute(select(b).order_by(b.c.starts_at)).mappings()]


def create_blackout(ctx: Ctx, data: dict) -> dict:
    authz.require_role(ctx, Role.PROGRAMME_MANAGER, Role.ADMIN)
    if data["ends_at"] <= data["starts_at"]:
        raise Unprocessable("A blackout must end after it starts")
    b = t("blackout_periods")
    r = dict(ctx.conn.execute(insert(b).values(tenant_id=ctx.me.tenant_id, **data).returning(b)).mappings().one())
    audit.record(ctx, "blackout.created", "BlackoutPeriod", r["id"], new=r)
    return r


def business_services(ctx: Ctx) -> list[dict]:
    bs = t("business_services")
    return [dict(r) for r in ctx.conn.execute(select(bs).order_by(bs.c.name)).mappings()]


def templates(ctx: Ctx) -> list[dict]:
    tt = t("test_templates")
    return [dict(r) for r in ctx.conn.execute(
        select(tt.c.id, tt.c.name, tt.c.category, tt.c.default_duration, tt.c.version, tt.c.description)
        .where(tt.c.is_active).order_by(tt.c.name)).mappings()]
