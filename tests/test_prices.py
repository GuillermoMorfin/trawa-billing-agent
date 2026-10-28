from datetime import UTC, datetime

import httpx
import pytest
from conftest import MONTH

from billing_agent.periods import billing_period
from billing_agent.prices import fetch_smard, load_prices, missing_price_intervals


def _ms(ts):
    return int(ts.replace(tzinfo=UTC).timestamp() * 1000)


def test_snapshot_covers_demo_month(con):
    assert load_prices(con, MONTH) == billing_period(MONTH).expected_intervals
    assert missing_price_intervals(con, MONTH) == 0


def test_snapshot_refuses_month_it_does_not_have(con):
    with pytest.raises(ValueError, match="quarter-hour prices"):
        load_prices(con, "2025-01")


def test_fetch_smard_picks_overlapping_weeks_and_trims():
    week0 = datetime(2026, 8, 30, 22, 0)
    week1 = datetime(2026, 9, 6, 22, 0)
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path.endswith("index_quarterhour.json"):
            return httpx.Response(200, json={"timestamps": [_ms(week0), _ms(week1)]})
        start = int(request.url.path.rsplit("_", 1)[1].removesuffix(".json"))
        series = [[start + i * 900_000, 100.0 + i] for i in range(7 * 96)]
        series[3][1] = None  # SMARD uses null for not-yet-published values
        return httpx.Response(200, json={"series": series})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    start, end = datetime(2026, 9, 5, 22, 0), datetime(2026, 9, 7, 22, 0)  # spans both weeks
    df = fetch_smard(start, end, client)

    assert len(requested) == 3  # index + two weekly files
    assert df["ts_utc"].min() == start and df["ts_utc"].max() < end
    assert len(df) == 2 * 96
    assert df["eur_mwh"].notna().all()

    df = fetch_smard(week0, week0 + (end - start), client)
    assert len(df) == 2 * 96 - 1  # the null value is dropped
