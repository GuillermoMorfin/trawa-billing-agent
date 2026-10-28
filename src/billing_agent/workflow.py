"""Invoice statuses, who may move an invoice between them, and the audit trail.

    draft ──► blocked        any blocking data issue, or the invoice cannot be calculated
          ├─► needs_review   warnings, or a reviewer asked for a second look
          └─► ready          nothing found
    needs_review / ready ──► approved   (people only)
    needs_review / ready ──► returned   (people only; fix the data and re-run)
    approved ──► issued                  (people only)

Re-running a month rebuilds every invoice that is not yet approved or issued. Approved and issued
invoices are never recalculated.
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from billing_agent.billing import BillingError, calculate_invoice
from billing_agent.checks import Issue, run_checks

FINAL = ("approved", "issued")
TRANSITIONS = {
    ("draft", "blocked"): {"system"},
    ("draft", "needs_review"): {"system"},
    ("draft", "ready"): {"system"},
    ("ready", "needs_review"): {"rules", "agent", "human"},
    ("needs_review", "approved"): {"human"},
    ("ready", "approved"): {"human"},
    ("needs_review", "returned"): {"human"},
    ("ready", "returned"): {"human"},
    ("approved", "issued"): {"human"},
}


class TransitionError(Exception):
    """The requested status change is not allowed for this actor."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _role(actor: str) -> str:
    return actor.split(":", 1)[0]


def invoice_id(month: str, customer_id: str) -> str:
    return f"RE-{month}-{customer_id}"


def log(
    con: duckdb.DuckDBPyConnection,
    invoice: str,
    actor: str,
    action: str,
    from_status: str | None = None,
    to_status: str | None = None,
    note: str | None = None,
) -> None:
    con.execute(
        """INSERT INTO audit_log (ts, invoice_id, actor, action, from_status, to_status, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [_now(), invoice, actor, action, from_status, to_status, note],
    )


def status_of(con: duckdb.DuckDBPyConnection, invoice: str) -> str:
    row = con.execute("SELECT status FROM invoices WHERE invoice_id = ?", [invoice]).fetchone()
    if row is None:
        raise TransitionError(f"invoice {invoice} does not exist")
    return row[0]


def transition(
    con: duckdb.DuckDBPyConnection, invoice: str, to_status: str, actor: str, note: str = ""
) -> None:
    current = status_of(con, invoice)
    allowed = TRANSITIONS.get((current, to_status))
    if allowed is None:
        raise TransitionError(f"{invoice}: cannot go from {current} to {to_status}")
    if _role(actor) not in allowed:
        raise TransitionError(
            f"{invoice}: {actor} may not move an invoice from {current} to {to_status}"
        )
    if to_status == "approved" and not note and current == "needs_review":
        raise TransitionError(f"{invoice}: approving a flagged invoice needs a note")
    con.execute(
        "UPDATE invoices SET status = ?, updated_at = ? WHERE invoice_id = ?",
        [to_status, _now(), invoice],
    )
    log(con, invoice, actor, "status", current, to_status, note or None)


def _clear_open(con: duckdb.DuckDBPyConnection, month: str) -> set[str]:
    """Delete this month's unfinished invoices; return the customers whose invoice is final."""
    final = {
        cid
        for (cid,) in con.execute(
            "SELECT customer_id FROM invoices WHERE month = ? AND list_contains(?, status)",
            [month, list(FINAL)],
        ).fetchall()
    }
    open_ids = [
        i
        for (i,) in con.execute(
            "SELECT invoice_id FROM invoices WHERE month = ? AND NOT list_contains(?, status)",
            [month, list(FINAL)],
        ).fetchall()
    ]
    if open_ids:
        con.execute("DELETE FROM invoice_lines WHERE list_contains(?, invoice_id)", [open_ids])
        con.execute("DELETE FROM reviews WHERE list_contains(?, invoice_id)", [open_ids])
        con.execute("DELETE FROM invoices WHERE list_contains(?, invoice_id)", [open_ids])
    con.execute("DELETE FROM issues WHERE month = ?", [month])
    return final


def _store_issues(con: duckdb.DuckDBPyConnection, month: str, issues: list[Issue]) -> None:
    con.executemany(
        "INSERT INTO issues VALUES (?, ?, ?, ?, ?)",
        [[month, i.customer_id, i.rule, i.severity, i.detail] for i in issues],
    )


def run_billing(con: duckdb.DuckDBPyConnection, month: str) -> dict[str, int]:
    """Check data, calculate every open invoice for `month`, and set its first status."""
    con.execute("BEGIN")
    try:
        final = _clear_open(con, month)
        issues = run_checks(con, month)
        _store_issues(con, month, issues)
        by_customer: dict[str, list[Issue]] = {}
        for i in issues:
            by_customer.setdefault(i.customer_id, []).append(i)

        counts: dict[str, int] = {}
        customers = [
            c for (c,) in con.execute("SELECT customer_id FROM customers ORDER BY 1").fetchall()
        ]
        for cid in customers:
            if cid in final:
                counts["kept_final"] = counts.get("kept_final", 0) + 1
                continue
            inv_id = invoice_id(month, cid)
            found = by_customer.get(cid, [])
            error = None
            try:
                inv = calculate_invoice(con, cid, month)
            except BillingError as exc:
                inv, error = None, str(exc)

            if error or any(i.severity == "block" for i in found):
                status = "blocked"
            elif found:
                status = "needs_review"
            else:
                status = "ready"

            con.execute(
                """INSERT INTO invoices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
                [
                    inv_id,
                    month,
                    cid,
                    inv.contract_type if inv else None,
                    inv.kwh if inv else None,
                    inv.net if inv else None,
                    inv.vat if inv else None,
                    inv.gross if inv else None,
                    inv.energy_ct_kwh if inv else None,
                    error,
                    _now(),
                ],
            )
            if inv:
                con.executemany(
                    "INSERT INTO invoice_lines VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        [
                            inv_id,
                            n,
                            ln.description,
                            ln.quantity,
                            ln.unit,
                            ln.unit_price,
                            ln.amount,
                            ln.kind,
                        ]
                        for n, ln in enumerate(inv.lines, start=1)
                    ],
                )
            log(con, inv_id, "system", "calculated", None, "draft", error)
            reasons = "; ".join(f"{i.rule}: {i.detail}" for i in found) or None
            transition(con, inv_id, status, "system", reasons or "")
            counts[status] = counts.get(status, 0) + 1
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return counts
