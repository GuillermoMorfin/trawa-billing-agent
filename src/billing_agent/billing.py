"""Invoice engine. Deterministic code does every calculation; nothing here is left to the agent.

Energy is priced per quarter-hour where the contract asks for it:
- fixed:  all kWh at one price
- spot:   each quarter-hour's kWh at that quarter-hour's day-ahead price, plus a margin
- ppa:    a baseload share of each quarter-hour at the PPA price; the rest at spot plus margin

Network charges, levies and taxes come from CHARGES. The rates are illustrative round numbers for
a commercial customer, not a current tariff sheet; a real system reads them per grid area and year.

Amounts are summed in full precision and rounded to cents once per invoice line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

import duckdb

from billing_agent.periods import billing_period

CENT = Decimal("0.01")
VAT_RATE = Decimal("0.19")

# (line description, ct/kWh). Illustrative values only.
CHARGES = [
    ("Netzentgelt (illustrative flat rate)", Decimal("4.50")),
    ("Umlagen: KWKG, §19 StromNEV, Offshore (illustrative)", Decimal("1.60")),
    ("Konzessionsabgabe (illustrative)", Decimal("0.11")),
    ("Stromsteuer (illustrative)", Decimal("2.05")),
]


class BillingError(Exception):
    """The invoice cannot be calculated from the data on file."""


@dataclass
class Line:
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal  # EUR per unit
    amount: Decimal  # EUR, rounded to cents
    kind: str = "energy"  # energy | charge


@dataclass
class Invoice:
    customer_id: str
    month: str
    contract_type: str
    kwh: Decimal
    lines: list[Line] = field(default_factory=list)

    @property
    def net(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0"))

    @property
    def vat(self) -> Decimal:
        return _cents(self.net * VAT_RATE)

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat

    @property
    def energy_ct_kwh(self) -> Decimal:
        """Average energy price before network charges and taxes, ct/kWh."""
        energy = sum((line.amount for line in self.lines if line.kind == "energy"), Decimal("0"))
        return (energy / self.kwh * 100).quantize(Decimal("0.001")) if self.kwh else Decimal("0")


def _cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def _d(x: float | Decimal | None) -> Decimal:
    return Decimal(str(x)) if x is not None else Decimal("0")


def _line(
    description: str, quantity: Decimal, unit: str, amount: Decimal, kind: str = "energy"
) -> Line:
    unit_price = (amount / quantity).quantize(Decimal("0.000001")) if quantity else Decimal("0")
    return Line(
        description, quantity.quantize(Decimal("0.001")), unit, unit_price, _cents(amount), kind
    )


def calculate_invoice(con: duckdb.DuckDBPyConnection, customer_id: str, month: str) -> Invoice:
    period = billing_period(month)
    contract = con.execute(
        """SELECT contract_type, fixed_ct_kwh, margin_ct_kwh, ppa_eur_mwh, ppa_share
           FROM contracts WHERE customer_id = ?""",
        [customer_id],
    ).fetchone()
    if contract is None:
        raise BillingError("no contract on file")
    ctype, fixed_ct, margin_ct, ppa_eur_mwh, ppa_share = contract

    kwh, spot_eur, missing_prices = con.execute(
        """SELECT coalesce(sum(r.kwh), 0),
                  coalesce(sum(r.kwh * p.eur_mwh / 1000), 0),
                  count(*) FILTER (WHERE p.eur_mwh IS NULL)
           FROM meter_readings r LEFT JOIN prices p USING (ts_utc)
           WHERE r.customer_id = ? AND r.ts_utc >= ? AND r.ts_utc < ?""",
        [customer_id, period.start_utc, period.end_utc],
    ).fetchone()
    kwh, spot_eur = _d(kwh), _d(spot_eur)
    if ctype in ("spot", "ppa") and missing_prices:
        raise BillingError(f"{missing_prices} quarter-hours have no day-ahead price")

    inv = Invoice(customer_id, month, ctype, kwh)
    if ctype == "fixed":
        if fixed_ct is None:
            raise BillingError("fixed contract has no price")
        inv.lines.append(_line("Energie, Festpreis", kwh, "kWh", kwh * _d(fixed_ct) / 100))
    elif ctype == "spot":
        if margin_ct is None:
            raise BillingError("spot contract has no margin")
        inv.lines.append(_line("Energie, Day-Ahead DE-LU je Viertelstunde", kwh, "kWh", spot_eur))
        inv.lines.append(_line("Beschaffungsmarge", kwh, "kWh", kwh * _d(margin_ct) / 100))
    elif ctype == "ppa":
        if None in (margin_ct, ppa_eur_mwh, ppa_share):
            raise BillingError("PPA contract is missing price, share or margin")
        share = _d(ppa_share)
        ppa_kwh = kwh * share
        residual_kwh = kwh - ppa_kwh
        inv.lines.append(
            _line(
                f"Energie aus PPA, {share:.0%} Baseload-Anteil",
                ppa_kwh,
                "kWh",
                ppa_kwh * _d(ppa_eur_mwh) / 1000,
            )
        )
        inv.lines.append(
            _line(
                "Reststrom, Day-Ahead DE-LU je Viertelstunde",
                residual_kwh,
                "kWh",
                spot_eur * (1 - share),
            )
        )
        inv.lines.append(
            _line(
                "Beschaffungsmarge Reststrom",
                residual_kwh,
                "kWh",
                residual_kwh * _d(margin_ct) / 100,
            )
        )
    else:
        raise BillingError(f"unknown contract type {ctype!r}")

    for description, ct_kwh in CHARGES:
        inv.lines.append(_line(description, kwh, "kWh", kwh * ct_kwh / 100, kind="charge"))
    return inv
