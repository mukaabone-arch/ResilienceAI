"""Deterministic composers: plain-English text built only from the gathered facts.

These always run. An external model, when one is configured, may only rewrite
their output (see provider.py); if it fails a check, this text is used as is.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..domain import forecast as fc


def _when(dt: datetime | None, tz: str) -> str:
    if not dt:
        return "-"
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %d %b, %H:%M")


def _hm(td_hours: float) -> str:
    h = abs(td_hours)
    if h < 1:
        return f"{round(h * 60)} min"
    if h < 48:
        return f"{round(h)} h"
    return f"{round(h / 24)} days"


def _who(name: str | None, fallback: str = "The test manager") -> str:
    return name or fallback


# ---------------------------------------------------------------------------
# Blocker briefing (RA-071)
# ---------------------------------------------------------------------------
def blocker_briefing(f: dict) -> dict:
    ev, g = f["event"], f["gate"]
    if g["total"] == 0:
        headline = "No mandatory readiness conditions are set yet, so the go decision can't be judged."
    elif g["passed"] and ev["lifecycle"] == "Ready" and f["go_decision_valid"]:
        headline = "Cleared to start: every mandatory condition is met and the go decision is recorded."
    elif g["passed"]:
        headline = f"All {g['total']} mandatory conditions are met; the go decision is the next step."
    else:
        left = g["total"] - g["met"]
        headline = f"{left} of {g['total']} mandatory condition{'s' if g['total'] != 1 else ''} still stand{'s' if left == 1 else ''} between this test and the go decision."

    holding: list[str] = []
    steps: list[tuple[int, str]] = []          # (priority, text)
    for b in sorted(f["blocked"], key=lambda b: -len(b["holds_up"])):
        held = f", holding up {len(b['holds_up'])} more ({', '.join(b['holds_up'])})" if b["holds_up"] else ""
        since = f" for {b['blocked_for']}" if b["blocked_for"] else ""
        holding.append(f"{b['code']} {b['title']} is blocked{since}{held}: {b['reason'] or 'no reason given'}.")
        steps.append((100 + 10 * len(b["holds_up"]), f"{_who(b['owner'])} to clear the blocker on {b['code']} ({b['title']}). It holds up {len(b['holds_up'])} other activit{'y' if len(b['holds_up']) == 1 else 'ies'}."))
    for i in f["issues"]:
        holding.append(f"{i['code']} ({i['severity']}): {i['title']}, open for {i['open_for']}, owned by {_who(i['owner'], 'nobody')}.")
        steps.append((90, f"{_who(i['owner'], 'Someone')} to resolve {i['code']}: {i['title']}." + ("" if i["owner"] else " Assign it an owner first.")))
    blocked_codes = {b["code"] for b in f["blocked"]}
    for u in f["unmet"]:
        why = "evidence awaiting review" if u["status"] == "Ready" else u["status"].lower()
        holding.append(f"Readiness condition '{u['title']}' is not met ({why}).")
        if u["linked_activity"] in blocked_codes:
            continue                                  # its activity's step covers it
        if u["status"] == "Ready" and u["evidence_pending"]:
            steps.append((70, f"{_who(f['event']['test_manager'])} to review the evidence for '{u['title']}'."))
        elif not u["owner"]:
            steps.append((75, f"Give '{u['title']}' an owner, so someone is answerable for confirming it."))
        else:
            steps.append((65, f"{u['owner']} to confirm '{u['title']}' (currently {u['status'].lower()})."))
    for o in f["overdue"]:
        holding.append(f"{o['code']} {o['title']} is overdue by {o['late_by']}.")
        steps.append((60, f"{_who(o['owner'])} to finish {o['code']} ({o['title']}), {o['late_by']} late."))
    if f["pending_acks"]:
        k = len(f["pending_acks"])
        holding.append(f"{k} {'person hasn' if k == 1 else 'people haven'}'t acknowledged their assignment.")
        steps.append((40, f"Chase acknowledgements from {', '.join(f['pending_acks'][:4])}{' and others' if len(f['pending_acks']) > 4 else ''}."))
    if f["evidence_pending"] and not any("review the evidence" in s for _, s in steps):
        steps.append((35, f"{_who(f['event']['test_manager'])} to review {f['evidence_pending']} evidence item{'s' if f['evidence_pending'] != 1 else ''} waiting for a decision."))
    if g["passed"] and ev["lifecycle"] == "Ready" and not f["go_decision_valid"]:
        steps.append((95, "Ask the approvers to record the go decision."))
    elif g["passed"] and ev["lifecycle"] in ("Mobilising", "Readiness"):
        steps.append((95, "Mark the test ready, then ask the approvers for the go decision."))

    ordered = [s for _, s in sorted(steps, key=lambda x: -x[0])][:6]
    lines = [headline]
    if holding:
        lines += ["", "What's holding it up:"] + [f"- {h}" for h in holding[:8]]
    if ordered:
        lines += ["", "Suggested next steps:"] + [f"{n}. {s}" for n, s in enumerate(ordered, start=1)]
    return {"headline": headline, "holding": holding, "steps": ordered, "text": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Slip forecast (RA-071)
# ---------------------------------------------------------------------------
def slip_forecast(f: dict) -> dict:
    fo: fc.Forecast = f["forecast"]
    tz = f["event"]["timezone"]
    if not fo.applicable:
        text = f"A readiness forecast applies before the test starts; this test is {f['event']['lifecycle'].lower()}."
        return {"verdict": "not_applicable", "headline": text, "text": text, "drivers": [], "rules": []}
    if fo.verdict == "ready":
        head = f"Readiness is complete, {_hm(fo.margin_hours)} before the planned start."
    elif fo.verdict == "on_track":
        head = f"On track: readiness is projected by {_when(fo.projected_ready, tz)}, {_hm(fo.margin_hours)} before the start."
    elif fo.verdict == "at_risk":
        head = f"At risk: readiness is projected by {_when(fo.projected_ready, tz)}, only {_hm(fo.margin_hours)} before the start."
    else:
        head = (f"Likely to slip: readiness is projected for {_when(fo.projected_ready, tz)}, about {_hm(fo.margin_hours)} "
                f"after the planned start ({_when(fo.needed_by, tz)}).")
    drivers = [{"ref": d.ref, "kind": d.kind, "projected": d.projected, "reason": d.reason} for d in fo.drivers]
    lines = [head]
    if drivers:
        lines += ["", "What drives the date:"] + [f"- {d['ref']}: projected {_when(d['projected'], tz)} ({d['reason']})." for d in drivers]
    lines += ["", "This is a rule-based estimate, not a guarantee. Rules used:"] + [f"- {r}" for r in fo.rules]
    return {"verdict": fo.verdict, "headline": head, "projected_ready": fo.projected_ready, "needed_by": fo.needed_by,
            "margin_hours": fo.margin_hours, "drivers": drivers, "rules": fo.rules, "text": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Status summary (RA-069)
# ---------------------------------------------------------------------------
def status_summary(f: dict) -> dict:
    ev, n, tz = f["event"], f["now"], f["event"]["timezone"]
    acts = f["activities"]
    done = [a for a in acts if a["status"] == "Completed"]
    doing = [a for a in acts if a["status"] in ("In Progress", "Submitted")]
    blocked = [a for a in acts if a["status"] == "Blocked"]
    s: list[str] = []
    if ev["lifecycle"] in ("Live", "Paused") and ev["actual_start_at"]:
        elapsed = (n - ev["actual_start_at"]).total_seconds() / 3600
        window = (ev["planned_end_at"] - ev["planned_start_at"]).total_seconds() / 3600
        s.append(f"{ev['title']} is {'paused' if ev['lifecycle'] == 'Paused' else 'live'}, {_hm(elapsed)} into a {_hm(window)} window"
                 + (f" (paused: {ev['status_reason']})." if ev["lifecycle"] == "Paused" and ev["status_reason"] else "."))
    else:
        s.append(f"{ev['title']} is {ev['lifecycle'].lower()}; planned for {_when(ev['planned_start_at'], tz)}.")
    if acts:
        part = f"{len(done)} of {len(acts)} activities are complete"
        if doing:
            part += "; in progress: " + ", ".join(f"{a['activity_code']} {a['title']}" + (f" ({a['owner']})" if a["owner"] else "") for a in doing[:3])
        s.append(part + ".")
    if blocked:
        s.append("Blocked: " + "; ".join(f"{a['activity_code']} ({a['blocked_reason'] or 'no reason given'})" for a in blocked[:3]) + ".")
    pending_ms = [m for m in f["milestones"] if m["status"] != "Completed"]
    late_ms = [m for m in pending_ms if m["planned_at"] and m["planned_at"] < n]
    if late_ms:
        m = late_ms[0]
        s.append(f"Milestone '{m['title']}' was due at {_when(m['planned_at'], tz)} and is {_hm((n - m['planned_at']).total_seconds() / 3600)} behind.")
    elif pending_ms and pending_ms[0]["planned_at"]:
        s.append(f"Next milestone: '{pending_ms[0]['title']}' at {_when(pending_ms[0]['planned_at'], tz)}.")
    if f["issues"]:
        s.append("Open: " + "; ".join(f"{i['issue_type'].lower()} {i['issue_code']} ({i['severity']}): {i['title']}" for i in f["issues"][:3]) + ".")
    elif ev["lifecycle"] in ("Live", "Paused"):
        s.append("No open blockers or incidents.")
    for m in f["measurements"]:
        s.append(f"{m['measurement_type']}: {m['actual_value']} {m['unit'].lower()} against a target of {m['target_value']} "
                 f"{m['target_unit'].lower()} ({'met' if m['target_met'] else 'not met' if m['target_met'] is False else 'recorded'}).")
    latest = next((x for x in f["logs"] if x["log_type"] in ("Status", "Announcement", "Decision", "Milestone")), None)
    if latest:
        s.append(f"Latest update ({_when(latest['occurred_at'], tz)}{', ' + latest['actor'] if latest['actor'] else ''}): {latest['message']}")
    text = " ".join(s)
    return {"headline": s[0], "text": text, "as_of": n}


# ---------------------------------------------------------------------------
# Message drafts (RA-070)
# ---------------------------------------------------------------------------
KINDS = {
    "reminder": "Reminder to people with open items",
    "escalation": "Escalation about blockers",
    "readiness_update": "Readiness update to everyone on the test",
    "go_request": "Request for the go decision",
}


def message_draft(kind: str, rf: dict, people: dict, me_name: str) -> dict:
    ev, g = rf["event"], rf["gate"]
    users, stake = people["users"], people["stakeholders"]
    name_to_id = {u["display_name"]: uid for uid, u in users.items()}
    when = _when(ev["planned_start_at"], ev["timezone"])
    per: dict = {}

    def add(name, line):
        uid = name_to_id.get(name)
        if uid:
            per.setdefault(uid, []).append(line)

    common: list[str] = []
    if kind == "reminder":
        subject = f"[{ev['code']}] Action needed before {ev['title']}"
        intro = (f"Hello,\n\nAhead of {ev['title']} on {when}, the items below are waiting on you. "
                 "Please update them in ResilienceAI, or reply if something is blocking you.")
        for b in rf["blocked"]:
            add(b["owner"], f"{b['code']} {b['title']} is blocked ({b['reason'] or 'no reason given'}); it holds up {len(b['holds_up'])} other activities.")
        for o in rf["overdue"]:
            add(o["owner"], f"{o['code']} {o['title']} is {o['late_by']} overdue.")
        for u in rf["unmet"]:
            if u["owner"] and not (u["status"] == "Ready" and u["evidence_pending"]):
                add(u["owner"], f"Readiness condition '{u['title']}' is {u['status'].lower()}.")
        for nme in rf["pending_acks"]:
            add(nme, "Please acknowledge your assignment to this test.")
    elif kind == "escalation":
        subject = f"[{ev['code']}] Escalation: blockers putting the go decision at risk"
        intro = (f"Hello,\n\nThe go decision for {ev['title']} ({when}) is at risk. "
                 f"{g['met']} of {g['total']} mandatory readiness conditions are met.")
        brief = blocker_briefing(rf)
        common = [f"- {h}" for h in brief["holding"][:6]]
        fo = slip_forecast(rf)
        if fo["verdict"] in ("at_risk", "likely_slip"):
            common += ["", fo["headline"]]
        for nme in {ev["test_manager"], ev["owner"]} - {None}:
            add(nme, "You are accountable for this test.")
        for s in stake:
            if s.get("team_id") and people["team_leads"].get(s["team_id"]):
                lead = users.get(people["team_leads"][s["team_id"]], {}).get("display_name")
                if lead and any(b["owner"] == users.get(s["user_id"], {}).get("display_name") for b in rf["blocked"]):
                    add(lead, f"Someone in your team owns a blocked item: {users[s['user_id']]['display_name']}.")
    elif kind == "readiness_update":
        subject = f"[{ev['code']}] Readiness update: {g['met']} of {g['total']} mandatory conditions met"
        intro = f"Hello,\n\nHere is where {ev['title']} ({when}) stands."
        brief = blocker_briefing(rf)
        common = [brief["headline"]] + ([""] + [f"- {h}" for h in brief["holding"][:6]] if brief["holding"] else [])
        fo = slip_forecast(rf)
        if fo["verdict"] != "not_applicable":
            common += ["", fo["headline"]]
        for s in stake:
            add(users.get(s["user_id"], {}).get("display_name"), "")
    elif kind == "go_request":
        subject = f"[{ev['code']}] Go decision requested: {ev['title']}"
        intro = (f"Hello,\n\n{ev['title']} is planned for {when}. All {g['total']} mandatory readiness conditions are met"
                 + (f" ({len(g['excepted'])} by approved exception)" if g["excepted"] else "")
                 + ". Please review the readiness page and record go or no-go.")
        for u in users.values():
            if "DRProgrammeManager" in (u["roles"] or []):
                add(u["display_name"], "")
        if ev["criticality"] != "Critical" and ev["owner"] and ev["owner"] != ev["test_manager"]:
            add(ev["owner"], "")
    else:
        raise ValueError(f"Unknown draft kind {kind}")

    recipients = [{"user_id": uid, "name": users[uid]["display_name"], "lines": [x for x in lines if x]}
                  for uid, lines in per.items() if not users[uid]["is_external"] or kind == "reminder"]
    return {"kind": kind, "label": KINDS[kind], "subject": subject, "intro": intro, "common": common,
            "recipients": sorted(recipients, key=lambda r: r["name"]), "signature": f"Sent by {me_name} via ResilienceAI"}


def render_message(draft: dict, recipient: dict, intro: str | None = None, subject: str | None = None) -> tuple[str, str]:
    body = [intro if intro is not None else draft["intro"]]
    if draft["common"]:
        body += [""] + draft["common"]
    if recipient["lines"]:
        body += ["", "Waiting on you:"] + [f"- {x}" for x in recipient["lines"]]
    body += ["", draft["signature"]]
    return subject if subject is not None else draft["subject"], "\n".join(body)
