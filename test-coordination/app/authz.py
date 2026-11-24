"""Role-based and event-level authorisation (spec §5, RA-004).

Principles enforced here:
- Users only see events they are authorised for; vendors only explicitly shared ones.
- Activity ownership never implies approval or programme rights.
- Approval permission is policy-driven and separate from management.
- A test manager cannot approve their own event's go/no-go or closure (segregation of duties).
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import and_, select

from .db import t
from .domain.enums import ApprovalType as AT
from .domain.enums import Role
from .errors import Forbidden
from .security import Ctx

POLICY_VERSION = "default-v1"

PORTFOLIO_VIEWERS = (Role.ADMIN, Role.PROGRAMME_MANAGER, Role.RISK, Role.SENIOR_MGMT, Role.AUDITOR)
READ_ONLY_ROLES = {Role.AUDITOR.value, Role.SENIOR_MGMT.value}

# Who may record each approval type. "owner" = the event's accountable test owner.
APPROVAL_POLICY: dict[AT, dict[str, Any]] = {
    AT.PLAN:                {"roles": {Role.PROGRAMME_MANAGER}, "owner": False},
    AT.GO_NO_GO:            {"roles": {Role.PROGRAMME_MANAGER}, "owner": True},
    AT.READINESS_EXCEPTION: {"roles": {Role.PROGRAMME_MANAGER, Role.RISK}, "owner": False},
    AT.RESUME:              {"roles": {Role.PROGRAMME_MANAGER}, "owner": True},
    AT.BUSINESS:            {"roles": {Role.BUSINESS_OWNER}, "owner": False},
    AT.RISK_REVIEW:         {"roles": {Role.RISK}, "owner": False},
    AT.CLOSURE:             {"roles": {Role.PROGRAMME_MANAGER}, "owner": True},
    AT.BLACKOUT_OVERRIDE:   {"roles": {Role.PROGRAMME_MANAGER}, "owner": False},
}
# Critical events: go/no-go and closure need the programme manager, not just the owner.
CRITICAL_REQUIRES_ROLE = {AT.GO_NO_GO, AT.CLOSURE}


def require_role(ctx: Ctx, *roles: Role) -> None:
    if not ctx.me.has(*roles):
        raise Forbidden(f"Requires one of roles: {', '.join(r.value for r in roles)}")


def require_writer(ctx: Ctx) -> None:
    if ctx.me.roles and ctx.me.roles <= READ_ONLY_ROLES:
        raise Forbidden("Read-only role")


def is_stakeholder(ctx: Ctx, event_id) -> bool:
    s = t("test_stakeholders")
    return ctx.conn.execute(select(s.c.id).where(and_(
        s.c.event_id == event_id, s.c.user_id == ctx.me.user_id, s.c.is_active))).first() is not None


def can_view_event(ctx: Ctx, event: Mapping) -> bool:
    if ctx.me.is_external:
        return is_stakeholder(ctx, event["id"])
    if ctx.me.has(*PORTFOLIO_VIEWERS):
        return True
    if ctx.me.user_id in (event["owner_user_id"], event["test_manager_user_id"]):
        return True
    return is_stakeholder(ctx, event["id"])


def require_view(ctx: Ctx, event: Mapping) -> None:
    if not can_view_event(ctx, event):
        # 404 rather than 403 so event existence is not disclosed.
        from .errors import NotFound
        raise NotFound("Test event not found")


def can_manage_event(ctx: Ctx, event: Mapping) -> bool:
    if ctx.me.is_external:
        return False
    return ctx.me.has(Role.PROGRAMME_MANAGER) or ctx.me.user_id in (
        event["owner_user_id"], event["test_manager_user_id"])


def require_manage(ctx: Ctx, event: Mapping) -> None:
    require_view(ctx, event)
    if not can_manage_event(ctx, event):
        raise Forbidden("Only the event's test manager, owner or a programme manager can do this")


def require_capability(ctx: Ctx, capability: str, event: Mapping) -> None:
    if capability in ("event.manage", "event.close"):
        require_manage(ctx, event)
    elif capability == "programme.approve":
        require_view(ctx, event)
        require_role(ctx, Role.PROGRAMME_MANAGER)
    else:
        raise Forbidden(f"Unknown capability {capability}")


def require_approver(ctx: Ctx, approval_type: AT, event: Mapping) -> None:
    require_view(ctx, event)
    if ctx.me.is_external:
        raise Forbidden("External users cannot approve")
    pol = APPROVAL_POLICY[approval_type]
    by_role = ctx.me.has(*pol["roles"])
    by_owner = pol["owner"] and ctx.me.user_id == event["owner_user_id"]
    if event["criticality"] == "Critical" and approval_type in CRITICAL_REQUIRES_ROLE:
        by_owner = False
    if not (by_role or by_owner):
        raise Forbidden(f"Not authorised to record a {approval_type} decision (policy {POLICY_VERSION})")
    if approval_type in (AT.GO_NO_GO, AT.CLOSURE, AT.RESUME) and ctx.me.user_id == event["test_manager_user_id"]:
        raise Forbidden("The event's test manager cannot approve their own event (segregation of duties)")
