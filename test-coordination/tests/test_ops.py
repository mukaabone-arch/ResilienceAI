"""Deployment safety: migrations, production guard, health and readiness."""
from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import text

from app.config import Settings, check_production
from app.db import worker_engine
from scripts import migrate


def test_migrations_recorded_and_idempotent(seeded):
    with worker_engine().connect() as c:
        versions = [r[0] for r in c.execute(text("SELECT version FROM schema_migrations ORDER BY version"))]
        assert versions == [f.stem for f in migrate.files()]
        assert migrate.apply(c, out=lambda *_: None) == []          # nothing pending on a second run


def test_edited_migration_is_detected(seeded, tmp_path, monkeypatch):
    first = migrate.files()[0]
    fake = tmp_path / first.name
    fake.write_text(first.read_text() + "\n-- edited after release\n")
    monkeypatch.setattr(migrate, "files", lambda: [fake])
    with worker_engine().connect() as c, pytest.raises(SystemExit, match="edited after it was applied"):
        migrate.apply(c, out=lambda *_: None)


def test_untracked_database_must_be_baselined(seeded):
    with worker_engine().connect() as c:
        raw = c.connection.driver_connection
        raw.execute("ALTER TABLE schema_migrations RENAME TO schema_migrations_keep")
        raw.commit()
        try:
            with pytest.raises(SystemExit, match="--baseline"):
                migrate.apply(c, out=lambda *_: None)
        finally:
            raw.rollback()
            raw.execute("DROP TABLE IF EXISTS schema_migrations")
            raw.execute("ALTER TABLE schema_migrations_keep RENAME TO schema_migrations")
            raw.commit()


def test_production_guard():
    dev = Settings()
    assert check_production(dataclasses.replace(dev, app_env="development")) == []
    problems = check_production(dataclasses.replace(dev, app_env="production"))
    assert any("AUTH_MODE=dev" in p for p in problems) and any("EVIDENCE_STORE" in p for p in problems)
    ok = dataclasses.replace(dev, app_env="production", auth_mode="oidc", evidence_store="s3",
                             entra_client_id="0b6f8c3e-1d2a-4e5f-8a9b-c0d1e2f3a4b5", entra_tenant_id="7d1f3c2a-5b6e-4f80-9a1b-2c3d4e5f6a7b",
                             database_url="postgresql+psycopg://app_user:Str0ng@db/resilience")
    assert check_production(ok) == []


def test_health_and_readiness(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
