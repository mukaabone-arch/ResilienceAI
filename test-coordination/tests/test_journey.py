"""End-to-end: the Core Banking DR Simulation journey (spec §31) plus QA-01..16 scenarios."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import engine, worker_engine
from app.services import notifications, people
from scripts.seed import CORE_BANKING_TEMPLATE

START = datetime(2027, 2, 20, 18, 30, tzinfo=timezone.utc)   # Sat 00:00 IST
SHA = hashlib.sha256(b"evidence").hexdigest()


@pytest.fixture(scope="module")
def journey(as_, seeded):
    ids = seeded["A"]
    pm, tm, owner = as_("arjun.pm"), as_("meera.tm"), as_("vikram.owner")
    # QA-01 programme
    prog = pm.post("/test-programmes", expect=201, json={
        "name": "FY2027 Resilience Programme", "programme_code": "FY27", "financial_year": "FY2027",
        "start_date": "2026-04-01", "end_date": "2027-03-31"}).json()
    tpl = pm.post("/test-templates", expect=201, json={
        "template_key": "core-banking-dr", "name": "Core Banking DR Simulation", "category": "Application",
        "default_duration": 360, "definition": CORE_BANKING_TEMPLATE}).json()
    ev = pm.post("/test-events", expect=201, headers={"Idempotency-Key": "evt-1"}, json={
        "programme_id": prog["id"], "template_id": tpl["id"], "title": "Core Banking DR Simulation",
        "category": "Application", "criticality": "Critical", "timezone": "Asia/Kolkata",
        "planned_start_at": START.isoformat(), "planned_end_at": (START + timedelta(hours=6)).isoformat(),
        "owner_user_id": str(ids["vikram.owner"]), "test_manager_user_id": str(ids["meera.tm"]),
        "business_service_ids": [str(ids["svc_cbs"])]}).json()
    return {"ids": ids, "prog": prog, "tpl": tpl, "ev": ev, "pm": pm, "tm": tm, "owner": owner}


def act_by_code(tm, eid):
    return {a["activity_code"]: a for a in tm.get(f"/test-events/{eid}/activities", expect=200).json()}


def test_core_banking_journey(journey, as_):
    ids, ev, pm, tm, owner = journey["ids"], journey["ev"], journey["pm"], journey["tm"], journey["owner"]
    eid = ev["id"]
    assert re.fullmatch(r"BANK-DR-2027-\d{3}", ev["event_code"])

    # Idempotent create returns the same event rather than a duplicate
    again = pm.post("/test-events", expect=201, headers={"Idempotency-Key": "evt-1"}, json={
        "programme_id": journey["prog"]["id"], "template_id": journey["tpl"]["id"], "title": "Core Banking DR Simulation",
        "category": "Application", "criticality": "Critical", "timezone": "Asia/Kolkata",
        "planned_start_at": START.isoformat(), "planned_end_at": (START + timedelta(hours=6)).isoformat(),
        "owner_user_id": str(ids["vikram.owner"]), "test_manager_user_id": str(ids["meera.tm"]),
        "business_service_ids": [str(ids["svc_cbs"])]}).json()
    assert again["id"] == eid

    # Template instantiated: 6 activities, 4 readiness items, dependencies
    acts = act_by_code(tm, eid)
    assert len(acts) == 6
    deps = tm.get(f"/test-events/{eid}/dependencies", expect=200).json()
    assert len(deps["edges"]) == 6

    # Lifecycle: submit -> approve plan (PM only)
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "submit"})
    tm.post(f"/test-events/{eid}/transitions", expect=403, json={"action": "approve_plan"})
    pm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "approve_plan"})

    # Can't begin readiness without stakeholders
    r = tm.post(f"/test-events/{eid}/transitions", expect=409, json={"action": "begin_readiness"})
    assert "No active stakeholders assigned" in r.json()["details"]

    # Assign stakeholders (individuals + team) — QA-03
    roles = {"anita.infra": "Infrastructure Lead", "kiran.net": "Network Lead", "deepa.app": "Application Lead",
             "farah.biz": "Business Validator", "ext.vendor": "Storage Vendor"}
    st = {}
    for sub, role in roles.items():
        st[sub] = tm.post(f"/test-events/{eid}/stakeholders", expect=201,
                          json={"user_id": str(ids[sub]), "event_role": role}).json()[0]
    team = tm.post(f"/test-events/{eid}/stakeholders", expect=201,
                   json={"team_id": str(ids["team_db"]), "event_role": "Database Recovery"}).json()
    st.update({next(k for k in ("sana.lead", "rahul.dba") if ids[k] == UUID(m["user_id"])): m for m in team})
    tm.post(f"/test-events/{eid}/stakeholders", expect=409, json={"user_id": str(ids["kiran.net"]), "event_role": "x"})

    # Assign activities to owners; reviewer for TV-01
    owners = {"INF-01": "anita.infra", "NET-01": "kiran.net", "DB-01": "rahul.dba", "APP-01": "deepa.app",
              "TV-01": "deepa.app", "BV-01": "farah.biz"}
    for code, sub in owners.items():
        tm.patch(f"/test-activities/{acts[code]['id']}", expect=200, json={"stakeholder_id": st[sub]["id"]})
    tm.post(f"/test-events/{eid}/activities", expect=201, json={
        "activity_code": "VND-01", "title": "Storage replication health confirmation", "stakeholder_id": st["ext.vendor"]["id"],
        "is_mandatory": True, "review_required": True, "reviewer_user_id": str(ids["sana.lead"])})

    # Stakeholders acknowledge (vendor via the acknowledgement record) — notifications were queued
    for sub in ("anita.infra", "kiran.net", "deepa.app", "farah.biz", "sana.lead", "rahul.dba"):
        as_(sub).post(f"/test-stakeholders/{st[sub]['id']}/respond", expect=200, json={"accept": True})
    as_("kiran.net").post(f"/test-stakeholders/{st['anita.infra']['id']}/respond", expect=403, json={"accept": True})
    ws = as_("ext.vendor").get("/me/workspace", expect=200).json()
    acks = [k for k in ws["pending_acknowledgements"] if k["event_id"] == eid]
    assert len(acks) == 1
    as_("ext.vendor").post(f"/acknowledgements/{acks[0]['id']}", expect=200, json={"accept": True})

    # Vendor sees only their own activity, not other assignments
    v_acts = as_("ext.vendor").get(f"/test-events/{eid}/activities", expect=200).json()
    assert [a["activity_code"] for a in v_acts] == ["VND-01"]

    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "begin_readiness"})

    # QA-06: block the database prerequisite; downstream identified
    rahul = as_("rahul.dba")
    as_("anita.infra").patch(f"/test-activities/{acts['INF-01']['id']}", expect=200, json={"status": "In Progress"})
    as_("anita.infra").patch(f"/test-activities/{acts['INF-01']['id']}", expect=200, json={"status": "Submitted"})
    # APP-01 can't start: waiting on DB-01 and NET-01
    r = as_("deepa.app").patch(f"/test-activities/{acts['APP-01']['id']}", expect=409, json={"status": "In Progress"})
    assert any("DB-01" in d for d in r.json()["details"])
    rahul.patch(f"/test-activities/{acts['DB-01']['id']}", expect=200, json={"status": "In Progress"})
    rahul.patch(f"/test-activities/{acts['DB-01']['id']}", expect=422, json={"status": "Blocked"})  # reason required
    rahul.patch(f"/test-activities/{acts['DB-01']['id']}", expect=200,
                json={"status": "Blocked", "blocked_reason": "Recovery point older than RPO; awaiting fresh snapshot"})
    dm = tm.get(f"/test-events/{eid}/dependencies", expect=200).json()
    state = {n["activity_code"]: n["dependency_state"] for n in dm["nodes"]}
    assert state["DB-01"] == "blocked" and state["APP-01"] == "blocked" and state["BV-01"] == "blocked"
    assert state["NET-01"] == "actionable"

    # Readiness: QA-08 — go decision prevented while mandatory readiness unmet
    rd = tm.get(f"/test-events/{eid}/readiness", expect=200).json()
    items = {i["title"]: i for i in rd["items"]}
    assert not rd["gate"]["passed"]
    r = tm.post(f"/test-events/{eid}/transitions", expect=409, json={"action": "mark_ready"})
    assert any("Database recovery ready" in d for d in r.json()["details"])

    # Unblock DB, complete prerequisites with evidence
    rahul.patch(f"/test-activities/{acts['DB-01']['id']}", expect=200, json={"status": "In Progress"})
    rahul.patch(f"/test-activities/{acts['DB-01']['id']}", expect=409, json={"status": "Submitted"})  # evidence first
    e1 = rahul.post(f"/test-activities/{acts['DB-01']['id']}/evidence", expect=201, json={
        "evidence_type": "Report", "storage_reference": "s3://evidence/bank/db-snapshot.pdf",
        "filename": "db-snapshot.pdf", "checksum": SHA}).json()
    rahul.patch(f"/test-activities/{acts['DB-01']['id']}", expect=200, json={"status": "Submitted"})
    rahul.post(f"/test-evidence/{e1['id']}/review", expect=403, json={"accept": True})          # no self-review
    # QA-11 reject + resubmit
    tm.post(f"/test-evidence/{e1['id']}/review", expect=422, json={"accept": False})
    tm.post(f"/test-evidence/{e1['id']}/review", expect=200, json={"accept": False, "comment": "Missing timestamp"})
    e2 = rahul.post(f"/test-activities/{acts['DB-01']['id']}/evidence", expect=201, json={
        "evidence_type": "Report", "storage_reference": "s3://evidence/bank/db-snapshot-v2.pdf",
        "checksum": SHA, "supersedes_id": e1["id"]}).json()
    tm.post(f"/test-evidence/{e2['id']}/review", expect=200, json={"accept": True})

    for code, sub in (("NET-01", "kiran.net"),):
        as_(sub).patch(f"/test-activities/{acts[code]['id']}", expect=200, json={"status": "In Progress"})
        as_(sub).patch(f"/test-activities/{acts[code]['id']}", expect=200, json={"status": "Submitted"})
    vnd = act_by_code(tm, eid)["VND-01"]
    vendor = as_("ext.vendor")
    vendor.patch(f"/test-activities/{vnd['id']}", expect=200, json={"status": "In Progress"})
    vendor.patch(f"/test-activities/{vnd['id']}", expect=200, json={"status": "Submitted"})
    assert act_by_code(tm, eid)["VND-01"]["status"] == "Submitted"        # review required → not completed
    as_("sana.lead").post(f"/test-activities/{vnd['id']}/review", expect=200, json={"accept": True})

    # Readiness items
    cr = items["Change request approved"]
    tm.patch(f"/test-readiness-items/{cr['id']}", expect=200, json={"status": "Ready"})
    tm.post(f"/test-readiness-items/{cr['id']}/evidence", expect=201, json={
        "evidence_type": "ExternalRecord", "storage_reference": "servicenow://CHG0041234"})
    rd = tm.get(f"/test-events/{eid}/readiness", expect=200).json()
    assert any("evidence not yet accepted" in u for u in rd["gate"]["unmet"])
    pending_ev = [e for e in rd["items"] if e["title"] == "Change request approved"]
    as_("rohan.risk").get(f"/test-events/{eid}/readiness", expect=200)
    # risk reviews the change-request evidence
    with engine().connect() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(ids["tenant"])})
        ev_id = c.execute(text("SELECT id FROM test_evidence WHERE readiness_item_id = :r"), {"r": cr["id"]}).scalar()
    as_("rohan.risk").post(f"/test-evidence/{ev_id}/review", expect=200, json={"accept": True})
    tm.post(f"/test-events/{eid}/readiness", expect=200, json={"updates": [
        {"id": items["Database recovery ready"]["id"], "status": "Ready"},
        {"id": items["Business validation team confirmed"]["id"], "status": "In Progress"}]})
    # Readiness exception for the business-team item (risk approves with justification)
    as_("rohan.risk").post(f"/test-events/{eid}/approvals", expect=422, json={
        "approval_type": "ReadinessException", "decision": "Approved", "subject_id": items["Business validation team confirmed"]["id"]})
    as_("rohan.risk").post(f"/test-events/{eid}/approvals", expect=201, json={
        "approval_type": "ReadinessException", "decision": "Approved", "subject_id": items["Business validation team confirmed"]["id"],
        "comments": "Named validators confirmed by email; roster upload pending"})
    assert pending_ev
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "mark_ready"})

    # Start requires a Go decision; test manager can't approve own event; owner can't for Critical
    r = tm.post(f"/test-events/{eid}/start", expect=409)
    assert "No valid approved Go decision" in r.json()["details"]
    tm.post(f"/test-events/{eid}/approvals", expect=403, json={"approval_type": "Go/No-go", "decision": "Approved"})
    owner.post(f"/test-events/{eid}/approvals", expect=403, json={"approval_type": "Go/No-go", "decision": "Approved"})
    # QA-09 go decision attributed & audited (with idempotency)
    g = pm.post(f"/test-events/{eid}/approvals", expect=201, headers={"Idempotency-Key": "go-1"},
                json={"approval_type": "Go/No-go", "decision": "Approved", "comments": "All mandatory items met"}).json()
    g2 = pm.post(f"/test-events/{eid}/approvals", expect=201, headers={"Idempotency-Key": "go-1"},
                 json={"approval_type": "Go/No-go", "decision": "Approved", "comments": "All mandatory items met"}).json()
    assert g["id"] == g2["id"] and g["readiness_snapshot"]["passed"]

    started = tm.post(f"/test-events/{eid}/start", expect=200).json()
    assert started["lifecycle_status"] == "Live" and started["actual_start_at"]

    # Live: announcements, milestones, status log, pause/resume with approval
    tm.post(f"/test-events/{eid}/announcements", expect=201, json={"message": "Failover window open. Ops bridge: +91-xx"})
    as_("deepa.app").post(f"/test-events/{eid}/logs", expect=403, json={"log_type": "Decision", "message": "x"})
    as_("deepa.app").post(f"/test-events/{eid}/logs", expect=201, json={"log_type": "Status", "message": "App tier up on DR"})
    tm.post(f"/test-events/{eid}/transitions", expect=409, json={"action": "pause"})        # reason required
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "pause", "reason": "Network flap at DR edge"})
    tm.post(f"/test-events/{eid}/transitions", expect=409, json={"action": "resume"})
    pm.post(f"/test-events/{eid}/approvals", expect=201, json={"approval_type": "Resume", "decision": "Approved"})
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "resume"})

    deepa = as_("deepa.app")
    deepa.patch(f"/test-activities/{acts['APP-01']['id']}", expect=200, json={"status": "In Progress"})
    deepa.patch(f"/test-activities/{acts['APP-01']['id']}", expect=200, json={"status": "Submitted"})
    deepa.patch(f"/test-activities/{acts['TV-01']['id']}", expect=200, json={"status": "In Progress"})
    ev_tv = deepa.post(f"/test-activities/{acts['TV-01']['id']}/evidence", expect=201, json={
        "evidence_type": "Screenshot", "storage_reference": "s3://evidence/bank/tv.png", "checksum": SHA}).json()
    deepa.patch(f"/test-activities/{acts['TV-01']['id']}", expect=200, json={"status": "Submitted"})
    as_("farah.biz").patch(f"/test-activities/{acts['BV-01']['id']}", expect=200, json={"status": "In Progress"})
    as_("farah.biz").patch(f"/test-activities/{acts['BV-01']['id']}", expect=200, json={"status": "Submitted"})

    # Measurements: typed RTO/RPO
    m1 = as_("rahul.dba").post(f"/test-events/{eid}/measurements", expect=201, json={
        "measurement_type": "RTO", "target_value": 4, "target_unit": "Hours", "actual_value": 230, "unit": "Minutes"}).json()
    assert m1["target_met"] is True and m1["result_status"] == "Not Validated"
    m2 = as_("rahul.dba").post(f"/test-events/{eid}/measurements", expect=201, json={
        "measurement_type": "RPO", "target_value": 15, "target_unit": "Minutes", "actual_value": 22, "unit": "Minutes"}).json()
    as_("rahul.dba").post(f"/test-events/{eid}/measurements", expect=422, json={
        "measurement_type": "RTO", "target_value": 4, "target_unit": "Hours", "actual_value": 99, "unit": "Percentage"})

    # QA-12 finding with remediation
    f = as_("rahul.dba").post(f"/test-events/{eid}/issues", expect=201, json={
        "title": "RPO exceeded by 7 minutes", "severity": "High", "issue_type": "Finding"}).json()
    as_("rahul.dba").post(f"/test-events/{eid}/issues", expect=201, json={
        "title": "DR edge firewall flap", "severity": "Critical", "issue_type": "Incident"})

    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "complete"})

    # QA-13: closure blocked — evidence review pending, measurements unvalidated, findings, approvals...
    tm.put(f"/test-events/{eid}/outcome", expect=200, json={"outcome": "Partial", "summary": "RTO met; RPO missed by 7 min"})
    rep = tm.get(f"/test-events/{eid}/closure-report", expect=200).json()
    assert rep["status"] == "draft"
    blockers = " | ".join(rep["blockers"])
    assert "evidence item(s) awaiting review" in blockers and "not validated" in blockers
    assert "has no owner/due date" in blockers and "Incident" in blockers
    r = tm.post(f"/test-events/{eid}/close", expect=409)
    assert "Closure approval not yet recorded" in r.json()["details"]

    # Work through the closure checklist
    tm.post(f"/test-evidence/{ev_tv['id']}/review", expect=200, json={"accept": True})
    as_("rahul.dba").post(f"/test-measurements/{m1['id']}/validate", expect=403)              # recorder can't validate
    as_("rohan.risk").post(f"/test-measurements/{m1['id']}/validate", expect=200)
    assert as_("rohan.risk").post(f"/test-measurements/{m2['id']}/validate", expect=200).json()["result_status"] == "Not Met"
    ra = tm.post(f"/test-issues/{f['id']}/remediation-actions", expect=201, json={
        "title": "Increase log-shipping frequency to 5 min", "owner_user_id": str(ids["rahul.dba"]),
        "due_at": (START + timedelta(days=30)).isoformat()}).json()
    assert ra["action_code"].endswith("-R01")
    incidents = [i for i in rep["issues"] if i["type"] == "Incident"]
    with engine().connect() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(ids["tenant"])})
        inc_id = c.execute(text("SELECT id FROM test_issues WHERE issue_code = :c"), {"c": incidents[0]["code"]}).scalar()
    tm.patch(f"/test-issues/{inc_id}", expect=200, json={"status": "Resolved"})

    # Closure approval can't precede business + risk sign-off
    r = pm.post(f"/test-events/{eid}/approvals", expect=409, json={"approval_type": "Closure", "decision": "Approved"})
    assert any("Business" in d for d in r.json()["details"])
    as_("farah.biz").post(f"/test-events/{eid}/approvals", expect=201, json={"approval_type": "Business", "decision": "Approved"})
    as_("rohan.risk").post(f"/test-events/{eid}/approvals", expect=201, json={
        "approval_type": "RiskReview", "decision": "Conditional", "conditions": "Remediation RA due within 30 days"})
    pm.post(f"/test-events/{eid}/approvals", expect=201, json={"approval_type": "Closure", "decision": "Approved"})
    closed = tm.post(f"/test-events/{eid}/close", expect=200).json()
    assert closed["lifecycle_status"] == "Closed" and closed["outcome"] == "Partial"

    # QA-16: final report = frozen snapshot of approved records
    rep = as_("leela.cio").get(f"/test-events/{eid}/closure-report", expect=200).json()
    assert rep["status"] == "final"
    assert {d["type"] for d in rep["decisions"]} >= {"Plan", "Go/No-go", "Resume", "Business", "RiskReview", "Closure"}
    assert any(m["result"] == "Not Met" for m in rep["measurements"])
    # Closed events can't be edited
    tm.patch(f"/test-events/{eid}", expect=409, json={"expected_version": closed["version"], "title": "x"})

    # Timeline, portfolio, audit chain
    tl = tm.get(f"/test-events/{eid}/timeline", expect=200).json()
    assert any(x["type"] == "Announcement" for x in tl) and any(x["type"] == "lifecycle.close" for x in tl)
    pf = as_("leela.cio").get("/portfolio/summary", expect=200).json()
    assert pf["by_status"]["Closed"] >= 1 and "data_as_of" in pf
    as_("rahul.dba").get("/portfolio/summary", expect=403)
    v = as_("audit.ia").get("/audit/verify", expect=200).json()
    assert v["valid"] and v["records"] > 40


def test_reschedule_invalidates_go_and_notifies(as_, journey, seeded):
    ids, pm, tm = journey["ids"], journey["pm"], journey["tm"]
    ev = pm.post("/test-events", expect=201, json={
        "programme_id": journey["prog"]["id"], "title": "Payments DB failover", "category": "Database",
        "criticality": "High", "planned_start_at": (START + timedelta(days=7)).isoformat(),
        "planned_end_at": (START + timedelta(days=7, hours=4)).isoformat(),
        "test_manager_user_id": str(ids["meera.tm"]), "owner_user_id": str(ids["vikram.owner"])}).json()
    eid = ev["id"]
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "submit"})
    pm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "approve_plan"})
    s = tm.post(f"/test-events/{eid}/stakeholders", expect=201, json={"user_id": str(ids["rahul.dba"]), "event_role": "DBA"}).json()[0]
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "begin_readiness"})
    it = tm.post(f"/test-events/{eid}/readiness-items", expect=201, json={"title": "Backups verified"}).json()
    tm.patch(f"/test-readiness-items/{it['id']}", expect=200, json={"status": "Ready"})
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "mark_ready"})
    as_("vikram.owner").post(f"/test-events/{eid}/approvals", expect=201, json={"approval_type": "Go/No-go", "decision": "Approved"})
    cur = tm.get(f"/test-events/{eid}", expect=200).json()

    # QA-04: reschedule → back to Readiness, go invalidated, impacted users get Outlook/email update
    tm.patch(f"/test-events/{eid}", expect=422, json={"expected_version": cur["version"],
             "planned_start_at": (START + timedelta(days=8)).isoformat(), "planned_end_at": (START + timedelta(days=8, hours=4)).isoformat()})
    tm.patch(f"/test-events/{eid}", expect=409, json={"expected_version": cur["version"] - 1, "title": "stale"})
    new = tm.patch(f"/test-events/{eid}", expect=200, json={
        "expected_version": cur["version"], "reschedule_reason": "Clashes with quarter-end batch",
        "planned_start_at": (START + timedelta(days=8)).isoformat(),
        "planned_end_at": (START + timedelta(days=8, hours=4)).isoformat()}).json()
    assert new["lifecycle_status"] == "Readiness"
    assert not tm.get(f"/test-events/{eid}/readiness", expect=200).json()["go_decision_valid"]
    comms = tm.get(f"/test-events/{eid}/communications", expect=200).json()
    sc = [c for c in comms if c["communication_type"] == "ScheduleChange"]
    assert len(sc) == 1 and sc[0]["delivery"].get("Queued", 0) >= 2

    # readiness regression: Ready item back to Blocked → event leaves Ready automatically
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "mark_ready"})
    tm.patch(f"/test-readiness-items/{it['id']}", expect=200, json={"status": "Blocked", "comment": "Backup job failed"})
    assert tm.get(f"/test-events/{eid}", expect=200).json()["lifecycle_status"] == "Readiness"

    # QA-05: unacknowledged assignment escalates once to the team lead / test manager
    later = datetime.now(timezone.utc) + timedelta(hours=49)
    r1 = people.run_escalations(seeded["A"]["tenant"], later)
    r2 = people.run_escalations(seeded["A"]["tenant"], later)
    assert r1["ack_escalations"] >= 1 and r2["ack_escalations"] == 0
    _ = s

    # Blackout: needs PM override with reason
    with worker_engine().begin() as c:
        c.execute(text("INSERT INTO blackout_periods (tenant_id, name, starts_at, ends_at) VALUES (:t, 'Year-end freeze', :s, :e)"),
                  {"t": seeded["A"]["tenant"], "s": datetime(2027, 3, 25, tzinfo=timezone.utc), "e": datetime(2027, 4, 5, tzinfo=timezone.utc)})
    body = {"programme_id": journey["prog"]["id"], "title": "Cyber recovery drill", "category": "Cyber", "criticality": "High",
            "planned_start_at": "2027-03-28T04:00:00+00:00", "planned_end_at": "2027-03-28T08:00:00+00:00"}
    r = tm.post("/test-events", expect=409, json=body)
    assert "Year-end freeze" in r.json()["details"][0]
    tm.post("/test-events", expect=403, json={**body, "blackout_override_reason": "Regulator deadline"})
    made = pm.post("/test-events", expect=201, json={**body, "blackout_override_reason": "Regulator deadline"}).json()
    assert made["event_code"].startswith("BANK-CY-2027-")


def test_circular_dependency_rejected(as_, journey):  # QA-07
    tm, eid = journey["tm"], journey["ev"]["id"]
    a = act_by_code(tm, eid)
    r = tm.post(f"/test-events/{eid}/dependencies", expect=422, json={
        "predecessor_activity_id": a["BV-01"]["id"], "successor_activity_id": a["INF-01"]["id"]})
    assert "cycle" in r.json()["error"]


def test_tenant_isolation(as_, journey, seeded):  # QA-14
    eid = journey["ev"]["id"]
    other = as_("arjun.pm", tenant="B")
    other.get(f"/test-events/{eid}", expect=404)
    assert other.get("/test-events", expect=200).json() == []
    # RLS holds even with a raw query that forgets the tenant filter
    with engine().connect() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(seeded["B"]["tenant"])})
        assert c.execute(text("SELECT count(*) FROM test_events")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM users")).scalar() == 14
        c.execute(text("SELECT set_config('app.tenant_id', '', false)"))
        assert c.execute(text("SELECT count(*) FROM test_events")).scalar() == 0
    # unrelated internal user without portfolio role can't see the event
    as_("kiran.net", tenant="B").get("/test-events", expect=200)
    with pytest.raises(AssertionError):
        as_("nobody").get("/me", expect=200)


def test_audit_is_append_only(seeded):  # RA-078
    with engine().connect() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(seeded["A"]["tenant"])})
        for stmt in ("UPDATE test_event_audit SET action='x'", "DELETE FROM test_event_audit"):
            with pytest.raises(DBAPIError):  # revoked grant and/or append-only trigger
                c.execute(text(stmt))
            c.rollback()
            c.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(seeded["A"]["tenant"])})
    # even the owner role is stopped by the trigger
    with worker_engine().connect() as c, pytest.raises(Exception, match="append-only"):
        c.execute(text("UPDATE test_event_audit SET action='x'"))


def test_notification_retry_and_dead_letter(seeded, monkeypatch):  # QA-15
    class Down:
        def send(self, *a, **k):
            raise ConnectionError("Graph API unavailable")
    monkeypatch.setitem(notifications.ADAPTERS, "Teams", Down())
    tid = seeded["A"]["tenant"]
    t0 = datetime.now(timezone.utc)
    r = notifications.dispatch_due(tid, t0, batch=10_000)
    assert r.sent > 0 and r.failed > 0
    far = t0
    for _ in range(6):
        far += timedelta(hours=2)
        notifications.dispatch_due(tid, far, batch=10_000)
    with worker_engine().connect() as c:
        rows = dict(c.execute(text("SELECT delivery_status, count(*) FROM notification_jobs WHERE tenant_id=:t GROUP BY 1"), {"t": tid}).all())
    assert rows.get("Dead", 0) > 0 and rows.get("Queued", 0) == 0
    monkeypatch.delitem(notifications.ADAPTERS, "Teams")


def test_failed_request_rolls_back(as_, journey, seeded):
    """A domain error after a partial write must leave no trace (one transaction per request)."""
    tm, eid, ids = journey["tm"], journey["ev"]["id"], seeded["A"]
    ev2 = journey["pm"].post("/test-events", expect=201, json={
        "programme_id": journey["prog"]["id"], "template_id": journey["tpl"]["id"], "title": "Rollback probe",
        "category": "Application", "criticality": "Low", "test_manager_user_id": str(ids["meera.tm"]),
        "planned_start_at": "2027-01-05T04:00:00+00:00", "planned_end_at": "2027-01-05T08:00:00+00:00"}).json()
    tm.post(f"/test-events/{ev2['id']}/transitions", expect=200, json={"action": "submit"})
    journey["pm"].post(f"/test-events/{ev2['id']}/transitions", expect=200, json={"action": "approve_plan"})
    s = tm.post(f"/test-events/{ev2['id']}/stakeholders", expect=201, json={"user_id": str(ids["deepa.app"]), "event_role": "App"}).json()[0]
    app01 = act_by_code(tm, ev2["id"])["APP-01"]
    # reassignment (writes history) + illegal status jump in one request → 409, history must roll back
    tm.patch(f"/test-activities/{app01['id']}", expect=409, json={"stakeholder_id": s["id"], "status": "Submitted"})
    with worker_engine().connect() as c:
        n = c.execute(text("SELECT count(*) FROM activity_assignment_history WHERE activity_id=:a"), {"a": app01["id"]}).scalar()
        owner = c.execute(text("SELECT stakeholder_id FROM test_activities WHERE id=:a"), {"a": app01["id"]}).scalar()
    assert n == 0 and owner is None
    _ = eid
