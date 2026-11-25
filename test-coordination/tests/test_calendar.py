"""Calendar feed, schedule check and blackout periods (Screen 02, RA-012/016)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

T0 = datetime(2027, 6, 7, 4, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def setup(as_, seeded):
    ids = seeded["A"]
    pm = as_("arjun.pm")
    prog = pm.post("/test-programmes", expect=201, json={
        "name": "FY2028 Programme", "programme_code": "FY28", "financial_year": "FY2028",
        "start_date": "2027-04-01", "end_date": "2028-03-31"}).json()

    def ev(title, start, hours, manager="meera.tm", services=True):
        return pm.post("/test-events", expect=201, json={
            "programme_id": prog["id"], "title": title, "category": "Database", "criticality": "High",
            "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=hours)).isoformat(),
            "test_manager_user_id": str(ids[manager]),
            "business_service_ids": [str(ids["svc_cbs"])] if services else []}).json()

    a = ev("Ledger restore", T0, 4)
    b = ev("Ledger failback", T0 + timedelta(hours=2), 4)                          # overlaps A: service + manager
    c = ev("Branch DNS swap", T0 + timedelta(days=3), 2, manager="vikram.owner", services=False)  # no overlap
    return {"ids": ids, "prog": prog, "a": a, "b": b, "c": c, "pm": pm}


def test_calendar_flags_conflicts(as_, setup):
    cal = as_("meera.tm").get(f"/calendar?start={T0.date()}T00:00:00Z&end={(T0 + timedelta(days=7)).date()}T00:00:00Z",
                              expect=200).json()
    by = {e["title"]: e for e in cal["events"]}
    assert {"Ledger restore", "Ledger failback"} <= set(by)
    kinds = {f["kind"] for f in by["Ledger restore"]["conflicts"]}
    assert kinds == {"shared_service", "same_test_manager"}
    assert by["Ledger restore"]["scope"] == ["Core Banking"]
    assert "Branch DNS swap" not in by or not by["Branch DNS swap"]["conflicts"]


def test_calendar_window_validation(as_):
    as_("meera.tm").get("/calendar?start=2027-06-10T00:00:00Z&end=2027-06-01T00:00:00Z", expect=422)
    as_("meera.tm").get("/calendar?start=2027-01-01T00:00:00Z&end=2028-06-01T00:00:00Z", expect=422)


def test_schedule_check_before_creating(as_, setup):
    ids = setup["ids"]
    r = as_("meera.tm").get(
        f"/schedule-check?start={(T0 + timedelta(hours=1)).isoformat().replace('+00:00', 'Z')}"
        f"&end={(T0 + timedelta(hours=3)).isoformat().replace('+00:00', 'Z')}"
        f"&business_service_ids={ids['svc_cbs']}&test_manager_user_id={ids['meera.tm']}", expect=200).json()
    assert {x["event_code"] for x in r["shared_service_overlaps"]} == {setup["a"]["event_code"], setup["b"]["event_code"]}
    assert len(r["test_manager_overlaps"]) == 2 and r["blackouts"] == []


def test_blackouts_are_programme_managed(as_, setup):
    body = {"name": "Monsoon release freeze", "starts_at": (T0 + timedelta(days=20)).isoformat(),
            "ends_at": (T0 + timedelta(days=27)).isoformat()}
    as_("meera.tm").post("/blackout-periods", expect=403, json=body)
    as_("arjun.pm").post("/blackout-periods", expect=422, json={**body, "ends_at": body["starts_at"]})
    made = as_("arjun.pm").post("/blackout-periods", expect=201, json=body).json()
    assert any(b["id"] == made["id"] for b in as_("rahul.dba").get("/blackout-periods", expect=200).json())
    cal = as_("arjun.pm").get(f"/calendar?start={(T0 + timedelta(days=19)).isoformat().replace('+00:00', 'Z')}"
                              f"&end={(T0 + timedelta(days=30)).isoformat().replace('+00:00', 'Z')}", expect=200).json()
    assert [b["name"] for b in cal["blackouts"]] == ["Monsoon release freeze"]


def test_lookups(as_):
    assert any(s["name"] == "Core Banking" for s in as_("meera.tm").get("/business-services", expect=200).json())
    assert isinstance(as_("meera.tm").get("/test-templates", expect=200).json(), list)
