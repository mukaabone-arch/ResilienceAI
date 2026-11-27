"""Evidence pre-checks: check each uploaded file against the activity it was attached to.

The rule-based checks (evidence_checks.py) always run, in the upload request, so
the uploader sees problems straight away and can fix them before review. When an
external model is enabled (AI_PROVIDER=anthropic) it adds two things the rules
can't do well: whether the file is really evidence for this activity, and a
short briefing for the reviewer. It sees only a masked excerpt with people's
names replaced, and every line it cites is checked against that excerpt; if
anything fails, the rule-based result stands and the reason is kept.

A pre-check never accepts or rejects evidence. It is logged like every other AI
suggestion, and when the reviewer decides, the log records whether they agreed.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, func, insert, select, update

from .. import authz
from .. import config
from ..db import t, tenant_transaction
from ..domain.enums import Role
from ..errors import Conflict, Forbidden, NotFound, Unprocessable
from ..security import Ctx
from ..services import audit, storage
from ..services.common import child_of_event, now, row
from . import evidence_checks as chk
from .evidence_text import Extracted, extract
from .provider import CODE, MAX_TOKENS, api_error, pseudonymise, reply_text, restore

PROMPT_VERSION = "evidence-precheck-v1"
FEATURE = "evidence_precheck"
RPO_WORDS = re.compile(r"(?i)\b(rpo|recovery point|backup|replicat\w*|snapshot|standby|data guard|apply lag|restore)\b")
RTO_WORDS = re.compile(r"(?i)\b(rto|failover|switchover|recover\w*|restore|failback|cut ?over)\b")
NONPROD_WORDS = re.compile(r"(?i)\b(uat|sit|staging|pre-?prod|non-?prod|sandbox|test environment|qa environment)\b")
ORDER = {"warn": 0, "info": 1, "ok": 2}
TRANSPORT = None          # tests put an httpx mock transport here; production calls the real API


def enabled() -> bool:
    return config.settings.evidence_precheck.lower() not in ("off", "false", "0", "no")


def model_enabled() -> bool:
    return config.settings.ai_provider == "anthropic" and bool(config.settings.anthropic_api_key)


# ---------------------------------------------------------------------------
# Context: what the file is evidence for
# ---------------------------------------------------------------------------
def _context(ctx: Ctx, item: dict, ev: dict) -> dict:
    act = row(ctx, "test_activities", item["activity_id"]) if item["activity_id"] else None
    ri = row(ctx, "test_readiness_items", item["readiness_item_id"]) if item["readiness_item_id"] else None
    rpo_s = ev.get("rpo_target_minutes")
    rto_s = ev.get("rto_target_minutes")
    rpo_s = rpo_s * 60 if rpo_s is not None else None
    rto_s = rto_s * 60 if rto_s is not None else None
    if rpo_s is None or rto_s is None:            # fall back to targets recorded with measurements
        m = t("test_measurements")
        for typ, val in ctx.conn.execute(select(m.c.measurement_type, func.min(m.c.target_canonical)).where(and_(
                m.c.event_id == ev["id"], m.c.measurement_type.in_(["RPO", "RTO"]))).group_by(m.c.measurement_type)).all():
            if typ == "RPO" and rpo_s is None and val is not None:
                rpo_s = float(val)
            if typ == "RTO" and rto_s is None and val is not None:
                rto_s = float(val)
    subject = act or ri or {}
    about = " ".join(x for x in (subject.get("title"), subject.get("description")) if x)
    scope = " ".join(x for x in (ev["title"], ev.get("description"), ev.get("objectives")) if x)
    starts = [x for x in (ev["planned_start_at"], ev.get("actual_start_at"), act and act.get("planned_start_at")) if x]
    return {
        "kind": "activity" if act else "readiness condition",
        "code": act["activity_code"] if act else None,
        "title": subject.get("title") or "",
        "description": subject.get("description") or "",
        "about": about,
        "terms": chk.key_terms(subject.get("title"), subject.get("description")),
        "rpo_s": rpo_s, "rto_s": rto_s,
        "expects_rpo": bool(RPO_WORDS.search(about)), "expects_rto": bool(RTO_WORDS.search(about)),
        "expects_nonprod": bool(NONPROD_WORDS.search(about + " " + scope)),
        "lower": min(starts) - timedelta(days=config.settings.evidence_lookback_days),
        "upper": item["submitted_at"] + timedelta(minutes=10),
        "tz": ZoneInfo(ev.get("timezone") or "UTC"),
    }


# ---------------------------------------------------------------------------
# Rule-based pass
# ---------------------------------------------------------------------------
def _duplicates(ctx: Ctx, item: dict) -> dict | None:
    if not item.get("checksum"):
        return None
    evd, a, e = t("test_evidence"), t("test_activities"), t("test_events")
    others = ctx.conn.execute(
        select(evd.c.id, evd.c.evidence_code, evd.c.event_id, evd.c.review_status, evd.c.submitted_at,
               a.c.activity_code, e.c.event_code)
        .outerjoin(a, a.c.id == evd.c.activity_id).join(e, e.c.id == evd.c.event_id)
        .where(and_(evd.c.checksum == item["checksum"], evd.c.id != item["id"]))
        .order_by(evd.c.submitted_at).limit(5)).mappings().all()
    if not others:
        return chk.finding("duplicate", "ok", "Not a copy of any other evidence file")
    where = []
    for o in others:
        place = o["activity_code"] or "a readiness condition"
        if o["event_id"] != item["event_id"]:
            place += f" in {o['event_code']}"
        rejected = " (rejected)" if o["review_status"] == "Rejected" else ""
        where.append(f"{o['evidence_code']} for {place}{rejected}")
    resubmit = item.get("supersedes_id") in {o["id"] for o in others}
    other_test = any(o["event_id"] != item["event_id"] for o in others)
    detail = ("It's byte-for-byte the same file as the one it replaces, so nothing has changed." if resubmit else
              "It may have been carried over from an earlier test." if other_test else
              "The same file is attached to more than one item. Check it really proves both.")
    return chk.finding("duplicate", "warn", "Same file already submitted: " + "; ".join(where), detail,
                       [{"line": None, "text": f"{o['evidence_code']} · SHA-256 match", "evidence_id": str(o["id"])} for o in others])


def rule_findings(ctx: Ctx, item: dict, c: dict, x: Extracted) -> tuple[list[dict], dict]:
    findings: list[dict] = []
    stats: dict = {"kind": x.kind, "lines": len(x.lines), "truncated": x.truncated}
    if x.readable:
        stamps = chk.all_stamps(x.lines, c["tz"], item["submitted_at"].year)
        if stamps:
            first, last = min(s.at for s in stamps), max(s.at for s in stamps)
            stats["dated_from"], stats["dated_to"] = first, last
        findings.append(chk.check_dates(x.lines, stamps, c["lower"], c["upper"], c["tz"]))
        findings += chk.check_recovery(x.lines, stamps, c["rpo_s"], c["rto_s"], c["expects_rpo"], c["expects_rto"])
        env = chk.check_environment(x.lines, c["expects_nonprod"])
        if env:
            findings.append(env)
        findings.append(chk.check_sensitive(x.lines))
        rel = chk.check_relevance(x.text, c["terms"], c["kind"])
        if rel:
            findings.append(rel)
    else:
        findings.append(chk.finding("readable", "info", x.note or "The file's text couldn't be read",
                                    "Dates, recovery figures, environment and sensitive data weren't checked."))
    dup = _duplicates(ctx, item)
    if dup:
        findings.append(dup)
    return findings, stats


def _summarise(content: dict) -> dict:
    fs = sorted(content["findings"], key=lambda f: ORDER[f["level"]])
    content["findings"] = fs
    s = {k: sum(1 for f in fs if f["level"] == k) for k in ("ok", "warn", "info")}
    content["summary"] = s
    warns = [f["title"] for f in fs if f["level"] == "warn"]
    if warns:
        content["headline"] = (f"{len(warns)} thing{'s' if len(warns) != 1 else ''} to check: "
                               + "; ".join(w[0].lower() + w[1:] for w in warns[:3]))
    elif s["ok"]:
        content["headline"] = f"No problems found by {s['ok']} check{'s' if s['ok'] != 1 else ''}"
    else:
        content["headline"] = "Nothing could be checked automatically"
    return content


def _rules_briefing(c: dict, stats: dict, findings: list[dict]) -> str:
    kinds = {"text": "Text file", "pdf": "PDF", "docx": "Word document", "xlsx": "Spreadsheet", "pptx": "Slide deck",
             "image": "Image", "binary": "File", "unreadable": "File"}
    bits = [kinds.get(stats["kind"], "File")]
    if stats.get("lines"):
        bits[0] += f", {stats['lines']:,} line{'s' if stats['lines'] != 1 else ''}"
    if stats.get("dated_from"):
        f, to = stats["dated_from"], stats["dated_to"]
        bits[0] += (f", dated {chk.fmt(f, c['tz'])}" if f == to else f", dated {chk.fmt(f, c['tz'])} to {chk.fmt(to, c['tz'])}")
    bits[0] += "."
    target = f"{c['kind']} {c['code'] + ' ' if c['code'] else ''}'{c['title']}'"
    bits.append(f"Submitted as evidence for the {target}.")
    warns = [f["title"] for f in findings if f["level"] == "warn"]
    if warns:
        bits.append(f"{len(warns)} of the checks above need{'s' if len(warns) == 1 else ''} a look before it is accepted.")
    else:
        bits.append("The automatic checks found nothing to query; the reviewer still decides whether it proves the "
                    + c["kind"] + ".")
    return " ".join(bits)


# ---------------------------------------------------------------------------
# Model pass (optional)
# ---------------------------------------------------------------------------
SYSTEM = (
    "You help reviewers at a bank check evidence for a resilience test. You get one activity and numbered lines from "
    "the file submitted as its evidence. Judge only from those lines. Reply with JSON only, no prose, in this shape: "
    '{"relevance": "relevant" | "partly" | "not_relevant" | "unclear", "reason": "one sentence", '
    '"shows": ["what the file demonstrates, up to 4 short points"], '
    '"missing": ["what a reviewer would expect for this activity but cannot find, up to 4 short points"], '
    '"citations": [{"line": <line number>, "quote": "a few words copied exactly from that line"}]}. '
    "Rules: cite the lines your judgement rests on (1 to 5); quote them exactly; do not invent numbers, dates, systems "
    "or names; keep placeholders like 'Person A' and record codes exactly as written; never say the evidence should "
    "be accepted or rejected, that is the reviewer's decision."
)
LEVEL = {"relevant": "ok", "partly": "info", "not_relevant": "warn", "unclear": "info"}
TITLE = {"relevant": "Relevant to the {k}", "partly": "Only partly covers the {k}",
         "not_relevant": "Doesn't look like evidence for this {k}", "unclear": "Couldn't tell whether it covers the {k}"}


def _excerpt(lines: list[str], findings: list[dict], terms: list[str], budget: int = 14000) -> list[int]:
    """0-based line indexes to send: the start, the end, lines the rules cited, and lines with the key terms."""
    pick: list[int] = []
    cited = {c["line"] - 1 for f in findings for c in f["cites"] if c.get("line")}
    for i in sorted(cited):
        pick += [j for j in range(i - 2, i + 3) if 0 <= j < len(lines)]
    pick += list(range(min(120, len(lines))))
    low = [x.lower() for x in lines]
    pick += [i for i, x in enumerate(low) if any(chk.term_found(t, x) for t in terms)][:120]
    pick += list(range(max(0, len(lines) - 60), len(lines)))
    out, size = [], 0
    for i in sorted(set(pick)):
        if not lines[i].strip():
            continue
        size += len(lines[i]) + 8
        if size > budget:
            break
        out.append(i)
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def model_pass(ctx: Ctx, c: dict, x: Extracted, findings: list[dict], transport=None) -> tuple[dict | None, str | None]:
    """Returns ({relevance, reason, shows, missing, cites}, None) or (None, why the model's answer wasn't used)."""
    import httpx
    idx = _excerpt(x.lines, findings, c["terms"])
    if not idx:
        return None, "nothing readable to send"
    names = [n for n in (r[0] for r in ctx.conn.execute(select(t("users").c.display_name))) if n]
    sent = {i + 1: chk.mask(x.lines[i]) for i in idx}
    block = "\n".join(f"L{n}: {txt[:400]}" for n, txt in sent.items())
    head = (f"{c['kind'].capitalize()}: {(c['code'] + ' ') if c['code'] else ''}{c['title']}\n"
            f"Description: {c['description'] or '(none)'}\n")
    safe, mapping = pseudonymise(head + "\nFile lines:\n" + block, names)
    try:
        with httpx.Client(timeout=config.settings.ai_timeout_s, transport=transport or TRANSPORT) as cl:
            r = cl.post("https://api.anthropic.com/v1/messages", headers={
                "x-api-key": config.settings.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                content=json.dumps({"model": config.settings.ai_model, "max_tokens": MAX_TOKENS, "system": SYSTEM,
                                    "messages": [{"role": "user", "content": safe}]}))
        if r.status_code != 200:
            return None, api_error(r)
        raw, why = reply_text(r)
        if why:
            return None, why
    except Exception as e:  # noqa: BLE001 - any failure leaves the rule-based result in place
        return None, f"model call failed ({type(e).__name__})"
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        out = json.loads(raw)
        assert isinstance(out, dict)
    except (ValueError, AssertionError):
        return None, "model reply wasn't valid JSON"
    rel = out.get("relevance")
    if rel not in LEVEL:
        return None, "model reply had no usable relevance judgement"
    reason = str(out.get("reason") or "")[:300]
    shows = [str(s)[:200] for s in (out.get("shows") or [])[:4] if str(s).strip()]
    missing = [str(s)[:200] for s in (out.get("missing") or [])[:4] if str(s).strip()]
    cites, bad = [], 0
    safe_lines = {n: pseudonymise(txt, names)[0] for n, txt in sent.items()}
    for cobj in (out.get("citations") or [])[:5]:
        try:
            n, q = int(cobj.get("line")), str(cobj.get("quote") or "")
        except (TypeError, ValueError, AttributeError):
            bad += 1
            continue
        if n in safe_lines and len(_norm(q)) >= 3 and _norm(q) in _norm(safe_lines[n]):
            cites.append(chk.cite(x.lines, n - 1))
        else:
            bad += 1
    if rel in ("relevant", "partly") and not cites:
        return None, "model cited no lines that match the file"
    words = " ".join([reason, *shows, *missing])
    unknown = set(CODE.findall(words)) - set(CODE.findall(safe))
    if unknown:
        return None, f"model mentioned records not in the file or activity: {', '.join(sorted(unknown))}"
    if set(re.findall(r"Person [A-Z]\b", words)) - set(mapping):
        return None, "model introduced people not in the file"
    if re.search(r"(?i)\b(should|must|recommend\w*)\b[^.]{0,40}\b(accept|reject|approv)\w*", words):
        return None, "model tried to recommend a review decision"
    def fix(s: str) -> str:
        return restore(s, mapping)
    return {"relevance": rel, "reason": fix(reason), "shows": [fix(s) for s in shows], "missing": [fix(s) for s in missing],
            "cites": cites, "dropped_citations": bad}, None


def _apply_model(content: dict, c: dict, res: dict) -> None:
    content["findings"] = [f for f in content["findings"] if f["key"] != "relevance"]
    content["findings"].append(chk.finding("relevance", LEVEL[res["relevance"]], TITLE[res["relevance"]].format(k=c["kind"]),
                                           res["reason"] or None, res["cites"]) | {"by": "model"})
    content["briefing"].update(by="model", shows=res["shows"], missing=res["missing"])
    _summarise(content)


# ---------------------------------------------------------------------------
# Running and storing
# ---------------------------------------------------------------------------
def _ai():
    return t("ai_activity_records")


def latest(ctx: Ctx, evidence_id: UUID) -> dict | None:
    ai = _ai()
    r = ctx.conn.execute(select(ai).where(and_(ai.c.subject_id == evidence_id, ai.c.feature == FEATURE,
                                               ai.c.status != "Superseded"))
                         .order_by(ai.c.created_at.desc()).limit(1)).mappings().first()
    return dict(r) if r else None


def run(ctx: Ctx, item: dict, ev: dict, data: bytes | None = None, *, use_model: bool = False,
        requested_by: UUID | None = None, transport=None) -> dict | None:
    """Check one evidence file and log the result. Returns the record, or None when there is nothing to check."""
    if not enabled() or item["storage_backend"] == "link" or item["scan_status"] in ("Pending", "Infected"):
        return None
    if data is None:
        try:
            data = storage.store_for(item["storage_backend"]).get(item["storage_reference"])
        except FileNotFoundError:
            return None
    c = _context(ctx, item, ev)
    x = extract(item["filename"] or "evidence", data)
    findings, stats = rule_findings(ctx, item, c, x)
    content = {"evidence_id": item["id"], "evidence_code": item["evidence_code"], "filename": item["filename"],
               "about": {"kind": c["kind"], "code": c["code"], "title": c["title"]}, "file": stats, "findings": findings,
               "briefing": {"by": "rules", "text": _rules_briefing(c, stats, findings)},
               "window": {"from": c["lower"], "to": c["upper"]},
               "targets": {"rpo_minutes": c["rpo_s"] / 60 if c["rpo_s"] is not None else None,
                           "rto_minutes": c["rto_s"] / 60 if c["rto_s"] is not None else None}}
    _summarise(content)
    provider, model, fallback = "rules", None, None
    if model_enabled() and x.readable:
        if use_model:
            res, fallback = model_pass(ctx, c, x, findings, transport=transport)
            model = config.settings.ai_model
            if res:
                _apply_model(content, c, res)
                provider = "anthropic"
            content["ai_status"] = "done" if res else "fallback"
        else:
            content["ai_status"] = "queued"        # the worker adds the model's view shortly
    else:
        content["ai_status"] = "not_configured" if x.readable else "not_applicable"
    sources = [{"type": "Evidence", "id": str(item["id"]), "label": item["evidence_code"]}]
    if item["activity_id"]:
        sources.append({"type": "Activity", "id": str(item["activity_id"]), "label": c["code"] or c["title"]})
    if item["readiness_item_id"]:
        sources.append({"type": "ReadinessItem", "id": str(item["readiness_item_id"]), "label": c["title"]})
    ai = _ai()
    ctx.conn.execute(update(ai).where(and_(ai.c.subject_id == item["id"], ai.c.feature == FEATURE,
                                           ai.c.status == "Proposed")).values(status="Superseded"))
    rec = dict(ctx.conn.execute(insert(ai).values(
        tenant_id=ctx.me.tenant_id, event_id=ev["id"], subject_id=item["id"], feature=FEATURE,
        content=jsonable_encoder(content), sources=sources, provider=provider, model=model,
        prompt_version=PROMPT_VERSION, fallback_reason=fallback, requested_by=requested_by).returning(ai)).mappings().one())
    audit.record(ctx, "ai.suggested", "AiSuggestion", rec["id"], event_id=ev["id"], actor=requested_by,
                 new={"feature": FEATURE, "evidence": item["evidence_code"], "provider": provider, "fallback": fallback,
                      "summary": content["summary"]})
    return rec


def safe_run(ctx: Ctx, item: dict, ev: dict, data: bytes | None = None, **kw) -> dict | None:
    """Pre-checks must never break an upload: any unexpected failure is logged and skipped."""
    try:
        with ctx.conn.begin_nested():
            return run(ctx, item, ev, data, **kw)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("resilienceai.precheck").exception("pre-check failed for %s: %s", item.get("evidence_code"), e)
        return None


def run_model_pending(tenant_id: UUID, limit: int = 10, transport=None) -> int:
    """Worker job: add the model's relevance judgement and briefing to pre-checks queued at upload."""
    if not model_enabled():
        return 0
    from ..services.people import _system_ctx
    done = 0
    with tenant_transaction(tenant_id) as conn:
        ctx = _system_ctx(conn, tenant_id)
        ai = _ai()
        recs = [dict(r) for r in conn.execute(select(ai).where(and_(
            ai.c.feature == FEATURE, ai.c.status == "Proposed", ai.c.content["ai_status"].astext == "queued"))
            .order_by(ai.c.created_at).limit(limit).with_for_update(skip_locked=True)).mappings()]
        for rec in recs:
            item = row(ctx, "test_evidence", rec["subject_id"])
            ev = row(ctx, "test_events", rec["event_id"])
            try:
                data = storage.store_for(item["storage_backend"]).get(item["storage_reference"])
            except FileNotFoundError:
                continue
            c = _context(ctx, item, ev)
            x = extract(item["filename"] or "evidence", data)
            content = dict(rec["content"])
            res, why = model_pass(ctx, c, x, content["findings"], transport=transport)
            values = {"model": config.settings.ai_model, "fallback_reason": why}
            if res:
                _apply_model(content, c, res)
                values["provider"] = "anthropic"
            content["ai_status"] = "done" if res else "fallback"
            values["content"] = jsonable_encoder(content)
            conn.execute(update(ai).where(ai.c.id == rec["id"]).values(**values))
            audit.record(ctx, "ai.evidence_reviewed_by_model", "AiSuggestion", rec["id"], event_id=ev["id"], actor=None,
                         new={"evidence": item["evidence_code"], "used": bool(res), "fallback": why})
            done += 1
    return done


# ---------------------------------------------------------------------------
# What people do with it
# ---------------------------------------------------------------------------
def warnings_of(rec: dict | None) -> list[str]:
    if not rec:
        return []
    disputed = (rec.get("final_content") or {}).get("finding_feedback", {})
    return [f["title"] for f in rec["content"]["findings"]
            if f["level"] == "warn" and disputed.get(f["key"], {}).get("verdict") != "wrong"]


def on_review(ctx: Ctx, item: dict, ev: dict, accepted: bool, comment: str | None) -> None:
    """Close the pre-check with the reviewer's decision, recording whether they agreed with it."""
    rec = latest(ctx, item["id"])
    if not rec or rec["status"] != "Proposed":
        return
    warns = [f for f in rec["content"]["findings"] if f["level"] == "warn"]
    agreed = (not accepted) if warns else accepted
    final = dict(rec.get("final_content") or {})
    final.update(review="Accepted" if accepted else "Rejected", agreed=agreed, open_warnings=len(warns))
    if agreed:
        feedback = None
    elif accepted:
        feedback = f"Accepted despite {len(warns)} warning{'s' if len(warns) != 1 else ''}: {comment or ''}".strip()
    else:
        feedback = f"Rejected although the pre-check found no problems: {comment or ''}".strip()
    ai = _ai()
    ctx.conn.execute(update(ai).where(ai.c.id == rec["id"]).values(
        status="Accepted" if agreed else "Dismissed", decided_by=ctx.me.user_id, decided_at=now(),
        feedback=feedback, final_content=jsonable_encoder(final)))
    audit.record(ctx, "ai.accepted" if agreed else "ai.dismissed", "AiSuggestion", rec["id"], event_id=ev["id"],
                 new={"feature": FEATURE, "evidence": item["evidence_code"], "review": final["review"], "agreed": agreed})


def _can_act(ctx: Ctx, item: dict, ev: dict) -> bool:
    if item["submitted_by"] == ctx.me.user_id:
        return True
    if ctx.me.is_external:
        return False
    if authz.can_manage_event(ctx, ev) or ctx.me.has(Role.RISK):
        return True
    if item["activity_id"]:
        return row(ctx, "test_activities", item["activity_id"])["reviewer_user_id"] == ctx.me.user_id
    return False


def rerun(ctx: Ctx, evidence_id: UUID) -> dict:
    item, ev = child_of_event(ctx, "test_evidence", evidence_id)
    if not _can_act(ctx, item, ev):
        raise Forbidden("Only the submitter, the test manager or a reviewer can run the pre-check again")
    if not enabled():
        raise Conflict("Evidence pre-checks are turned off for this installation")
    if item["storage_backend"] == "link":
        raise Conflict("This evidence is a link to an external record, so there is no file to check")
    if item["scan_status"] in ("Pending", "Infected"):
        raise Conflict("The file can be checked once the malware scan has found it clean")
    if item["review_status"] != "Pending":
        raise Conflict(f"This evidence was already {item['review_status'].lower()}")
    rec = run(ctx, item, ev, use_model=True, requested_by=ctx.me.user_id)
    if rec is None:
        raise NotFound("The stored file is missing")
    return present(ctx, rec, ev)


def feedback(ctx: Ctx, evidence_id: UUID, key: str, verdict: str, note: str | None) -> dict:
    item, ev = child_of_event(ctx, "test_evidence", evidence_id)
    if not _can_act(ctx, item, ev):
        raise Forbidden("Only the submitter, the test manager or a reviewer can comment on the pre-check")
    if verdict not in ("wrong", "right"):
        raise Unprocessable("verdict must be wrong or right")
    rec = latest(ctx, evidence_id)
    if not rec:
        raise NotFound("This evidence has no pre-check")
    if rec["status"] != "Proposed":
        raise Conflict("The evidence has been reviewed; the pre-check is closed")
    if key not in {f["key"] for f in rec["content"]["findings"]}:
        raise Unprocessable("That check isn't part of this pre-check")
    if verdict == "wrong" and not (note or "").strip():
        raise Unprocessable("Say briefly what the check got wrong")
    final = dict(rec.get("final_content") or {})
    fb = dict(final.get("finding_feedback") or {})
    fb[key] = {"verdict": verdict, "note": (note or "").strip() or None, "by": ctx.me.display_name, "by_id": str(ctx.me.user_id),
               "at": now().isoformat()}
    final["finding_feedback"] = fb
    ai = _ai()
    new = dict(ctx.conn.execute(update(ai).where(ai.c.id == rec["id"]).values(final_content=final).returning(ai)).mappings().one())
    audit.record(ctx, "ai.precheck_feedback", "AiSuggestion", rec["id"], event_id=ev["id"],
                 new={"evidence": item["evidence_code"], "check": key, "verdict": verdict, "note": note})
    return present(ctx, new, ev)


def present(ctx: Ctx, rec: dict | None, ev: dict) -> dict | None:
    """The pre-check as the viewer may see it. External users don't learn about files in other tests."""
    if not rec:
        return None
    content = json.loads(json.dumps(rec["content"]))
    if ctx.me.is_external:
        for f in content["findings"]:
            if f["key"] == "duplicate" and f["level"] == "warn":
                f["title"], f["cites"] = "Same file already submitted for another item", []
    return {"id": rec["id"], "status": rec["status"], "provider": rec["provider"], "model": rec["model"],
            "fallback_reason": rec["fallback_reason"] if not ctx.me.is_external else None,
            "created_at": rec["created_at"], "decided_at": rec["decided_at"], "feedback": rec["feedback"],
            "final_content": rec.get("final_content"), **content}


def for_event(ctx: Ctx, event_id: UUID) -> dict[UUID, dict]:
    ai = _ai()
    out: dict[UUID, dict] = {}
    for r in ctx.conn.execute(select(ai).where(and_(ai.c.event_id == event_id, ai.c.feature == FEATURE,
                                                    ai.c.status != "Superseded"))
                              .order_by(ai.c.created_at.desc())).mappings():
        out.setdefault(r["subject_id"], dict(r))
    return out
