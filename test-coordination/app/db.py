"""Database access.

The SQL migration is the single source of truth for the schema; tables are
reflected at startup and queried with SQLAlchemy Core. Every request runs in
one transaction with `app.tenant_id` set via SET LOCAL semantics, so
PostgreSQL row-level security isolates tenants even if a query forgets a
tenant filter.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator
from uuid import UUID

from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.engine import Connection, Engine

from .config import settings


@lru_cache
def engine() -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True, future=True)


@lru_cache
def worker_engine() -> Engine:
    return create_engine(settings.worker_database_url, future=True)


_metadata = MetaData()


@lru_cache
def tables() -> dict[str, Table]:
    # Reflect with the privileged connection: RLS does not affect catalog reads,
    # but app_user may lack visibility of some catalog details on locked-down installs.
    _metadata.reflect(bind=worker_engine())
    return dict(_metadata.tables)


def t(name: str) -> Table:
    return tables()[name]


@contextmanager
def tenant_transaction(tenant_id: UUID) -> Iterator[Connection]:
    with engine().begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        yield conn
