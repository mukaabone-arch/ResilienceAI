"""ResilienceAI — Test Programme & Coordination Centre API."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from .api.routes import router
from .errors import DomainError

log = logging.getLogger("resilienceai")

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

    from .config import settings
    from .db import worker_engine
    if settings.auth_mode != "dev":
        return JSONResponse(status_code=404, content={"error": "Not found", "details": []})
    with worker_engine().connect() as c:
        tenants = [dict(r) for r in c.execute(text("SELECT id, code, name FROM tenants ORDER BY code")).mappings()]
        users = [dict(r) for r in c.execute(text(
            "SELECT tenant_id, idp_subject, display_name, roles, department FROM users WHERE is_active ORDER BY display_name")).mappings()]
    return {"tenants": tenants, "users": users}


@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok"}
