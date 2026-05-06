#!/usr/bin/env python
"""EIA incremental update script — fetch only the most recent data.

Designed to run monthly (or on demand) after the initial backfill.
Determines the latest period already in the database for each table and
fetches only data from that point forward.

Usage::

    # Standard incremental update (all tables)
    python scripts/eia_incremental_update.py

    # Southeast states only
    python scripts/eia_incremental_update.py --states NC SC VA GA TN FL

    # Dry-run: show what would be fetched without writing to DB
    python scripts/eia_incremental_update.py --dry-run

"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from duke_rates.config import get_settings
from duke_rates.db.schema import migrate
from duke_rates.eia.client import EIAClient
from duke_rates.eia.endpoints import (
    ALL_STATES,
    GENERATION_FUELS,
    RETAIL_SECTOR_ALL,
    fetch_generation_by_fuel,
    fetch_retail_sales,
    fetch_state_capability,
    fetch_state_profile_summary,
    fetch_state_source_disposition,
)
from duke_rates.eia.loaders import (
    upsert_generation_by_fuel,
    upsert_retail_sales,
    upsert_source_disposition,
    upsert_state_capability,
    upsert_state_profile_summary,
)
from duke_rates.eia.transformers import (
    make_batch_id,
    transform_generation_by_fuel,
    transform_retail_sales,
    transform_source_disposition,
    transform_state_capability,
    transform_state_profile_summary,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eia_incremental")


def _latest_period(conn: sqlite3.Connection, table: str, frequency: str = "annual") -> str | None:
    """Return the latest period string in a table, or None if empty."""
    row = conn.execute(
        f"SELECT MAX(period) FROM {table} WHERE frequency=?", (frequency,)
    ).fetchone()
    return row[0] if row and row[0] else None


def _year_before(period: str | None) -> str | None:
    """Return YYYY-1 to get a one-year overlap buffer, or None."""
    if not period:
        return None
    try:
        yr = int(str(period)[:4])
        return str(yr - 1)
    except ValueError:
        return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--states", nargs="+", default=None,
                   help="Limit to these state codes (default: all 50 + DC + US)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be fetched without writing to DB")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--db", default=None)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()

    db_path = Path(args.db) if args.db else settings.database_path
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    if not settings.eia_api_key:
        log.error("EIA_API_KEY not set")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    migrate(conn)

    client = EIAClient(
        api_key=settings.eia_api_key,
        cache_dir=cache_dir,
        request_delay=settings.eia_request_delay,
    )
    batch_id = make_batch_id()
    states = args.states or ALL_STATES

    # ---- Retail sales (annual) ----
    latest_annual = _latest_period(conn, "eia_retail_sales", "annual")
    start_annual = _year_before(latest_annual) or "2001"
    log.info("retail-sales annual: latest in DB=%s, fetching from %s", latest_annual, start_annual)
    if not args.dry_run:
        raw = fetch_retail_sales(client, states=states, sectors=RETAIL_SECTOR_ALL,
                                  frequency="annual", start=start_annual)
        records = transform_retail_sales(raw, frequency="annual", batch_id=batch_id)
        ins, skp = upsert_retail_sales(conn, records)
        log.info("  -> %d upserted, %d unchanged", ins, skp)

    # ---- Retail sales (monthly) ----
    latest_monthly = _latest_period(conn, "eia_retail_sales", "monthly")
    # Step back 2 months for monthly overlap
    if latest_monthly and len(latest_monthly) == 7:
        yr, mo = int(latest_monthly[:4]), int(latest_monthly[5:7])
        mo -= 2
        if mo <= 0:
            yr -= 1
            mo += 12
        start_monthly = f"{yr}-{mo:02d}"
    else:
        start_monthly = "2001-01"
    log.info("retail-sales monthly: latest=%s, fetching from %s", latest_monthly, start_monthly)
    if not args.dry_run:
        raw_m = fetch_retail_sales(client, states=states, sectors=RETAIL_SECTOR_ALL,
                                    frequency="monthly", start=start_monthly)
        records_m = transform_retail_sales(raw_m, frequency="monthly", batch_id=batch_id)
        ins_m, skp_m = upsert_retail_sales(conn, records_m)
        log.info("  -> %d upserted, %d unchanged", ins_m, skp_m)

    # ---- Generation by fuel (annual) ----
    latest_gen = _latest_period(conn, "eia_generation_by_fuel", "annual")
    start_gen = _year_before(latest_gen) or "2001"
    log.info("generation annual: latest=%s, fetching from %s", latest_gen, start_gen)
    if not args.dry_run:
        gen_states = [s for s in states if s != "US"]
        raw_g = fetch_generation_by_fuel(client, states=gen_states, fuels=GENERATION_FUELS,
                                          sectors=["99"], frequency="annual", start=start_gen)
        records_g = transform_generation_by_fuel(raw_g, frequency="annual", batch_id=batch_id)
        ins_g, skp_g = upsert_generation_by_fuel(conn, records_g)
        log.info("  -> %d upserted, %d unchanged", ins_g, skp_g)

    # ---- State profile summary (annual) ----
    latest_prof = _latest_period(conn, "eia_state_profile_summary", "annual")
    start_prof = _year_before(latest_prof) or "2008"
    log.info("state-profile-summary: latest=%s, fetching from %s", latest_prof, start_prof)
    if not args.dry_run:
        prof_states = [s for s in states if s != "US"]
        raw_p = fetch_state_profile_summary(client, states=prof_states, start=start_prof)
        records_p = transform_state_profile_summary(raw_p, batch_id=batch_id)
        ins_p, skp_p = upsert_state_profile_summary(conn, records_p)
        log.info("  -> %d upserted, %d unchanged", ins_p, skp_p)

    # ---- Capability (annual) ----
    latest_cap = _latest_period(conn, "eia_state_capability", "annual")
    start_cap = _year_before(latest_cap) or "1990"
    log.info("state-capability: latest=%s, fetching from %s", latest_cap, start_cap)
    if not args.dry_run:
        cap_states = [s for s in states if s != "US"]
        raw_c = fetch_state_capability(client, states=cap_states,
                                        energy_sources=["ALL", "NG", "NUC", "HYC", "WND", "SOL", "COL", "PET"],
                                        start=start_cap)
        records_c = transform_state_capability(raw_c, batch_id=batch_id)
        ins_c, skp_c = upsert_state_capability(conn, records_c)
        log.info("  -> %d upserted, %d unchanged", ins_c, skp_c)

    # ---- Source disposition (annual) ----
    latest_disp = _latest_period(conn, "eia_source_disposition", "annual")
    start_disp = _year_before(latest_disp) or "1990"
    log.info("source-disposition: latest=%s, fetching from %s", latest_disp, start_disp)
    if not args.dry_run:
        disp_states = [s for s in states if s != "US"]
        raw_d = fetch_state_source_disposition(client, states=disp_states, start=start_disp)
        records_d = transform_source_disposition(raw_d, batch_id=batch_id)
        ins_d, skp_d = upsert_source_disposition(conn, records_d)
        log.info("  -> %d upserted, %d unchanged", ins_d, skp_d)

    conn.close()
    if args.dry_run:
        log.info("Dry run complete — no data written")
    else:
        log.info("Incremental update complete (batch_id=%s)", batch_id)


if __name__ == "__main__":
    main()
