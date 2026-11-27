"""Background worker: runs the scheduled jobs for every tenant.

Usage:  python -m scripts.worker            (runs every minute until stopped)
        python -m scripts.worker --once     (one pass, e.g. from cron)

Jobs: scan evidence stored while the malware scanner was unreachable, deliver queued notifications, escalate overdue acknowledgements and
activities, and write a status summary for each live test every 30 minutes.
Each job is safe to run repeatedly; production would schedule these with
Temporal (spec §22) instead of this loop.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from sqlalchemy import text

from app.ai.service import run_live_summaries
from app.db import worker_engine
from app.services.evidence import rescan_pending
from app.services.notifications import dispatch_all_tenants
from app.services.people import run_escalations


def one_pass() -> dict:
    with worker_engine().connect() as c:
        tenants = [r[0] for r in c.execute(text("SELECT id FROM tenants"))]
    out = {"escalations": 0, "summaries": 0, "scanned": 0, "quarantined": 0}
    for tid in tenants:
        sc = rescan_pending(tid)                 # first, so a quarantine notice goes out in this pass
        out["scanned"] += sc["clean"] + sc["infected"]
        out["quarantined"] += sc["infected"]
        e = run_escalations(tid)
        out["escalations"] += e["ack_escalations"] + e["overdue_escalations"]
        out["summaries"] += run_live_summaries(tid)
    d = dispatch_all_tenants()          # after the jobs, so their messages go out in the same pass
    out.update(sent=d.sent, failed=d.failed, dead=d.dead)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--every", type=int, default=60, help="seconds between passes")
    a = ap.parse_args()
    while True:
        r = one_pass()
        print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z", r, flush=True)
        if a.once:
            return
        time.sleep(a.every)


if __name__ == "__main__":
    main()
