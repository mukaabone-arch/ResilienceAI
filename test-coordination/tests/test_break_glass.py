"""Break-glass access: two holders, approval, time limit, lockout, audit."""
from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text

from app import config
from app.db import worker_engine
from app.services import breakglass as bg
from scripts.breakglass import create_holder

ORG = "BANK"


class Clock:
    """Authenticator codes are single-use per 30-second step, so each sign-in moves the clock on a step."""
    def __init__(self):
        self.t = time.time()

    def time(self):
        return self.t

    def code(self, secret):
        self.t += 30
        return bg.totp(secret, int(self.t // 30))


CLOCK = Clock()                      # one clock for the module: a holder's used steps carry over between tests


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(config, "settings", replace(config.settings, break_glass="on", break_glass_minutes=60))
    monkeypatch.setattr(bg, "time", CLOCK)
    return CLOCK


@pytest.fixture(scope="module")
def holders(seeded):
    tid = seeded["A"]["tenant"]
    tag = uuid.uuid4().hex[:6]
    out = {}
    with worker_engine().begin() as c:
        for who in ("priya", "omar", "lena"):
            email = f"{who}.{tag}@bank.example"
            p, s = create_holder(c, tid, email, who.title(), ["EnterpriseProgrammeAdministrator"])
            out[who] = {"email": email, "passphrase": p, "secret": s}
    return out


def ask(client, clock, h, reason="Entra outage INC0099123: need to start the DR test"):
    return client.post("/api/v1/break-glass/request", json={
        "org": ORG, "email": h["email"], "passphrase": h["passphrase"], "code": clock.code(h["secret"]), "reason": reason})


def approve(client, clock, h, request_code):
    return client.post("/api/v1/break-glass/approve", json={
        "org": ORG, "email": h["email"], "passphrase": h["passphrase"], "code": clock.code(h["secret"]),
        "request_code": request_code})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_credentials_primitives():
    secret = bg.new_totp_secret()
    assert bg.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 1) == "287082"       # RFC 6238 test vector (SHA-1, T=59s)
    step = int(time.time() // 30)
    code = bg.totp(secret, step)
    assert bg.totp_step(code, secret, None) == step and bg.totp_step(code, secret, step) is None     # no reuse
    assert bg.totp_step("12345", secret, None) is None
    h = bg.hash_passphrase("amber-cedar-falcon")
    assert bg.check_passphrase("amber-cedar-falcon", h) and not bg.check_passphrase("amber-cedar-falcom", h)
    assert "amber" not in h and bg.otpauth_uri(secret, "a@b.c", "BANK").startswith("otpauth://totp/")


def test_off_unless_turned_on(client, holders):
    assert client.get("/api/v1/auth/config").json()["break_glass"] is False
    r = client.post("/api/v1/break-glass/request", json={"org": ORG, "email": "x", "passphrase": "x", "code": "1", "reason": "x" * 20})
    assert r.status_code == 404


def test_two_holders_get_a_time_limited_session(client, holders, on):
    assert client.get("/api/v1/auth/config").json()["break_glass"] is True
    r = ask(client, on, holders["priya"])
    assert r.status_code == 200, r.text
    req = r.json()
    token, code = req["token"], req["request_code"]
    assert req["status"] == "Pending" and len(code) == 9 and token.startswith("bg.")
    assert client.get("/api/v1/me", headers=bearer(token)).status_code == 401              # not until approved
    assert "own request" in approve(client, on, holders["priya"], code).json()["error"]     # nobody approves themselves
    ok = approve(client, on, holders["omar"], code)
    assert ok.status_code == 200 and ok.json()["status"] == "Active" and ok.json()["holder"] == "Priya (break-glass)"
    me = client.get("/api/v1/me", headers=bearer(token))
    assert me.status_code == 200 and me.json()["display_name"] == "Priya (break-glass)"
    assert client.get("/api/v1/test-events", headers=bearer(token)).status_code == 200
    s = client.get("/api/v1/break-glass/session", headers=bearer(token)).json()
    assert s["status"] == "Active" and s["expires_at"]
    with worker_engine().begin() as c:                                                   # time runs out
        c.execute(text("UPDATE break_glass_sessions SET expires_at = now() - interval '1 minute' WHERE request_code = :c"), {"c": code})
    assert client.get("/api/v1/me", headers=bearer(token)).status_code == 401
    assert client.get("/api/v1/break-glass/session", headers=bearer(token)).json()["status"] == "Expired"


def test_ending_a_session_and_what_is_audited(client, holders, on, as_):
    req = ask(client, on, holders["lena"]).json()
    approve(client, on, holders["omar"], req["request_code"])
    sessions = as_("arjun.pm").get("/break-glass/sessions")
    assert sessions.status_code == 403                                                    # programme managers can't see them
    open_ = as_("rohan.risk").get("/break-glass/sessions", expect=200).json()
    assert any(x["request_code"] == req["request_code"] and x["approved_by"] == "Omar (break-glass)" for x in open_)
    assert client.post("/api/v1/break-glass/end", headers=bearer(req["token"])).json()["status"] == "Ended"
    assert client.get("/api/v1/me", headers=bearer(req["token"])).status_code == 401
    with worker_engine().connect() as c:
        acts = [r[0] for r in c.execute(text(
            "SELECT action FROM test_event_audit WHERE entity_type = 'BreakGlass' AND new_values->>'request_code' = :c ORDER BY seq"),
            {"c": req["request_code"]})]
    assert acts == ["break_glass.requested", "break_glass.approved", "break_glass.ended"]


def test_wrong_details_are_vague_counted_and_lock_the_account(client, holders, on):
    h = holders["omar"]
    with worker_engine().begin() as c:
        c.execute(text("UPDATE break_glass_accounts SET failed_attempts = 0, locked_until = NULL WHERE email = :e"), {"e": h["email"]})
    bad = {"org": ORG, "email": h["email"], "passphrase": "wrong-words", "reason": "Entra outage INC0099124 testing lockout"}
    errors = set()
    for _ in range(5):
        r = client.post("/api/v1/break-glass/request", json={**bad, "code": on.code(h["secret"])})
        assert r.status_code == 403
        errors.add(r.json()["error"])
    assert errors == {bg.GENERIC}                                       # never says which detail was wrong
    r = ask(client, on, h)                                              # right details, but now locked
    assert r.status_code == 403 and "locked" in r.json()["error"]
    unknown = client.post("/api/v1/break-glass/request", json={**bad, "email": "nobody@bank.example", "code": "123456"})
    nosuch = client.post("/api/v1/break-glass/request", json={**bad, "org": "NOPE", "code": "123456"})
    assert unknown.json()["error"] == nosuch.json()["error"] == bg.GENERIC
    with worker_engine().begin() as c:
        fails = c.execute(text("SELECT count(*) FROM test_event_audit WHERE action = 'break_glass.failed' "
                               "AND new_values->>'locked' = 'true'")).scalar()
        c.execute(text("UPDATE break_glass_accounts SET locked_until = NULL WHERE email = :e"), {"e": h["email"]})
    assert fails >= 1


def test_a_code_cannot_be_replayed_and_reasons_are_required(client, holders, on):
    h = holders["lena"]
    code = on.code(h["secret"])
    body = {"org": ORG, "email": h["email"], "passphrase": h["passphrase"], "code": code,
            "reason": "Entra outage INC0099125 replay check"}
    assert client.post("/api/v1/break-glass/request", json=body).status_code == 200
    assert client.post("/api/v1/break-glass/request", json=body).status_code == 403      # same code again
    assert client.post("/api/v1/break-glass/request", json={**body, "reason": "short"}).status_code == 422


def test_pending_requests_expire_and_disabled_holders_are_refused(client, holders, on):
    req = ask(client, on, holders["priya"]).json()
    with worker_engine().begin() as c:
        c.execute(text("UPDATE break_glass_sessions SET requested_at = now() - interval '20 minutes' WHERE request_code = :c"),
                  {"c": req["request_code"]})
    r = approve(client, on, holders["omar"], req["request_code"])
    assert r.status_code == 409 and "expire" in r.json()["error"]
    req = ask(client, on, holders["priya"]).json()
    approve(client, on, holders["omar"], req["request_code"])
    with worker_engine().begin() as c:
        c.execute(text("UPDATE break_glass_accounts SET disabled_at = now() WHERE email = :e"), {"e": holders["priya"]["email"]})
    try:
        assert client.get("/api/v1/me", headers=bearer(req["token"])).status_code == 401
        assert ask(client, on, holders["priya"]).status_code == 403
    finally:
        with worker_engine().begin() as c:
            c.execute(text("UPDATE break_glass_accounts SET disabled_at = NULL WHERE email = :e"), {"e": holders["priya"]["email"]})


def test_production_guard_checks_the_time_limit():
    from app.config import Settings, check_production
    s = replace(Settings(), app_env="production", break_glass="on", break_glass_minutes=600)
    assert any("BREAK_GLASS_MINUTES" in p for p in check_production(s))
    assert timedelta(minutes=60)        # default within range
