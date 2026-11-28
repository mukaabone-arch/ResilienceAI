"""Runbook automation with Ansible Automation Platform (a stand-in AAP serves the API in-process)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app.db import worker_engine
from app.services import aap, automation, storage
from scripts.automation import create_aap
from scripts.fake_aap import FakeAAP


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture(scope="module", autouse=True)
def temp_store(tmp_path_factory):
    store = storage.LocalStore(str(tmp_path_factory.mktemp("automation")))
    original = storage.store
    storage.store = lambda: store
    yield store
    storage.store = original


@pytest.fixture(scope="module")
def clock():
    return Clock()


@pytest.fixture(scope="module")
def fake(clock):
    f = FakeAAP(step_seconds=10, clock=clock)
    aap.TRANSPORT = f.transport()
    yield f
    aap.TRANSPORT = None


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("AAP_TOKEN", "dev-aap-token")


@pytest.fixture(scope="module")
def world(as_, seeded, fake):
    ids = seeded["A"]
    tag = uuid.uuid4().hex[:4]
    with worker_engine().begin() as c:
        iid, secret = create_aap(c, ids["tenant"], f"Bank AAP {tag}", "https://aap.bank.example/api/controller/v2", "AAP_TOKEN")
    pm, tm = as_("arjun.pm"), as_("meera.tm")
    prog = pm.post("/test-programmes", expect=201, json={"name": f"Auto {tag}", "programme_code": f"AU{tag}",
                                                          "financial_year": "FY2031", "start_date": "2030-04-01", "end_date": "2031-03-31"}).json()

    def make(title):
        start = datetime.now(timezone.utc) + timedelta(days=3)
        e = pm.post("/test-events", expect=201, json={
            "programme_id": prog["id"], "title": title, "category": "Database", "criticality": "High",
            "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=4)).isoformat(),
            "test_manager_user_id": str(ids["meera.tm"])}).json()
        tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "submit"})
        pm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "approve_plan"})
        st = {s: tm.post(f"/test-events/{e['id']}/stakeholders", expect=201, json={"user_id": str(ids[s]), "event_role": "x"}).json()[0]
              for s in ("rahul.dba", "ext.vendor")}
        tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "begin_readiness"})
        acts = {}
        for code, title_, ev_req in (("DB-1", "Prepare database recovery point", True), ("APP-1", "Smoke tests", False),
                                     ("DB-2", "Verify standby", False)):
            acts[code] = tm.post(f"/test-events/{e['id']}/activities", expect=201, json={
                "activity_code": code, "title": title_, "stakeholder_id": st["rahul.dba"]["id"], "evidence_required": ev_req}).json()
        return e, acts
    e, acts = make("Automated DR test")
    return {"iid": str(iid), "secret": secret, "ev": e, "acts": acts, "make": make, "tenant": ids["tenant"]}


def act(as_, w, code, ev=None):
    rows = as_("meera.tm").get(f"/test-events/{(ev or w['ev'])['id']}/activities", expect=200).json()
    return next(x for x in rows if x["activity_code"] == code)


def link(as_, w, code, template=12, extra=None, acts=None, expect=200):
    return as_("meera.tm").put(f"/test-activities/{(acts or w['acts'])[code]['id']}/runbook", expect=expect, json={
        "integration_id": w["iid"], "job_template_id": template, "extra_vars": extra if extra is not None else {"db_host": "cbs-drc-db01"}})


def tick(clock, fake, w, seconds=40):
    clock.t += seconds
    return automation.sync_active(w["tenant"])


def test_templates_and_linking_rules(as_, world):
    tm = as_("meera.tm")
    assert world["iid"] in [i["id"] for i in tm.get("/automation/integrations", expect=200).json()]
    tpls = tm.get(f"/automation/integrations/{world['iid']}/job-templates?search=standby", expect=200).json()
    assert [t["name"] for t in tpls] == ["DR - Verify standby apply lag"]
    as_("ext.vendor").get("/automation/integrations", expect=403)
    as_("rahul.dba").put(f"/test-activities/{world['acts']['DB-1']['id']}/runbook", expect=403,
                         json={"integration_id": world["iid"], "job_template_id": 12})
    r = as_("meera.tm").put(f"/test-activities/{world['acts']['DB-1']['id']}/runbook", expect=409,
                            json={"integration_id": world["iid"], "job_template_id": 12, "launch_mode": "auto"})
    assert "CISO" in r.json()["error"]
    assert "Prompt on launch" in link(as_, world, "APP-1", template=14, expect=422).json()["error"]
    assert "resilienceai_" in link(as_, world, "DB-1", extra={"resilienceai_run": "x"}, expect=422).json()["error"]
    rb = link(as_, world, "DB-1").json()
    assert rb["job_template_name"] == "DR - Prepare database recovery point" and rb["extra_vars"] == {"db_host": "cbs-drc-db01"}
    shown = act(as_, world, "DB-1")["runbook"]
    assert shown["job_template"] == rb["job_template_name"] and shown["last_run"] is None
    vendor_rows = as_("ext.vendor").get(f"/test-events/{world['ev']['id']}/activities", expect=200).json()
    assert all("runbook" not in x for x in vendor_rows)


def test_a_successful_job_updates_the_activity_and_attaches_its_output(as_, world, fake, clock):
    rahul = as_("rahul.dba")
    as_("ext.vendor").post(f"/test-activities/{world['acts']['DB-1']['id']}/runbook/run", expect=403)
    run = rahul.post(f"/test-activities/{world['acts']['DB-1']['id']}/runbook/run", expect=200).json()
    assert run["status"] == "Pending" and run["external_job_id"] and "/execution/jobs/playbook/" in run["external_url"]
    sent = fake.launches[-1]["extra_vars"]
    assert sent["db_host"] == "cbs-drc-db01" and sent["resilienceai_run"] == run["id"] and sent["resilienceai_activity"] == "DB-1"
    assert act(as_, world, "DB-1")["status"] == "In Progress"
    rahul.post(f"/test-activities/{world['acts']['DB-1']['id']}/runbook/run", expect=409)          # one run at a time
    out = tick(clock, fake, world, 15)
    assert out["checked"] >= 1 and act(as_, world, "DB-1")["runbook"]["last_run"]["status"] == "Running"
    tick(clock, fake, world, 40)
    a = act(as_, world, "DB-1")
    last = a["runbook"]["last_run"]
    assert last["status"] == "Successful" and last["evidence_id"] and a["status"] == "Completed"     # no reviewer: completed
    reg = as_("meera.tm").get(f"/test-events/{world['ev']['id']}/evidence", expect=200).json()
    item = next(x for x in reg if x["id"] == last["evidence_id"])
    assert item["filename"].startswith(f"ansible-job-{last['external_job_id']}") and item["submitted_by_name"].endswith("(automation)")
    assert item["precheck"]["status"] == "Proposed"                                       # the output was pre-checked
    with worker_engine().connect() as c:
        acts_ = [r[0] for r in c.execute(text("SELECT action FROM test_event_audit WHERE entity_id = :a AND action LIKE 'automation.%' "
                                              "ORDER BY seq"), {"a": world["acts"]["DB-1"]["id"]})]
    assert acts_ == ["automation.linked", "automation.launched", "automation.successful"]


def test_a_failed_job_blocks_the_activity_with_the_reason(as_, world, fake, clock):
    link(as_, world, "APP-1", template=12, extra={"simulate": "fail"})
    run = as_("meera.tm").post(f"/test-activities/{world['acts']['APP-1']['id']}/runbook/run", expect=200).json()
    tick(clock, fake, world, 60)
    a = act(as_, world, "APP-1")
    assert a["status"] == "Blocked" and a["runbook"]["last_run"]["status"] == "Failed"
    assert f"Ansible job {run['external_job_id']} failed: task 'Run smoke tests' on cbs-drc-db01: Login journey returned HTTP 503" \
        in a["blocked_reason"]
    with worker_engine().connect() as c:
        n = c.execute(text("SELECT count(*) FROM test_communications WHERE dedupe_key LIKE :k"),
                      {"k": f"critical_issue:run:{run['id']}:Failed"}).scalar()
    assert n == 1                                                                           # owner and test manager told
    again = as_("rahul.dba").post(f"/test-activities/{world['acts']['APP-1']['id']}/runbook/run", expect=200).json()
    assert again["attempt"] == 2 and act(as_, world, "APP-1")["status"] == "In Progress"   # a rerun unblocks it
    as_("rahul.dba").post(f"/automation-runs/{again['id']}/cancel", expect=200)
    tick(clock, fake, world, 5)
    a = act(as_, world, "APP-1")
    assert a["runbook"]["last_run"]["status"] == "Canceled" and "cancelled in Ansible" in a["blocked_reason"]
    as_("meera.tm").delete(f"/test-activities/{world['acts']['APP-1']['id']}/runbook", expect=409)   # history is kept


def test_the_webhook_is_a_doorbell(client, as_, world, fake, clock):
    link(as_, world, "DB-2", template=11)
    run = as_("rahul.dba").post(f"/test-activities/{world['acts']['DB-2']['id']}/runbook/run", expect=200).json()
    url = f"/api/v1/integrations/{world['iid']}/aap-events"
    assert client.post(url, json={"id": run["external_job_id"]}, headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.post(f"/api/v1/integrations/{uuid.uuid4()}/aap-events", json={"id": 1}).status_code == 404
    clock.t += 60
    r = client.post(url, json={"id": run["external_job_id"], "status": "failed"},      # the body's status is ignored
                    headers={"Authorization": f"Bearer {world['secret']}"})
    assert r.json() == {"tracked": True, "status": "Successful"}
    assert client.post(url, json={"id": 999999}, headers={"Authorization": f"Bearer {world['secret']}"}).json() == {"tracked": False}


def test_a_lost_launch_answer_is_reconciled(as_, world, fake, clock, monkeypatch):
    e, acts = world["make"]("Lost launch test")
    link(as_, world, "DB-2", template=11, acts=acts)
    real = fake.transport()

    def flaky(request: httpx.Request):
        resp = real.handle_request(request)
        if request.url.path.endswith("/launch/"):
            raise httpx.ReadTimeout("no answer", request=request)                    # AAP started it, but we never heard
        return resp
    monkeypatch.setattr(aap, "TRANSPORT", httpx.MockTransport(flaky))
    run = as_("rahul.dba").post(f"/test-activities/{acts['DB-2']['id']}/runbook/run", expect=200).json()
    assert run["status"] == "Launching" and run["external_job_id"] is None and "didn't answer" in run["message"]
    monkeypatch.setattr(aap, "TRANSPORT", real)
    clock.t += 60
    automation.sync_active(world["tenant"])
    last = act(as_, world, "DB-2", ev=e)["runbook"]["last_run"]
    assert last["external_job_id"] == fake.launches[-1]["job"] and last["status"] == "Successful"


def test_pausing_or_cancelling_the_test_stops_its_jobs(as_, world, fake, clock):
    e, acts = world["make"]("Kill switch test")
    link(as_, world, "DB-2", template=11, acts=acts)
    run = as_("rahul.dba").post(f"/test-activities/{acts['DB-2']['id']}/runbook/run", expect=200).json()
    as_("arjun.pm").post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "cancel", "reason": "Vendor outage"})
    assert fake.jobs[run["external_job_id"]].get("canceled")
    clock.t += 5
    automation.sync_active(world["tenant"])
    assert act(as_, world, "DB-2", ev=e)["runbook"]["last_run"]["status"] == "Canceled"


def test_aap_problems_are_explained(as_, world, fake, monkeypatch):
    e, acts = world["make"]("AAP down test")
    link(as_, world, "DB-2", template=11, acts=acts)
    fake.down = True
    try:
        run = as_("rahul.dba").post(f"/test-activities/{acts['DB-2']['id']}/runbook/run", expect=200).json()
        assert run["status"] == "Error" and "503" in run["message"]
        assert act(as_, world, "DB-2", ev=e)["status"] == "Not Started"
    finally:
        fake.down = False
    monkeypatch.delenv("AAP_TOKEN")
    r = as_("meera.tm").get(f"/automation/integrations/{world['iid']}/job-templates", expect=409).json()
    assert "AAP_TOKEN" in r["error"]
