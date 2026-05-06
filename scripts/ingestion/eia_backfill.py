#!/usr/bin/env python
"""EIA historical backfill script — fetch all available history for all states.

Run once to populate the EIA tables from scratch.  Safe to re-run; all
upsert operations are idempotent.

Usage::

    # Full 50-state backfill with all datasets (takes 5-15 minutes)
    python scripts/eia_backfill.py

    # Faster: Southeast states only
    python scripts/eia_backfill.py --states NC SC VA GA TN FL AL MS KY WV

    # Skip generation data (faster; just retail sales + profiles)
    python scripts/eia_backfill.py --skip-generation

    # Use a local cache directory to avoid re-fetching on reruns
    python scripts/eia_backfill.py --cache-dir data/eia_cache

Environment variables required::

    EIA_API_KEY   — registered EIA API key

"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# ---- ensure src/ is importable when run from project root ----
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
    upsert_state_capability,
    upsert_state_profile_summary,
    upsert_source_disposition,
)
from duke_rates.eia.references import seed_market_structure_lookup, seed_state_region_lookup
from duke_rates.eia.transformers import (
    make_batch_id,
    transform_generation_by_fuel,
    transform_retail_sales,
    transform_state_capability,
    transform_state_profile_summary,
    transform_source_disposition,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eia_backfill")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--states", nargs="+", default=None,
                   help="Limit to these state codes (default: all 50 + DC + US)")
    p.add_argument("--start", default="2001",
                   help="Start year for retail-sales and generation (default: 2001)")
    p.add_argument("--end", default=None,
                   help="End year (default: most recent available)")
    p.add_argument("--skip-generation", action="store_true",
                   help="Skip generation-by-fuel fetch (faster)")
    p.add_argument("--skip-monthly-generation", action="store_true",
                   help="Skip monthly generation-by-fuel fetch (large dataset, key fuels only)")
    p.add_argument("--skip-profiles", action="store_true",
                   help="Skip state-profile-summary fetch")
    p.add_argument("--skip-capability", action="store_true",
                   help="Skip state-capability fetch")
    p.add_argument("--skip-disposition", action="store_true",
                   help="Skip source-disposition fetch")
    p.add_argument("--cache-dir", default=None,
                   help="Directory for local JSON response caching")
    p.add_argument("--db", default=None,
                   help="Path to SQLite database (default: from settings)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()

    db_path = Path(args.db) if args.db else settings.database_path
    cache_dir = Path(args.cache_dir) if args.cache_dir else settings.eia_cache_dir

    if not settings.eia_api_key:
        log.error("EIA_API_KEY not set in environment or .env file")
        sys.exit(1)

    log.info("Database: %s", db_path)
    log.info("Cache dir: %s", cache_dir)

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

    log.info("Batch ID: %s  States: %d  Start: %s  End: %s",
             batch_id, len(states), args.start, args.end or "latest")

    # ----------------------------------------------------------------
    # 1. Seed static reference tables (idempotent, fast)
    # ----------------------------------------------------------------
    log.info("Seeding reference tables...")
    ri, rs = seed_state_region_lookup(conn)
    log.info("  eia_state_region_lookup: %d inserted, %d skipped", ri, rs)
    mi, ms = seed_market_structure_lookup(conn)
    log.info("  eia_market_structure_lookup: %d inserted, %d skipped", mi, ms)

    # ----------------------------------------------------------------
    # 2. Retail sales — annual (all sectors, all states, full history)
    # ----------------------------------------------------------------
    log.info("Fetching retail sales (annual, all sectors)...")
    raw = fetch_retail_sales(
        client,
        states=states,
        sectors=RETAIL_SECTOR_ALL,
        frequency="annual",
        start=args.start,
        end=args.end,
    )
    records = transform_retail_sales(raw, frequency="annual", batch_id=batch_id)
    ins, skp = upsert_retail_sales(conn, records)
    log.info("  eia_retail_sales (annual): %d upserted, %d skipped", ins, skp)

    # ----------------------------------------------------------------
    # 3. Retail sales — monthly (all sectors, all states)
    # ----------------------------------------------------------------
    log.info("Fetching retail sales (monthly, all sectors)...")
    raw_m = fetch_retail_sales(
        client,
        states=states,
        sectors=RETAIL_SECTOR_ALL,
        frequency="monthly",
        start=args.start,
        end=args.end,
    )
    records_m = transform_retail_sales(raw_m, frequency="monthly", batch_id=batch_id)
    ins_m, skp_m = upsert_retail_sales(conn, records_m)
    log.info("  eia_retail_sales (monthly): %d upserted, %d skipped", ins_m, skp_m)

    # ----------------------------------------------------------------
    # 4. Generation by fuel (annual, all sectors, key fuels)
    # ----------------------------------------------------------------
    if not args.skip_generation:
        log.info("Fetching generation by fuel (annual, all fuels)...")
        raw_g = fetch_generation_by_fuel(
            client,
            states=states,
            fuels=GENERATION_FUELS,
            sectors=["99"],  # all sectors
            frequency="annual",
            start=args.start,
            end=args.end,
        )
        records_g = transform_generation_by_fuel(raw_g, frequency="annual", batch_id=batch_id)
        ins_g, skp_g = upsert_generation_by_fuel(conn, records_g)
        log.info("  eia_generation_by_fuel (annual): %d upserted, %d skipped", ins_g, skp_g)

        # ----------------------------------------------------------------
        # 4b. Generation by fuel — monthly (key fuels for seasonal analysis)
        # ----------------------------------------------------------------
        # Monthly generation is large (~100k+ records for all states × fuels).
        # Default: fetch only the analytically critical fuels (NG, NUC, WND, SUN,
        # COW, HYC) to keep the dataset manageable.  Use --skip-monthly-generation
        # to bypass entirely.
        if not args.skip_monthly_generation:
            MONTHLY_GENERATION_FUELS = ["ALL", "NG", "NUC", "WND", "SUN", "COW", "HYC"]
            log.info("Fetching generation by fuel (monthly, key fuels: %s)...", MONTHLY_GENERATION_FUELS)
            raw_gm = fetch_generation_by_fuel(
                client,
                states=states,
                fuels=MONTHLY_GENERATION_FUELS,
                sectors=["99"],
                frequency="monthly",
                start=args.start,
                end=args.end,
            )
            records_gm = transform_generation_by_fuel(raw_gm, frequency="monthly", batch_id=batch_id)
            ins_gm, skp_gm = upsert_generation_by_fuel(conn, records_gm)
            log.info("  eia_generation_by_fuel (monthly): %d upserted, %d skipped", ins_gm, skp_gm)
        else:
            log.info("Skipping monthly generation fetch (--skip-monthly-generation)")
    else:
        log.info("Skipping generation fetch (--skip-generation)")

    # ----------------------------------------------------------------
    # 5. State profile summary (annual, 2008+)
    # ----------------------------------------------------------------
    if not args.skip_profiles:
        log.info("Fetching state profile summary (annual, 2008+)...")
        # Profile summary only has 50 states + DC (no US aggregate)
        profile_states = [s for s in states if s != "US"]
        raw_p = fetch_state_profile_summary(
            client,
            states=profile_states,
            start=max(args.start, "2008") if args.start else "2008",
            end=args.end,
        )
        records_p = transform_state_profile_summary(raw_p, batch_id=batch_id)
        ins_p, skp_p = upsert_state_profile_summary(conn, records_p)
        log.info("  eia_state_profile_summary: %d upserted, %d skipped", ins_p, skp_p)
    else:
        log.info("Skipping profile summary (--skip-profiles)")

    # ----------------------------------------------------------------
    # 6. Source disposition (annual, 1990+)
    # ----------------------------------------------------------------
    if not args.skip_disposition:
        log.info("Fetching source-disposition (annual, 1990+)...")
        disp_states = [s for s in states if s != "US"]
        raw_d = fetch_state_source_disposition(
            client,
            states=disp_states,
            start="1990",
            end=args.end,
        )
        records_d = transform_source_disposition(raw_d, batch_id=batch_id)
        ins_d, skp_d = upsert_source_disposition(conn, records_d)
        log.info("  eia_source_disposition: %d upserted, %d skipped", ins_d, skp_d)
    else:
        log.info("Skipping source-disposition (--skip-disposition)")

    # ----------------------------------------------------------------
    # 7. State capability (annual, 1990+, all energy sources)
    # ----------------------------------------------------------------
    if not args.skip_capability:
        log.info("Fetching state capability (annual, 1990+)...")
        cap_states = [s for s in states if s != "US"]
        raw_c = fetch_state_capability(
            client,
            states=cap_states,
            energy_sources=["ALL", "NG", "NUC", "HYC", "WND", "SOL", "COL", "PET"],
            start="1990",
            end=args.end,
        )
        records_c = transform_state_capability(raw_c, batch_id=batch_id)
        ins_c, skp_c = upsert_state_capability(conn, records_c)
        log.info("  eia_state_capability: %d upserted, %d skipped", ins_c, skp_c)
    else:
        log.info("Skipping capability (--skip-capability)")

    conn.close()
    log.info("Backfill complete (batch_id=%s)", batch_id)


if __name__ == "__main__":
    main()
