"""Microsoft 365 delivery for queued notifications (NOTIFY_MODE=m365).

- Email   -> Outlook mail sent through Microsoft Graph from one ResilienceAI mailbox (app-only, Mail.Send).
- Outlook -> a calendar invitation for the test window, created in that mailbox's calendar with the person as
             attendee (Calendars.ReadWrite). A later change to the window updates the same invitation.
- Teams   -> an Adaptive Card posted to a Teams Workflows webhook. The bank's flow sends it to the person's
             chat with the Workflows bot (or to a channel). No inbound connection to the bank is needed.

Every message that asks for an acknowledgement carries an "Acknowledge" button that opens the one-tap
acknowledgement page. Opening a link never acknowledges anything: mail scanners follow links, people don't.
"""
from __future__ import annotations

import html
import threading
import time
from datetime import datetime, timezone

import httpx

from .. import config

_token_lock = threading.Lock()
_token: dict = {"value": None, "expires_at": 0.0}


def _client() -> httpx.Client:
    return httpx.Client(timeout=15.0)


class DeliveryError(RuntimeError):
    pass


def _check(r: httpx.Response, what: str) -> httpx.Response:
    if r.status_code >= 400:
        detail = r.text[:300].replace("\n", " ")
        raise DeliveryError(f"{what} failed ({r.status_code}): {detail}")
    return r


def graph_token() -> str:
    """App-only token for Microsoft Graph (client credentials), cached until shortly before it expires."""
    s = config.settings
    with _token_lock:
        if _token["value"] and time.time() < _token["expires_at"] - 120:
            return _token["value"]
        with _client() as c:
            r = _check(c.post(f"{s.entra_authority_host.rstrip('/')}/{s.graph_tenant_id}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": s.graph_client_id,
                "client_secret": s.graph_client_secret, "scope": "https://graph.microsoft.com/.default"}),
                "Getting a Microsoft Graph token")
        body = r.json()
        _token.update(value=body["access_token"], expires_at=time.time() + int(body.get("expires_in", 3600)))
        return _token["value"]


def _graph(method: str, path: str, json: dict) -> httpx.Response:
    s = config.settings
    with _client() as c:
        return c.request(method, f"{s.graph_base_url.rstrip('/')}{path}", json=json,
                         headers={"Authorization": f"Bearer {graph_token()}"})


# ---- Content: one rendering for mail, calendar and Teams ----

def _when(payload: dict) -> str | None:
    try:
        start = datetime.fromisoformat(str(payload["starts_at"]))
        end = datetime.fromisoformat(str(payload["ends_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    return f"{start.astimezone(timezone.utc):%d %b %Y, %H:%M} to {end.astimezone(timezone.utc):%H:%M} UTC"


def _facts(payload: dict) -> list[tuple[str, str]]:
    facts = [("Test", payload.get("event_code") or "")]
    if (w := _when(payload)):
        facts.append(("Window", w))
    facts.append(("Priority", payload.get("priority") or ""))
    return [(k, v) for k, v in facts if v]


def _primary_action(payload: dict) -> tuple[str, str]:
    if payload.get("ack_url"):
        return "Acknowledge", payload["ack_url"]
    return "Open in ResilienceAI", payload["link"]


def render_html(recipient: dict, payload: dict) -> str:
    label, url = _primary_action(payload)
    esc = html.escape
    rows = "".join(f'<tr><td style="color:#6f7a88;padding:2px 16px 2px 0">{esc(k)}</td><td>{esc(v)}</td></tr>'
                   for k, v in _facts(payload))
    second = (f'<p style="margin:16px 0 0"><a href="{esc(payload["link"])}" style="color:#134e5e">Open the test</a></p>'
              if payload.get("ack_url") else "")
    return f"""<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#16233b;max-width:560px">
<p>Hello {esc(recipient.get("display_name") or "")},</p>
<p style="white-space:pre-line">{esc(payload.get("body") or "")}</p>
<table style="font-size:13px;margin:12px 0">{rows}</table>
<p style="margin:20px 0 0"><a href="{esc(url)}" style="background:#134e5e;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600">{esc(label)}</a></p>
{second}
<p style="color:#6f7a88;font-size:12px;margin-top:28px">Sent by ResilienceAI. This message records a test coordination
action; it does not change any system.</p></div>"""


def adaptive_card(recipient: dict, payload: dict) -> dict:
    name = recipient.get("display_name") or ""
    # Mention by Entra object id once the person has signed in, otherwise by their work email (UPN).
    mention_id = recipient.get("idp_subject") if recipient.get("idp_bound_at") else recipient.get("email")
    urgent = payload.get("priority") in ("Critical", "Escalation")
    label, url = _primary_action(payload)
    actions = [{"type": "Action.OpenUrl", "title": label, "url": url, "style": "positive"}]
    if payload.get("ack_url"):
        actions.append({"type": "Action.OpenUrl", "title": "Open the test", "url": payload["link"]})
    return {
        "type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4",
        "msteams": {"width": "Full", "entities": [
            {"type": "mention", "text": f"<at>{name}</at>", "mentioned": {"id": mention_id, "name": name}}]},
        "body": [
            {"type": "Container", "style": "attention" if urgent else "emphasis", "bleed": True, "items": [
                {"type": "TextBlock", "text": payload.get("subject") or "", "weight": "Bolder", "size": "Medium", "wrap": True}]},
            {"type": "TextBlock", "text": f"<at>{name}</at> {payload.get('body') or ''}", "wrap": True, "spacing": "Medium"},
            {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in _facts(payload)]},
        ],
        "actions": actions,
    }


# ---- Adapters ----

class GraphMailAdapter:
    def send(self, channel, recipient, payload, idempotency_key):
        if not recipient.get("email"):
            raise DeliveryError("Recipient has no email address")
        s = config.settings
        _check(_graph("POST", f"/users/{s.graph_sender}/sendMail", {
            "message": {
                "subject": payload.get("subject") or "ResilienceAI",
                "body": {"contentType": "HTML", "content": render_html(recipient, payload)},
                "toRecipients": [{"emailAddress": {"address": recipient["email"], "name": recipient.get("display_name")}}],
                "importance": "high" if payload.get("priority") in ("Critical", "Escalation", "Action") else "normal",
                "internetMessageHeaders": [{"name": "X-ResilienceAI-Key", "value": idempotency_key[:200]}],
            },
            "saveToSentItems": False}), "Sending Outlook mail")
        return f"graph-mail:{idempotency_key}"


class GraphCalendarAdapter:
    """One invitation per person per test, held in the sender mailbox's calendar; updates keep the same event."""

    def send(self, channel, recipient, payload, idempotency_key):
        if not recipient.get("email"):
            raise DeliveryError("Recipient has no email address")
        s = config.settings

        def at(v: str) -> dict:
            d = datetime.fromisoformat(str(v)).astimezone(timezone.utc)
            return {"dateTime": d.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}

        body = {"subject": payload.get("subject") or "Resilience test",
                "body": {"contentType": "HTML", "content": render_html(recipient, payload)},
                "start": at(payload["starts_at"]), "end": at(payload["ends_at"]),
                "showAs": "busy", "isReminderOn": True, "reminderMinutesBeforeStart": 60,
                "attendees": [{"type": "required", "emailAddress": {
                    "address": recipient["email"], "name": recipient.get("display_name")}}]}
        prev = payload.get("previous_provider_id") or ""
        if prev.startswith("graph-event:"):
            r = _graph("PATCH", f"/users/{s.graph_sender}/events/{prev.split(':', 1)[1]}", body)
            if r.status_code != 404:            # deleted in Outlook: fall through and send a fresh invitation
                _check(r, "Updating the calendar invitation")
                return prev
        r = _check(_graph("POST", f"/users/{s.graph_sender}/events",
                          {**body, "transactionId": idempotency_key[:200]}), "Sending the calendar invitation")
        return f"graph-event:{r.json()['id']}"


class TeamsWorkflowAdapter:
    def send(self, channel, recipient, payload, idempotency_key):
        url = config.settings.teams_webhook_url
        if not url:
            return "teams-not-configured"     # Teams is optional; mail still goes out
        with _client() as c:
            _check(c.post(url, json={
                "type": "message",
                "recipient": recipient.get("email"),          # the flow posts to this person's chat
                "summary": payload.get("subject"),
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
                                 "content": adaptive_card(recipient, payload)}],
            }), "Posting to Teams")
        return f"teams:{idempotency_key}"


_ADAPTERS = {"Email": GraphMailAdapter(), "Outlook": GraphCalendarAdapter(), "Teams": TeamsWorkflowAdapter()}


def adapter(channel: str):
    try:
        return _ADAPTERS[channel]
    except KeyError:
        raise DeliveryError(f"No Microsoft 365 delivery for channel {channel}") from None
