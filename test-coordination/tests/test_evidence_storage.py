"""Secure evidence storage: encrypted S3-compatible object storage with Object Lock retention and legal holds,
and ClamAV malware scanning (clean, infected, scanner unavailable then rescanned by the worker).

S3 is moto's in-process stand-in (with Object Lock); clamd is a small server speaking the real INSTREAM protocol.
"""
from __future__ import annotations

import hashlib
import socketserver
import struct
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import text

from app import config
from app.db import worker_engine
from app.services import evidence, storage

BUCKET = "bank-evidence"
PDF = b"%PDF-1.4\n% restore log\n" + b"1" * 4096


class FakeClamd(socketserver.BaseRequestHandler):
    """Reads an INSTREAM request and answers like clamd: OK, or the EICAR signature FOUND."""
    def handle(self):
        assert self.request.recv(10) == b"zINSTREAM\0"
        data = b""
        while True:
            size = struct.unpack(">I", self._exact(4))[0]
            if size == 0:
                break
            data += self._exact(size)
        self.server.scanned.append(len(data))
        reply = b"stream: Eicar-Test-Signature FOUND\0" if storage.EICAR in data else b"stream: OK\0"
        self.request.sendall(reply)

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            buf += self.request.recv(n - len(buf))
        return buf


@pytest.fixture(scope="module")
def clamd():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeClamd)
    srv.scanned = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def s3(monkeypatch, clamd):
    with mock_aws():
        c = boto3.client("s3", region_name="ap-south-1")
        c.create_bucket(Bucket=BUCKET, ObjectLockEnabledForBucket=True,
                        CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        storage._s3_client.cache_clear()
        monkeypatch.setattr(config, "settings", replace(
            config.settings, evidence_store="s3", evidence_s3_bucket=BUCKET, evidence_s3_endpoint="",
            evidence_s3_region="ap-south-1", evidence_s3_sse="AES256", evidence_retention_days=30,
            evidence_retention_mode="COMPLIANCE", scanner="clamav", clamd_host="127.0.0.1",
            clamd_port=clamd.server_address[1], clamd_timeout_s=5))
        yield c
        storage._s3_client.cache_clear()


@pytest.fixture
def ev(as_, seeded):
    ids = seeded["A"]
    pm, tm = as_("arjun.pm"), as_("meera.tm")
    prog = pm.post("/test-programmes", expect=201, json={
        "name": "Storage programme", "programme_code": f"S{uuid.uuid4().hex[:6].upper()}", "financial_year": "FY2028",
        "start_date": "2027-04-01", "end_date": "2028-03-31"}).json()
    start = datetime(2027, 11, 1, 4, 0, tzinfo=timezone.utc)
    e = pm.post("/test-events", expect=201, json={
        "programme_id": prog["id"], "title": "Storage test", "category": "Database", "criticality": "High",
        "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=2)).isoformat(),
        "test_manager_user_id": str(ids["meera.tm"])}).json()
    tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "submit"})
    pm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "approve_plan"})
    st = tm.post(f"/test-events/{e['id']}/stakeholders", expect=201, json={"user_id": str(ids["rahul.dba"]), "event_role": "DBA"}).json()[0]
    a = tm.post(f"/test-events/{e['id']}/activities", expect=201, json={
        "activity_code": f"DB-{uuid.uuid4().hex[:4]}", "title": "Restore", "stakeholder_id": st["id"], "evidence_required": True}).json()
    return {"ev": e, "a": a, "tenant": seeded["A"]["tenant"]}


def up(client, ev, data=PDF, name="restore.pdf", expect=201):
    return client.post(f"/test-activities/{ev['a']['id']}/evidence/upload", expect=expect,
                       files={"file": (name, data, "application/pdf")}, data={"evidence_type": "Log"})


def test_upload_is_encrypted_retention_locked_and_scanned_clean(as_, ev, s3, clamd):
    r = up(as_("rahul.dba"), ev).json()
    assert r["storage_backend"] == "s3" and r["scan_status"] == "Clean" and r["scan_engine"] == "clamav"
    assert r["encryption"] == "AES256" and r["retain_until"]
    head = s3.head_object(Bucket=BUCKET, Key=r["storage_reference"], ChecksumMode="ENABLED")
    assert head["ServerSideEncryption"] == "AES256"
    assert head["ObjectLockMode"] == "COMPLIANCE"
    assert head["ObjectLockRetainUntilDate"] > datetime.now(timezone.utc) + timedelta(days=29)
    assert r["storage_reference"].startswith(f"evidence/{ev['tenant']}/{ev['ev']['id']}/")
    d = as_("meera.tm").get(f"/test-evidence/{r['id']}/download", expect=200)
    assert d.content == PDF and d.headers["x-evidence-sha256"] == hashlib.sha256(PDF).hexdigest()


def test_retention_lock_stops_deletion_and_overwrite(as_, ev, s3):
    r = up(as_("rahul.dba"), ev).json()
    version = s3.head_object(Bucket=BUCKET, Key=r["storage_reference"])["VersionId"]
    with pytest.raises(Exception) as e:
        s3.delete_object(Bucket=BUCKET, Key=r["storage_reference"], VersionId=version)
    assert "AccessDenied" in str(e.value) or "locked" in str(e.value).lower()


def test_a_refused_upload_stores_nothing(as_, seeded, s3):
    """Rules are checked before the write: a retention-locked object couldn't be removed afterwards."""
    pm = as_("arjun.pm")
    prog = pm.post("/test-programmes", expect=201, json={"name": "Draft only", "programme_code": f"D{uuid.uuid4().hex[:6].upper()}",
                   "financial_year": "FY2028", "start_date": "2027-04-01", "end_date": "2028-03-31"}).json()
    start = datetime(2027, 12, 1, 4, 0, tzinfo=timezone.utc)
    e = pm.post("/test-events", expect=201, json={"programme_id": prog["id"], "title": "Still a draft", "category": "Database",
                "criticality": "Low", "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=1)).isoformat(),
                "test_manager_user_id": str(seeded["A"]["meera.tm"])}).json()
    a = as_("meera.tm").post(f"/test-events/{e['id']}/activities", expect=201, json={"activity_code": "X-1", "title": "x"}).json()
    r = as_("meera.tm").post(f"/test-activities/{a['id']}/evidence/upload", files={"file": ("a.pdf", PDF, "application/pdf")},
                             data={"evidence_type": "Log"})
    assert r.status_code == 409 and "Draft" in r.json()["error"]
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


def test_infected_upload_is_refused_and_not_stored(as_, ev, s3):
    before = s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0)
    r = up(as_("rahul.dba"), ev, data=storage.EICAR + b"\n", name="dropper.txt", expect=422).json()
    assert "Eicar-Test-Signature" in r["error"] and "not stored" in r["error"]
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == before


def test_scanner_down_holds_the_file_until_the_worker_scans_it(as_, ev, s3, monkeypatch, clamd):
    monkeypatch.setattr(config, "settings", replace(config.settings, clamd_port=1))     # nothing listens there
    r = up(as_("rahul.dba"), ev).json()
    assert r["scan_status"] == "Pending" and "unreachable" in r["scan_detail"]
    tm = as_("meera.tm")
    assert "waiting for its malware scan" in tm.get(f"/test-evidence/{r['id']}/download").json()["error"]
    reg = {x["id"]: x for x in tm.get(f"/test-events/{ev['ev']['id']}/evidence", expect=200).json()}
    assert reg[r["id"]]["can_review"] is False
    assert "waiting for its malware scan" in tm.post(f"/test-evidence/{r['id']}/review", json={"accept": True}).json()["error"]

    assert evidence.rescan_pending(ev["tenant"])["still_pending"] >= 1                   # still down: stays held
    monkeypatch.setattr(config, "settings", replace(config.settings, clamd_port=clamd.server_address[1]))
    assert evidence.rescan_pending(ev["tenant"])["clean"] >= 1
    assert tm.get(f"/test-evidence/{r['id']}/download", expect=200).content == PDF
    tm.post(f"/test-evidence/{r['id']}/review", expect=200, json={"accept": True})


def test_file_found_infected_later_is_quarantined_and_reported(as_, ev, s3, monkeypatch, clamd):
    monkeypatch.setattr(config, "settings", replace(config.settings, clamd_port=1))
    bad = b"%PDF-1.4\n" + storage.EICAR
    r = up(as_("rahul.dba"), ev, data=bad, name="report.pdf").json()
    assert r["scan_status"] == "Pending"
    monkeypatch.setattr(config, "settings", replace(config.settings, clamd_port=clamd.server_address[1]))
    assert evidence.rescan_pending(ev["tenant"])["infected"] == 1
    tm = as_("meera.tm")
    blocked = tm.get(f"/test-evidence/{r['id']}/download")
    assert blocked.status_code == 409 and "quarantined" in blocked.json()["error"]
    tags = {t["Key"]: t["Value"] for t in s3.get_object_tagging(Bucket=BUCKET, Key=r["storage_reference"])["TagSet"]}
    assert tags["resilienceai-quarantine"] == "infected" and "Eicar" in tags["resilienceai-signature"]
    with worker_engine().connect() as c:
        notice = c.execute(text("SELECT subject FROM test_communications WHERE dedupe_key = :k"),
                           {"k": f"critical_issue:quarantine:{r['id']}"}).scalar()
        actions = [x for (x,) in c.execute(text("SELECT action FROM test_event_audit WHERE entity_id = :e ORDER BY seq"),
                                           {"e": r["id"]})]
    assert "quarantined by the malware scan" in notice
    assert "evidence.quarantined" in actions and "evidence.download_blocked" in actions


def test_legal_hold(as_, ev, s3):
    r = up(as_("rahul.dba"), ev).json()
    path = f"/test-evidence/{r['id']}/legal-hold"
    assert as_("meera.tm").post(path, json={"on": True, "reason": "x"}).status_code == 403       # test manager can't
    assert "Give the reason" in as_("rohan.risk").post(path, json={"on": True}).json()["error"]
    h = as_("rohan.risk").post(path, expect=200, json={"on": True, "reason": "Regulator request RBI/2027/114"}).json()
    assert h["legal_hold"] is True and h["legal_hold_reason"].startswith("Regulator request")
    assert s3.get_object_legal_hold(Bucket=BUCKET, Key=r["storage_reference"])["LegalHold"]["Status"] == "ON"
    reg = {x["id"]: x for x in as_("rohan.risk").get(f"/test-events/{ev['ev']['id']}/evidence", expect=200).json()}
    assert reg[r["id"]]["can_legal_hold"] is True and reg[r["id"]]["legal_hold"] is True
    assert as_("rohan.risk").post(path, json={"on": True, "reason": "again"}).status_code == 409
    as_("audit.ia").post(path, expect=200, json={"on": False})
    assert s3.get_object_legal_hold(Bucket=BUCKET, Key=r["storage_reference"])["LegalHold"]["Status"] == "OFF"


def test_tampered_object_is_not_served(as_, ev, s3, monkeypatch):
    monkeypatch.setattr(config, "settings", replace(config.settings, evidence_retention_days=0))  # unlocked, to tamper
    r = up(as_("rahul.dba"), ev).json()
    s3.put_object(Bucket=BUCKET, Key=r["storage_reference"], Body=PDF + b"edited")
    d = as_("meera.tm").get(f"/test-evidence/{r['id']}/download")
    assert d.status_code == 409 and "Integrity check failed" in d.json()["error"]


def test_bucket_checks_for_readiness(s3, monkeypatch):
    st = storage.store()
    assert st.check() == []
    s3.create_bucket(Bucket="no-lock", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
    monkeypatch.setattr(config, "settings", replace(config.settings, evidence_s3_bucket="no-lock"))
    assert storage.store().check() == ["Object Lock is not enabled on the evidence bucket"]
    monkeypatch.setattr(config, "settings", replace(config.settings, evidence_s3_bucket="missing"))
    assert "not reachable" in storage.store().check()[0]


def test_production_needs_storage_and_scanner():
    s = replace(config.settings, app_env="production", evidence_store="s3", evidence_s3_bucket="",
                evidence_retention_days=0, scanner="builtin")
    problems = " | ".join(config.check_production(s))
    assert "EVIDENCE_S3_BUCKET" in problems and "EVIDENCE_RETENTION_DAYS" in problems and "SCANNER must be clamav" in problems
