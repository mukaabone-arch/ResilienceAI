"""Management reports (Screen 10): closure report as PDF, programme status as an Excel workbook.

Both are built from the same records the screens use, so a report never disagrees
with the app. A closed test's PDF is rendered from the snapshot frozen at closure.
"""
from __future__ import annotations

import io
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select

from .. import authz
from ..db import t
from ..security import Ctx
from . import audit
from .common import event, now
from .events import closure_report, list_events, readiness_gate

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
DECISION_LABEL = {"Plan": "Plan approval", "Go/No-go": "Go / no-go", "ReadinessException": "Readiness exception",
                  "Resume": "Resume after pause", "Business": "Business sign-off", "RiskReview": "Risk review",
                  "Closure": "Closure approval", "BlackoutOverride": "Blackout override"}
ASCII = str.maketrans({"→": "->", "–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...", "₹": "Rs ", " ": " "})


def _txt(v) -> str:
    """Helvetica in PDFs covers Latin-1 only: map common typography to ASCII and drop the rest."""
    s = "" if v is None else str(v)
    return s.translate(ASCII).encode("latin-1", "replace").decode("latin-1")


def _dt(v, tz: str, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not v:
        return "-"
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v.astimezone(ZoneInfo(tz)).strftime(fmt)


def _tenant_name(ctx: Ctx) -> str:
    return ctx.conn.execute(select(t("tenants").c.name)).scalar() or ""


def _names(ctx: Ctx) -> dict:
    u = t("users")
    return {r.id: r.display_name for r in ctx.conn.execute(select(u.c.id, u.c.display_name))}


# ---------------------------------------------------------------------------
# Closure report (PDF)
# ---------------------------------------------------------------------------
def closure_pdf(ctx: Ctx, event_id: UUID) -> tuple[bytes, str]:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ev = event(ctx, event_id)
    rep = closure_report(ctx, event_id)              # final snapshot, or draft with live blockers
    final = rep["status"] == "final"
    tz = ev["timezone"]
    e = rep["event"]
    org = _tenant_name(ctx)

    INK, MUTED, LINE = colors.HexColor("#16233b"), colors.HexColor("#4b5768"), colors.HexColor("#d3d9d0")
    GOOD, BAD, WARN = colors.HexColor("#2f7a4f"), colors.HexColor("#b42318"), colors.HexColor("#9a5b06")
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK)
    small = ParagraphStyle("s", parent=body, fontSize=8, leading=10.5, textColor=MUTED)
    h1 = ParagraphStyle("h1", parent=body, fontName="Helvetica-Bold", fontSize=17, leading=21, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=10, spaceAfter=4)
    cell = ParagraphStyle("c", parent=body, fontSize=8.5, leading=11, alignment=TA_LEFT)
    cellb = ParagraphStyle("cb", parent=cell, fontName="Helvetica-Bold")

    def P(v, st=cell):
        return Paragraph(_txt(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), st)

    def table(header, rows, widths, colour_col=None):
        data = [[P(h, cellb) for h in header]] + [[P(c) for c in r] for r in rows]
        tb = Table(data, colWidths=widths, repeatRows=1)
        style = [("LINEBELOW", (0, 0), (-1, 0), 0.8, INK), ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3),
                 ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if colour_col is not None:
            for i, r in enumerate(rows, start=1):
                v = str(r[colour_col])
                c = GOOD if v in ("Met", "Approved", "Completed", "Accepted", "Closed", "Resolved") else \
                    BAD if v in ("Not Met", "Rejected", "Open", "Blocked", "Failed") else WARN if v in ("Conditional", "Pending", "In Progress") else None
                if c is not None:
                    style.append(("TEXTCOLOR", (colour_col, i), (colour_col, i), c))
        tb.setStyle(TableStyle(style))
        return tb

    W = A4[0] - 36 * mm
    story = []
    story.append(Paragraph(_txt(f"{org}  |  Resilience test closure report"), small))
    story.append(Paragraph(_txt(e["title"]), h1))
    status_line = (f"{ev['event_code']}  |  Outcome: {e['outcome'] or 'not recorded'}  |  "
                   + ("Final: frozen at closure " + _dt(rep["generated_at"], tz) if final else "DRAFT: records may still change"))
    story.append(Paragraph(_txt(status_line), ParagraphStyle("st", parent=body, textColor=GOOD if final else BAD, fontName="Helvetica-Bold")))
    story.append(Spacer(1, 8))

    facts = [["Criticality", e["criticality"], "Category", e["category"]],
             ["Planned window", f"{_dt(e['planned_start_at'], tz)} to {_dt(e['planned_end_at'], tz, '%H:%M')} ({tz})", "Owner", rep.get("owner") or "-"],
             ["Actual window", f"{_dt(e['actual_start_at'], tz)} to {_dt(e['actual_end_at'], tz)}", "Test manager", rep.get("test_manager") or "-"]]
    ft = Table([[P(a, cellb), P(b), P(c, cellb), P(d)] for a, b, c, d in facts], colWidths=[W * .16, W * .44, W * .14, W * .26])
    ft.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
                            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(ft)
    if e.get("outcome_summary"):
        story += [Paragraph("Summary", h2), P(e["outcome_summary"], body)]
    if rep.get("scope"):
        story += [Paragraph("Scope", h2), P(", ".join(rep["scope"]), body)]

    if not final and rep.get("blockers"):
        story += [Paragraph("Outstanding before closure", h2),
                  table(["Item"], [[b] for b in rep["blockers"]], [W])]

    story.append(Paragraph("Recovery measurements", h2))
    story.append(table(["Measure", "Target", "Actual", "Result"],
                       [[m["type"], m["target"], m["actual"], m["result"]] for m in rep["measurements"]] or [["None recorded", "", "", ""]],
                       [W * .2, W * .25, W * .25, W * .3], colour_col=3))

    story.append(Paragraph("Decisions and approvals", h2))
    story.append(table(["Decision", "Result", "By", "When", "Notes"],
                       [[DECISION_LABEL.get(d["type"], d["type"]), d["decision"] + (" (superseded)" if d["superseded"] else ""), d["by"] or "-",
                         _dt(d["at"], tz), d.get("comments") or ""] for d in rep["decisions"]] or [["None", "", "", "", ""]],
                       [W * .2, W * .15, W * .15, W * .18, W * .32], colour_col=1))

    story.append(Paragraph("Readiness at go", h2))
    story.append(table(["Condition", "Mandatory", "Status"],
                       [[r["title"], "Yes" if r["mandatory"] else "No", r["status"]] for r in rep["readiness"]] or [["None", "", ""]],
                       [W * .6, W * .15, W * .25]))

    story.append(Paragraph("Activities", h2))
    story.append(table(["Code", "Activity", "Status", "Completed"],
                       [[a["code"], a["title"], a["status"], _dt(a["completed_at"], tz)] for a in rep["activities"]] or [["", "None", "", ""]],
                       [W * .12, W * .5, W * .16, W * .22], colour_col=2))

    ra, iss = t("test_remediation_actions"), t("test_issues")
    names = _names(ctx)
    remediation = ctx.conn.execute(select(iss.c.issue_code, ra.c.action_code, ra.c.title, ra.c.owner_user_id, ra.c.due_at, ra.c.status)
                                   .join(iss, iss.c.id == ra.c.issue_id).where(iss.c.event_id == event_id)
                                   .order_by(ra.c.action_code)).mappings().all()
    story.append(Paragraph("Issues and findings", h2))
    story.append(table(["Code", "Type", "Severity", "Title", "Status"],
                       [[i["code"], i["type"], i["severity"], i["title"], i["status"]] for i in rep["issues"]] or [["", "", "", "None raised", ""]],
                       [W * .27, W * .11, W * .11, W * .37, W * .14], colour_col=4))
    if remediation:
        story.append(Paragraph("Remediation actions", h2))
        story.append(table(["Action", "For", "Corrective action", "Owner", "Due", "Status"],
                           [[r["action_code"], r["issue_code"], r["title"], names.get(r["owner_user_id"], "-"), _dt(r["due_at"], tz, "%d %b %Y"), r["status"]]
                            for r in remediation], [W * .27, W * .23, W * .2, W * .1, W * .1, W * .1], colour_col=5))

    evd = t("test_evidence")
    evrows = ctx.conn.execute(select(evd).where(evd.c.event_id == event_id).order_by(evd.c.evidence_code)).mappings().all()
    story.append(Paragraph("Evidence register", h2))
    story.append(table(["Reference", "Item", "SHA-256 (first 16)", "Review"],
                       [[x["evidence_code"], x["filename"] or x["storage_reference"],
                         (x["checksum"] or "link")[:16], x["review_status"]] for x in evrows] or [["", "None", "", ""]],
                       [W * .26, W * .34, W * .22, W * .18], colour_col=3))

    story.append(Paragraph("Participants", h2))
    story.append(table(["Name", "Role in the test", "Assignment", "Acknowledged"],
                       [[p["name"], p["role"], p["status"], _dt(p["acknowledged_at"], tz)] for p in rep["participants"] if p["active"]],
                       [W * .28, W * .32, W * .16, W * .24]))

    generated = now()

    def frame(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, _txt(f"{ev['event_code']}  |  Generated {_dt(generated, tz)} ({tz}) by ResilienceAI"))
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        if not final:
            canvas.setFont("Helvetica-Bold", 60)
            canvas.setFillColor(colors.Color(0.7, 0.14, 0.09, alpha=0.08))
            canvas.translate(A4[0] / 2, A4[1] / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, "DRAFT")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                            title=_txt(f"{ev['event_code']} closure report"), author=_txt(org), subject="Resilience test closure report")
    doc.build([KeepTogether(story[:5])] + story[5:], onFirstPage=frame, onLaterPages=frame)
    audit.record(ctx, "report.closure_pdf", "Event", event_id, event_id=event_id, new={"status": rep["status"]})
    return buf.getvalue(), f"{ev['event_code']}-closure-report{'' if final else '-DRAFT'}.pdf"


# ---------------------------------------------------------------------------
# Programme status (Excel)
# ---------------------------------------------------------------------------
def programme_xlsx(ctx: Ctx, programme_id: UUID | None, tz: str = "Asia/Kolkata") -> tuple[bytes, str]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    authz.require_role(ctx, *authz.PORTFOLIO_VIEWERS)
    zone = ZoneInfo(tz)
    p = t("test_programmes")
    prog = None
    if programme_id:
        prog = ctx.conn.execute(select(p).where(p.c.id == programme_id)).mappings().first()
    elif (prog := ctx.conn.execute(select(p).where(p.c.status == "Active").order_by(p.c.start_date.desc()).limit(1)).mappings().first()):
        programme_id = prog["id"]
    evs = list_events(ctx, programme_id=programme_id, limit=1000)
    names = _names(ctx)
    n = now()

    def local(v):
        return v.astimezone(zone).replace(tzinfo=None) if v else None

    wb = Workbook()
    HEAD = Font(bold=True, color="FFFFFF")
    FILL = PatternFill("solid", fgColor="16233B")
    RED, GREEN, AMBER = Font(color="B42318", bold=True), Font(color="2F7A4F", bold=True), Font(color="9A5B06", bold=True)

    def sheet(ws, header, rows, widths, date_cols=(), status_col=None):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill, c.alignment = HEAD, FILL, Alignment(vertical="center", wrap_text=True)
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for col in date_cols:
            for c in ws[get_column_letter(col)][1:]:
                c.number_format = "dd mmm yyyy hh:mm"
        if status_col:
            for c in ws[get_column_letter(status_col)][1:]:
                v = str(c.value or "")
                tone = RED if v in ("Blocked", "Not Met", "Open", "Overdue", "Rejected", "Paused") else \
                    GREEN if v in ("Closed", "Ready", "Met", "Accepted", "Approved", "Successful", "Resolved") else \
                    AMBER if v in ("Readiness", "Mobilising", "Live", "In Progress", "Pending", "Conditional", "Partial") else None
                if tone is not None:
                    c.font = tone
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions

    # --- Tests
    i, ap = t("test_issues"), t("test_approvals")
    blockers = dict(ctx.conn.execute(select(i.c.event_id, func.count()).where(and_(
        i.c.issue_type == "Blocker", i.c.status.in_(["Open", "In Progress"]))).group_by(i.c.event_id)).all())
    test_rows, readiness = [], {}
    for e in evs:
        g = readiness_gate(ctx, e)
        readiness[e["id"]] = g
        test_rows.append([e["event_code"], e["title"], e["category"], e["criticality"], e["lifecycle_status"], e["outcome"] or "",
                          local(e["planned_start_at"]), local(e["planned_end_at"]), e["timezone"],
                          names.get(e["test_manager_user_id"], ""), names.get(e["owner_user_id"], ""),
                          f"{g.mandatory_ready + len(g.excepted)}/{g.mandatory_total}" if g.mandatory_total else "not set",
                          "Yes" if g.passed else "No", blockers.get(e["id"], 0)])

    # --- Summary
    ws = wb.active
    ws.title = "Summary"
    by_status: dict[str, int] = {}
    for e in evs:
        by_status[e["lifecycle_status"]] = by_status.get(e["lifecycle_status"], 0) + 1
    planned = sum(v for k, v in by_status.items() if k != "Cancelled")
    a, s, ack = t("test_activities"), t("test_stakeholders"), t("test_acknowledgements")
    ids = [e["id"] for e in evs] or [None]
    overdue = ctx.conn.execute(select(func.count()).where(and_(a.c.event_id.in_(ids), a.c.due_at < n,
                                                              a.c.status.notin_(["Completed", "Submitted"])))).scalar()
    acks = ctx.conn.execute(select(func.count().filter(ack.c.status == "Acknowledged"), func.count().filter(ack.c.status == "Pending"),
                                   func.count().filter(and_(ack.c.status == "Pending", ack.c.due_at < n)))
                            .select_from(ack.join(s, s.c.id == ack.c.stakeholder_id)).where(s.c.event_id.in_(ids))).one()
    findings = dict(ctx.conn.execute(select(i.c.severity, func.count()).where(and_(
        i.c.event_id.in_(ids), i.c.issue_type == "Finding", i.c.status != "Closed")).group_by(i.c.severity)).all())
    ws["A1"] = f"{_tenant_name(ctx)}: resilience test programme status"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Programme: {prog['name'] if prog else 'All programmes'}"
    ws["A3"] = f"Generated {n.astimezone(zone):%d %b %Y %H:%M} ({tz}). Times in this workbook are in {tz}."
    ws["A3"].font = Font(italic=True, color="4B5768")
    rows = [("Tests in programme", len(evs)), ("Closed", by_status.get("Closed", 0)),
            ("Completion (closed of planned)", f"{round(100 * by_status.get('Closed', 0) / planned, 1) if planned else 0}%"),
            ("Activities overdue", overdue), ("Acknowledgements received", acks[0]),
            ("Acknowledgements pending", acks[1]), ("Acknowledgements past deadline", acks[2])]
    rows += [(f"Tests in stage: {k}", v) for k, v in sorted(by_status.items())]
    rows += [(f"Open findings: {k}", v) for k, v in findings.items()]
    for r_i, (k, v) in enumerate(rows, start=5):
        ws.cell(row=r_i, column=1, value=k).font = Font(bold=True)
        ws.cell(row=r_i, column=2, value=v)
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 36, 18

    sheet(wb.create_sheet("Tests"),
          ["Code", "Test", "Category", "Criticality", "Stage", "Outcome", "Planned start", "Planned end", "Test time zone",
           "Test manager", "Owner", "Mandatory readiness met", "Gate passed", "Open blockers"],
          test_rows, [18, 40, 14, 11, 12, 11, 17, 17, 15, 18, 18, 14, 11, 10], date_cols=(7, 8), status_col=5)

    # --- Exceptions: at-risk readiness, delays, overdue activities
    exc = []
    for e in evs:
        g = readiness[e["id"]]
        if e["lifecycle_status"] in ("Mobilising", "Readiness", "Ready") and not g.passed:
            for u in g.unmet:
                exc.append([e["event_code"], e["title"], "Readiness unmet", u, local(e["planned_start_at"])])
        if e["lifecycle_status"] in ("Draft", "Planned", "Mobilising", "Readiness", "Ready") and e["planned_start_at"] < n:
            exc.append([e["event_code"], e["title"], "Delayed", f"Past planned start, still {e['lifecycle_status']}", local(e["planned_start_at"])])
    for r in ctx.conn.execute(select(a.c.activity_code, a.c.title, a.c.due_at, a.c.event_id).where(and_(
            a.c.event_id.in_(ids), a.c.due_at < n, a.c.status.notin_(["Completed", "Submitted"])))).mappings():
        e = next(x for x in evs if x["id"] == r["event_id"])
        if e["lifecycle_status"] not in ("Closed", "Cancelled"):
            exc.append([e["event_code"], e["title"], "Overdue", f"{r['activity_code']} {r['title']}", local(r["due_at"])])
    sheet(wb.create_sheet("Exceptions"), ["Code", "Test", "Exception", "Detail", "Date"], exc, [18, 36, 16, 60, 17], date_cols=(5,), status_col=3)

    # --- Findings and remediation
    ra = t("test_remediation_actions")
    fr = []
    for r in ctx.conn.execute(select(i, ra.c.action_code, ra.c.title.label("action_title"), ra.c.owner_user_id.label("action_owner"),
                                     ra.c.due_at.label("action_due"), ra.c.status.label("action_status"))
                              .outerjoin(ra, ra.c.issue_id == i.c.id).where(i.c.event_id.in_(ids))
                              .order_by(i.c.issue_code)).mappings():
        fr.append([r["issue_code"], r["issue_type"], r["severity"], r["title"], r["status"], names.get(r["owner_user_id"], ""),
                   local(r["due_at"]), r["action_code"] or "", r["action_title"] or "", names.get(r["action_owner"], ""),
                   local(r["action_due"]), r["action_status"] or ""])
    sheet(wb.create_sheet("Findings"), ["Issue", "Type", "Severity", "Title", "Status", "Owner", "Due", "Remediation",
                                        "Corrective action", "Action owner", "Action due", "Action status"],
          fr, [22, 10, 10, 40, 12, 18, 17, 22, 36, 18, 17, 13], date_cols=(7, 11), status_col=5)

    # --- Decisions
    dec = [[next((e["event_code"] for e in evs if e["id"] == r["event_id"]), ""), DECISION_LABEL.get(r["approval_type"], r["approval_type"]), r["decision"],
            names.get(r["approver_user_id"], ""), local(r["decided_at"]), "Yes" if r["superseded_at"] else "", r["comments"] or "",
            r["conditions"] or ""]
           for r in ctx.conn.execute(select(ap).where(ap.c.event_id.in_(ids)).order_by(ap.c.decided_at)).mappings()]
    sheet(wb.create_sheet("Decisions"), ["Test", "Decision", "Result", "By", "When", "Superseded", "Comments", "Conditions"],
          dec, [18, 18, 12, 18, 17, 11, 50, 40], date_cols=(5,), status_col=3)

    # --- Schedule changes (from the audit trail)
    au = t("test_event_audit")
    ch = [[next((e["event_code"] for e in evs if e["id"] == r["event_id"]), ""), names.get(r["actor_user_id"], "system"),
           local(r["occurred_at"]), local(datetime.fromisoformat(r["old_values"]["planned_start_at"])) if r["old_values"] and r["old_values"].get("planned_start_at") else None,
           local(datetime.fromisoformat(r["new_values"]["planned_start_at"])) if r["new_values"] and r["new_values"].get("planned_start_at") else None,
           (r["new_values"] or {}).get("reason") or ""]
          for r in ctx.conn.execute(select(au).where(and_(au.c.event_id.in_(ids), au.c.action == "event.rescheduled"))
                                    .order_by(au.c.seq)).mappings()]
    sheet(wb.create_sheet("Schedule changes"), ["Test", "Changed by", "When", "Old start", "New start", "Reason"],
          ch, [18, 18, 17, 17, 17, 50], date_cols=(3, 4, 5))

    buf = io.BytesIO()
    wb.properties.title = "Resilience test programme status"
    wb.properties.creator = "ResilienceAI"
    wb.save(buf)
    audit.record(ctx, "report.programme_xlsx", "Programme", programme_id, new={"tests": len(evs)})
    label = (prog["programme_code"] if prog else "all").replace(" ", "-")
    return buf.getvalue(), f"programme-status-{label}-{n.astimezone(zone):%Y%m%d}.xlsx"

