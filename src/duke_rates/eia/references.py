"""Static reference / lookup tables for EIA integration.

These are maintained manually because EIA does not provide them via the API.
They are seeded into the database once and serve as dimension tables for
analytics queries.

Tables:
    eia_state_region_lookup     -- state -> census region/subregion
    eia_market_structure_lookup -- state -> regulated/hybrid/restructured
    eia_rto_lookup              -- state -> primary ISO/RTO affiliation

Sources for market structure:
    EIA: https://www.eia.gov/electricity/deregulation/
    FERC market oversight pages
    Status as of 2025-01; may be updated as state policies change.

    regulated   = vertically integrated monopoly utilities, rate-of-return
                  regulation, no retail choice
    hybrid      = some retail access or major municipal/co-op carve-outs;
                  still predominantly regulated IOU structure
    restructured = retail competition / deregulation enacted; ERCOT or an
                   ISO operates the wholesale market
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Census divisions / subregions
# EIA uses Census Bureau groupings for regional aggregation.
# ---------------------------------------------------------------------------
STATE_REGION: dict[str, dict[str, str]] = {
    "CT": {"census_division": "New England",          "census_region": "Northeast"},
    "ME": {"census_division": "New England",          "census_region": "Northeast"},
    "MA": {"census_division": "New England",          "census_region": "Northeast"},
    "NH": {"census_division": "New England",          "census_region": "Northeast"},
    "RI": {"census_division": "New England",          "census_region": "Northeast"},
    "VT": {"census_division": "New England",          "census_region": "Northeast"},
    "NJ": {"census_division": "Middle Atlantic",      "census_region": "Northeast"},
    "NY": {"census_division": "Middle Atlantic",      "census_region": "Northeast"},
    "PA": {"census_division": "Middle Atlantic",      "census_region": "Northeast"},
    "IL": {"census_division": "East North Central",   "census_region": "Midwest"},
    "IN": {"census_division": "East North Central",   "census_region": "Midwest"},
    "MI": {"census_division": "East North Central",   "census_region": "Midwest"},
    "OH": {"census_division": "East North Central",   "census_region": "Midwest"},
    "WI": {"census_division": "East North Central",   "census_region": "Midwest"},
    "IA": {"census_division": "West North Central",   "census_region": "Midwest"},
    "KS": {"census_division": "West North Central",   "census_region": "Midwest"},
    "MN": {"census_division": "West North Central",   "census_region": "Midwest"},
    "MO": {"census_division": "West North Central",   "census_region": "Midwest"},
    "NE": {"census_division": "West North Central",   "census_region": "Midwest"},
    "ND": {"census_division": "West North Central",   "census_region": "Midwest"},
    "SD": {"census_division": "West North Central",   "census_region": "Midwest"},
    "DE": {"census_division": "South Atlantic",       "census_region": "South"},
    "DC": {"census_division": "South Atlantic",       "census_region": "South"},
    "FL": {"census_division": "South Atlantic",       "census_region": "South"},
    "GA": {"census_division": "South Atlantic",       "census_region": "South"},
    "MD": {"census_division": "South Atlantic",       "census_region": "South"},
    "NC": {"census_division": "South Atlantic",       "census_region": "South"},
    "SC": {"census_division": "South Atlantic",       "census_region": "South"},
    "VA": {"census_division": "South Atlantic",       "census_region": "South"},
    "WV": {"census_division": "South Atlantic",       "census_region": "South"},
    "AL": {"census_division": "East South Central",   "census_region": "South"},
    "KY": {"census_division": "East South Central",   "census_region": "South"},
    "MS": {"census_division": "East South Central",   "census_region": "South"},
    "TN": {"census_division": "East South Central",   "census_region": "South"},
    "AR": {"census_division": "West South Central",   "census_region": "South"},
    "LA": {"census_division": "West South Central",   "census_region": "South"},
    "OK": {"census_division": "West South Central",   "census_region": "South"},
    "TX": {"census_division": "West South Central",   "census_region": "South"},
    "AZ": {"census_division": "Mountain",             "census_region": "West"},
    "CO": {"census_division": "Mountain",             "census_region": "West"},
    "ID": {"census_division": "Mountain",             "census_region": "West"},
    "MT": {"census_division": "Mountain",             "census_region": "West"},
    "NV": {"census_division": "Mountain",             "census_region": "West"},
    "NM": {"census_division": "Mountain",             "census_region": "West"},
    "UT": {"census_division": "Mountain",             "census_region": "West"},
    "WY": {"census_division": "Mountain",             "census_region": "West"},
    "AK": {"census_division": "Pacific Noncontiguous","census_region": "West"},
    "HI": {"census_division": "Pacific Noncontiguous","census_region": "West"},
    "CA": {"census_division": "Pacific Contiguous",   "census_region": "West"},
    "OR": {"census_division": "Pacific Contiguous",   "census_region": "West"},
    "WA": {"census_division": "Pacific Contiguous",   "census_region": "West"},
}


# ---------------------------------------------------------------------------
# Market structure
# last_reviewed: 2026-03-22 (knowledge basis: through August 2025)
# source: EIA Electric Power Monthly Table ES1 / state PUC websites
# review_cadence: annually, or when a state makes a major deregulation/re-regulation decision
# ---------------------------------------------------------------------------
MARKET_STRUCTURE: dict[str, dict[str, str]] = {
    # Restructured (retail competition available; ISO/RTO wholesale market)
    "CT": {"market_structure": "restructured", "retail_choice": "yes", "notes": "ISO-NE; residential choice available"},
    "DC": {"market_structure": "restructured", "retail_choice": "yes", "notes": "PJM; retail choice"},
    "DE": {"market_structure": "restructured", "retail_choice": "yes", "notes": "PJM; retail choice"},
    "IL": {"market_structure": "restructured", "retail_choice": "yes", "notes": "PJM; retail choice"},
    "MA": {"market_structure": "restructured", "retail_choice": "yes", "notes": "ISO-NE; retail choice"},
    "MD": {"market_structure": "restructured", "retail_choice": "yes", "notes": "PJM; retail choice"},
    "ME": {"market_structure": "restructured", "retail_choice": "yes", "notes": "ISO-NE; retail choice"},
    "MI": {"market_structure": "restructured", "retail_choice": "yes", "notes": "MISO; partial retail choice"},
    "NH": {"market_structure": "restructured", "retail_choice": "yes", "notes": "ISO-NE; retail choice"},
    "NJ": {"market_structure": "restructured", "retail_choice": "yes", "notes": "PJM; retail choice"},
    "NY": {"market_structure": "restructured", "retail_choice": "yes", "notes": "NYISO; retail choice"},
    "OH": {"market_structure": "restructured", "retail_choice": "yes", "notes": "PJM; retail choice"},
    "PA": {"market_structure": "restructured", "retail_choice": "yes", "notes": "PJM; retail choice"},
    "RI": {"market_structure": "restructured", "retail_choice": "yes", "notes": "ISO-NE; retail choice"},
    "TX": {"market_structure": "restructured", "retail_choice": "yes", "notes": "ERCOT; most of state retail choice"},
    # Hybrid (some elements of restructuring; still largely regulated IOU territory)
    "AZ": {"market_structure": "hybrid",       "retail_choice": "limited", "notes": "APS/SRP/TEP regulated; some industrial choice"},
    "CA": {"market_structure": "hybrid",       "retail_choice": "limited", "notes": "CAISO wholesale; residential choice frozen 2001"},
    "OR": {"market_structure": "hybrid",       "retail_choice": "limited", "notes": "PGE/PacifiCorp regulated; some choice for large C&I"},
    "VA": {"market_structure": "hybrid",       "retail_choice": "limited", "notes": "PJM member; Dominion and APCo regulated; RGGI member"},
    "MT": {"market_structure": "hybrid",       "retail_choice": "limited", "notes": "Northwestern regulated; partial C&I choice"},
    # Regulated (vertically integrated, rate-of-return regulated)
    "AL": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Alabama Power (Southern Co.)"},
    "AK": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Isolated grid; no interstate wholesale"},
    "AR": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Entergy Arkansas"},
    "CO": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Xcel Energy/PSCo; SPP/WEIS"},
    "FL": {"market_structure": "regulated",    "retail_choice": "no", "notes": "FPL/Duke FL/Tampa Electric regulated"},
    "GA": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Georgia Power (Southern Co.)"},
    "HI": {"market_structure": "regulated",    "retail_choice": "no", "notes": "HECO; isolated island grids"},
    "IA": {"market_structure": "regulated",    "retail_choice": "no", "notes": "MidAmerican/Alliant; MISO"},
    "ID": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Idaho Power/PacifiCorp"},
    "IN": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Duke Energy Indiana; MISO/PJM"},
    "KS": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Evergy; SPP"},
    "KY": {"market_structure": "regulated",    "retail_choice": "no", "notes": "LG&E-KU (PPL); Duke Kentucky; PJM/MISO"},
    "LA": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Entergy Louisiana; MISO"},
    "MN": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Xcel/NSP; MISO"},
    "MO": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Ameren/KCP&L; MISO/SPP"},
    "MS": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Entergy Mississippi"},
    "NC": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Duke Energy Progress/Carolinas; PJM wholesale member (joined 2012); retail fully regulated"},
    "ND": {"market_structure": "regulated",    "retail_choice": "no", "notes": "MDU/Basin Electric; MISO"},
    "NE": {"market_structure": "regulated",    "retail_choice": "no", "notes": "NPPD/LES/OG&E; SPP; many public power districts"},
    "NM": {"market_structure": "regulated",    "retail_choice": "no", "notes": "PNM/EPE; WECC"},
    "NV": {"market_structure": "regulated",    "retail_choice": "no", "notes": "NV Energy; WECC"},
    "OK": {"market_structure": "regulated",    "retail_choice": "no", "notes": "OG&E/PSO; SPP"},
    "SC": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Duke Energy Carolinas/Progress; Dominion SC"},
    "SD": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Northwestern/MDU; MISO"},
    "TN": {"market_structure": "regulated",    "retail_choice": "no", "notes": "TVA; quasi-federal system"},
    "UT": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Rocky Mountain Power (PacifiCorp); WECC"},
    "VT": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Green Mountain Power; ISO-NE"},
    "WA": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Puget Sound Energy/PSE; BPA/WECC; high hydro"},
    "WI": {"market_structure": "regulated",    "retail_choice": "no", "notes": "WE Energies/Alliant; MISO"},
    "WV": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Appalachian Power/Mon Power; PJM"},
    "WY": {"market_structure": "regulated",    "retail_choice": "no", "notes": "Rocky Mountain Power; WECC"},
}


# ---------------------------------------------------------------------------
# Primary ISO/RTO affiliation
# States with significant portions in multiple RTOs are listed with the
# dominant one.  Many Southern/Southeast states have no RTO.
# ---------------------------------------------------------------------------
RTO_AFFILIATION: dict[str, dict[str, str]] = {
    # PJM Interconnection
    "DC": {"rto": "PJM",    "rto_full": "PJM Interconnection"},
    "DE": {"rto": "PJM",    "rto_full": "PJM Interconnection"},
    "IL": {"rto": "PJM",    "rto_full": "PJM Interconnection"},  # ComEd; also MISO for southern IL
    "IN": {"rto": "PJM",    "rto_full": "PJM Interconnection"},  # also MISO
    "KY": {"rto": "PJM",    "rto_full": "PJM Interconnection"},  # LG&E/KU; also MISO (Big Rivers)
    "MD": {"rto": "PJM",    "rto_full": "PJM Interconnection"},
    "MI": {"rto": "MISO",   "rto_full": "Midcontinent ISO"},     # mostly MISO; Upper Peninsula PJM
    "NJ": {"rto": "PJM",    "rto_full": "PJM Interconnection"},
    "NC": {"rto": "PJM",    "rto_full": "PJM Interconnection"},  # Duke joined PJM 2012
    "OH": {"rto": "PJM",    "rto_full": "PJM Interconnection"},
    "PA": {"rto": "PJM",    "rto_full": "PJM Interconnection"},
    "VA": {"rto": "PJM",    "rto_full": "PJM Interconnection"},
    "WV": {"rto": "PJM",    "rto_full": "PJM Interconnection"},
    # ISO-NE
    "CT": {"rto": "ISO-NE", "rto_full": "ISO New England"},
    "MA": {"rto": "ISO-NE", "rto_full": "ISO New England"},
    "ME": {"rto": "ISO-NE", "rto_full": "ISO New England"},
    "NH": {"rto": "ISO-NE", "rto_full": "ISO New England"},
    "NY": {"rto": "NYISO",  "rto_full": "New York ISO"},
    "RI": {"rto": "ISO-NE", "rto_full": "ISO New England"},
    "VT": {"rto": "ISO-NE", "rto_full": "ISO New England"},
    # MISO
    "AR": {"rto": "MISO",   "rto_full": "Midcontinent ISO"},
    "IA": {"rto": "MISO",   "rto_full": "Midcontinent ISO"},
    "LA": {"rto": "MISO",   "rto_full": "Midcontinent ISO"},
    "MN": {"rto": "MISO",   "rto_full": "Midcontinent ISO"},
    "MO": {"rto": "MISO",   "rto_full": "Midcontinent ISO"},
    "MS": {"rto": "MISO",   "rto_full": "Midcontinent ISO"},
    "ND": {"rto": "MISO",   "rto_full": "Midcontinent ISO"},
    "SD": {"rto": "MISO",   "rto_full": "Midcontinent ISO"},
    "WI": {"rto": "MISO",   "rto_full": "Midcontinent ISO"},
    # SPP
    "KS": {"rto": "SPP",    "rto_full": "Southwest Power Pool"},
    "NE": {"rto": "SPP",    "rto_full": "Southwest Power Pool"},
    "OK": {"rto": "SPP",    "rto_full": "Southwest Power Pool"},
    # ERCOT (Texas)
    "TX": {"rto": "ERCOT",  "rto_full": "Electric Reliability Council of Texas"},
    # CAISO
    "CA": {"rto": "CAISO",  "rto_full": "California ISO"},
    # WECC / no RTO (West)
    "AK": {"rto": "None",   "rto_full": "No RTO — isolated grid"},
    "AZ": {"rto": "WECC",   "rto_full": "WECC (no RTO)"},
    "CO": {"rto": "WECC",   "rto_full": "WECC (SPP / no RTO)"},
    "HI": {"rto": "None",   "rto_full": "No RTO — isolated island grids"},
    "ID": {"rto": "WECC",   "rto_full": "WECC (no RTO)"},
    "MT": {"rto": "WECC",   "rto_full": "WECC (no RTO)"},
    "NM": {"rto": "WECC",   "rto_full": "WECC (no RTO)"},
    "NV": {"rto": "WECC",   "rto_full": "WECC (no RTO)"},
    "OR": {"rto": "WECC",   "rto_full": "WECC (no RTO)"},
    "UT": {"rto": "WECC",   "rto_full": "WECC (no RTO)"},
    "WA": {"rto": "WECC",   "rto_full": "WECC (no RTO)"},
    "WY": {"rto": "WECC",   "rto_full": "WECC (no RTO)"},
    # Southeast — no RTO
    "AL": {"rto": "SERC",   "rto_full": "SERC (no RTO — regulated)"},
    "FL": {"rto": "FRCC",   "rto_full": "Florida Reliability Coordinating Council (no RTO)"},
    "GA": {"rto": "SERC",   "rto_full": "SERC (no RTO — regulated)"},
    "SC": {"rto": "SERC",   "rto_full": "SERC (no RTO — regulated)"},  # Duke joined PJM partially
    "TN": {"rto": "TVA",    "rto_full": "Tennessee Valley Authority (federal power authority)"},
}


# ---------------------------------------------------------------------------
# Seeder functions — insert reference rows into SQLite
# ---------------------------------------------------------------------------

import sqlite3


def seed_state_region_lookup(conn: sqlite3.Connection) -> tuple[int, int]:
    """Seed ``eia_state_region_lookup`` from static reference data.

    Idempotent — skips existing rows.
    Returns ``(inserted, skipped)``.
    """
    from datetime import UTC, datetime
    now = datetime.now(UTC).isoformat()
    inserted = 0
    skipped = 0

    for state, vals in STATE_REGION.items():
        existing = conn.execute(
            "SELECT 1 FROM eia_state_region_lookup WHERE state=?", (state,)
        ).fetchone()
        if existing:
            skipped += 1
            continue
        conn.execute(
            """
            INSERT INTO eia_state_region_lookup
                (state, census_division, census_region, created_at)
            VALUES (?,?,?,?)
            """,
            (state, vals["census_division"], vals["census_region"], now),
        )
        inserted += 1

    conn.commit()
    return inserted, skipped


def seed_market_structure_lookup(conn: sqlite3.Connection) -> tuple[int, int]:
    """Seed ``eia_market_structure_lookup`` from static reference data."""
    from datetime import UTC, datetime
    now = datetime.now(UTC).isoformat()
    inserted = 0
    skipped = 0

    for state, vals in MARKET_STRUCTURE.items():
        rto_info = RTO_AFFILIATION.get(state, {})
        existing = conn.execute(
            "SELECT 1 FROM eia_market_structure_lookup WHERE state=?", (state,)
        ).fetchone()
        if existing:
            skipped += 1
            continue
        conn.execute(
            """
            INSERT INTO eia_market_structure_lookup
                (state, market_structure, retail_choice, rto, rto_full, notes, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                state,
                vals["market_structure"],
                vals["retail_choice"],
                rto_info.get("rto"),
                rto_info.get("rto_full"),
                vals.get("notes"),
                now,
            ),
        )
        inserted += 1

    conn.commit()
    return inserted, skipped
