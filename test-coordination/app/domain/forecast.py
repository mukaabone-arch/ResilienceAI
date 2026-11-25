"""Readiness slip forecast (RA-071): a transparent, rule-based estimate.

Pilots have little history, so this is not a trained model. Each rule is
stated in plain words in the result, so a person can see why it says what it
says, and a learned model can replace the per-item estimates later.

Rules
- Done work constrains nothing. Submitted work needs about 12 hours of review.
- An overdue activity is assumed to slip again by as long as it is already late
  (at least 4 hours, at most 3 days).
- A blocked activity is assumed to need at least as long again as it has been
  blocked (at least 1 day).
- Work cannot finish before the work it depends on, plus its own planned length.
- A readiness condition linked to an activity follows that activity; evidence
  still to be accepted adds about 12 hours. A blocked or failed condition is
  assumed to need 2 days. Others are assumed met by the day before the start.
- Readiness is needed by the planned start. Under 24 hours of margin is "at risk".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

H = timedelta(hours=1)
REVIEW = 12 * H
DEFAULT_LENGTH = 8 * H
MARGIN = 24 * H


@dataclass
class Act:
    id: Any
    code: str
    title: str
    status: str
    mandatory: bool = True
    due_at: datetime | None = None
    planned_start_at: datetime | None = None
    updated_at: datetime | None = None      # when a Blocked status was set
    owner: str | None = None


@dataclass
class Cond:
    id: Any
    title: str
    status: str
    mandatory: bool = True
    activity_id: Any = None
    evidence_pending: bool = False
    excepted: bool = False


@dataclass
class Driver:
    kind: str           # activity | condition
    ref: str            # code or title
    id: Any
    projected: datetime
    reason: str


@dataclass
class Forecast:
    applicable: bool
    verdict: str = "not_applicable"      # on_track | at_risk | likely_slip | not_applicable | ready
    projected_ready: datetime | None = None
    needed_by: datetime | None = None
    margin_hours: float | None = None
    drivers: list[Driver] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)


RULES_TEXT = [
    "Overdue work is assumed to slip again by as long as it is already late (4 hours to 3 days).",
    "Blocked work is assumed to need at least as long again as it has been blocked (minimum 1 day).",
    "Work can't finish before the work it depends on, plus its own planned length.",
    "Evidence still awaiting review adds about 12 hours.",
    "Readiness is needed by the planned start; under 24 hours of margin counts as at risk.",
]


def _hours(td: timedelta) -> float:
    return round(td.total_seconds() / 3600, 1)


def _length(a: Act) -> timedelta:
    if a.planned_start_at and a.due_at and a.due_at > a.planned_start_at:
        return a.due_at - a.planned_start_at
    return DEFAULT_LENGTH


def project(now: datetime, planned_start: datetime, lifecycle: str, acts: list[Act],
            edges: list[tuple[Any, Any, bool]], conds: list[Cond]) -> Forecast:
    if lifecycle not in ("Mobilising", "Readiness", "Ready"):
        return Forecast(applicable=False)

    by_id = {a.id: a for a in acts}
    preds: dict[Any, list[Any]] = {}
    for p, s, mandatory in edges:
        if mandatory:
            preds.setdefault(s, []).append(p)

    memo: dict[Any, tuple[datetime | None, str]] = {}

    def finish(aid, stack=()) -> tuple[datetime | None, str]:
        """Projected finish of an activity (None if already done) and why."""
        if aid in memo:
            return memo[aid]
        a = by_id[aid]
        if a.status == "Completed" or aid in stack:
            memo[aid] = (None, "done")
            return memo[aid]
        if a.status == "Submitted":
            base, why = now + REVIEW, "awaiting review"
        elif a.status == "Blocked":
            blocked_for = max(now - (a.updated_at or now), timedelta(0))
            base = now + max(blocked_for, 24 * H)
            why = f"blocked for {_hours(blocked_for)} h"
        elif a.due_at and a.due_at < now:
            late = now - a.due_at
            base = now + min(max(late, 4 * H), 72 * H)
            why = f"overdue by {_hours(late)} h"
        else:
            base = max(a.due_at or (planned_start - 24 * H), now)
            why = "on its planned date"
        for p in preds.get(aid, []):
            pf, _ = finish(p, stack + (aid,))
            if pf and pf + _length(a) > base:
                base = pf + _length(a)
                why = f"waiting on {by_id[p].code}"
        memo[aid] = (base, why)
        return memo[aid]

    needed_by = planned_start
    projections: list[Driver] = []
    for c in conds:
        if not c.mandatory or c.excepted:
            continue
        if c.status == "Ready" and not c.evidence_pending:
            continue
        if c.activity_id in by_id:
            f, why = finish(c.activity_id)
            f = f or now
            why = f"follows {by_id[c.activity_id].code} ({why})"
        elif c.status in ("Blocked", "Failed"):
            f, why = now + 48 * H, f"{c.status.lower()}; assumed 2 days to resolve"
        elif c.status == "Ready":
            f, why = now, "ready"
        else:
            f, why = max(planned_start - 24 * H, now + 4 * H), "not yet confirmed"
        if c.evidence_pending:
            f, why = f + REVIEW, why + "; evidence awaiting review"
        projections.append(Driver("condition", c.title, c.id, f, why))
    for a in acts:
        if a.mandatory and a.status != "Completed":
            f, why = finish(a.id)
            if f and a.due_at and a.due_at <= planned_start:   # pre-start work counts toward readiness
                projections.append(Driver("activity", a.code, a.id, f, why + (f", {a.owner}" if a.owner else "")))

    if not projections:
        return Forecast(applicable=True, verdict="ready", projected_ready=now, needed_by=needed_by,
                        margin_hours=_hours(needed_by - now), rules=RULES_TEXT)
    projected = max(d.projected for d in projections)
    margin = needed_by - projected
    verdict = "on_track" if margin >= MARGIN else "at_risk" if margin >= timedelta(0) else "likely_slip"
    drivers = sorted([d for d in projections if d.projected > needed_by - MARGIN], key=lambda d: d.projected, reverse=True)[:5]
    return Forecast(applicable=True, verdict=verdict, projected_ready=projected, needed_by=needed_by,
                    margin_hours=_hours(margin), drivers=drivers, rules=RULES_TEXT)
