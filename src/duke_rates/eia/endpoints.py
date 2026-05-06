"""EIA API v2 endpoint definitions and fetch functions.

Each public function in this module fetches one logical dataset and returns
a list of raw EIA record dicts.  The transformers module is responsible for
normalizing these into canonical Python dicts.

Critical API gotchas encoded here:
- retail-sales uses facet ``stateid`` and string sector codes (RES, COM, IND, ALL)
- electric-power-operational-data uses facet ``location`` and numeric sector codes
- state-electricity-profiles/summary uses ``stateID`` (capital I and D)
- All numeric values returned as strings — cast in transformers, not here
- state-electricity-profiles sub-routes are annual only
- generation units are thousand-MWh, not MWh
"""
from __future__ import annotations

import logging
from typing import Any

from duke_rates.eia.client import EIAClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical sector codes per endpoint family
# ---------------------------------------------------------------------------

# retail-sales: string codes
RETAIL_SECTOR_ALL = ["ALL", "RES", "COM", "IND", "TRA"]
RETAIL_SECTOR_RESIDENTIAL = ["RES"]

# electric-power-operational-data: numeric codes
# 99 = all sectors, 1 = electric utilities, 2 = IPP non-CHP, 3 = IPP CHP
EPOD_SECTOR_ALL = ["99"]

# Generation fuel types to ingest (the most analytically useful subset)
GENERATION_FUELS = [
    "ALL",   # all fuels (used for share calculations)
    "NG",    # natural gas
    "COW",   # all coal
    "NUC",   # nuclear
    "HYC",   # conventional hydro
    "WND",   # wind (all)
    "SUN",   # solar (all)
    "PET",   # petroleum
    "GEO",   # geothermal
    "BIO",   # biomass
]

# All 50 states + DC for full-nation ingests
ALL_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC","US",
]

# Southeast states for focused analyses
SOUTHEAST_STATES = ["NC", "SC", "VA", "GA", "TN", "FL", "AL", "MS", "KY", "WV"]

# Duke-served states
DUKE_STATES = ["NC", "SC", "IN", "OH", "KY", "FL"]


# ---------------------------------------------------------------------------
# 1. Retail Sales (EIA-826 / EIA-861 / EIA-861M)
# ---------------------------------------------------------------------------

def fetch_retail_sales(
    client: EIAClient,
    *,
    states: list[str] | None = None,
    sectors: list[str] | None = None,
    frequency: str = "annual",
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Fetch retail electricity sales, revenue, price, and customers by state/sector.

    Route: ``electricity/retail-sales``
    Facets: stateid, sectorid
    Frequency: annual | monthly | quarterly
    Coverage: 2001-01 to present

    Returns raw EIA record dicts.  Key fields:
        period, stateid, stateDescription, sectorid, sectorName,
        sales, revenue, price, customers (all strings)
    """
    facets: dict[str, list[str]] = {}
    if states:
        facets["stateid"] = states
    if sectors:
        facets["sectorid"] = sectors

    log.info(
        "Fetching EIA retail-sales: freq=%s states=%s sectors=%s start=%s end=%s",
        frequency, states, sectors, start, end,
    )
    records = client.fetch_all(
        "electricity/retail-sales",
        frequency=frequency,
        data_cols=["sales", "revenue", "price", "customers"],
        facets=facets if facets else None,
        start=start,
        end=end,
    )
    log.info("EIA retail-sales: %d records fetched", len(records))
    return records


# ---------------------------------------------------------------------------
# 2. Generation by Fuel (EIA-923)
# ---------------------------------------------------------------------------

def fetch_generation_by_fuel(
    client: EIAClient,
    *,
    states: list[str] | None = None,
    fuels: list[str] | None = None,
    sectors: list[str] | None = None,
    frequency: str = "annual",
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Fetch net electricity generation by state, fuel type, and sector.

    Route: ``electricity/electric-power-operational-data``
    Facets: location (NOT stateid), fueltypeid, sectorid (numeric)
    Frequency: annual | monthly | quarterly
    Coverage: 2001-01 to present
    Units: thousand MWh

    IMPORTANT: facet key is ``location``, not ``stateid``.

    Returns raw EIA record dicts.  Key field: ``generation`` (thousand MWh, str).
    """
    facets: dict[str, list[str]] = {}
    if states:
        facets["location"] = states
    if fuels:
        facets["fueltypeid"] = fuels
    if sectors:
        facets["sectorid"] = sectors

    log.info(
        "Fetching EIA generation: freq=%s states=%s fuels=%s start=%s end=%s",
        frequency, states, fuels, start, end,
    )
    records = client.fetch_all(
        "electricity/electric-power-operational-data",
        frequency=frequency,
        data_cols=["generation"],
        facets=facets if facets else None,
        start=start,
        end=end,
    )
    log.info("EIA generation: %d records fetched", len(records))
    return records


# ---------------------------------------------------------------------------
# 3. State Electricity Profile — Summary (State Rankings)
# ---------------------------------------------------------------------------

def fetch_state_profile_summary(
    client: EIAClient,
    *,
    states: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Fetch annual state-level summary statistics and US rankings.

    Route: ``electricity/state-electricity-profiles/summary``
    Facet: stateID  (NOTE: capital I and D — differs from other endpoints)
    Frequency: annual only
    Coverage: 2008 to present

    Returns raw records with fields including:
        net-summer-capacity, net-generation, total-retail-sales,
        average-retail-price, carbon-dioxide (plus *-rank variants)
    """
    facets: dict[str, list[str]] = {}
    if states:
        # This endpoint uses stateID (capital I,D) — not stateid
        facets["stateID"] = states

    log.info("Fetching EIA state-profile-summary: states=%s start=%s end=%s", states, start, end)
    records = client.fetch_all(
        "electricity/state-electricity-profiles/summary",
        frequency="annual",
        data_cols=[
            "net-summer-capacity",
            "net-generation",
            "total-retail-sales",
            "average-retail-price",
            "carbon-dioxide",
            "net-summer-capacity-rank",
            "net-generation-rank",
            "total-retail-sales-rank",
            "average-retail-price-rank",
        ],
        facets=facets if facets else None,
        start=start,
        end=end,
    )
    log.info("EIA state-profile-summary: %d records fetched", len(records))
    return records


# ---------------------------------------------------------------------------
# 4. State Source-Disposition (Supply & Disposition balance)
# ---------------------------------------------------------------------------

def fetch_state_source_disposition(
    client: EIAClient,
    *,
    states: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Fetch annual state electricity supply and disposition balance.

    Route: ``electricity/state-electricity-profiles/source-disposition``
    Facet: state
    Frequency: annual only
    Coverage: 1990 to present
    Units: MWh

    Key fields: total-net-generation, total-supply, total-elect-indust
    (retail sales), net-interstate-trade, estimated-losses.
    """
    facets: dict[str, list[str]] = {}
    if states:
        facets["state"] = states

    log.info("Fetching EIA source-disposition: states=%s", states)
    records = client.fetch_all(
        "electricity/state-electricity-profiles/source-disposition",
        frequency="annual",
        data_cols=[
            "total-net-generation",
            "total-supply",
            "total-elect-indust",
            "net-interstate-trade",
            "estimated-losses",
            "direct-use",
        ],
        facets=facets if facets else None,
        start=start,
        end=end,
    )
    log.info("EIA source-disposition: %d records fetched", len(records))
    return records


# ---------------------------------------------------------------------------
# 5. Generating Capacity by Fuel (state-level)
# ---------------------------------------------------------------------------

def fetch_state_capability(
    client: EIAClient,
    *,
    states: list[str] | None = None,
    energy_sources: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Fetch annual net summer generating capacity by state and energy source.

    Route: ``electricity/state-electricity-profiles/capability``
    Facets: stateId, producertypeid, energysourceid
    Frequency: annual only
    Coverage: 1990 to present
    Units: megawatts

    Useful energy source codes: ALL, NG, NUC, HYC, WND, SOL, COL, PET
    Producer type: TOT (all sectors)
    """
    facets: dict[str, list[str]] = {}
    if states:
        facets["stateId"] = states
    if energy_sources:
        facets["energysourceid"] = energy_sources
    # Default to total (all producer types)
    facets["producertypeid"] = ["TOT"]

    log.info("Fetching EIA capability: states=%s sources=%s", states, energy_sources)
    records = client.fetch_all(
        "electricity/state-electricity-profiles/capability",
        frequency="annual",
        data_cols=["capability"],
        facets=facets if facets else None,
        start=start,
        end=end,
    )
    log.info("EIA capability: %d records fetched", len(records))
    return records
