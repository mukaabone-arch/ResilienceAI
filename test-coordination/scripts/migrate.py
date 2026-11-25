"""Apply pending SQL migrations in order. Safe to run on every deploy.

Usage:  python -m scripts.migrate              apply what's pending
        python -m scripts.migrate --status     list applied and pending
        python -m scripts.migrate --baseline   mark every migration file as applied without running it
                                               (only for a database built before migration tracking existed)

Runs as the schema owner (WORKER_DATABASE_URL). Each file runs in its own
transaction (the files carry BEGIN/COMMIT); a failed file leaves nothing behind
and stops the run. Applied files are recorded with a checksum, so an edited
migration is detected instead of silently skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

from app.db import worker_engine

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK = 727_101   # advisory lock id: only one migrator at a time


def files() -> list[pathlib.Path]:
    return sorted((ROOT / "migrations").glob("*.sql"))


def _sum(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def apply(conn, *, baseline: bool = False, out=print) -> list[str]:
    """Apply pending migrations on a SQLAlchemy connection owned by the schema owner."""
    raw = conn.connection.driver_connection        # raw driver: '%' in PL/pgSQL isn't a bind marker
    raw.execute("SELECT pg_advisory_lock(%s)", (LOCK,))
    try:
        raw.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                         version text PRIMARY KEY, checksum char(64) NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())""")
        raw.commit()
        applied = dict(raw.execute("SELECT version, checksum FROM schema_migrations").fetchall())
        if not applied and not baseline:
            exists = raw.execute("SELECT to_regclass('public.test_events') IS NOT NULL").fetchone()[0]
            if exists:
                raise SystemExit("This database already has tables but no migration history. If it is up to date, "
                                 "run: python -m scripts.migrate --baseline")
        done = []
        for f in files():
            v, cs = f.stem, _sum(f)
            if v in applied:
                if applied[v] != cs:
                    raise SystemExit(f"Migration {f.name} was edited after it was applied. Add a new migration instead.")
                continue
            if not baseline:
                out(f"applying {f.name}")
                raw.execute(f.read_text())
            raw.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (v, cs))
            raw.commit()
            done.append(v)
        return done
    finally:
        raw.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
        raw.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    a = ap.parse_args()
    with worker_engine().connect() as conn:
        if a.status:
            raw = conn.connection.driver_connection
            have = raw.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL").fetchone()[0]
            applied = {r[0] for r in raw.execute("SELECT version FROM schema_migrations")} if have else set()
            for f in files():
                print(("applied  " if f.stem in applied else "PENDING  ") + f.name)
            return
        done = apply(conn, baseline=a.baseline)
    print(("marked as applied: " if a.baseline else "applied: ") + (", ".join(done) if done else "nothing pending"))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            sys.exit(1)
        raise
