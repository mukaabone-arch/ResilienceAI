"""A small client for Ansible Automation Platform's automation controller API.

`base_url` is the controller's API root, e.g. https://aap.bank.example/api/controller/v2 (AAP 2.5 and later, through
the platform gateway) or https://aap.bank.example/api/v2 (earlier versions). The token is an OAuth2 / personal access
token for a dedicated AAP user that may only *execute* the job templates linked from ResilienceAI. It comes from an
environment variable named on the integration (filled from the bank's vault); it is never stored in the database.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import httpx

TRANSPORT = None              # tests put an httpx mock transport here
MAX_STDOUT = 5 * 1024 * 1024


class AAPError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message, self.status = message, status


@dataclass
class Job:
    id: int
    status: str                  # new | pending | waiting | running | successful | failed | error | canceled
    name: str | None = None
    started: str | None = None
    finished: str | None = None
    elapsed: float | None = None
    failed: bool = False


class AAP:
    def __init__(self, base_url: str, token_env: str, transport=None):
        self.base = base_url.rstrip("/")
        self.token = os.environ.get(token_env, "")
        if not self.token:
            raise AAPError(f"The Ansible token isn't configured on this server (set {token_env} from the vault).")
        self.transport = transport or TRANSPORT
        self.verify = os.environ.get("AAP_CA_BUNDLE") or True

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base, timeout=15.0, transport=self.transport, verify=self.verify,
                            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            with self._client() as c:
                r = c.request(method, path, **kw)
        except httpx.TimeoutException as e:
            raise AAPError("Ansible Automation Platform didn't answer in time") from e
        except httpx.HTTPError as e:
            raise AAPError(f"Can't reach Ansible Automation Platform ({type(e).__name__})") from e
        if r.status_code in (401, 403):
            raise AAPError("Ansible refused ResilienceAI's token (check the AAP user and its permissions)", r.status_code)
        if r.status_code == 404:
            raise AAPError("Not found in Ansible Automation Platform", 404)
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                detail = body.get("detail") or "; ".join(f"{k}: {v}" for k, v in body.items())[:300]
            except Exception:  # noqa: BLE001
                pass
            raise AAPError(f"Ansible answered {r.status_code}{': ' + detail if detail else ''}", r.status_code)
        return r

    # ---- what ResilienceAI uses -------------------------------------------------------------------------------------
    def ping(self) -> dict:
        return self._call("GET", "/me/").json()

    def job_templates(self, search: str = "") -> list[dict]:
        params = {"page_size": 50, "order_by": "name"}
        if search:
            params["search"] = search
        data = self._call("GET", "/job_templates/", params=params).json()
        return [{"id": t["id"], "name": t["name"], "description": t.get("description") or "",
                 "playbook": t.get("playbook"), "ask_variables_on_launch": t.get("ask_variables_on_launch", False)}
                for t in data.get("results", [])]

    def job_template(self, template_id: int) -> dict:
        t = self._call("GET", f"/job_templates/{template_id}/").json()
        return {"id": t["id"], "name": t["name"], "ask_variables_on_launch": t.get("ask_variables_on_launch", False)}

    def launch(self, template_id: int, extra_vars: dict) -> int:
        body = self._call("POST", f"/job_templates/{template_id}/launch/", json={"extra_vars": extra_vars}).json()
        job_id = body.get("job") or body.get("id")
        if not isinstance(job_id, int):
            raise AAPError("Ansible accepted the launch but didn't say which job it started")
        return job_id

    def job(self, job_id: int) -> Job:
        j = self._call("GET", f"/jobs/{job_id}/").json()
        return Job(id=j["id"], status=j.get("status", "unknown"), name=j.get("name"), started=j.get("started"),
                   finished=j.get("finished"), elapsed=j.get("elapsed"), failed=bool(j.get("failed")))

    def find_job(self, marker: str) -> int | None:
        """A job launched with this marker in its extra variables (to reconcile a launch whose answer was lost)."""
        data = self._call("GET", "/jobs/", params={"extra_vars__icontains": marker, "order_by": "-id", "page_size": 1}).json()
        res = data.get("results", [])
        return res[0]["id"] if res else None

    def stdout(self, job_id: int) -> bytes:
        r = self._call("GET", f"/jobs/{job_id}/stdout/", params={"format": "txt_download"})
        return r.content[:MAX_STDOUT]

    def failure_summary(self, job_id: int) -> str | None:
        """The first failed task, host and message, in one line."""
        try:
            data = self._call("GET", f"/jobs/{job_id}/job_events/",
                              params={"failed": "true", "event": "runner_on_failed", "order_by": "counter", "page_size": 3}).json()
        except AAPError:
            return None
        for e in data.get("results", []):
            res = (e.get("event_data") or {}).get("res") or {}
            msg = res.get("msg") or res.get("stderr") or res.get("stdout") or ""
            msg = re.sub(r"\s+", " ", str(msg)).strip()[:240]
            return f"task '{e.get('task') or '?'}' on {e.get('host_name') or 'a host'}" + (f": {msg}" if msg else "")
        return None

    def cancel(self, job_id: int) -> None:
        self._call("POST", f"/jobs/{job_id}/cancel/")

    def job_url(self, job_id: int) -> str:
        ui = re.sub(r"/api(/controller)?/v2$", "", self.base)
        return f"{ui}/execution/jobs/playbook/{job_id}/output" if "/api/controller/" in self.base + "/" \
            else f"{ui}/#/jobs/playbook/{job_id}/output"
