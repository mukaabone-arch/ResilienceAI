"""A stand-in for Ansible Automation Platform, for trying the integration without a real AAP.

    python -m scripts.fake_aap                 serves on http://127.0.0.1:8052/api/v2, token "dev-aap-token"

Then connect it:   python -m scripts.automation add-aap BANK "Local AAP" http://127.0.0.1:8052/api/v2 --token-env AAP_TOKEN
and start the API and worker with AAP_TOKEN=dev-aap-token.

It knows a few DR job templates. A launched job goes pending -> running -> successful (or failed) over a few seconds,
with realistic output. A template whose name contains "fail" fails at a task; variables can force it too:
{"simulate": "fail"}. It also POSTs a webhook to the URL in FAKE_AAP_WEBHOOK (with header FAKE_AAP_WEBHOOK_TOKEN), like
an AAP notifier, when a job starts and finishes. The same class is used by the tests as an httpx transport.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = os.environ.get("FAKE_AAP_TOKEN", "dev-aap-token")
TEMPLATES = [
    {"id": 11, "name": "DR - Verify standby apply lag", "description": "Data Guard: check transport and apply lag on the standby",
     "playbook": "dr/verify_apply_lag.yml", "ask_variables_on_launch": True},
    {"id": 12, "name": "DR - Prepare database recovery point", "description": "Force a log switch and confirm the recovery point",
     "playbook": "dr/prepare_recovery_point.yml", "ask_variables_on_launch": True},
    {"id": 13, "name": "DR - Data Guard switchover", "description": "Switch the CBS database to the DR site",
     "playbook": "dr/switchover.yml", "ask_variables_on_launch": True},
    {"id": 14, "name": "DR - Application smoke tests (fail demo)", "description": "Always fails at the smoke test, for demos",
     "playbook": "dr/smoke.yml", "ask_variables_on_launch": False},
]


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class FakeAAP:
    def __init__(self, step_seconds: float = 2.0, clock=time.time):
        self.jobs: dict[int, dict] = {}
        self.next_id = 4520
        self.step = step_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self.launches: list[dict] = []
        self.webhook: tuple[str, str] | None = None
        self.down = False

    # ---- state ----------------------------------------------------------------------------------------------------
    def _status(self, j: dict) -> str:
        if j.get("canceled"):
            return "canceled"
        age = self.clock() - j["created"]
        if age < self.step:
            return "pending"
        if age < 3 * self.step:
            return "running"
        return "failed" if j["fail"] else "successful"

    def _view(self, j: dict) -> dict:
        st = self._status(j)
        started = j["created"] + self.step if st != "pending" else None
        finished = j["created"] + 3 * self.step if st in ("successful", "failed") else (j.get("canceled") if st == "canceled" else None)
        return {"id": j["id"], "type": "job", "name": j["name"], "status": st, "failed": st == "failed",
                "started": _iso(started) if started else None, "finished": _iso(finished) if finished else None,
                "elapsed": round((finished or self.clock()) - started, 1) if started else 0,
                "extra_vars": json.dumps(j["extra_vars"]), "job_template": j["template"]}

    def _stdout(self, j: dict) -> str:
        v = j["extra_vars"]
        host = v.get("db_host", "cbs-drc-db01")
        now = datetime.fromtimestamp(j["created"], timezone.utc).astimezone()
        ts = lambda s: (now.timestamp() + s)  # noqa: E731
        fmt = lambda s: datetime.fromtimestamp(ts(s)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        lines = [f"PLAY [{j['name']}] *******************************************************",
                 f"{fmt(0)} TASK [Gathering Facts] ok: [{host}]",
                 f"{fmt(4)} TASK [Check Data Guard broker configuration] ok: [{host}] => SUCCESS",
                 f"{fmt(9)} TASK [Last applied log on standby] ok: [{host}] => sequence 99213 applied (recovery point)",
                 f"{fmt(12)} TASK [Measure apply lag] ok: [{host}] => apply lag 00:00:04",
                 f"{fmt(15)} TASK [Database recovery point confirmed] ok: [{host}] => database recovery point verified for CBS"]
        if j["fail"]:
            lines.append(f"{fmt(18)} TASK [Run smoke tests] fatal: [{host}]: FAILED! => {{\"msg\": \"Login journey returned HTTP 503\"}}")
            lines.append(f"PLAY RECAP {host} : ok=5 changed=0 unreachable=0 failed=1")
        else:
            lines.append(f"PLAY RECAP {host} : ok=6 changed=1 unreachable=0 failed=0")
        return "\n".join(lines) + "\n"

    def _notify(self, j: dict, when: str) -> None:
        if not self.webhook:
            return
        import httpx
        url, token = self.webhook
        try:
            httpx.post(url, json={"id": j["id"], "name": j["name"], "status": when}, timeout=5,
                       headers={"Authorization": f"Bearer {token}"})
        except Exception:  # noqa: BLE001
            pass

    # ---- HTTP -----------------------------------------------------------------------------------------------------
    def handle(self, method: str, path: str, query: dict, body: dict | None, auth: str) -> tuple[int, object]:
        if self.down:
            return 503, {"detail": "Service unavailable"}
        if auth != f"Bearer {TOKEN}":
            return 401, {"detail": "Authentication credentials were not provided."}
        p = re.sub(r"^.*?/api(/controller)?/v2", "", path.rstrip("/")) or "/"     # AAP 2.5+ or earlier API roots
        parts = [x for x in p.split("/") if x]
        with self.lock:
            if parts == ["me"]:
                return 200, {"results": [{"username": "resilienceai"}]}
            if parts == ["job_templates"]:
                s = (query.get("search") or [""])[0].lower()
                return 200, {"results": [t for t in TEMPLATES if s in t["name"].lower()]}
            if len(parts) == 2 and parts[0] == "job_templates":
                t = next((t for t in TEMPLATES if str(t["id"]) == parts[1]), None)
                return (200, t) if t else (404, {"detail": "Not found."})
            if len(parts) == 3 and parts[0] == "job_templates" and parts[2] == "launch" and method == "POST":
                t = next((t for t in TEMPLATES if str(t["id"]) == parts[1]), None)
                if t is None:
                    return 404, {"detail": "Not found."}
                sent = (body or {}).get("extra_vars") or {}
                ev = sent if t["ask_variables_on_launch"] else {}      # like AAP: ignored unless prompted on launch
                jid = self.next_id = self.next_id + 1
                j = {"id": jid, "name": t["name"], "template": t["id"], "extra_vars": ev, "created": self.clock(),
                     "fail": "fail" in t["name"].lower() or ev.get("simulate") == "fail"}
                self.jobs[jid] = j
                self.launches.append({"template": t["id"], "extra_vars": ev, "job": jid})
                out = {"job": jid, "id": jid, "url": f"/api/v2/jobs/{jid}/"}
                if sent and not ev:
                    out["ignored_fields"] = {"extra_vars": sent}
                return 201, out
            if parts == ["jobs"]:
                marker = (query.get("extra_vars__icontains") or [""])[0]
                hits = sorted((j for j in self.jobs.values() if marker and marker in json.dumps(j["extra_vars"])),
                              key=lambda j: -j["id"])
                return 200, {"results": [self._view(j) for j in hits[:1]]}
            if len(parts) >= 2 and parts[0] == "jobs":
                j = self.jobs.get(int(parts[1])) if parts[1].isdigit() else None
                if j is None:
                    return 404, {"detail": "Not found."}
                if len(parts) == 2:
                    return 200, self._view(j)
                if parts[2] == "stdout":
                    return 200, self._stdout(j)
                if parts[2] == "job_events":
                    if self._status(j) != "failed":
                        return 200, {"results": []}
                    return 200, {"results": [{"task": "Run smoke tests", "host_name": j["extra_vars"].get("db_host", "cbs-drc-db01"),
                                              "event": "runner_on_failed",
                                              "event_data": {"res": {"msg": "Login journey returned HTTP 503"}}}]}
                if parts[2] == "cancel" and method == "POST":
                    if self._status(j) in ("pending", "running"):
                        j["canceled"] = self.clock()
                        return 202, {}
                    return 405, {"detail": "Job is not running"}
        return 404, {"detail": "Not found."}

    def transport(self):
        import httpx

        def handler(request: httpx.Request):
            body = json.loads(request.content) if request.content else None
            q = parse_qs(request.url.query.decode())
            status, data = self.handle(request.method, request.url.path, q, body, request.headers.get("authorization", ""))
            if isinstance(data, str):
                return httpx.Response(status, text=data)
            return httpx.Response(status, json=data)
        return httpx.MockTransport(handler)

    def serve(self, port: int = 8052) -> None:
        fake = self
        hook = os.environ.get("FAKE_AAP_WEBHOOK")
        if hook:
            self.webhook = (hook, os.environ.get("FAKE_AAP_WEBHOOK_TOKEN", ""))

        class H(BaseHTTPRequestHandler):
            def _go(self, method):
                u = urlparse(self.path)
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                status, data = fake.handle(method, u.path, parse_qs(u.query), body, self.headers.get("authorization", ""))
                raw = data.encode() if isinstance(data, str) else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("content-type", "text/plain" if isinstance(data, str) else "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

            def log_message(self, *a):
                pass

        def notifier():                                    # like AAP's notifier: on start and on finish
            seen: dict[int, str] = {}
            while True:
                time.sleep(0.5)
                for j in list(fake.jobs.values()):
                    st = fake._status(j)
                    if seen.get(j["id"]) != st and st in ("running", "successful", "failed", "canceled"):
                        seen[j["id"]] = st
                        fake._notify(j, st)
        threading.Thread(target=notifier, daemon=True).start()
        print(f"Stand-in AAP on http://127.0.0.1:{port}/api/v2 (token {TOKEN})")
        ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8052)
    ap.add_argument("--step", type=float, default=4.0, help="seconds per job phase")
    a = ap.parse_args()
    FakeAAP(step_seconds=a.step).serve(a.port)
