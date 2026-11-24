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
