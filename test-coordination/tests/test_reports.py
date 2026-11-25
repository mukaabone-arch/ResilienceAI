"""Management reports: closure PDF and programme status workbook (Screen 10)."""
from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook


def _closed_event(as_):
    evs = as_("arjun.pm").get("/test-events?status=Closed", expect=200).json()
    if not evs:
        pytest.skip("needs a closed test; run the whole suite (test_journey closes one)")
    return evs[0]


def test_closure_pdf_for_closed_test(as_):
    ev = _closed_event(as_)
    r = as_("leela.cio").get(f"/test-events/{ev['id']}/closure-report.pdf", expect=200)
    assert r.content[:5] == b"%PDF-" and len(r.content) > 3000
    assert r.headers["content-type"] == "application/pdf"
    assert "closure-report.pdf" in r.headers["content-disposition"] and "DRAFT" not in r.headers["content-disposition"]


def test_closure_pdf_needs_closure_and_visibility(as_):
    ev = _closed_event(as_)
    drafts = as_("arjun.pm").get("/test-events?status=Draft", expect=200).json()
    if drafts:
        as_("arjun.pm").get(f"/test-events/{drafts[0]['id']}/closure-report.pdf", expect=404)
    as_("arjun.pm", tenant="B").get(f"/test-events/{ev['id']}/closure-report.pdf", expect=404)


def test_programme_workbook(as_):
    ev = _closed_event(as_)
    r = as_("arjun.pm").get(f"/reports/programme-status.xlsx?programme_id={ev['programme_id']}&tz=Asia/Kolkata", expect=200)
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Summary", "Tests", "Exceptions", "Findings", "Decisions", "Schedule changes"]
    tests = list(wb["Tests"].iter_rows(values_only=True))
    assert tests[0][:5] == ("Code", "Test", "Category", "Criticality", "Stage") and len(tests) > 1
    assert "Closed" in {row[4] for row in tests[1:]}
    assert wb["Summary"]["A1"].value.endswith("resilience test programme status")
    assert any(row[0] for row in wb["Decisions"].iter_rows(min_row=2, values_only=True))


def test_workbook_is_for_portfolio_roles(as_):
    as_("rahul.dba").get("/reports/programme-status.xlsx", expect=403)
    as_("audit.ia").get("/reports/programme-status.xlsx", expect=200)
    as_("arjun.pm").get("/reports/programme-status.xlsx?tz=Mars/Base", expect=422)
