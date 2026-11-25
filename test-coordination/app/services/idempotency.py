"""Idempotency-Key support for critical writes (Appendix A API standards)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, insert, select, text

from ..db import t
from ..errors import Conflict
from ..security import Ctx


def run(ctx: Ctx, key: str | None, scope: str, body: Any, fn: Callable[[], Any], status: int = 201) -> Any:
    if not key:
        return fn()
    k = f"{scope}:{key}"[:200]
    h = hashlib.sha256(json.dumps(jsonable_encoder(body), sort_keys=True).encode()).hexdigest()
    ik = t("idempotency_keys")
    # Serialise concurrent retries of the same key.
    ctx.conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"idem:{ctx.me.tenant_id}:{k}"})
    prev = ctx.conn.execute(select(ik).where(and_(ik.c.key == k))).mappings().first()
    if prev:
        if prev["request_hash"] != h:
            raise Conflict("Idempotency-Key was reused with a different request body")
        return prev["response_body"]
    result = jsonable_encoder(fn())
    ctx.conn.execute(insert(ik).values(tenant_id=ctx.me.tenant_id, key=k, request_hash=h,
                                       response_status=status, response_body=result))
    return result
