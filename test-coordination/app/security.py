"""Authentication and the request context.

AUTH_MODE=dev trusts X-Tenant-Id / X-User-Subject headers -- for local
development and tests ONLY.

AUTH_MODE=oidc accepts Microsoft Entra ID v2 access tokens issued for this API:
signature checked against Entra's published keys, then issuer, audience, expiry
and the API scope. The token's directory (`tid`) selects the organisation and its
object id (`oid`) the person. A person provisioned by email is linked to their
Entra account at first sign-in. Entra app roles, when present, set the person's roles.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator
from uuid import UUID, uuid4

import httpx
import jwt
from fastapi import Header, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Connection

from . import config
from .db import engine, t, tenant_transaction
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


log = logging.getLogger("resilienceai.auth")


@dataclass
class Identity:
    """Who a request is from, as established by authentication (before the users table is consulted)."""
    tenant_id: UUID
    subject: str
    claims: dict = field(default_factory=dict)   # verified token claims (oidc only)


# ---- Entra signing keys: fetched from the JWKS endpoint, cached, refreshed when a new key id appears ----
_JWKS_TTL_S = 6 * 3600
_JWKS_MIN_REFRESH_S = 60          # an unknown key id can't make us hammer the endpoint
_jwks_lock = threading.Lock()
_jwks: dict = {"keys": {}, "fetched_at": 0.0}


def _fetch_jwks(url: str) -> dict:
    r = httpx.get(url, timeout=5.0)
    r.raise_for_status()
    return r.json()


def _signing_key(kid: str):
    with _jwks_lock:
        age = time.time() - _jwks["fetched_at"]
        if age > _JWKS_TTL_S or (kid not in _jwks["keys"] and age > _JWKS_MIN_REFRESH_S):
            try:
                data = _fetch_jwks(config.settings.entra_jwks_url)
                _jwks["keys"] = {k["kid"]: k for k in data.get("keys", []) if k.get("kid")}
                _jwks["fetched_at"] = time.time()
            except Exception as e:  # noqa: BLE001 - keep serving with the cached keys
                log.warning("could not refresh Entra signing keys: %s", e)
        jwk = _jwks["keys"].get(kid)
    if jwk is None:
        raise Unauthenticated("Token signed with an unknown key")
    return jwt.PyJWK(jwk).key


def verify_entra_token(token: str) -> dict:
    """Return the verified claims of an Entra ID v2 access token for this API, or raise Unauthenticated."""
    s = config.settings
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise Unauthenticated("Unsupported token")
        unverified = jwt.decode(token, options={"verify_signature": False})
        tid = str(UUID(str(unverified.get("tid", ""))))
        claims = jwt.decode(
            token, _signing_key(header["kid"]), algorithms=["RS256"],
            audience=s.entra_audiences, issuer=s.entra_issuer_template.format(tid=tid), leeway=60,
            options={"require": ["exp", "iat", "iss", "aud", "tid", "oid"]})
    except Unauthenticated:
        raise
    except jwt.ExpiredSignatureError as e:
        raise Unauthenticated("Your sign-in has expired") from e
    except (jwt.PyJWTError, ValueError) as e:
        raise Unauthenticated("Invalid token") from e
    if s.entra_tenant_id not in ("", "organizations", "common") and tid != s.entra_tenant_id:
        raise Unauthenticated("Token is from a directory this deployment doesn't accept")
    if s.entra_api_scope_name not in str(claims.get("scp", "")).split():
        raise Unauthenticated("Token was not issued for this API")
    return claims


def _resolve(request: Request, x_tenant_id: str | None, x_user_subject: str | None) -> Identity:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer bg."):                    # a break-glass session (Entra unavailable)
        from .services import breakglass
        try:
            tenant_id, subject, _ = breakglass.resolve(auth[7:].strip())
        except breakglass.BreakGlassError as e:
            raise Unauthenticated(e.message) from e
        return Identity(tenant_id, subject)
    if config.settings.auth_mode == "dev":
        if not (x_tenant_id and x_user_subject):
            raise Unauthenticated("Missing X-Tenant-Id / X-User-Subject (dev auth)")
        try:
            return Identity(UUID(x_tenant_id), x_user_subject)
        except ValueError as e:
            raise Unauthenticated("Invalid tenant id") from e
    if not auth.lower().startswith("bearer "):
        raise Unauthenticated("Bearer token required")
    claims = verify_entra_token(auth[7:].strip())
    with engine().connect() as c:
        tenant_id = c.execute(text("SELECT tenant_for_entra(:t)"), {"t": claims["tid"]}).scalar()
    if tenant_id is None:
        raise Forbidden("Your organisation isn't set up in ResilienceAI yet. Ask your administrator.")
    return Identity(tenant_id, str(claims["oid"]), claims)


def _user_for(conn: Connection, ident: Identity) -> dict | None:
    """Find the person; in oidc mode, link a person provisioned by email and apply Entra app roles."""
    u = t("users")
    row = conn.execute(select(u).where(u.c.idp_subject == ident.subject)).mappings().first()
    if not ident.claims:
        return dict(row) if row else None
    s = config.settings
    if row is None and s.entra_bind_by_email:
        emails = {str(ident.claims.get(k, "")).strip().lower() for k in ("preferred_username", "email", "upn")} - {""}
        if emails:
            cand = conn.execute(select(u).where(func.lower(u.c.email).in_(emails), u.c.idp_bound_at.is_(None))).mappings().first()
            if cand:
                conn.execute(update(u).where(u.c.id == cand["id"]).values(idp_subject=ident.subject, idp_bound_at=func.now()))
                log.info("linked user %s to Entra object %s", cand["id"], ident.subject)
                row = conn.execute(select(u).where(u.c.id == cand["id"])).mappings().first()
    if row is None:
        return None
    if s.entra_roles_from_token and isinstance(ident.claims.get("roles"), list):
        valid = {r.value for r in Role}
        roles = sorted(set(map(str, ident.claims["roles"])) & valid)
        if roles != sorted(row["roles"] or []):
            conn.execute(update(u).where(u.c.id == row["id"]).values(roles=roles))
            row = {**row, "roles": roles}
    return dict(row)


def get_ctx(
    request: Request,
    x_tenant_id: str | None = Header(default=None),
    x_user_subject: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
) -> Iterator[Ctx]:
    ident = _resolve(request, x_tenant_id, x_user_subject)
    tenant_id = ident.tenant_id
    with tenant_transaction(tenant_id) as conn:
        row = _user_for(conn, ident)
        if row is None or not row["is_active"]:
            raise Forbidden("Your account isn't set up in ResilienceAI. Ask your administrator to add you."
                            if ident.claims else "User is not an active member of this tenant")
        try:
            corr = UUID(x_correlation_id) if x_correlation_id else uuid4()
        except ValueError:
            corr = uuid4()
        yield Ctx(conn=conn, correlation_id=corr, me=Principal(
            tenant_id=tenant_id, user_id=row["id"], display_name=row["display_name"],
            roles=frozenset(row["roles"] or []), is_external=row["is_external"]))
