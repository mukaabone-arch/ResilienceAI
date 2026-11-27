"""Cross-test learning: what went wrong in related earlier tests, brought to the test being planned.

Related tests are earlier tests of the same series or template, or covering the same business services. From
them we read what is already recorded: blockers and how long they lasted, findings and their root causes,
remediation that is still open, missed RTO/RPO targets, activities that overran, and evidence that was sent back.
Each lesson is matched to the activity it concerns in this test, grouped when it happened more than once, and
ranked. Every lesson links to the records it came from.

Planners can turn a lesson into a readiness condition of this test (so the go decision waits for it) or set it
aside with a reason. Nothing is decided automatically, and nothing here uses an external model.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, delete, insert, select

from .. import authz
from ..db import t
from ..domain.enums import LifecycleStatus as S
from ..errors import Conflict, Forbidden, NotFound, Unprocessable
from ..security import Ctx
from . import audit
from .common import event, log, now

PLANNING = {S.DRAFT, S.PLANNED, S.MOBILISING, S.READINESS}
LEARNED_FROM = ("Closed", "Closure", "Live", "Paused", "Ready", "Readiness")   # tests that have something to learn from
SEVERITY = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}
STOP = set("the a an and of to for on in at with by from is be up check confirm verify prepare run".split())
MAX_RELATED = 6


# ---------------------------------------------------------------------------
# Which earlier tests are related
# ---------------------------------------------------------------------------
def _template_key(ctx: Ctx, template_id) -> str | None:
    if not template_id:
        return None
    tp = t("test_templates")
    return ctx.conn.execute(select(tp.c.template_key).where(tp.c.id == template_id)).scalar()


def related_tests(ctx: Ctx, ev: dict) -> list[dict]:
    e, sc, tp, bs = t("test_events"), t("test_scope_items"), t("test_templates"), t("business_services")
    mine_svc = {r[0] for r in ctx.conn.execute(select(sc.c.business_service_id).where(and_(
        sc.c.event_id == ev["id"], sc.c.business_service_id.isnot(None))))}
    key = _template_key(ctx, ev.get("template_id"))
    cands = [dict(r) for r in ctx.conn.execute(
        select(e.c.id, e.c.event_code, e.c.title, e.c.category, e.c.series_id, e.c.planned_start_at, e.c.actual_end_at,
               e.c.lifecycle_status, e.c.outcome, e.c.owner_user_id, e.c.test_manager_user_id, e.c.timezone,
               tp.c.template_key)
        .outerjoin(tp, tp.c.id == e.c.template_id)
        .where(and_(e.c.id != ev["id"], e.c.planned_start_at < ev["planned_start_at"],
                    e.c.lifecycle_status.in_(LEARNED_FROM)))).mappings()]
    if not cands:
        return []
    svc_rows = ctx.conn.execute(select(sc.c.event_id, bs.c.name).join(bs, bs.c.id == sc.c.business_service_id).where(and_(
        sc.c.event_id.in_([c["id"] for c in cands]), sc.c.business_service_id.in_(mine_svc or {UUID(int=0)})))).all()
    shared: dict[UUID, list[str]] = defaultdict(list)
    for eid, name in svc_rows:
        shared[eid].append(name)
    out = []
    for c in cands:
        why, score = [], 0
        if ev.get("series_id") and c["series_id"] == ev["series_id"]:
            score += 4
            why.append("earlier run of the same series")
        if key and c["template_key"] == key:
            score += 3
            why.append("same test template")
        if shared.get(c["id"]):
            score += 2 * len(shared[c["id"]])
            why.append("also covered " + ", ".join(sorted(shared[c["id"]])))
        if c["category"] == ev["category"]:
            score += 1
            if not why:
                why.append(f"same category ({c['category']})")
        if score >= 2 or (score == 1 and c["lifecycle_status"] in ("Closed", "Closure")):
            out.append({**c, "score": score, "why": why})
    out.sort(key=lambda c: (-c["score"], -c["planned_start_at"].timestamp()))
    return out[:MAX_RELATED]


# ---------------------------------------------------------------------------
# Matching an earlier activity to one in this test
# ---------------------------------------------------------------------------
def _words(s: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP and len(w) > 2}


def _match(prev: dict, current: list[dict]) -> dict | None:
    for a in current:
        if a["activity_code"] == prev["activity_code"] and len(_words(a["title"]) & _words(prev["title"])) >= 1:
            return a
    best, best_score = None, 0.0
    pw = _words(prev["title"])
    for a in current:
        aw = _words(a["title"])
        if not pw or not aw:
            continue
        j = len(pw & aw) / len(pw | aw)
        if j > best_score:
            best, best_score = a, j
    return best if best_score >= 0.5 else None


def _dur(td: timedelta) -> str:
    h = td.total_seconds() / 3600
    if h < 1:
        return f"{max(1, round(h * 60))} min"
    if h < 48:
        return f"{h:.1f} h".replace(".0 h", " h")
    return f"{round(h / 24)} days"


# ---------------------------------------------------------------------------
# Lessons from one earlier test
# ---------------------------------------------------------------------------
def _blocked_spans(ctx: Ctx, rel: dict, acts: dict[UUID, dict]) -> list[dict]:
    """When each activity of the earlier test was blocked, for how long, and why (from the audit trail)."""
    au = t("test_event_audit")
    rows = ctx.conn.execute(select(au.c.entity_id, au.c.new_values, au.c.occurred_at).where(and_(
        au.c.event_id == rel["id"], au.c.entity_type == "Activity", au.c.action.in_(["activity.updated", "activity.created"])))
        .order_by(au.c.seq)).all()
    open_: dict[UUID, tuple[datetime, str | None]] = {}
    spans = []
    for eid, new, at in rows:
        status = (new or {}).get("status")
        if status is None or eid not in acts:
            continue
        if status == "Blocked" and eid not in open_:
            open_[eid] = (at, (new or {}).get("blocked_reason") or (new or {}).get("comment"))
        elif status != "Blocked" and eid in open_:
            start, reason = open_.pop(eid)
            spans.append({"activity": acts[eid], "from": start, "to": at, "reason": reason, "resolved": True})
    end = rel.get("actual_end_at") or now()
    for eid, (start, reason) in open_.items():
        spans.append({"activity": acts[eid], "from": start, "to": end, "reason": reason or acts[eid].get("blocked_reason"),
                      "resolved": False})
    return spans


def _lessons_from(ctx: Ctx, rel: dict, current_acts: list[dict]) -> list[dict]:
    a, i, ra, m, evd, u = (t(x) for x in ("test_activities", "test_issues", "test_remediation_actions",
                                          "test_measurements", "test_evidence", "users"))
    names = {r.id: r.display_name for r in ctx.conn.execute(select(u.c.id, u.c.display_name))}
    acts = {r["id"]: dict(r) for r in ctx.conn.execute(select(a).where(a.c.event_id == rel["id"])).mappings()}
    tz = ZoneInfo(rel.get("timezone") or "UTC")
    src = {"event_id": str(rel["id"]), "event_code": rel["event_code"], "event_title": rel["title"],
           "when": rel["planned_start_at"], "outcome": rel["outcome"], "relation": rel["score"]}
    out: list[dict] = []

    def matched(act: dict | None) -> dict | None:
        if not act:
            return None
        hit = _match(act, current_acts)
        return {"id": str(hit["id"]), "code": hit["activity_code"], "title": hit["title"]} if hit else None

    def lesson(kind, about, severity, title, detail, records, act=None, suggestion=None, extra=None):
        out.append({"kind": kind, "about": about, "severity": severity, "title": title, "detail": detail,
                    "source": src, "records": records, "applies_to": matched(act),
                    "prev_activity": {"code": act["activity_code"], "title": act["title"]} if act else None,
                    "suggestion": suggestion, **(extra or {})})

    # Blockers, with how long they held things up.
    issues = [dict(r) for r in ctx.conn.execute(select(i).where(i.c.event_id == rel["id"])).mappings()]
    blocker_issue = {x["activity_id"]: x for x in issues if x["issue_type"] == "Blocker" and x["activity_id"]}
    for sp in _blocked_spans(ctx, rel, acts):
        act = sp["activity"]
        iss = blocker_issue.get(act["id"])
        reason = sp["reason"] or (iss and iss["title"]) or "no reason recorded"
        short = reason if len(reason) <= 110 else reason[:109].rsplit(" ", 1)[0] + "…"
        held = sp["to"] - sp["from"]
        sev = 3 if held >= timedelta(hours=4) else 2
        lesson("blocker", f"act:{act['activity_code']}:{'/'.join(sorted(_words(act['title'])))}", sev,
               f"{act['activity_code']} was blocked for {_dur(held)}",
               f"{reason}." + ("" if sp["resolved"] else " It was never unblocked."),
               [{"type": "Activity", "id": str(act["id"]), "label": act["activity_code"]}]
               + ([{"type": "Issue", "id": str(iss["id"]), "label": iss["issue_code"]}] if iss else []),
               act, f"{{act}} won't be blocked again: {short}",
               {"hours": round(held.total_seconds() / 3600, 1)})
    # Findings and incidents, with root cause (those with open remediation are covered by that lesson below).
    open_fix = {r[0] for r in ctx.conn.execute(select(ra.c.issue_id).where(and_(
        ra.c.issue_id.in_([x["id"] for x in issues] or [UUID(int=0)]), ra.c.status != "Accepted")))}
    for x in issues:
        if x["issue_type"] == "Blocker" or SEVERITY.get(x["severity"], 1) < 2 or x["id"] in open_fix:
            continue
        act = acts.get(x["activity_id"]) if x["activity_id"] else None
        cause = f" Root cause: {x['root_cause']}." if x.get("root_cause") else ""
        lesson("finding", f"issue:{'/'.join(sorted(_words(x['title'])))[:80]}", SEVERITY[x["severity"]],
               f"{x['issue_type']} ({x['severity'].lower()}): {x['title']}",
               f"Raised in {rel['event_code']}{' on ' + act['activity_code'] if act else ''}.{cause}",
               [{"type": "Issue", "id": str(x["id"]), "label": x["issue_code"]}], act,
               f"Confirm the fix for this earlier {x['issue_type'].lower()} is in place: {x['title']}")
    # Remediation still open: the most important kind of lesson.
    by_issue = {x["id"]: x for x in issues}
    for r in ctx.conn.execute(select(ra).where(and_(ra.c.issue_id.in_(list(by_issue) or [UUID(int=0)]),
                                                     ra.c.status != "Accepted"))).mappings():
        iss = by_issue[r["issue_id"]]
        late = now() - r["due_at"]
        overdue = late > timedelta(0)
        lesson("open_action", f"action:{r['id']}", 5 if overdue else 4,
               f"Remediation {r['action_code']} is still {r['status'].lower()}: {r['title']}",
               f"Agreed after {rel['event_code']}: {iss['title']}."
               + (f" Root cause: {iss['root_cause']}." if iss.get("root_cause") else "")
               + f" Owner {names.get(r['owner_user_id'], 'unknown')}, "
               + (f"due {r['due_at'].astimezone(tz):%d %b}, {_dur(late)} overdue." if overdue
                  else f"due {r['due_at'].astimezone(tz):%d %b}."),
               [{"type": "RemediationAction", "id": str(r["id"]), "label": r["action_code"]},
                {"type": "Issue", "id": str(iss["id"]), "label": iss["issue_code"]}],
               acts.get(iss["activity_id"]) if iss["activity_id"] else None,
               f"Remediation {r['action_code']} accepted before go: {r['title']}",
               {"overdue": overdue})
    # Missed recovery targets.
    for x in ctx.conn.execute(select(m).where(and_(m.c.event_id == rel["id"], m.c.target_met.is_(False)))).mappings():
        took = (_dur(timedelta(seconds=float(x["actual_canonical"]))) if x["actual_canonical"] is not None
                else f"{x['actual_value']:g} {x['unit'].lower()}")
        goal = (_dur(timedelta(seconds=float(x["target_canonical"]))) if x["target_canonical"] is not None
                else f"{x['target_value']:g} {x['target_unit'].lower()}")
        lesson("missed_target", f"target:{x['measurement_type']}", 4,
               f"{x['measurement_type']} target missed: {took} against {goal}",
               f"Measured in {rel['event_code']}" + (" and validated." if x["validated_by"] else "."),
               [{"type": "Measurement", "id": str(x["id"]), "label": x["measurement_type"]}], None,
               f"Show how the {x['measurement_type']} target will be met this time (missed in {rel['event_code']})")
    # Activities that overran their due time.
    for act in acts.values():
        if act["completed_at"] and act["due_at"] and act["completed_at"] - act["due_at"] >= timedelta(minutes=30):
            late = act["completed_at"] - act["due_at"]
            lesson("overran", f"late:{act['activity_code']}:{'/'.join(sorted(_words(act['title'])))}",
                   3 if late >= timedelta(hours=4) else 2,
                   f"{act['activity_code']} finished {_dur(late)} after it was due",
                   f"{act['title']} in {rel['event_code']}.",
                   [{"type": "Activity", "id": str(act["id"]), "label": act["activity_code"]}], act,
                   f"Allow more time for {{act}}: it overran by {_dur(late)} last time", {"hours": round(late.total_seconds() / 3600, 1)})
    # Evidence that was sent back.
    for x in ctx.conn.execute(select(evd).where(and_(evd.c.event_id == rel["id"], evd.c.review_status == "Rejected"))).mappings():
        act = acts.get(x["activity_id"]) if x["activity_id"] else None
        lesson("rejected_evidence", f"evidence:{act['activity_code'] if act else 'readiness'}:{'/'.join(sorted(_words(act['title'] if act else '')))}", 2,
               f"Evidence for {act['activity_code'] if act else 'a readiness condition'} was sent back",
               f"Reviewer's reason: {x['review_comment'] or 'none given'}",
               [{"type": "Evidence", "id": str(x["id"]), "label": x["evidence_code"]}], act,
               f"Agree up front what evidence {{act}} needs (sent back last time: {x['review_comment'] or 'no reason given'})")
    return out


# ---------------------------------------------------------------------------
# Putting it together for one test
# ---------------------------------------------------------------------------
def _key(lesson: dict) -> str:
    target = lesson["applies_to"]["code"] if lesson["applies_to"] else lesson["about"]
    raw = f"{lesson['kind']}|{target}|{lesson['about']}" if lesson["kind"] != "open_action" else lesson["about"]
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _suggest(lesson: dict) -> str:
    act = lesson["applies_to"]["code"] if lesson["applies_to"] else (lesson["prev_activity"] or {}).get("code", "this step")
    return lesson["suggestion"].replace("{act}", act)[:250]


def build(ctx: Ctx, ev: dict) -> dict:
    a = t("test_activities")
    current = [dict(r) for r in ctx.conn.execute(select(a.c.id, a.c.activity_code, a.c.title, a.c.status, a.c.blocked_reason)
                                                 .where(a.c.event_id == ev["id"])).mappings()]
    status_of = {str(x["id"]): x for x in current}
    rel = related_tests(ctx, ev)
    raw = [x for r in rel for x in _lessons_from(ctx, r, current)]
    groups: dict[str, list[dict]] = defaultdict(list)
    for x in raw:
        groups[_key(x)].append(x)
    lessons = []
    for key, xs in groups.items():
        xs.sort(key=lambda x: x["source"]["when"], reverse=True)
        head = dict(xs[0])
        tests = {x["source"]["event_id"] for x in xs}
        head["key"] = key
        head["times"] = len(tests)
        head["also_in"] = [x["source"] for x in xs[1:] if x["source"]["event_id"] != head["source"]["event_id"]]
        head["records"] = [r | {"event_code": x["source"]["event_code"]} for x in xs for r in x["records"]]
        age_days = max(0, (now() - head["source"]["when"]).days)
        head["rank"] = (head["severity"] * 10 + (len(tests) - 1) * 12 + (8 if head["applies_to"] else 0)
                        + 2 * max(x["source"]["relation"] for x in xs) - min(age_days // 60, 10))
        head["suggested_condition"] = _suggest(head)
        now_act = status_of.get(head["applies_to"]["id"]) if head["applies_to"] else None
        head["happening_again"] = bool(now_act and head["kind"] == "blocker" and now_act["status"] == "Blocked")
        if head["happening_again"]:
            head["rank"] += 25
            head["now"] = f"{now_act['activity_code']} is blocked again in this test: {now_act['blocked_reason'] or 'no reason given'}"
        if head["times"] > 1:
            head["title"] = f"{head['title']} (happened in {head['times']} earlier tests)"
        lessons.append(head)
    lessons.sort(key=lambda x: -x["rank"])

    dec = t("test_lesson_decisions")
    u, r = t("users"), t("test_readiness_items")
    decisions = {d["lesson_key"]: dict(d) for d in ctx.conn.execute(
        select(dec, u.c.display_name.label("decided_by_name"), r.c.title.label("readiness_title"), r.c.status.label("readiness_status"))
        .join(u, u.c.id == dec.c.decided_by).outerjoin(r, r.c.id == dec.c.readiness_item_id)
        .where(dec.c.event_id == ev["id"])).mappings()}
    for x in lessons:
        d = decisions.get(x["key"])
        x["decision"] = None if not d else {
            "decision": d["decision"], "reason": d["reason"], "by": d["decided_by_name"], "at": d["decided_at"],
            "readiness_item_id": d["readiness_item_id"], "readiness_title": d["readiness_title"],
            "readiness_status": d["readiness_status"]}
    can = authz.can_manage_event(ctx, ev) and S(ev["lifecycle_status"]) in PLANNING
    return {
        "related": [{"id": c["id"], "event_code": c["event_code"], "title": c["title"], "when": c["planned_start_at"],
                     "status": c["lifecycle_status"], "outcome": c["outcome"], "why": c["why"]} for c in rel],
        "lessons": lessons,
        "summary": {"total": len(lessons), "open": sum(1 for x in lessons if not x["decision"]),
                    "applied": sum(1 for x in lessons if x["decision"] and x["decision"]["decision"] == "Applied"),
                    "dismissed": sum(1 for x in lessons if x["decision"] and x["decision"]["decision"] == "Dismissed"),
                    "open_actions": sum(1 for x in lessons if x["kind"] == "open_action")},
        "can_decide": can,
        "note": None if can else ("Lessons can be acted on while the test is being planned." if not ctx.me.is_external else None),
    }


def for_event(ctx: Ctx, event_id: UUID) -> dict:
    if ctx.me.is_external:
        raise Forbidden("Lessons from earlier tests are for the bank's own staff")
    return build(ctx, event(ctx, event_id))


def _find(ctx: Ctx, ev: dict, key: str) -> dict:
    for x in build(ctx, ev)["lessons"]:
        if x["key"] == key:
            return x
    raise NotFound("That lesson no longer applies to this test")


def _guard(ctx: Ctx, event_id: UUID) -> dict:
    ev = event(ctx, event_id, for_update=True)
    authz.require_manage(ctx, ev)
    if S(ev["lifecycle_status"]) not in PLANNING:
        raise Conflict("Lessons can be acted on only while the test is being planned (before it is marked ready)")
    return ev


def apply(ctx: Ctx, event_id: UUID, key: str, *, title: str | None, owner_user_id: UUID | None,
          mandatory: bool = True, evidence_required: bool = False) -> dict:
    from .work import create_readiness_item
    ev = _guard(ctx, event_id)
    lesson = _find(ctx, ev, key)
    if lesson["decision"]:
        raise Conflict(f"This lesson was already {lesson['decision']['decision'].lower()}")
    title = (title or lesson["suggested_condition"]).strip()[:250]
    if not title:
        raise Unprocessable("Give the readiness condition a title")
    desc = (f"From an earlier test ({lesson['source']['event_code']}, {lesson['source']['event_title']}): "
            f"{lesson['title']}. {lesson['detail']}")
    item = create_readiness_item(ctx, event_id, {
        "title": title, "description": desc, "owner_user_id": owner_user_id,
        "activity_id": UUID(lesson["applies_to"]["id"]) if lesson["applies_to"] else None,
        "is_mandatory": mandatory, "evidence_required": evidence_required})
    dec = t("test_lesson_decisions")
    rec = dict(ctx.conn.execute(insert(dec).values(
        tenant_id=ctx.me.tenant_id, event_id=event_id, lesson_key=key, decision="Applied", readiness_item_id=item["id"],
        lesson=jsonable_encoder(lesson), decided_by=ctx.me.user_id).returning(dec)).mappings().one())
    audit.record(ctx, "lesson.applied", "Lesson", rec["id"], event_id=event_id,
                 new={"lesson": lesson["title"], "from": lesson["source"]["event_code"], "readiness_item": title})
    log(ctx, event_id, "Status", f"Lesson from {lesson['source']['event_code']} added as a readiness condition: {title}")
    return {"decision": rec, "readiness_item": item}


def dismiss(ctx: Ctx, event_id: UUID, key: str, reason: str) -> dict:
    ev = _guard(ctx, event_id)
    if not (reason or "").strip():
        raise Unprocessable("Say why this lesson doesn't apply; it's kept for the next planner and for audit")
    lesson = _find(ctx, ev, key)
    if lesson["decision"]:
        raise Conflict(f"This lesson was already {lesson['decision']['decision'].lower()}")
    dec = t("test_lesson_decisions")
    rec = dict(ctx.conn.execute(insert(dec).values(
        tenant_id=ctx.me.tenant_id, event_id=event_id, lesson_key=key, decision="Dismissed", reason=reason.strip(),
        lesson=jsonable_encoder(lesson), decided_by=ctx.me.user_id).returning(dec)).mappings().one())
    audit.record(ctx, "lesson.dismissed", "Lesson", rec["id"], event_id=event_id,
                 new={"lesson": lesson["title"], "from": lesson["source"]["event_code"], "reason": reason})
    return rec


def reopen(ctx: Ctx, event_id: UUID, key: str) -> dict:
    """Undo a dismissal (an applied lesson is a readiness condition now; change that condition instead)."""
    _guard(ctx, event_id)
    dec = t("test_lesson_decisions")
    d = ctx.conn.execute(select(dec).where(and_(dec.c.event_id == event_id, dec.c.lesson_key == key))).mappings().first()
    if not d:
        raise NotFound("Nothing to undo for this lesson")
    if d["decision"] != "Dismissed":
        raise Conflict("This lesson is now a readiness condition; change or waive that condition instead")
    ctx.conn.execute(delete(dec).where(dec.c.id == d["id"]))
    audit.record(ctx, "lesson.reopened", "Lesson", d["id"], event_id=event_id, old={"reason": d["reason"]})
    return {"reopened": True}


def lessons_for_items(ctx: Ctx, item_ids: list[UUID]) -> dict[UUID, dict]:
    """For the readiness list: which conditions came from a lesson, and from which test."""
    if not item_ids:
        return {}
    dec = t("test_lesson_decisions")
    out = {}
    for d in ctx.conn.execute(select(dec.c.readiness_item_id, dec.c.lesson).where(
            dec.c.readiness_item_id.in_(item_ids))).mappings():
        les = d["lesson"]
        out[d["readiness_item_id"]] = {"event_code": les["source"]["event_code"], "event_id": les["source"]["event_id"],
                                       "title": les["title"]}
    return out
