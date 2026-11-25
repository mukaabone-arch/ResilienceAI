"""Integration fixtures: a fresh schema in a real PostgreSQL database per test session.

The API connects as app_user (not the table owner) so RLS is genuinely enforced.
Set TEST_DATABASE_ADMIN_URL / DATABASE_URL to point at another server.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import tables, worker_engine
from scripts.seed import seed_tenant, reset


@pytest.fixture(scope="session")
def seeded():
    with worker_engine().begin() as conn:
        reset(conn)
        conn.exec_driver_sql("GRANT ALL ON SCHEMA public TO app_user")
        a = seed_tenant(conn, "BANK", "Demo Bank")
        b = seed_tenant(conn, "OTHR", "Other Bank")
    tables.cache_clear()
    return {"A": a, "B": b}


@pytest.fixture(scope="session")
def client(seeded):
    from app.main import app
    return TestClient(app)


class As:
    """Call the API as a named seeded user."""
    def __init__(self, client, ids, subject):
        self.c, self.h = client, {"X-Tenant-Id": str(ids["tenant"]), "X-User-Subject": subject}

    def __getattr__(self, method):
        def call(path, expect=None, **kw):
            r = getattr(self.c, method)(f"/api/v1{path}", headers={**self.h, **kw.pop("headers", {})}, **kw)
            if expect is not None:
                assert r.status_code == expect, f"{method.upper()} {path} -> {r.status_code}: {r.text}"
            return r
        return call


@pytest.fixture(scope="session")
def as_(client, seeded):
    def make(subject, tenant="A"):
        return As(client, seeded[tenant], subject)
    return make
