"""An MCP server that gives the bank's own AI agent its ResilienceAI tools (stdio transport).

The agent framework starts it as a subprocess (it needs only Python and httpx):

    RESILIENCEAI_URL=https://resilience.bank.example  RESILIENCEAI_AGENT_KEY=ag.xxxxx  python scripts/mcp_agent_server.py

Every tool is a thin call to the ResilienceAI API with the agent's key, so the agent can do exactly what its scopes
allow and nothing more: refusals come back as tool errors with the reason, and every call is in the agent action log.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

PROTOCOL = "2025-06-18"
URL = os.environ.get("RESILIENCEAI_URL", "http://localhost:3000").rstrip("/") + "/api/v1"
KEY = os.environ.get("RESILIENCEAI_AGENT_KEY", "")
TRANSPORT = None                       # tests put an in-process transport here


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


S = {"type": "string"}
TOOLS = [
    {"name": "my_work", "description": "Your assigned activities across tests (what you can start now, what is waiting, "
     "what is overdue) and assignments waiting for your acknowledgement.", "inputSchema": _schema({}, [])},
    {"name": "test_activities", "description": "All activities of a test you are part of, with owners, status and dependencies.",
     "inputSchema": _schema({"event_id": S}, ["event_id"])},
    {"name": "acknowledge_assignment", "description": "Accept (or decline, with a reason) being assigned to a test.",
     "inputSchema": _schema({"stakeholder_id": S, "accept": {"type": "boolean"}, "reason": S}, ["stakeholder_id", "accept"])},
    {"name": "set_activity_status", "description": "Move your activity: 'In Progress' to start, 'Submitted' when done "
     "(attach evidence first if it is required). Add a short comment.",
     "inputSchema": _schema({"activity_id": S, "status": {"type": "string", "enum": ["In Progress", "Submitted"]}, "comment": S},
                            ["activity_id", "status"])},
    {"name": "report_blocker", "description": "Report that your activity is blocked and why. Your accountable person, the test "
     "manager and owners of dependent activities are told.", "inputSchema": _schema({"activity_id": S, "reason": S}, ["activity_id", "reason"])},
    {"name": "post_update", "description": "Post a progress note on your activity (shows on the test's timeline).",
     "inputSchema": _schema({"activity_id": S, "message": S}, ["activity_id", "message"])},
    {"name": "attach_evidence", "description": "Attach a text file (log, report) as evidence for your activity. It is "
     "malware-scanned and pre-checked; a person reviews it.",
     "inputSchema": _schema({"activity_id": S, "filename": S, "content": S,
                             "evidence_type": {"type": "string", "enum": ["Log", "Report", "Other"]}}, ["activity_id", "filename", "content"])},
    {"name": "request_runbook_run", "description": "Ask for your activity's linked Ansible runbook to run. A person (your "
     "accountable person or the test manager) approves or declines; you can't run it yourself.",
     "inputSchema": _schema({"activity_id": S, "reason": S}, ["activity_id", "reason"])},
]


def _call(method: str, path: str, **kw):
    with httpx.Client(timeout=30, transport=TRANSPORT) as c:
        r = c.request(method, URL + path, headers={"Authorization": f"Bearer {KEY}"}, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"text": r.text}
    if r.status_code >= 400:
        raise RuntimeError(body.get("error") or body.get("detail") or f"HTTP {r.status_code}")
    return body


def run_tool(name: str, a: dict):
    if name == "my_work":
        return _call("GET", "/me/workspace")
    if name == "test_activities":
        return _call("GET", f"/test-events/{a['event_id']}/activities")
    if name == "acknowledge_assignment":
        body = {"accept": a["accept"]}
        if a.get("reason"):
            body["comment"] = a["reason"]
        return _call("POST", f"/test-stakeholders/{a['stakeholder_id']}/respond", json=body)
    if name == "set_activity_status":
        body = {"status": a["status"]}
        if a.get("comment"):
            body["comment"] = a["comment"]
        return _call("PATCH", f"/test-activities/{a['activity_id']}", json=body)
    if name == "report_blocker":
        return _call("PATCH", f"/test-activities/{a['activity_id']}", json={"status": "Blocked", "blocked_reason": a["reason"]})
    if name == "post_update":
        return _call("POST", f"/test-activities/{a['activity_id']}/updates", json={"message": a["message"]})
    if name == "attach_evidence":
        data = a["content"].encode()
        return _call("POST", f"/test-activities/{a['activity_id']}/evidence/upload",
                     files={"file": (a["filename"], data, "text/plain")}, data={"evidence_type": a.get("evidence_type", "Log")})
    if name == "request_runbook_run":
        return _call("POST", f"/test-activities/{a['activity_id']}/runbook/run-requests", json={"reason": a["reason"]})
    raise RuntimeError(f"Unknown tool {name}")


def handle(msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:                       # a notification (e.g. notifications/initialized): no answer
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "resilienceai-agent", "version": "1.0"},
                  "instructions": "Tools for a ResilienceAI agent: read your work, update your activities, attach evidence "
                                  "and ask for runbooks to run. People approve; you can't."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            out = run_tool(params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(out, default=str)[:60000]}], "isError": False}
        except Exception as e:  # noqa: BLE001 - refusals and errors go back to the agent as tool errors
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    if not KEY.startswith("ag."):
        sys.stderr.write("Set RESILIENCEAI_AGENT_KEY to the agent's key (starts with ag.)\n")
        sys.exit(1)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
