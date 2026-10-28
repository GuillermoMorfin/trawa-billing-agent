from conftest import MONTH

from billing_agent.checks import run_checks
from billing_agent.generate import INJECTED_ISSUES, SPECS


def _by_customer(issues):
    out = {}
    for i in issues:
        out.setdefault(i.customer_id, set()).add((i.rule, i.severity))
    return out


def test_every_injected_issue_is_caught(demo_con):
    found = _by_customer(run_checks(demo_con, MONTH))
    assert ("malo_check_digit", "block") in found["C03"]
    assert ("intervals_missing", "warn") in found["C05"]
    assert ("negative_reading", "block") in found["C06"]
    assert ("volume_change", "warn") in found["C07"]
    assert ("duplicate_malo", "block") in found["C08"]
    assert ("duplicate_malo", "block") in found["C02"]
    assert ("contract_dates", "block") in found["C09"]
    assert ("contract_type", "block") in found["C10"]


def test_clean_customers_have_no_issues(demo_con):
    found = _by_customer(run_checks(demo_con, MONTH))
    flagged = set(INJECTED_ISSUES) | {"C02"}  # C02 shares its MaLo-ID with C08
    clean = {s.customer_id for s in SPECS} - flagged
    assert clean and not clean & set(found), {c: found[c] for c in clean & set(found)}


def test_large_gap_blocks(demo_con):
    demo_con.execute(
        """DELETE FROM meter_readings WHERE customer_id = 'C01'
           AND ts_utc >= TIMESTAMP '2026-09-10 00:00' AND ts_utc < TIMESTAMP '2026-09-10 02:00'"""
    )
    found = _by_customer(run_checks(demo_con, MONTH))
    assert ("intervals_missing", "block") in found["C01"]


def test_contract_not_covering_month_blocks(demo_con):
    demo_con.execute("UPDATE contracts SET end_date = DATE '2026-08-31' WHERE customer_id = 'C01'")
    found = _by_customer(run_checks(demo_con, MONTH))
    assert ("contract_not_active", "block") in found["C01"]
