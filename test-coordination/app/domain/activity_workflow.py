"""Activity status and review workflow (RA-031).

Assignment acknowledgement, task submission and reviewer acceptance are
separate steps. Where review is required, submission leaves the activity in
Submitted until a reviewer accepts it.
"""
from __future__ import annotations

from .enums import ActivityStatus as A
from .enums import ReviewStatus as R

# Owner-driven transitions.
OWNER_TRANSITIONS: dict[A, set[A]] = {
    A.NOT_STARTED: {A.IN_PROGRESS, A.BLOCKED},
    A.IN_PROGRESS: {A.BLOCKED, A.SUBMITTED},
    A.BLOCKED: {A.IN_PROGRESS},
    A.SUBMITTED: set(),
    A.COMPLETED: set(),
}


class ActivityTransitionError(ValueError):
    pass


def owner_update(current: A, requested: A, review: R) -> tuple[A, R]:
    """Apply an owner's status change. Returns (new_status, new_review_status)."""
    if requested not in OWNER_TRANSITIONS[current]:
        raise ActivityTransitionError(f"Cannot move activity from '{current}' to '{requested}'")
    if requested == A.SUBMITTED:
        if review == R.NOT_REQUIRED:
            return A.COMPLETED, R.NOT_REQUIRED
        return A.SUBMITTED, R.PENDING
    return requested, review


def review(current: A, review_status: R, accept: bool) -> tuple[A, R]:
    if current != A.SUBMITTED or review_status != R.PENDING:
        raise ActivityTransitionError("Activity is not awaiting review")
    return (A.COMPLETED, R.ACCEPTED) if accept else (A.IN_PROGRESS, R.REJECTED)
