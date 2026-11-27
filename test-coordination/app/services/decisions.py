"""Go/no-go decision records: what a decision rested on, who was there, who disagreed, and a replay of the test.

Sealing. When a go/no-go decision is recorded, a snapshot of everything it rested on is taken in the same transaction:
the readiness gate item by item (status, owner, evidence, exceptions with their approver and justification),
activities and blockers, open issues, evidence status, acknowledgements, who was present and any dissent raised in the
call. The snapshot's SHA-256 fingerprint is written into the tenant's hash-chained audit trail, so changing the record
later breaks either the fingerprint or the chain.

Dissent. Anyone on the test can add "I disagree" (or a concern) to a decision afterwards. Dissent is insert-only.

Replay. The state of a test at any moment is rebuilt from the audit trail: stage, readiness conditions, activities,
acknowledgements and decisions as they stood then.

Audit pack. A ZIP an examiner can check without ResilienceAI: decision records, the test's audit records with their
chain hashes, the timeline, a manifest of SHA-256 fingerprints and a standard-library Python verifier.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections import Counter
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, insert, select, update

from .. import authz
from ..authz import Role
from ..db import t
from ..domain import audit_chain
from ..domain.enums import ApprovalType as AT
from ..errors import Conflict, Forbidden, NotFound, Unprocessable
from ..security import Ctx
from . import audit
from .common import event, now, row

PACK_ROLES = (Role.ADMIN, Role.PROGRAMME_MANAGER, Role.RISK, Role.AUDITOR, Role.SENIOR_MGMT)


def fingerprint(snapshot: dict) -> str:
    return hashlib.sha256(audit_chain.canonical(snapshot).encode()).hexdigest()


def _names(ctx: Ctx) -> dict:
    u = t("users")
    return {r.id: r.display_name for r in ctx.conn.execute(select(u.c.id, u.c.display_name))}


# ---------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------
def build_snapshot(ctx: Ctx, ev: dict, approval: dict, present: list[UUID], dissents: list[dict]) -> dict:
    from .events import readiness_gate        # local import: events imports work, which imports this module
    names = _names(ctx)
    r, evd, ap = t("test_readiness_items"), t("test_evidence"), t("test_approvals")
    a, iss, st = t("test_activities"), t("test_issues"), t("test_stakeholders")
    gate = readiness_gate(ctx, ev)
    accepted = {x[0] for x in ctx.conn.execute(select(evd.c.readiness_item_id).where(and_(
        evd.c.event_id == ev["id"], evd.c.review_status == "Accepted", evd.c.readiness_item_id.isnot(None))))}
    exceptions = {x["subject_id"]: x for x in ctx.conn.execute(select(ap).where(and_(
        ap.c.event_id == ev["id"], ap.c.approval_type == AT.READINESS_EXCEPTION.value, ap.c.superseded_at.is_(None),
        ap.c.decision.in_(["Approved", "Conditional"])))).mappings()}
    items = []
    for i in ctx.conn.execute(select(r).where(r.c.event_id == ev["id"]).order_by(r.c.title)).mappings():
        exc = exceptions.get(i["id"])
        items.append({
            "title": i["title"], "status": i["status"], "mandatory": i["is_mandatory"],
            "evidence_required": i["evidence_required"], "evidence_accepted": i["id"] in accepted,
            "owner": names.get(i["owner_user_id"]),
            "exception": {"approved_by": names.get(exc["approver_user_id"]), "justification": exc["comments"],
                          "decided_at": exc["decided_at"].isoformat()} if exc else None,
        })
    acts = [dict(x) for x in ctx.conn.execute(select(a).where(a.c.event_id == ev["id"])).mappings()]
    n = now()
    issues = [{"code": x["issue_code"], "title": x["title"], "severity": x["severity"], "status": x["status"]}
              for x in ctx.conn.execute(select(iss).where(and_(iss.c.event_id == ev["id"], iss.c.status != "Closed",
                                                                iss.c.status != "Resolved"))).mappings()]
    ev_rows = list(ctx.conn.execute(select(evd.c.review_status, evd.c.scan_status).where(evd.c.event_id == ev["id"])).all())
    stakeholders = [dict(x) for x in ctx.conn.execute(select(st).where(and_(st.c.event_id == ev["id"], st.c.is_active))).mappings()]
    return {
        "format": "resilienceai.decision-record/1",
        "event": {"id": str(ev["id"]), "code": ev["event_code"], "title": ev["title"], "version": ev["version"],
                  "lifecycle": ev["lifecycle_status"], "criticality": ev["criticality"], "timezone": ev["timezone"],
                  "planned_start_at": ev["planned_start_at"].isoformat(), "planned_end_at": ev["planned_end_at"].isoformat()},
        "decision": {"type": approval["approval_type"], "decision": approval["decision"],
                     "decided_by": names.get(approval["approver_user_id"]), "decided_at": approval["decided_at"].isoformat(),
                     "conditions": approval["conditions"], "comments": approval["comments"],
                     "policy_version": approval["policy_version"]},
        "readiness": {"passed": gate.passed, "percent_ready": gate.percent_ready, "unmet": gate.unmet,
                      "excepted": gate.excepted, "items": items},
        "activities": {
            "by_status": dict(Counter(x["status"] for x in acts)),
            "blocked": [{"code": x["activity_code"], "title": x["title"], "reason": x["blocked_reason"]}
                        for x in acts if x["status"] == "Blocked"],
            "overdue": [{"code": x["activity_code"], "title": x["title"], "due_at": x["due_at"].isoformat()}
                        for x in acts if x["due_at"] and x["due_at"] < n and x["status"] != "Completed"],
        },
        "open_issues": issues,
        "evidence": {"by_review_status": dict(Counter(x[0] for x in ev_rows)),
                     "awaiting_malware_scan": sum(1 for x in ev_rows if x[1] == "Pending")},
        "people": {
            "stakeholders": len(stakeholders),
            "acknowledged": sum(1 for x in stakeholders if x["assignment_status"] == "Accepted"),
            "not_acknowledged": sorted(names.get(x["user_id"], "?") for x in stakeholders if x["assignment_status"] != "Accepted"),
            "present": sorted(names.get(p, "?") for p in present),
        },
        "dissent_in_call": [{"by": names.get(d["user_id"]), "kind": d["kind"], "reason": d["reason"]} for d in dissents],
    }


def seal(ctx: Ctx, ev: dict, approval: dict, present: list[UUID] | None, dissents: list[dict] | None) -> dict:
    """Write the decision record for a just-recorded go/no-go decision (same transaction)."""
    present = list(dict.fromkeys(present or []))
    dissents = [d for d in (dissents or []) if (d.get("reason") or "").strip()]
    u = t("users")
    people = set(present) | {d["user_id"] for d in dissents}
    if people:
        found = {x[0] for x in ctx.conn.execute(select(u.c.id).where(and_(u.c.id.in_(people), u.c.is_active)))}
        if people - found:
            raise Unprocessable("Everyone recorded as present or disagreeing must be an active user")
    for d in dissents:
        d.setdefault("kind", "Dissent")
        if d["kind"] not in ("Dissent", "Concern"):
            raise Unprocessable("kind must be Dissent or Concern")
    snapshot = build_snapshot(ctx, ev, approval, present, dissents)
    digest = fingerprint(snapshot)
    dr = t("test_decision_records")
    rec = dict(ctx.conn.execute(insert(dr).values(
        tenant_id=ctx.me.tenant_id, event_id=ev["id"], approval_id=approval["id"], approval_type=approval["approval_type"],
        decision=approval["decision"], decided_by=approval["approver_user_id"], decided_at=approval["decided_at"],
        present_user_ids=present, snapshot=snapshot, content_sha256=digest).returning(dr)).mappings().one())
    dd = t("test_decision_dissents")
    for d in dissents:
        ctx.conn.execute(insert(dd).values(tenant_id=ctx.me.tenant_id, decision_record_id=rec["id"], user_id=d["user_id"],
                                           recorded_by=ctx.me.user_id, kind=d["kind"], reason=d["reason"].strip(),
                                           during_decision=True))
    anchor = audit.record(ctx, "decision.sealed", "DecisionRecord", rec["id"], event_id=ev["id"],
                          new={"approval_id": str(approval["id"]), "decision": approval["decision"],
                               "content_sha256": digest, "present": len(present), "dissent": len(dissents)})
    ctx.conn.execute(update(dr).where(dr.c.id == rec["id"]).values(audit_seq=anchor["seq"], audit_row_hash=anchor["row_hash"]))
    return {**rec, "audit_seq": anchor["seq"], "audit_row_hash": anchor["row_hash"]}


# ---------------------------------------------------------------------------
# Reading, dissent
# ---------------------------------------------------------------------------
def _can_add_dissent(ctx: Ctx, ev: dict) -> bool:
    if ctx.me.is_external or ctx.me.has(Role.AUDITOR):
        return False
    st = t("test_stakeholders")
    on_test = ctx.conn.execute(select(st.c.id).where(and_(st.c.event_id == ev["id"], st.c.user_id == ctx.me.user_id))).first()
    return bool(on_test) or authz.can_manage_event(ctx, ev) or ctx.me.has(Role.RISK, Role.PROGRAMME_MANAGER)


def list_records(ctx: Ctx, event_id: UUID) -> dict:
    ev = event(ctx, event_id)
    if ctx.me.is_external:
        raise Forbidden("Decision records aren't shared with external vendors")
    names = _names(ctx)
    dr, dd, au = t("test_decision_records"), t("test_decision_dissents"), t("test_event_audit")
    out = []
    for r in ctx.conn.execute(select(dr).where(dr.c.event_id == event_id).order_by(dr.c.decided_at.desc())).mappings():
        anchored = ctx.conn.execute(select(au.c.row_hash, au.c.new_values).where(and_(
            au.c.tenant_id == ctx.me.tenant_id, au.c.seq == r["audit_seq"]))).first()
        intact = fingerprint(r["snapshot"]) == r["content_sha256"]
        in_chain = bool(anchored and anchored.row_hash == r["audit_row_hash"]
                        and (anchored.new_values or {}).get("content_sha256") == r["content_sha256"])
        dissent = [{"id": x["id"], "by": names.get(x["user_id"]), "recorded_by": names.get(x["recorded_by"]),
                    "kind": x["kind"], "reason": x["reason"], "during_decision": x["during_decision"], "raised_at": x["raised_at"]}
                   for x in ctx.conn.execute(select(dd).where(dd.c.decision_record_id == r["id"]).order_by(dd.c.raised_at)).mappings()]
        out.append({"id": r["id"], "approval_type": r["approval_type"], "decision": r["decision"],
                    "decided_by": names.get(r["decided_by"]), "decided_at": r["decided_at"], "snapshot": r["snapshot"],
                    "content_sha256": r["content_sha256"], "audit_seq": r["audit_seq"],
                    "verified": intact and in_chain, "fingerprint_matches": intact, "anchored_in_audit_chain": in_chain,
                    "dissent": dissent})
    return {"records": out, "can_add_dissent": ev["lifecycle_status"] != "Closed" and _can_add_dissent(ctx, ev),
            "can_download_pack": ctx.me.has(*PACK_ROLES) or authz.can_manage_event(ctx, ev)}


def add_dissent(ctx: Ctx, record_id: UUID, kind: str, reason: str) -> dict:
    rec = row(ctx, "test_decision_records", record_id)
    ev = event(ctx, rec["event_id"])
    if not _can_add_dissent(ctx, ev):
        raise Forbidden("Only people on this test, its managers and risk can add dissent")
    if ev["lifecycle_status"] == "Closed":
        raise Conflict("The test is closed; its decision records can no longer take new dissent")
    if kind not in ("Dissent", "Concern"):
        raise Unprocessable("kind must be Dissent or Concern")
    if not (reason or "").strip():
        raise Unprocessable("Say why: dissent without a reason can't be acted on")
    dd = t("test_decision_dissents")
    new = dict(ctx.conn.execute(insert(dd).values(
        tenant_id=ctx.me.tenant_id, decision_record_id=record_id, user_id=ctx.me.user_id, recorded_by=ctx.me.user_id,
        kind=kind, reason=reason.strip(), during_decision=False).returning(dd)).mappings().one())
    audit.record(ctx, f"decision.{kind.lower()}_added", "DecisionRecord", record_id, event_id=ev["id"],
                 new={"dissent_id": str(new["id"]), "kind": kind, "reason": reason.strip()})
    return new


# ---------------------------------------------------------------------------
# Replay: the test as it stood at a moment, rebuilt from the audit trail
# ---------------------------------------------------------------------------
LIFECYCLE_MARKS = {"lifecycle.submit", "lifecycle.approve_plan", "lifecycle.begin_readiness", "lifecycle.mark_ready",
                   "lifecycle.start", "lifecycle.pause", "lifecycle.resume", "lifecycle.complete", "lifecycle.close",
                   "lifecycle.cancel", "lifecycle.return_to_mobilising", "lifecycle.return_to_readiness"}


def _mark_label(action: str, new: dict, names: dict) -> str | None:
    if action in LIFECYCLE_MARKS or action.startswith("lifecycle."):
        return f"Stage: {new.get('lifecycle_status')}" if new.get("lifecycle_status") else None
    if action == "decision.sealed":
        return f"Go/no-go: {new.get('decision')}"
    if action == "approval.recorded" and new.get("approval_type") == "ReadinessException":
        return "Readiness exception granted"
    if action == "issue.created":
        return f"Blocker raised: {new.get('title')}"
    if action.startswith("decision.") and action.endswith("_added"):
        return f"{new.get('kind')} added"
    return None


def replay(ctx: Ctx, event_id: UUID, at: datetime | None = None) -> dict:
    ev = event(ctx, event_id)
    if ctx.me.is_external:
        raise Forbidden("Replay isn't shared with external vendors")
    names = _names(ctx)
    au, a, r = t("test_event_audit"), t("test_activities"), t("test_readiness_items")
    rows = [dict(x) for x in ctx.conn.execute(select(au).where(au.c.event_id == event_id).order_by(au.c.seq)).mappings()]
    if not rows:
        raise NotFound("No history recorded for this test")
    start, end = rows[0]["occurred_at"], rows[-1]["occurred_at"]
    at = min(max(at or end, start), end)
    upto = [x for x in rows if x["occurred_at"] <= at]

    marks = []
    for x in rows:
        label = _mark_label(x["action"], x["new_values"] or {}, names)
        if label:
            marks.append({"at": x["occurred_at"], "label": label, "seq": x["seq"], "by": names.get(x["actor_user_id"]) or "system"})

    # Stage
    stage = "Draft"
    for x in upto:
        nv = x["new_values"] or {}
        if x["action"] == "event.created":
            stage = nv.get("lifecycle_status") or "Draft"
        elif x["action"].startswith("lifecycle.") and nv.get("lifecycle_status"):
            stage = nv["lifecycle_status"]
    # Activities: existence by created_at, status from audit updates
    acts = {x["id"]: {"code": x["activity_code"], "title": x["title"], "status": "Not Started", "created_at": x["created_at"]}
            for x in ctx.conn.execute(select(a).where(a.c.event_id == event_id)).mappings()}
    template_at = next((x["occurred_at"] for x in rows if x["action"] in ("event.template_applied", "event.created")), start)
    items = {x["id"]: {"title": x["title"], "mandatory": x["is_mandatory"], "status": "Pending", "exists_from": template_at}
             for x in ctx.conn.execute(select(r).where(r.c.event_id == event_id)).mappings()}
    for x in rows:                                  # when was each readiness item added (template items: at template time)
        if x["action"] == "readiness.item_added" and x["entity_id"] in items:
            items[x["entity_id"]]["exists_from"] = x["occurred_at"]
    excepted, decision, acks, stakeholders = set(), None, set(), set()
    for x in upto:
        nv, eid = x["new_values"] or {}, x["entity_id"]
        if x["action"] in ("activity.updated", "activity.created") and eid in acts and nv.get("status"):
            acts[eid]["status"] = nv["status"]
        elif x["action"] == "readiness.updated" and eid in items and nv.get("status"):
            items[eid]["status"] = nv["status"]
        elif x["action"] == "approval.recorded" and nv.get("approval_type") == "ReadinessException" \
                and nv.get("decision") in ("Approved", "Conditional"):
            excepted.add(nv.get("subject_id"))
        elif x["action"] == "approval.recorded" and nv.get("approval_type") == "Go/No-go":
            decision = {"decision": nv.get("decision"), "by": names.get(x["actor_user_id"]), "at": x["occurred_at"]}
        elif x["action"] == "approvals.superseded" and "Go/No-go" in (nv.get("types") or []):
            decision = None if decision is None else {**decision, "superseded_at": x["occurred_at"]}
        elif x["action"] == "stakeholder.assigned":
            stakeholders.add(eid)
        elif x["action"] == "stakeholder.accepted":
            acks.add(eid)
    live_acts = [v for v in acts.values() if v["created_at"] <= at]
    live_items = [{"title": v["title"], "mandatory": v["mandatory"], "status": v["status"],
                   "excepted": str(k) in excepted} for k, v in items.items() if v["exists_from"] <= at]
    unmet = [i["title"] for i in live_items if i["mandatory"] and i["status"] != "Ready" and not i["excepted"]]
    return {
        "event": {"id": ev["id"], "code": ev["event_code"], "title": ev["title"]},
        "start": start, "end": end, "at": at, "marks": marks,
        "state": {
            "stage": stage,
            "readiness": {"items": sorted(live_items, key=lambda i: i["title"]), "unmet": sorted(unmet),
                          "passed": bool(live_items) and not unmet},
            "activities": {"by_status": dict(Counter(v["status"] for v in live_acts)),
                           "blocked": sorted(f"{v['code']} {v['title']}" for v in live_acts if v["status"] == "Blocked")},
            "people": {"assigned": len(stakeholders), "acknowledged": len(acks & stakeholders)},
            "go_decision": decision,
            "recent": [{"at": x["occurred_at"], "action": x["action"], "by": names.get(x["actor_user_id"]) or "system"}
                       for x in upto[-8:]][::-1],
        },
        "note": "Rebuilt from the hash-chained audit trail. Sealed decision records show the full state at each decision.",
    }


# ---------------------------------------------------------------------------
# Audit pack
# ---------------------------------------------------------------------------
VERIFY_PY = r'''"""Verify a ResilienceAI audit pack without ResilienceAI. Python 3.9+, standard library only.

    python verify.py            (run inside the unzipped folder)

Checks: 1) every file matches the SHA-256 in manifest.json; 2) every audit record's hash is recomputed from its own
content and the previous record's hash; consecutive records link; 3) every decision record's fingerprint matches its
snapshot and is the fingerprint written into the audit trail at the recorded position.
"""
import hashlib, json, sys
from pathlib import Path

here = Path(__file__).resolve().parent
ok = True

def say(good, text):
    global ok
    ok = ok and good
    print(("PASS  " if good else "FAIL  ") + text)

def canonical(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"))

manifest = json.loads((here / "manifest.json").read_text())
for name, digest in manifest["files"].items():
    say(hashlib.sha256((here / name).read_bytes()).hexdigest() == digest, f"file {name} matches the manifest")

KEYS = ("id", "tenant_id", "seq", "event_id", "actor_user_id", "action", "entity_type", "entity_id",
        "old_values", "new_values", "occurred_at", "correlation_id")
rows = json.loads((here / "audit_records.json").read_text())
bad, prev = [], None
for r in rows:
    payload = {k: r.get(k) for k in KEYS}
    if hashlib.sha256((r["prev_hash"] + canonical(payload)).encode()).hexdigest() != r["row_hash"]:
        bad.append(r["seq"])
    if prev and r["seq"] == prev["seq"] + 1 and r["prev_hash"] != prev["row_hash"]:
        bad.append(r["seq"])
    prev = r
say(not bad, f"{len(rows)} audit records recompute and link" + (f" (problems at seq {bad})" if bad else ""))

by_seq = {r["seq"]: r for r in rows}
for d in json.loads((here / "decision_records.json").read_text()):
    fp = hashlib.sha256(canonical(d["snapshot"]).encode()).hexdigest()
    a = by_seq.get(d["audit_seq"])
    anchored = bool(a and a["row_hash"] == d["audit_row_hash"] and (a.get("new_values") or {}).get("content_sha256") == fp)
    say(fp == d["content_sha256"] and anchored, f"decision {d['decision']} at {d['decided_at']}: fingerprint and audit anchor")

print("\nRESULT: " + ("all checks passed" if ok else "VERIFICATION FAILED"))
sys.exit(0 if ok else 1)
'''

README = """ResilienceAI audit pack: {code} {title}
Exported {exported} by {who}.

Files
  decision_records.json  each go/no-go decision: the sealed snapshot of what it rested on, who was present, dissent,
                         its SHA-256 fingerprint and the audit-trail position where the fingerprint was written
  audit_records.json     every audit record for this test, with the hash-chain values (prev_hash, row_hash)
  timeline.csv           the test's timeline (log entries and audit actions) in time order
  manifest.json          SHA-256 of each file above
  verify.py              checks all of this with Python's standard library: python verify.py

The audit trail is one hash chain per organisation; this pack holds the records for this test. Each record's hash
is recomputed from its own content, so a changed record is detected. Records from other tests are not included, so
links are checked between consecutive records only. ResilienceAI can verify the full chain on request.
"""


def _json(o) -> bytes:
    return json.dumps(o, indent=2, sort_keys=True, default=audit_chain._default, ensure_ascii=False).encode()


def audit_pack(ctx: Ctx, event_id: UUID) -> tuple[bytes, str]:
    ev = event(ctx, event_id)
    if not (ctx.me.has(*PACK_ROLES) or authz.can_manage_event(ctx, ev)):
        raise Forbidden("Audit packs are for the test's managers, programme, risk, audit and senior management")
    names = _names(ctx)
    au, dr, dd = t("test_event_audit"), t("test_decision_records"), t("test_decision_dissents")
    audit_rows = []
    for x in ctx.conn.execute(select(au).where(au.c.event_id == event_id).order_by(au.c.seq)).mappings():
        hp = audit_chain.hash_payload(dict(x))
        audit_rows.append({**hp, "prev_hash": x["prev_hash"], "row_hash": x["row_hash"],
                           "actor_name": names.get(x["actor_user_id"])})
    records = []
    for r in ctx.conn.execute(select(dr).where(dr.c.event_id == event_id).order_by(dr.c.decided_at)).mappings():
        dissent = [{"by": names.get(x["user_id"]), "kind": x["kind"], "reason": x["reason"],
                    "during_decision": x["during_decision"], "raised_at": x["raised_at"]}
                   for x in ctx.conn.execute(select(dd).where(dd.c.decision_record_id == r["id"]).order_by(dd.c.raised_at)).mappings()]
        records.append({"id": r["id"], "decision": r["decision"], "decided_at": r["decided_at"],
                        "decided_by": names.get(r["decided_by"]), "snapshot": r["snapshot"],
                        "content_sha256": r["content_sha256"], "audit_seq": r["audit_seq"],
                        "audit_row_hash": r["audit_row_hash"], "dissent_added_later": [d for d in dissent if not d["during_decision"]]})
    from .views import timeline
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["occurred_at_utc", "kind", "type", "message_or_entity", "actor"])
    for x in timeline(ctx, event_id, limit=100000):
        w.writerow([x["occurred_at"].astimezone(timezone.utc).isoformat(), x["kind"], x["type"],
                    x.get("message") or x.get("entity") or "", x.get("actor") or ""])
    files = {"decision_records.json": _json(records), "audit_records.json": _json(audit_rows),
             "timeline.csv": buf.getvalue().encode()}
    manifest = {"format": "resilienceai.audit-pack/1", "event": {"code": ev["event_code"], "title": ev["title"]},
                "exported_at": now().isoformat(), "exported_by": ctx.me.display_name,
                "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
        z.writestr("manifest.json", _json(manifest))
        z.writestr("verify.py", VERIFY_PY)
        z.writestr("README.txt", README.format(code=ev["event_code"], title=ev["title"],
                                               exported=now().strftime("%d %b %Y %H:%M UTC"), who=ctx.me.display_name))
    data = out.getvalue()
    audit.record(ctx, "audit_pack.exported", "Event", event_id, event_id=event_id,
                 new={"sha256": hashlib.sha256(data).hexdigest(), "decision_records": len(records), "audit_records": len(audit_rows)})
    return data, f"{ev['event_code']}-audit-pack-{datetime.now(timezone.utc):%Y%m%d}.zip"

