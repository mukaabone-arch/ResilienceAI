"""Evidence pre-checks: rule-based checks, the optional model pass, and what reviewers do with them."""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import config
from app.ai import evidence_checks as chk
from app.ai import prechecks
from app.ai.evidence_text import extract
from app.services import storage

IST = ZoneInfo("Asia/Kolkata")
CARD = "4111 1111 1111 1111"
AADHAAR = "2345 6789 0124"          # passes the Verhoeff check


# ---------------------------------------------------------------------------
# The checks on their own
# ---------------------------------------------------------------------------
def _lines(text: str) -> list[str]:
    return text.strip("\n").split("\n")


def test_dates_old_future_ok_and_missing():
    up = datetime(2026, 11, 27, 4, 0, tzinfo=IST)
    lo, hi = up - timedelta(days=14), up + timedelta(minutes=10)
    old = _lines("backup finished 27-Oct-2026 02:10:00")
    f = chk.check_dates(old, chk.all_stamps(old, IST, 2026), lo, hi, IST)
    assert f["level"] == "warn" and "27 Oct 2026" in f["title"] and f["cites"][0]["line"] == 1
    fut = _lines("2026-11-27 02:00:00 start\n2026-12-03 09:00:00 done")
    assert chk.check_dates(fut, chk.all_stamps(fut, IST, 2026), lo, hi, IST)["level"] == "warn"
    nxt = _lines("2026-11-27 02:00:00 start\nnext backup scheduled 2026-12-03 09:00:00")
    assert chk.check_dates(nxt, chk.all_stamps(nxt, IST, 2026), lo, hi, IST)["level"] == "ok"     # forward-looking line
    ok = _lines("Fri Nov 27 01:58:02 IST 2026 start\n27/11/2026 02:30 done")
    f = chk.check_dates(ok, chk.all_stamps(ok, IST, 2026), lo, hi, IST)
    assert f["level"] == "ok" and "01:58 to 02:30" in f["title"]
    assert chk.check_dates(["no dates"], [], lo, hi, IST)["level"] == "info"


def test_recovery_point_and_time_against_targets():
    log = _lines("""
2026-11-27 01:30:05 INFO last applied log sequence 88421 (recovery point)
2026-11-27 02:10:11 WARN primary down: failover initiated
2026-11-27 02:31:47 INFO failover completed, database opened read write
2026-11-27 02:33:00 INFO RPO target: 15 min""")
    st = chk.all_stamps(log, IST, 2026)
    rpo, rto = chk.check_recovery(log, st, 15 * 60, 30 * 60, True, True)
    assert rpo["level"] == "warn" and "40 min" in rpo["title"] and [c["line"] for c in rpo["cites"]] == [1, 2]
    assert rto["level"] == "ok" and "22 min" in rto["title"]
    lag = _lines("Apply Lag: +00 00:00:12\nRPO target: 15 min")       # explicit figure; the target line is ignored
    assert chk.check_recovery(lag, [], 900, None, True, False)[0]["title"].startswith("Recovery point about 12 s")
    none = chk.check_recovery(["nothing here"], [], 900, None, True, False)
    assert none[0]["level"] == "info" and "No recovery point figure" in none[0]["title"]
    assert chk.check_recovery(lag, [], None, None, True, False)[0]["level"] == "info"          # no target set


def test_environment():
    uat = _lines("connected to uat-cbs-db02.bank.local\nprimary cbs-prd-db01")
    f = chk.check_environment(uat, expects_nonprod=False)
    assert f["level"] == "warn" and "UAT" in f["title"] and "cbs-prd-db01" in f["detail"]
    assert chk.check_environment(uat, expects_nonprod=True) is None
    assert chk.check_environment(["host cbs-prd-db01 and drc-app02"], False)["level"] == "ok"
    assert chk.check_environment(["DR test plan v1.2 for test-plan.pdf"], False) is None        # words, not hosts
    assert chk.check_environment(["*** UAT ENVIRONMENT ***"], False)["level"] == "warn"


def test_sensitive_data_is_found_and_never_repeated():
    lines = [f"card {CARD} used", f"Aadhaar {AADHAAR}", "password=Welcome#123", "pwd: ****",
             "jdbc://sys:S3cret@db:1521", "PAN ABCPD1234E", "sequence 4111111111111111 applied"]
    f = chk.check_sensitive(lines)
    assert f["level"] == "warn"
    assert "1 card number" in f["title"] and "1 Aadhaar number" in f["title"] and "2 passwords or keys" in f["title"]
    dumped = json.dumps(f, ensure_ascii=False)
    for raw in (CARD, AADHAAR, "Welcome#123", "S3cret", "ABCPD1234E"):
        assert raw not in dumped
    assert "4111 •••• •••• 1111" in dumped and "XXXX XXXX 0124" in dumped
    assert chk.check_sensitive(["all clean", "pwd: ****"])["level"] == "ok"


def test_relevance_by_key_terms():
    terms = chk.key_terms("Prepare database recovery point", "Confirm standby apply lag for CBS")
    assert chk.check_relevance("standby database recovery point for CBS, apply lag 3s", terms, "activity")["level"] == "ok"
    assert chk.check_relevance("quarterly marketing newsletter", terms, "activity")["level"] == "warn"


def _zip(files: dict[str, str]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


def test_text_extraction_from_office_and_pdf_files():
    docx = _zip({"word/document.xml": "<w:document><w:p><w:r><w:t>Failover completed &amp; verified</w:t></w:r></w:p>"
                                      "<w:p><w:r><w:t>2026-11-27 02:31</w:t></w:r></w:p></w:document>"})
    assert extract("r.docx", docx).lines[:2] == ["Failover completed & verified", "2026-11-27 02:31"]
    xlsx = _zip({"xl/sharedStrings.xml": "<sst><si><t>Host</t></si><si><t>uat-db01</t></si></sst>",
                 "xl/worksheets/sheet1.xml": '<sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c><c><v>42</v></c></row></sheetData>'})
    assert extract("r.xlsx", xlsx).lines == ["Host\tuat-db01\t42"]
    from reportlab.pdfgen import canvas
    b = io.BytesIO()
    cv = canvas.Canvas(b)
    cv.drawString(72, 720, "Recovery point 2026-11-27 01:30:05")
    cv.save()
    assert "Recovery point" in extract("r.pdf", b.getvalue()).text
    assert extract("shot.png", b"\x89PNG").kind == "image"
    assert extract("broken.docx", b"not a zip").kind == "unreadable"


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def temp_store(tmp_path_factory):
    store = storage.LocalStore(str(tmp_path_factory.mktemp("prechecks")))
    original = storage.store
    storage.store = lambda: store
    yield store
    storage.store = original


@pytest.fixture(scope="module")
def ev(as_, seeded):
    ids = seeded["A"]
    pm, tm = as_("arjun.pm"), as_("meera.tm")
    prog = pm.post("/test-programmes", expect=201, json={
        "name": "Pre-check programme", "programme_code": f"PC{uuid.uuid4().hex[:4]}", "financial_year": "FY2028",
        "start_date": "2027-04-01", "end_date": "2028-03-31"}).json()
    start = datetime.now(timezone.utc) + timedelta(days=2)
    e = pm.post("/test-events", expect=201, json={
        "programme_id": prog["id"], "title": "CBS database failover", "category": "Database", "criticality": "High",
        "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=4)).isoformat(),
        "timezone": "Asia/Kolkata", "test_manager_user_id": str(ids["meera.tm"]), "rpo_target_minutes": 15,
        "rto_target_minutes": 60}).json()
    assert e["rpo_target_minutes"] == 15
    tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "submit"})
    pm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "approve_plan"})
    st = {s: tm.post(f"/test-events/{e['id']}/stakeholders", expect=201, json={"user_id": str(ids[s]), "event_role": "x"}).json()[0]
          for s in ("rahul.dba", "ext.vendor")}
    a = tm.post(f"/test-events/{e['id']}/activities", expect=201, json={
        "activity_code": "DB-1", "title": "Prepare database recovery point",
        "description": "Confirm the CBS standby apply lag before failover", "stakeholder_id": st["rahul.dba"]["id"],
        "evidence_required": True}).json()
    b = tm.post(f"/test-events/{e['id']}/activities", expect=201, json={
        "activity_code": "DB-2", "title": "Verify standby", "stakeholder_id": st["rahul.dba"]["id"]}).json()
    v = tm.post(f"/test-events/{e['id']}/activities", expect=201, json={
        "activity_code": "VN-1", "title": "Vendor network check", "stakeholder_id": st["ext.vendor"]["id"]}).json()
    return {"ev": e, "a": a, "b": b, "v": v, "ids": ids, "tenant": ids["tenant"]}


def up(client, activity_id, text: str, name="restore.log", expect=201):
    return client.post(f"/test-activities/{activity_id}/evidence/upload", expect=expect,
                       files={"file": (name, text.encode(), "text/plain")}, data={"evidence_type": "Log"})


def _stamp(days_ago: float = 0, minutes: int = 0) -> str:
    return (datetime.now(IST) - timedelta(days=days_ago) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def bad_log() -> str:
    return "\n".join([
        f"{_stamp(30)} INFO standby uat-cbs-db02 last applied log sequence 8841 (recovery point)",
        f"{_stamp(30, 40)} WARN primary down: failover initiated",
        f"{_stamp(30, 130)} INFO failover completed on uat-cbs-db02",
        f"{_stamp(30, 131)} smoke test used card {CARD}",
        f"run {uuid.uuid4()}",
    ])


def good_log() -> str:
    return "\n".join([
        f"{_stamp(0, -50)} INFO CBS standby cbs-drc-db01 apply lag check before failover",
        f"{_stamp(0, -45)} INFO last applied log sequence 9921 (recovery point) on cbs-drc-db01",
        f"{_stamp(0, -40)} WARN primary cbs-prd-db01 down: failover initiated",
        f"{_stamp(0, -20)} INFO failover completed, database opened read write",
        f"run {uuid.uuid4()}",
    ])


def by_key(pc: dict) -> dict:
    return {f["key"]: f for f in pc["findings"]}


def test_upload_returns_the_precheck_and_masks_what_it_finds(as_, ev):
    r = up(as_("rahul.dba"), ev["a"]["id"], bad_log()).json()
    pc = r["precheck"]
    f = by_key(pc)
    assert pc["status"] == "Proposed" and pc["summary"]["warn"] >= 3 and pc["headline"].startswith(f"{pc['summary']['warn']} things to check")
    assert f["dates"]["level"] == "warn" and f["environment"]["level"] == "warn" and f["sensitive"]["level"] == "warn"
    assert f["rpo"]["level"] == "warn" and "beyond the 15 min RPO target" in f["rpo"]["title"]
    assert f["rto"]["level"] == "warn"                                          # 90 min against 60
    assert CARD not in json.dumps(r) and pc["ai_status"] == "not_configured"
    assert pc["findings"][0]["level"] == "warn"                                  # warnings first
    reg = {x["id"]: x for x in as_("meera.tm").get(f"/test-events/{ev['ev']['id']}/evidence", expect=200).json()}
    assert reg[r["id"]]["precheck"]["id"] == pc["id"] and reg[r["id"]]["can_precheck"] is True


def test_accepting_despite_warnings_needs_a_note_and_is_logged(as_, ev):
    r = up(as_("rahul.dba"), ev["a"]["id"], bad_log()).json()
    risk = as_("rohan.risk")
    err = risk.post(f"/test-evidence/{r['id']}/review", expect=422, json={"accept": True}).json()["error"]
    assert "pre-check raised" in err and "Add a note" in err
    risk.post(f"/test-evidence/{r['id']}/review", expect=200, json={"accept": True, "comment": "Old run accepted by CRO memo 12"})
    log = as_("arjun.pm").get("/assistant/log?feature=evidence_precheck", expect=200).json()
    mine = next(x for x in log if x["headline"] == r["precheck"]["headline"] and x["status"] == "Dismissed")
    assert "Accepted despite" in mine["feedback"] and "CRO memo 12" in mine["feedback"]


def test_rejecting_flagged_evidence_counts_as_agreement(as_, ev):
    r = up(as_("rahul.dba"), ev["a"]["id"], bad_log()).json()
    as_("meera.tm").post(f"/test-evidence/{r['id']}/review", expect=200, json={"accept": False, "comment": "Wrong environment"})
    reg = {x["id"]: x for x in as_("meera.tm").get(f"/test-events/{ev['ev']['id']}/evidence", expect=200).json()}
    pc = reg[r["id"]]["precheck"]
    assert pc["status"] == "Accepted" and pc["final_content"]["agreed"] is True and pc["final_content"]["review"] == "Rejected"


def test_clean_file_has_no_warnings_and_accepts_without_a_note(as_, ev):
    r = up(as_("rahul.dba"), ev["a"]["id"], good_log()).json()
    f = by_key(r["precheck"])
    assert r["precheck"]["summary"]["warn"] == 0, r["precheck"]["findings"]
    assert f["rpo"]["level"] == "ok" and f["rto"]["level"] == "ok" and f["environment"]["level"] == "ok"
    assert f["relevance"]["level"] == "ok" and f["duplicate"]["level"] == "ok"
    as_("rohan.risk").post(f"/test-evidence/{r['id']}/review", expect=200, json={"accept": True})


def test_duplicates_and_what_vendors_see(as_, ev):
    text = good_log()
    first = up(as_("rahul.dba"), ev["a"]["id"], text).json()
    second = up(as_("rahul.dba"), ev["b"]["id"], text).json()
    dup = by_key(second["precheck"])["duplicate"]
    assert dup["level"] == "warn" and first["evidence_code"] in dup["title"] and "DB-1" in dup["title"]
    vend = up(as_("ext.vendor"), ev["v"]["id"], text, name="net.log").json()
    vdup = by_key(vend["precheck"])["duplicate"]
    assert vdup["title"] == "Same file already submitted for another item" and vdup["cites"] == []
    assert vend["precheck"]["fallback_reason"] is None
    staff = {x["id"]: x for x in as_("meera.tm").get(f"/test-events/{ev['ev']['id']}/evidence", expect=200).json()}
    assert first["evidence_code"] in by_key(staff[vend["id"]]["precheck"])["duplicate"]["title"]


def test_marking_a_finding_wrong_lifts_the_note_requirement(as_, ev):
    log = good_log().replace("cbs-drc-db01", "uat-cbs-db09")
    r = up(as_("rahul.dba"), ev["a"]["id"], log).json()
    assert prechecks.warnings_of({"content": r["precheck"], "final_content": None}) == [by_key(r["precheck"])["environment"]["title"]]
    rahul, risk = as_("rahul.dba"), as_("rohan.risk")
    rahul.post(f"/test-evidence/{r['id']}/precheck/feedback", expect=422, json={"key": "environment", "verdict": "wrong"})
    as_("ext.vendor").post(f"/test-evidence/{r['id']}/precheck/feedback", expect=403,
                           json={"key": "environment", "verdict": "wrong", "note": "x"})
    pc = rahul.post(f"/test-evidence/{r['id']}/precheck/feedback", expect=200,
                    json={"key": "environment", "verdict": "wrong", "note": "uat-cbs-db09 is the DR host's legacy name"}).json()
    assert pc["final_content"]["finding_feedback"]["environment"]["by"]
    risk.post(f"/test-evidence/{r['id']}/review", expect=200, json={"accept": True})
    rahul.post(f"/test-evidence/{r['id']}/precheck/feedback", expect=409, json={"key": "dates", "verdict": "right"})


def test_rerun_uses_the_latest_targets(as_, ev):
    r = up(as_("rahul.dba"), ev["a"]["id"], bad_log()).json()
    tm = as_("meera.tm")
    e = tm.get(f"/test-events/{ev['ev']['id']}", expect=200).json()
    tm.patch(f"/test-events/{ev['ev']['id']}", expect=200, json={"expected_version": e["version"], "rpo_target_minutes": 60})
    again = tm.post(f"/test-evidence/{r['id']}/precheck", expect=200).json()
    assert by_key(again)["rpo"]["level"] == "ok" and again["id"] != r["precheck"]["id"]
    reg = {x["id"]: x for x in tm.get(f"/test-events/{ev['ev']['id']}/evidence", expect=200).json()}
    assert reg[r["id"]]["precheck"]["id"] == again["id"]                       # the old one is superseded
    as_("ext.vendor").post(f"/test-evidence/{r['id']}/precheck", expect=403)
    tm.patch(f"/test-events/{ev['ev']['id']}", expect=200, json={"expected_version": e["version"] + 1, "rpo_target_minutes": 15})


def test_images_get_a_note_not_a_guess(as_, ev):
    r = as_("rahul.dba").post(f"/test-activities/{ev['a']['id']}/evidence/upload", expect=201,
                              files={"file": ("screen.png", b"\x89PNG" + uuid.uuid4().bytes, "image/png")},
                              data={"evidence_type": "Screenshot"}).json()
    f = by_key(r["precheck"])
    assert f["readable"]["level"] == "info" and "by eye" in f["readable"]["title"]
    assert r["precheck"]["ai_status"] == "not_applicable"


# ---------------------------------------------------------------------------
# The optional model pass (a fake transport stands in for the API)
# ---------------------------------------------------------------------------
def _fake(reply, seen: list | None = None, status: int = 200):
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        text = reply(body) if callable(reply) else reply
        return httpx.Response(status, json={"content": [{"type": "text", "text": text}]})
    return httpx.MockTransport(handler)


@pytest.fixture
def with_model(monkeypatch):
    monkeypatch.setattr(config, "settings", replace(config.settings, ai_provider="anthropic", anthropic_api_key="test-key"))
    yield
    prechecks.TRANSPORT = None


def _line_of(body: dict, needle: str) -> tuple[int, str]:
    for ln in body["messages"][0]["content"].split("\n"):
        if ln.startswith("L") and needle in ln:
            n, txt = ln.split(": ", 1)
            return int(n[1:]), txt
    raise AssertionError(needle)


def test_model_judges_relevance_from_a_masked_name_free_excerpt(as_, ev, with_model):
    seen: list = []

    def reply(body):
        n, txt = _line_of(body, "recovery point")
        return json.dumps({"relevance": "partly", "reason": "Shows the recovery point but not the apply lag check.",
                           "shows": ["Recovery point on the standby"], "missing": ["Apply lag figure for CBS"],
                           "citations": [{"line": n, "quote": "last applied log sequence"}]})
    prechecks.TRANSPORT = _fake(reply, seen)
    log = bad_log() + "\nchecked by Rahul Das"
    r = up(as_("rahul.dba"), ev["a"]["id"], log).json()
    assert r["precheck"]["ai_status"] == "queued" and seen == []               # the upload doesn't wait for the model
    assert prechecks.run_model_pending(ev["tenant"]) >= 1
    sent = seen[-1]["messages"][0]["content"]
    assert CARD not in sent and "Rahul Das" not in sent and "Person " in sent and "4111 •••• •••• 1111" in sent
    pc = as_("meera.tm").get(f"/test-events/{ev['ev']['id']}/evidence", expect=200).json()
    pc = next(x for x in pc if x["id"] == r["id"])["precheck"]
    rel = by_key(pc)["relevance"]
    assert pc["provider"] == "anthropic" and pc["ai_status"] == "done" and rel["by"] == "model" and rel["level"] == "info"
    assert rel["title"] == "Only partly covers the activity" and rel["cites"][0]["text"].endswith("(recovery point)")
    assert pc["briefing"]["missing"] == ["Apply lag figure for CBS"]


@pytest.mark.parametrize("reply,why", [
    (json.dumps({"relevance": "relevant", "reason": "ok", "citations": [{"line": 2, "quote": "words not in the file"}]}),
     "cited no lines"),
    (json.dumps({"relevance": "relevant", "reason": "Matches DB-77.", "citations": []}), "cited no lines"),
    (json.dumps({"relevance": "not_relevant", "reason": "Reviewer should reject this file."}), "recommend a review decision"),
    (json.dumps({"relevance": "not_relevant", "reason": "Belongs to DB-77, not this activity."}), "records not in the file"),
    ("Sure! Here is my view", "valid JSON"),
])
def test_model_answers_that_fail_a_check_are_not_used(as_, ev, with_model, reply, why):
    prechecks.TRANSPORT = _fake(reply)
    r = up(as_("rahul.dba"), ev["a"]["id"], good_log()).json()
    pc = as_("meera.tm").post(f"/test-evidence/{r['id']}/precheck", expect=200).json()
    assert pc["provider"] == "rules" and pc["ai_status"] == "fallback" and why in pc["fallback_reason"]
    assert by_key(pc)["relevance"].get("by") is None                           # the rule-based judgement stands


def test_model_outage_leaves_the_rules_result(as_, ev, with_model):
    prechecks.TRANSPORT = _fake("", status=529)
    r = up(as_("rahul.dba"), ev["a"]["id"], good_log()).json()
    pc = as_("meera.tm").post(f"/test-evidence/{r['id']}/precheck", expect=200).json()
    assert pc["fallback_reason"] == "model call failed (529)" and pc["summary"]["warn"] == 0


def test_precheck_failures_never_block_an_upload(as_, ev, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(prechecks, "rule_findings", boom)
    r = up(as_("rahul.dba"), ev["a"]["id"], good_log()).json()
    assert r["precheck"] is None and r["evidence_code"]
