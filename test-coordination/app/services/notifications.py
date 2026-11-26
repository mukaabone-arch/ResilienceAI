"""Notification engine (spec §21, RA-041..048).

Trigger -> policy evaluation -> recipient resolution -> message generation ->
delivery -> acknowledgement tracking -> escalation.

Communications and their delivery jobs are written in the SAME transaction as
the business change (transactional outbox). A separate dispatcher delivers
jobs through channel adapters with retry, backoff and idempotency keys, so a
retried business request never sends twice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .. import config
from ..db import t, tenant_transaction
from ..security import Ctx

# Catalogue: trigger -> (communication_type, channels, priority). Mirrors §21.
CATALOGUE: dict[str, tuple[str, tuple[str, ...], str]] = {
    "stakeholder_assigned":   ("Invitation", ("Teams", "Email"), "Action"),
    "ack_reminder":           ("Reminder", ("Teams", "Email"), "Action"),
    "ack_sla_exceeded":       ("Escalation", ("Teams", "Email"), "Escalation"),
    "activity_overdue":       ("Escalation", ("Teams", "Email"), "Escalation"),
    "dependency_blocked":     ("Notification", ("Teams", "Portal"), "High"),
    "schedule_changed":       ("ScheduleChange", ("Outlook", "Email"), "High"),
    "approval_requested":     ("ApprovalRequest", ("Portal", "Teams"), "Action"),
    "critical_issue":         ("Escalation", ("Teams", "Email"), "Critical"),
    "test_started":           ("Announcement", ("Teams", "Portal"), "Informational"),
    "announcement":           ("Announcement", ("Teams", "Portal"), "Informational"),
    "test_closed":            ("Announcement", ("Email", "Portal"), "Informational"),
}


def enqueue(ctx: Ctx, trigger: str, *, event: dict, recipients: list[UUID], subject: str,
            body: str, dedupe: str, activity_id: UUID | None = None, requires_ack: bool = False,
            stakeholder_by_user: dict[UUID, UUID] | None = None, trigger_type: str = "Event-driven",
            actor: UUID | None | str = "me") -> UUID | None:
    """Create a communication + per-recipient/per-channel jobs. `dedupe` makes it idempotent."""
    recipients = list(dict.fromkeys(r for r in recipients if r))
    if not recipients:
        return None
    ctype, channels, priority = CATALOGUE[trigger]
    u = t("users")
    prefs = {r.id: set(r.notification_channels or []) for r in ctx.conn.execute(
        select(u.c.id, u.c.notification_channels).where(u.c.id.in_(recipients)))}

    comm_id = ctx.conn.execute(pg_insert(t("test_communications")).values(
        tenant_id=ctx.me.tenant_id, event_id=event["id"], activity_id=activity_id,
        communication_type=ctype, subject=subject[:250], content_reference=f"tmpl:{trigger}:v1",
        trigger_type=trigger_type, requires_ack=requires_ack, dedupe_key=f"{trigger}:{dedupe}"[:200],
        created_by=ctx.me.user_id if actor == "me" else actor,
    ).on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_key"])
     .returning(t("test_communications").c.id)).scalar()
    if comm_id is None:
        return None  # already enqueued for this business event

    link = f"{config.settings.portal_base_url.rstrip('/')}/tests/{event['id']}" + ("?tab=activities" if activity_id else "")
    for r in recipients:
        # Portal is always on; other channels honour user preference unless Critical/Escalation.
        mine = prefs.get(r, set())
        chosen = [c for c in channels if c == "Portal" or priority in ("Critical", "Escalation")
                  or c in mine or (c == "Outlook" and "Email" in mine)] or ["Portal"]
        for ch in chosen:
            ctx.conn.execute(insert(t("notification_jobs")).values(
                tenant_id=ctx.me.tenant_id, communication_id=comm_id, recipient_user_id=r, channel=ch,
                idempotency_key=f"{comm_id}:{r}:{ch}",
                payload={"subject": subject, "body": body, "link": link, "priority": priority,
                         "event_code": event.get("event_code"), "trigger": trigger,
                         # Outlook: a stable iCal UID per event means updates replace, never duplicate.
                         "ical_uid": f"{event['id']}@resilienceai" if ch == "Outlook" else None,
                         "starts_at": str(event.get("planned_start_at")),
                         "ends_at": str(event.get("planned_end_at"))}))
    if requires_ack:
        due = datetime.now(timezone.utc) + timedelta(hours=config.settings.ack_sla_hours)
        for r in recipients:
            ctx.conn.execute(insert(t("test_acknowledgements")).values(
                tenant_id=ctx.me.tenant_id, communication_id=comm_id, recipient_user_id=r,
                stakeholder_id=(stakeholder_by_user or {}).get(r), due_at=due))
    return comm_id


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
class ChannelAdapter(Protocol):
    def send(self, channel: str, recipient: dict, payload: dict, idempotency_key: str) -> str:
        """Deliver and return the provider message id. Raise on failure."""


class SimulatedAdapter:
    """Default for dev and the vertical prototype ("communication simulation", §32)."""
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, channel, recipient, payload, idempotency_key):
        self.sent.append((channel, recipient.get("email") or str(recipient["id"]), payload["subject"]))
        return f"sim-{channel.lower()}-{abs(hash(idempotency_key)) % 10**10}"


# Explicit overrides (tests); otherwise NOTIFY_MODE picks simulated or Microsoft 365 adapters (app/services/m365.py).
ADAPTERS: dict[str, ChannelAdapter] = {}
_default = SimulatedAdapter()


def adapter_for(channel: str) -> ChannelAdapter:
    if channel in ADAPTERS:
        return ADAPTERS[channel]
    if config.settings.notify_mode == "m365":
        from . import m365
        return m365.adapter(channel)
    return _default


def _enrich(conn, job: dict) -> dict:
    """What a channel needs beyond the stored payload: the one-tap acknowledgement link, and for calendar
    invitations the provider id of the invitation already sent to this person (so a change updates it)."""
    payload = dict(job["payload"])
    ack = t("test_acknowledgements")
    ack_id = conn.execute(select(ack.c.id).where(and_(
        ack.c.communication_id == job["communication_id"], ack.c.recipient_user_id == job["recipient_user_id"],
        ack.c.status == "Pending"))).scalar()
    if ack_id:
        payload["ack_url"] = f"{config.settings.portal_base_url.rstrip('/')}/acknowledge/{ack_id}"
    if job["channel"] == "Outlook" and payload.get("ical_uid"):
        nj = t("notification_jobs")
        payload["previous_provider_id"] = conn.execute(
            select(nj.c.provider_message_id).where(and_(
                nj.c.recipient_user_id == job["recipient_user_id"], nj.c.channel == "Outlook",
                nj.c.delivery_status == "Sent", nj.c.payload["ical_uid"].astext == payload["ical_uid"],
                nj.c.id != job["id"])).order_by(nj.c.sent_at.desc()).limit(1)).scalar()
    return payload


@dataclass
class DispatchResult:
    sent: int = 0
    failed: int = 0
    dead: int = 0


def dispatch_due(tenant_id: UUID, now: datetime | None = None, batch: int = 100) -> DispatchResult:
    now = now or datetime.now(timezone.utc)
    out = DispatchResult()
    nj, u = t("notification_jobs"), t("users")
    with tenant_transaction(tenant_id) as conn:
        jobs = conn.execute(
            select(nj).where(and_(nj.c.delivery_status.in_(["Queued", "Failed"]), nj.c.next_attempt_at <= now))
            .order_by(nj.c.created_at).limit(batch).with_for_update(skip_locked=True)).mappings().all()
        for j in jobs:
            rec = conn.execute(select(u.c.id, u.c.email, u.c.display_name, u.c.idp_subject, u.c.idp_bound_at)
                               .where(u.c.id == j["recipient_user_id"])).mappings().one()
            attempts = j["attempts"] + 1
            try:
                if j["channel"] == "Portal":
                    msg_id = f"portal-{j['id']}"   # in-app inbox reads communications directly
                else:
                    msg_id = adapter_for(j["channel"]).send(j["channel"], dict(rec), _enrich(conn, j), j["idempotency_key"])
                conn.execute(update(nj).where(nj.c.id == j["id"]).values(
                    delivery_status="Sent", attempts=attempts, provider_message_id=msg_id, sent_at=now, last_error=None))
                out.sent += 1
            except Exception as e:  # noqa: BLE001 - adapter boundary
                dead = attempts >= config.settings.notification_max_attempts
                conn.execute(update(nj).where(nj.c.id == j["id"]).values(
                    delivery_status="Dead" if dead else "Failed", attempts=attempts,
                    last_error=str(e)[:1000], next_attempt_at=now + timedelta(seconds=30 * 2 ** attempts)))
                out.dead += dead
                out.failed += not dead
    return out


def dispatch_all_tenants(now: datetime | None = None) -> DispatchResult:
    """Worker entry point: enumerate tenants with the BYPASSRLS worker role, deliver per tenant."""
    from sqlalchemy import text
    from ..db import worker_engine
    total = DispatchResult()
    with worker_engine().connect() as c:
        ids = [r[0] for r in c.execute(text(
            "SELECT DISTINCT tenant_id FROM notification_jobs WHERE delivery_status IN ('Queued','Failed')"))]
    for tid in ids:
        r = dispatch_due(tid, now)
        total.sent += r.sent
        total.failed += r.failed
        total.dead += r.dead
    return total
