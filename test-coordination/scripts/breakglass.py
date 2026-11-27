"""Break-glass holders for an organisation (run with the owner database login, by operations, never from the app).

    python -m scripts.breakglass add BANK priya.nair@bank.example "Priya Nair" EnterpriseProgrammeAdministrator
        Create a holder. Prints a passphrase and an authenticator secret ONCE: hand them over in person or put them
        in the bank's vault / sealed envelope. Default role: EnterpriseProgrammeAdministrator.

    python -m scripts.breakglass rotate BANK priya.nair@bank.example
        New passphrase and authenticator secret (after any use, and on the bank's schedule). Ends open sessions.

    python -m scripts.breakglass disable BANK priya.nair@bank.example
        Stop a holder signing in (they leave, or the credentials may be exposed). Ends open sessions.

    python -m scripts.breakglass list BANK
        Holders, when they were last used, and open sessions. Never shows secrets.

At least two holders are needed: a session must be approved by a different holder.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import uuid

from sqlalchemy import text

from app.db import worker_engine
from app.domain.enums import Role
from app.services.breakglass import hash_passphrase, new_totp_secret, otpauth_uri

WORDS = ("amber anchor basalt beacon cedar comet delta ember falcon glacier harbor indigo juniper kestrel lantern "
         "meadow nimbus orchid pebble quartz raven saffron tundra umber velvet willow zephyr cobalt marble sierra").split()


def new_passphrase() -> str:
    return "-".join(secrets.choice(WORDS) for _ in range(5)) + "-" + str(secrets.randbelow(90) + 10)


def _tenant(conn, code):
    row = conn.execute(text("SELECT id, code, name FROM tenants WHERE upper(code) = upper(:c)"), {"c": code}).mappings().first()
    if not row:
        sys.exit(f"No organisation with code {code}")
    return row


def _account(conn, tid, email):
    row = conn.execute(text("SELECT * FROM break_glass_accounts WHERE tenant_id = :t AND email = :e"),
                       {"t": tid, "e": email.strip().lower()}).mappings().first()
    if not row:
        sys.exit(f"No break-glass holder {email}")
    return row


def _end_open(conn, account_id, why):
    conn.execute(text("""UPDATE break_glass_sessions SET status = 'Ended', ended_at = now(), ended_reason = :w
                         WHERE account_id = :a AND status IN ('Pending', 'Active')"""), {"a": account_id, "w": why})


def _show(org, email, passphrase, secret):
    print("\n  Hand these over securely; they won't be shown again. The passphrase is kept only as a salted hash. The")
    print("  authenticator secret has to be kept so codes can be checked: it's readable only by the database owner and")
    print("  the API, and is useless without the passphrase.\n")
    print(f"  Organisation code:     {org}")
    print(f"  Email:                 {email}")
    print(f"  Passphrase:            {passphrase}")
    print(f"  Authenticator secret:  {secret}")
    print(f"  Authenticator link:    {otpauth_uri(secret, email, org)}\n")


def create_holder(conn, tenant_id, email: str, name: str, roles: list[str]) -> tuple[str, str]:
    """A break-glass person record and its credentials. Returns (passphrase, authenticator secret)."""
    uid = uuid.uuid4()
    conn.execute(text("""INSERT INTO users (id, tenant_id, idp_subject, display_name, department, roles, is_external,
                         notification_channels) VALUES (:i, :t, :s, :n, 'Break-glass', :r, false, :ch)"""),
                 {"i": uid, "t": tenant_id, "s": f"breakglass:{uid}", "n": f"{name} (break-glass)", "r": roles,
                  "ch": ["Email", "Portal"]})
    passphrase, secret = new_passphrase(), new_totp_secret()
    conn.execute(text("""INSERT INTO break_glass_accounts (tenant_id, user_id, email, passphrase_hash, totp_secret)
                         VALUES (:t, :u, :e, :h, :s)"""),
                 {"t": tenant_id, "u": uid, "e": email.strip().lower(), "h": hash_passphrase(passphrase), "s": secret})
    return passphrase, secret


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("code")
    add.add_argument("email")
    add.add_argument("name")
    add.add_argument("roles", nargs="*")
    for c in ("rotate", "disable"):
        p = sub.add_parser(c)
        p.add_argument("code")
        p.add_argument("email")
    sub.add_parser("list").add_argument("code")
    a = ap.parse_args(argv)

    with worker_engine().begin() as conn:
        tn = _tenant(conn, a.code)
        if a.cmd == "add":
            roles = a.roles or [Role.ADMIN.value]
            bad = [r for r in roles if r not in {x.value for x in Role}]
            if bad:
                sys.exit(f"Unknown role(s): {', '.join(bad)}")
            if Role.VENDOR.value in roles:
                sys.exit("Break-glass holders are the bank's own staff, not external users")
            email = a.email.strip().lower()
            if conn.execute(text("SELECT 1 FROM break_glass_accounts WHERE tenant_id = :t AND email = :e"),
                            {"t": tn["id"], "e": email}).first():
                sys.exit(f"{email} is already a break-glass holder; use rotate for new credentials")
            passphrase, secret = create_holder(conn, tn["id"], email, a.name, roles)
            print(f"Break-glass holder added for {tn['name']}: {a.name}, roles {', '.join(roles)}")
            _show(tn["code"], email, passphrase, secret)
        elif a.cmd == "rotate":
            acc = _account(conn, tn["id"], a.email)
            passphrase, secret = new_passphrase(), new_totp_secret()
            conn.execute(text("""UPDATE break_glass_accounts SET passphrase_hash = :h, totp_secret = :s, last_totp_step = NULL,
                                 failed_attempts = 0, locked_until = NULL, rotated_at = now(), disabled_at = NULL WHERE id = :i"""),
                         {"h": hash_passphrase(passphrase), "s": secret, "i": acc["id"]})
            conn.execute(text("UPDATE users SET is_active = true WHERE id = :u"), {"u": acc["user_id"]})
            _end_open(conn, acc["id"], "credentials rotated")
            print(f"New credentials for {acc['email']}; any open session was ended.")
            _show(tn["code"], acc["email"], passphrase, secret)
        elif a.cmd == "disable":
            acc = _account(conn, tn["id"], a.email)
            conn.execute(text("UPDATE break_glass_accounts SET disabled_at = now() WHERE id = :i"), {"i": acc["id"]})
            conn.execute(text("UPDATE users SET is_active = false WHERE id = :u"), {"u": acc["user_id"]})
            _end_open(conn, acc["id"], "holder disabled")
            print(f"{acc['email']} can no longer use break-glass access; any open session was ended.")
        else:
            rows = conn.execute(text("""
                SELECT a.email, u.display_name, u.roles, a.disabled_at, a.locked_until, a.rotated_at, a.created_at,
                       (SELECT max(requested_at) FROM break_glass_sessions s WHERE s.account_id = a.id) AS last_request,
                       (SELECT count(*) FROM break_glass_sessions s WHERE s.account_id = a.id AND s.status IN ('Pending','Active')) AS open
                FROM break_glass_accounts a JOIN users u ON u.id = a.user_id WHERE a.tenant_id = :t ORDER BY a.email"""),
                {"t": tn["id"]}).mappings().all()
            live = [r for r in rows if not r["disabled_at"]]
            print(f"{tn['name']}: {len(live)} active break-glass holder(s)"
                  + ("  (at least two are needed: a second holder must approve)" if len(live) < 2 else ""))
            for r in rows:
                state = "disabled" if r["disabled_at"] else "locked" if r["locked_until"] else "active"
                print(f"  {r['email']:<36} {r['display_name']:<34} {state:<9} last request {r['last_request'] or 'never'}"
                      f"{'  OPEN SESSION' if r['open'] else ''}")


if __name__ == "__main__":
    main()
