"""Adding a whole team to a test (RA-026): team list, expansion, duplicates, inactive members."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import worker_engine

T0 = datetime(2027, 9, 6, 4, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def ev(as_, seeded):
    ids = seeded["A"]
    pm = as_("arjun.pm")
    prog = pm.post("/test-programmes", expect=201, json={
        "name": "Team assignment programme", "programme_code": "TEAMS", "financial_year": "FY2028",
        "start_date": "2027-04-01", "end_date": "2028-03-31"}).json()
    e = pm.post("/test-events", expect=201, json={
        "programme_id": prog["id"], "title": "Team assignment drill", "category": "Database", "criticality": "High",
        "planned_start_at": T0.isoformat(), "planned_end_at": (T0 + timedelta(hours=3)).isoformat(),
        "test_manager_user_id": str(ids["meera.tm"])}).json()
    return {"ids": ids, "event": e}


def _team(as_, name):
    return next(t for t in as_("meera.tm").get("/teams", expect=200).json() if t["name"] == name)


def test_teams_listed_with_members(as_):
    teams = as_("meera.tm").get("/teams", expect=200).json()
    names = [t["name"] for t in teams]
    assert {"Database Engineering", "Infrastructure and Network", "Business Validation"} <= set(names)
    db = next(t for t in teams if t["name"] == "Database Engineering")
    assert [m["display_name"] for m in db["members"]] == ["Rahul Das", "Sana Qureshi"]
    assert as_("ext.vendor").get("/teams", expect=200).json() == []      # vendors don't see the bank's teams


def test_add_team_skips_people_already_on_the_test(as_, ev):
    tm, ids, eid = as_("meera.tm"), ev["ids"], ev["event"]["id"]
    infra = _team(as_, "Infrastructure and Network")
    tm.post(f"/test-events/{eid}/stakeholders", expect=201, json={"user_id": str(ids["kiran.net"]), "event_role": "Network lead"})
    added = tm.post(f"/test-events/{eid}/stakeholders", expect=201,
                    json={"team_id": infra["id"], "event_role": "Infrastructure recovery"}).json()
    assert [a["display_name"] for a in added] == ["Anita Kulkarni"]        # Kiran was already on it
    people = tm.get(f"/test-events/{eid}/stakeholders", expect=200).json()
    anita = next(p for p in people if p["display_name"] == "Anita Kulkarni")
    assert anita["team_name"] == "Infrastructure and Network" and anita["event_role"] == "Infrastructure recovery"
    kiran = next(p for p in people if p["display_name"] == "Kiran Shetty")
    assert kiran["team_name"] is None and kiran["event_role"] == "Network lead"

    r = tm.post(f"/test-events/{eid}/stakeholders", expect=409, json={"team_id": infra["id"], "event_role": "Again"})
    assert "Everyone in Infrastructure and Network is already on this test" in r.text


def test_inactive_member_is_skipped(as_, ev, seeded):
    tm, ids, eid = as_("meera.tm"), ev["ids"], ev["event"]["id"]
    with worker_engine().begin() as c:
        c.execute(text("UPDATE users SET is_active = false WHERE id = :u"), {"u": ids["rohan.risk"]})
    try:
        biz = _team(as_, "Business Validation")
        assert [m["display_name"] for m in biz["members"]] == ["Farah Khan"]
        added = tm.post(f"/test-events/{eid}/stakeholders", expect=201,
                        json={"team_id": biz["id"], "event_role": "Business validation"}).json()
        assert [a["display_name"] for a in added] == ["Farah Khan"]
    finally:
        with worker_engine().begin() as c:
            c.execute(text("UPDATE users SET is_active = true WHERE id = :u"), {"u": ids["rohan.risk"]})


def test_only_managers_add_teams(as_, ev):
    db = _team(as_, "Database Engineering")
    as_("kiran.net").post(f"/test-events/{ev['event']['id']}/stakeholders", expect=403,   # on the test, not a manager
                          json={"team_id": db["id"], "event_role": "Database recovery"})
