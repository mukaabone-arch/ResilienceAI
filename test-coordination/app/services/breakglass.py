"""Break-glass access: signing in when Microsoft Entra ID is unavailable.

A small number of named holders per organisation (created by the operations script, never through the app) each
have a passphrase and an authenticator-app secret. To get in during an outage:

1. A holder requests a session with their passphrase, a current authenticator code and a reason. They get a
   request code (e.g. 7K3Q-M9PD) and a token that doesn't work yet.
2. A *different* holder approves that request code with their own passphrase and code.
3. The session is active for BREAK_GLASS_MINUTES, then expires. It can be ended earlier.

The holder acts as their own break-glass person record (shown as "Name (break-glass)"), so everything they do is
attributed and audited like any other action. Every request, approval, refusal, failed attempt and ending is written
to the audit trail and logged at WARNING for the bank's SIEM. Five wrong attempts lock an account for 30 minutes,
and an authenticator code can't be used twice. Errors never say which detail was wrong.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, insert, select, text, update
from sqlalchemy.engine import Connection

from .. import config
from ..db import engine, t, tenant_transaction
from ..errors import DomainError
from . import audit
from .common import now

log = logging.getLogger("resilienceai.breakglass")

MAX_FAILURES = 5
LOCK_MINUTES = 30
PENDING_MINUTES = 15
TOKEN_PREFIX = "bg."
GENERIC = "Those details didn't work. Check the organisation, email, passphrase and authenticator code."


class BreakGlassError(DomainError):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message, status_code=status)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_passphrase(passphrase: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(passphrase.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_passphrase(passphrase: str, stored: str) -> bool:
    try:
        scheme, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(passphrase.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret: str, step: int) -> str:
    """RFC 6238: 6 digits, 30-second steps, HMAC-SHA1 (what every authenticator app uses)."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(int.from_bytes(digest[o:o + 4], 'big') & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_step(code: str, secret: str, last_step: int | None, at: float | None = None) -> int | None:
    """The time step a code belongs to (one step of clock drift either way), or None. A step can't be reused."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return None
    current = int((at or time.time()) // 30)
    for step in (current, current - 1, current + 1):
        if hmac.compare_digest(totp(secret, step), code) and (last_step is None or step > last_step):
            return step
    return None


def otpauth_uri(secret: str, email: str, org: str) -> str:
    from urllib.parse import quote
    return (f"otpauth://totp/{quote('ResilienceAI break-glass')}:{quote(email)}?secret={secret}"
            f"&issuer={quote('ResilienceAI ' + org)}&digits=6&period=30")


def new_token() -> tuple[str, str]:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def new_request_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"          # no 0/O, 1/I: it is read out over the phone
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


# ---------------------------------------------------------------------------
# Inside one organisation
# ---------------------------------------------------------------------------
@dataclass
class Holder:
    account: dict
    user: dict


def _audit_ctx(conn: Connection, tenant_id: UUID, user: dict | None):
    from ..security import Ctx, Principal
    return Ctx(conn=conn, correlation_id=uuid4(), me=Principal(
        tenant_id=tenant_id, user_id=user["id"] if user else None,  # type: ignore[arg-type]
        display_name=user["display_name"] if user else "break-glass", roles=frozenset(user["roles"] or []) if user else frozenset(),
        is_external=False))


def _record(conn, tenant_id, user, action, entity_id, new):
    audit.record(_audit_ctx(conn, tenant_id, user), action, "BreakGlass", entity_id, new=new,
                 actor=user["id"] if user else None)
    log.warning("break-glass %s: %s", action, new)


def _verify(conn: Connection, tenant_id: UUID, email: str, passphrase: str, code: str, source: str | None) -> tuple[Holder | None, str | None]:
    """Check a holder's credentials. Failures are counted and audited; the caller commits either way."""
    a, u = t("break_glass_accounts"), t("users")
    acc = conn.execute(select(a).where(a.c.email == (email or "").strip().lower()).with_for_update()).mappings().first()
    if acc is None:
        _record(conn, tenant_id, None, "break_glass.failed", None, {"email": (email or "")[:120], "why": "unknown holder", "from": source})
        return None, GENERIC
    acc = dict(acc)
    user = dict(conn.execute(select(u).where(u.c.id == acc["user_id"])).mappings().one())
    if acc["disabled_at"] or not user["is_active"]:
        _record(conn, tenant_id, user, "break_glass.failed", acc["id"], {"why": "account disabled", "from": source})
        return None, GENERIC
    if acc["locked_until"] and acc["locked_until"] > now():
        _record(conn, tenant_id, user, "break_glass.failed", acc["id"], {"why": "locked", "from": source})
        return None, "Too many wrong attempts. This break-glass account is locked for a while; use another holder or wait."
    # Spaces around a pasted passphrase are a copying slip, not part of it (generated passphrases never contain spaces).
    pass_ok = check_passphrase((passphrase or "").strip(), acc["passphrase_hash"])
    step = totp_step(code, acc["totp_secret"], acc["last_totp_step"]) if pass_ok else None
    if step is None:
        fails = acc["failed_attempts"] + 1
        locked = now() + timedelta(minutes=LOCK_MINUTES) if fails >= MAX_FAILURES else None
        conn.execute(update(a).where(a.c.id == acc["id"]).values(failed_attempts=0 if locked else fails, locked_until=locked))
        _record(conn, tenant_id, user, "break_glass.failed", acc["id"],
                {"why": "wrong passphrase or code", "attempt": fails, "locked": bool(locked), "from": source})
        return None, GENERIC
    conn.execute(update(a).where(a.c.id == acc["id"]).values(failed_attempts=0, locked_until=None, last_totp_step=step))
    return Holder(acc, user), None


def _expire(conn: Connection) -> None:
    s = t("break_glass_sessions")
    conn.execute(update(s).where(and_(s.c.status == "Pending", s.c.requested_at < now() - timedelta(minutes=PENDING_MINUTES)))
                 .values(status="Expired", ended_at=now(), ended_reason="not approved in time"))
    conn.execute(update(s).where(and_(s.c.status == "Active", s.c.expires_at < now()))
                 .values(status="Expired", ended_at=now(), ended_reason="time limit reached"))


def _tenant(org: str) -> UUID:
    with engine().connect() as c:
        tid = c.execute(text("SELECT tenant_for_code(:c)"), {"c": (org or "").strip()}).scalar()
    if tid is None:
        raise BreakGlassError(GENERIC)
    return tid


def _enabled() -> None:
    if not config.settings.break_glass_on:
        raise BreakGlassError("Break-glass access isn't turned on for this installation.", 404)


def public(sess: dict, holder_name: str | None = None) -> dict:
    return {"id": sess["id"], "request_code": sess["request_code"], "status": sess["status"],
            "requested_at": sess["requested_at"], "approved_at": sess["approved_at"], "expires_at": sess["expires_at"],
            "ended_at": sess["ended_at"], "ended_reason": sess["ended_reason"], "holder": holder_name,
            "minutes": config.settings.break_glass_minutes}


def request(org: str, email: str, passphrase: str, code: str, reason: str, source: str | None) -> dict:
    """Step 1: a holder asks for a session. Returns the request code and a token that works once approved."""
    _enabled()
    if len((reason or "").strip()) < 10:
        raise BreakGlassError("Say why break-glass access is needed (for example the Entra incident number).", 422)
    tid = _tenant(org)
    with tenant_transaction(tid) as conn:
        _expire(conn)
        holder, problem = _verify(conn, tid, email, passphrase, code, source)
        if holder is None:
            out = None
        else:
            s = t("break_glass_sessions")
            conn.execute(update(s).where(and_(s.c.account_id == holder.account["id"], s.c.status.in_(["Pending", "Active"])))
                         .values(status="Ended", ended_at=now(), ended_reason="replaced by a new request"))
            token, token_hash = new_token()
            sess = dict(conn.execute(insert(s).values(
                tenant_id=tid, account_id=holder.account["id"], request_code=new_request_code(), token_hash=token_hash,
                reason=reason.strip()[:2000], requested_from=(source or "")[:80]).returning(s)).mappings().one())
            _record(conn, tid, holder.user, "break_glass.requested", sess["id"],
                    {"request_code": sess["request_code"], "reason": sess["reason"], "from": source})
            out = {**public(sess, holder.user["display_name"]), "token": token}
    if out is None:
        raise BreakGlassError(problem or GENERIC)
    return out


def approve(org: str, email: str, passphrase: str, code: str, request_code: str, source: str | None) -> dict:
    """Step 2: a different holder approves the request code with their own credentials."""
    _enabled()
    tid = _tenant(org)
    problem = None
    with tenant_transaction(tid) as conn:
        _expire(conn)
        holder, problem = _verify(conn, tid, email, passphrase, code, source)
        out = None
        if holder is not None:
            s, a, u = t("break_glass_sessions"), t("break_glass_accounts"), t("users")
            sess = conn.execute(select(s).where(and_(s.c.request_code == (request_code or "").strip().upper(),
                                                     s.c.status == "Pending")).with_for_update()).mappings().first()
            if sess is None:
                problem = "No waiting request has that code. Requests expire after 15 minutes; ask for a new one."
            elif sess["account_id"] == holder.account["id"]:
                _record(conn, tid, holder.user, "break_glass.self_approval_refused", sess["id"], {"from": source})
                problem = "You can't approve your own request. A second break-glass holder has to approve it."
            else:
                expires = now() + timedelta(minutes=config.settings.break_glass_minutes)
                sess = dict(conn.execute(update(s).where(s.c.id == sess["id"]).values(
                    status="Active", approved_by=holder.account["id"], approved_at=now(), expires_at=expires).returning(s)).mappings().one())
                requester = conn.execute(select(u.c.display_name).join(a, a.c.user_id == u.c.id)
                                         .where(a.c.id == sess["account_id"])).scalar()
                _record(conn, tid, holder.user, "break_glass.approved", sess["id"],
                        {"request_code": sess["request_code"], "for": requester, "until": expires.isoformat(), "from": source})
                out = public(sess, requester)
    if out is None:
        raise BreakGlassError(problem or GENERIC, 409 if problem and "request" in problem else 403)
    return out


def _session_by_token(conn: Connection, token: str) -> dict | None:
    s = t("break_glass_sessions")
    r = conn.execute(select(s).where(s.c.token_hash == hashlib.sha256(token.encode()).hexdigest())).mappings().first()
    return dict(r) if r else None


def status(token: str) -> dict:
    """For the requester's screen while they wait; works before the session is active."""
    _enabled()
    tid = _tenant_for_token(token)
    with tenant_transaction(tid) as conn:
        _expire(conn)
        sess = _session_by_token(conn, token)
        if sess is None:
            raise BreakGlassError("Unknown session", 401)
        return public(sess)


def end(token: str, why: str = "ended by the holder") -> dict:
    _enabled()
    tid = _tenant_for_token(token)
    with tenant_transaction(tid) as conn:
        sess = _session_by_token(conn, token)
        if sess is None:
            raise BreakGlassError("Unknown session", 401)
        if sess["status"] in ("Pending", "Active"):
            s, a, u = t("break_glass_sessions"), t("break_glass_accounts"), t("users")
            conn.execute(update(s).where(s.c.id == sess["id"]).values(status="Ended", ended_at=now(), ended_reason=why))
            user = dict(conn.execute(select(u).join(a, a.c.user_id == u.c.id).where(a.c.id == sess["account_id"])).mappings().one())
            _record(conn, tid, user, "break_glass.ended", sess["id"], {"request_code": sess["request_code"], "why": why})
            sess = {**sess, "status": "Ended", "ended_at": now(), "ended_reason": why}
        return public(sess)


def _tenant_for_token(token: str) -> UUID:
    if not token.startswith(TOKEN_PREFIX):
        raise BreakGlassError("Unknown session", 401)
    with engine().connect() as c:
        tid = c.execute(text("SELECT break_glass_tenant(:h)"), {"h": hashlib.sha256(token.encode()).hexdigest()}).scalar()
    if tid is None:
        raise BreakGlassError("Unknown session", 401)
    return tid


def resolve(token: str) -> tuple[UUID, str, datetime]:
    """For authentication: (organisation, the holder's subject, when the session ends) for an active session."""
    _enabled()
    tid = _tenant_for_token(token)
    with tenant_transaction(tid) as conn:
        _expire(conn)
        sess = _session_by_token(conn, token)
        if sess is None or sess["status"] != "Active":
            raise BreakGlassError("This break-glass session isn't active" + (
                " (waiting for approval)" if sess and sess["status"] == "Pending" else ""), 401)
        a, u = t("break_glass_accounts"), t("users")
        subject = conn.execute(select(u.c.idp_subject).join(a, a.c.user_id == u.c.id).where(and_(
            a.c.id == sess["account_id"], a.c.disabled_at.is_(None)))).scalar()
    if subject is None:
        raise BreakGlassError("This break-glass account has been disabled", 401)
    return tid, subject, sess["expires_at"]


def open_sessions(conn: Connection) -> list[dict]:
    """Pending and active sessions in this organisation (for administrators and monitoring)."""
    _expire(conn)
    s, a, u = t("break_glass_sessions"), t("break_glass_accounts"), t("users")
    ap = a.alias("ap")
    apu = u.alias("apu")
    return [dict(r) for r in conn.execute(
        select(s.c.id, s.c.request_code, s.c.status, s.c.reason, s.c.requested_at, s.c.approved_at, s.c.expires_at,
               u.c.display_name.label("holder"), apu.c.display_name.label("approved_by"))
        .join(a, a.c.id == s.c.account_id).join(u, u.c.id == a.c.user_id)
        .outerjoin(ap, ap.c.id == s.c.approved_by).outerjoin(apu, apu.c.id == ap.c.user_id)
        .where(s.c.status.in_(["Pending", "Active"])).order_by(s.c.requested_at)).mappings()]
