"""Request models. Responses are plain JSON records from the service layer."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Crit = Literal["Critical", "High", "Medium", "Low"]
Cat = Literal["Infrastructure", "Application", "Database", "Cyber", "Business"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProgrammeIn(Model):
    name: str = Field(max_length=200)
    programme_code: str = Field(max_length=50)
    financial_year: str = Field(max_length=20)
    start_date: date
    end_date: date
    owner_user_id: Optional[UUID] = None


class TemplateIn(Model):
    template_key: str = Field(max_length=80)
    name: str = Field(max_length=200)
    category: Cat
    description: Optional[str] = None
    default_duration: int = Field(gt=0)
    definition: dict[str, Any] = Field(default_factory=dict, description=(
        "{activities:[{code,title,category,priority,due_offset_hours,is_mandatory,evidence_required}],"
        " dependencies:[[pred_code,succ_code,mandatory?]], readiness_items:[{title,is_mandatory,evidence_required,activity_code}],"
        " milestones:[{title,offset_minutes}]}"))


class EventIn(Model):
    programme_id: UUID
    template_id: Optional[UUID] = None
    title: str = Field(max_length=250)
    description: Optional[str] = None
    objectives: Optional[str] = None
    business_impact: Optional[str] = None
    category: Cat
    criticality: Crit
    planned_start_at: AwareDatetime
    planned_end_at: AwareDatetime
    timezone: str = "UTC"
    owner_user_id: Optional[UUID] = None
    test_manager_user_id: Optional[UUID] = None
    business_service_ids: list[UUID] = Field(default_factory=list)
    blackout_override_reason: Optional[str] = None


class EventPatch(Model):
    expected_version: int
    title: Optional[str] = Field(default=None, max_length=250)
    description: Optional[str] = None
    objectives: Optional[str] = None
    business_impact: Optional[str] = None
    criticality: Optional[Crit] = None
    test_manager_user_id: Optional[UUID] = None
    planned_start_at: Optional[AwareDatetime] = None
    planned_end_at: Optional[AwareDatetime] = None
    timezone: Optional[str] = None
    reschedule_reason: Optional[str] = None
    blackout_override_reason: Optional[str] = None


class TransitionIn(Model):
    action: str
    reason: Optional[str] = None
    expected_version: Optional[int] = None


class OutcomeIn(Model):
    outcome: Literal["Successful", "Partial", "Failed", "Aborted"]
    summary: Optional[str] = None
    expected_version: Optional[int] = None


class ScopeIn(Model):
    business_service_id: Optional[UUID] = None
    name: Optional[str] = None
    item_type: str = "BusinessService"


class StakeholderIn(Model):
    user_id: Optional[UUID] = None
    team_id: Optional[UUID] = None
    event_role: str = Field(max_length=60)
    responsibility: Optional[str] = None
    is_primary: bool = True
    alternate_user_id: Optional[UUID] = None


class RespondIn(Model):
    accept: bool
    comment: Optional[str] = None


class ReplaceIn(Model):
    new_user_id: UUID
    reason: str


class ActivityIn(Model):
    activity_code: str = Field(max_length=60)
    title: str = Field(max_length=250)
    description: Optional[str] = None
    category: Optional[str] = None
    priority: Crit = "Medium"
    stakeholder_id: Optional[UUID] = None
    planned_start_at: Optional[AwareDatetime] = None
    due_at: Optional[AwareDatetime] = None
    is_mandatory: bool = True
    evidence_required: bool = False
    review_required: bool = False
    reviewer_user_id: Optional[UUID] = None


class ActivityPatch(Model):
    expected_version: Optional[int] = None
    status: Optional[Literal["Not Started", "In Progress", "Blocked", "Submitted"]] = None
    blocked_reason: Optional[str] = None
    comment: Optional[str] = None
    stakeholder_id: Optional[UUID] = None
    due_at: Optional[AwareDatetime] = None


class ReviewIn(Model):
    accept: bool
    comment: Optional[str] = None


class DependencyIn(Model):
    predecessor_activity_id: UUID
    successor_activity_id: UUID
    dependency_type: Literal["finish-to-start"] = "finish-to-start"
    is_mandatory: bool = True


class ReadinessItemIn(Model):
    title: str = Field(max_length=250)
    description: Optional[str] = None
    owner_user_id: Optional[UUID] = None
    activity_id: Optional[UUID] = None
    is_mandatory: bool = True
    evidence_required: bool = False


class ReadinessPatch(Model):
    status: Literal["Pending", "In Progress", "Ready", "Blocked", "Failed"]
    comment: Optional[str] = None
    owner_user_id: Optional[UUID] = None
    expected_version: Optional[int] = None


class ReadinessUpdateIn(Model):
    """POST /test-events/{id}/readiness (Appendix A): batch update of items."""
    updates: list[dict[str, Any]]


class EvidenceIn(Model):
    evidence_type: Literal["Report", "Screenshot", "Log", "Approval", "ExternalRecord", "Other"]
    storage_reference: str = Field(max_length=500)
    filename: Optional[str] = Field(default=None, max_length=255)
    checksum: Optional[str] = None
    supersedes_id: Optional[UUID] = None


class IssueIn(Model):
    title: str = Field(max_length=250)
    description: Optional[str] = None
    severity: Crit
    issue_type: Literal["Blocker", "Finding", "Risk", "Incident"]
    activity_id: Optional[UUID] = None
    root_cause: Optional[str] = None
    owner_user_id: Optional[UUID] = None
    due_at: Optional[AwareDatetime] = None


class IssuePatch(Model):
    status: Optional[Literal["Open", "In Progress", "Resolved", "Closed"]] = None
    comment: Optional[str] = None
    owner_user_id: Optional[UUID] = None
    due_at: Optional[AwareDatetime] = None
    root_cause: Optional[str] = None
    severity: Optional[Crit] = None


class RemediationIn(Model):
    title: str = Field(max_length=250)
    owner_user_id: UUID
    due_at: AwareDatetime


class RemediationPatch(Model):
    status: Literal["Open", "In Progress", "Submitted", "Accepted"]
    resolution_evidence_id: Optional[UUID] = None


class MeasurementIn(Model):
    measurement_type: Literal["RTO", "RPO", "Availability", "Other"]
    target_value: Decimal
    target_unit: Literal["Seconds", "Minutes", "Hours", "Percentage"]
    actual_value: Decimal
    unit: Literal["Seconds", "Minutes", "Hours", "Percentage"]
    evidence_id: Optional[UUID] = None


class ApprovalIn(Model):
    approval_type: Literal["Go/No-go", "ReadinessException", "Resume", "Business", "RiskReview", "Closure"]
    decision: Literal["Approved", "Rejected", "Conditional"]
    comments: Optional[str] = None
    conditions: Optional[str] = None
    subject_id: Optional[UUID] = None


class ApprovalRequestIn(Model):
    approval_type: str
    approver_user_ids: list[UUID]


class AnnouncementIn(Model):
    message: str = Field(min_length=1, max_length=4000)


class LogIn(Model):
    log_type: Literal["Status", "Decision", "Milestone"]
    message: str = Field(min_length=1, max_length=4000)
    activity_id: Optional[UUID] = None
    occurred_at: Optional[AwareDatetime] = None
    external_reference: Optional[str] = None


class MilestonePatch(Model):
    status: Literal["Pending", "Completed", "Delayed"]
    actual_at: Optional[AwareDatetime] = None


class CloseIn(Model):
    expected_version: Optional[int] = None


