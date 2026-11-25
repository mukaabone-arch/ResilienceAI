"""Recurring test series (RA-015): preview every occurrence's clashes, then create them all in one transaction."""
from __future__ import annotations

from sqlalchemy import insert, select

from .. import authz
from ..db import t
from ..domain import recurrence as rc
from ..domain.enums import Role
from ..errors import Conflict, Forbidden, Unprocessable
from ..security import Ctx
from . import audit
from .events import create_event, schedule_conflicts


def _plan(ctx: Ctx, data: dict) -> list[dict]:
    try:
        occ = rc.occurrences(data["planned_start_at"], data["planned_end_at"], data.get("timezone", "UTC"),
                             data["frequency"], data["count"])
    except rc.RecurrenceError as e:
        raise Unprocessable(str(e)) from e
    plan = []
    for i, (s, e) in enumerate(occ, start=1):
        c = schedule_conflicts(ctx, s, e, manager=data.get("test_manager_user_id"),
                               service_ids=data.get("business_service_ids") or None)
        plan.append({"index": i, "title": rc.occurrence_title(data["title"], s, data.get("timezone", "UTC"), data["frequency"]),
                     "planned_start_at": s, "planned_end_at": e, "conflicts": c,
                     "in_blackout": bool(c["blackouts"])})
    return plan


def preview(ctx: Ctx, data: dict) -> dict:
    authz.require_role(ctx, Role.PROGRAMME_MANAGER, Role.TEST_MANAGER)
    plan = _plan(ctx, data)
    return {"frequency": data["frequency"], "label": rc.LABEL[data["frequency"]], "occurrences": plan,
            "in_blackout": sum(p["in_blackout"] for p in plan),
            "with_clashes": sum(bool(p["conflicts"]["shared_service_overlaps"] or p["conflicts"]["test_manager_overlaps"]) for p in plan)}


def create(ctx: Ctx, data: dict) -> dict:
    authz.require_role(ctx, Role.PROGRAMME_MANAGER, Role.TEST_MANAGER)
    plan = _plan(ctx, data)
    blocked = [p for p in plan if p["in_blackout"]]
    skip = data.get("skip_blackout_occurrences", False)
    if blocked and not skip:
        if not data.get("blackout_override_reason"):
            raise Conflict(f"{len(blocked)} occurrence(s) fall in a blackout period. Skip them, or give an override reason.",
                           [f"{p['title']}: {', '.join(b['name'] for b in p['conflicts']['blackouts'])}" for p in blocked])
        if not ctx.me.has(Role.PROGRAMME_MANAGER):
            raise Forbidden("Only a DR programme manager can schedule inside a blackout")
    to_make = [p for p in plan if not (skip and p["in_blackout"])]
    if len(to_make) < 1:
        raise Unprocessable("Every occurrence falls in a blackout period; nothing to create")

    st = t("test_event_series")
    series = dict(ctx.conn.execute(insert(st).values(
        tenant_id=ctx.me.tenant_id, title=data["title"], frequency=data["frequency"], occurrences=len(to_make),
        created_by=ctx.me.user_id).returning(st)).mappings().one())
    audit.record(ctx, "series.created", "Series", series["id"],
                 new={"title": data["title"], "frequency": data["frequency"], "occurrences": len(to_make),
                      "skipped": [p["title"] for p in plan if p not in to_make]})
    base = {k: v for k, v in data.items() if k not in ("frequency", "count", "skip_blackout_occurrences")}
    made = []
    for n, p in enumerate(to_make, start=1):
        ev = create_event(ctx, {**base, "title": p["title"], "planned_start_at": p["planned_start_at"],
                                "planned_end_at": p["planned_end_at"],
                                "blackout_override_reason": data.get("blackout_override_reason") if p["in_blackout"] else None},
                          series=(series["id"], n))
        made.append({k: ev[k] for k in ("id", "event_code", "title", "planned_start_at", "planned_end_at")})
    return {"series": series, "events": made, "skipped": [p["title"] for p in plan if p not in to_make]}


def siblings(ctx: Ctx, series_id) -> list[dict]:
    e = t("test_events")
    return [dict(r) for r in ctx.conn.execute(
        select(e.c.id, e.c.event_code, e.c.series_index, e.c.planned_start_at, e.c.lifecycle_status)
        .where(e.c.series_id == series_id).order_by(e.c.series_index)).mappings()]
