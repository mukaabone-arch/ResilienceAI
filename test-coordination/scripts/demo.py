"""Reset the database and load a realistic demo portfolio through the real API.

Usage:  python -m scripts.demo
Creates tests at different stages relative to today, so every screen has
something to show:
  - Core Banking DR Simulation      Readiness, database prerequisite blocked
  - Payments Gateway Failover       Ready, waiting for the go decision
  - Internet Banking App Recovery   Live, coordination in progress
  - Card Switch DR Rehearsal        Planned
  - Treasury DB Restore Test        Draft
  - Branch Network Cutover          Cancelled
  - Net Banking DR Drill            Closed three weeks ago, with sign-offs (for reports)
  - ATM Network Failover            Draft, overlaps the Card Switch rehearsal (same test manager)
plus a year-end change freeze (blackout period).
"""
from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import tables, worker_engine
from app.main import app
from scripts.seed import CORE_BANKING_TEMPLATE, reset, seed_tenant

SHA = hashlib.sha256(b"demo").hexdigest()
NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


class Api:
    def __init__(self, client: TestClient, tenant: str):
        self.c, self.tenant = client, tenant

    def __call__(self, who: str, method: str, path: str, json=None):
        r = self.c.request(method, f"/api/v1{path}", json=json,
                           headers={"X-Tenant-Id": self.tenant, "X-User-Subject": who})
        if r.status_code >= 400:
            raise RuntimeError(f"{who} {method} {path} -> {r.status_code} {r.text}")
        return r.json()


def main() -> None:
    # A reset starts from an empty local evidence store too (never touches a real object store).
    from app.config import settings
    if settings.evidence_store == "local":
        shutil.rmtree(settings.evidence_dir, ignore_errors=True)
    with worker_engine().begin() as conn:
        reset(conn)
        conn.exec_driver_sql("GRANT ALL ON SCHEMA public TO app_user")
        ids = seed_tenant(conn, "BANK", "Demo Bank")
    tables.cache_clear()
    api = Api(TestClient(app), str(ids["tenant"]))
    u = {k: str(v) for k, v in ids.items()}

    prog = api("arjun.pm", "POST", "/test-programmes", {
        "name": "FY2027 Resilience Programme", "programme_code": "FY27", "financial_year": "FY2027",
        "start_date": "2026-04-01", "end_date": "2027-03-31"})
    tpl = api("arjun.pm", "POST", "/test-templates", {
        "template_key": "core-banking-dr", "name": "Core Banking DR Simulation", "category": "Application",
        "default_duration": 360, "definition": CORE_BANKING_TEMPLATE})

    def new_event(title, category, criticality, start, hours=6, template=True, manager="meera.tm", owner="vikram.owner",
                  rto=None, rpo=None):
        return api("arjun.pm", "POST", "/test-events", {
            "programme_id": prog["id"], "template_id": tpl["id"] if template else None, "title": title,
            "category": category, "criticality": criticality, "timezone": "Asia/Kolkata",
            "planned_start_at": start.isoformat(), "planned_end_at": (start + timedelta(hours=hours)).isoformat(),
            "owner_user_id": u[owner], "test_manager_user_id": u[manager],
            "business_service_ids": [u["svc_cbs"]] if template else [],
            "rto_target_minutes": rto, "rpo_target_minutes": rpo})

    def mobilise(ev, people):
        eid = ev["id"]
        api("meera.tm", "POST", f"/test-events/{eid}/transitions", {"action": "submit"})
        api("arjun.pm", "POST", f"/test-events/{eid}/transitions", {"action": "approve_plan"})
        st = {}
        for sub, role in people.items():
            st[sub] = api("meera.tm", "POST", f"/test-events/{eid}/stakeholders", {"user_id": u[sub], "event_role": role})[0]
        return st

    def activities(eid):
        return {a["activity_code"]: a for a in api("meera.tm", "GET", f"/test-events/{eid}/activities")}

    def assign(eid, st, mapping):
        acts = activities(eid)
        for code, sub in mapping.items():
            api("meera.tm", "PATCH", f"/test-activities/{acts[code]['id']}", {"stakeholder_id": st[sub]["id"]})
        return acts

    def move(who, act_id, *statuses, reason=None):
        for s in statuses:
            body = {"status": s}
            if s == "Blocked":
                body["blocked_reason"] = reason
            api(who, "PATCH", f"/test-activities/{act_id}", body)

    people = {"anita.infra": "Infrastructure lead", "kiran.net": "Network lead", "rahul.dba": "Database recovery",
              "deepa.app": "Application lead", "farah.biz": "Business validation", "ext.vendor": "Storage vendor",
              "sana.lead": "Database team lead"}
    owners = {"INF-01": "anita.infra", "NET-01": "kiran.net", "DB-01": "rahul.dba", "APP-01": "deepa.app",
              "TV-01": "deepa.app", "BV-01": "farah.biz"}

    # 1. Core Banking — Readiness, DB blocked -----------------------------------
    cb = new_event("Core Banking DR Simulation", "Application", "Critical", NOW + timedelta(days=5, hours=13),
                   rto=240, rpo=15)
    st = mobilise(cb, people)
    acts = assign(cb["id"], st, owners)
    for sub in ("anita.infra", "kiran.net", "rahul.dba", "deepa.app", "farah.biz", "sana.lead"):
        api(sub, "POST", f"/test-stakeholders/{st[sub]['id']}/respond", {"accept": True})
    api("meera.tm", "POST", f"/test-events/{cb['id']}/transitions", {"action": "begin_readiness"})
    move("anita.infra", acts["INF-01"]["id"], "In Progress", "Submitted")
    log = (b"2026-11-24 22:10 DR site power and cooling checks passed\n"
           b"2026-11-24 22:25 Storage array replication healthy, lag 42s\n"
           b"2026-11-24 22:40 Compute capacity reserved: 64 vCPU, 512 GB\n")
    r = api.c.post(f"/api/v1/test-activities/{acts['INF-01']['id']}/evidence/upload",
                   headers={"X-Tenant-Id": api.tenant, "X-User-Subject": "anita.infra"},
                   files={"file": ("dr-site-readiness.log", log, "text/plain")}, data={"evidence_type": "Log"})
    if r.status_code >= 400:
        raise RuntimeError(f"demo upload failed: {r.status_code} {r.text}")
    move("kiran.net", acts["NET-01"]["id"], "In Progress")
    move("rahul.dba", acts["DB-01"]["id"], "In Progress", "Blocked",
         reason="Recovery point is 40 minutes old; waiting for a fresh storage snapshot from the vendor")
    rd = {i["title"]: i for i in api("meera.tm", "GET", f"/test-events/{cb['id']}/readiness")["items"]}
    cr = rd["Change request approved"]
    api("meera.tm", "PATCH", f"/test-readiness-items/{cr['id']}", {"status": "Ready"})
    ev = api("meera.tm", "POST", f"/test-readiness-items/{cr['id']}/evidence",
             {"evidence_type": "ExternalRecord", "storage_reference": "servicenow://CHG0041234"})
    api("rohan.risk", "POST", f"/test-evidence/{ev['id']}/review", {"accept": True})
    api("meera.tm", "PATCH", f"/test-readiness-items/{rd['Database recovery ready']['id']}",
        {"status": "Blocked", "comment": "Depends on DB-01", "owner_user_id": u["rahul.dba"]})
    api("meera.tm", "PATCH", f"/test-readiness-items/{rd['Business validation team confirmed']['id']}",
        {"status": "In Progress", "owner_user_id": u["farah.biz"]})
    api("rahul.dba", "POST", f"/test-events/{cb['id']}/issues", {
        "title": "Storage snapshot schedule not aligned to RPO", "severity": "High", "issue_type": "Blocker",
        "activity_id": acts["DB-01"]["id"], "owner_user_id": u["ext.vendor"]})

    # 2. Payments — Ready, awaiting go ------------------------------------------
    pay = new_event("Payments Gateway Failover", "Infrastructure", "High", NOW + timedelta(days=2, hours=17), hours=4, template=False,
                    rto=60, rpo=5)
    st2 = mobilise(pay, {"anita.infra": "Infrastructure lead", "kiran.net": "Network lead", "farah.biz": "Business validation"})
    for sub in st2:
        api(sub, "POST", f"/test-stakeholders/{st2[sub]['id']}/respond", {"accept": True})
    api("meera.tm", "POST", f"/test-events/{pay['id']}/transitions", {"action": "begin_readiness"})
    for code, title, sub, due in (("PG-01", "Confirm standby gateway capacity", "anita.infra", -24),
                                  ("PG-02", "Pre-stage DNS change", "kiran.net", -6),
                                  ("PG-03", "Merchant test transactions", "farah.biz", 3)):
        a = api("meera.tm", "POST", f"/test-events/{pay['id']}/activities", {
            "activity_code": code, "title": title, "stakeholder_id": st2[sub]["id"],
            "due_at": (NOW + timedelta(days=2, hours=17 + due)).isoformat()})
        if code != "PG-03":
            move(sub, a["id"], "In Progress", "Submitted")
    for title in ("Change request approved", "Standby gateway health check passed", "Merchant comms sent"):
        it = api("meera.tm", "POST", f"/test-events/{pay['id']}/readiness-items", {"title": title})
        api("meera.tm", "PATCH", f"/test-readiness-items/{it['id']}", {"status": "Ready"})
    api("meera.tm", "POST", f"/test-events/{pay['id']}/transitions", {"action": "mark_ready"})
    api("meera.tm", "POST", f"/test-events/{pay['id']}/approval-requests",
        {"approval_type": "Go/No-go", "approver_user_ids": [u["vikram.owner"], u["arjun.pm"]]})

    # 3. Internet Banking — Live -------------------------------------------------
    ib = new_event("Internet Banking App Recovery", "Application", "High", NOW - timedelta(hours=2), hours=6, template=False,
                   manager="vikram.owner", owner="meera.tm")
    st3 = mobilise(ib, {"deepa.app": "Application lead", "anita.infra": "Infrastructure lead", "farah.biz": "Business validation",
                        "rahul.dba": "Database recovery"})
    for sub in st3:
        api(sub, "POST", f"/test-stakeholders/{st3[sub]['id']}/respond", {"accept": True})
    api("vikram.owner", "POST", f"/test-events/{ib['id']}/transitions", {"action": "begin_readiness"})
    ib_acts = {}
    for code, title, sub, due in (("IB-01", "Fail web tier to DR", "anita.infra", 0), ("IB-02", "Restore session store", "rahul.dba", 1),
                                  ("IB-03", "Smoke-test login and transfers", "deepa.app", 2), ("IB-04", "Customer journey sign-off", "farah.biz", 3)):
        ib_acts[code] = api("vikram.owner", "POST", f"/test-events/{ib['id']}/activities", {
            "activity_code": code, "title": title, "stakeholder_id": st3[sub]["id"],
            "due_at": (NOW - timedelta(hours=2) + timedelta(hours=due)).isoformat(), "evidence_required": code == "IB-03"})
    for a, b in (("IB-01", "IB-02"), ("IB-02", "IB-03"), ("IB-03", "IB-04")):
        api("vikram.owner", "POST", f"/test-events/{ib['id']}/dependencies",
            {"predecessor_activity_id": ib_acts[a]["id"], "successor_activity_id": ib_acts[b]["id"]})
    it = api("vikram.owner", "POST", f"/test-events/{ib['id']}/readiness-items", {"title": "Change request approved"})
    api("vikram.owner", "PATCH", f"/test-readiness-items/{it['id']}", {"status": "Ready"})
    api("vikram.owner", "POST", f"/test-events/{ib['id']}/transitions", {"action": "mark_ready"})
    api("arjun.pm", "POST", f"/test-events/{ib['id']}/approvals",
        {"approval_type": "Go/No-go", "decision": "Approved", "comments": "Readiness complete; ops bridge staffed"})
    api("vikram.owner", "POST", f"/test-events/{ib['id']}/start")
    api("vikram.owner", "POST", f"/test-events/{ib['id']}/announcements",
        {"message": "Failover window is open. Bridge line and war-room channel are live."})
    move("anita.infra", ib_acts["IB-01"]["id"], "In Progress", "Submitted")
    move("rahul.dba", ib_acts["IB-02"]["id"], "In Progress")
    api("rahul.dba", "POST", f"/test-events/{ib['id']}/logs", {"log_type": "Status", "message": "Session store restore at 70%"})

    # 4–6. Planned, Draft, Cancelled --------------------------------------------
    card = new_event("Card Switch DR Rehearsal", "Infrastructure", "Critical", NOW + timedelta(days=24), template=False)
    api("meera.tm", "POST", f"/test-events/{card['id']}/transitions", {"action": "submit"})
    new_event("Treasury DB Restore Test", "Database", "Medium", NOW + timedelta(days=45), hours=3, template=False)
    br = new_event("Branch Network Cutover", "Infrastructure", "Low", NOW + timedelta(days=12), hours=2, template=False)
    api("arjun.pm", "POST", f"/test-events/{br['id']}/transitions",
        {"action": "cancel", "reason": "Superseded by the SD-WAN migration programme"})

    # 6b. A finished drill, closed with sign-offs, so reports have something final -------
    nb = new_event("Net Banking DR Drill", "Application", "High", NOW - timedelta(days=21, hours=-14), hours=4, template=False)
    nid = nb["id"]
    stn = mobilise(nb, {"deepa.app": "Application lead", "rahul.dba": "Database recovery", "farah.biz": "Business validation"})
    for sub in stn:
        api(sub, "POST", f"/test-stakeholders/{stn[sub]['id']}/respond", {"accept": True})
    api("meera.tm", "POST", f"/test-events/{nid}/transitions", {"action": "begin_readiness"})
    nb_acts = {}
    for code, title, sub in (("NB-01", "Fail over web and API tiers", "deepa.app"), ("NB-02", "Recover customer database", "rahul.dba"),
                             ("NB-03", "Business journey validation", "farah.biz")):
        nb_acts[code] = api("meera.tm", "POST", f"/test-events/{nid}/activities", {"activity_code": code, "title": title,
                                                                                 "stakeholder_id": stn[sub]["id"]})
    it = api("meera.tm", "POST", f"/test-events/{nid}/readiness-items", {"title": "Change request approved"})
    api("meera.tm", "PATCH", f"/test-readiness-items/{it['id']}", {"status": "Ready"})
    api("meera.tm", "POST", f"/test-events/{nid}/transitions", {"action": "mark_ready"})
    api("arjun.pm", "POST", f"/test-events/{nid}/approvals", {"approval_type": "Go/No-go", "decision": "Approved",
                                                            "comments": "Bridge staffed; customers notified"})
    api("meera.tm", "POST", f"/test-events/{nid}/start")
    for code, sub in (("NB-01", "deepa.app"), ("NB-02", "rahul.dba"), ("NB-03", "farah.biz")):
        move(sub, nb_acts[code]["id"], "In Progress", "Submitted")
    m1 = api("rahul.dba", "POST", f"/test-events/{nid}/measurements", {"measurement_type": "RTO", "target_value": 2,
             "target_unit": "Hours", "actual_value": 96, "unit": "Minutes"})
    m2 = api("rahul.dba", "POST", f"/test-events/{nid}/measurements", {"measurement_type": "RPO", "target_value": 5,
             "target_unit": "Minutes", "actual_value": 3, "unit": "Minutes"})
    api("meera.tm", "POST", f"/test-events/{nid}/transitions", {"action": "complete"})
    for m in (m1, m2):
        api("rohan.risk", "POST", f"/test-measurements/{m['id']}/validate")
    f = api("deepa.app", "POST", f"/test-events/{nid}/issues", {"title": "Login page cached old DNS for 6 minutes",
            "severity": "Medium", "issue_type": "Finding"})
    api("meera.tm", "POST", f"/test-issues/{f['id']}/remediation-actions", {"title": "Lower DNS TTL to 60 seconds",
        "owner_user_id": u["kiran.net"], "due_at": (NOW + timedelta(days=30)).isoformat()})
    nbv = api("meera.tm", "GET", f"/test-events/{nid}")
    api("meera.tm", "PUT", f"/test-events/{nid}/outcome", {"outcome": "Successful", "expected_version": nbv["version"],
        "summary": "Recovered in 96 minutes against a 2-hour RTO; data loss 3 minutes against a 5-minute RPO."})
    api("farah.biz", "POST", f"/test-events/{nid}/approvals", {"approval_type": "Business", "decision": "Approved"})
    api("rohan.risk", "POST", f"/test-events/{nid}/approvals", {"approval_type": "RiskReview", "decision": "Approved"})
    api("arjun.pm", "POST", f"/test-events/{nid}/approvals", {"approval_type": "Closure", "decision": "Approved"})
    api("meera.tm", "POST", f"/test-events/{nid}/close")

    # 7. A clash and a blackout, so the calendar has conflicts to show ------------
    new_event("ATM Network Failover", "Infrastructure", "High", NOW + timedelta(days=24, hours=3), hours=4, template=False)
    api("arjun.pm", "POST", "/blackout-periods", {
        "name": "Year-end change freeze", "starts_at": (NOW + timedelta(days=33)).isoformat(),
        "ends_at": (NOW + timedelta(days=40)).isoformat()})

    print("Demo data loaded for tenant BANK", ids["tenant"])


if __name__ == "__main__":
    main()
