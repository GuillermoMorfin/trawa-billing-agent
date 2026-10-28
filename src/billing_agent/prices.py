"""Day-ahead electricity prices for the DE-LU bidding zone from SMARD (Bundesnetzagentur).

SMARD publishes one JSON file per week and resolution; an index file lists the week start
timestamps. Filter 4169 is the DE-LU day-ahead price in EUR/MWh. Since October 2025 the
day-ahead market clears in quarter-hours, so meter data and prices line up 1:1.

A snapshot CSV ships with the package so the demo runs offline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import duckdb
import httpx
import pandas as pd

from billing_agent.periods import QUARTER_HOUR, billing_period

BASE_URL = "https://www.smard.de/app/chart_data"
FILTER_DAY_AHEAD_DE_LU = 4169
REGION = "DE"
RESOLUTION = "quarterhour"
WEEK = timedelta(days=7)
SNAPSHOT = "day_ahead_de_lu.csv"


def _ms(ts: datetime) -> int:
    return int(ts.replace(tzinfo=UTC).timestamp() * 1000)


def fetch_smard(start_utc: datetime, end_utc: datetime, client: httpx.Client) -> pd.DataFrame:
    """Download quarter-hour prices covering [start_utc, end_utc)."""
    folder = f"{BASE_URL}/{FILTER_DAY_AHEAD_DE_LU}/{REGION}"
    index = client.get(f"{folder}/index_{RESOLUTION}.json").raise_for_status().json()
    lo, hi = _ms(start_utc - WEEK), _ms(end_utc)
    weeks = [t for t in index["timestamps"] if lo < t < hi]

    rows = []
    for t in weeks:
        url = f"{folder}/{FILTER_DAY_AHEAD_DE_LU}_{REGION}_{RESOLUTION}_{t}.json"
        series = client.get(url).raise_for_status().json()["series"]
        rows.extend((ts, value) for ts, value in series if value is not None)

    df = pd.DataFrame(rows, columns=["ts_ms", "eur_mwh"])
    df["ts_utc"] = pd.to_datetime(df["ts_ms"], unit="ms")
    df = df[(df["ts_utc"] >= start_utc) & (df["ts_utc"] < end_utc)]
    return df[["ts_utc", "eur_mwh"]].drop_duplicates("ts_utc").sort_values("ts_utc")


def load_snapshot() -> pd.DataFrame:
    with resources.files("billing_agent").joinpath(f"snapshots/{SNAPSHOT}").open() as f:
        df = pd.read_csv(f, parse_dates=["ts_utc"])
    return df


def save_snapshot(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, date_format="%Y-%m-%dT%H:%M:%S")


def store(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    df = df.assign(resolution=RESOLUTION)[["ts_utc", "resolution", "eur_mwh"]]
    con.register("prices_df", df)
    con.execute("INSERT OR REPLACE INTO prices SELECT * FROM prices_df")
    con.unregister("prices_df")
    return len(df)


def load_prices(con: duckdb.DuckDBPyConnection, month: str, source: str = "snapshot") -> int:
    """Put prices for `month` into the database from the snapshot or live from SMARD."""
    period = billing_period(month)
    if source == "smard":
        with httpx.Client(timeout=30, headers={"User-Agent": "trawa-billing-agent"}) as client:
            df = fetch_smard(period.start_utc, period.end_utc, client)
    else:
        df = load_snapshot()
        df = df[(df["ts_utc"] >= period.start_utc) & (df["ts_utc"] < period.end_utc)]
    if len(df) < period.expected_intervals:
        raise ValueError(
            f"{source} has {len(df)} of {period.expected_intervals} quarter-hour prices for "
            f"{month}; try --source smard or another month"
        )
    return store(con, df)


def missing_price_intervals(con: duckdb.DuckDBPyConnection, month: str) -> int:
    period = billing_period(month)
    (n,) = con.execute(
        "SELECT count(*) FROM prices WHERE ts_utc >= ? AND ts_utc < ?",
        [period.start_utc, period.end_utc],
    ).fetchone()
    return int((period.end_utc - period.start_utc) / QUARTER_HOUR) - n
