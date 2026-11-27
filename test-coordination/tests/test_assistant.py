"""Assistant: grounded suggestions, permissions, sending drafts, oversight, schedules and model safeguards (RA-069..072)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app.ai import provider as prov
from app.ai import service as assistant
from app.db import worker_engine
from scripts.seed import CORE_BANKING_TEMPLATE


@pytest.fixture(scope="module")
def ev(as_, seeded):
    ids = seeded["A"]
    pm, tm = as_("arjun.pm"), as_("meera.tm")
    prog = pm.post("/test-programmes", expect=201, json={
        "name": "Assistant programme", "programme_code": "AIP", "financial_year": "FY2027",
        "start_date": "2026-04-01", "end_date": "2027-03-31"}).json()
    tpl = pm.post("/test-templates", expect=201, json={"template_key": "ai-cbs", "name": "CBS for assistant", "category": "Application",
                                                       "default_duration": 360, "definition": CORE_BANKING_TEMPLATE}).json()
    start = datetime.now(timezone.utc) + timedelta(hours=30)
    e = pm.post("/test-events", expect=201, json={
        "programme_id": prog["id"], "template_id": tpl["id"], "title": "Assistant drill", "category": "Application",
        "criticality": "High", "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=6)).isoformat(),
        "test_manager_user_id": str(ids["meera.tm"]), "owner_user_id": str(ids["vikram.owner"])}).json()
    eid = e["id"]
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "submit"})
    pm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "approve_plan"})
    st = {s: tm.post(f"/test-events/{eid}/stakeholders", expect=201, json={"user_id": str(ids[s]), "event_role": r}).json()[0]
          for s, r in (("rahul.dba", "DBA"), ("kiran.net", "Network"), ("ext.vendor", "Vendor"))}
    as_("rahul.dba").post(f"/test-stakeholders/{st['rahul.dba']['id']}/respond", expect=200, json={"accept": True})
    acts = {a["activity_code"]: a for a in tm.get(f"/test-events/{eid}/activities", expect=200).json()}
    for code, sub in (("INF-01", "kiran.net"), ("DB-01", "rahul.dba"), ("NET-01", "kiran.net")):
        tm.patch(f"/test-activities/{acts[code]['id']}", expect=200, json={"stakeholder_id": st[sub]["id"]})
    tm.post(f"/test-events/{eid}/transitions", expect=200, json={"action": "begin_readiness"})
    for status in ("In Progress", "Submitted"):                       # DB-01 depends on INF-01
        as_("kiran.net").patch(f"/test-activities/{acts['INF-01']['id']}", expect=200, json={"status": status})
    as_("rahul.dba").patch(f"/test-activities/{acts['DB-01']['id']}", expect=200, json={"status": "In Progress"})
    as_("rahul.dba").patch(f"/test-activities/{acts['DB-01']['id']}", expect=200,
                           json={"status": "Blocked", "blocked_reason": "Snapshot older than RPO"})
    items = {i["title"]: i for i in tm.get(f"/test-events/{eid}/readiness", expect=200).json()["items"]}
    tm.patch(f"/test-readiness-items/{items['Database recovery ready']['id']}", expect=200,
             json={"status": "Blocked", "owner_user_id": str(ids["rahul.dba"])})
    return {"id": eid, "code": e["event_code"], "ids": ids, "acts": acts}


def gen(client, ev, feature, kind=None, expect=201):
    return client.post(f"/test-events/{ev['id']}/assistant", expect=expect, json={"feature": feature, **({"kind": kind} if kind else {})})


def test_briefing_is_grounded_and_actionable(as_, ev):
    r = gen(as_("meera.tm"), ev, "blocker_briefing").json()
    c = r["content"]
    assert r["status"] == "Proposed" and r["provider"] == "rules" and r["prompt_version"] == "assistant-v1"
    assert "mandatory condition" in c["headline"]
    assert any("DB-01" in h and "Snapshot older than RPO" in h for h in c["holding"])
    assert c["steps"][0].startswith("Rahul Das to clear the blocker on DB-01")      # biggest impact first
    assert "holds up 3 other activities" in c["steps"][0]                            # APP-01, then TV-01 and BV-01
    labels = {(s["type"], s["label"]) for s in r["sources"]}
    assert ("activity", "DB-01") in labels and ("readiness_item", "Database recovery ready") in labels
    again = gen(as_("meera.tm"), ev, "blocker_briefing").json()
    hist = as_("meera.tm").get(f"/test-events/{ev['id']}/assistant", expect=200).json()["history"]
    assert {h["id"]: h["status"] for h in hist}[r["id"]] == "Superseded" and again["status"] == "Proposed"


def test_forecast_explains_its_date(as_, ev):
    c = gen(as_("rahul.dba"), ev, "slip_forecast").json()["content"]       # any internal participant may ask
    assert c["verdict"] in ("at_risk", "likely_slip")
    assert any(d["ref"] in ("DB-01", "APP-01", "Database recovery ready") for d in c["drivers"])
    assert "rule-based estimate" in c["text"] and c["rules"]


def test_permissions(as_, ev):
    gen(as_("ext.vendor"), ev, "blocker_briefing", expect=403)
    gen(as_("rahul.dba"), ev, "message_draft", "reminder", expect=403)
    gen(as_("kiran.net", tenant="B"), ev, "blocker_briefing", expect=404)      # another bank can't even see it exists
    gen(as_("arjun.pm", tenant="B"), ev, "blocker_briefing", expect=404)
    gen(as_("meera.tm"), ev, "message_draft", "go_request", expect=409)          # not Ready yet
    as_("meera.tm").post(f"/test-events/{ev['id']}/assistant", expect=422, json={"feature": "message_draft"})


def test_reminder_draft_sends_as_the_manager(as_, ev):
    tm = as_("meera.tm")
    d = gen(tm, ev, "message_draft", "reminder").json()
    rec = {r["name"]: r for r in d["content"]["recipients"]}
    assert "Rahul Das" in rec and any("DB-01" in x for x in rec["Rahul Das"]["lines"])
    assert any("acknowledge" in x for r in rec.values() for x in r["lines"])          # Kiran and the vendor haven't
    tm.post(f"/assistant/suggestions/{d['id']}/decision", expect=422, json={"action": "dismiss"})   # needs a reason
    kiran = rec["Kiran Shetty"]["user_id"]
    out = tm.post(f"/assistant/suggestions/{d['id']}/decision", expect=200, json={
        "action": "accept", "subject": "Please update your items today", "exclude_recipients": [kiran]}).json()
    assert out["status"] == "Edited" and out["sent"] == len(rec) - 1
    tm.post(f"/assistant/suggestions/{d['id']}/decision", expect=409, json={"action": "accept"})
    with worker_engine().connect() as c:
        rows = c.execute(text("SELECT subject, created_by FROM test_communications WHERE dedupe_key LIKE :k"),
                         {"k": f"ack_reminder:ai:{d['id']}:%"}).all()
    assert len(rows) == len(rec) - 1 and {r.subject for r in rows} == {"Please update your items today"}
    assert {str(r.created_by) for r in rows} == {str(ev["ids"]["meera.tm"])}          # sent as the person who pressed Send
    tl = tm.get(f"/test-events/{ev['id']}/timeline", expect=200).json()
    assert any("drafted by the assistant, edited" in (x.get("message") or "") for x in tl)


def test_dismiss_and_oversight_log(as_, ev):
    s = gen(as_("meera.tm"), ev, "slip_forecast").json()
    as_("meera.tm").post(f"/assistant/suggestions/{s['id']}/decision", expect=200,
                         json={"action": "dismiss", "feedback": "Vendor already confirmed a fix for tonight"})
    log = as_("audit.ia").get("/assistant/log", expect=200).json()
    mine = next(x for x in log if x["id"] == s["id"])
    assert mine["status"] == "Dismissed" and mine["decided_by"] == "Meera Iyer" and mine["event_code"] == ev["code"]
    assert mine["feedback"].startswith("Vendor already")
    as_("rahul.dba").get("/assistant/log", expect=403)
    with worker_engine().connect() as c:
        acts = {r[0] for r in c.execute(text("SELECT DISTINCT action FROM test_event_audit WHERE action LIKE 'ai.%'"))}
    assert {"ai.suggested", "ai.dismissed", "ai.edited"} <= acts


def test_live_summaries_scheduled_once_per_slot(as_, ev, seeded):
    pm = as_("arjun.pm")
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    e = pm.post("/test-events", expect=201, json={
        "programme_id": as_("arjun.pm").get(f"/test-events/{ev['id']}", expect=200).json()["programme_id"],
        "title": "Live summary drill", "category": "Database", "criticality": "Medium",
        "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=4)).isoformat(),
        "test_manager_user_id": str(ev["ids"]["meera.tm"])}).json()
    tm = as_("meera.tm")
    tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "submit"})
    pm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "approve_plan"})
    tm.post(f"/test-events/{e['id']}/stakeholders", expect=201, json={"user_id": str(ev["ids"]["rahul.dba"]), "event_role": "DBA"})
    tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "begin_readiness"})
    it = tm.post(f"/test-events/{e['id']}/readiness-items", expect=201, json={"title": "Backups verified"}).json()
    tm.patch(f"/test-readiness-items/{it['id']}", expect=200, json={"status": "Ready"})
    tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "mark_ready"})
    pm.post(f"/test-events/{e['id']}/approvals", expect=201, json={"approval_type": "Go/No-go", "decision": "Approved"})
    tm.post(f"/test-events/{e['id']}/start", expect=200)
    at = datetime.now(timezone.utc)
    first = assistant.run_live_summaries(seeded["A"]["tenant"], at)
    assert first >= 1 and assistant.run_live_summaries(seeded["A"]["tenant"], at) == 0
    latest = tm.get(f"/test-events/{e['id']}/assistant", expect=200).json()["latest"]["status_summary"]
    assert latest["requested_by"] is None and "is live" in latest["content"]["text"] and latest["slot_key"]


# ---- the optional Claude provider (no real calls: a fake transport stands in) ----
DRAFT = "Rahul Das to clear the blocker on DB-01. Meera Iyer to review evidence for BANK-DR-2027-004."


def _fake(reply: str | None = None, status: int = 200, seen: list | None = None, boom: bool = False):
    def handler(request: httpx.Request):
        if boom:
            raise httpx.ConnectTimeout("timed out")
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, json={"content": [{"type": "text", "text": reply or ""}]})
    return httpx.MockTransport(handler)


def test_claude_provider_hides_names_and_restores_them():
    seen: list = []
    p = prov.AnthropicProvider(transport=_fake("Person A should clear the DB-01 blocker; Person B should review the evidence for BANK-DR-2027-004.", seen=seen))
    r = p.rewrite("blocker_briefing", DRAFT, ["Rahul Das", "Meera Iyer"])
    sent = seen[0]["messages"][0]["content"]
    assert "Rahul" not in sent and "Meera" not in sent and "Person A" in sent and "temperature" not in seen[0]
    assert r.provider == "anthropic" and r.fallback_reason is None
    assert "Rahul Das should clear the DB-01 blocker" in r.text or "Meera Iyer should clear" in r.text


def test_claude_provider_falls_back_safely():
    bad = prov.AnthropicProvider(transport=_fake("Person A should clear DB-01 and APP-99 first.")).rewrite("x", DRAFT, ["Rahul Das", "Meera Iyer"])
    assert bad.text == DRAFT and bad.provider == "rules" and "APP-99" in bad.fallback_reason
    stranger = prov.AnthropicProvider(transport=_fake("Person C owns DB-01.")).rewrite("x", DRAFT, ["Rahul Das", "Meera Iyer"])
    assert stranger.text == DRAFT and "people not in the facts" in stranger.fallback_reason
    down = prov.AnthropicProvider(transport=_fake(boom=True)).rewrite("x", DRAFT, ["Rahul Das"])
    assert down.text == DRAFT and "ConnectTimeout" in down.fallback_reason
    err = prov.AnthropicProvider(transport=_fake("x", status=529)).rewrite("x", DRAFT, ["Rahul Das"])
    assert err.text == DRAFT and "529" in err.fallback_reason
