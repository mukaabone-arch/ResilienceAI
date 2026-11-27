"""Monitoring: the worker's heartbeat and audit verification, and GET /health/deep."""
from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import text

from app import config
from app.db import worker_engine
from app.services import ops

TOKEN = "mon-" + "x" * 28


@pytest.fixture(scope="module", autouse=True)
def some_history(as_, seeded):
    """Something in the audit trail to verify, even when this file runs alone."""
    import uuid
    as_("arjun.pm").post("/test-programmes", expect=201, json={
        "name": "Monitoring programme", "programme_code": f"MON{uuid.uuid4().hex[:4]}", "financial_year": "FY2030",
        "start_date": "2029-04-01", "end_date": "2030-03-31"})


@pytest.fixture
def monitored(monkeypatch):
    monkeypatch.setattr(config, "settings", replace(config.settings, monitoring_token=TOKEN, audit_verify_hours=24))


def deep(client, token=TOKEN):
    return client.get("/health/deep", headers={"X-Monitoring-Token": token} if token else {})


def checks(r):
    return {c["check"]: c for c in r.json()["checks"]}


def worker_pass(force=True):
    with worker_engine().begin() as c:
        ops.verify_audit_chains(c, force=force)
        return ops.record_worker_pass(c, {"sent": 0})


def test_off_without_a_token_and_refuses_a_wrong_one(client, seeded, monkeypatch):
    monkeypatch.setattr(config, "settings", replace(config.settings, monitoring_token=""))
    assert deep(client).status_code == 404
    monkeypatch.setattr(config, "settings", replace(config.settings, monitoring_token=TOKEN))
    assert deep(client, "wrong").status_code == 401 and deep(client, None).status_code == 401


def test_healthy_after_a_worker_pass(client, seeded, monitored):
    figures = worker_pass()
    assert set(figures) >= {"dead_notifications_24h", "stuck_notifications", "scans_waiting", "ai_fallbacks_24h"}
    r = deep(client)
    c = checks(r)
    assert r.status_code == 200 and r.json()["status"] in ("ok", "warn")
    assert c["database"]["status"] == c["worker"]["status"] == c["audit_chain"]["status"] == "ok"
    assert c["audit_chain"]["detail"]["records"] > 0


def test_a_stopped_worker_fails_the_check(client, seeded, monitored):
    worker_pass()
    with worker_engine().begin() as c:
        c.execute(text("UPDATE ops_status SET checked_at = now() - interval '20 minutes' WHERE check_name = 'worker_pass'"))
    r = deep(client)
    assert r.status_code == 503 and checks(r)["worker"]["status"] == "fail" and "20 minutes" in checks(r)["worker"]["message"]
    worker_pass()
    assert deep(client).status_code == 200


def test_an_altered_audit_record_is_caught(client, seeded, monitored, as_):
    worker_pass()
    tid = seeded["A"]["tenant"]
    with worker_engine().begin() as c:
        seq, old = c.execute(text("SELECT seq, new_values FROM test_event_audit WHERE tenant_id = :t AND new_values IS NOT NULL "
                                  "ORDER BY seq DESC LIMIT 1"), {"t": tid}).one()
        c.execute(text("ALTER TABLE test_event_audit DISABLE TRIGGER trg_audit_no_update"))
        c.execute(text("UPDATE test_event_audit SET new_values = '{\"tampered\": true}' WHERE tenant_id = :t AND seq = :s"),
                  {"t": tid, "s": seq})
    try:
        assert worker_pass(force=False) is not None
        assert checks(deep(client))["audit_chain"]["status"] == "ok"          # not due yet: verified once a day
        worker_pass(force=True)
        r = deep(client)
        assert r.status_code == 503
        msg = checks(r)["audit_chain"]["message"]
        assert "does not verify for BANK" in msg and f"from record {seq}" in msg
        assert as_("rohan.risk").get("/ops/status", expect=200).json()["status"] == "fail"   # the same report in the app
        as_("meera.tm").get("/ops/status", expect=403)
    finally:
        with worker_engine().begin() as c:
            c.execute(text("UPDATE test_event_audit SET new_values = CAST(:v AS jsonb) WHERE tenant_id = :t AND seq = :s"),
                      {"t": tid, "s": seq, "v": __import__("json").dumps(old)})
            c.execute(text("ALTER TABLE test_event_audit ENABLE TRIGGER trg_audit_no_update"))
        worker_pass(force=True)
    assert checks(deep(client))["audit_chain"]["status"] == "ok"


def test_problems_worth_a_look_are_warnings(client, seeded, monitored):
    worker_pass()
    with worker_engine().begin() as c:
        c.execute(text("""UPDATE ops_status SET detail = jsonb_set(jsonb_set(detail, '{figures,scans_waiting}', '3'),
                          '{figures,dead_notifications_24h}', '2') WHERE check_name = 'worker_pass'"""))
    r = deep(client)
    c = checks(r)
    assert r.status_code == 200 and r.json()["status"] == "warn"
    assert c["malware_scanning"]["status"] == "warn" and "3 file(s)" in c["malware_scanning"]["message"]
    assert c["notifications"]["status"] == "warn" and "2 notification(s) gave up" in c["notifications"]["message"]
    worker_pass()
