"""Authentication and the request context.

AUTH_MODE=dev trusts X-Tenant-Id / X-User-Subject headers -- for local
development and tests ONLY. Production must use AUTH_MODE=oidc with a token
verifier wired to the enterprise IdP (Entra ID by default, spec §30); the
verifier maps the token's tenant claim and `sub` to a users row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator
from uuid import UUID, uuid4

from fastapi import Header, Request
from sqlalchemy import select
from sqlalchemy.engine import Connection

from .config import settings
from .db import t, tenant_transaction
from .domain.enums import Role
from .errors import DomainError, Forbidden


@dataclass
class Principal:
    tenant_id: UUID
    user_id: UUID
    display_name: str
    roles: frozenset[str]
    is_external: bool

    def has(self, *roles: Role) -> bool:
        return any(r.value in self.roles for r in roles)


@dataclass
class Ctx:
    conn: Connection
    me: Principal
    correlation_id: UUID


class Unauthenticated(DomainError):
    status_code = 401


def verify_oidc_token(token: str) -> tuple[UUID, str]:  # pragma: no cover - integration point
    """Return (tenant_id, subject). Implement with the IdP's JWKS (RA-005)."""
    raise Unauthenticated("OIDC verification not configured for this deployment")


def _resolve(request: Request, x_tenant_id: str | None, x_user_subject: str | None) -> tuple[UUID, str]:
    if settings.auth_mode == "dev":
        if not (x_tenant_id and x_user_subject):
            raise Unauthenticated("Missing X-Tenant-Id / X-User-Subject (dev auth)")
        try:
            return UUID(x_tenant_id), x_user_subject
        except ValueError as e:
            raise Unauthenticated("Invalid tenant id") from e
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise Unauthenticated("Bearer token required")
    return verify_oidc_token(auth[7:])


def get_ctx(
    request: Request,
    x_tenant_id: str | None = Header(default=None),
    x_user_subject: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
) -> Iterator[Ctx]:
    tenant_id, subject = _resolve(request, x_tenant_id, x_user_subject)
    with tenant_transaction(tenant_id) as conn:
        u = t("users")
        row = conn.execute(select(u).where(u.c.idp_subject == subject)).mappings().first()
        if row is None or not row["is_active"]:
            raise Forbidden("User is not an active member of this tenant")
        try:
            corr = UUID(x_correlation_id) if x_correlation_id else uuid4()
        except ValueError:
            corr = uuid4()
        yield Ctx(conn=conn, correlation_id=corr, me=Principal(
            tenant_id=tenant_id, user_id=row["id"], display_name=row["display_name"],
            roles=frozenset(row["roles"] or []), is_external=row["is_external"]))
