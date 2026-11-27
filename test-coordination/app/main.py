"""ResilienceAI — Test Programme & Coordination Centre API."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from .api.routes import router
from .config import check_production
from .errors import DomainError

log = logging.getLogger("resilienceai")

_unsafe = check_production()
if _unsafe:
    raise RuntimeError("Refusing to start in production: " + "; ".join(_unsafe))

app = FastAPI(
    title="ResilienceAI — Test Coordination API",
    version="0.1.0",
    description=("Coordination-first MVP. Records and governs resilience tests; it never executes "
                 "failover or infrastructure commands."),
)
app.include_router(router, prefix="/api/v1")


@app.exception_handler(DomainError)
async def domain_error(_: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message, "details": exc.details})


@app.exception_handler(IntegrityError)
async def integrity_error(_: Request, exc: IntegrityError):
    log.warning("integrity error: %s", exc.orig)
    return JSONResponse(status_code=409, content={"error": "Request conflicts with existing data or a data rule",
                                                  "details": [str(exc.orig).splitlines()[0]]})


@app.get("/api/v1/dev/directory", tags=["dev"], summary="Dev only: tenants and users for the sign-in switcher")
def dev_directory():
    """Exists only with AUTH_MODE=dev. Lets the web app offer 'act as' without an IdP."""
    from sqlalchemy import text

    from . import config
    from .db import worker_engine
    if config.settings.auth_mode != "dev":
        return JSONResponse(status_code=404, content={"error": "Not found", "details": []})
    with worker_engine().connect() as c:
        tenants = [dict(r) for r in c.execute(text("SELECT id, code, name FROM tenants ORDER BY code")).mappings()]
        users = [dict(r) for r in c.execute(text(
            "SELECT tenant_id, idp_subject, display_name, roles, department FROM users WHERE is_active ORDER BY display_name")).mappings()]
    return {"tenants": tenants, "users": users}


@app.get("/api/v1/auth/config", tags=["auth"], summary="How the web app should sign people in (public)")
def auth_config():
    from . import config
    s = config.settings
    if s.auth_mode != "oidc":
        return {"mode": "dev", "break_glass": s.break_glass_on}
    return {"mode": "oidc", "provider": "entra", "client_id": s.entra_client_id,
            "authority": f"{s.entra_authority_host.rstrip('/')}/{s.entra_tenant_id or 'organizations'}",
            "scopes": [s.entra_api_scope], "break_glass": s.break_glass_on}


# ---- Break-glass access (no sign-in: these are how a holder gets in when Entra is down) ----
class _BgRequest(BaseModel):
    org: str = Field(max_length=60)
    email: str = Field(max_length=320)
    passphrase: str = Field(max_length=500)
    code: str = Field(max_length=10)
    reason: str = Field(max_length=2000)


class _BgApprove(BaseModel):
    org: str = Field(max_length=60)
    email: str = Field(max_length=320)
    passphrase: str = Field(max_length=500)
    code: str = Field(max_length=10)
    request_code: str = Field(max_length=12)


def _client(request: Request) -> str | None:
    return request.client.host if request.client else None


def _bg_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


@app.post("/api/v1/break-glass/request", tags=["auth"], summary="Break-glass: ask for a session (step 1)")
def break_glass_request(data: _BgRequest, request: Request):
    from .services import breakglass
    return breakglass.request(data.org, data.email, data.passphrase, data.code, data.reason, _client(request))


@app.post("/api/v1/break-glass/approve", tags=["auth"], summary="Break-glass: a second holder approves (step 2)")
def break_glass_approve(data: _BgApprove, request: Request):
    from .services import breakglass
    return breakglass.approve(data.org, data.email, data.passphrase, data.code, data.request_code, _client(request))


@app.get("/api/v1/break-glass/session", tags=["auth"], summary="Break-glass: the state of my session")
def break_glass_status(request: Request):
    from .services import breakglass
    return breakglass.status(_bg_token(request))


@app.post("/api/v1/break-glass/end", tags=["auth"], summary="Break-glass: end my session now")
def break_glass_end(request: Request):
    from .services import breakglass
    return breakglass.end(_bg_token(request))


@app.get("/health", tags=["ops"], summary="Liveness: the process is up")
def health():
    return {"status": "ok"}


@app.get("/ready", tags=["ops"], summary="Readiness: the database is migrated and the evidence store is set up")
def ready():
    from sqlalchemy import text

    from .db import engine
    try:
        with engine().connect() as c:
            c.execute(text("SELECT 1 FROM test_events LIMIT 1"))
    except Exception as e:  # noqa: BLE001 - report, don't crash the probe
        log.warning("not ready: %s", e)
        return JSONResponse(status_code=503, content={"status": "not ready", "reason": type(e).__name__})
    from .services import storage
    try:
        st = storage.store()
        problems = st.check() if hasattr(st, "check") else []
    except Exception as e:  # noqa: BLE001
        problems = [f"Evidence store: {type(e).__name__}"]
    if problems:
        log.warning("not ready: %s", problems)
        return JSONResponse(status_code=503, content={"status": "not ready", "reason": "; ".join(problems)})
    return {"status": "ready"}
