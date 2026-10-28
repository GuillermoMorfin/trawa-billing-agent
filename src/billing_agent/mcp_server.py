"""MCP server that lets an agent (e.g. Claude Code) review a billing run.

Six read-only tools let the agent see invoices, customers, issues and meter data. One write tool,
`submit_review`, stores a review through the same validation as the rules reviewer. There is no
tool that approves, issues or changes an invoice: those steps stay with people.

Run with: `billing-agent mcp` (stdio). Claude Code starts it from `.mcp.json`.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from billing_agent.config import db_path
from billing_agent.db import connect
from billing_agent.periods import BERLIN, billing_period, previous_month
from billing_agent.reviewer import Review, ReviewError, record_review
from billing_agent.workflow import TransitionError

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

server = MCPServer(
    name="billing-review",
    instructions=(
        "Tools for reviewing a monthly electricity billing run. Amounts are calculated by code and "
        "are final for review purposes: never recalculate or restate an amount the tools did not "
        "return. Use submit_review to record 'ok' or 'needs_review' with reasons. You cannot "
        "approve or issue invoices; a person does that."
    ),
)


@contextmanager
def _db(read_only: bool = True):
    con = connect(db_path(), read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def _plain(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _rows(con, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [{c: _plain(v) for c, v in zip(cols, row, strict=True)} for row in cur.fetchall()]


def _one(con, sql: str, params: list) -> dict | None:
    rows = _rows(con, sql, params)
    return rows[0] if rows else None


@server.tool(annotations=READ_ONLY)
def list_invoices(month: str) -> list[dict]:
    """All invoices for a billing month (YYYY-MM) with status, gross amount and open issues."""
    with _db() as con:
        return _rows(
            con,
            """SELECT i.invoice_id, i.customer_id, c.name, i.contract_type, i.status,
                      i.kwh, i.gross_eur, i.energy_ct_kwh, i.billing_error,
                      list(s.rule ORDER BY s.rule) FILTER (WHERE s.rule IS NOT NULL) AS issue_rules
               FROM invoices i JOIN customers c USING (customer_id)
               LEFT JOIN issues s ON s.month = i.month AND s.customer_id = i.customer_id
               WHERE i.month = ? GROUP BY ALL ORDER BY i.invoice_id""",
            [month],
        )


@server.tool(annotations=READ_ONLY)
def get_customer(customer_id: str) -> dict:
    """Customer master data and contract terms."""
    with _db() as con:
        customer = _one(con, "SELECT * FROM customers WHERE customer_id = ?", [customer_id])
        if customer is None:
            return {"error": f"no customer {customer_id}"}
        customer["contract"] = _one(
            con, "SELECT * FROM contracts WHERE customer_id = ?", [customer_id]
        )
        return customer


@server.tool(annotations=READ_ONLY)
def get_issues(customer_id: str, month: str) -> list[dict]:
    """Data-check findings for a customer and month. 'block' stops billing; 'warn' needs a look."""
    with _db() as con:
        return _rows(
            con,
            "SELECT rule, severity, detail FROM issues WHERE customer_id = ? AND month = ?",
            [customer_id, month],
        )


@server.tool(annotations=READ_ONLY)
def get_invoice(invoice_id: str) -> dict:
    """Invoice header, lines, earlier reviews and audit trail."""
    with _db() as con:
        invoice = _one(con, "SELECT * FROM invoices WHERE invoice_id = ?", [invoice_id])
        if invoice is None:
            return {"error": f"no invoice {invoice_id}"}
        invoice["lines"] = _rows(
            con,
            """SELECT * EXCLUDE (invoice_id) FROM invoice_lines
               WHERE invoice_id = ? ORDER BY line_no""",
            [invoice_id],
        )
        invoice["reviews"] = _rows(
            con,
            "SELECT * EXCLUDE (invoice_id) FROM reviews WHERE invoice_id = ? ORDER BY created_at",
            [invoice_id],
        )
        invoice["audit"] = _rows(
            con,
            """SELECT ts, actor, action, from_status, to_status, note FROM audit_log
               WHERE invoice_id = ? ORDER BY entry_id""",
            [invoice_id],
        )
        return invoice


def _summary(con, customer_id: str, month: str) -> dict:
    p = billing_period(month)
    s = _one(
        con,
        """SELECT count(*) AS intervals,
                  sum(r.kwh) AS kwh,
                  max(r.kwh) * 4 AS peak_kw,
                  quantile_cont(r.kwh, 0.05) * 4 AS base_kw,
                  count(*) FILTER (WHERE r.kwh < 0) AS negative_readings,
                  sum(r.kwh * p.eur_mwh) / nullif(sum(r.kwh), 0) AS load_weighted_eur_mwh
           FROM meter_readings r LEFT JOIN prices p USING (ts_utc)
           WHERE r.customer_id = ? AND r.ts_utc >= ? AND r.ts_utc < ?""",
        [customer_id, p.start_utc, p.end_utc],
    )
    s["month"] = month
    s["expected_intervals"] = p.expected_intervals
    s["missing_intervals"] = p.expected_intervals - s["intervals"]
    s["days"] = (p.end_utc - p.start_utc).days
    s["kwh_per_day"] = (s["kwh"] or 0) / s["days"]
    return s


@server.tool(annotations=READ_ONLY)
def get_consumption_summary(customer_id: str, month: str) -> dict:
    """Meter-data summary: totals, peak and base load, an average day by local hour, weekday vs
    weekend, and the load-weighted vs plain average day-ahead price (shows whether the customer
    uses power at expensive times)."""
    p = billing_period(month)
    with _db() as con:
        summary = _summary(con, customer_id, month)
        (avg_price,) = con.execute(
            "SELECT avg(eur_mwh) FROM prices WHERE ts_utc >= ? AND ts_utc < ?",
            [p.start_utc, p.end_utc],
        ).fetchone()
        summary["market_avg_eur_mwh"] = avg_price
        profile = con.execute(
            """SELECT ts_utc, kwh FROM meter_readings
               WHERE customer_id = ? AND ts_utc >= ? AND ts_utc < ?""",
            [customer_id, p.start_utc, p.end_utc],
        ).df()
    if profile.empty:
        return summary
    local = profile["ts_utc"].dt.tz_localize("UTC").dt.tz_convert(BERLIN)
    kw = profile["kwh"] * 4
    summary["avg_kw_by_local_hour"] = kw.groupby(local.dt.hour).mean().round(1).tolist()
    daily = (
        profile.assign(day=local.dt.date, weekend=local.dt.day_of_week >= 5)
        .groupby(["day", "weekend"])["kwh"]
        .sum()
        .reset_index()
    )
    summary["avg_kwh_weekday"] = round(daily.loc[~daily["weekend"], "kwh"].mean(), 1)
    summary["avg_kwh_weekend_day"] = round(daily.loc[daily["weekend"], "kwh"].mean(), 1)
    return summary


@server.tool(annotations=READ_ONLY)
def compare_prior_month(customer_id: str, month: str) -> dict:
    """Consumption this month vs the month before: total, per day, peak and base load."""
    with _db() as con:
        now = _summary(con, customer_id, month)
        before = _summary(con, customer_id, previous_month(month))
    change = {}
    for key in ("kwh", "kwh_per_day", "peak_kw", "base_kw"):
        if now.get(key) is not None and before.get(key):
            change[key] = round(now[key] / before[key] - 1, 4)
    return {"this_month": now, "prior_month": before, "relative_change": change}


@server.tool()
def submit_review(
    invoice_id: str, verdict: str, reasons: list[str], customer_explanation: str
) -> dict:
    """Record your review of one invoice.

    verdict: "ok" or "needs_review". Use needs_review whenever a person should look before
    approving, and say why in `reasons` (short English sentences, cite the numbers you saw).
    customer_explanation: 2-5 plain German sentences for the customer explaining what drove this
    month's bill, using only numbers returned by the tools.
    This does not approve anything. A needs_review verdict moves a ready invoice to needs_review.
    """
    review = Review(verdict, list(reasons), customer_explanation)
    try:
        with _db(read_only=False) as con:
            status = record_review(con, invoice_id, "agent", review)
    except (ReviewError, TransitionError) as exc:
        return {"stored": False, "error": str(exc)}
    return {"stored": True, "invoice_id": invoice_id, "status": status}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
