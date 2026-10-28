"""Synthetic customers, contracts and 15-minute meter data for a demo billing run.

Fifteen fictional German SMEs with load shapes typical for their industry. A few records carry
deliberate data problems (listed in INJECTED_ISSUES) so the checks and the review step have
something real to catch.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd

from billing_agent.malo import with_check_digit
from billing_agent.periods import BERLIN, billing_period, previous_month


@dataclass(frozen=True)
class Spec:
    customer_id: str
    name: str
    industry: str
    city: str
    postcode: str
    street: str
    peak_kw: float
    contract_type: str
    fixed_ct_kwh: float | None = None
    margin_ct_kwh: float | None = None
    ppa_eur_mwh: float | None = None
    ppa_share: float | None = None


SPECS = [
    Spec("C01", "Bäckerei Hoffmann GmbH", "bakery", "Berlin", "10997", "Wiener Str. 12", 180, "fixed", fixed_ct_kwh=11.40),
    Spec("C02", "Kühlhaus Nord GmbH", "cold_storage", "Hamburg", "21107", "Reiherdamm 40", 420, "spot", margin_ct_kwh=1.35),
    Spec("C03", "Backstube Lindner KG", "bakery", "Leipzig", "04109", "Brühl 8", 140, "fixed", fixed_ct_kwh=11.90),
    Spec("C04", "Metallbau Krüger GmbH", "metal_works", "Chemnitz", "09113", "Leipziger Str. 201", 650, "ppa", margin_ct_kwh=1.10, ppa_eur_mwh=68.00, ppa_share=0.45),
    Spec("C05", "Hotel am Kanal", "hotel", "Potsdam", "14467", "Am Kanal 3", 220, "spot", margin_ct_kwh=1.60),
    Spec("C06", "Rechenzentrum Elbe GmbH", "data_room", "Dresden", "01067", "Ostra-Allee 15", 380, "ppa", margin_ct_kwh=0.95, ppa_eur_mwh=66.50, ppa_share=0.60),
    Spec("C07", "Kunststofftechnik Weber GmbH", "plastics", "Magdeburg", "39104", "Otto-von-Guericke-Str. 77", 520, "spot", margin_ct_kwh=1.20),
    Spec("C08", "Kuehlhaus Nord GmbH", "cold_storage", "Hamburg", "21107", "Reiherdamm 40", 410, "fixed", fixed_ct_kwh=10.80),
    Spec("C09", "Autohaus Brandt GmbH", "car_dealership", "Rostock", "18055", "Lange Str. 30", 160, "fixed", fixed_ct_kwh=12.10),
    Spec("C10", "Wäscherei Sauber & Co. KG", "laundry", "Hannover", "30159", "Georgstr. 5", 300, "flex"),
    Spec("C11", "Brauerei Spreewald GmbH", "brewery", "Lübbenau", "03222", "Ehm-Welk-Str. 2", 450, "ppa", margin_ct_kwh=1.15, ppa_eur_mwh=71.00, ppa_share=0.35),
    Spec("C12", "Frischmarkt Schulz e.K.", "supermarket", "Erfurt", "99084", "Anger 50", 260, "spot", margin_ct_kwh=1.45),
    Spec("C13", "Steuerbüro Neumann & Partner", "office", "Berlin", "10117", "Friedrichstr. 95", 90, "fixed", fixed_ct_kwh=12.60),
    Spec("C14", "Gießerei Hartmann GmbH", "metal_works", "Zwickau", "08056", "Werdauer Str. 120", 800, "spot", margin_ct_kwh=0.95),
    Spec("C15", "Landhotel Seeblick", "hotel", "Waren (Müritz)", "17192", "Strandstr. 1", 150, "fixed", fixed_ct_kwh=11.70),
]  # fmt: skip

# What the generator breaks on purpose, and what should catch it.
INJECTED_ISSUES = {
    "C03": "MaLo-ID with a wrong check digit",
    "C05": "3 quarter-hours missing in the billing month",
    "C06": "one negative meter reading",
    "C07": "consumption up ~60% on the prior month",
    "C08": "duplicate of C02: same MaLo-ID under a second customer number",
    "C09": "contract end date before its start date",
    "C10": "unknown contract type 'flex'",
}


def _shape(industry: str, hour: np.ndarray, weekday: np.ndarray) -> np.ndarray:
    """Relative load (0..1) by local hour and weekday (0 = Monday)."""
    sat, sun = weekday == 5, weekday == 6
    workday = weekday < 5
    if industry == "bakery":
        s = np.where((hour >= 1) & (hour < 9), 1.0, np.where((hour >= 9) & (hour < 14), 0.5, 0.2))
        return np.where(sun, s * 0.3, s)
    if industry == "cold_storage":
        return 0.8 + 0.15 * np.sin((hour - 9) / 24 * 2 * np.pi).clip(0)
    if industry == "metal_works":
        s = np.where(workday & (hour >= 6) & (hour < 22), 1.0, 0.15)
        return np.where(sat & (hour >= 6) & (hour < 14), 0.6, s)
    if industry == "hotel":
        return np.where((hour >= 6) & (hour < 10), 0.9, np.where(hour >= 17, 0.85, 0.45))
    if industry == "data_room":
        return np.full(hour.shape, 0.95)
    if industry == "plastics":
        return np.where(workday, 1.0, 0.3)
    if industry == "car_dealership":
        s = np.where(workday & (hour >= 8) & (hour < 19), 0.9, 0.2)
        return np.where(sat & (hour >= 9) & (hour < 16), 0.7, s)
    if industry == "laundry":
        return np.where(~sun & (hour >= 6) & (hour < 20), 1.0, 0.1)
    if industry == "brewery":
        return np.where(workday & (hour >= 5) & (hour < 17), 1.0, 0.35)
    if industry == "supermarket":
        return np.where(~sun & (hour >= 7) & (hour < 21), 1.0, 0.55)
    if industry == "office":
        return np.where(workday & (hour >= 8) & (hour < 18), 1.0, np.where(workday, 0.25, 0.2))
    raise ValueError(f"unknown industry {industry!r}")


def _readings(spec: Spec, start_utc, end_utc, rng: np.random.Generator) -> pd.DataFrame:
    ts = pd.date_range(start_utc, end_utc, freq="15min", inclusive="left")
    local = ts.tz_localize("UTC").tz_convert(BERLIN)
    shape = _shape(spec.industry, local.hour.to_numpy(), local.day_of_week.to_numpy())
    noise = rng.lognormal(mean=0.0, sigma=0.06, size=len(ts))
    kwh = spec.peak_kw * 0.25 * shape * noise
    return pd.DataFrame({"customer_id": spec.customer_id, "ts_utc": ts, "kwh": kwh.round(3)})


def _malo_ids(rng: np.random.Generator) -> dict[str, str]:
    ids = {}
    for spec in SPECS:
        first_ten = str(rng.integers(4, 10)) + "".join(str(d) for d in rng.integers(0, 10, 9))
        ids[spec.customer_id] = with_check_digit(first_ten)
    bad = ids["C03"]
    ids["C03"] = bad[:10] + str((int(bad[10]) + 3) % 10)
    ids["C08"] = ids["C02"]
    return ids


def generate(con: duckdb.DuckDBPyConnection, month: str, seed: int = 7) -> dict[str, int]:
    """Write customers, contracts and meter data for `month` and the month before it."""
    rng = np.random.default_rng(seed)
    current, prior = billing_period(month), billing_period(previous_month(month))
    malo = _malo_ids(rng)

    customers = pd.DataFrame(
        [
            (s.customer_id, s.name, s.industry, malo[s.customer_id], s.street, s.postcode, s.city)
            for s in SPECS
        ],
        columns=["customer_id", "name", "industry", "malo_id", "street", "postcode", "city"],
    )

    contract_start = pd.Timestamp("2026-01-01").date()
    contract_end = pd.Timestamp("2027-12-31").date()
    contracts = pd.DataFrame(
        [
            (
                f"K-{s.customer_id}",
                s.customer_id,
                s.contract_type,
                contract_start,
                pd.Timestamp("2025-12-31").date() if s.customer_id == "C09" else contract_end,
                s.fixed_ct_kwh,
                s.margin_ct_kwh,
                s.ppa_eur_mwh,
                s.ppa_share,
            )
            for s in SPECS
        ],
        columns=[
            "contract_id",
            "customer_id",
            "contract_type",
            "start_date",
            "end_date",
            "fixed_ct_kwh",
            "margin_ct_kwh",
            "ppa_eur_mwh",
            "ppa_share",
        ],
    )

    frames = []
    for spec in SPECS:
        df = _readings(spec, prior.start_utc, current.end_utc, rng)
        in_current = df["ts_utc"] >= current.start_utc
        if spec.customer_id == "C07":
            df.loc[in_current, "kwh"] = (df.loc[in_current, "kwh"] * 1.6).round(3)
        if spec.customer_id == "C05":
            gap = df.index[in_current][1000:1003]
            df = df.drop(gap)
        if spec.customer_id == "C06":
            df.loc[df.index[in_current][2400], "kwh"] = -12.5
        frames.append(df)
    readings = pd.concat(frames, ignore_index=True)

    for table in ("meter_readings", "contracts", "customers"):
        con.execute(f"DELETE FROM {table}")
    con.register("customers_df", customers)
    con.register("contracts_df", contracts)
    con.register("readings_df", readings)
    con.execute("INSERT INTO customers SELECT * FROM customers_df")
    con.execute("INSERT INTO contracts SELECT * FROM contracts_df")
    con.execute("INSERT INTO meter_readings SELECT * FROM readings_df")
    for name in ("customers_df", "contracts_df", "readings_df"):
        con.unregister(name)
    return {"customers": len(customers), "contracts": len(contracts), "readings": len(readings)}
