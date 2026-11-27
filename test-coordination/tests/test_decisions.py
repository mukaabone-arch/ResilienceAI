"""Go/no-go decision records: sealing, dissent, verification, replay and the offline-verifiable audit pack."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import worker_engine

T0 = datetime(2028, 1, 10, 4, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def ready(as_, seeded):
    """A test at Ready with one mandatory condition met by an approved exception and one met outright."""
    ids = seeded["A"]
    pm, tm = as_("arjun.pm"), as_("meera.tm")
    prog = pm.post("/test-programmes", expect=201, json={
        "name": "Decision programme", "programme_code": f"G{uuid.uuid4().hex[:6].upper()}", "financial_year": "FY2028",
        "start_date": "2027-04-01", "end_date": "2028-03-31"}).json()
    e = pm.post("/test-events", expect=201, json={
        "programme_id": prog["id"], "title": "Payments switch DR", "category": "Application", "criticality": "High",
        "planned_start_at": T0.isoformat(), "planned_end_at": (T0 + timedelta(hours=4)).isoformat(),
        "test_manager_user_id": str(ids["meera.tm"])}).json()
    eid = e["id"]
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "submit"})
    pm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "approve_plan"})
    for who in ("rahul.dba", "farah.biz", "kiran.net"):
        tm.post(f"/test-events/{eid}/stakeholders", expect=201, json={"user_id": str(ids[who]), "event_role": "Lead"})
    for who in ("rahul.dba", "farah.biz"):
        ack = next(a for a in as_(who).get("/me/workspace", expect=200).json()["pending_acknowledgements"] if a["event_id"] == eid)
        as_(who).post(f"/acknowledgements/{ack['id']}", expect=200, json={"accept": True})
    db = tm.post(f"/test-events/{eid}/readiness-items", expect=201, json={"title": "Database replica in sync"}).json()
    vendor = tm.post(f"/test-events/{eid}/readiness-items", expect=201, json={"title": "Vendor engineer on call"}).json()
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "begin_readiness"})
    tm.patch(f"/test-readiness-items/{db['id']}", expect=200, json={"status": "Ready"})
    as_("rohan.risk").post(f"/test-events/{eid}/approvals", expect=201, json={
        "approval_type": "ReadinessException", "decision": "Approved", "subject_id": vendor["id"],
        "comments": "Vendor confirmed by phone; written confirmation follows"})
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "mark_ready"})
    return {"eid": eid, "ids": ids}


@pytest.fixture(scope="module")
def go(as_, ready):
    ids = ready["ids"]
    return as_("arjun.pm").post(f"/test-events/{ready['eid']}/approvals", expect=201, json={
        "approval_type": "Go/No-go", "decision": "Conditional", "conditions": "Abort if replica lag exceeds 5 minutes",
        "comments": "Go for 09:30", "present_user_ids": [str(ids[x]) for x in ("arjun.pm", "meera.tm", "rahul.dba", "farah.biz")],
        "dissents": [{"user_id": str(ids["rahul.dba"]), "kind": "Concern",
                      "reason": "Replica lag was 4 minutes yesterday; little headroom"}]}).json()


def test_go_decision_is_sealed_with_what_it_rested_on(as_, ready, go):
    assert go["decision_record_id"] and len(go["decision_record_sha256"]) == 64
    recs = as_("meera.tm").get(f"/test-events/{ready['eid']}/decision-records", expect=200).json()
    rec = recs["records"][0]
    assert rec["verified"] and rec["fingerprint_matches"] and rec["anchored_in_audit_chain"]
    s = rec["snapshot"]
    assert s["decision"]["decision"] == "Conditional" and s["decision"]["decided_by"] == "Arjun Mehta"
    assert s["decision"]["conditions"] == "Abort if replica lag exceeds 5 minutes"
    items = {i["title"]: i for i in s["readiness"]["items"]}
    assert items["Database replica in sync"]["status"] == "Ready"
    exc = items["Vendor engineer on call"]["exception"]
    assert exc["approved_by"] == "Rohan Gupta" and "written confirmation follows" in exc["justification"]
    assert s["people"]["present"] == ["Arjun Mehta", "Farah Khan", "Meera Iyer", "Rahul Das"]
    assert s["people"]["not_acknowledged"] == ["Kiran Shetty"] and s["people"]["acknowledged"] == 2
    assert s["dissent_in_call"] == [{"by": "Rahul Das", "kind": "Concern", "reason": "Replica lag was 4 minutes yesterday; little headroom"}]
    assert rec["dissent"][0]["during_decision"] is True and rec["dissent"][0]["recorded_by"] == "Arjun Mehta"


def test_dissent_can_be_added_later_but_never_changed(as_, ready, go):
    rec_id = go["decision_record_id"]
    as_("kiran.net").post(f"/decision-records/{rec_id}/dissent", expect=201,
                          json={"kind": "Dissent", "reason": "Network change window overlaps; I wasn't consulted"})
    blank = as_("farah.biz").post(f"/decision-records/{rec_id}/dissent", json={"reason": " "})
    assert blank.status_code == 422 and "Say why" in blank.json()["error"]
    assert as_("audit.ia").post(f"/decision-records/{rec_id}/dissent", json={"reason": "x"}).status_code == 403
    assert as_("anita.infra").post(f"/decision-records/{rec_id}/dissent", json={"reason": "x"}).status_code in (403, 404)
    rec = as_("meera.tm").get(f"/test-events/{ready['eid']}/decision-records", expect=200).json()["records"][0]
    later = [d for d in rec["dissent"] if not d["during_decision"]]
    assert later and later[0]["by"] == "Kiran Shetty" and rec["verified"]       # adding dissent doesn't break the seal
    with worker_engine().connect() as c:                                         # the API's login can't edit or remove dissent
        grants = {r[0] for r in c.execute(text("""SELECT privilege_type FROM information_schema.role_table_grants
                                                   WHERE grantee='app_user' AND table_name='test_decision_dissents'"""))}
    assert grants == {"SELECT", "INSERT"}


def test_tampering_with_a_record_is_detected(as_, ready, go):
    with worker_engine().begin() as c:
        c.execute(text("""UPDATE test_decision_records SET snapshot = jsonb_set(snapshot, '{people,present}', '["Arjun Mehta"]')
                          WHERE id = :i"""), {"i": go["decision_record_id"]})
    try:
        rec = as_("meera.tm").get(f"/test-events/{ready['eid']}/decision-records", expect=200).json()["records"][0]
        assert rec["verified"] is False and rec["fingerprint_matches"] is False
    finally:
        with worker_engine().begin() as c:
            c.execute(text("""UPDATE test_decision_records SET snapshot = jsonb_set(snapshot, '{people,present}',
                              '["Arjun Mehta","Farah Khan","Meera Iyer","Rahul Das"]') WHERE id = :i"""), {"i": go["decision_record_id"]})


def test_vendors_dont_see_decision_records(as_, ready, go):
    as_("meera.tm").post(f"/test-events/{ready['eid']}/stakeholders", json={"user_id": str(ready["ids"]["ext.vendor"]), "event_role": "Vendor"})
    assert as_("ext.vendor").get(f"/test-events/{ready['eid']}/decision-records").status_code == 403


def test_replay_shows_the_test_as_it_stood(as_, ready, go):
    tm = as_("meera.tm")
    full = tm.get(f"/test-events/{ready['eid']}/replay", expect=200).json()
    labels = [m["label"] for m in full["marks"]]
    assert "Stage: Readiness" in labels and "Readiness exception granted" in labels and "Go/no-go: Conditional" in labels
    assert full["state"]["stage"] == "Ready" and full["state"]["go_decision"]["decision"] == "Conditional"
    assert full["state"]["readiness"]["passed"] is True
    before_exception = next(m["at"] for m in full["marks"] if m["label"] == "Readiness exception granted")
    t_minus = (datetime.fromisoformat(before_exception) - timedelta(microseconds=1)).isoformat()
    early = tm.get(f"/test-events/{ready['eid']}/replay", params={"at": t_minus}, expect=200).json()["state"]
    assert early["stage"] == "Readiness" and early["go_decision"] is None
    assert "Vendor engineer on call" in early["readiness"]["unmet"]
    assert early["people"]["acknowledged"] == 2


def test_audit_pack_verifies_offline_and_catches_edits(as_, ready, go, tmp_path):
    assert as_("rahul.dba").get(f"/test-events/{ready['eid']}/audit-pack").status_code == 403
    r = as_("audit.ia").get(f"/test-events/{ready['eid']}/audit-pack", expect=200)
    assert r.headers["content-type"] == "application/zip" and "audit-pack" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert set(z.namelist()) == {"decision_records.json", "audit_records.json", "timeline.csv", "manifest.json", "verify.py", "README.txt"}
    z.extractall(tmp_path)
    ok = subprocess.run([sys.executable, "-I", "verify.py"], cwd=tmp_path, capture_output=True, text=True)
    assert ok.returncode == 0 and "all checks passed" in ok.stdout, ok.stdout
    # An examiner who edits a decision (and even fixes the manifest) is caught by the fingerprint and the audit anchor.
    recs = json.loads((tmp_path / "decision_records.json").read_text())
    recs[0]["snapshot"]["dissent_in_call"] = []
    (tmp_path / "decision_records.json").write_text(json.dumps(recs))
    man = json.loads((tmp_path / "manifest.json").read_text())
    import hashlib
    man["files"]["decision_records.json"] = hashlib.sha256((tmp_path / "decision_records.json").read_bytes()).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    bad = subprocess.run([sys.executable, "-I", "verify.py"], cwd=tmp_path, capture_output=True, text=True)
    assert bad.returncode == 1 and "FAIL  decision Conditional" in bad.stdout


def test_no_go_is_sealed_too(as_, ready, go):
    nogo = as_("arjun.pm").post(f"/test-events/{ready['eid']}/approvals", expect=201, json={
        "approval_type": "Go/No-go", "decision": "Rejected", "comments": "Replica lag 7 minutes at 09:20"}).json()
    recs = as_("meera.tm").get(f"/test-events/{ready['eid']}/decision-records", expect=200).json()["records"]
    assert recs[0]["id"] == nogo["decision_record_id"] and recs[0]["decision"] == "Rejected" and len(recs) == 2
    assert all(r["verified"] for r in recs)
