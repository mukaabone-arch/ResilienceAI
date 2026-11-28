"""Connect an organisation to Ansible Automation Platform (run with the owner database login, by operations).

    python -m scripts.automation add-aap BANK "Bank AAP" https://aap.bank.example/api/controller/v2 --token-env AAP_TOKEN
        Register an AAP instance. Prints the webhook URL and secret for AAP's job notifications ONCE.
        The API and worker read the AAP token from the environment variable named here (fill it from the vault).

    python -m scripts.automation list BANK
    python -m scripts.automation rotate-webhook BANK "Bank AAP"     new webhook secret (update it in AAP)
    python -m scripts.automation disable BANK "Bank AAP"            stop using it (links and history are kept)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
import uuid

from sqlalchemy import text

from app.db import worker_engine


def _tenant(conn, code):
    row = conn.execute(text("SELECT id, code, name FROM tenants WHERE upper(code) = upper(:c)"), {"c": code}).mappings().first()
    if not row:
        sys.exit(f"No organisation with code {code}")
    return row


def _integration(conn, tid, name):
    row = conn.execute(text("SELECT * FROM automation_integrations WHERE tenant_id = :t AND name = :n"),
                       {"t": tid, "n": name}).mappings().first()
    if not row:
        sys.exit(f"No integration named {name}")
    return row


def create_aap(conn, tenant_id, name: str, base_url: str, token_env: str) -> tuple[uuid.UUID, str]:
    """Returns (integration id, webhook secret)."""
    uid, iid = uuid.uuid4(), uuid.uuid4()
    conn.execute(text("""INSERT INTO users (id, tenant_id, idp_subject, display_name, department, roles, is_external,
                         notification_channels) VALUES (:i, :t, :s, :n, 'Automation', '{}', false, '{Portal}')"""),
                 {"i": uid, "t": tenant_id, "s": f"automation:{iid}", "n": f"{name} (automation)"})
    secret = secrets.token_urlsafe(32)
    conn.execute(text("""INSERT INTO automation_integrations (id, tenant_id, name, kind, base_url, token_env, webhook_secret_hash, user_id)
                         VALUES (:i, :t, :n, 'aap', :u, :e, :h, :usr)"""),
                 {"i": iid, "t": tenant_id, "n": name, "u": base_url.rstrip("/"), "e": token_env,
                  "h": hashlib.sha256(secret.encode()).hexdigest(), "usr": uid})
    return iid, secret


def _show_webhook(iid, secret):
    from app.config import settings
    portal = settings.portal_base_url.rstrip("/")
    print("\n  In AAP: Administration > Notifiers > Add, type Webhook, then add it to each linked job template")
    print("  (or the organisation) for Start, Success and Failure. Shown once:\n")
    print(f"  Target URL:     {portal}/api/v1/integrations/{iid}/aap-events")
    print("  HTTP headers:   {\"Authorization\": \"Bearer " + secret + "\"}")
    print("  HTTP method:    POST\n")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-aap")
    add.add_argument("code")
    add.add_argument("name")
    add.add_argument("base_url")
    add.add_argument("--token-env", default="AAP_TOKEN")
    sub.add_parser("list").add_argument("code")
    for c in ("rotate-webhook", "disable"):
        p = sub.add_parser(c)
        p.add_argument("code")
        p.add_argument("name")
    a = ap.parse_args(argv)
    with worker_engine().begin() as conn:
        tn = _tenant(conn, a.code)
        if a.cmd == "add-aap":
            if not a.base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                sys.exit("Use https for AAP (plain http only for a local stand-in)")
            iid, secret = create_aap(conn, tn["id"], a.name, a.base_url, a.token_env)
            print(f"{a.name} connected for {tn['name']} (API root {a.base_url}).")
            print(f"Set {a.token_env} on the API and worker to the token of an AAP user that may only execute the job "
                  "templates you will link.")
            _show_webhook(iid, secret)
        elif a.cmd == "rotate-webhook":
            i = _integration(conn, tn["id"], a.name)
            secret = secrets.token_urlsafe(32)
            conn.execute(text("UPDATE automation_integrations SET webhook_secret_hash = :h WHERE id = :i"),
                         {"h": hashlib.sha256(secret.encode()).hexdigest(), "i": i["id"]})
            print("New webhook secret; update the notifier's header in AAP.")
            _show_webhook(i["id"], secret)
        elif a.cmd == "disable":
            i = _integration(conn, tn["id"], a.name)
            conn.execute(text("UPDATE automation_integrations SET disabled_at = now() WHERE id = :i"), {"i": i["id"]})
            print(f"{a.name} disabled. Linked activities can no longer run; their history is kept.")
        else:
            for r in conn.execute(text("""
                    SELECT i.name, i.base_url, i.token_env, i.disabled_at,
                           (SELECT count(*) FROM activity_runbooks b WHERE b.integration_id = i.id) AS links,
                           (SELECT count(*) FROM automation_runs r JOIN activity_runbooks b ON b.id = r.runbook_id
                             WHERE b.integration_id = i.id) AS runs
                    FROM automation_integrations i WHERE i.tenant_id = :t ORDER BY i.name"""), {"t": tn["id"]}).mappings():
                print(f"  {r['name']:<24} {'disabled' if r['disabled_at'] else 'active':<9} {r['links']} linked, {r['runs']} runs  "
                      f"{r['base_url']}  token from ${r['token_env']}"
                      + ("" if os.environ.get(r["token_env"]) else "  (not set in this shell)"))


if __name__ == "__main__":
    main()
