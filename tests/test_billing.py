"""Each contract type against a hand-worked example.

Two quarter-hours of 10 kWh each, priced at 100 EUR/MWh and -20 EUR/MWh (negative prices happen
on sunny, windy days). Charges total 4.50 + 1.60 + 0.11 + 2.05 = 8.26 ct/kWh -> 1.652 EUR on 20 kWh.
"""

from decimal import Decimal

import pytest
from conftest import MONTH

from billing_agent.billing import BillingError, calculate_invoice
from billing_agent.periods import billing_period

CHARGES_EUR = Decimal("1.65")  # 20 kWh x 8.26 ct, rounded per line: 0.90 + 0.32 + 0.02 + 0.41


@pytest.fixture
def tiny(con):
    p = billing_period(MONTH)
    t0, t1 = p.start_utc, p.start_utc.replace(minute=15)
    con.execute("INSERT INTO customers VALUES ('X', 'Test GmbH', 'office', NULL, NULL, NULL, NULL)")
    con.execute("INSERT INTO meter_readings VALUES ('X', ?, 10), ('X', ?, 10)", [t0, t1])
    con.execute(
        "INSERT INTO prices VALUES (?, 'quarterhour', 100), (?, 'quarterhour', -20)", [t0, t1]
    )
    return con


def _contract(con, ctype, fixed=None, margin=None, ppa=None, share=None):
    con.execute(
        """INSERT INTO contracts
           VALUES ('K', 'X', ?, DATE '2026-01-01', DATE '2027-12-31', ?, ?, ?, ?)""",
        [ctype, fixed, margin, ppa, share],
    )


def _amounts(inv):
    return [line.amount for line in inv.lines if line.kind == "energy"]


def test_fixed(tiny):
    _contract(tiny, "fixed", fixed=12.0)
    inv = calculate_invoice(tiny, "X", MONTH)
    assert inv.kwh == Decimal("20")
    assert _amounts(inv) == [Decimal("2.40")]  # 20 kWh x 12 ct
    assert inv.net == Decimal("2.40") + CHARGES_EUR
    assert inv.vat == (inv.net * Decimal("0.19")).quantize(Decimal("0.01"))
    assert inv.energy_ct_kwh == Decimal("12.000")


def test_spot_prices_each_quarter_hour(tiny):
    _contract(tiny, "spot", margin=1.5)
    inv = calculate_invoice(tiny, "X", MONTH)
    # 10 x 0.10 + 10 x (-0.02) = 0.80 EUR; margin 20 x 1.5 ct = 0.30 EUR
    assert _amounts(inv) == [Decimal("0.80"), Decimal("0.30")]


def test_ppa_splits_baseload_share(tiny):
    _contract(tiny, "ppa", margin=1.5, ppa=70.0, share=0.5)
    inv = calculate_invoice(tiny, "X", MONTH)
    # PPA 10 kWh x 0.07 = 0.70; residual spot 5 x 0.10 + 5 x (-0.02) = 0.40; margin 10 x 1.5 ct
    assert _amounts(inv) == [Decimal("0.70"), Decimal("0.40"), Decimal("0.15")]
    assert inv.lines[0].quantity == Decimal("10.000")


def test_missing_price_refuses_spot(tiny):
    _contract(tiny, "spot", margin=1.5)
    tiny.execute("DELETE FROM prices WHERE eur_mwh < 0")
    with pytest.raises(BillingError, match="no day-ahead price"):
        calculate_invoice(tiny, "X", MONTH)


def test_unknown_contract_type(tiny):
    _contract(tiny, "flex")
    with pytest.raises(BillingError, match="unknown contract type"):
        calculate_invoice(tiny, "X", MONTH)


def test_demo_month_bills_clean_customers(demo_con):
    from billing_agent.prices import load_prices

    load_prices(demo_con, MONTH)
    inv = calculate_invoice(demo_con, "C14", MONTH)  # foundry on spot
    assert inv.kwh > 0 and inv.gross > inv.net > 0
    assert 5 < inv.energy_ct_kwh < 30  # plausible all-in energy price in ct/kWh
