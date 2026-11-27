"""Cross-test learning: lessons from related earlier tests, and what planners do with them."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import worker_engine
from app.services import lessons, storage

TEMPLATE = {
    "activities": [
        {"code": "DB-01", "title": "Prepare database recovery point", "due_offset_hours": -24, "evidence_required": True},
        {"code": "APP-01", "title": "Application recovery runbook walkthrough", "due_offset_hours": -12},
    ],
    "readiness_items": [{"title": "Change request approved", "is_mandatory": True}],
}


@pytest.fixture(scope="module", autouse=True)
def temp_store(tmp_path_factory):
    store = storage.LocalStore(str(tmp_path_factory.mktemp("lessons")))
    original = storage.store
    storage.store = lambda: store
    yield store
    storage.store = original


@pytest.fixture(scope="module")
def world(as_, seeded):
    ids = seeded["A"]
    pm, tm = as_("arjun.pm"), as_("meera.tm")
    tag = uuid.uuid4().hex[:4]
    prog = pm.post("/test-programmes", expect=201, json={
        "name": f"Lessons {tag}", "programme_code": f"LS{tag}", "financial_year": "FY2029",
        "start_date": "2028-04-01", "end_date": "2029-03-31"}).json()
    tpl = pm.post("/test-templates", expect=201, json={"template_key": f"cbs-{tag}", "name": "CBS DR", "category": "Database",
                                                      "default_duration": 240, "definition": TEMPLATE}).json()
    base = datetime(2028, 6, 1, 2, 0, tzinfo=timezone.utc)
    svc = uuid.uuid4()                                        # a service of our own, so other test files' events don't relate
    with worker_engine().begin() as c:
        c.execute(text("INSERT INTO business_services (id, tenant_id, code, name, criticality) "
                       "VALUES (:i, :t, :c, 'Treasury Core', 'Critical')"), {"i": svc, "t": ids["tenant"], "c": f"TRS{tag}"})

    def make(title, start, template=True, category="Database", services=True):
        e = pm.post("/test-events", expect=201, json={
            "programme_id": prog["id"], "template_id": tpl["id"] if template else None, "title": title, "category": category,
            "criticality": "High", "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=4)).isoformat(),
            "test_manager_user_id": str(ids["meera.tm"]), "business_service_ids": [str(svc)] if services else []}).json()
        tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "submit"})
        pm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "approve_plan"})
        st = tm.post(f"/test-events/{e['id']}/stakeholders", expect=201, json={"user_id": str(ids["rahul.dba"]), "event_role": "DBA"}).json()[0]
        acts = {a["activity_code"]: a for a in tm.get(f"/test-events/{e['id']}/activities", expect=200).json()}
        for a in acts.values():
            tm.patch(f"/test-activities/{a['id']}", expect=200, json={"stakeholder_id": st["id"]})
        tm.post(f"/test-events/{e['id']}/transitions", expect=200, json={"action": "begin_readiness"})
        return e, acts

    def block(e, acts, reason):
        rahul = as_("rahul.dba")
        rahul.patch(f"/test-activities/{acts['DB-01']['id']}", expect=200, json={"status": "In Progress"})
        rahul.patch(f"/test-activities/{acts['DB-01']['id']}", expect=200, json={"status": "Blocked", "blocked_reason": reason})

    # Two earlier runs of the same template, both blocked on DB-01; the first also has a finding and rejected evidence.
    e1, a1 = make("CBS DR run 1", base)
    block(e1, a1, "Snapshot schedule not aligned to RPO")
    as_("rahul.dba").patch(f"/test-activities/{a1['DB-01']['id']}", expect=200, json={"status": "In Progress"})
    f = as_("rahul.dba").post(f"/test-events/{e1['id']}/issues", expect=201, json={
        "title": "Batch jobs restarted twice after failover", "severity": "High", "issue_type": "Finding",
        "activity_id": a1["APP-01"]["id"]}).json()
    tm.post(f"/test-issues/{f['id']}/remediation-actions", expect=201, json={
        "title": "Point the scheduler at the DR alias", "owner_user_id": str(ids["sana.lead"]),
        "due_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()})
    as_("rahul.dba").post(f"/test-events/{e1['id']}/issues", expect=201, json={
        "title": "Runbook step 14 out of date", "severity": "Medium", "issue_type": "Finding"})
    bad = as_("rahul.dba").post(f"/test-activities/{a1['DB-01']['id']}/evidence/upload", expect=201,
                                files={"file": ("snap.log", f"snapshot ok {uuid.uuid4()}".encode(), "text/plain")},
                                data={"evidence_type": "Log"}).json()
    as_("rohan.risk").post(f"/test-evidence/{bad['id']}/review", expect=200, json={"accept": False, "comment": "Log is from UAT"})
    with worker_engine().begin() as c:                      # a missed RTO (measurements need a live test; set it directly)
        c.execute(text("""INSERT INTO test_measurements (tenant_id, event_id, measurement_type, target_value, target_unit,
                          actual_value, unit, target_canonical, actual_canonical, target_met, result_status)
                          VALUES (:t, :e, 'RTO', 4, 'Hours', 312, 'Minutes', 14400, 18720, false, 'Not Met')"""),
                  {"t": ids["tenant"], "e": e1["id"]})
    e2, a2 = make("CBS DR run 2", base + timedelta(days=90))
    block(e2, a2, "Snapshot schedule not aligned to RPO")
    unrelated, _ = make("Unrelated network test", base + timedelta(days=95), template=False, category="Infrastructure", services=False)
    later, _ = make("CBS DR run 9 (later)", base + timedelta(days=400))
    nxt, an = make("CBS DR run 3", base + timedelta(days=180))
    block(nxt, an, "Waiting for vendor snapshot")
    return {"ids": ids, "e1": e1, "e2": e2, "next": nxt, "an": an, "unrelated": unrelated, "later": later}


def get(as_, w, who="meera.tm"):
    return as_(who).get(f"/test-events/{w['next']['id']}/lessons", expect=200).json()


def by_kind(d, kind):
    return [x for x in d["lessons"] if x["kind"] == kind]


def test_related_tests_are_earlier_and_actually_related(as_, world):
    d = get(as_, world)
    codes = [r["event_code"] for r in d["related"]]
    assert codes[:2] == [world["e2"]["event_code"], world["e1"]["event_code"]]           # most related, then most recent
    assert world["later"]["event_code"] not in codes and world["unrelated"]["event_code"] not in codes
    assert "same test template" in d["related"][0]["why"] and any("Treasury Core" in w for w in d["related"][0]["why"])


def test_lessons_cover_blockers_remediation_targets_and_evidence(as_, world):
    d = get(as_, world)
    blk = by_kind(d, "blocker")[0]
    assert blk["times"] == 2 and "happened in 2 earlier tests" in blk["title"]
    assert blk["applies_to"]["code"] == "DB-01" and blk["applies_to"]["id"] == world["an"]["DB-01"]["id"]
    assert blk["happening_again"] and "blocked again in this test" in blk["now"] and d["lessons"][0]["key"] == blk["key"]
    act = by_kind(d, "open_action")[0]
    assert act["overdue"] and "Root cause" not in act["detail"] and act["applies_to"]["code"] == "APP-01"
    assert act["suggested_condition"].startswith("Remediation ") and "accepted before go" in act["suggested_condition"]
    titles = [x["title"] for x in by_kind(d, "finding")]
    assert any("Runbook step 14" in x for x in titles) and not any("Batch jobs" in x for x in titles)   # folded into the action
    assert by_kind(d, "missed_target")[0]["title"] == "RTO target missed: 5.2 h against 4 h"
    assert "Log is from UAT" in by_kind(d, "rejected_evidence")[0]["detail"]
    assert all(r["event_code"] for x in d["lessons"] for r in x["records"])                # every lesson links its sources
    assert d["can_decide"] is True and d["summary"]["open"] == d["summary"]["total"]


def test_apply_makes_a_linked_readiness_condition(as_, world):
    tm = as_("meera.tm")
    blk = by_kind(get(as_, world), "blocker")[0]
    as_("rahul.dba").post(f"/test-events/{world['next']['id']}/lessons/{blk['key']}/apply", expect=403, json={})
    out = tm.post(f"/test-events/{world['next']['id']}/lessons/{blk['key']}/apply", expect=201,
                  json={"evidence_required": True}).json()
    item = out["readiness_item"]
    assert item["title"] == blk["suggested_condition"] and item["is_mandatory"] and item["evidence_required"]
    assert item["activity_id"] == world["an"]["DB-01"]["id"] and world["e2"]["event_code"] in item["description"]
    rd = tm.get(f"/test-events/{world['next']['id']}/readiness", expect=200).json()
    mine = next(x for x in rd["items"] if x["id"] == item["id"])
    assert mine["from_lesson"]["event_code"] == world["e2"]["event_code"]
    assert any(item["title"] in u for u in rd["gate"]["unmet"])                              # the go decision now waits for it
    tm.post(f"/test-events/{world['next']['id']}/lessons/{blk['key']}/apply", expect=409, json={})
    again = by_kind(get(as_, world), "blocker")[0]["decision"]
    assert again["decision"] == "Applied" and again["readiness_title"] == item["title"] and again["by"]
    tm.delete(f"/test-events/{world['next']['id']}/lessons/{blk['key']}/decision", expect=409)


def test_dismiss_needs_a_reason_and_can_be_undone(as_, world):
    tm = as_("meera.tm")
    les = by_kind(get(as_, world), "rejected_evidence")[0]
    url = f"/test-events/{world['next']['id']}/lessons/{les['key']}"
    tm.post(f"{url}/dismiss", expect=422, json={"reason": ""})
    tm.post(f"{url}/dismiss", expect=200, json={"reason": "Evidence format agreed with audit this time"})
    d = get(as_, world)
    assert by_kind(d, "rejected_evidence")[0]["decision"]["reason"] == "Evidence format agreed with audit this time"
    assert d["summary"]["dismissed"] == 1
    tm.delete(f"{url}/decision", expect=200)
    assert by_kind(get(as_, world), "rejected_evidence")[0]["decision"] is None
    tm.post(f"/test-events/{world['next']['id']}/lessons/not-a-key/dismiss", expect=404, json={"reason": "x"})


def test_who_can_see_and_when_lessons_can_be_acted_on(as_, world, seeded):
    as_("ext.vendor").get(f"/test-events/{world['next']['id']}/lessons", expect=403)
    assert get(as_, world, "arjun.pm")["can_decide"] is True
    assert as_("meera.tm", tenant="B").get(f"/test-events/{world['next']['id']}/lessons").status_code == 404
    with worker_engine().begin() as c:                           # once the test is under way, lessons are read-only
        c.execute(text("UPDATE test_events SET lifecycle_status = 'Live' WHERE id = :e"), {"e": world["next"]["id"]})
    try:
        d = get(as_, world)
        assert d["can_decide"] is False
        key = next(x["key"] for x in d["lessons"] if not x["decision"])
        as_("meera.tm").post(f"/test-events/{world['next']['id']}/lessons/{key}/dismiss", expect=409, json={"reason": "late"})
    finally:
        with worker_engine().begin() as c:
            c.execute(text("UPDATE test_events SET lifecycle_status = 'Readiness' WHERE id = :e"), {"e": world["next"]["id"]})


def test_activity_matching():
    cur = [{"id": 1, "activity_code": "DB-01", "title": "Prepare database recovery point"},
           {"id": 2, "activity_code": "APP-07", "title": "Walk through the application recovery runbook"}]
    assert lessons._match({"activity_code": "DB-01", "title": "Prepare the database recovery point"}, cur)["id"] == 1
    assert lessons._match({"activity_code": "NB-02", "title": "Application recovery runbook walkthrough"}, cur)["id"] == 2
    assert lessons._match({"activity_code": "DB-01", "title": "Network routing"}, cur) is None    # same code, different work
    assert lessons._match({"activity_code": "X-1", "title": "Business validation"}, cur) is None
