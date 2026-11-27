"""Evidence upload, integrity-checked download and the evidence register (Screen 09, RA-057/058)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import worker_engine
from app.services import storage

PDF = b"%PDF-1.4\n% DR evidence\n" + b"0" * 2048


@pytest.fixture(scope="module", autouse=True)
def temp_store(tmp_path_factory):
    store = storage.LocalStore(str(tmp_path_factory.mktemp("evidence")))
    original = storage.store
    storage.store = lambda: store
    yield store
    storage.store = original


@pytest.fixture(scope="module")
def ev(as_, seeded):
    ids = seeded["A"]
    pm, tm = as_("arjun.pm"), as_("meera.tm")
    prog = pm.post("/test-programmes", expect=201, json={
        "name": "Evidence programme", "programme_code": "EVP", "financial_year": "FY2028",
        "start_date": "2027-04-01", "end_date": "2028-03-31"}).json()
    start = datetime(2027, 9, 1, 4, 0, tzinfo=timezone.utc)
    e = pm.post("/test-events", expect=201, json={
        "programme_id": prog["id"], "title": "Evidence test", "category": "Database", "criticality": "Medium",
        "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=2)).isoformat(),
        "test_manager_user_id": str(ids["meera.tm"])}).json()
    tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "submit"})
    pm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "approve_plan"})
    st = {s: tm.post(f"/test-events/{e['id']}/stakeholders", expect=201, json={"user_id": str(ids[s]), "event_role": "x"}).json()[0]
          for s in ("rahul.dba", "ext.vendor")}
    a = tm.post(f"/test-events/{e['id']}/activities", expect=201, json={
        "activity_code": "DB-9", "title": "Snapshot", "stakeholder_id": st["rahul.dba"]["id"], "evidence_required": True}).json()
    v = tm.post(f"/test-events/{e['id']}/activities", expect=201, json={
        "activity_code": "VN-9", "title": "Vendor check", "stakeholder_id": st["ext.vendor"]["id"]}).json()
    item = tm.post(f"/test-events/{e['id']}/readiness-items", expect=201, json={"title": "Backups verified", "evidence_required": True}).json()
    return {"ev": e, "a": a, "v": v, "item": item, "ids": ids}


def up(client, path, data=PDF, name="snapshot.pdf", sha=None, etype="Report", expect=201):
    files = {"file": (name, data, "application/pdf")}
    form = {"evidence_type": etype}
    if sha is not None:
        form["sha256"] = sha
    return client.post(path, expect=expect, files=files, data=form)


def test_upload_and_download_roundtrip(as_, ev):
    rahul = as_("rahul.dba")
    r = up(rahul, f"/test-activities/{ev['a']['id']}/evidence/upload", sha=hashlib.sha256(PDF).hexdigest()).json()
    assert r["checksum"] == hashlib.sha256(PDF).hexdigest() and r["size_bytes"] == len(PDF)
    assert r["storage_backend"] == "local" and r["scan_status"] == "NotScanned" and r["filename"] == "snapshot.pdf"
    d = as_("meera.tm").get(f"/test-evidence/{r['id']}/download", expect=200)
    assert d.content == PDF
    assert d.headers["content-type"] == "application/octet-stream"
    assert "attachment" in d.headers["content-disposition"] and d.headers["x-evidence-sha256"] == r["checksum"]
    assert as_("meera.tm").post(f"/test-evidence/{r['id']}/verify", expect=200).json()["valid"] is True


def test_rejections(as_, ev):
    rahul, path = as_("rahul.dba"), f"/test-activities/{ev['a']['id']}/evidence/upload"
    assert "changed between your browser" in up(rahul, path, sha="0" * 64, expect=422).json()["error"]
    assert "can't be uploaded" in up(rahul, path, name="run.exe", expect=422).json()["error"]
    assert "empty" in up(rahul, path, data=b"", expect=422).json()["error"]
    eicar = storage.EICAR + b"\n"
    assert "malware" in up(rahul, path, data=eicar, name="eicar.txt", expect=422).json()["error"]
    up(as_("kiran.net"), path, expect=404)                         # not on this test: can't see it
    up(as_("ext.vendor"), path, expect=403)                        # on the test, but not their activity
    up(rahul, path, etype="Bogus", expect=422)


def test_tampered_file_is_refused(as_, ev, temp_store):
    r = up(as_("rahul.dba"), f"/test-activities/{ev['a']['id']}/evidence/upload", name="log.txt", data=b"recovery ok\n").json()
    (temp_store.root / r["storage_reference"]).write_bytes(b"recovery FAILED\n")       # someone edits the stored file
    d = as_("meera.tm").get(f"/test-evidence/{r['id']}/download", expect=409).json()
    assert "Integrity check failed" in d["error"]
    assert as_("meera.tm").post(f"/test-evidence/{r['id']}/verify", expect=200).json()["valid"] is False
    with worker_engine().connect() as c:
        n = c.execute(text("SELECT count(*) FROM test_event_audit WHERE action='evidence.integrity_failed' AND entity_id=:i"),
                      {"i": r["id"]}).scalar()
    assert n == 2


def test_readiness_upload_counts_once_accepted(as_, ev):
    tm = as_("meera.tm")
    r = up(tm, f"/test-readiness-items/{ev['item']['id']}/evidence/upload", data=PDF + b"%backup report", name="backup-report.pdf").json()
    tm.patch(f"/test-readiness-items/{ev['item']['id']}", expect=200, json={"status": "Ready"})
    gate = tm.get(f"/test-events/{ev['ev']['id']}/readiness", expect=200).json()["gate"]
    assert any("evidence not yet accepted" in u for u in gate["unmet"])
    as_("rohan.risk").post(f"/test-evidence/{r['id']}/review", expect=200, json={"accept": True})
    gate = tm.get(f"/test-events/{ev['ev']['id']}/readiness", expect=200).json()["gate"]
    assert not any("Backups verified" in u for u in gate["unmet"])


def test_register_and_vendor_visibility(as_, ev):
    vendor = as_("ext.vendor")
    mine = up(vendor, f"/test-activities/{ev['v']['id']}/evidence/upload", name="vendor.png").json()
    reg = as_("meera.tm").get(f"/test-events/{ev['ev']['id']}/evidence", expect=200).json()
    assert {"snapshot.pdf", "vendor.png", "backup-report.pdf"} <= {x["filename"] for x in reg}
    assert all("can_review" in x for x in reg)
    vreg = vendor.get(f"/test-events/{ev['ev']['id']}/evidence", expect=200).json()
    assert [x["id"] for x in vreg] == [mine["id"]]
    other = next(x for x in reg if x["filename"] == "snapshot.pdf")
    vendor.get(f"/test-evidence/{other['id']}/download", expect=403)
    vendor.get(f"/test-evidence/{mine['id']}/download", expect=200)
