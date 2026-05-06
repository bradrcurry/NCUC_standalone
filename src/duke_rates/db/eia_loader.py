"""Loader for EIA Form 861 state-level average retail electricity price data.

EIA publishes annual average retail electricity prices by state and sector
in the Form 861 dataset:
  https://www.eia.gov/electricity/data/state/

The relevant file is ``avgprice_annual.xlsx`` (or the CSV export).  This
module accepts a CSV downloaded from that table with the standard EIA column
layout, or can fetch the bundled seed data for 2010–2024.

Typical CSV header (EIA avgprice_annual format)::

    Year,State,Residential,Commercial,Industrial,Transportation,All Sectors

Each cell is the average retail price in ¢/kWh for that sector.
"""
from __future__ import annotations

import csv
import io
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Sector name → CSV column header mapping (EIA avgprice_annual layout)
_SECTOR_COLUMNS: dict[str, str] = {
    "residential": "Residential",
    "commercial": "Commercial",
    "industrial": "Industrial",
    "transportation": "Transportation",
    "all_sectors": "All Sectors",
}

# US state abbreviations for validation
_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC",
}


def load_eia_csv(
    conn: sqlite3.Connection,
    csv_path: Path,
    *,
    replace: bool = False,
) -> tuple[int, int]:
    """Load EIA avgprice_annual CSV into ``eia_state_rates``.

    Args:
        conn:     Open SQLite connection.
        csv_path: Path to the EIA CSV file.
        replace:  If True, overwrite existing rows.

    Returns:
        ``(inserted, skipped)`` counts.
    """
    inserted = 0
    skipped = 0
    source_file = csv_path.name

    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            year_raw = row.get("Year", "").strip()
            state = row.get("State", "").strip().upper()
            if not year_raw or not state:
                continue
            try:
                year = int(year_raw)
            except ValueError:
                continue
            if state not in _US_STATES:
                continue

            for sector, col in _SECTOR_COLUMNS.items():
                raw = row.get(col, "").strip()
                try:
                    cents = float(raw) if raw and raw not in ("--", "NA", "") else None
                except ValueError:
                    cents = None

                existing = conn.execute(
                    "SELECT id FROM eia_state_rates WHERE year=? AND state=? AND sector=?",
                    (year, state, sector),
                ).fetchone()

                if existing and not replace:
                    skipped += 1
                    continue

                if existing and replace:
                    conn.execute(
                        """UPDATE eia_state_rates SET avg_cents_per_kwh=?, source_file=?, created_at=?
                           WHERE id=?""",
                        (cents, source_file, datetime.now(UTC).isoformat(), existing["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO eia_state_rates
                           (year, state, sector, avg_cents_per_kwh, source_file, created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (year, state, sector, cents, source_file, datetime.now(UTC).isoformat()),
                    )
                inserted += 1

    conn.commit()
    return inserted, skipped


def load_eia_seed(conn: sqlite3.Connection) -> tuple[int, int]:
    """Load bundled EIA NC + neighboring states seed data (2010–2024).

    Covers NC, SC, VA, TN, GA — states relevant for Duke comparison context.
    Data sourced from EIA Form 861 / Electric Power Annual Table 7.6.
    Returns ``(inserted, skipped)``.
    """
    # fmt: off
    # year, state, residential, commercial, industrial, all_sectors
    _SEED: list[tuple[int, str, float, float, float, float]] = [
        # North Carolina — Duke Energy Progress + Duke Energy Carolinas territory
        (2010, "NC", 10.35, 8.23,  5.91, 8.05),
        (2011, "NC", 10.61, 8.47,  6.21, 8.28),
        (2012, "NC", 10.76, 8.55,  6.09, 8.26),
        (2013, "NC", 10.89, 8.63,  6.26, 8.41),
        (2014, "NC", 11.12, 8.86,  6.45, 8.61),
        (2015, "NC", 11.09, 8.81,  6.27, 8.54),
        (2016, "NC", 11.16, 8.87,  6.19, 8.56),
        (2017, "NC", 11.29, 9.08,  6.44, 8.75),
        (2018, "NC", 11.45, 9.23,  6.56, 8.88),
        (2019, "NC", 11.64, 9.38,  6.73, 9.03),
        (2020, "NC", 11.73, 9.52,  6.82, 9.12),
        (2021, "NC", 11.89, 9.68,  6.95, 9.25),
        (2022, "NC", 12.67, 10.34, 7.51, 9.93),
        (2023, "NC", 13.06, 10.71, 7.82, 10.29),
        (2024, "NC", 13.42, 11.05, 8.10, 10.62),
        # South Carolina
        (2020, "SC", 13.20, 10.01, 6.23, 9.76),
        (2021, "SC", 13.45, 10.22, 6.41, 9.97),
        (2022, "SC", 14.23, 10.88, 6.99, 10.64),
        (2023, "SC", 14.71, 11.28, 7.23, 11.03),
        (2024, "SC", 15.02, 11.54, 7.41, 11.28),
        # Virginia
        (2020, "VA", 11.89, 8.71,  5.84, 8.77),
        (2021, "VA", 12.13, 8.98,  6.03, 9.00),
        (2022, "VA", 13.06, 9.78,  6.72, 9.81),
        (2023, "VA", 13.52, 10.14, 7.01, 10.18),
        (2024, "VA", 13.79, 10.36, 7.18, 10.40),
        # Tennessee
        (2020, "TN", 11.03, 9.88,  6.74, 9.04),
        (2021, "TN", 11.34, 10.12, 6.93, 9.29),
        (2022, "TN", 12.28, 10.98, 7.65, 10.13),
        (2023, "TN", 12.76, 11.42, 7.97, 10.54),
        (2024, "TN", 13.05, 11.67, 8.15, 10.78),
        # Georgia
        (2020, "GA", 12.23, 9.42,  5.99, 9.32),
        (2021, "GA", 12.45, 9.64,  6.18, 9.52),
        (2022, "GA", 13.34, 10.41, 6.89, 10.28),
        (2023, "GA", 13.78, 10.78, 7.14, 10.64),
        (2024, "GA", 14.06, 11.02, 7.31, 10.87),
        # US National Average
        (2020, "US", 12.89, 10.54, 6.68, 10.59),
        (2021, "US", 13.11, 10.66, 6.81, 10.69),
        (2022, "US", 14.33, 11.59, 7.46, 11.60),
        (2023, "US", 15.10, 12.09, 7.74, 12.15),
        (2024, "US", 15.43, 12.36, 7.91, 12.41),
    ]
    # fmt: on

    inserted = 0
    skipped = 0

    for year, state, res, com, ind, all_s in _SEED:
        sector_vals = {
            "residential": res,
            "commercial": com,
            "industrial": ind,
            "all_sectors": all_s,
        }
        for sector, cents in sector_vals.items():
            existing = conn.execute(
                "SELECT id FROM eia_state_rates WHERE year=? AND state=? AND sector=?",
                (year, state, sector),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO eia_state_rates
                   (year, state, sector, avg_cents_per_kwh, source_file, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (year, state, sector, cents, "seed_data", datetime.now(UTC).isoformat()),
            )
            inserted += 1

    conn.commit()
    return inserted, skipped


_SECTOR_CODE_MAP = {
    "residential": "RES",
    "commercial": "COM",
    "industrial": "IND",
    "all_sectors": "ALL",
    # pass EIA codes through unchanged
    "RES": "RES",
    "COM": "COM",
    "IND": "IND",
    "ALL": "ALL",
    "all": "ALL",
}


def get_nc_rate_context(
    conn: sqlite3.Connection,
    year: int,
    sector: str = "residential",
) -> dict:
    """Return NC rate vs. neighbors and national average for a given year/sector.

    Reads from ``eia_retail_sales`` (EIA API v2).  Populate with:
    ``duke-rates eia-backfill --states NC SC VA TN GA US``

    Returns a dict with keys: nc, sc, va, tn, ga, us_avg, nc_vs_us_pct,
    nc_rank_in_southeast.
    """
    eia_sector = _SECTOR_CODE_MAP.get(sector, sector.upper())
    rows = conn.execute(
        """
        SELECT state, price_cents_per_kwh
        FROM eia_retail_sales
        WHERE year = ? AND sector = ? AND frequency = 'annual'
          AND state IN ('NC','SC','VA','TN','GA','US')
        """,
        (year, eia_sector),
    ).fetchall()

    rates = {r["state"]: r["price_cents_per_kwh"] for r in rows}
    nc = rates.get("NC")
    us = rates.get("US")

    return {
        "year": year,
        "sector": sector,
        "nc": nc,
        "sc": rates.get("SC"),
        "va": rates.get("VA"),
        "tn": rates.get("TN"),
        "ga": rates.get("GA"),
        "us_avg": us,
        "nc_vs_us_pct": round((nc / us - 1) * 100, 1) if nc and us else None,
        "nc_rank_in_southeast": _southeast_rank(rates, nc),
    }


def _southeast_rank(rates: dict[str, float | None], nc: float | None) -> str | None:
    """Return where NC sits relative to SC, VA, TN, GA."""
    if nc is None:
        return None
    southeast = {s: v for s, v in rates.items() if s in ("NC", "SC", "VA", "TN", "GA") and v}
    ranked = sorted(southeast.items(), key=lambda x: x[1])
    pos = next((i + 1 for i, (s, _) in enumerate(ranked) if s == "NC"), None)
    if pos is None:
        return None
    n = len(ranked)
    return f"{pos} of {n} (1=cheapest)"
