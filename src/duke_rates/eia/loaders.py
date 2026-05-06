"""EIA data loaders — idempotent SQLite upsert functions.

Each function accepts a list of normalized dicts (from transformers.py) and an
open SQLite connection, and inserts or updates rows in the corresponding EIA
table.  All operations are idempotent: re-running with the same data is safe.

Natural keys (used for deduplication):
    eia_retail_sales:            (period, state, sector, frequency)
    eia_generation_by_fuel:      (period, state, sector, fuel_type, frequency)
    eia_state_profile_summary:   (period, state)
    eia_source_disposition:      (period, state)
    eia_state_capability:        (period, state, energy_source, producer_type)
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# eia_retail_sales
# ---------------------------------------------------------------------------

def upsert_retail_sales(
    conn: sqlite3.Connection,
    records: list[dict],
) -> tuple[int, int]:
    """Upsert normalized retail-sales records into ``eia_retail_sales``.

    Returns ``(inserted_or_updated, skipped)`` counts.
    """
    inserted = 0
    skipped = 0

    for r in records:
        key = (r["period"], r["state"], r["sector"], r["frequency"])
        existing = conn.execute(
            "SELECT id FROM eia_retail_sales WHERE period=? AND state=? AND sector=? AND frequency=?",
            key,
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE eia_retail_sales SET
                    year=?, month=?, state_name=?, sector_name=?,
                    sales_million_kwh=?, revenue_million_dollars=?,
                    price_cents_per_kwh=?, customers=?,
                    batch_id=?, ingested_at=?
                WHERE id=?
                """,
                (
                    r["year"], r["month"], r["state_name"], r["sector_name"],
                    r["sales_million_kwh"], r["revenue_million_dollars"],
                    r["price_cents_per_kwh"], r["customers"],
                    r["batch_id"], r["ingested_at"],
                    existing["id"],
                ),
            )
            skipped += 1
        else:
            conn.execute(
                """
                INSERT INTO eia_retail_sales (
                    dataset, frequency, period, year, month,
                    state, state_name, sector, sector_name,
                    sales_million_kwh, revenue_million_dollars,
                    price_cents_per_kwh, customers,
                    batch_id, ingested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["dataset"], r["frequency"], r["period"], r["year"], r["month"],
                    r["state"], r["state_name"], r["sector"], r["sector_name"],
                    r["sales_million_kwh"], r["revenue_million_dollars"],
                    r["price_cents_per_kwh"], r["customers"],
                    r["batch_id"], r["ingested_at"],
                ),
            )
            inserted += 1

    conn.commit()
    log.info("eia_retail_sales: %d upserted, %d skipped", inserted, skipped)
    return inserted, skipped


# ---------------------------------------------------------------------------
# eia_generation_by_fuel
# ---------------------------------------------------------------------------

def upsert_generation_by_fuel(
    conn: sqlite3.Connection,
    records: list[dict],
) -> tuple[int, int]:
    """Upsert normalized generation records into ``eia_generation_by_fuel``."""
    inserted = 0
    skipped = 0

    for r in records:
        key = (r["period"], r["state"], r["sector"], r["fuel_type"], r["frequency"])
        existing = conn.execute(
            """
            SELECT id FROM eia_generation_by_fuel
            WHERE period=? AND state=? AND sector=? AND fuel_type=? AND frequency=?
            """,
            key,
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE eia_generation_by_fuel SET
                    year=?, month=?, fuel_type_name=?,
                    generation_thousand_mwh=?, generation_mwh=?,
                    batch_id=?, ingested_at=?
                WHERE id=?
                """,
                (
                    r["year"], r["month"], r["fuel_type_name"],
                    r["generation_thousand_mwh"], r["generation_mwh"],
                    r["batch_id"], r["ingested_at"],
                    existing["id"],
                ),
            )
            skipped += 1
        else:
            conn.execute(
                """
                INSERT INTO eia_generation_by_fuel (
                    dataset, frequency, period, year, month,
                    state, sector, fuel_type, fuel_type_name,
                    generation_thousand_mwh, generation_mwh,
                    batch_id, ingested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["dataset"], r["frequency"], r["period"], r["year"], r["month"],
                    r["state"], r["sector"], r["fuel_type"], r["fuel_type_name"],
                    r["generation_thousand_mwh"], r["generation_mwh"],
                    r["batch_id"], r["ingested_at"],
                ),
            )
            inserted += 1

    conn.commit()
    log.info("eia_generation_by_fuel: %d upserted, %d skipped", inserted, skipped)
    return inserted, skipped


# ---------------------------------------------------------------------------
# eia_state_profile_summary
# ---------------------------------------------------------------------------

def upsert_state_profile_summary(
    conn: sqlite3.Connection,
    records: list[dict],
) -> tuple[int, int]:
    """Upsert normalized state-profile-summary records."""
    inserted = 0
    skipped = 0

    for r in records:
        existing = conn.execute(
            "SELECT id FROM eia_state_profile_summary WHERE period=? AND state=?",
            (r["period"], r["state"]),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE eia_state_profile_summary SET
                    year=?, state_name=?,
                    net_summer_capacity_mw=?, net_generation_mwh=?,
                    total_retail_sales_mwh=?, average_retail_price_cents_per_kwh=?,
                    co2_thousand_metric_tons=?,
                    net_summer_capacity_rank=?, net_generation_rank=?,
                    total_retail_sales_rank=?, average_retail_price_rank=?,
                    batch_id=?, ingested_at=?
                WHERE id=?
                """,
                (
                    r["year"], r["state_name"],
                    r["net_summer_capacity_mw"], r["net_generation_mwh"],
                    r["total_retail_sales_mwh"], r["average_retail_price_cents_per_kwh"],
                    r["co2_thousand_metric_tons"],
                    r["net_summer_capacity_rank"], r["net_generation_rank"],
                    r["total_retail_sales_rank"], r["average_retail_price_rank"],
                    r["batch_id"], r["ingested_at"],
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO eia_state_profile_summary (
                    dataset, frequency, period, year, state, state_name,
                    net_summer_capacity_mw, net_generation_mwh,
                    total_retail_sales_mwh, average_retail_price_cents_per_kwh,
                    co2_thousand_metric_tons,
                    net_summer_capacity_rank, net_generation_rank,
                    total_retail_sales_rank, average_retail_price_rank,
                    batch_id, ingested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["dataset"], r["frequency"], r["period"], r["year"], r["state"], r["state_name"],
                    r["net_summer_capacity_mw"], r["net_generation_mwh"],
                    r["total_retail_sales_mwh"], r["average_retail_price_cents_per_kwh"],
                    r["co2_thousand_metric_tons"],
                    r["net_summer_capacity_rank"], r["net_generation_rank"],
                    r["total_retail_sales_rank"], r["average_retail_price_rank"],
                    r["batch_id"], r["ingested_at"],
                ),
            )
        inserted += 1

    conn.commit()
    log.info("eia_state_profile_summary: %d upserted", inserted)
    return inserted, skipped


# ---------------------------------------------------------------------------
# eia_source_disposition
# ---------------------------------------------------------------------------

def upsert_source_disposition(
    conn: sqlite3.Connection,
    records: list[dict],
) -> tuple[int, int]:
    """Upsert normalized source-disposition records."""
    inserted = 0
    skipped = 0

    for r in records:
        existing = conn.execute(
            "SELECT id FROM eia_source_disposition WHERE period=? AND state=?",
            (r["period"], r["state"]),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE eia_source_disposition SET
                    year=?, state_name=?,
                    total_net_generation_mwh=?, total_supply_mwh=?,
                    retail_sales_mwh=?, net_interstate_trade_mwh=?,
                    estimated_losses_mwh=?, direct_use_mwh=?,
                    batch_id=?, ingested_at=?
                WHERE id=?
                """,
                (
                    r["year"], r["state_name"],
                    r["total_net_generation_mwh"], r["total_supply_mwh"],
                    r["retail_sales_mwh"], r["net_interstate_trade_mwh"],
                    r["estimated_losses_mwh"], r["direct_use_mwh"],
                    r["batch_id"], r["ingested_at"],
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO eia_source_disposition (
                    dataset, frequency, period, year, state, state_name,
                    total_net_generation_mwh, total_supply_mwh, retail_sales_mwh,
                    net_interstate_trade_mwh, estimated_losses_mwh, direct_use_mwh,
                    batch_id, ingested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["dataset"], r["frequency"], r["period"], r["year"], r["state"], r["state_name"],
                    r["total_net_generation_mwh"], r["total_supply_mwh"], r["retail_sales_mwh"],
                    r["net_interstate_trade_mwh"], r["estimated_losses_mwh"], r["direct_use_mwh"],
                    r["batch_id"], r["ingested_at"],
                ),
            )
        inserted += 1

    conn.commit()
    log.info("eia_source_disposition: %d upserted", inserted)
    return inserted, skipped


# ---------------------------------------------------------------------------
# eia_state_capability
# ---------------------------------------------------------------------------

def upsert_state_capability(
    conn: sqlite3.Connection,
    records: list[dict],
) -> tuple[int, int]:
    """Upsert normalized state-capability records."""
    inserted = 0
    skipped = 0

    for r in records:
        key = (r["period"], r["state"], r["energy_source"], r["producer_type"])
        existing = conn.execute(
            """
            SELECT id FROM eia_state_capability
            WHERE period=? AND state=? AND energy_source=? AND producer_type=?
            """,
            key,
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE eia_state_capability SET
                    year=?, net_summer_capacity_mw=?,
                    batch_id=?, ingested_at=?
                WHERE id=?
                """,
                (
                    r["year"], r["net_summer_capacity_mw"],
                    r["batch_id"], r["ingested_at"],
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO eia_state_capability (
                    dataset, frequency, period, year, state,
                    energy_source, producer_type, net_summer_capacity_mw,
                    batch_id, ingested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["dataset"], r["frequency"], r["period"], r["year"], r["state"],
                    r["energy_source"], r["producer_type"], r["net_summer_capacity_mw"],
                    r["batch_id"], r["ingested_at"],
                ),
            )
        inserted += 1

    conn.commit()
    log.info("eia_state_capability: %d upserted", inserted)
    return inserted, skipped
