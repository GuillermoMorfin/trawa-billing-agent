"""Streamlit review app: the human step of the billing run.

Launch with `billing-agent app`. Every database access opens a short-lived connection, so the app
and the MCP server (used by the review agent) can work on the same file.
"""

from __future__ import annotations

import time

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

from billing_agent.config import db_path
from billing_agent.db import connect
from billing_agent.periods import BERLIN, billing_period
from billing_agent.reviewer import review_run
from billing_agent.workflow import TransitionError, run_billing, transition

# Chart colors from the dataviz reference palette (categorical slot 1), stepped per theme.
SERIES = {"light": "#2a78d6", "dark": "#3987e5"}
STATUS_BADGE = {
    "ready": ("green", ":material/check_circle:"),
    "needs_review": ("orange", ":material/visibility:"),
    "blocked": ("red", ":material/block:"),
    "approved": ("blue", ":material/verified:"),
    "issued": ("gray", ":material/send:"),
    "returned": ("violet", ":material/undo:"),
}
# Who usually fixes each kind of blocking problem.
FIX_OWNER = {
    "malo_check_digit": "Customer / grid operator: confirm the correct MaLo-ID",
    "malo_format": "Customer / grid operator: confirm the correct MaLo-ID",
    "duplicate_malo": "Master data team: merge or correct the duplicate customer",
    "negative_reading": "Grid operator (Messstellenbetreiber): request corrected meter data",
    "intervals_missing": "Grid operator: request substitute values (Ersatzwerte)",
    "meter_data_missing": "Grid operator: request the month's load profile",
    "contract_dates": "Sales / contract team: correct the contract term",
    "contract_not_active": "Sales / contract team: renew or end the supply",
    "contract_type": "Sales / contract team: map the contract to a supported product",
    "contract_prices": "Sales / contract team: complete the price terms",
    "contract_missing": "Sales / contract team: add the contract",
    "required_fields": "Master data team: complete the customer record",
}


def _connect(read_only: bool) -> duckdb.DuckDBPyConnection:
    # The agent may hold the file for a moment while it stores a review; wait briefly.
    for attempt in range(10):
        try:
            return connect(db_path(), read_only=read_only)
        except duckdb.IOException:
            if attempt == 9:
                raise
            time.sleep(0.2)
    raise RuntimeError("unreachable")


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    con = _connect(read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


def act(fn, *args) -> str | None:
    """Run a write action; return an error message or None."""
    con = _connect(read_only=False)
    try:
        fn(con, *args)
        return None
    except TransitionError as exc:
        return str(exc)
    finally:
        con.close()


def badge(status: str) -> None:
    color, icon = STATUS_BADGE.get(status, ("gray", None))
    st.badge(status.replace("_", " "), color=color, icon=icon)


def eur(x) -> str:
    return "–" if x is None or pd.isna(x) else f"{x:,.2f} EUR"


def series_color() -> str:
    theme = getattr(st.context, "theme", None)
    return SERIES["dark" if theme and theme.type == "dark" else "light"]


def line_chart(df: pd.DataFrame, field: str, title: str, unit: str) -> alt.Chart:
    """Single-series line with a hover crosshair and tooltip."""
    hover = alt.selection_point(nearest=True, on="pointerover", fields=["local"], empty=False)
    base = alt.Chart(df).encode(
        x=alt.X("local:T", title=None, axis=alt.Axis(format="%d %b", grid=False)),
        y=alt.Y(f"{field}:Q", title=unit, axis=alt.Axis(gridOpacity=0.4)),
    )
    line = base.mark_line(strokeWidth=1.5, color=series_color())
    points = base.mark_point(size=60, filled=True, color=series_color()).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0))
    )
    rule = (
        alt.Chart(df)
        .mark_rule(opacity=0.5)
        .encode(
            x="local:T",
            opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
            tooltip=[
                alt.Tooltip("local:T", title="Time", format="%a %d %b %H:%M"),
                alt.Tooltip(f"{field}:Q", title=unit, format=",.1f"),
            ],
        )
        .add_params(hover)
    )
    return (line + points + rule).properties(title=title, height=220)


# --- page ----------------------------------------------------------------------------------------

st.set_page_config(
    page_title="Billing run review", page_icon=":material/receipt_long:", layout="wide"
)

months = query("SELECT DISTINCT month FROM invoices ORDER BY month DESC")["month"].tolist()
with st.sidebar:
    st.header("Billing run")
    if not months:
        st.info("No invoices yet. Run `billing-agent run` first.")
        st.stop()
    month = st.selectbox("Month", months, key="month")
    reviewer = st.text_input("Your name (for the audit log)", value="", key="reviewer").strip()
    st.divider()
    if st.button("Re-run checks and billing", key="rerun", icon=":material/refresh:"):
        err = act(lambda con: (run_billing(con, month), review_run(con, month)))
        st.toast(err or "Re-ran the month. Approved and issued invoices were kept.")
    st.caption(
        "Code checks the data and calculates every amount. Reviews come from rules and from the "
        "agent (`/review-billing-run` in Claude Code). Only people approve and issue."
    )

invoices = query(
    """SELECT i.*, c.name, c.industry,
              coalesce((SELECT string_agg(rule, ', ' ORDER BY rule) FROM issues s
               WHERE s.month = i.month AND s.customer_id = i.customer_id), '') AS rules,
              (SELECT count(*) FROM reviews r WHERE r.invoice_id = i.invoice_id
               AND r.reviewer = 'agent') AS agent_reviews
       FROM invoices i JOIN customers c USING (customer_id)
       WHERE i.month = ? ORDER BY i.invoice_id""",
    [month],
)
by_status = invoices["status"].value_counts()
billable = invoices[invoices["status"] != "blocked"]

st.title(f"Billing run {month}")
cols = st.columns(5)
cols[0].metric("Invoices", len(invoices))
cols[1].metric("Gross billable, EUR", f"{billable['gross_eur'].sum() / 1000:,.1f}k")
cols[2].metric("Blocked", int(by_status.get("blocked", 0)))
cols[3].metric("Needs review", int(by_status.get("needs_review", 0)))
cols[4].metric("Approved or issued", int(by_status.get("approved", 0) + by_status.get("issued", 0)))

queue_tab, invoice_tab, blocked_tab, audit_tab = st.tabs(
    ["Review queue", "Invoice detail", "Blocked", "Audit log"]
)

with queue_tab:
    table = invoices.assign(
        agent=invoices["agent_reviews"].gt(0).map({True: "reviewed", False: ""}),
    )
    st.dataframe(
        table[
            [
                "invoice_id",
                "name",
                "contract_type",
                "status",
                "kwh",
                "energy_ct_kwh",
                "gross_eur",
                "rules",
                "agent",
            ]
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "invoice_id": "Invoice",
            "name": "Customer",
            "contract_type": "Contract",
            "status": "Status",
            "kwh": st.column_config.NumberColumn("kWh", format="%,.0f"),
            "energy_ct_kwh": st.column_config.NumberColumn("Energy ct/kWh", format="%.2f"),
            "gross_eur": st.column_config.NumberColumn("Gross EUR", format="%,.2f"),
            "rules": "Data checks",
            "agent": "Agent",
        },
    )

with invoice_tab:
    open_first = invoices.sort_values(
        "status", key=lambda s: s.map({"needs_review": 0, "ready": 1}).fillna(2)
    )
    labels = {r.invoice_id: f"{r.invoice_id} · {r.name}" for r in open_first.itertuples()}
    selected = st.selectbox("Invoice", list(labels), format_func=labels.get, key="invoice")
    inv = invoices.set_index("invoice_id").loc[selected]

    head = st.columns([3, 2])
    with head[0]:
        st.subheader(inv["name"])
        badge(inv["status"])
        st.caption(
            f"{inv['industry'].replace('_', ' ')} · {inv['contract_type'] or 'no contract type'}"
        )
    with head[1]:
        st.metric("Gross", eur(inv["gross_eur"]))
        if pd.notna(inv["billing_error"]):
            st.error(f"Cannot calculate: {inv['billing_error']}")

    reviews = query("SELECT * FROM reviews WHERE invoice_id = ? ORDER BY created_at", [selected])
    if reviews.empty:
        st.info("No reviews yet. Blocked invoices are not reviewed until their data is fixed.")
    else:
        rcols = st.columns(len(reviews))
        for col, r in zip(rcols, reviews.itertuples(), strict=True):
            with col.container(border=True):
                st.markdown(f"**{'Agent' if r.reviewer == 'agent' else 'Rules'} review**")
                st.badge(
                    r.verdict.replace("_", " "),
                    color="orange" if r.verdict == "needs_review" else "green",
                )
                for reason in r.reasons:
                    st.markdown(f"- {reason}")
                st.caption("For the customer")
                st.write(r.customer_explanation)

    # Actions: only what the workflow allows from this status.
    with st.container(border=True):
        st.markdown("**Decision**")
        status = inv["status"]
        if not reviewer:
            st.caption("Enter your name in the sidebar to approve, return or issue.")
        elif status in ("ready", "needs_review"):
            note = st.text_area(
                "Note"
                + (" (required to approve a flagged invoice)" if status == "needs_review" else ""),
                key=f"note-{selected}",
            )
            a, b = st.columns(2)
            if a.button(
                "Approve", type="primary", key=f"approve-{selected}", icon=":material/check:"
            ):
                err = act(transition, selected, "approved", f"human:{reviewer}", note)
                st.toast(err or f"Approved {selected}")
                if not err:
                    st.rerun()
            if b.button("Send back", key=f"return-{selected}", icon=":material/undo:"):
                err = act(transition, selected, "returned", f"human:{reviewer}", note)
                st.toast(err or f"Sent back {selected}. Fix the data and re-run.")
                if not err:
                    st.rerun()
        elif status == "approved":
            if st.button(
                "Issue invoice", type="primary", key=f"issue-{selected}", icon=":material/send:"
            ):
                err = act(transition, selected, "issued", f"human:{reviewer}", "")
                st.toast(err or f"Issued {selected}")
                if not err:
                    st.rerun()
        else:
            st.caption(f"No decision possible while the invoice is {status.replace('_', ' ')}.")

    lines = query(
        """SELECT description, quantity, unit, unit_price, amount_eur, kind
           FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no""",
        [selected],
    )
    if not lines.empty:
        st.markdown("**Invoice lines**")
        st.dataframe(
            lines,
            hide_index=True,
            width="stretch",
            column_config={
                "description": "Line",
                "quantity": st.column_config.NumberColumn("Quantity", format="%,.0f"),
                "unit": "Unit",
                "unit_price": st.column_config.NumberColumn("EUR per unit", format="%.5f"),
                "amount_eur": st.column_config.NumberColumn("Amount EUR", format="%,.2f"),
                "kind": "Kind",
            },
        )
        st.caption(
            f"Net {eur(inv['net_eur'])} · VAT 19% {eur(inv['vat_eur'])} · "
            f"Gross {eur(inv['gross_eur'])}. Network charges and levies are illustrative rates."
        )

    p = billing_period(month)
    series = query(
        """SELECT r.ts_utc, r.kwh * 4 AS kw, p.eur_mwh
           FROM meter_readings r LEFT JOIN prices p USING (ts_utc)
           WHERE r.customer_id = ? AND r.ts_utc >= ? AND r.ts_utc < ? ORDER BY r.ts_utc""",
        [inv["customer_id"], p.start_utc, p.end_utc],
    )
    if not series.empty:
        series["local"] = (
            series["ts_utc"].dt.tz_localize("UTC").dt.tz_convert(BERLIN).dt.tz_localize(None)
        )
        st.altair_chart(line_chart(series, "kw", "Load, 15-minute average", "kW"), width="stretch")
        st.altair_chart(
            line_chart(series, "eur_mwh", "Day-ahead price DE-LU", "EUR/MWh"), width="stretch"
        )

with blocked_tab:
    blocked = query(
        """SELECT s.customer_id, c.name, s.rule, s.detail FROM issues s
           JOIN customers c USING (customer_id)
           WHERE s.month = ? AND s.severity = 'block' ORDER BY s.customer_id, s.rule""",
        [month],
    )
    if blocked.empty:
        st.success("Nothing is blocked this month.")
    else:
        blocked["fix"] = blocked["rule"].map(FIX_OWNER).fillna("Billing team")
        st.dataframe(
            blocked,
            hide_index=True,
            width="stretch",
            column_config={
                "customer_id": "Customer",
                "name": "Name",
                "rule": "Check",
                "detail": "What is wrong",
                "fix": "Who fixes it",
            },
        )

with audit_tab:
    audit = query(
        """SELECT a.ts, a.invoice_id, a.actor, a.action, a.from_status, a.to_status, a.note
           FROM audit_log a JOIN invoices i USING (invoice_id)
           WHERE i.month = ? ORDER BY a.entry_id DESC""",
        [month],
    )
    st.dataframe(audit, hide_index=True, width="stretch")
