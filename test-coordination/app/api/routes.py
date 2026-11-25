"""REST API (Appendix A, extended with the endpoints the ten screens need)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile
from fastapi.responses import Response

from .. import schemas as sc
from ..ai import service as assistant
from ..security import Ctx, get_ctx
from ..services import audit, calendar, events, evidence, idempotency, people, reports, series, views, work

router = APIRouter()
D = Depends(get_ctx)


def body(m) -> dict:
    return m.model_dump(exclude_unset=True)


# ---- Me / portfolio ----------------------------------------------------------
@router.get("/me", tags=["me"])
def me(ctx: Ctx = D):
    return {"user_id": ctx.me.user_id, "tenant_id": ctx.me.tenant_id, "display_name": ctx.me.display_name,
            "roles": sorted(ctx.me.roles), "is_external": ctx.me.is_external}


@router.get("/users", tags=["directory"], summary="People in this tenant (for assignment pickers)")
def users(ctx: Ctx = D):
    from sqlalchemy import select

    from ..db import t
    if ctx.me.is_external:
        return []
    u = t("users")
    return [dict(r) for r in ctx.conn.execute(
        select(u.c.id, u.c.display_name, u.c.department, u.c.roles, u.c.is_external)
        .where(u.c.is_active).order_by(u.c.display_name)).mappings()]


@router.get("/test-programmes", tags=["programmes"])
def list_programmes(ctx: Ctx = D):
    from sqlalchemy import select

    from ..db import t
    p = t("test_programmes")
    return [dict(r) for r in ctx.conn.execute(select(p).order_by(p.c.start_date.desc())).mappings()]


@router.get("/me/workspace", tags=["me"], summary="Screen 06 — My Workspace")
def my_workspace(ctx: Ctx = D):
    return views.my_workspace(ctx)


@router.get("/portfolio/summary", tags=["portfolio"], summary="Screen 01 — Enterprise Command Centre")
def portfolio(programme_id: Optional[UUID] = None, horizon_days: int = Query(30, ge=1, le=365), ctx: Ctx = D):
    return views.portfolio(ctx, programme_id, horizon_days)


@router.get("/audit/verify", tags=["governance"], summary="Verify the tenant audit hash chain")
def verify_audit(ctx: Ctx = D):
    from ..authz import require_role
    from ..domain.enums import Role
    require_role(ctx, Role.AUDITOR, Role.RISK, Role.ADMIN)
    return audit.verify_tenant_chain(ctx)


# ---- Calendar & scheduling ---------------------------------------------------
@router.get("/calendar", tags=["calendar"], summary="Screen 02 — tests in a window, with conflict flags and blackouts")
def get_calendar(start: datetime, end: datetime, category: Optional[str] = None, criticality: Optional[str] = None,
                 programme_id: Optional[UUID] = None, ctx: Ctx = D):
    return calendar.calendar(ctx, start, end, category=category, criticality=criticality, programme_id=programme_id)


@router.get("/schedule-check", tags=["calendar"], summary="Conflicts a proposed window would create")
def schedule_check(start: datetime, end: datetime, test_manager_user_id: Optional[UUID] = None,
                   business_service_ids: list[UUID] = Query(default=[]), exclude_event_id: Optional[UUID] = None,
                   ctx: Ctx = D):
    from ..errors import Unprocessable
    if end <= start:
        raise Unprocessable("The end must be after the start")
    return events.schedule_conflicts(ctx, start, end, exclude_event=exclude_event_id, manager=test_manager_user_id,
                                     service_ids=business_service_ids or None)


@router.get("/blackout-periods", tags=["calendar"])
def blackouts(ctx: Ctx = D):
    return calendar.list_blackouts(ctx)


@router.post("/blackout-periods", status_code=201, tags=["calendar"])
def create_blackout(data: sc.BlackoutIn, ctx: Ctx = D):
    return calendar.create_blackout(ctx, body(data))


@router.get("/business-services", tags=["directory"])
def business_services(ctx: Ctx = D):
    return calendar.business_services(ctx)


@router.get("/test-templates", tags=["templates"])
def list_templates(ctx: Ctx = D):
    return calendar.templates(ctx)


# ---- Programmes & templates --------------------------------------------------
@router.post("/test-programmes", status_code=201, tags=["programmes"])
def create_programme(data: sc.ProgrammeIn, ctx: Ctx = D):
    return events.create_programme(ctx, body(data))


@router.get("/test-programmes/{programme_id}", tags=["programmes"])
def get_programme(programme_id: UUID, ctx: Ctx = D):
    return events.get_programme(ctx, programme_id)


@router.post("/test-templates", status_code=201, tags=["templates"])
def create_template(data: sc.TemplateIn, ctx: Ctx = D):
    return events.create_template(ctx, body(data))


# ---- Events ------------------------------------------------------------------
@router.post("/test-events", status_code=201, tags=["events"], summary="Register a test")
def create_event(data: sc.EventIn, ctx: Ctx = D, idempotency_key: Optional[str] = Header(default=None)):
    return idempotency.run(ctx, idempotency_key, "create_event", data.model_dump(),
                           lambda: events.create_event(ctx, body(data)))


@router.post("/test-events/series/preview", tags=["events"], summary="Every occurrence of a recurring test, with its clashes")
def preview_series(data: sc.SeriesIn, ctx: Ctx = D):
    return series.preview(ctx, body(data))


@router.post("/test-events/series", status_code=201, tags=["events"], summary="Create a recurring test series")
def create_series(data: sc.SeriesIn, ctx: Ctx = D, idempotency_key: Optional[str] = Header(default=None)):
    return idempotency.run(ctx, idempotency_key, "create_series", data.model_dump(), lambda: series.create(ctx, body(data)))


@router.get("/test-events", tags=["events"], summary="Screens 02/03 — calendar and register")
def list_events(programme_id: Optional[UUID] = None, status: Optional[str] = None, category: Optional[str] = None,
                criticality: Optional[str] = None, start_from: Optional[datetime] = None, start_to: Optional[datetime] = None,
                q: Optional[str] = None, limit: int = Query(200, le=1000), offset: int = 0, ctx: Ctx = D):
    return events.list_events(ctx, programme_id=programme_id, status=status, category=category, criticality=criticality,
                              start_from=start_from, start_to=start_to, q=q, limit=limit, offset=offset)


@router.get("/test-events/{event_id}", tags=["events"], summary="Screen 04 — overview")
def get_event(event_id: UUID, ctx: Ctx = D):
    return events.get_event_overview(ctx, event_id)


@router.patch("/test-events/{event_id}", tags=["events"], summary="Update details or reschedule (versioned)")
def patch_event(event_id: UUID, data: sc.EventPatch, ctx: Ctx = D):
    return events.update_event(ctx, event_id, body(data))


@router.get("/test-events/{event_id}/conflicts", tags=["events"])
def conflicts(event_id: UUID, ctx: Ctx = D):
    from ..services.common import event as load
    ev = load(ctx, event_id)
    return events.schedule_conflicts(ctx, ev["planned_start_at"], ev["planned_end_at"], exclude_event=event_id,
                                     manager=ev["test_manager_user_id"])


@router.post("/test-events/{event_id}/scope", status_code=201, tags=["events"])
def add_scope(event_id: UUID, data: sc.ScopeIn, ctx: Ctx = D):
    return events.add_scope_item(ctx, event_id, body(data))


@router.post("/test-events/{event_id}/transitions", tags=["lifecycle"], summary="Apply a lifecycle action")
def transition(event_id: UUID, data: sc.TransitionIn, ctx: Ctx = D):
    return events.transition(ctx, event_id, data.action, reason=data.reason, expected_version=data.expected_version)


@router.post("/test-events/{event_id}/start", tags=["lifecycle"], summary="Activate live coordination")
def start(event_id: UUID, data: sc.CloseIn = sc.CloseIn(), ctx: Ctx = D):
    return events.transition(ctx, event_id, "start", expected_version=data.expected_version)


@router.put("/test-events/{event_id}/outcome", tags=["closure"])
def set_outcome(event_id: UUID, data: sc.OutcomeIn, ctx: Ctx = D):
    return events.set_outcome(ctx, event_id, data.outcome, data.summary, data.expected_version)


@router.post("/test-events/{event_id}/close", tags=["closure"], summary="Request formal closure")
def close(event_id: UUID, data: sc.CloseIn = sc.CloseIn(), ctx: Ctx = D):
    return events.transition(ctx, event_id, "close", expected_version=data.expected_version)


@router.get("/test-events/{event_id}/closure-report", tags=["closure"])
def closure_report(event_id: UUID, ctx: Ctx = D):
    return events.closure_report(ctx, event_id)


def _file(data: bytes, name: str, media: str) -> Response:
    from urllib.parse import quote
    return Response(content=data, media_type=media, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"})


@router.get("/test-events/{event_id}/closure-report.pdf", tags=["reports"], summary="Closure report as PDF (draft until closed)")
def closure_report_pdf(event_id: UUID, ctx: Ctx = D):
    data, name = reports.closure_pdf(ctx, event_id)
    return _file(data, name, "application/pdf")


@router.get("/reports/programme-status.xlsx", tags=["reports"], summary="Programme status workbook for management packs")
def programme_status_xlsx(programme_id: Optional[UUID] = None, tz: str = "Asia/Kolkata", ctx: Ctx = D):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from ..errors import Unprocessable
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise Unprocessable(f"Unknown time zone '{tz}'") from e
    data, name = reports.programme_xlsx(ctx, programme_id, tz)
    return _file(data, name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---- Assistant (RA-069..072) ---------------------------------------------------
@router.post("/test-events/{event_id}/assistant", status_code=201, tags=["assistant"],
             summary="Generate a grounded suggestion (briefing, forecast, summary or message draft)")
def assistant_generate(event_id: UUID, data: sc.AiGenerateIn, ctx: Ctx = D):
    return assistant.generate(ctx, event_id, data.feature, data.kind)


@router.get("/test-events/{event_id}/assistant", tags=["assistant"], summary="Latest suggestions and their history")
def assistant_list(event_id: UUID, ctx: Ctx = D):
    return assistant.list_for_event(ctx, event_id)


@router.post("/assistant/suggestions/{record_id}/decision", tags=["assistant"],
             summary="Accept (send, for message drafts) or dismiss a suggestion")
def assistant_decide(record_id: UUID, data: sc.AiDecisionIn, ctx: Ctx = D):
    return assistant.decide(ctx, record_id, data.action, feedback=data.feedback, subject=data.subject,
                            intro=data.intro, exclude=data.exclude_recipients)


@router.get("/assistant/log", tags=["assistant"], summary="AI oversight log for programme, risk, audit and admin roles")
def assistant_log(feature: Optional[str] = None, status: Optional[str] = None, limit: int = Query(200, le=1000), ctx: Ctx = D):
    return assistant.oversight_log(ctx, limit, feature, status)


@router.get("/test-events/{event_id}/timeline", tags=["live"])
def timeline(event_id: UUID, ctx: Ctx = D):
    return views.timeline(ctx, event_id)


@router.get("/test-events/{event_id}/communications", tags=["communications"])
def communications(event_id: UUID, ctx: Ctx = D):
    return views.communications(ctx, event_id)


# ---- Stakeholders ------------------------------------------------------------
@router.post("/test-events/{event_id}/stakeholders", status_code=201, tags=["stakeholders"])
def add_stakeholders(event_id: UUID, data: sc.StakeholderIn, ctx: Ctx = D):
    return people.add_stakeholders(ctx, event_id, body(data))


@router.get("/test-events/{event_id}/stakeholders", tags=["stakeholders"])
def list_stakeholders(event_id: UUID, include_history: bool = False, ctx: Ctx = D):
    return people.list_stakeholders(ctx, event_id, include_history)


@router.post("/test-stakeholders/{stakeholder_id}/respond", tags=["stakeholders"])
def respond(stakeholder_id: UUID, data: sc.RespondIn, ctx: Ctx = D):
    return people.respond_to_assignment(ctx, stakeholder_id, data.accept, data.comment)


@router.post("/test-stakeholders/{stakeholder_id}/replace", tags=["stakeholders"])
def replace(stakeholder_id: UUID, data: sc.ReplaceIn, ctx: Ctx = D):
    return people.replace_stakeholder(ctx, stakeholder_id, data.new_user_id, data.reason)


@router.post("/acknowledgements/{ack_id}", tags=["communications"])
def acknowledge(ack_id: UUID, data: sc.RespondIn, ctx: Ctx = D):
    return people.acknowledge(ctx, ack_id, data.accept, data.comment)


# ---- Activities & dependencies ----------------------------------------------
@router.post("/test-events/{event_id}/activities", status_code=201, tags=["activities"])
def create_activity(event_id: UUID, data: sc.ActivityIn, ctx: Ctx = D):
    return work.create_activity(ctx, event_id, body(data))


@router.get("/test-events/{event_id}/activities", tags=["activities"])
def list_activities(event_id: UUID, ctx: Ctx = D):
    return work.list_activities(ctx, event_id)


@router.patch("/test-activities/{activity_id}", tags=["activities"], summary="Update status / reassign")
def patch_activity(activity_id: UUID, data: sc.ActivityPatch, ctx: Ctx = D):
    return work.update_activity(ctx, activity_id, body(data))


@router.post("/test-activities/{activity_id}/review", tags=["activities"])
def review_activity(activity_id: UUID, data: sc.ReviewIn, ctx: Ctx = D):
    return work.review_activity(ctx, activity_id, data.accept, data.comment)


@router.post("/test-activities/{activity_id}/evidence", status_code=201, tags=["evidence"])
def activity_evidence(activity_id: UUID, data: sc.EvidenceIn, ctx: Ctx = D,
                      idempotency_key: Optional[str] = Header(default=None)):
    return idempotency.run(ctx, idempotency_key, f"evidence:{activity_id}", data.model_dump(),
                           lambda: work.submit_evidence(ctx, activity_id=activity_id, data=body(data)))


@router.post("/test-events/{event_id}/dependencies", status_code=201, tags=["dependencies"])
def add_dependency(event_id: UUID, data: sc.DependencyIn, ctx: Ctx = D):
    return work.add_dependency(ctx, event_id, body(data))


@router.get("/test-events/{event_id}/dependencies", tags=["dependencies"])
def dependency_map(event_id: UUID, ctx: Ctx = D):
    return work.dependency_map(ctx, event_id)


# ---- Readiness ---------------------------------------------------------------
@router.post("/test-events/{event_id}/readiness-items", status_code=201, tags=["readiness"])
def create_readiness_item(event_id: UUID, data: sc.ReadinessItemIn, ctx: Ctx = D):
    return work.create_readiness_item(ctx, event_id, body(data))


@router.patch("/test-readiness-items/{item_id}", tags=["readiness"])
def patch_readiness(item_id: UUID, data: sc.ReadinessPatch, ctx: Ctx = D):
    return work.update_readiness_item(ctx, item_id, body(data))


@router.post("/test-events/{event_id}/readiness", tags=["readiness"], summary="Batch readiness update")
def batch_readiness(event_id: UUID, data: sc.ReadinessUpdateIn, ctx: Ctx = D):
    out = []
    for u in data.updates:
        p = sc.ReadinessPatch(**{k: v for k, v in u.items() if k != "id"})
        item = work.update_readiness_item(ctx, UUID(str(u["id"])), body(p))
        if item["event_id"] != event_id:
            from ..errors import Unprocessable
            raise Unprocessable("Readiness item belongs to another event")
        out.append(item)
    return out


@router.get("/test-events/{event_id}/readiness", tags=["readiness"], summary="Screen 07 — readiness centre")
def readiness(event_id: UUID, ctx: Ctx = D):
    return work.readiness_summary(ctx, event_id)


@router.post("/test-readiness-items/{item_id}/evidence", status_code=201, tags=["evidence"])
def readiness_evidence(item_id: UUID, data: sc.EvidenceIn, ctx: Ctx = D):
    return work.submit_evidence(ctx, readiness_item_id=item_id, data=body(data))


EVIDENCE_TYPES = ("Report", "Screenshot", "Log", "Approval", "ExternalRecord", "Other")


async def _upload(ctx: Ctx, file: UploadFile, evidence_type: str, sha256: Optional[str], supersedes_id: Optional[UUID],
                  activity_id: Optional[UUID] = None, readiness_item_id: Optional[UUID] = None):
    from ..config import settings
    from ..errors import Unprocessable
    if evidence_type not in EVIDENCE_TYPES:
        raise Unprocessable(f"evidence_type must be one of {', '.join(EVIDENCE_TYPES)}")
    data = await file.read(settings.evidence_max_mb * 1024 * 1024 + 1)
    return evidence.upload(ctx, activity_id=activity_id, readiness_item_id=readiness_item_id, filename=file.filename or "",
                           data=data, content_type=file.content_type, evidence_type=evidence_type,
                           client_sha256=sha256, supersedes_id=supersedes_id)


@router.post("/test-activities/{activity_id}/evidence/upload", status_code=201, tags=["evidence"],
             summary="Upload an evidence file for an activity (multipart)")
async def upload_activity_evidence(activity_id: UUID, file: UploadFile = File(...), evidence_type: str = Form("Report"),
                                   sha256: Optional[str] = Form(None), supersedes_id: Optional[UUID] = Form(None), ctx: Ctx = D):
    return await _upload(ctx, file, evidence_type, sha256, supersedes_id, activity_id=activity_id)


@router.post("/test-readiness-items/{item_id}/evidence/upload", status_code=201, tags=["evidence"],
             summary="Upload an evidence file for a readiness condition (multipart)")
async def upload_readiness_evidence(item_id: UUID, file: UploadFile = File(...), evidence_type: str = Form("Report"),
                                    sha256: Optional[str] = Form(None), supersedes_id: Optional[UUID] = Form(None), ctx: Ctx = D):
    return await _upload(ctx, file, evidence_type, sha256, supersedes_id, readiness_item_id=item_id)


@router.get("/test-events/{event_id}/evidence", tags=["evidence"], summary="Screen 09 — evidence register")
def evidence_register(event_id: UUID, ctx: Ctx = D):
    return evidence.register(ctx, event_id)


@router.get("/test-evidence/{evidence_id}/download", tags=["evidence"], summary="Download (checksum verified first)")
def download_evidence(evidence_id: UUID, ctx: Ctx = D):
    from urllib.parse import quote
    from fastapi.responses import JSONResponse
    data, item, failure = evidence.download(ctx, evidence_id)
    if failure:
        return JSONResponse(status_code=409, content=failure)
    name = item["filename"] or "evidence"
    # Always served as an attachment with a neutral type, so an uploaded file can never run in the app's origin.
    return Response(content=data, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff", "X-Evidence-SHA256": item["checksum"], "Cache-Control": "no-store"})


@router.post("/test-evidence/{evidence_id}/verify", tags=["evidence"], summary="Re-check a stored file against its checksum")
def verify_evidence(evidence_id: UUID, ctx: Ctx = D):
    return evidence.verify(ctx, evidence_id)


@router.post("/test-evidence/{evidence_id}/review", tags=["evidence"])
def review_evidence(evidence_id: UUID, data: sc.ReviewIn, ctx: Ctx = D):
    return work.review_evidence(ctx, evidence_id, data.accept, data.comment)


# ---- Approvals ---------------------------------------------------------------
@router.post("/test-events/{event_id}/approvals", status_code=201, tags=["approvals"])
def approve(event_id: UUID, data: sc.ApprovalIn, ctx: Ctx = D, idempotency_key: Optional[str] = Header(default=None)):
    return idempotency.run(ctx, idempotency_key, f"approval:{event_id}", data.model_dump(),
                           lambda: work.record_approval(ctx, event_id, body(data)))


@router.post("/test-events/{event_id}/approval-requests", tags=["approvals"])
def request_approval(event_id: UUID, data: sc.ApprovalRequestIn, ctx: Ctx = D):
    return work.request_approval(ctx, event_id, data.approval_type, data.approver_user_ids)


# ---- Live coordination -------------------------------------------------------
@router.post("/test-events/{event_id}/announcements", status_code=201, tags=["live"])
def announce(event_id: UUID, data: sc.AnnouncementIn, ctx: Ctx = D):
    return work.announce(ctx, event_id, data.message)


@router.post("/test-events/{event_id}/logs", status_code=201, tags=["live"])
def add_log(event_id: UUID, data: sc.LogIn, ctx: Ctx = D):
    return work.add_log_entry(ctx, event_id, body(data))


@router.patch("/test-milestones/{milestone_id}", tags=["live"])
def patch_milestone(milestone_id: UUID, data: sc.MilestonePatch, ctx: Ctx = D):
    return work.update_milestone(ctx, milestone_id, body(data))


# ---- Issues, remediation, measurements --------------------------------------
@router.post("/test-events/{event_id}/issues", status_code=201, tags=["findings"])
def create_issue(event_id: UUID, data: sc.IssueIn, ctx: Ctx = D):
    return work.create_issue(ctx, event_id, body(data))


@router.patch("/test-issues/{issue_id}", tags=["findings"])
def patch_issue(issue_id: UUID, data: sc.IssuePatch, ctx: Ctx = D):
    return work.update_issue(ctx, issue_id, body(data))


@router.post("/test-issues/{issue_id}/remediation-actions", status_code=201, tags=["findings"])
def create_remediation(issue_id: UUID, data: sc.RemediationIn, ctx: Ctx = D):
    return work.create_remediation(ctx, issue_id, body(data))


@router.patch("/test-remediation-actions/{action_id}", tags=["findings"])
def patch_remediation(action_id: UUID, data: sc.RemediationPatch, ctx: Ctx = D):
    return work.update_remediation(ctx, action_id, body(data))


@router.post("/test-events/{event_id}/measurements", status_code=201, tags=["measurements"])
def record_measurement(event_id: UUID, data: sc.MeasurementIn, ctx: Ctx = D):
    return work.record_measurement(ctx, event_id, body(data))


@router.post("/test-measurements/{measurement_id}/validate", tags=["measurements"])
def validate_measurement(measurement_id: UUID, ctx: Ctx = D):
    return work.validate_measurement(ctx, measurement_id)
