"""Readiness gate evaluation (Screen 07, RA-037..039).

A readiness percentage is a progress indicator only. The gate passes when every
mandatory item is Ready -- with accepted evidence where evidence is required --
or is covered by an approved ReadinessException.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from .enums import ReadinessStatus


@dataclass(frozen=True)
class ReadinessItemFacts:
    id: UUID
    title: str
    status: ReadinessStatus
    is_mandatory: bool
    evidence_required: bool
    has_accepted_evidence: bool = False
    has_approved_exception: bool = False


@dataclass
class GateResult:
    passed: bool
    percent_ready: float
    total: int
    mandatory_total: int
    mandatory_ready: int
    unmet: list[str] = field(default_factory=list)
    excepted: list[str] = field(default_factory=list)
    unmet_item_ids: list[UUID] = field(default_factory=list)


def _satisfied(i: ReadinessItemFacts) -> bool:
    return i.status == ReadinessStatus.READY and (not i.evidence_required or i.has_accepted_evidence)


def evaluate(items: list[ReadinessItemFacts]) -> GateResult:
    total = len(items)
    ready = sum(1 for i in items if _satisfied(i))
    mandatory = [i for i in items if i.is_mandatory]
    unmet, excepted, unmet_ids = [], [], []
    m_ready = 0
    for i in mandatory:
        if _satisfied(i):
            m_ready += 1
        elif i.has_approved_exception:
            excepted.append(i.title)
        else:
            why = f"status {i.status}"
            if i.status == ReadinessStatus.READY and i.evidence_required:
                why = "evidence not yet accepted"
            unmet.append(f"Mandatory readiness item '{i.title}' not met ({why})")
            unmet_ids.append(i.id)
    # An event with no mandatory items cannot pass by default: readiness must be defined.
    if not mandatory:
        unmet.append("No mandatory readiness items defined for this event")
    return GateResult(
        passed=not unmet,
        percent_ready=round(100.0 * ready / total, 1) if total else 0.0,
        total=total,
        mandatory_total=len(mandatory),
        mandatory_ready=m_ready,
        unmet=unmet,
        excepted=excepted,
        unmet_item_ids=unmet_ids,
    )
