import asyncio

import pytest
from conftest import MONTH

from billing_agent import mcp_server
from billing_agent.db import connect
from billing_agent.generate import generate
from billing_agent.prices import load_prices
from billing_agent.reviewer import Review, ReviewError, record_review, review_run, rules_review
from billing_agent.workflow import invoice_id, run_billing, status_of

OK = Review("ok", [], "Im September haben Sie 1.000 kWh verbraucht.")


@pytest.fixture
def run_con(demo_con):
    load_prices(demo_con, MONTH)
    run_billing(demo_con, MONTH)
    return demo_con


def test_rules_review_flags_large_and_warned_invoices(run_con):
    counts = review_run(run_con, MONTH)
    assert sum(counts.values()) == 9  # 7 ready + 2 needs_review; blocked ones are not reviewed
    c07 = rules_review(run_con, invoice_id(MONTH, "C07"))
    assert c07.verdict == "needs_review"
    assert any(r.startswith("large_invoice") for r in c07.reasons)
    assert any(r.startswith("volume_change") for r in c07.reasons)
    assert "September 2026" in c07.customer_explanation


def test_clean_invoice_passes_with_german_explanation(run_con):
    review = rules_review(run_con, invoice_id(MONTH, "C13"))
    assert review.verdict == "ok" and review.reasons == []
    assert "kWh" in review.customer_explanation and "EUR brutto" in review.customer_explanation


@pytest.mark.parametrize(
    "review",
    [
        Review("approved", [], "x"),  # no such verdict
        Review("needs_review", [], "x"),  # flag without a reason
        Review("ok", [], " "),  # empty explanation
        Review("ok", [], "x" * 2000),  # too long
        Review("needs_review", ["x" * 400], "x"),  # reason too long
    ],
)
def test_bad_reviews_are_rejected(run_con, review):
    with pytest.raises(ReviewError):
        record_review(run_con, invoice_id(MONTH, "C01"), "agent", review)


def test_review_cannot_unflag_or_touch_blocked(run_con):
    assert record_review(run_con, invoice_id(MONTH, "C07"), "agent", OK) == "needs_review"
    with pytest.raises(ReviewError, match="blocked"):
        record_review(run_con, invoice_id(MONTH, "C03"), "agent", OK)


def test_agent_flag_moves_ready_to_needs_review(run_con):
    inv = invoice_id(MONTH, "C01")
    flagged = Review("needs_review", ["Baseload doubled at night"], "Erklärung.")
    assert record_review(run_con, inv, "agent", flagged) == "needs_review"
    assert status_of(run_con, inv) == "needs_review"
    actors = [a for (a,) in run_con.execute(
        "SELECT actor FROM audit_log WHERE invoice_id = ? ORDER BY entry_id", [inv]
    ).fetchall()]  # fmt: skip
    assert actors[-2:] == ["agent", "agent"]  # the review, then the status change


# --- MCP server -------------------------------------------------------------------------------


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "billing.duckdb"
    with connect(path) as con:
        generate(con, MONTH)
        load_prices(con, MONTH)
        run_billing(con, MONTH)
    monkeypatch.setenv("BILLING_DB_PATH", str(path))
    return path


def test_server_has_no_approve_or_issue_tool():
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    assert set(tools) == {
        "list_invoices",
        "get_customer",
        "get_issues",
        "get_invoice",
        "get_consumption_summary",
        "compare_prior_month",
        "submit_review",
    }
    writers = [n for n, t in tools.items() if not (t.annotations and t.annotations.read_only_hint)]
    assert writers == ["submit_review"]


def test_read_tools_return_plain_json(db_file):
    import json

    invoices = mcp_server.list_invoices(MONTH)
    assert len(invoices) == 15
    detail = mcp_server.get_invoice(invoice_id(MONTH, "C04"))
    assert detail["contract_type"] == "ppa" and len(detail["lines"]) == 7
    summary = mcp_server.get_consumption_summary("C04", MONTH)
    assert len(summary["avg_kw_by_local_hour"]) == 24
    assert summary["avg_kwh_weekday"] > summary["avg_kwh_weekend_day"]  # metal works
    compare = mcp_server.compare_prior_month("C07", MONTH)
    assert compare["relative_change"]["kwh"] > 0.35
    json.dumps([invoices, detail, summary, compare, mcp_server.get_customer("C02")])


def test_submit_review_reports_errors_instead_of_raising(db_file):
    out = mcp_server.submit_review(invoice_id(MONTH, "C03"), "ok", [], "Erklärung.")
    assert out["stored"] is False and "blocked" in out["error"]
    out = mcp_server.submit_review("RE-1999-01-C99", "ok", [], "Erklärung.")
    assert out["stored"] is False
    out = mcp_server.submit_review(invoice_id(MONTH, "C01"), "ok", [], "Erklärung.")
    assert out == {"stored": True, "invoice_id": invoice_id(MONTH, "C01"), "status": "ready"}
