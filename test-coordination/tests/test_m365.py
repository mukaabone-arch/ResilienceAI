"""Microsoft 365 delivery (NOTIFY_MODE=m365) and the one-tap acknowledgement page.

Microsoft Graph and the Teams Workflows webhook are replaced by a recording stand-in (httpx.MockTransport).
"""
from __future__ import annotations

import json
import urllib.parse
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app import config
from app.db import worker_engine
from app.services import m365, notifications

T0 = datetime(2027, 10, 4, 4, 0, tzinfo=timezone.utc)
WEBHOOK = "https://prod-00.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=x"


class FakeMicrosoft:
    """Answers Graph and the Teams webhook the way they would, and records every request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.fail_teams_for: dict[str, int] = {}   # recipient email -> failures to return
        self.events = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "graph-token", "expires_in": 3599, "token_type": "Bearer"})
        if url.startswith(WEBHOOK.split("?")[0]):
            who = json.loads(request.content).get("recipient")
            if self.fail_teams_for.get(who):
                self.fail_teams_for[who] -= 1
                return httpx.Response(500, text="flow failed")
            return httpx.Response(202)
        assert request.headers["authorization"] == "Bearer graph-token"
        if url.endswith("/sendMail"):
            return httpx.Response(202)
        if request.method == "POST" and url.endswith("/events"):
            self.events += 1
            return httpx.Response(201, json={"id": f"AAMkEvent{self.events}"})
        if request.method == "PATCH" and "/events/" in url:
            return httpx.Response(200, json={"id": url.rsplit("/", 1)[1]})
        return httpx.Response(404, text="not faked")

    def bodies(self, contains: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if contains in str(r.url) and r.content]


@pytest.fixture
def ms(monkeypatch):
    fake = FakeMicrosoft()
    monkeypatch.setattr(m365, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setitem(m365._token, "value", None)
    monkeypatch.setitem(m365._token, "expires_at", 0.0)
    monkeypatch.setattr(config, "settings", replace(
        config.settings, notify_mode="m365", graph_tenant_id="7d1f3c2a-5b6e-4f80-9a1b-2c3d4e5f6a7b",
        graph_client_id="graph-app", graph_client_secret="s3cret", graph_sender="resilience@bank.example",
        teams_webhook_url=WEBHOOK, portal_base_url="https://resilience.bank.example"))
    return fake


@pytest.fixture
def ev(as_, seeded):
    ids = seeded["A"]
    pm = as_("arjun.pm")
    prog = pm.post("/test-programmes", expect=201, json={
        "name": "M365 programme", "programme_code": f"M{uuid.uuid4().hex[:6].upper()}", "financial_year": "FY2028",
        "start_date": "2027-04-01", "end_date": "2028-03-31"}).json()
    e = pm.post("/test-events", expect=201, json={
        "programme_id": prog["id"], "title": "Notification drill", "category": "Database", "criticality": "High",
        "planned_start_at": T0.isoformat(), "planned_end_at": (T0 + timedelta(hours=3)).isoformat(),
        "test_manager_user_id": str(ids["meera.tm"])}).json()
    return {"ids": ids, "event": e}


def _jobs(comm_subject_like: str):
    with worker_engine().connect() as c:
        return c.execute(text("""SELECT j.channel, j.delivery_status, j.provider_message_id, j.last_error, j.attempts
                                 FROM notification_jobs j JOIN test_communications m ON m.id = j.communication_id
                                 WHERE m.subject LIKE :s ORDER BY j.channel"""), {"s": comm_subject_like}).mappings().all()


def _flush(tenant):
    return notifications.dispatch_due(tenant, now=datetime.now(timezone.utc) + timedelta(hours=1))


def test_invitation_goes_to_outlook_and_teams_with_an_acknowledge_button(as_, ev, ms, seeded):
    tm, ids, eid = as_("meera.tm"), ev["ids"], ev["event"]["id"]
    tm.post(f"/test-events/{eid}/stakeholders", expect=201, json={"user_id": str(ids["kiran.net"]), "event_role": "Network lead"})
    _flush(seeded["A"]["tenant"])
    kiran = as_("kiran.net")
    ack = next(a for a in kiran.get("/me/workspace", expect=200).json()["pending_acknowledgements"] if a["event_id"] == eid)
    ack_url = f"https://resilience.bank.example/acknowledge/{ack['id']}"

    token_calls = [r for r in ms.requests if str(r.url).endswith("/token")]
    form = dict(urllib.parse.parse_qsl(token_calls[0].content.decode()))
    assert form["grant_type"] == "client_credentials" and form["scope"] == "https://graph.microsoft.com/.default"
    assert str(token_calls[0].url) == "https://login.microsoftonline.com/7d1f3c2a-5b6e-4f80-9a1b-2c3d4e5f6a7b/oauth2/v2.0/token"

    mail = ms.bodies("/users/resilience@bank.example/sendMail")[-1]["message"]
    assert mail["toRecipients"][0]["emailAddress"]["address"] == "kiran.net@bank.example"
    assert "assigned to Notification drill as Network lead" in mail["subject"]
    assert ack_url in mail["body"]["content"] and ">Acknowledge<" in mail["body"]["content"]
    assert f"/tests/{eid}" in mail["body"]["content"]             # links go to real web pages

    post = ms.bodies("/workflows/abc/")[-1]
    card = post["attachments"][0]["content"]
    assert post["recipient"] == "kiran.net@bank.example" and card["type"] == "AdaptiveCard"
    assert card["actions"][0] == {"type": "Action.OpenUrl", "title": "Acknowledge", "url": ack_url, "style": "positive"}
    assert card["msteams"]["entities"][0]["mentioned"] == {"id": "kiran.net@bank.example", "name": "Kiran Shetty"}

    sent = {j["channel"]: j for j in _jobs("%assigned to Notification drill as Network lead%")}
    assert sent["Email"]["delivery_status"] == "Sent" and sent["Teams"]["delivery_status"] == "Sent"
    assert sent["Teams"]["provider_message_id"].startswith("teams:")


def test_the_graph_token_is_reused(as_, ev, ms, seeded):
    tm, ids, eid = as_("meera.tm"), ev["ids"], ev["event"]["id"]
    for who in ("anita.infra", "deepa.app"):
        tm.post(f"/test-events/{eid}/stakeholders", expect=201, json={"user_id": str(ids[who]), "event_role": "Lead"})
    _flush(seeded["A"]["tenant"])
    assert len([r for r in ms.requests if str(r.url).endswith("/token")]) == 1
    assert len(ms.bodies("/sendMail")) >= 2


def test_a_schedule_change_updates_the_same_calendar_invitation(as_, ev, ms, seeded):
    tm, pm, ids, eid = as_("meera.tm"), as_("arjun.pm"), ev["ids"], ev["event"]["id"]
    tm.post(f"/test-events/{eid}/stakeholders", expect=201, json={"user_id": str(ids["rahul.dba"]), "event_role": "DBA"})
    _flush(seeded["A"]["tenant"])
    version = pm.get(f"/test-events/{eid}", expect=200).json()["version"]
    pm.patch(f"/test-events/{eid}", expect=200, json={"expected_version": version, "reschedule_reason": "Vendor window",
             "planned_start_at": (T0 + timedelta(days=1)).isoformat(), "planned_end_at": (T0 + timedelta(days=1, hours=3)).isoformat()})
    _flush(seeded["A"]["tenant"])
    created = [b for b in ms.bodies("/users/resilience@bank.example/events") if b.get("transactionId")]
    mine = [b for b in created if b["attendees"][0]["emailAddress"]["address"] == "rahul.dba@bank.example"]
    assert len(mine) == 1 and mine[0]["start"] == {"dateTime": "2027-10-05T04:00:00", "timeZone": "UTC"}

    version = pm.get(f"/test-events/{eid}", expect=200).json()["version"]
    pm.patch(f"/test-events/{eid}", expect=200, json={"expected_version": version, "reschedule_reason": "Moved again",
             "planned_start_at": (T0 + timedelta(days=2)).isoformat(), "planned_end_at": (T0 + timedelta(days=2, hours=3)).isoformat()})
    _flush(seeded["A"]["tenant"])
    patches = [r for r in ms.requests if r.method == "PATCH"]
    rahul_event = [j for j in _jobs("%Schedule changed: Notification drill%") if j["channel"] == "Outlook"]
    assert patches and all(j["provider_message_id"].startswith("graph-event:AAMkEvent") for j in rahul_event)
    assert json.loads(patches[-1].content)["start"] == {"dateTime": "2027-10-06T04:00:00", "timeZone": "UTC"}
    assert len([b for b in ms.bodies("/events") if b.get("transactionId")]) == len(created)   # no new invitations


def test_a_failed_teams_post_is_retried(as_, ev, ms, seeded):
    tm, ids, eid = as_("meera.tm"), ev["ids"], ev["event"]["id"]
    ms.fail_teams_for["sana.lead@bank.example"] = 1
    tm.post(f"/test-events/{eid}/stakeholders", expect=201, json={"user_id": str(ids["sana.lead"]), "event_role": "DB lead"})
    _flush(seeded["A"]["tenant"])
    teams = next(j for j in _jobs("%Notification drill as DB lead%") if j["channel"] == "Teams")
    assert teams["delivery_status"] == "Failed" and "Posting to Teams failed (500)" in teams["last_error"]
    notifications.dispatch_due(seeded["A"]["tenant"], now=datetime.now(timezone.utc) + timedelta(hours=2))
    teams = next(j for j in _jobs("%Notification drill as DB lead%") if j["channel"] == "Teams")
    assert teams["delivery_status"] == "Sent" and teams["attempts"] == 2


def test_acknowledgement_page_api(as_, ev, seeded):
    tm, ids, eid = as_("meera.tm"), ev["ids"], ev["event"]["id"]
    tm.post(f"/test-events/{eid}/stakeholders", expect=201, json={"user_id": str(ids["farah.biz"]), "event_role": "Business validator"})
    farah = as_("farah.biz")
    ack = next(a for a in farah.get("/me/workspace", expect=200).json()["pending_acknowledgements"] if a["event_id"] == eid)
    d = farah.get(f"/acknowledgements/{ack['id']}", expect=200).json()
    assert d["status"] == "Pending" and d["event"]["title"] == "Notification drill"
    assert d["assignment"]["event_role"] == "Business validator"
    farah.get(f"/acknowledgements/{ack['id']}", expect=200)          # opening the page twice changes nothing
    assert farah.get(f"/acknowledgements/{ack['id']}").json()["status"] == "Pending"
    r = as_("rahul.dba").get(f"/acknowledgements/{ack['id']}")
    assert r.status_code == 403 and "for someone else" in r.json()["error"]
    farah.post(f"/acknowledgements/{ack['id']}", expect=200, json={"accept": True})
    assert farah.get(f"/acknowledgements/{ack['id']}").json()["status"] == "Acknowledged"
    r = farah.post(f"/acknowledgements/{ack['id']}", json={"accept": False})
    assert r.status_code == 409 and "Already acknowledged" in r.json()["error"]


def test_simulated_mode_sends_nothing(monkeypatch):
    monkeypatch.setattr(config, "settings", replace(config.settings, notify_mode="simulated"))
    assert isinstance(notifications.adapter_for("Email"), notifications.SimulatedAdapter)


def test_teams_is_optional(ms, monkeypatch):
    monkeypatch.setattr(config, "settings", replace(config.settings, teams_webhook_url=""))
    out = m365.TeamsWorkflowAdapter().send("Teams", {"email": "a@b"}, {"subject": "s", "link": "l"}, "k")
    assert out == "teams-not-configured" and not ms.requests


def test_production_needs_graph_settings():
    s = replace(config.settings, app_env="production", notify_mode="m365", graph_client_secret="")
    assert any("GRAPH_CLIENT_SECRET" in p for p in config.check_production(s))
