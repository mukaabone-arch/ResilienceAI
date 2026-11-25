"""Recurring test series (RA-015)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.db import worker_engine

S0 = datetime(2027, 7, 10, 20, 30, tzinfo=timezone.utc)    # Sun 11 Jul 02:00 IST


@pytest.fixture(scope="module")
def ctx(as_, seeded):
    ids = seeded["A"]
    pm = as_("arjun.pm")
    prog = pm.post("/test-programmes", expect=201, json={
        "name": "FY2028 Series", "programme_code": "FY28S", "financial_year": "FY2028",
        "start_date": "2027-04-01", "end_date": "2028-03-31"}).json()
    with worker_engine().begin() as c:   # a blackout covering the 3rd quarterly occurrence (Jan 2028)
        c.execute(text("INSERT INTO blackout_periods (tenant_id, name, starts_at, ends_at) VALUES (:t, 'FY-end freeze', :s, :e)"),
                  {"t": ids["tenant"], "s": datetime(2028, 1, 5, tzinfo=timezone.utc), "e": datetime(2028, 1, 20, tzinfo=timezone.utc)})
    body = {"programme_id": prog["id"], "title": "Payments quarterly DR drill", "category": "Infrastructure",
            "criticality": "High", "timezone": "Asia/Kolkata", "planned_start_at": S0.isoformat(),
            "planned_end_at": (S0 + timedelta(hours=3)).isoformat(), "test_manager_user_id": str(ids["meera.tm"]),
            "frequency": "quarterly", "count": 4}
    return {"ids": ids, "pm": pm, "body": body}


def test_preview_flags_each_occurrence(ctx, as_):
    pv = as_("meera.tm").post("/test-events/series/preview", expect=200, json=ctx["body"]).json()
    titles = [o["title"] for o in pv["occurrences"]]
    assert titles == ["Payments quarterly DR drill (Jul 2027)", "Payments quarterly DR drill (Oct 2027)",
                      "Payments quarterly DR drill (Jan 2028)", "Payments quarterly DR drill (Apr 2028)"]
    assert [o["in_blackout"] for o in pv["occurrences"]] == [False, False, True, False]
    assert pv["in_blackout"] == 1
    as_("rahul.dba").post("/test-events/series/preview", expect=403, json=ctx["body"])


def test_blackout_needs_skip_or_pm_override(ctx, as_):
    r = as_("meera.tm").post("/test-events/series", expect=409, json=ctx["body"])
    assert "Jan 2028" in r.json()["details"][0]
    as_("meera.tm").post("/test-events/series", expect=403, json={**ctx["body"], "blackout_override_reason": "Need it"})


def test_create_series_skipping_blackout(ctx, as_):
    out = as_("meera.tm").post("/test-events/series", expect=201,
                               json={**ctx["body"], "skip_blackout_occurrences": True}).json()
    assert len(out["events"]) == 3 and out["skipped"] == ["Payments quarterly DR drill (Jan 2028)"]
    ov = as_("meera.tm").get(f"/test-events/{out['events'][1]['id']}", expect=200).json()
    assert ov["series"]["frequency"] == "quarterly"
    assert [o["series_index"] for o in ov["series"]["occurrences"]] == [1, 2, 3]
    # wall-clock time kept: 02:00 IST each time
    ist = ZoneInfo("Asia/Kolkata")
    assert {datetime.fromisoformat(e["planned_start_at"].replace("Z", "+00:00")).astimezone(ist).strftime("%H:%M")
            for e in out["events"]} == {"02:00"}


def test_pm_override_creates_all_four(ctx, as_):
    out = as_("arjun.pm").post("/test-events/series", expect=201, json={
        **ctx["body"], "title": "Cards quarterly DR drill", "blackout_override_reason": "Audit requires a January run"}).json()
    assert len(out["events"]) == 4 and out["skipped"] == []


def test_series_validation(ctx, as_):
    as_("meera.tm").post("/test-events/series/preview", expect=422, json={**ctx["body"], "count": 1})
    as_("meera.tm").post("/test-events/series/preview", expect=422, json={
        **ctx["body"], "frequency": "weekly", "planned_end_at": (S0 + timedelta(days=8)).isoformat()})
