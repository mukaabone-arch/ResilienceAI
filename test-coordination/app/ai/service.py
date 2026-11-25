"""Assistant service (RA-069..072): generate grounded suggestions, log them, and record what people do with them.

The assistant reads; it never changes a test. The one thing a suggestion can lead
to is a message, and only when a test manager presses Send, attributed to them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .. import authz
from ..db import t, tenant_transaction
from ..domain.enums import Role
from ..errors import Conflict, Forbidden, NotFound, Unprocessable
from ..security import Ctx
from ..services import audit, notifications
from ..services.common import event as load_event
from ..services.common import log, now
from . import compose, facts
from .provider import PROMPT_VERSION, provider

FEATURES = ("blocker_briefing", "slip_forecast", "status_summary", "message_draft")
OVERSIGHT = (Role.ADMIN, Role.PROGRAMME_MANAGER, Role.RISK, Role.AUDITOR)
TRIGGER_FOR_KIND = {"reminder": "ack_reminder", "escalation": "ack_sla_exceeded",
                    "readiness_update": "announcement", "go_request": "approval_requested"}


def _require_internal(ctx: Ctx) -> None:
    if ctx.me.is_external:
        raise Forbidden("The assistant isn't available to external users")


def _build(ctx: Ctx, ev: dict, feature: str, kind: str | None) -> tuple[dict, list[dict], list[str], str]:
    """Returns (content, sources, names in play, text the provider may rewrite)."""
    if feature in ("blocker_briefing", "slip_forecast", "message_draft"):
        rf = facts.readiness_facts(ctx, ev)
        names = [n for n in [rf["event"]["test_manager"], rf["event"]["owner"]] + [x.get("owner") for k in ("blocked", "overdue", "unmet", "issues") for x in rf[k]] + rf["pending_acks"] if n]
        if feature == "blocker_briefing":
            c = compose.blocker_briefing(rf)
            return c, rf["sources"], names, c["text"]
        if feature == "slip_forecast":
            c = compose.slip_forecast(rf)
            return c, rf["sources"], names, c["text"]
        people = facts.people_for_messages(ctx, ev)
        c = compose.message_draft(kind or "", rf, people, ctx.me.display_name)
        if not c["recipients"]:
            raise Unprocessable("Nobody needs this message right now: there are no recipients for it.")
        return c, rf["sources"], names + [r["name"] for r in c["recipients"]], c["intro"]
    lf = facts.live_facts(ctx, ev)
    c = compose.status_summary(lf)
    names = [a["owner"] for a in lf["activities"] if a["owner"]] + [x["actor"] for x in lf["logs"] if x["actor"]]
    return c, lf["sources"], names, c["text"]


def _create(ctx: Ctx, ev: dict, feature: str, kind: str | None = None, slot_key: str | None = None,
            requested_by: UUID | None = None) -> dict | None:
    content, sources, names, rewritable = _build(ctx, ev, feature, kind)
    res = provider().rewrite(feature, rewritable, names)
    if feature == "message_draft":
        content["intro"] = res.text
    else:
        content["text"] = res.text
    ai = t("ai_activity_records")
    # A new suggestion replaces the open one of the same kind for this test.
    open_same = and_(ai.c.event_id == ev["id"], ai.c.feature == feature, ai.c.status == "Proposed")
    if feature == "message_draft":
        open_same = and_(open_same, ai.c.content["kind"].astext == kind)
    values = dict(tenant_id=ctx.me.tenant_id, event_id=ev["id"], feature=feature, content=jsonable_encoder(content),
                  sources=sources, provider=res.provider, model=res.model, prompt_version=PROMPT_VERSION,
                  fallback_reason=res.fallback_reason, slot_key=slot_key, requested_by=requested_by)
    if slot_key:
        stmt = pg_insert(ai).values(**values).on_conflict_do_nothing(
            index_elements=["tenant_id", "event_id", "feature", "slot_key"], index_where=ai.c.slot_key.isnot(None)).returning(ai)
        rec = ctx.conn.execute(stmt).mappings().first()
        if rec is None:
            return None
        ctx.conn.execute(update(ai).where(and_(open_same, ai.c.id != rec["id"])).values(status="Superseded"))
    else:
        ctx.conn.execute(update(ai).where(open_same).values(status="Superseded"))
        rec = ctx.conn.execute(insert(ai).values(**values).returning(ai)).mappings().one()
    audit.record(ctx, "ai.suggested", "AiSuggestion", rec["id"], event_id=ev["id"],
                 new={"feature": feature, "kind": kind, "provider": res.provider, "fallback": res.fallback_reason,
                      "sources": len(sources)}, actor=requested_by)
    return dict(rec)


def generate(ctx: Ctx, event_id: UUID, feature: str, kind: str | None = None) -> dict:
    _require_internal(ctx)
    if feature not in FEATURES:
        raise Unprocessable(f"Unknown assistant feature '{feature}'")
    ev = load_event(ctx, event_id)
    if feature == "message_draft":
        if not authz.can_manage_event(ctx, ev):
            raise Forbidden("Only the test manager, owner or a programme manager can draft messages to the test's people")
        if kind not in compose.KINDS:
            raise Unprocessable(f"kind must be one of {', '.join(compose.KINDS)}")
        if ev["lifecycle_status"] in ("Closed", "Cancelled"):
            raise Conflict("This test is closed; there is nobody to chase")
        if kind == "go_request" and not (ev["lifecycle_status"] == "Ready"):
            raise Conflict("A go request makes sense once the test is marked ready")
    return _create(ctx, ev, feature, kind, requested_by=ctx.me.user_id)


def list_for_event(ctx: Ctx, event_id: UUID) -> dict:
    _require_internal(ctx)
    load_event(ctx, event_id)
    ai, u = t("ai_activity_records"), t("users")
    rows = [dict(r) for r in ctx.conn.execute(
        select(ai, u.c.display_name.label("decided_by_name")).outerjoin(u, u.c.id == ai.c.decided_by)
        .where(ai.c.event_id == event_id).order_by(ai.c.created_at.desc()).limit(100)).mappings()]
    latest: dict[str, dict] = {}
    for r in rows:
        key = r["feature"] + (":" + r["content"].get("kind", "") if r["feature"] == "message_draft" else "")
        if key not in latest and r["status"] != "Superseded":
            latest[key] = r
    return {"latest": latest, "history": rows}


def decide(ctx: Ctx, record_id: UUID, action: str, *, feedback: str | None = None,
           subject: str | None = None, intro: str | None = None, exclude: list[UUID] | None = None) -> dict:
    _require_internal(ctx)
    ai = t("ai_activity_records")
    rec = ctx.conn.execute(select(ai).where(ai.c.id == record_id).with_for_update()).mappings().first()
    if rec is None:
        raise NotFound("Suggestion not found")
    ev = load_event(ctx, rec["event_id"])
    if rec["status"] != "Proposed":
        raise Conflict(f"This suggestion was already {rec['status'].lower()}")
    if action not in ("accept", "dismiss"):
        raise Unprocessable("action must be accept or dismiss")
    if action == "dismiss" and not (feedback and feedback.strip()):
        raise Unprocessable("Say briefly why it wasn't useful; it helps improve the assistant")

    values: dict = {"decided_by": ctx.me.user_id, "decided_at": now(), "feedback": feedback}
    sent = 0
    if rec["feature"] == "message_draft" and action == "accept":
        if not authz.can_manage_event(ctx, ev):
            raise Forbidden("Only the test manager, owner or a programme manager can send this")
        draft = rec["content"]
        edited = (subject is not None and subject != draft["subject"]) or (intro is not None and intro != draft["intro"])
        skip = {str(x) for x in (exclude or [])}
        recipients = [r for r in draft["recipients"] if str(r["user_id"]) not in skip]
        if not recipients:
            raise Unprocessable("Choose at least one recipient")
        for r in recipients:
            subj, body = compose.render_message(draft, r, intro=intro, subject=subject)
            if notifications.enqueue(ctx, TRIGGER_FOR_KIND[draft["kind"]], event=ev, recipients=[r["user_id"]],
                                     subject=subj, body=body, dedupe=f"ai:{record_id}:{r['user_id']}", trigger_type="Manual"):
                sent += 1
        values["status"] = "Edited" if edited else "Accepted"
        values["final_content"] = {"subject": subject or draft["subject"], "intro": intro if intro is not None else draft["intro"],
                                   "recipients": [r["name"] for r in recipients]}
        log(ctx, ev["id"], "Announcement" if draft["kind"] == "readiness_update" else "Status",
            f"Message sent to {sent} {'person' if sent == 1 else 'people'}: {values['final_content']['subject']} "
            f"(drafted by the assistant{', edited' if edited else ''})")
    else:
        values["status"] = "Accepted" if action == "accept" else "Dismissed"
    new = dict(ctx.conn.execute(update(ai).where(ai.c.id == record_id).values(**values).returning(ai)).mappings().one())
    audit.record(ctx, f"ai.{new['status'].lower()}", "AiSuggestion", record_id, event_id=ev["id"],
                 new={"feature": rec["feature"], "feedback": feedback, "sent": sent})
    return {**new, "sent": sent}


def oversight_log(ctx: Ctx, limit: int = 200, feature: str | None = None, status: str | None = None) -> list[dict]:
    authz.require_role(ctx, *OVERSIGHT)
    ai, u, e = t("ai_activity_records"), t("users"), t("test_events")
    req, dec = u.alias("req"), u.alias("dec")
    q = (select(ai.c.id, ai.c.feature, ai.c.status, ai.c.provider, ai.c.model, ai.c.prompt_version, ai.c.fallback_reason,
                ai.c.created_at, ai.c.decided_at, ai.c.feedback, ai.c.slot_key, ai.c.content["headline"].astext.label("headline"),
                ai.c.content["subject"].astext.label("subject"), e.c.event_code, e.c.title.label("event_title"), e.c.id.label("event_id"),
                req.c.display_name.label("requested_by"), dec.c.display_name.label("decided_by"))
         .join(e, e.c.id == ai.c.event_id).outerjoin(req, req.c.id == ai.c.requested_by).outerjoin(dec, dec.c.id == ai.c.decided_by))
    if feature:
        q = q.where(ai.c.feature == feature)
    if status:
        q = q.where(ai.c.status == status)
    return [dict(r) for r in ctx.conn.execute(q.order_by(ai.c.created_at.desc()).limit(min(limit, 1000))).mappings()]


def run_live_summaries(tenant_id: UUID, at: datetime | None = None, every_minutes: int = 30) -> int:
    """Scheduled job: one executive summary per live test per slot. Safe to run repeatedly."""
    from ..services.people import _system_ctx
    at = at or datetime.now(timezone.utc)
    slot = at.replace(minute=(at.minute // every_minutes) * every_minutes, second=0, microsecond=0)
    made = 0
    with tenant_transaction(tenant_id) as conn:
        ctx = _system_ctx(conn, tenant_id)
        e = t("test_events")
        for ev in conn.execute(select(e).where(e.c.lifecycle_status.in_(["Live", "Paused"]))).mappings():
            if _create(ctx, dict(ev), "status_summary", slot_key=slot.isoformat()):
                made += 1
    return made
