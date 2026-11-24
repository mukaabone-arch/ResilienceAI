"""Activity dependency graph: cycle prevention and status propagation (Screen 07, RA-033..036).

MVP supports finish-to-start dependencies only. A mandatory (hard) dependency
gates the successor; a soft dependency only produces an at-risk signal.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .enums import ActivityStatus

SUPPORTED_TYPES = frozenset({"finish-to-start"})


@dataclass(frozen=True)
class Edge:
    predecessor: UUID
    successor: UUID
    mandatory: bool = True


@dataclass(frozen=True)
class ActivityNode:
    id: UUID
    status: ActivityStatus
    due_at: datetime | None = None


class DependencyError(ValueError):
    pass


def _adjacency(edges: list[Edge]) -> dict[UUID, list[UUID]]:
    adj: dict[UUID, list[UUID]] = defaultdict(list)
    for e in edges:
        adj[e.predecessor].append(e.successor)
    return adj


def find_path(edges: list[Edge], start: UUID, goal: UUID) -> list[UUID] | None:
    """BFS path start -> goal, or None."""
    adj = _adjacency(edges)
    prev: dict[UUID, UUID | None] = {start: None}
    q = deque([start])
    while q:
        n = q.popleft()
        if n == goal:
            path = [n]
            while prev[path[-1]] is not None:
                path.append(prev[path[-1]])  # type: ignore[arg-type]
            return list(reversed(path))
        for m in adj.get(n, []):
            if m not in prev:
                prev[m] = n
                q.append(m)
    return None


def validate_new_edge(existing: list[Edge], new: Edge, dependency_type: str = "finish-to-start") -> None:
    """Raise DependencyError if adding `new` is invalid (self-loop, duplicate, cycle)."""
    if dependency_type not in SUPPORTED_TYPES:
        raise DependencyError(f"Unsupported dependency type '{dependency_type}'")
    if new.predecessor == new.successor:
        raise DependencyError("An activity cannot depend on itself")
    if any(e.predecessor == new.predecessor and e.successor == new.successor for e in existing):
        raise DependencyError("Dependency already exists")
    # Adding pred -> succ creates a cycle iff succ already reaches pred.
    path = find_path(existing, new.successor, new.predecessor)
    if path:
        raise DependencyError(
            "Dependency would create a cycle: " + " -> ".join(str(p) for p in path + [new.successor])
        )


def downstream(edges: list[Edge], root: UUID) -> set[UUID]:
    """All activities transitively dependent on `root`."""
    adj = _adjacency(edges)
    seen: set[UUID] = set()
    stack = list(adj.get(root, []))
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack.extend(adj.get(n, []))
    return seen


@dataclass
class NodeState:
    """Derived dependency state for display and gating."""
    state: str                    # done | actionable | waiting | blocked | at_risk
    blocking_activities: list[UUID]


def propagate(nodes: list[ActivityNode], edges: list[Edge], now: datetime) -> dict[UUID, NodeState]:
    """Classify each activity.

    - done: completed
    - blocked: a transitive mandatory predecessor is Blocked (or itself Blocked)
    - waiting: a direct mandatory predecessor is not yet Completed
    - at_risk: actionable, but a predecessor (any type) is overdue or a soft predecessor is blocked
    - actionable: can be worked on now
    """
    by_id = {n.id: n for n in nodes}
    preds: dict[UUID, list[Edge]] = defaultdict(list)
    for e in edges:
        preds[e.successor].append(e)

    # Transitive closure of blockage over mandatory edges.
    blocked_roots: dict[UUID, set[UUID]] = defaultdict(set)
    mandatory_edges = [e for e in edges if e.mandatory]
    for n in nodes:
        if n.status == ActivityStatus.BLOCKED:
            for d in downstream(mandatory_edges, n.id):
                blocked_roots[d].add(n.id)

    result: dict[UUID, NodeState] = {}
    for n in nodes:
        if n.status == ActivityStatus.COMPLETED:
            result[n.id] = NodeState("done", [])
            continue
        if n.status == ActivityStatus.BLOCKED:
            result[n.id] = NodeState("blocked", [n.id])
            continue
        if blocked_roots.get(n.id):
            result[n.id] = NodeState("blocked", sorted(blocked_roots[n.id]))
            continue
        incomplete = [e.predecessor for e in preds[n.id]
                      if e.mandatory and by_id[e.predecessor].status != ActivityStatus.COMPLETED]
        if incomplete:
            result[n.id] = NodeState("waiting", incomplete)
            continue
        risky = [e.predecessor for e in preds[n.id]
                 if by_id[e.predecessor].status != ActivityStatus.COMPLETED and (
                     (by_id[e.predecessor].due_at and by_id[e.predecessor].due_at < now)
                     or by_id[e.predecessor].status == ActivityStatus.BLOCKED)]
        result[n.id] = NodeState("at_risk" if risky else "actionable", risky)
    return result


def can_start(activity: UUID, nodes: list[ActivityNode], edges: list[Edge]) -> list[UUID]:
    """Mandatory predecessors that are not Completed (empty list = may start)."""
    by_id = {n.id: n for n in nodes}
    return [e.predecessor for e in edges
            if e.successor == activity and e.mandatory
            and by_id[e.predecessor].status != ActivityStatus.COMPLETED]
