"""EIA API response transformers.

Each function takes a list of raw EIA record dicts (as returned by the client)
and returns a list of normalized Python dicts ready for database insertion.

Normalization responsibilities:
- Cast all numeric string values to float/int (EIA returns everything as strings)
- Standardize field names to snake_case
- Add dataset_name, frequency, and ingestion_batch_id metadata
- Handle missing/null indicators ("--", "", null)
- Normalize period strings to ISO format where possible
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


def _float_or_none(v: Any) -> float | None:
    """Convert EIA string value to float, returning None for missing markers."""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "--", "NA", "N/A", "Not Available"):
        return None
    try:
        return float(s)
    except ValueError:
        log.debug("EIA: could not cast %r to float", s)
        return None


def _int_or_none(v: Any) -> int | None:
    f = _float_or_none(v)
    return int(f) if f is not None else None


def _normalize_period(period: str, frequency: str) -> tuple[str, int | None, int | None]:
    """Return (period_str, year, month) from an EIA period string.

    Examples:
        "2024"      annual  -> ("2024",      2024, None)
        "2024-06"   monthly -> ("2024-06",   2024, 6)
        "2024-Q2"   quarterly -> ("2024-Q2", 2024, None)
    """
    period = str(period).strip()
    year: int | None = None
    month: int | None = None

    if re.match(r"^\d{4}$", period):
        year = int(period)
    elif re.match(r"^\d{4}-\d{2}$", period):
        year = int(period[:4])
        month = int(period[5:7])
    elif re.match(r"^\d{4}-Q\d$", period):
        year = int(period[:4])

    return period, year, month


def transform_retail_sales(
    records: list[dict],
    *,
    frequency: str = "annual",
    batch_id: str | None = None,
    ingested_at: str | None = None,
) -> list[dict]:
    """Normalize raw retail-sales records.

    Input fields (from EIA):
        period, stateid, stateDescription, sectorid, sectorName,
        sales, revenue, price, customers

    Output fields:
        dataset, frequency, period, year, month,
        state, state_name, sector, sector_name,
        sales_million_kwh, revenue_million_dollars, price_cents_per_kwh,
        customers, batch_id, ingested_at
    """
    now = ingested_at or datetime.now(UTC).isoformat()
    out = []
    for r in records:
        period, year, month = _normalize_period(r.get("period", ""), frequency)
        out.append({
            "dataset": "retail-sales",
            "frequency": frequency,
            "period": period,
            "year": year,
            "month": month,
            "state": str(r.get("stateid", "")).upper(),
            "state_name": r.get("stateDescription"),
            "sector": str(r.get("sectorid", "")).upper(),
            "sector_name": r.get("sectorName"),
            "sales_million_kwh": _float_or_none(r.get("sales")),
            "revenue_million_dollars": _float_or_none(r.get("revenue")),
            "price_cents_per_kwh": _float_or_none(r.get("price")),
            "customers": _int_or_none(r.get("customers")),
            "batch_id": batch_id,
            "ingested_at": now,
        })
    return out


def transform_generation_by_fuel(
    records: list[dict],
    *,
    frequency: str = "annual",
    batch_id: str | None = None,
    ingested_at: str | None = None,
) -> list[dict]:
    """Normalize raw electric-power-operational-data generation records.

    Input fields (from EIA):
        period, location, stateDescription (may be absent), sectorid,
        sectorDescription (may be absent), fueltypeid, fuelTypeDescription,
        generation

    Output fields:
        dataset, frequency, period, year, month,
        state, sector, fuel_type, fuel_type_name,
        generation_thousand_mwh, generation_mwh (derived),
        batch_id, ingested_at
    """
    now = ingested_at or datetime.now(UTC).isoformat()
    out = []
    for r in records:
        period, year, month = _normalize_period(r.get("period", ""), frequency)
        gen_tmwh = _float_or_none(r.get("generation"))
        out.append({
            "dataset": "generation-by-fuel",
            "frequency": frequency,
            "period": period,
            "year": year,
            "month": month,
            "state": str(r.get("location", "")).upper(),
            "sector": str(r.get("sectorid", "")),
            "fuel_type": str(r.get("fueltypeid", "")).upper(),
            "fuel_type_name": r.get("fuelTypeDescription") or r.get("type-name"),
            "generation_thousand_mwh": gen_tmwh,
            # Derived convenience field (MWh)
            "generation_mwh": gen_tmwh * 1000 if gen_tmwh is not None else None,
            "batch_id": batch_id,
            "ingested_at": now,
        })
    return out


def transform_state_profile_summary(
    records: list[dict],
    *,
    batch_id: str | None = None,
    ingested_at: str | None = None,
) -> list[dict]:
    """Normalize raw state-electricity-profiles/summary records.

    Coverage: 2008–present, annual only.

    Input key fields:
        period, stateId (or stateID), stateDescription,
        net-summer-capacity, net-generation, total-retail-sales,
        average-retail-price, carbon-dioxide,
        net-summer-capacity-rank, average-retail-price-rank, etc.

    Output fields:
        dataset, frequency, period, year, state, state_name,
        net_summer_capacity_mw, net_generation_mwh,
        total_retail_sales_mwh, average_retail_price_cents_per_kwh,
        co2_thousand_metric_tons, avg_price_rank,
        batch_id, ingested_at
    """
    now = ingested_at or datetime.now(UTC).isoformat()
    out = []
    for r in records:
        period, year, _ = _normalize_period(r.get("period", ""), "annual")
        # stateID capitalization varies across records
        state = (r.get("stateId") or r.get("stateID") or r.get("stateid") or "").upper()
        out.append({
            "dataset": "state-profile-summary",
            "frequency": "annual",
            "period": period,
            "year": year,
            "state": state,
            "state_name": r.get("stateDescription"),
            "net_summer_capacity_mw": _float_or_none(r.get("net-summer-capacity")),
            "net_generation_mwh": _float_or_none(r.get("net-generation")),
            "total_retail_sales_mwh": _float_or_none(r.get("total-retail-sales")),
            "average_retail_price_cents_per_kwh": _float_or_none(r.get("average-retail-price")),
            "co2_thousand_metric_tons": _float_or_none(r.get("carbon-dioxide")),
            "net_summer_capacity_rank": _int_or_none(r.get("net-summer-capacity-rank")),
            "net_generation_rank": _int_or_none(r.get("net-generation-rank")),
            "total_retail_sales_rank": _int_or_none(r.get("total-retail-sales-rank")),
            "average_retail_price_rank": _int_or_none(r.get("average-retail-price-rank")),
            "batch_id": batch_id,
            "ingested_at": now,
        })
    return out


def transform_source_disposition(
    records: list[dict],
    *,
    batch_id: str | None = None,
    ingested_at: str | None = None,
) -> list[dict]:
    """Normalize state-electricity-profiles/source-disposition records.

    Coverage: 1990–present, annual only.  Units: MWh.

    Output fields:
        dataset, frequency, period, year, state,
        total_net_generation_mwh, total_supply_mwh, retail_sales_mwh,
        net_interstate_trade_mwh, estimated_losses_mwh, direct_use_mwh,
        batch_id, ingested_at
    """
    now = ingested_at or datetime.now(UTC).isoformat()
    out = []
    for r in records:
        period, year, _ = _normalize_period(r.get("period", ""), "annual")
        state = str(r.get("state", "") or r.get("stateid", "")).upper()
        out.append({
            "dataset": "source-disposition",
            "frequency": "annual",
            "period": period,
            "year": year,
            "state": state,
            "state_name": r.get("stateDescription"),
            "total_net_generation_mwh": _float_or_none(r.get("total-net-generation")),
            "total_supply_mwh": _float_or_none(r.get("total-supply")),
            "retail_sales_mwh": _float_or_none(r.get("total-elect-indust")),
            "net_interstate_trade_mwh": _float_or_none(r.get("net-interstate-trade")),
            "estimated_losses_mwh": _float_or_none(r.get("estimated-losses")),
            "direct_use_mwh": _float_or_none(r.get("direct-use")),
            "batch_id": batch_id,
            "ingested_at": now,
        })
    return out


def transform_state_capability(
    records: list[dict],
    *,
    batch_id: str | None = None,
    ingested_at: str | None = None,
) -> list[dict]:
    """Normalize state-electricity-profiles/capability records.

    Coverage: 1990–present, annual only.  Units: MW.

    Output fields:
        dataset, frequency, period, year, state, energy_source, producer_type,
        net_summer_capacity_mw, batch_id, ingested_at
    """
    now = ingested_at or datetime.now(UTC).isoformat()
    out = []
    for r in records:
        period, year, _ = _normalize_period(r.get("period", ""), "annual")
        state = str(r.get("stateId") or r.get("stateid") or r.get("state") or "").upper()
        out.append({
            "dataset": "capability",
            "frequency": "annual",
            "period": period,
            "year": year,
            "state": state,
            "energy_source": str(r.get("energysourceid", "")).upper(),
            "producer_type": str(r.get("producertypeid", "")).upper(),
            "net_summer_capacity_mw": _float_or_none(r.get("capability")),
            "batch_id": batch_id,
            "ingested_at": now,
        })
    return out


def make_batch_id() -> str:
    """Return a sortable UTC batch identifier string."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
