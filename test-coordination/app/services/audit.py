"""Append-only, hash-chained audit writer (RA-007)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text

from ..db import t
from ..domain import audit_chain
from ..security import Ctx


def _jsonable(v: Any) -> Any:
    if v is None:
        return None
    return json.loads(audit_chain.canonical(dict(v)))


def record(ctx: Ctx, action: str, entity_type: str, entity_id: UUID | None, *,
           event_id: UUID | None = None, old: dict | None = None, new: dict | None = None,
           actor: UUID | None | str = "me") -> None:
    a = t("test_event_audit")
    # Serialise chain appends per tenant for this transaction.
    ctx.conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"audit:{ctx.me.tenant_id}"})
    last = ctx.conn.execute(
        select(a.c.seq, a.c.row_hash).where(a.c.tenant_id == ctx.me.tenant_id)
        .order_by(a.c.seq.desc()).limit(1)).first()
    seq, prev = (last.seq + 1, last.row_hash) if last else (1, audit_chain.GENESIS)
    row = {
        "id": uuid4(), "tenant_id": ctx.me.tenant_id, "seq": seq, "event_id": event_id,
        "actor_user_id": ctx.me.user_id if actor == "me" else actor,
        "action": action, "entity_type": entity_type, "entity_id": entity_id,
        "old_values": _jsonable(old), "new_values": _jsonable(new),
        "occurred_at": datetime.now(timezone.utc), "correlation_id": ctx.correlation_id,
    }
    row["prev_hash"] = prev
    row["row_hash"] = audit_chain.compute_hash(prev, audit_chain.hash_payload(row))
    ctx.conn.execute(insert(a).values(**row))


def verify_tenant_chain(ctx: Ctx) -> dict:
    a = t("test_event_audit")
    rows = [dict(r) for r in ctx.conn.execute(
        select(a).where(a.c.tenant_id == ctx.me.tenant_id).order_by(a.c.seq)).mappings()]
    ok, bad = audit_chain.verify(rows)
    return {"valid": ok, "records": len(rows), "first_invalid_seq": bad,
            "verified_at": datetime.now(timezone.utc).isoformat()}


def diff(old: dict, new: dict) -> tuple[dict, dict]:
    keys = [k for k in new if old.get(k) != new.get(k)]
    return {k: old.get(k) for k in keys}, {k: new.get(k) for k in keys}

