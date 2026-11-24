"""Hash-chained audit records for tamper detection (spec §18.5, RA-007, RA-078).

Each audit row stores the previous row's hash and its own hash over a canonical
JSON encoding. Any edit, deletion or reordering breaks the chain. This is
tamper *evidence*; immutability additionally needs DB grants/triggers (in the
migration) and export to WORM storage (deployment concern).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

GENESIS = "0" * 64


def _default(o: Any) -> Any:
    if isinstance(o, (UUID, Decimal)):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"Unserialisable {type(o)}")


def canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_default)


def compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(payload)).encode()).hexdigest()


def hash_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Fields covered by the hash (everything material, excluding the hashes themselves)."""
    keys = ("id", "tenant_id", "seq", "event_id", "actor_user_id", "action", "entity_type",
            "entity_id", "old_values", "new_values", "occurred_at", "correlation_id")
    out = {k: row.get(k) for k in keys}
    # Normalise timestamps to UTC ISO so DB round-trips hash identically.
    ts = out.get("occurred_at")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            raise ValueError("Audit timestamps must be timezone-aware")
        out["occurred_at"] = ts.astimezone(timezone.utc).isoformat(timespec="microseconds")
    for k in ("id", "tenant_id", "event_id", "actor_user_id", "entity_id", "correlation_id"):
        if out.get(k) is not None:
            out[k] = str(out[k])
    return out


def verify(rows: list[dict[str, Any]]) -> tuple[bool, int | None]:
    """Verify rows ordered by seq. Returns (ok, first_bad_seq)."""
    prev = GENESIS
    expected_seq = 1
    for r in rows:
        if r["seq"] != expected_seq or r["prev_hash"] != prev:
            return False, r["seq"]
        if compute_hash(prev, hash_payload(r)) != r["row_hash"]:
            return False, r["seq"]
        prev = r["row_hash"]
        expected_seq += 1
    return True, None
