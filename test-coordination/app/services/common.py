from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..authz import require_view
from ..db import t
from ..errors import Conflict, NotFound
from ..security import Ctx


def now() -> datetime:
    return datetime.now(timezone.utc)


def row(ctx: Ctx, table: str, id_: UUID, *, for_update: bool = False) -> dict:
    tb = t(table)
    q = select(tb).where(tb.c.id == id_)
    if for_update:
        q = q.with_for_update()
    r = ctx.conn.execute(q).mappings().first()
    if r is None:
        raise NotFound(f"{table.replace('_', ' ').rstrip('s')} not found")
    return dict(r)


def event(ctx: Ctx, event_id: UUID, *, for_update: bool = False) -> dict:
    ev = row(ctx, "test_events", event_id, for_update=for_update)
    require_view(ctx, ev)
    return ev


def child_of_event(ctx: Ctx, table: str, id_: UUID, *, for_update: bool = False) -> tuple[dict, dict]:
    """Load a child record and its (authorised) parent event."""
    r = row(ctx, table, id_, for_update=for_update)
    return r, event(ctx, r["event_id"])


def check_version(current: int, expected: int | None) -> None:
    if expected is not None and expected != current:
        raise Conflict(f"Record was modified by someone else (version {current}, you had {expected})")


def log(ctx: Ctx, event_id: UUID, log_type: str, message: str, *, activity_id: UUID | None = None,
        source: str = "User", occurred_at: datetime | None = None, external_reference: str | None = None) -> None:
    ctx.conn.execute(insert(t("test_execution_logs")).values(
        tenant_id=ctx.me.tenant_id, event_id=event_id, activity_id=activity_id, log_type=log_type,
        message=message, source=source, actor_user_id=ctx.me.user_id if source == "User" else None,
        occurred_at=occurred_at or now(), external_reference=external_reference))


def next_seq(ctx: Ctx, scope: str) -> int:
    s = t("event_code_sequences")
    stmt = pg_insert(s).values(tenant_id=ctx.me.tenant_id, scope=scope, last_value=1)
    stmt = stmt.on_conflict_do_update(index_elements=["tenant_id", "scope"],
                                      set_={"last_value": s.c.last_value + 1}).returning(s.c.last_value)
    return ctx.conn.execute(stmt).scalar_one()


def active_stakeholders(ctx: Ctx, event_id: UUID) -> list[dict]:
    s = t("test_stakeholders")
    return [dict(r) for r in ctx.conn.execute(
        select(s).where(and_(s.c.event_id == event_id, s.c.is_active))).mappings()]


def participant_ids(ctx: Ctx, ev: dict) -> list[UUID]:
    ids = [s["user_id"] for s in active_stakeholders(ctx, ev["id"])]
    return list(dict.fromkeys([ev["owner_user_id"], ev["test_manager_user_id"], *ids]))
