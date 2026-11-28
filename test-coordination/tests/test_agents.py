"""Governed access for the bank's own AI agents."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import worker_engine
from app.services import aap, storage
from scripts.agents import create_agent
from scripts.automation import create_aap
from scripts.fake_aap import FakeAAP

ALL = ["read", "acknowledge", "update_status", "report_blocker", "post_update", "attach_evidence", "request_run"]


@pytest.fixture(scope="module", autouse=True)
def temp_store(tmp_path_factory):
    store = storage.LocalStore(str(tmp_path_factory.mktemp("agents")))
    original = storage.store
    storage.store = lambda: store
    yield store
    storage.store = original


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("AAP_TOKEN", "dev-aap-token")


@pytest.fixture(scope="module")
def fake():
    import os
    f = FakeAAP(step_seconds=0.01)
    aap.TRANSPORT = f.transport()
    old = os.environ.get("AAP_TOKEN")
    os.environ["AAP_TOKEN"] = "dev-aap-token"                 # module-wide: the world fixture links a runbook
    yield f
    aap.TRANSPORT = None
    if old is None:
        os.environ.pop("AAP_TOKEN", None)


class Agent:
    def __init__(self, client, key):
        self.c, self.h = client, {"Authorization": f"Bearer {key}"}

    def __getattr__(self, method):
        def call(path, expect=None, **kw):
            r = getattr(self.c, method)(f"/api/v1{path}", headers=self.h, **kw)
            if expect is not None:
                assert r.status_code == expect, f"{method.upper()} {path} -> {r.status_code}: {r.text}"
            return r
        return call


@pytest.fixture(scope="module")
def world(client, as_, seeded, fake):
    ids = seeded["A"]
    tag = uuid.uuid4().hex[:4]
    with worker_engine().begin() as c:
        aid, key = create_agent(c, ids["tenant"], f"DBA Agent {tag}", "Checks standby lag and prepares recovery points",
                                ids["rahul.dba"], ALL, 90)
        _, narrow_key = create_agent(c, ids["tenant"], f"Reader {tag}", "Reads only", ids["rahul.dba"], ["read"], 90)
        iid, _ = create_aap(c, ids["tenant"], f"AAP {tag}", "https://aap.bank.example/api/v2", "AAP_TOKEN")
        agent_user = c.execute(text("SELECT user_id FROM agent_identities WHERE id = :i"), {"i": aid}).scalar()
    pm, tm = as_("arjun.pm"), as_("meera.tm")
    prog = pm.post("/test-programmes", expect=201, json={"name": f"Agents {tag}", "programme_code": f"AG{tag}",
                                                          "financial_year": "FY2032", "start_date": "2031-04-01", "end_date": "2032-03-31"}).json()
    start = datetime.now(timezone.utc) + timedelta(days=3)
    e = pm.post("/test-events", expect=201, json={
        "programme_id": prog["id"], "title": "Agent-run DR test", "category": "Database", "criticality": "High",
        "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=4)).isoformat(),
        "test_manager_user_id": str(ids["meera.tm"])}).json()
    tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "submit"})
    pm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "approve_plan"})
    picker = {u["display_name"]: u for u in tm.get("/users", expect=200).json()}
    assert picker[f"DBA Agent {tag} (agent)"]["is_agent"] and not any(n.endswith("(automation)") for n in picker)
    st = tm.post(f"/test-events/{e['id']}/stakeholders", expect=201, json={"user_id": str(agent_user), "event_role": "DBA agent"}).json()[0]
    rahul_st = tm.post(f"/test-events/{e['id']}/stakeholders", expect=201, json={"user_id": str(ids["rahul.dba"]), "event_role": "DBA"}).json()[0]
    tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "begin_readiness"})
    mine = tm.post(f"/test-events/{e['id']}/activities", expect=201, json={
        "activity_code": "DB-A", "title": "Prepare database recovery point", "stakeholder_id": st["id"], "evidence_required": True}).json()
    other = tm.post(f"/test-events/{e['id']}/activities", expect=201, json={
        "activity_code": "DB-H", "title": "Human task", "stakeholder_id": rahul_st["id"]}).json()
    tm.put(f"/test-activities/{mine['id']}/runbook", expect=200, json={"integration_id": str(iid), "job_template_id": 11,
                                                                      "extra_vars": {"db_host": "cbs-drc-db01"}})
    return {"agent": Agent(client, key), "reader": Agent(client, narrow_key), "key": key, "aid": str(aid), "ev": e,
            "mine": mine, "other": other, "st": st, "tenant": ids["tenant"], "tag": tag}


def test_an_agent_works_its_own_activity(world, as_):
    ag = world["agent"]
    me = ag.get("/me", expect=200).json()
    assert me["is_agent"] and me["roles"] == [] and "request_run" in me["agent_scopes"]
    ag.post(f"/test-stakeholders/{world['st']['id']}/respond", expect=200, json={"accept": True})
    work = ag.get("/me/workspace", expect=200).json()
    assert any(json.dumps(work).count(world["mine"]["id"]) for _ in [0])
    ag.patch(f"/test-activities/{world['mine']['id']}", expect=200, json={"status": "In Progress", "comment": "Starting checks"})
    ag.post(f"/test-activities/{world['mine']['id']}/updates", expect=201, json={"message": "Apply lag 4 s, within the 15 min RPO"})
    log = f"{datetime.now():%Y-%m-%d %H:%M:%S} recovery point verified on cbs-drc-db01, database recovery point prepared {uuid.uuid4()}\n"
    up = ag.post(f"/test-activities/{world['mine']['id']}/evidence/upload", expect=201,
                 files={"file": ("lag-check.log", log.encode(), "text/plain")}, data={"evidence_type": "Log"}).json()
    assert up["precheck"]["status"] == "Proposed"
    timeline = as_("meera.tm").get(f"/test-events/{world['ev']['id']}/timeline", expect=200).json()
    assert any("Apply lag 4 s" in (x.get("message") or "") and (x.get("actor") or "").endswith("(agent)") for x in timeline)


def test_an_agent_cannot_decide_or_reach_beyond_its_work(world, as_):
    ag, e = world["agent"], world["ev"]
    refused = [
        ag.patch(f"/test-activities/{world['other']['id']}", json={"status": "In Progress"}),                 # not its activity
        ag.patch(f"/test-activities/{world['mine']['id']}", json={"due_at": "2031-01-01T00:00:00Z"}),          # not status
        ag.post(f"/test-events/{e['id']}/transitions", json={"action": "mark_ready"}),                         # stage
        ag.post(f"/test-events/{e['id']}/approvals", json={"approval_type": "Go/No-go", "decision": "Approved"}),
        ag.post(f"/test-activities/{world['mine']['id']}/runbook/run"),                                       # run directly
        ag.put(f"/test-activities/{world['mine']['id']}/runbook", json={"integration_id": str(uuid.uuid4()), "job_template_id": 1}),
        ag.get("/users"), ag.get("/agents"), ag.get("/test-events"),
    ]
    assert all(r.status_code == 403 for r in refused), [(r.status_code, r.text) for r in refused]
    ev_id = as_("meera.tm").get(f"/test-events/{e['id']}/evidence", expect=200).json()[0]["id"]
    assert ag.post(f"/test-evidence/{ev_id}/review", json={"accept": True}).status_code == 403
    reader = world["reader"]
    assert reader.post(f"/test-activities/{world['mine']['id']}/updates", json={"message": "x"}).status_code == 403
    assert "post_update" in reader.post(f"/test-activities/{world['mine']['id']}/updates", json={"message": "x"}).json()["error"]
    with worker_engine().connect() as c:
        refused_logged = c.execute(text("SELECT count(*) FROM agent_actions a JOIN agent_identities i ON i.id = a.agent_id "
                                        "WHERE i.name = :n AND a.outcome = 'refused'"), {"n": f"DBA Agent {world['tag']}"}).scalar()
    assert refused_logged >= 9                                                     # every refused call is in the log


def test_run_requests_wait_for_a_person(world, as_, fake):
    ag = world["agent"]
    r = ag.post(f"/test-activities/{world['mine']['id']}/runbook/run-requests", expect=201,
                json={"reason": "Lag is 4 s and the snapshot is fresh; ready to prepare the recovery point"}).json()
    ag.post(f"/test-activities/{world['mine']['id']}/runbook/run-requests", expect=409, json={"reason": "again please, same thing"})
    rows = as_("meera.tm").get(f"/test-events/{world['ev']['id']}/activities", expect=200).json()
    assert next(x for x in rows if x["id"] == world["mine"]["id"])["run_request"]["id"] == r["id"]
    assert ag.post(f"/run-requests/{r['id']}/decision", json={"approve": True}).status_code == 403          # can't approve itself
    assert as_("deepa.app").post(f"/run-requests/{r['id']}/decision", json={"approve": True}).status_code in (403, 404)  # unrelated
    as_("rahul.dba").post(f"/run-requests/{r['id']}/decision", expect=422, json={"approve": False})           # decline needs a note
    out = as_("rahul.dba").post(f"/run-requests/{r['id']}/decision", expect=200, json={"approve": True}).json()  # accountable person
    assert out["status"] == "Approved" and out["run"]["external_job_id"] == fake.launches[-1]["job"]
    with worker_engine().connect() as c:
        who = c.execute(text("SELECT u.display_name FROM automation_runs r JOIN users u ON u.id = r.approved_by WHERE r.id = :i"),
                        {"i": out["run"]["id"]}).scalar()
    assert who == "Rahul Das"                                                   # the person's approval launched it


def test_suspending_stops_an_agent_at_once(world, as_):
    as_("meera.tm").post(f"/agents/{world['aid']}/suspend", expect=403, json={"reason": "x"})   # not admin/risk/accountable
    view = as_("rohan.risk").get("/agents", expect=200).json()
    mine = next(a for a in view["agents"] if a["id"] == world["aid"])
    assert mine["accountable"] == "Rahul Das" and mine["calls_24h"] > 0 and "key_hash" not in mine
    assert as_("rahul.dba").get("/agents", expect=200).json()["agents"]                           # accountable sees theirs
    as_("rahul.dba").post(f"/agents/{world['aid']}/suspend", expect=200, json={"reason": "Behaving oddly in test 4"})
    r = world["agent"].get("/me")
    assert r.status_code == 403 and "suspended" in r.json()["error"]
    with worker_engine().begin() as c:
        c.execute(text("UPDATE agent_identities SET suspended_at = NULL, suspended_reason = NULL, suspended_by = NULL WHERE id = :i"),
                  {"i": world["aid"]})


def test_expired_keys_unknown_keys_and_rate_limit(world, client, monkeypatch):
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer ag.nope"}).status_code == 401
    with worker_engine().begin() as c:
        c.execute(text("UPDATE agent_identities SET expires_at = now() - interval '1 day' WHERE id = :i"), {"i": world["aid"]})
    try:
        assert "expired" in world["agent"].get("/me").json()["error"]
    finally:
        with worker_engine().begin() as c:
            c.execute(text("UPDATE agent_identities SET expires_at = now() + interval '30 days' WHERE id = :i"), {"i": world["aid"]})
    from dataclasses import replace

    from app import config
    monkeypatch.setattr(config, "settings", replace(config.settings, agent_rate_per_min=1))
    assert world["agent"].get("/me").status_code == 429


def test_the_mcp_server_drives_the_same_api(world, client):
    from scripts import mcp_agent_server as mcp
    import httpx

    def via_app(request: httpx.Request):
        r = client.request(request.method, request.url.path + (f"?{request.url.query.decode()}" if request.url.query else ""),
                           headers=dict(request.headers), content=request.content)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "")})
    mcp.TRANSPORT, mcp.KEY, mcp.URL = httpx.MockTransport(via_app), world["key"], "http://testserver/api/v1"
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["capabilities"] == {"tools": {}}
    names = [t["name"] for t in mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert "request_runbook_run" in names and not any("approve" in n for n in names)
    ok = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "post_update", "arguments": {"activity_id": world["mine"]["id"], "message": "Via MCP"}}})
    assert ok["result"]["isError"] is False
    bad = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "report_blocker", "arguments": {"activity_id": world["other"]["id"], "reason": "not mine"}}})
    assert bad["result"]["isError"] and "assigned to it" in bad["result"]["content"][0]["text"]
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    ack = mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                      "params": {"name": "acknowledge_assignment", "arguments": {"stakeholder_id": world["st"]["id"], "accept": True}}})
    assert "Extra inputs" not in ack["result"]["content"][0]["text"]            # the tool sends only what the API accepts
