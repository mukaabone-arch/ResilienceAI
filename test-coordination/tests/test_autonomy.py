"""Autonomy policy: the model-risk committee proposes, the CISO approves; runs without a person only within its rules."""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import worker_engine
from app.services import aap, automation, storage
from scripts.agents import create_agent
from scripts.automation import create_aap
from scripts.fake_aap import FakeAAP

ALL = ["read", "acknowledge", "update_status", "report_blocker", "post_update", "attach_evidence", "request_run"]


@pytest.fixture(scope="module", autouse=True)
def env(tmp_path_factory):
    store = storage.LocalStore(str(tmp_path_factory.mktemp("autonomy")))
    original = storage.store
    storage.store = lambda: store
    fake = FakeAAP(step_seconds=0.01)
    aap.TRANSPORT = fake.transport()
    old = os.environ.get("AAP_TOKEN")
    os.environ["AAP_TOKEN"] = "dev-aap-token"
    yield fake
    storage.store = original
    aap.TRANSPORT = None
    if old is None:
        os.environ.pop("AAP_TOKEN", None)


@pytest.fixture(scope="module", autouse=True)
def no_policy_left_over():
    with worker_engine().begin() as c:                    # start this file with no policy in force for tenant A
        c.execute(text("UPDATE autonomy_policies SET status = 'Superseded' WHERE status = 'Approved'"))
    yield


@pytest.fixture(scope="module")
def world(client, as_, seeded):
    ids = seeded["A"]
    tag = uuid.uuid4().hex[:4]
    with worker_engine().begin() as c:
        iid, _ = create_aap(c, ids["tenant"], f"AAP {tag}", "https://aap.bank.example/api/v2", "AAP_TOKEN")
        aid, key = create_agent(c, ids["tenant"], f"DBA Agent {tag}", "Lag checks", ids["rahul.dba"], ALL, 90)
        aid2, key2 = create_agent(c, ids["tenant"], f"Other Agent {tag}", "Not in the policy", ids["rahul.dba"], ALL, 90)
        users = {r.id: r.user_id for r in c.execute(text("SELECT id, user_id FROM agent_identities WHERE id IN (:a, :b)"),
                                                     {"a": aid, "b": aid2})}
    pm, tm = as_("arjun.pm"), as_("meera.tm")
    prog = pm.post("/test-programmes", expect=201, json={"name": f"Autonomy {tag}", "programme_code": f"AT{tag}",
                                                          "financial_year": "FY2033", "start_date": "2032-04-01", "end_date": "2033-03-31"}).json()

    def make(crit="Medium"):
        start = datetime.now(timezone.utc) + timedelta(days=3)
        e = pm.post("/test-events", expect=201, json={
            "programme_id": prog["id"], "title": f"Autonomy test {crit}", "category": "Database", "criticality": crit,
            "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=4)).isoformat(),
            "test_manager_user_id": str(ids["meera.tm"])}).json()
        tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "submit"})
        pm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "approve_plan"})
        st = {k: tm.post(f"/test-events/{e['id']}/stakeholders", expect=201, json={"user_id": str(u), "event_role": "x"}).json()[0]
              for k, u in (("rahul", ids["rahul.dba"]), ("agent", users[aid]), ("agent2", users[aid2]))}
        tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "begin_readiness"})
        acts = {}
        for code, owner in (("CHK", "rahul"), ("AG1", "agent"), ("AG2", "agent2"), ("AG3", "agent")):
            acts[code] = tm.post(f"/test-events/{e['id']}/activities", expect=201, json={
                "activity_code": code, "title": f"Standby check {code}", "stakeholder_id": st[owner]["id"]}).json()
        for code in ("AG1", "AG2", "AG3"):
            tm.put(f"/test-activities/{acts[code]['id']}/runbook", expect=200, json={"integration_id": str(iid), "job_template_id": 11})
        return e, acts

    class A:
        def __init__(self, k):
            self.h = {"Authorization": f"Bearer {k}"}

        def post(self, path, **kw):
            return client.post(f"/api/v1{path}", headers=self.h, **kw)
    return {"iid": str(iid), "aid": str(aid), "agent": A(key), "agent2": A(key2), "make": make, "tenant": ids["tenant"]}


def rule(w, **kw):
    r = {"name": "Read-only standby checks", "integration_id": w["iid"], "job_template_ids": [11], "max_criticality": "High",
         "stages": ["Mobilising", "Readiness"], "auto_launch": True, "agents": [w["aid"]], "max_runs_per_day": 20}
    return {**r, **kw}


RATIONALE = ("Template 11 only reads Data Guard status; it changes nothing and has run 40 times under supervision "
             "with no incident. Approving each run adds delay without adding control.")


def approve(as_, w, rules, review=None):
    nisha, vikas = as_("nisha.mrc"), as_("vikas.ciso")
    p = nisha.post("/autonomy/policies", expect=201, json={"rules": rules, "rationale": RATIONALE,
                                                         "review_by": str(review or date.today() + timedelta(days=180))}).json()
    nisha.post(f"/autonomy/policies/{p['id']}/propose", expect=200)
    return vikas.post(f"/autonomy/policies/{p['id']}/decision", expect=200, json={"approve": True, "note": "Reviewed with MRC minutes 14"}).json()


def test_only_the_committee_drafts_and_only_the_ciso_decides(as_, world):
    body = {"rules": [rule(world)], "rationale": "short", "review_by": str(date.today() + timedelta(days=90))}
    as_("meera.tm").post("/autonomy/policies", expect=403, json=body)
    as_("vikas.ciso").post("/autonomy/policies", expect=403, json=body)
    nisha = as_("nisha.mrc")
    for bad, msg in (({"rules": []}, "at least one rule"), ({"review_by": str(date.today() + timedelta(days=500))}, "12 months"),
                     ({"rules": [rule(world, auto_launch=False, agents=[])]}, "allows nothing"),
                     ({"rules": [rule(world, job_template_ids=[])]}, "job templates"),
                     ({"rules": [rule(world, max_runs_per_day=500)]}, "daily limit")):
        assert msg in nisha.post("/autonomy/policies", expect=422, json={**body, **bad}).json()["error"]
    p = nisha.post("/autonomy/policies", expect=201, json=body).json()
    assert "rationale" in nisha.post(f"/autonomy/policies/{p['id']}/propose", expect=422).json()["error"]
    nisha.put(f"/autonomy/policies/{p['id']}", expect=200, json={**body, "rationale": RATIONALE})
    nisha.post(f"/autonomy/policies/{p['id']}/propose", expect=200)
    nisha.post(f"/autonomy/policies/{p['id']}/decision", expect=403, json={"approve": True, "note": "mine"})
    as_("vikas.ciso").post(f"/autonomy/policies/{p['id']}/decision", expect=422, json={"approve": True, "note": " "})
    r = as_("vikas.ciso").post(f"/autonomy/policies/{p['id']}/decision", expect=200, json={"approve": False, "note": "Cap too high"}).json()
    assert r["status"] == "Rejected"
    view = as_("rohan.risk").get("/autonomy", expect=200).json()
    assert view["in_force"] is None and view["policies"][0]["decided_by_name"] == "Vikas Menon" and not view["can_decide"]
    as_("meera.tm").get("/autonomy", expect=403)


def test_without_a_policy_every_run_needs_a_person(as_, world):
    e, acts = world["make"]()
    r = as_("meera.tm").put(f"/test-activities/{acts['AG1']['id']}/runbook", expect=409,
                            json={"integration_id": world["iid"], "job_template_id": 11, "launch_mode": "auto"}).json()
    assert "No autonomy policy" in r["error"]
    req = world["agent"].post(f"/test-activities/{acts['AG1']['id']}/runbook/run-requests", json={"reason": "Lag check before failover"})
    assert req.status_code == 201 and req.json()["status"] == "Pending" and "No autonomy policy" in req.json()["needs_approval_because"]


def test_an_approved_policy_lets_covered_runs_go_without_a_person(as_, world, env):
    pol = approve(as_, world, [rule(world)])
    assert pol["status"] == "Approved"
    e, acts = world["make"]()
    tm = as_("meera.tm")
    tm.put(f"/test-activities/{acts['CHK']['id']}/runbook", expect=200,
           json={"integration_id": world["iid"], "job_template_id": 11, "launch_mode": "auto"})
    assert "doesn't let this job template" in tm.put(f"/test-activities/{acts['CHK']['id']}/runbook", expect=409, json={
        "integration_id": world["iid"], "job_template_id": 13, "launch_mode": "auto"}).json()["error"]       # switchover: not covered
    assert automation.auto_launch(world["tenant"]) >= 1                                                        # prerequisites done: starts
    rows = {x["activity_code"]: x for x in tm.get(f"/test-events/{e['id']}/activities", expect=200).json()}
    last = rows["CHK"]["runbook"]["last_run"]
    assert last["approved_by_name"] is None and last["policy_rule"] == "Read-only standby checks" and last["policy_version"] == pol["version"]
    assert rows["CHK"]["status"] == "In Progress"
    before = len(env.launches)
    automation.auto_launch(world["tenant"])
    assert len(env.launches) == before                                                                         # at most once
    ok = world["agent"].post(f"/test-activities/{acts['AG1']['id']}/runbook/run-requests", json={"reason": "Lag check before failover"}).json()
    assert ok["status"] == "AutoApproved" and ok["run"]["external_job_id"] and ok["policy"]["rule"] == "Read-only standby checks"
    other = world["agent2"].post(f"/test-activities/{acts['AG2']['id']}/runbook/run-requests", json={"reason": "Lag check before failover"}).json()
    assert other["status"] == "Pending" and "this agent" in other["needs_approval_because"]                  # not named in the rule
    with worker_engine().connect() as c:
        told = c.execute(text("SELECT count(*) FROM test_communications WHERE dedupe_key = :k"),
                         {"k": f"announcement:auto-run:{ok['run']['id']}"}).scalar()
    assert told == 1


def test_rules_are_narrow(as_, world):
    approve(as_, world, [rule(world, max_criticality="Medium", max_runs_per_day=1)])
    e, acts = world["make"]("High")
    assert "up to Medium" in world["agent"].post(f"/test-activities/{acts['AG1']['id']}/runbook/run-requests",
                                                 json={"reason": "Lag check before failover"}).json()["needs_approval_because"]
    e2, acts2 = world["make"]("Low")
    assert world["agent"].post(f"/test-activities/{acts2['AG1']['id']}/runbook/run-requests",
                               json={"reason": "Lag check before failover"}).json()["status"] == "AutoApproved"
    capped = world["agent"].post(f"/test-activities/{acts2['AG3']['id']}/runbook/run-requests", json={"reason": "Lag check again now"}).json()
    assert capped["status"] == "Pending" and "limit of 1 runs" in capped["needs_approval_because"]


def test_revoking_or_lapsing_returns_every_run_to_people(as_, world):
    pol = approve(as_, world, [rule(world)])
    as_("meera.tm").post(f"/autonomy/policies/{pol['id']}/revoke", expect=403, json={"reason": "x"})
    as_("nisha.mrc").post(f"/autonomy/policies/{pol['id']}/revoke", expect=200, json={"reason": "Incident INC-77 under review"})
    e, acts = world["make"]()
    assert world["agent"].post(f"/test-activities/{acts['AG1']['id']}/runbook/run-requests",
                               json={"reason": "Lag check before failover"}).json()["status"] == "Pending"
    pol2 = approve(as_, world, [rule(world)])
    with worker_engine().begin() as c:
        c.execute(text("UPDATE autonomy_policies SET review_by = current_date - 1 WHERE id = :i"), {"i": pol2["id"]})
    view = as_("vikas.ciso").get("/autonomy", expect=200).json()
    assert view["in_force"] is None and view["policies"][0]["lapsed"] is True
    assert world["agent"].post(f"/test-activities/{acts['AG3']['id']}/runbook/run-requests",
                               json={"reason": "Lag check before failover"}).json()["status"] == "Pending"
    with worker_engine().begin() as c:
        c.execute(text("UPDATE autonomy_policies SET status = 'Superseded' WHERE id = :i"), {"i": pol2["id"]})


def test_agents_and_integrations_cannot_govern(world):
    r = world["agent"].post("/autonomy/policies", json={"rules": [], "rationale": "", "review_by": "2030-01-01"})
    assert r.status_code == 403
