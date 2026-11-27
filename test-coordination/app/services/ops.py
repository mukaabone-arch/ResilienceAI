"""Operational monitoring: what the worker records each pass, and the deep health report built from it.

The worker (owner login) records:
- a heartbeat with the figures monitoring cares about: notifications that died or are stuck, evidence waiting too
  long for its malware scan, open break-glass sessions, AI answers that fell back to rules;
- the result of re-verifying each organisation's hash-chained audit trail, every AUDIT_VERIFY_HOURS.

The API reads those (plus live database and evidence-store checks) for GET /health/deep and the admin view, so it
never needs to read across organisations itself. Overall status: "ok", "warn" (someone should look today) or "fail"
(page someone now; /health/deep answers 503).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .. import config
from ..domain import audit_chain

log = logging.getLogger("resilienceai.ops")

HEARTBEAT_STALE = timedelta(minutes=5)       # the worker runs every minute
STUCK_QUEUE = timedelta(minutes=15)
STUCK_SCAN = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert(conn, check: str, scope: str, ok: bool, detail: dict) -> None:
    import json
    conn.execute(text("""INSERT INTO ops_status (check_name, scope, ok, detail, checked_at)
                         VALUES (:c, :s, :o, CAST(:d AS jsonb), now())
                         ON CONFLICT (check_name, scope) DO UPDATE
                         SET ok = EXCLUDED.ok, detail = EXCLUDED.detail, checked_at = EXCLUDED.checked_at"""),
                 {"c": check, "s": scope, "o": ok, "d": json.dumps(detail, default=str)})


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------
def record_worker_pass(conn, pass_result: dict) -> dict:
    """Heartbeat plus the figures monitoring alerts on. `conn` is the worker's (owner) connection."""
    q = lambda sql, **p: conn.execute(text(sql), p).scalar() or 0  # noqa: E731
    at = _now()
    figures = {
        "dead_notifications_24h": q("SELECT count(*) FROM notification_jobs WHERE delivery_status = 'Dead' "
                                    "AND created_at > :t", t=at - timedelta(hours=24)),
        "stuck_notifications": q("SELECT count(*) FROM notification_jobs WHERE delivery_status IN ('Queued', 'Failed') "
                                 "AND next_attempt_at < :t", t=at - STUCK_QUEUE),
        "scans_waiting": q("SELECT count(*) FROM test_evidence WHERE scan_status = 'Pending' AND submitted_at < :t",
                           t=at - STUCK_SCAN),
        "scan_errors": q("SELECT count(*) FROM test_evidence WHERE scan_status = 'Error'"),
        "ai_fallbacks_24h": q("SELECT count(*) FROM ai_activity_records WHERE fallback_reason IS NOT NULL "
                              "AND model IS NOT NULL AND created_at > :t", t=at - timedelta(hours=24)),
        "break_glass_open": q("SELECT count(*) FROM break_glass_sessions WHERE status IN ('Pending', 'Active') "
                              "AND (expires_at IS NULL OR expires_at > now())")
                            if conn.execute(text("SELECT to_regclass('public.break_glass_sessions')")).scalar() else 0,
    }
    _upsert(conn, "worker_pass", "installation", True, {"figures": figures, "last_pass": pass_result})
    return figures


def verify_audit_chains(conn, *, force: bool = False) -> list[dict]:
    """Re-verify each organisation's audit chain if its last check is older than AUDIT_VERIFY_HOURS."""
    every = timedelta(hours=config.settings.audit_verify_hours)
    last = {r.scope: r.checked_at for r in conn.execute(text(
        "SELECT scope, checked_at FROM ops_status WHERE check_name = 'audit_chain'"))}
    out = []
    for tid, code in conn.execute(text("SELECT id, code FROM tenants ORDER BY code")).all():
        if not force and code in last and _now() - last[code] < every:
            continue
        # Streamed in batches (statement-level option: it mustn't stick to the connection).
        rows = conn.execute(text("SELECT * FROM test_event_audit WHERE tenant_id = :t ORDER BY seq")
                            .execution_options(yield_per=2000), {"t": tid}).mappings()
        count = 0

        def counted(it):
            nonlocal count
            for r in it:
                count += 1
                yield dict(r)
        ok, bad = audit_chain.verify(counted(rows))
        detail = {"records": count, "first_invalid_seq": bad}
        _upsert(conn, "audit_chain", code, ok, detail)
        if ok:
            log.info("audit chain verified for %s: %s records", code, count)
        else:
            log.error("AUDIT CHAIN BROKEN for %s at seq %s: records may have been altered", code, bad)
        out.append({"organisation": code, "ok": ok, **detail})
    return out


# ---------------------------------------------------------------------------
# API side
# ---------------------------------------------------------------------------
def deep_status() -> dict:
    from ..db import engine
    from . import storage
    checks: list[dict] = []

    def add(name, level, message, **detail):
        checks.append({"check": name, "status": level, "message": message, **({"detail": detail} if detail else {})})

    rows: list = []
    try:
        with engine().connect() as c:
            c.execute(text("SELECT 1"))
            rows = c.execute(text("SELECT check_name, scope, ok, detail, checked_at FROM ops_status")).mappings().all()
        add("database", "ok", "Database answers")
    except Exception as e:  # noqa: BLE001
        add("database", "fail", f"Database unreachable ({type(e).__name__})")
    try:
        st = storage.store()
        problems = st.check() if hasattr(st, "check") else []
        add("evidence_store", "fail" if problems else "ok", "; ".join(problems) if problems else f"Evidence store ({st.backend}) ready")
    except Exception as e:  # noqa: BLE001
        add("evidence_store", "fail", f"Evidence store unreachable ({type(e).__name__})")

    now = _now()
    hb = next((r for r in rows if r["check_name"] == "worker_pass"), None)
    if hb is None:
        add("worker", "fail", "The worker has never reported. Is it running?")
    else:
        age = now - hb["checked_at"]
        if age > HEARTBEAT_STALE:
            add("worker", "fail", f"No worker pass for {int(age.total_seconds() // 60)} minutes", last_pass=hb["checked_at"])
        else:
            add("worker", "ok", "Worker running", last_pass=hb["checked_at"])
        f = (hb["detail"] or {}).get("figures", {})
        if f.get("dead_notifications_24h"):
            add("notifications", "warn", f"{f['dead_notifications_24h']} notification(s) gave up in the last 24 hours")
        elif f.get("stuck_notifications"):
            add("notifications", "warn", f"{f['stuck_notifications']} notification(s) waiting more than 15 minutes")
        else:
            add("notifications", "ok", "Notifications are being delivered")
        if f.get("scans_waiting") or f.get("scan_errors"):
            add("malware_scanning", "warn", f"{f.get('scans_waiting', 0)} file(s) waiting over 30 minutes for a scan, "
                                            f"{f.get('scan_errors', 0)} scan error(s)")
        else:
            add("malware_scanning", "ok", "No files waiting for a scan")
        if f.get("break_glass_open"):
            add("break_glass", "warn", f"{f['break_glass_open']} break-glass session(s) open or waiting for approval")
        if config.settings.ai_provider == "anthropic":
            n = f.get("ai_fallbacks_24h", 0)
            add("ai", "warn" if n else "ok", f"{n} AI answer(s) fell back to rules in the last 24 hours" if n
                else "AI answers are being used")

    chains = [r for r in rows if r["check_name"] == "audit_chain"]
    every = timedelta(hours=config.settings.audit_verify_hours)
    broken = [r for r in chains if not r["ok"]]
    stale = [r for r in chains if now - r["checked_at"] > every + timedelta(hours=2)]
    if broken:
        add("audit_chain", "fail", "Audit trail does not verify for " + ", ".join(
            f"{r['scope']} (from record {r['detail'].get('first_invalid_seq')})" for r in broken))
    elif not chains:
        add("audit_chain", "warn", "The audit trails haven't been verified yet")
    elif stale:
        add("audit_chain", "warn", "Audit verification is overdue for " + ", ".join(r["scope"] for r in stale))
    else:
        newest = min(r["checked_at"] for r in chains)
        total = sum(int(r["detail"].get("records", 0)) for r in chains)
        add("audit_chain", "ok", f"Verified for {len(chains)} organisation{'s' if len(chains) != 1 else ''} "
            f"({total:,} records)", oldest_check=newest, records=total)

    levels = [c["status"] for c in checks]
    overall = "fail" if "fail" in levels else "warn" if "warn" in levels else "ok"
    return {"status": overall, "checked_at": now, "checks": checks}
