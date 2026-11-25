"""Seed a demo tenant (users for every role, teams, services, a Core Banking DR template).

Usage:  python -m scripts.seed  [--reset]
Runs as the schema owner (WORKER_DATABASE_URL) because tenant provisioning is an
admin operation outside normal application workflows.
"""
from __future__ import annotations

import argparse
import pathlib
import uuid

from sqlalchemy import text

from app.db import worker_engine

ROOT = pathlib.Path(__file__).resolve().parents[1]

USERS = [
    # subject, name, roles, department, external
    ("priya.admin", "Priya Nair", ["EnterpriseProgrammeAdministrator"], "Technology Risk", False),
    ("arjun.pm", "Arjun Mehta", ["DRProgrammeManager"], "Resilience Office", False),
    ("meera.tm", "Meera Iyer", ["TestManager"], "Resilience Office", False),
    ("vikram.owner", "Vikram Rao", ["TestManager"], "Core Banking", False),
    ("sana.lead", "Sana Qureshi", ["TeamLead", "TechnicalStakeholder"], "Database Engineering", False),
    ("rahul.dba", "Rahul Das", ["TechnicalStakeholder"], "Database Engineering", False),
    ("anita.infra", "Anita Kulkarni", ["TechnicalStakeholder"], "Infrastructure", False),
    ("kiran.net", "Kiran Shetty", ["TechnicalStakeholder"], "Network", False),
    ("deepa.app", "Deepa Menon", ["TechnicalStakeholder"], "Core Banking Apps", False),
    ("farah.biz", "Farah Khan", ["BusinessOwner"], "Retail Operations", False),
    ("rohan.risk", "Rohan Gupta", ["RiskCompliance"], "Operational Risk", False),
    ("leela.cio", "Leela Srinivasan", ["SeniorManagement"], "Office of the CIO", False),
    ("ext.vendor", "Tom Becker (Vendor)", ["ExternalVendor"], "StorageCo", True),
    ("audit.ia", "Neha Joshi", ["Auditor"], "Internal Audit", False),
]

CORE_BANKING_TEMPLATE = {
    "activities": [
        {"code": "INF-01", "title": "Confirm DR site infrastructure readiness", "category": "Infrastructure", "due_offset_hours": -72},
        {"code": "NET-01", "title": "Validate network routing to DR site", "category": "Network", "due_offset_hours": -48},
        {"code": "DB-01", "title": "Prepare database recovery point", "category": "Database", "due_offset_hours": -24, "evidence_required": True},
        {"code": "APP-01", "title": "Application recovery runbook walkthrough", "category": "Application", "due_offset_hours": -12},
        {"code": "TV-01", "title": "Technical validation of recovered services", "category": "Validation", "due_offset_hours": 4, "evidence_required": True},
        {"code": "BV-01", "title": "Business functional validation", "category": "Business", "due_offset_hours": 6},
    ],
    "dependencies": [["INF-01", "DB-01"], ["DB-01", "APP-01"], ["INF-01", "NET-01"], ["NET-01", "APP-01"],
                     ["APP-01", "TV-01"], ["TV-01", "BV-01"]],
    "readiness_items": [
        {"title": "Change request approved", "is_mandatory": True, "evidence_required": True},
        {"title": "Database recovery ready", "is_mandatory": True, "activity_code": "DB-01"},
        {"title": "Business validation team confirmed", "is_mandatory": True},
        {"title": "Customer communication drafted", "is_mandatory": False},
    ],
    "milestones": [{"title": "Failover initiated (by operations, outside platform)", "offset_minutes": 0},
                   {"title": "Database recovered", "offset_minutes": 90},
                   {"title": "Applications available", "offset_minutes": 180},
                   {"title": "Business validation complete", "offset_minutes": 300}],
}


def apply_migrations(conn) -> None:
    # Use the raw driver so '%' in PL/pgSQL format() strings is not treated as a bind placeholder.
    raw = conn.connection.driver_connection
    for f in sorted((ROOT / "migrations").glob("*.sql")):
        raw.execute(f.read_text())


def reset(conn) -> None:
    conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO public;")
    apply_migrations(conn)


def seed_tenant(conn, code: str, name: str) -> dict:
    tid = uuid.uuid4()
    conn.execute(text("INSERT INTO tenants (id, code, name) VALUES (:i, :c, :n)"), {"i": tid, "c": code, "n": name})
    ids: dict[str, uuid.UUID] = {"tenant": tid}
    for sub, nm, roles, dept, ext in USERS:
        uid = uuid.uuid4()
        conn.execute(text("""INSERT INTO users (id, tenant_id, idp_subject, display_name, email, department, roles, is_external,
                             notification_channels) VALUES (:i,:t,:s,:n,:e,:d,:r,:x,:ch)"""),
                     {"i": uid, "t": tid, "s": sub, "n": nm, "e": f"{sub}@{code.lower()}.example", "d": dept,
                      "r": roles, "x": ext, "ch": ["Email", "Teams", "Portal"]})
        ids[sub] = uid
    team = uuid.uuid4()
    conn.execute(text("INSERT INTO teams (id, tenant_id, name, lead_user_id) VALUES (:i,:t,'Database Engineering',:l)"),
                 {"i": team, "t": tid, "l": ids["sana.lead"]})
    for m in ("sana.lead", "rahul.dba"):
        conn.execute(text("INSERT INTO team_members (tenant_id, team_id, user_id) VALUES (:t,:tm,:u)"),
                     {"t": tid, "tm": team, "u": ids[m]})
    ids["team_db"] = team
    svc = uuid.uuid4()
    conn.execute(text("INSERT INTO business_services (id, tenant_id, code, name, criticality) VALUES (:i,:t,'CBS','Core Banking','Critical')"),
                 {"i": svc, "t": tid})
    ids["svc_cbs"] = svc
    return ids


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    with worker_engine().begin() as conn:
        if a.reset:
            reset(conn)
        ids = seed_tenant(conn, "BANK", "Demo Bank")
    print("Seeded tenant BANK", ids["tenant"])
    print("Dev auth headers: X-Tenant-Id:", ids["tenant"], " X-User-Subject: arjun.pm (or any subject above)")


if __name__ == "__main__":
    main()
