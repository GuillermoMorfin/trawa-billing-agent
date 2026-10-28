from datetime import datetime

from billing_agent.periods import billing_period, previous_month


def test_september_has_full_days():
    p = billing_period("2026-09")
    assert p.expected_intervals == 30 * 96
    assert p.start_utc == datetime(2026, 8, 31, 22, 0)
    assert p.end_utc == datetime(2026, 9, 30, 22, 0)


def test_clock_changes():
    # Clocks go back on 25 Oct 2026 (one 25-hour day) and forward on 29 Mar 2026 (23 hours).
    assert billing_period("2026-10").expected_intervals == 31 * 96 + 4
    assert billing_period("2026-03").expected_intervals == 31 * 96 - 4


def test_previous_month_crosses_year():
    assert previous_month("2027-01") == "2026-12"
    assert billing_period("2026-12").last_day.isoformat() == "2026-12-31"
