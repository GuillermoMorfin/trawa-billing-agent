"""Second-line review of calculated invoices, by rules or by an agent.

A review can only say "ok" or "needs_review" and add an explanation. It can move a `ready` invoice
to `needs_review`, never the other way, and it can never approve anything or change an amount.
Both the rules reviewer below and the agent (through the MCP server) go through `record_review`,
so the same limits apply to both.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime

import duckdb

from billing_agent.periods import billing_period, previous_month
from billing_agent.workflow import log, status_of, transition

VERDICTS = ("ok", "needs_review")
REVIEWABLE = ("ready", "needs_review")
REVIEWERS = ("rules", "agent")
# Four-eyes rule: large invoices always get a person's eyes, whatever else is true.
LARGE_INVOICE_EUR = 100_000
# An energy price this far from the median of the same contract type is worth a look.
PEER_DEVIATION = 0.25
MAX_REASONS = 8
MAX_REASON_CHARS = 300
MAX_EXPLANATION_CHARS = 1500

MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]  # fmt: skip


class ReviewError(ValueError):
    """The review was rejected; nothing was stored."""


@dataclass
class Review:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    customer_explanation: str = ""


def _month_de(month: str) -> str:
    year, m = month.split("-")
    return f"{MONTHS_DE[int(m) - 1]} {year}"


def _de_number(x: float, decimals: int = 0) -> str:
    s = f"{x:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def validate(review: Review) -> None:
    if review.verdict not in VERDICTS:
        raise ReviewError(f"verdict must be one of {VERDICTS}, got {review.verdict!r}")
    if review.verdict == "needs_review" and not review.reasons:
        raise ReviewError("a needs_review verdict must give at least one reason")
    if len(review.reasons) > MAX_REASONS:
        raise ReviewError(f"at most {MAX_REASONS} reasons")
    if any(not r.strip() or len(r) > MAX_REASON_CHARS for r in review.reasons):
        raise ReviewError(f"each reason must be 1-{MAX_REASON_CHARS} characters")
    if not review.customer_explanation.strip():
        raise ReviewError("customer_explanation must not be empty")
    if len(review.customer_explanation) > MAX_EXPLANATION_CHARS:
        raise ReviewError(f"customer_explanation is limited to {MAX_EXPLANATION_CHARS} characters")


def record_review(
    con: duckdb.DuckDBPyConnection, invoice: str, reviewer: str, review: Review
) -> str:
    """Validate and store a review; return the invoice's status afterwards."""
    if reviewer not in REVIEWERS:
        raise ReviewError(f"reviewer must be one of {REVIEWERS}")
    validate(review)
    status = status_of(con, invoice)
    if status not in REVIEWABLE:
        raise ReviewError(f"{invoice} is {status}; only {' or '.join(REVIEWABLE)} can be reviewed")

    con.execute(
        "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
        [
            invoice,
            reviewer,
            review.verdict,
            review.reasons,
            review.customer_explanation,
            datetime.now(UTC).replace(tzinfo=None),
        ],
    )
    log(
        con,
        invoice,
        reviewer,
        "review",
        status,
        status,
        f"{review.verdict}: {'; '.join(review.reasons)}",
    )
    if review.verdict == "needs_review" and status == "ready":
        transition(con, invoice, "needs_review", reviewer, "; ".join(review.reasons))
        return "needs_review"
    return status


def _explanation(con: duckdb.DuckDBPyConnection, inv: dict) -> str:
    period = billing_period(inv["month"])
    prior = billing_period(previous_month(inv["month"]))
    (prior_kwh,) = con.execute(
        "SELECT sum(kwh) FROM meter_readings WHERE customer_id = ? AND ts_utc >= ? AND ts_utc < ?",
        [inv["customer_id"], prior.start_utc, prior.end_utc],
    ).fetchone()
    days = (period.end_utc - period.start_utc).days
    kwh = float(inv["kwh"])
    text = (
        f"Im {_month_de(inv['month'])} haben Sie {_de_number(kwh)} kWh verbraucht, "
        f"im Schnitt {_de_number(kwh / days)} kWh pro Tag"
    )
    if prior_kwh:
        change = kwh / prior_kwh - 1
        text += f" ({_de_number(change * 100, 1)} % gegenüber {_month_de(prior.month)})"
    price = _de_number(float(inv["energy_ct_kwh"]), 2)
    text += f". Ihr durchschnittlicher Energiepreis lag bei {price} ct/kWh"
    if inv["contract_type"] == "spot":
        text += ", abgerechnet je Viertelstunde zum Day-Ahead-Preis zuzüglich Marge"
    elif inv["contract_type"] == "ppa":
        text += ", aus PPA-Strom zum Festpreis und Reststrom zum Day-Ahead-Preis"
    text += (
        f". Mit Netzentgelten, Umlagen und Steuern beträgt die Rechnung "
        f"{_de_number(float(inv['gross_eur']), 2)} EUR brutto."
    )
    return text


def rules_review(con: duckdb.DuckDBPyConnection, invoice: str) -> Review:
    cur = con.execute("SELECT * FROM invoices WHERE invoice_id = ?", [invoice])
    inv = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))

    reasons = [
        f"{rule}: {detail}"
        for rule, detail in con.execute(
            """SELECT rule, detail FROM issues
               WHERE month = ? AND customer_id = ? AND severity = 'warn'""",
            [inv["month"], inv["customer_id"]],
        ).fetchall()
    ]
    if inv["gross_eur"] >= LARGE_INVOICE_EUR:
        reasons.append(
            f"large_invoice: {inv['gross_eur']:,.2f} EUR gross is above the "
            f"{LARGE_INVOICE_EUR:,} EUR four-eyes limit"
        )
    peers = [
        float(p)
        for (p,) in con.execute(
            """SELECT energy_ct_kwh FROM invoices
               WHERE month = ? AND contract_type = ? AND energy_ct_kwh IS NOT NULL""",
            [inv["month"], inv["contract_type"]],
        ).fetchall()
    ]
    if len(peers) >= 3:
        median = statistics.median(peers)
        own = float(inv["energy_ct_kwh"])
        if median and abs(own / median - 1) > PEER_DEVIATION:
            reasons.append(
                f"price_outlier: energy price {own:.2f} ct/kWh vs {median:.2f} ct/kWh median "
                f"for {inv['contract_type']} contracts"
            )
    return Review("needs_review" if reasons else "ok", reasons, _explanation(con, inv))


def review_run(con: duckdb.DuckDBPyConnection, month: str) -> dict[str, int]:
    """Run the rules reviewer over every reviewable invoice of the month."""
    counts: dict[str, int] = {}
    invoices = [
        i
        for (i,) in con.execute(
            """SELECT invoice_id FROM invoices
               WHERE month = ? AND list_contains(?, status) ORDER BY 1""",
            [month, list(REVIEWABLE)],
        ).fetchall()
    ]
    for invoice in invoices:
        review = rules_review(con, invoice)
        record_review(con, invoice, "rules", review)
        counts[review.verdict] = counts.get(review.verdict, 0) + 1
    return counts
