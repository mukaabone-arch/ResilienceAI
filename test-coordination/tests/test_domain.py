"""Unit tests for pure domain rules (no database)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain import activity_workflow as aw
from app.domain import audit_chain, dependencies as dep, lifecycle as lc, measurements as ms, readiness as rd
from app.domain.enums import ActivityStatus as A, LifecycleStatus as S, ReadinessStatus as RS, ReviewStatus as R

NOW = datetime(2027, 1, 10, tzinfo=timezone.utc)


# ---- lifecycle ---------------------------------------------------------------

def test_happy_path_transitions():
    f = lc.EventFacts(active_stakeholder_count=3, has_test_manager=True,
                      readiness_gate_passed=True, has_valid_go_approval=True)
    path = [("submit", S.DRAFT), ("approve_plan", S.PLANNED), ("begin_readiness", S.MOBILISING),
            ("mark_ready", S.READINESS), ("start", S.READY), ("complete", S.LIVE), ("close", S.CLOSURE)]
    for action, state in path:
        lc.check_transition(state, action, f, None)


def test_start_blocked_without_go_and_readiness():  # QA-08
    f = lc.EventFacts(readiness_gate_passed=False, readiness_unmet=["DB recovery not ready"])
    with pytest.raises(lc.TransitionError) as e:
        lc.check_transition(S.READY, "start", f, None)
    assert "DB recovery not ready" in e.value.details
    assert "No valid approved Go decision" in e.value.details


def test_invalid_source_and_reason_required():
    with pytest.raises(lc.TransitionError):
        lc.check_transition(S.DRAFT, "start", lc.EventFacts(), None)
    with pytest.raises(lc.TransitionError, match="requires a reason"):
        lc.check_transition(S.LIVE, "abort", lc.EventFacts(), "  ")
    assert lc.outcome_for("abort", None).value == "Aborted"


def test_resume_needs_approval_and_close_needs_clean_checklist():
    with pytest.raises(lc.TransitionError):
        lc.check_transition(S.PAUSED, "resume", lc.EventFacts(), None)
    with pytest.raises(lc.TransitionError) as e:
        lc.check_transition(S.CLOSURE, "close", lc.EventFacts(closure_blockers=["Missing evidence"]), None)
    assert e.value.details == ["Missing evidence"]


# ---- dependencies ------------------------------------------------------------

def test_cycle_rejected():  # QA-07
    a, b, c = uuid4(), uuid4(), uuid4()
    edges = [dep.Edge(a, b), dep.Edge(b, c)]
    with pytest.raises(dep.DependencyError, match="cycle"):
        dep.validate_new_edge(edges, dep.Edge(c, a))
    with pytest.raises(dep.DependencyError):
        dep.validate_new_edge(edges, dep.Edge(a, a))
    with pytest.raises(dep.DependencyError):
        dep.validate_new_edge(edges, dep.Edge(a, b))
    dep.validate_new_edge(edges, dep.Edge(a, c))  # diamond shortcut is fine


def test_blocked_prerequisite_propagates():  # QA-06, mirrors spec flow diagram
    infra, db, net, app_, tech = (uuid4() for _ in range(5))
    edges = [dep.Edge(infra, db), dep.Edge(db, app_), dep.Edge(infra, net),
             dep.Edge(net, app_), dep.Edge(app_, tech)]
    nodes = [dep.ActivityNode(infra, A.COMPLETED), dep.ActivityNode(db, A.BLOCKED),
             dep.ActivityNode(net, A.IN_PROGRESS, due_at=NOW - timedelta(hours=1)),
             dep.ActivityNode(app_, A.NOT_STARTED), dep.ActivityNode(tech, A.NOT_STARTED)]
    st = dep.propagate(nodes, edges, NOW)
    assert st[infra].state == "done"
    assert st[db].state == "blocked"
    assert st[net].state == "actionable"
    assert st[app_].state == "blocked" and st[app_].blocking_activities == [db]
    assert st[tech].state == "blocked"
    assert dep.downstream(edges, db) == {app_, tech}
    assert set(dep.can_start(app_, nodes, edges)) == {db, net}


def test_soft_dependency_gives_at_risk_only():
    a, b = uuid4(), uuid4()
    nodes = [dep.ActivityNode(a, A.IN_PROGRESS, due_at=NOW - timedelta(days=1)), dep.ActivityNode(b, A.NOT_STARTED)]
    st = dep.propagate(nodes, [dep.Edge(a, b, mandatory=False)], NOW)
    assert st[b].state == "at_risk"


# ---- readiness ---------------------------------------------------------------

def _item(status, mandatory=True, ev=False, accepted=False, exc=False, title="x"):
    return rd.ReadinessItemFacts(uuid4(), title, status, mandatory, ev, accepted, exc)


def test_gate():
    assert rd.evaluate([_item(RS.READY), _item(RS.PENDING, mandatory=False)]).passed
    r = rd.evaluate([_item(RS.READY), _item(RS.BLOCKED, title="DB")])
    assert not r.passed and "DB" in r.unmet[0] and r.percent_ready == 50.0
    assert not rd.evaluate([_item(RS.READY, ev=True)]).passed        # evidence not accepted
    assert rd.evaluate([_item(RS.READY, ev=True, accepted=True)]).passed
    r = rd.evaluate([_item(RS.FAILED, exc=True, title="Vendor")])
    assert r.passed and r.excepted == ["Vendor"]
    assert not rd.evaluate([]).passed


# ---- measurements ------------------------------------------------------------

def test_measurements_typed():
    met, t, a = ms.evaluate("RTO", Decimal(4), "Hours", Decimal(230), "Minutes")
    assert met and t == 14400 and a == 13800
    assert not ms.evaluate("RPO", Decimal(15), "Minutes", Decimal(16), "Minutes")[0]
    assert ms.evaluate("Availability", Decimal("99.9"), "Percentage", Decimal("99.95"), "Percentage")[0]
    with pytest.raises(ms.MeasurementError):
        ms.evaluate("RTO", Decimal(4), "Hours", Decimal(99), "Percentage")


# ---- activity workflow -------------------------------------------------------

def test_review_required_submission_does_not_complete():
    assert aw.owner_update(A.IN_PROGRESS, A.SUBMITTED, R.PENDING) == (A.SUBMITTED, R.PENDING)
    assert aw.owner_update(A.IN_PROGRESS, A.SUBMITTED, R.NOT_REQUIRED) == (A.COMPLETED, R.NOT_REQUIRED)
    assert aw.review(A.SUBMITTED, R.PENDING, accept=False) == (A.IN_PROGRESS, R.REJECTED)
    with pytest.raises(aw.ActivityTransitionError):
        aw.owner_update(A.NOT_STARTED, A.COMPLETED, R.NOT_REQUIRED)


# ---- audit chain -------------------------------------------------------------

def test_audit_chain_detects_tampering():
    rows, prev = [], audit_chain.GENESIS
    for i in range(1, 4):
        r = {"id": uuid4(), "tenant_id": uuid4(), "seq": i, "event_id": None, "actor_user_id": None,
             "action": f"a{i}", "entity_type": "Event", "entity_id": uuid4(), "old_values": None,
             "new_values": {"n": i}, "occurred_at": NOW, "correlation_id": None, "prev_hash": prev}
        r["row_hash"] = audit_chain.compute_hash(prev, audit_chain.hash_payload(r))
        prev = r["row_hash"]
        rows.append(r)
    assert audit_chain.verify(rows) == (True, None)
    rows[1]["new_values"] = {"n": 999}
    assert audit_chain.verify(rows) == (False, 2)


# ---- recurrence --------------------------------------------------------------
from app.domain import recurrence as rc  # noqa: E402


def test_recurrence_keeps_wall_clock_and_clamps_month_end():
    s = datetime(2027, 1, 31, 20, 30, tzinfo=timezone.utc)          # 31 Jan 02:00 IST next day -> 1 Feb 02:00 IST
    occ = rc.occurrences(s, s + timedelta(hours=4), "Asia/Kolkata", "monthly", 3)
    assert [o[0].isoformat() for o in occ] == ["2027-01-31T20:30:00+00:00", "2027-02-28T20:30:00+00:00", "2027-03-31T20:30:00+00:00"]
    assert all(e - s_ == timedelta(hours=4) for s_, e in occ)
    q = rc.occurrences(datetime(2027, 4, 10, 4, 0, tzinfo=timezone.utc), datetime(2027, 4, 10, 8, 0, tzinfo=timezone.utc),
                       "Asia/Kolkata", "quarterly", 4)
    assert [o[0].month for o in q] == [4, 7, 10, 1]
    # London: same 09:00 local across the March clock change
    w = rc.occurrences(datetime(2027, 3, 22, 9, 0, tzinfo=timezone.utc), datetime(2027, 3, 22, 10, 0, tzinfo=timezone.utc),
                       "Europe/London", "weekly", 2)
    assert w[1][0].hour == 8          # 09:00 BST = 08:00 UTC
    assert rc.occurrence_title("DR drill", q[0][0], "Asia/Kolkata", "quarterly") == "DR drill (Apr 2027)"


def test_recurrence_validation():
    s = datetime(2027, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(rc.RecurrenceError):
        rc.occurrences(s, s + timedelta(days=8), "UTC", "weekly", 3)      # overlaps the next occurrence
    with pytest.raises(rc.RecurrenceError):
        rc.occurrences(s, s + timedelta(hours=1), "UTC", "monthly", 1)
    with pytest.raises(rc.RecurrenceError):
        rc.occurrences(s, s + timedelta(hours=1), "UTC", "daily", 3)


# ---- slip forecast -----------------------------------------------------------
from app.domain import forecast as fc  # noqa: E402


def test_forecast_blocked_chain_slips():
    start = NOW + timedelta(hours=36)
    db = fc.Act("db", "DB-01", "Recovery point", "Blocked", due_at=NOW + timedelta(hours=6),
                updated_at=NOW - timedelta(hours=40), owner="Rahul Das")
    app_ = fc.Act("app", "APP-01", "App walkthrough", "Not Started", due_at=NOW + timedelta(hours=20),
                  planned_start_at=NOW + timedelta(hours=12))
    cond = fc.Cond("c1", "Database recovery ready", "Blocked", activity_id="db")
    f = fc.project(NOW, start, "Readiness", [db, app_], [("db", "app", True)], [cond])
    assert f.verdict == "likely_slip"                  # 40 h blocked -> +40 h, then APP-01's 8 h
    assert f.projected_ready == NOW + timedelta(hours=48) and f.margin_hours == -12.0
    assert f.drivers[0].ref == "APP-01" and "waiting on DB-01" in f.drivers[0].reason
    assert any("blocked for 40.0 h" in d.reason for d in f.drivers)


def test_forecast_on_track_and_not_applicable():
    start = NOW + timedelta(days=6)
    a = fc.Act("a", "INF-01", "Infra", "In Progress", due_at=NOW + timedelta(days=2))
    c = fc.Cond("c", "Change approved", "Ready", evidence_pending=True)
    f = fc.project(NOW, start, "Readiness", [a], [], [c])
    assert f.verdict == "on_track" and f.margin_hours >= 24
    assert fc.project(NOW, start, "Live", [a], [], [c]).applicable is False
    done = fc.project(NOW, start, "Ready", [], [], [fc.Cond("x", "All good", "Ready")])
    assert done.verdict == "ready"


def test_forecast_overdue_rule_is_bounded():
    start = NOW + timedelta(hours=30)
    late = fc.Act("l", "NET-01", "Routing", "In Progress", due_at=NOW - timedelta(days=10))
    f = fc.project(NOW, start, "Readiness", [late], [], [fc.Cond("c", "Net ready", "Pending", activity_id="l")])
    assert f.projected_ready == NOW + timedelta(hours=72)          # capped at 3 days
    assert f.verdict == "likely_slip" and f.margin_hours == -42.0
