"""The bank's own AI agents: register, rotate, suspend (run with the owner database login, by operations).

    python -m scripts.agents add BANK "DBA Agent" --accountable rahul.dba@bank.example \\
        --purpose "Checks standby lag and prepares recovery points for DR tests" \\
        --scopes read,acknowledge,update_status,report_blocker,post_update,attach_evidence,request_run --days 90
        Prints the agent's key ONCE. Put it in the vault the agent reads from.

    python -m scripts.agents rotate BANK "DBA Agent" [--days 90]     new key (old one stops at once)
    python -m scripts.agents scopes BANK "DBA Agent" read,post_update change what it may do
    python -m scripts.agents resume BANK "DBA Agent"                 lift a suspension (after review)
    python -m scripts.agents list BANK

Scopes: read (its tests and activities), acknowledge (accept being assigned), update_status (start, submit),
report_blocker, post_update (progress notes), attach_evidence, request_run (ask for its runbook to run; a person
decides). There is no scope for approving, reviewing or changing a test's stage.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db import worker_engine
from app.services.agents import PREFIX, SCOPES, key_hash


def _tenant(conn, code):
    row = conn.execute(text("SELECT id, code, name FROM tenants WHERE upper(code) = upper(:c)"), {"c": code}).mappings().first()
    if not row:
        sys.exit(f"No organisation with code {code}")
    return row


def _agent(conn, tid, name):
    row = conn.execute(text("SELECT * FROM agent_identities WHERE tenant_id = :t AND name = :n"), {"t": tid, "n": name}).mappings().first()
    if not row:
        sys.exit(f"No agent named {name}")
    return row


def _scopes(raw: str) -> list[str]:
    s = [x.strip() for x in raw.split(",") if x.strip()]
    bad = [x for x in s if x not in SCOPES]
    if bad:
        sys.exit(f"Unknown scope(s): {', '.join(bad)}. Valid: {', '.join(SCOPES)}")
    return s


def new_key() -> str:
    return PREFIX + secrets.token_urlsafe(36)


def create_agent(conn, tenant_id, name: str, purpose: str, accountable_id, scopes: list[str], days: int,
                 created_by: str = "operations") -> tuple[uuid.UUID, str]:
    uid, aid = uuid.uuid4(), uuid.uuid4()
    conn.execute(text("""INSERT INTO users (id, tenant_id, idp_subject, display_name, department, roles, is_external,
                         notification_channels) VALUES (:i, :t, :s, :n, 'AI agent', '{}', false, '{Portal}')"""),
                 {"i": uid, "t": tenant_id, "s": f"agent:{aid}", "n": f"{name} (agent)"})
    key = new_key()
    conn.execute(text("""INSERT INTO agent_identities (id, tenant_id, user_id, name, purpose, accountable_user_id, scopes, key_hash,
                         key_hint, expires_at, created_by) VALUES (:i, :t, :u, :n, :p, :a, :s, :h, :hint, :x, :by)"""),
                 {"i": aid, "t": tenant_id, "u": uid, "n": name, "p": purpose, "a": accountable_id, "s": scopes,
                  "h": key_hash(key), "hint": key[-6:], "x": datetime.now(timezone.utc) + timedelta(days=days), "by": created_by})
    return aid, key


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("code")
    add.add_argument("name")
    add.add_argument("--accountable", required=True, help="email of the person accountable for the agent")
    add.add_argument("--purpose", required=True)
    add.add_argument("--scopes", default="read,acknowledge,update_status,report_blocker,post_update,attach_evidence")
    add.add_argument("--days", type=int, default=90)
    rot = sub.add_parser("rotate")
    rot.add_argument("code")
    rot.add_argument("name")
    rot.add_argument("--days", type=int, default=90)
    sc = sub.add_parser("scopes")
    sc.add_argument("code")
    sc.add_argument("name")
    sc.add_argument("scopes")
    for c in ("resume",):
        p = sub.add_parser(c)
        p.add_argument("code")
        p.add_argument("name")
    sub.add_parser("list").add_argument("code")
    a = ap.parse_args(argv)
    if getattr(a, "days", 90) > 365 or getattr(a, "days", 90) < 1:
        sys.exit("Keys last 1 to 365 days")
    with worker_engine().begin() as conn:
        tn = _tenant(conn, a.code)
        if a.cmd == "add":
            person = conn.execute(text("SELECT id, display_name, is_external, is_active FROM users WHERE tenant_id = :t "
                                       "AND lower(email) = lower(:e)"), {"t": tn["id"], "e": a.accountable}).mappings().first()
            if not person or not person["is_active"] or person["is_external"]:
                sys.exit("The accountable person must be an active member of the bank's own staff (by work email)")
            aid, key = create_agent(conn, tn["id"], a.name, a.purpose, person["id"], _scopes(a.scopes), a.days)
            print(f"{a.name} registered for {tn['name']}; accountable: {person['display_name']}; scopes: {a.scopes}")
            print(f"\n  Agent key (shown once; store it in the vault the agent reads from):\n\n  {key}\n")
            print("  The agent calls the API with header  Authorization: Bearer <key>")
            print("  Add the agent to a test as a stakeholder and assign it activities, like a person.\n")
        elif a.cmd == "rotate":
            ag = _agent(conn, tn["id"], a.name)
            key = new_key()
            conn.execute(text("UPDATE agent_identities SET key_hash = :h, key_hint = :hint, expires_at = :x WHERE id = :i"),
                         {"h": key_hash(key), "hint": key[-6:], "x": datetime.now(timezone.utc) + timedelta(days=a.days), "i": ag["id"]})
            print(f"New key for {a.name} (the old one no longer works):\n\n  {key}\n")
        elif a.cmd == "scopes":
            ag = _agent(conn, tn["id"], a.name)
            conn.execute(text("UPDATE agent_identities SET scopes = :s WHERE id = :i"), {"s": _scopes(a.scopes), "i": ag["id"]})
            print(f"{a.name} may now: {a.scopes}")
        elif a.cmd == "resume":
            ag = _agent(conn, tn["id"], a.name)
            conn.execute(text("UPDATE agent_identities SET suspended_at = NULL, suspended_by = NULL, suspended_reason = NULL "
                              "WHERE id = :i"), {"i": ag["id"]})
            print(f"{a.name} can act again (it was suspended for: {ag['suspended_reason']}).")
        else:
            for r in conn.execute(text("""SELECT a.name, a.scopes, a.expires_at, a.last_used_at, a.suspended_at, a.key_hint,
                                                u.display_name AS accountable
                                         FROM agent_identities a JOIN users u ON u.id = a.accountable_user_id
                                         WHERE a.tenant_id = :t ORDER BY a.name"""), {"t": tn["id"]}).mappings():
                state = "suspended" if r["suspended_at"] else "expired" if r["expires_at"] < datetime.now(timezone.utc) else "active"
                print(f"  {r['name']:<22} {state:<9} accountable {r['accountable']:<16} key …{r['key_hint']}  "
                      f"expires {r['expires_at']:%d %b %Y}  last used {r['last_used_at'] or 'never'}\n      {', '.join(r['scopes'])}")


if __name__ == "__main__":
    main()
