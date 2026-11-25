"""Test event lifecycle state machine (spec §20, extended).

The spec's diagram uses states (Readiness, Paused) that its `lifecycle_status`
column omits, and leaves Cancelled / Rescheduled undefined. This module is the
single authoritative definition; see docs/DESIGN_NOTES.md §1.

Rules here are pure: the service layer gathers `EventFacts` from the database,
then asks this module whether a transition is allowed. The lifecycle is driven
by authorised users and business rules only -- never by an AI component.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .enums import LifecycleStatus as S
from .enums import Outcome


@dataclass(frozen=True)
class Transition:
    action: str
    sources: frozenset[S]
    target: S
    capability: str            # checked by the authorisation layer
    requires_reason: bool = False
    description: str = ""


TRANSITIONS: dict[str, Transition] = {t.action: t for t in [
    Transition("submit", frozenset({S.DRAFT}), S.PLANNED, "event.manage",
               description="Submit plan for approval"),
    Transition("approve_plan", frozenset({S.PLANNED}), S.MOBILISING, "programme.approve",
               description="Programme manager approves the test plan"),
    Transition("return_to_draft", frozenset({S.PLANNED}), S.DRAFT, "programme.approve",
               requires_reason=True, description="Plan rejected"),
    Transition("begin_readiness", frozenset({S.MOBILISING}), S.READINESS, "event.manage",
               description="Stakeholders assigned; readiness tracking starts"),
    Transition("mark_ready", frozenset({S.READINESS}), S.READY, "event.manage",
               description="All mandatory readiness conditions met"),
    Transition("return_to_mobilising", frozenset({S.READINESS, S.READY, S.CLOSURE}), S.MOBILISING,
               "event.manage", requires_reason=True,
               description="Outstanding actions / additional preparation required"),
    Transition("start", frozenset({S.READY}), S.LIVE, "event.manage",
               description="Authorised start of live coordination (requires Go approval)"),
    Transition("pause", frozenset({S.LIVE}), S.PAUSED, "event.manage", requires_reason=True),
    Transition("resume", frozenset({S.PAUSED}), S.LIVE, "event.manage",
               description="Requires an approved Resume decision"),
    Transition("complete", frozenset({S.LIVE}), S.CLOSURE, "event.manage",
               description="Coordination complete"),
    Transition("abort", frozenset({S.LIVE, S.PAUSED}), S.CLOSURE, "event.manage",
               requires_reason=True, description="Authorised stop/abort; outcome = Aborted"),
    Transition("close", frozenset({S.CLOSURE}), S.CLOSED, "event.close",
               description="Formal closure after checklist and approvals"),
    Transition("cancel", frozenset({S.DRAFT, S.PLANNED, S.MOBILISING, S.READINESS, S.READY}),
               S.CANCELLED, "programme.approve", requires_reason=True),
]}

# States in which schedule changes are permitted (rescheduling is an action,
# not a state). Rescheduling a Ready event invalidates its Go decision.
RESCHEDULABLE = frozenset({S.DRAFT, S.PLANNED, S.MOBILISING, S.READINESS, S.READY})
TERMINAL = frozenset({S.CLOSED, S.CANCELLED})


@dataclass
class EventFacts:
    """Facts the guards need; assembled by the service layer."""
    active_stakeholder_count: int = 0
    has_test_manager: bool = False
    readiness_gate_passed: bool = False
    readiness_unmet: list[str] = field(default_factory=list)
    has_valid_go_approval: bool = False
    has_valid_resume_approval: bool = False
    closure_blockers: list[str] = field(default_factory=list)


class TransitionError(Exception):
    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = details or []


def check_transition(current: S, action: str, facts: EventFacts, reason: str | None) -> Transition:
    """Return the transition if allowed, else raise TransitionError."""
    t = TRANSITIONS.get(action)
    if t is None:
        raise TransitionError(f"Unknown lifecycle action '{action}'")
    if current not in t.sources:
        raise TransitionError(
            f"Action '{action}' is not allowed from state '{current}'",
            [f"allowed from: {sorted(s.value for s in t.sources)}"],
        )
    if t.requires_reason and not (reason and reason.strip()):
        raise TransitionError(f"Action '{action}' requires a reason")

    problems: list[str] = []
    if action == "begin_readiness":
        if facts.active_stakeholder_count == 0:
            problems.append("No active stakeholders assigned")
        if not facts.has_test_manager:
            problems.append("No test manager assigned")
    elif action == "mark_ready":
        if not facts.readiness_gate_passed:
            problems.extend(facts.readiness_unmet or ["Mandatory readiness conditions unmet"])
    elif action == "start":
        if not facts.readiness_gate_passed:
            problems.extend(facts.readiness_unmet or ["Mandatory readiness conditions unmet"])
        if not facts.has_valid_go_approval:
            problems.append("No valid approved Go decision")
    elif action == "resume":
        if not facts.has_valid_resume_approval:
            problems.append("No approved Resume decision since the pause")
    elif action == "close":
        problems.extend(facts.closure_blockers)

    if problems:
        raise TransitionError(f"Guard failed for '{action}'", problems)
    return t


def outcome_for(action: str, requested: Outcome | None) -> Outcome | None:
    """Outcome implied by a transition. Lifecycle and outcome stay separate fields."""
    if action == "abort":
        return Outcome.ABORTED
    if action == "cancel":
        return Outcome.CANCELLED
    return requested


def allowed_actions(current: S) -> list[str]:
    return [a for a, t in TRANSITIONS.items() if current in t.sources]
