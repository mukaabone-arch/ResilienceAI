"""Controlled vocabularies for the test-coordination domain.

Values are stored as strings in PostgreSQL (with CHECK constraints in the
migration) so they stay readable in reports and audit records.
"""
from enum import StrEnum


class LifecycleStatus(StrEnum):
    DRAFT = "Draft"
    PLANNED = "Planned"
    MOBILISING = "Mobilising"
    READINESS = "Readiness"
    READY = "Ready"
    LIVE = "Live"
    PAUSED = "Paused"
    CLOSURE = "Closure"
    CLOSED = "Closed"
    CANCELLED = "Cancelled"


class Outcome(StrEnum):
    SUCCESSFUL = "Successful"
    PARTIAL = "Partial"
    FAILED = "Failed"
    ABORTED = "Aborted"
    CANCELLED = "Cancelled"


class Criticality(StrEnum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class TestCategory(StrEnum):
    INFRASTRUCTURE = "Infrastructure"
    APPLICATION = "Application"
    DATABASE = "Database"
    CYBER = "Cyber"
    BUSINESS = "Business"


class ActivityStatus(StrEnum):
    NOT_STARTED = "Not Started"
    IN_PROGRESS = "In Progress"
    BLOCKED = "Blocked"
    SUBMITTED = "Submitted"
    COMPLETED = "Completed"


class ReviewStatus(StrEnum):
    NOT_REQUIRED = "Not Required"
    PENDING = "Pending"
    ACCEPTED = "Accepted"
    REJECTED = "Rejected"


class ReadinessStatus(StrEnum):
    PENDING = "Pending"
    IN_PROGRESS = "In Progress"
    READY = "Ready"
    BLOCKED = "Blocked"
    FAILED = "Failed"


class ApprovalType(StrEnum):
    PLAN = "Plan"                       # Planned -> Mobilising
    GO_NO_GO = "Go/No-go"
    READINESS_EXCEPTION = "ReadinessException"
    RESUME = "Resume"
    BUSINESS = "Business"
    RISK_REVIEW = "RiskReview"
    CLOSURE = "Closure"
    BLACKOUT_OVERRIDE = "BlackoutOverride"


class Decision(StrEnum):
    APPROVED = "Approved"
    REJECTED = "Rejected"
    CONDITIONAL = "Conditional"


class Role(StrEnum):
    """Tenant-level enterprise roles (spec §5)."""
    ADMIN = "EnterpriseProgrammeAdministrator"
    PROGRAMME_MANAGER = "DRProgrammeManager"
    TEST_MANAGER = "TestManager"
    TEAM_LEAD = "TeamLead"
    TECHNICAL = "TechnicalStakeholder"
    BUSINESS_OWNER = "BusinessOwner"
    RISK = "RiskCompliance"
    SENIOR_MGMT = "SeniorManagement"
    VENDOR = "ExternalVendor"
    AUDITOR = "Auditor"
    MODEL_RISK = "ModelRiskCommittee"      # proposes the autonomy policy for automation and AI agents
    CISO = "CISO"                          # final authority: approves or revokes the autonomy policy
