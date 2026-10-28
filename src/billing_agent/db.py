"""DuckDB storage: one file holds master data, meter data, prices, invoices and the audit trail.

Timestamps are stored in UTC. A billing month is defined in Europe/Berlin local time, which is
how German energy contracts and invoices are written.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id  VARCHAR PRIMARY KEY,
    name         VARCHAR,
    industry     VARCHAR,
    malo_id      VARCHAR,
    street       VARCHAR,
    postcode     VARCHAR,
    city         VARCHAR
);

CREATE TABLE IF NOT EXISTS contracts (
    contract_id        VARCHAR PRIMARY KEY,
    customer_id        VARCHAR,
    contract_type      VARCHAR,   -- fixed | spot | ppa
    start_date         DATE,
    end_date           DATE,
    fixed_ct_kwh       DECIMAL(10, 4),  -- fixed: energy price
    margin_ct_kwh      DECIMAL(10, 4),  -- spot / ppa residual: supplier margin on top of spot
    ppa_eur_mwh        DECIMAL(10, 2),  -- ppa: price of the PPA volume
    ppa_share          DECIMAL(5, 4)    -- ppa: share of each interval covered by the PPA
);

CREATE TABLE IF NOT EXISTS meter_readings (
    customer_id  VARCHAR,
    ts_utc       TIMESTAMP,   -- start of the 15-minute interval
    kwh          DOUBLE,
    PRIMARY KEY (customer_id, ts_utc)
);

CREATE TABLE IF NOT EXISTS prices (
    ts_utc       TIMESTAMP PRIMARY KEY,  -- start of the price interval
    resolution   VARCHAR,                -- quarterhour | hour
    eur_mwh      DOUBLE
);
"""


def connect(path: Path | str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    con.execute("SET TimeZone = 'UTC'")
    return con
