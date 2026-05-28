"""Residential bill component breakdown for the Streamlit dashboard.

Returns a flat per-component DataFrame suitable for donut/treemap rendering:
the latest snapshot of base + each named rider, expressed as $/month at a
caller-provided monthly kWh.

Pulls glossary metadata (short_name, category, description) from
``rider_descriptions`` when available so the chart hover/labels can show
plain-English captions without hardcoding.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from duke_rates.analytics.canonical_residential import (
    load_canonical_residential_timeline,
)
from duke_rates.analytics.canonical_rider_components import (
    load_dec_rs_canonical_rider_components,
    load_dep_res_canonical_rider_components,
)
from duke_rates.analytics.dep_progress import _require_pandas

Utility = Literal["DEP", "DEC"]


def load_residential_event_annotations(
    *,
    database_path: Path | None = None,
):
    """Load legislative + market events for timeline annotations.

    Returns a DataFrame with columns:
        effective_date, short_title, summary, impact_category, bill_number,
        utilities_affected, source_url, confidence
    """
    pd = _require_pandas()
    db_path = Path(database_path) if database_path else None
    if db_path is None:
        return pd.DataFrame()
    conn = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            """
            SELECT effective_date, short_title, summary, impact_category,
                   bill_number, utilities_affected, source_url, confidence
            FROM legislative_actions
            WHERE effective_date IS NOT NULL
            ORDER BY effective_date
            """,
            conn,
        )
    finally:
        conn.close()
    if df.empty:
        return df
    df["effective_date"] = pd.to_datetime(df["effective_date"], errors="coerce")
    return df.dropna(subset=["effective_date"])


def load_rider_glossary(
    *,
    database_path: Path | None = None,
):
    """Load rider_descriptions catalog. Returns indexed-by-code DataFrame."""
    pd = _require_pandas()
    db_path = Path(database_path) if database_path else None
    if db_path is None:
        return pd.DataFrame()
    conn = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            """
            SELECT rider_code, short_name, full_name, description, category
            FROM rider_descriptions
            """,
            conn,
        )
    finally:
        conn.close()
    return df


def load_latest_residential_breakdown(
    *,
    utility: Utility,
    monthly_kwh: float,
    database_path: Path | None = None,
):
    """Return a per-component breakdown of the most-recent residential bill.

    Output DataFrame columns:
        component      : str   — 'Base rate' or rider code (e.g. 'CPRE')
        component_kind : str   — 'base' | 'rider' | 'residual'
        short_name     : str   — plain-English label (falls back to component)
        category       : str   — 'base' | 'fuel' | 'renewable' | 'tax' | ...
        description    : str   — long description for hover (may be empty)
        cents_per_kwh  : float — per-kWh contribution (may be negative for credits)
        dollars        : float — cents_per_kwh * monthly_kwh / 100
        effective_date : Timestamp
        rider_effective_date : Timestamp | NaT (rider components only)

    The breakdown reconciles to the canonical all-in rate; any unexplained
    delta between the canonical rider total and the sum of itemized
    components becomes a 'Residual / non-itemized' row.
    """
    pd = _require_pandas()
    if monthly_kwh <= 0:
        raise ValueError("monthly_kwh must be positive")

    timeline = load_canonical_residential_timeline(database_path=database_path)
    if timeline.empty:
        return pd.DataFrame()
    timeline = timeline[timeline["utility"] == utility].copy()
    if timeline.empty:
        return pd.DataFrame()
    timeline["effective_date"] = pd.to_datetime(timeline["effective_date"])
    timeline = timeline.sort_values("effective_date")
    latest = timeline.iloc[-1]

    if utility == "DEP":
        components = load_dep_res_canonical_rider_components(database_path=database_path)
    else:
        components = load_dec_rs_canonical_rider_components(database_path=database_path)

    rider_effective_date = pd.to_datetime(
        latest.get("rider_effective_date"), errors="coerce"
    )
    snapshot = pd.DataFrame()
    if not components.empty and pd.notna(rider_effective_date):
        snapshot = components[components["effective_date"] == rider_effective_date].copy()

    glossary = load_rider_glossary(database_path=database_path)
    if not glossary.empty:
        glossary = glossary.set_index("rider_code")

    rows: list[dict] = []
    base_cents = float(latest.get("base_cents_per_kwh") or 0.0)
    rows.append(
        {
            "component": "Base rate",
            "component_kind": "base",
            "short_name": "Base rate (energy + customer charge)",
            "category": "base",
            "description": (
                "The underlying residential energy and customer charges set in the "
                "most recent rate case. Everything below is added on top of this."
            ),
            "cents_per_kwh": base_cents,
            "dollars": base_cents * monthly_kwh / 100.0,
            "effective_date": pd.to_datetime(latest["effective_date"]),
            "rider_effective_date": pd.NaT,
        }
    )

    explained_total_cents = 0.0
    if not snapshot.empty:
        for _, row in snapshot.iterrows():
            code = row["rider_code"]
            cents = float(row["cents_per_kwh"] or 0.0)
            explained_total_cents += cents
            meta_short = code
            meta_category = "rider"
            meta_desc = ""
            if not glossary.empty and code in glossary.index:
                g = glossary.loc[code]
                meta_short = g.get("short_name") or g.get("full_name") or code
                meta_category = g.get("category") or "rider"
                meta_desc = g.get("description") or ""
            rows.append(
                {
                    "component": code,
                    "component_kind": "rider",
                    "short_name": meta_short,
                    "category": meta_category,
                    "description": meta_desc,
                    "cents_per_kwh": cents,
                    "dollars": cents * monthly_kwh / 100.0,
                    "effective_date": pd.to_datetime(latest["effective_date"]),
                    "rider_effective_date": rider_effective_date,
                }
            )

    rider_total_cents = float(latest.get("rider_cents_per_kwh") or 0.0)
    residual = round(rider_total_cents - explained_total_cents, 4)
    if abs(residual) > 0.001:
        rows.append(
            {
                "component": "Residual",
                "component_kind": "residual",
                "short_name": "Residual / non-itemized riders",
                "category": "residual",
                "description": (
                    "Gap between the canonical rider total and the sum of itemized "
                    "rider components for this snapshot. Usually reflects reconstructed "
                    "(pre-2023) periods where component-level data is incomplete."
                ),
                "cents_per_kwh": residual,
                "dollars": residual * monthly_kwh / 100.0,
                "effective_date": pd.to_datetime(latest["effective_date"]),
                "rider_effective_date": rider_effective_date,
            }
        )

    return pd.DataFrame(rows)
