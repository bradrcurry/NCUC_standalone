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

    # --- Direct-billed riders (in_rider_summary = 0) ---
    db_path = Path(database_path) if database_path else None
    if db_path is not None and pd.notna(rider_effective_date):
        base_family_key = "nc-progress-leaf-500" if utility == "DEP" else "nc-carolinas-schedule-RS"
        date_str = str(rider_effective_date.date())
        
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Query all mandatory applicability links for this base schedule where in_rider_summary = 0
            links = conn.execute(
                """
                SELECT rider_family_key, effective_start, effective_end, mandatory
                FROM rider_applicability
                WHERE applies_to_family_key = ?
                  AND in_rider_summary = 0
                  AND mandatory = 1
                """,
                (base_family_key,),
            ).fetchall()
            
            # Filter links active on rider_effective_date
            active_links = []
            for lnk in links:
                start = lnk["effective_start"]
                end = lnk["effective_end"]
                if (start is None or start <= date_str) and (end is None or end >= date_str):
                    active_links.append(lnk["rider_family_key"])
                    
            active_links = list(set(active_links))
            
            from duke_rates.billing.tariff_engine import _label_class, _class_matches
            
            for rider_key in active_links:
                # Find all versions of this rider
                versions = conn.execute(
                    "SELECT id, effective_start, effective_end FROM tariff_versions WHERE family_key = ?",
                    (rider_key,),
                ).fetchall()
                
                # Select version
                dated_versions = [v for v in versions if v["effective_start"]]
                selected_version_id = None
                if dated_versions:
                    eligible_versions = [v for v in dated_versions if v["effective_start"] <= date_str]
                    if eligible_versions:
                        selected = max(eligible_versions, key=lambda v: v["effective_start"])
                    else:
                        selected = min(dated_versions, key=lambda v: v["effective_start"])
                    selected_version_id = selected["id"]
                elif versions:
                    selected_version_id = versions[0]["id"]
                    
                if selected_version_id is not None:
                    # Query charges for this version
                    charges = conn.execute(
                        "SELECT charge_label, rate_value, rate_unit, customer_class FROM tariff_charges WHERE version_id = ? AND charge_type = 'adjustment'",
                        (selected_version_id,),
                    ).fetchall()
                    
                    cents_sum = 0.0
                    found_charge = False
                    for c in charges:
                        resolved_class = c["customer_class"] or _label_class(c["charge_label"])
                        if _class_matches(resolved_class, "residential"):
                            val = c["rate_value"] or 0.0
                            unit = (c["rate_unit"] or "").lower()
                            if "kwh" in unit:
                                if "cent" in unit or "¢" in unit:
                                    cents_sum += val
                                else:
                                    cents_sum += val * 100.0
                                found_charge = True
                    
                    if found_charge:
                        # Map known family keys to clean codes
                        import re
                        leaf_match = re.search(r'leaf-(\d+)$', rider_key)
                        code = rider_key.split("-")[-1].upper()
                        
                        meta_short = code
                        meta_category = "rider"
                        meta_desc = ""
                        
                        known_codes = {"607": "STS", "613": "STS-2", "113": "SSR"}
                        matched_known = False
                        if leaf_match and leaf_match.group(1) in known_codes:
                            code = known_codes[leaf_match.group(1)]
                            matched_known = True
                            if not glossary.empty and code in glossary.index:
                                g = glossary.loc[code]
                                meta_short = g.get("short_name") or g.get("full_name") or code
                                meta_category = g.get("category") or "rider"
                                meta_desc = g.get("description") or ""
                            else:
                                if code == "STS-2":
                                    meta_short = "Rider STS-2"
                                    meta_category = "storm"
                                    meta_desc = (
                                        "Storm Securitization Rider 2. Services debt on AAA-rated bonds "
                                        "issued to finance restoration costs from other major hurricanes. "
                                        "Securitizing these costs keeps interest rates and monthly bills lower "
                                        "than traditional utility financing."
                                    )
                                elif code == "SSR":
                                    meta_short = "Rider SSR"
                                    meta_category = "storm"
                                    meta_desc = (
                                        "Storm Securitization Rider. Services debt on storm recovery bonds "
                                        "for historical storm damage."
                                    )
                                
                        if not matched_known:
                            desc_row = None
                            if leaf_match:
                                leaf_no = leaf_match.group(1)
                                desc_row = conn.execute(
                                    "SELECT rider_code, short_name, full_name, description, category FROM rider_descriptions WHERE notes LIKE ? OR description LIKE ? OR full_name LIKE ?",
                                    (f"%Leaf No. {leaf_no}%", f"%Leaf {leaf_no}%", f"%Leaf No. {leaf_no}%")
                                ).fetchone()
                                
                            if desc_row:
                                code = desc_row["rider_code"]
                                meta_short = desc_row["short_name"] or desc_row["full_name"] or code
                                meta_category = desc_row["category"] or "rider"
                                meta_desc = desc_row["description"] or ""
                                
                        rows.append(
                            {
                                "component": code,
                                "component_kind": "rider",
                                "short_name": meta_short,
                                "category": meta_category,
                                "description": meta_desc,
                                "cents_per_kwh": cents_sum,
                                "dollars": cents_sum * monthly_kwh / 100.0,
                                "effective_date": pd.to_datetime(latest["effective_date"]),
                                "rider_effective_date": rider_effective_date,
                            }
                        )
        finally:
            conn.close()

    return pd.DataFrame(rows)

