"""Master-data and meter-data checks that run before any invoice is calculated.

Each rule returns Issues. `block` means the invoice must not be issued until the data is fixed;
`warn` means a person should look before approving.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import duckdb

from billing_agent import malo
from billing_agent.periods import Period, billing_period, previous_month

CONTRACT_TYPES = {
    "fixed": ("fixed_ct_kwh",),
    "spot": ("margin_ct_kwh",),
    "ppa": ("margin_ct_kwh", "ppa_eur_mwh", "ppa_share"),
}
REQUIRED_FIELDS = ("name", "malo_id", "street", "postcode", "city")
# Up to this many missing quarter-hours can be billed after review; more needs substitute values
# (Ersatzwerte) from the grid operator first.
MAX_MISSING_INTERVALS_WARN = 4
MOM_CHANGE_WARN = 0.35


@dataclass(frozen=True)
class Issue:
    customer_id: str
    rule: str
    severity: str  # block | warn
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def check_master_data(con: duckdb.DuckDBPyConnection) -> list[Issue]:
    issues = []
    customers = _rows(con, "SELECT * FROM customers ORDER BY customer_id")
    for c in customers:
        cid = c["customer_id"]
        missing = [f for f in REQUIRED_FIELDS if not c.get(f)]
        if missing:
            issues.append(Issue(cid, "required_fields", "block", f"Missing: {', '.join(missing)}"))
        m = c["malo_id"]
        if m and not malo.is_valid_format(m):
            issues.append(
                Issue(cid, "malo_format", "block", f"MaLo-ID {m} is not 11 digits starting 1-9")
            )
        elif m and not malo.is_valid(m):
            issues.append(
                Issue(
                    cid,
                    "malo_check_digit",
                    "block",
                    f"MaLo-ID {m} fails the check digit (expected {malo.check_digit(m[:10])}, "
                    f"found {m[10]})",
                )
            )

    for d in _rows(
        con,
        """SELECT malo_id, list(customer_id ORDER BY customer_id) AS ids
           FROM customers WHERE malo_id IS NOT NULL
           GROUP BY malo_id HAVING count(*) > 1""",
    ):
        for cid in d["ids"]:
            others = ", ".join(i for i in d["ids"] if i != cid)
            issues.append(
                Issue(
                    cid,
                    "duplicate_malo",
                    "block",
                    f"MaLo-ID {d['malo_id']} is also assigned to {others}",
                )
            )
    return issues


def check_contracts(con: duckdb.DuckDBPyConnection, period: Period) -> list[Issue]:
    issues = []
    rows = _rows(
        con,
        """SELECT c.customer_id, k.* EXCLUDE (customer_id)
           FROM customers c LEFT JOIN contracts k USING (customer_id)
           ORDER BY c.customer_id""",
    )
    for k in rows:
        cid = k["customer_id"]
        if k["contract_id"] is None:
            issues.append(Issue(cid, "contract_missing", "block", "No contract on file"))
            continue
        ctype = k["contract_type"]
        if ctype not in CONTRACT_TYPES:
            issues.append(
                Issue(
                    cid,
                    "contract_type",
                    "block",
                    f"Unknown contract type {ctype!r}; expected one of {sorted(CONTRACT_TYPES)}",
                )
            )
        else:
            absent = [f for f in CONTRACT_TYPES[ctype] if k[f] is None]
            if absent:
                issues.append(
                    Issue(
                        cid,
                        "contract_prices",
                        "block",
                        f"{ctype} contract missing {', '.join(absent)}",
                    )
                )
            if ctype == "ppa" and k["ppa_share"] is not None and not 0 < k["ppa_share"] <= 1:
                issues.append(
                    Issue(
                        cid, "contract_prices", "block", f"PPA share {k['ppa_share']} not in (0, 1]"
                    )
                )
        start, end = k["start_date"], k["end_date"]
        if start and end and end < start:
            issues.append(
                Issue(
                    cid, "contract_dates", "block", f"Contract ends {end} before it starts {start}"
                )
            )
        elif start and end and (start > period.last_day or end < period.first_day):
            issues.append(
                Issue(
                    cid,
                    "contract_not_active",
                    "block",
                    f"Contract {start} to {end} does not cover {period.month}",
                )
            )
    return issues


def check_meter_data(con: duckdb.DuckDBPyConnection, period: Period) -> list[Issue]:
    issues = []
    prior = billing_period(previous_month(period.month))
    stats = _rows(
        con,
        """SELECT c.customer_id,
                  count(r.kwh) FILTER (WHERE r.ts_utc >= ? AND r.ts_utc < ?) AS n,
                  count(*) FILTER (WHERE r.kwh < 0 AND r.ts_utc >= ? AND r.ts_utc < ?) AS neg,
                  min(r.kwh) FILTER (WHERE r.ts_utc >= ? AND r.ts_utc < ?) AS min_kwh,
                  sum(r.kwh) FILTER (WHERE r.ts_utc >= ? AND r.ts_utc < ?) AS kwh,
                  sum(r.kwh) FILTER (WHERE r.ts_utc >= ? AND r.ts_utc < ?) AS prior_kwh
           FROM customers c LEFT JOIN meter_readings r USING (customer_id)
           GROUP BY c.customer_id ORDER BY c.customer_id""",
        [period.start_utc, period.end_utc] * 4 + [prior.start_utc, prior.end_utc],
    )
    expected = period.expected_intervals
    for s in stats:
        cid = s["customer_id"]
        missing = expected - s["n"]
        if s["n"] == 0:
            issues.append(
                Issue(cid, "meter_data_missing", "block", f"No readings for {period.month}")
            )
            continue
        if missing > MAX_MISSING_INTERVALS_WARN:
            issues.append(
                Issue(
                    cid,
                    "intervals_missing",
                    "block",
                    f"{missing} of {expected} quarter-hours missing; request substitute values",
                )
            )
        elif missing > 0:
            issues.append(
                Issue(
                    cid,
                    "intervals_missing",
                    "warn",
                    f"{missing} of {expected} quarter-hours missing; billed as 0 kWh for now",
                )
            )
        if s["neg"]:
            issues.append(
                Issue(
                    cid,
                    "negative_reading",
                    "block",
                    f"{s['neg']} negative reading(s), lowest {s['min_kwh']} kWh",
                )
            )
        if s["prior_kwh"]:
            change = s["kwh"] / s["prior_kwh"] - 1
            if abs(change) > MOM_CHANGE_WARN:
                issues.append(
                    Issue(
                        cid,
                        "volume_change",
                        "warn",
                        f"Consumption {change:+.0%} vs {prior.month} "
                        f"({s['kwh']:,.0f} vs {s['prior_kwh']:,.0f} kWh)",
                    )
                )
    return issues


def run_checks(con: duckdb.DuckDBPyConnection, month: str) -> list[Issue]:
    period = billing_period(month)
    return check_master_data(con) + check_contracts(con, period) + check_meter_data(con, period)
