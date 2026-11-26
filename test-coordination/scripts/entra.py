"""Microsoft Entra ID setup for an organisation (run with the owner database login).

    python -m scripts.entra link BANK 11111111-2222-3333-4444-555555555555
        Link organisation BANK to its Entra directory (the directory/tenant id from Entra admin centre).

    python -m scripts.entra add-user BANK priya.nair@bank.example "Priya Nair" EnterpriseProgrammeAdministrator
        Provision a person by email. Their first Microsoft sign-in links their Entra account.
        Roles are optional when Entra app roles are used (ENTRA_ROLES_FROM_TOKEN=true).

    python -m scripts.entra status BANK
        Show the linked directory and who has signed in.
"""
from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import text

from app.db import worker_engine
from app.domain.enums import Role


def _tenant(conn, code: str):
    row = conn.execute(text("SELECT id, name, entra_tenant_id FROM tenants WHERE code = :c"), {"c": code}).mappings().first()
    if not row:
        sys.exit(f"No organisation with code {code}")
    return row


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    link = sub.add_parser("link", help="link an organisation to its Entra directory")
    link.add_argument("code")
    link.add_argument("entra_tenant_id", type=uuid.UUID)
    add = sub.add_parser("add-user", help="provision a person by email")
    add.add_argument("code")
    add.add_argument("email")
    add.add_argument("name")
    add.add_argument("roles", nargs="*")
    add.add_argument("--department")
    add.add_argument("--external", action="store_true")
    status = sub.add_parser("status", help="show the linked directory and sign-in state")
    status.add_argument("code")
    args = ap.parse_args(argv)

    with worker_engine().begin() as conn:
        tn = _tenant(conn, args.code)
        if args.cmd == "link":
            conn.execute(text("UPDATE tenants SET entra_tenant_id = :e WHERE id = :t"), {"e": args.entra_tenant_id, "t": tn["id"]})
            print(f"{tn['name']} now signs in with Entra directory {args.entra_tenant_id}")
        elif args.cmd == "add-user":
            valid = {r.value for r in Role}
            bad = [r for r in args.roles if r not in valid]
            if bad:
                sys.exit(f"Unknown role(s): {', '.join(bad)}. Valid: {', '.join(sorted(valid))}")
            email = args.email.strip().lower()
            # Until the first sign-in, the email stands in as the subject; sign-in replaces it with the Entra object id.
            conn.execute(text("""INSERT INTO users (tenant_id, idp_subject, display_name, email, department, roles, is_external)
                                 VALUES (:t, :s, :n, :e, :d, :r, :x)"""),
                         {"t": tn["id"], "s": f"pending:{email}", "n": args.name, "e": email,
                          "d": args.department, "r": args.roles, "x": args.external})
            print(f"Added {args.name} <{email}> to {tn['name']}; they can now sign in with Microsoft")
        else:
            print(f"{tn['name']}: Entra directory {tn['entra_tenant_id'] or 'not linked'}")
            for r in conn.execute(text("""SELECT display_name, email, roles, idp_bound_at FROM users
                                          WHERE tenant_id = :t AND is_active ORDER BY display_name"""), {"t": tn["id"]}).mappings():
                state = f"signed in, linked {r['idp_bound_at']:%d %b %Y}" if r["idp_bound_at"] else "not yet signed in"
                print(f"  {r['display_name']:<28} {r['email'] or '':<34} {', '.join(r['roles']):<40} {state}")


if __name__ == "__main__":
    main()
