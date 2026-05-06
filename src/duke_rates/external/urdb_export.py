"""URDB (Utility Rate Database) JSON exporter from tariff_charges tables.

Converts duke-rates tariff DB records into the OpenEI URDB JSON format for
manual curation and submission to https://openei.org/apps/USURDB/.

URDB format reference:
    https://openei.org/services/doc/rest/util_rates/?version=8

Key URDB structures generated:
    fixedcharges       — list of {period, tier, charge, chargetype}
    energyratestructure — 3D list [period][tier]{rate, unit, max?, ...}
    energyweekdayschedule / energyweekendschedule — 8760-like period maps
    demandratestructure — similar to energy
    demandweekdayschedule / demandweekendschedule
    tou_periods        — period label → hour ranges

The export is a *curation aid*, not a direct upload payload. URDB submissions
require human review of period maps, sector classification, and effective dates
before submission through the OpenEI web interface.

Confidence threshold: only families with at least one charge having
confidence_score ≥ 0.7 are included in bulk exports. Individual exports
ignore the threshold.
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from typing import Any

_UTILITY_NAME_MAP = {
    "progress": "Duke Energy Progress, LLC",
    "carolinas": "Duke Energy Carolinas, LLC",
    "florida": "Duke Energy Florida, LLC",
    "indiana": "Duke Energy Indiana, LLC",
    "kentucky": "Duke Energy Kentucky, Inc.",
    "ohio": "Duke Energy Ohio, Inc.",
}

_STATE_FULL_NAME = {
    "NC": "North Carolina", "SC": "South Carolina", "FL": "Florida",
    "IN": "Indiana", "KY": "Kentucky", "OH": "Ohio",
}

_SECTOR_MAP = {
    "residential": "Residential",
    "commercial": "Commercial",
    "industrial": "Industrial",
    "lighting": "Lighting",
}

# Duke NC TOU period hour ranges (0-based, inclusive start, exclusive end)
# Used to build weekday/weekend schedule arrays
_DUKE_NC_TOU_HOURS: dict[str, list[tuple[int, int]]] = {
    "on_peak":  [(14, 21)],                    # 2 PM – 9 PM weekdays
    "discount": [(0, 6), (21, 24)],            # midnight–6 AM + 9 PM–midnight weekdays
    "off_peak": [(6, 14)],                     # 6 AM – 2 PM weekdays (+ all weekend)
}

_MIN_CONFIDENCE = 0.7   # minimum charge confidence for bulk export inclusion


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class URDBRecord:
    """One URDB-shaped record for a single tariff family version."""
    family_key: str
    schedule_code: str | None
    utility: str | None
    name: str | None
    state: str | None
    sector: str | None
    effective_start: str | None
    effective_end: str | None
    source_url: str | None
    description: str | None
    tou: bool
    uri: str | None = None              # existing URDB label (if matched)
    # Structured charge data (URDB format)
    fixedcharges: list[dict] = field(default_factory=list)
    energyratestructure: list[list[dict]] = field(default_factory=list)
    energyweekdayschedule: list[list[int]] = field(default_factory=list)
    energyweekendschedule: list[list[int]] = field(default_factory=list)
    demandratestructure: list[list[dict]] = field(default_factory=list)
    demandweekdayschedule: list[list[int]] = field(default_factory=list)
    demandweekendschedule: list[list[int]] = field(default_factory=list)
    # Rider info (not submitted directly — listed for curation)
    rider_family_keys: list[str] = field(default_factory=list)
    # Quality metadata
    min_confidence: float = 0.0
    missing_fields: list[str] = field(default_factory=list)
    curation_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a URDB-compatible dict for JSON output."""
        d: dict[str, Any] = {
            "_duke_rates_family_key": self.family_key,
            "name": self.name,
            "utility": self.utility,
            "state": _STATE_FULL_NAME.get(self.state or "", self.state),
            "sector": self.sector,
            "startdate": self.effective_start,
            "enddate": self.effective_end,
            "source": self.source_url,
            "description": self.description,
            "tou": 1 if self.tou else 0,
            "schedule_code": self.schedule_code,
        }
        if self.uri:
            d["label"] = self.uri
        if self.fixedcharges:
            d["fixedcharges"] = self.fixedcharges
        if self.energyratestructure:
            d["energyratestructure"] = self.energyratestructure
            d["energyweekdayschedule"] = self.energyweekdayschedule
            d["energyweekendschedule"] = self.energyweekendschedule
        if self.demandratestructure:
            d["demandratestructure"] = self.demandratestructure
            d["demandweekdayschedule"] = self.demandweekdayschedule
            d["demandweekendschedule"] = self.demandweekendschedule
        if self.rider_family_keys:
            d["_rider_family_keys"] = self.rider_family_keys
        # Curation metadata (prefixed with _ to distinguish from URDB fields)
        d["_min_confidence"] = round(self.min_confidence, 3)
        d["_missing_fields"] = self.missing_fields
        d["_curation_notes"] = self.curation_notes
        return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_family_to_urdb(
    conn,
    family_key: str,
    *,
    source_url: str | None = None,
) -> URDBRecord | None:
    """Build a URDBRecord for a single tariff family (latest version).

    Args:
        conn: sqlite3 connection to the duke_rates DB.
        family_key: Tariff family key to export.
        source_url: Override the source URL (otherwise derived from version metadata).

    Returns:
        URDBRecord, or None if the family has no parseable charges.
    """
    family = _fetch_family(conn, family_key)
    if family is None:
        return None
    version = _fetch_latest_version(conn, family_key)
    if version is None:
        return None
    charges = _fetch_charges(conn, version["id"])
    if not charges:
        return None

    riders = _fetch_rider_keys(conn, family_key)
    record = _build_record(family, version, charges, riders,
                           source_url=source_url)
    return record


def export_bulk_to_urdb(
    conn,
    *,
    state: str | None = None,
    company: str | None = None,
    family_type: str = "rate_schedule",
    min_confidence: float = _MIN_CONFIDENCE,
    source_url_prefix: str | None = None,
) -> list[URDBRecord]:
    """Build URDBRecords for all matching tariff families.

    Args:
        state: Filter by state code (e.g. "NC").
        company: Filter by company slug (e.g. "progress").
        family_type: Usually "rate_schedule"; set "rider" to export riders.
        min_confidence: Skip families whose best-charge confidence is below this.
        source_url_prefix: Base URL prepended to revision_label for source_url.

    Returns:
        List of URDBRecord sorted by family_key, excluding low-confidence families.
    """
    families = _fetch_families(conn, state=state, company=company,
                               family_type=family_type)
    records: list[URDBRecord] = []
    for fam in families:
        fk = fam["family_key"]
        version = _fetch_latest_version(conn, fk)
        if version is None:
            continue
        charges = _fetch_charges(conn, version["id"])
        if not charges:
            continue
        best_conf = max((c["confidence_score"] or 0.0) for c in charges)
        if best_conf < min_confidence:
            continue
        riders = _fetch_rider_keys(conn, fk)
        src_url = source_url_prefix  # callers may override per-record later
        record = _build_record(fam, version, charges, riders, source_url=src_url)
        records.append(record)
    return sorted(records, key=lambda r: r.family_key)


def records_to_json(records: list[URDBRecord], *, indent: int = 2) -> str:
    """Serialize a list of URDBRecords to JSON string."""
    return json.dumps([r.to_dict() for r in records], indent=indent, default=str)


# ---------------------------------------------------------------------------
# Private helpers — DB access
# ---------------------------------------------------------------------------

def _fetch_family(conn, family_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM tariff_families WHERE family_key = ?", (family_key,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(conn, "tariff_families", row)


def _fetch_families(conn, state=None, company=None, family_type=None) -> list[dict]:
    q = "SELECT * FROM tariff_families WHERE 1=1"
    params: list = []
    if state:
        q += " AND state = ?"
        params.append(state)
    if company:
        q += " AND company = ?"
        params.append(company)
    if family_type:
        q += " AND family_type = ?"
        params.append(family_type)
    rows = conn.execute(q, params).fetchall()
    col_names = [d[1] for d in conn.execute("PRAGMA table_info(tariff_families)").fetchall()]
    return [dict(zip(col_names, row)) for row in rows]


def _fetch_latest_version(conn, family_key: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM tariff_versions
           WHERE family_key = ?
           ORDER BY effective_start DESC NULLS LAST, id DESC
           LIMIT 1""",
        (family_key,),
    ).fetchone()
    if row is None:
        return None
    col_names = [d[1] for d in conn.execute("PRAGMA table_info(tariff_versions)").fetchall()]
    return dict(zip(col_names, row))


def _fetch_charges(conn, version_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM tariff_charges WHERE version_id = ?", (version_id,)
    ).fetchall()
    col_names = [d[1] for d in conn.execute("PRAGMA table_info(tariff_charges)").fetchall()]
    return [dict(zip(col_names, row)) for row in rows]


def _fetch_rider_keys(conn, family_key: str) -> list[str]:
    rows = conn.execute(
        "SELECT rider_family_key FROM rider_applicability WHERE applies_to_family_key = ?",
        (family_key,),
    ).fetchall()
    return [r[0] for r in rows]


def _row_to_dict(conn, table: str, row) -> dict:
    col_names = [d[1] for d in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return dict(zip(col_names, row))


# ---------------------------------------------------------------------------
# Private helpers — URDB record construction
# ---------------------------------------------------------------------------

def _build_record(
    family: dict,
    version: dict,
    charges: list[dict],
    rider_keys: list[str],
    *,
    source_url: str | None,
) -> URDBRecord:
    """Assemble a URDBRecord from raw DB dicts."""
    company = family.get("company") or ""
    state = family.get("state") or ""
    title = family.get("title") or family.get("schedule_code") or family["family_key"]
    sector = _infer_sector(family, charges)

    fixed = [c for c in charges if c["charge_type"] == "fixed"]
    energy = [c for c in charges if c["charge_type"] in ("energy_block", "tou_energy")]
    demand = [c for c in charges if c["charge_type"] == "demand"]
    is_tou = any(c["tou_period"] for c in energy)

    min_conf = min((c["confidence_score"] or 0.0) for c in charges) if charges else 0.0
    missing = _check_missing(family, version, charges)
    notes = _curation_notes(family, version, charges, rider_keys)

    # Fixed charges → URDB fixedcharges format
    urdb_fixed = _build_fixed_charges(fixed)

    # Energy charges → URDB energyratestructure + schedules
    energy_structure, wd_sched, we_sched = _build_energy_structure(energy, is_tou, state)

    # Demand charges → URDB demandratestructure + schedules
    demand_structure, d_wd_sched, d_we_sched = _build_demand_structure(demand)

    return URDBRecord(
        family_key=family["family_key"],
        schedule_code=family.get("schedule_code"),
        utility=_UTILITY_NAME_MAP.get(company.lower(), company or None),
        name=title,
        state=state or None,
        sector=sector,
        effective_start=version.get("effective_start"),
        effective_end=version.get("effective_end"),
        source_url=source_url or version.get("source_pdf"),
        description=f"Duke Energy tariff: {title}. Rev: {version.get('revision_label', '')}.",
        tou=is_tou,
        rider_family_keys=rider_keys,
        fixedcharges=urdb_fixed,
        energyratestructure=energy_structure,
        energyweekdayschedule=wd_sched,
        energyweekendschedule=we_sched,
        demandratestructure=demand_structure,
        demandweekdayschedule=d_wd_sched,
        demandweekendschedule=d_we_sched,
        min_confidence=min_conf,
        missing_fields=missing,
        curation_notes=notes,
    )


def _build_fixed_charges(charges: list[dict]) -> list[dict]:
    """Convert fixed charges to URDB fixedcharges list."""
    result = []
    for c in charges:
        rate = c.get("rate_value")
        unit = c.get("rate_unit") or "$/month"
        # Normalize unit to URDB expected values
        if "month" in unit.lower():
            charge_unit = "$/month"
        elif "day" in unit.lower():
            charge_unit = "$/day"
        else:
            charge_unit = unit
        result.append({
            "period": "All",
            "tier": 1,
            "charge": rate,
            "chargetype": charge_unit,
            "_label": c.get("charge_label"),
        })
    return result


def _build_energy_structure(
    charges: list[dict],
    is_tou: bool,
    state: str,
) -> tuple[list[list[dict]], list[list[int]], list[list[int]]]:
    """Build URDB energyratestructure + weekday/weekend schedule arrays.

    URDB energyratestructure is a 2D list [period_idx][tier_idx] of rate dicts.
    Weekday/weekend schedules are 12-month × 24-hour arrays where each cell
    contains the period_idx for that month/hour.

    For non-TOU (flat rate): one period (idx 0), all hours → period 0.
    For TOU (Duke NC): three periods: on_peak=0, off_peak=1, discount=2.
    """
    if not charges:
        return [], [], []

    if not is_tou:
        # Flat rate — group by season, build one period per season tier
        seasons = _group_by_season(charges)
        structure = []
        for season_charges in seasons.values():
            period_tiers = []
            for c in sorted(season_charges, key=lambda x: x.get("tier_min") or 0):
                tier_dict: dict[str, Any] = {
                    "rate": _normalize_rate(c),
                    "unit": "$/kWh",
                }
                if c.get("tier_max") is not None:
                    tier_dict["max"] = c["tier_max"]
                if c.get("tier_min") and c["tier_min"] > 0:
                    tier_dict["adj"] = True  # additional block
                period_tiers.append(tier_dict)
            structure.append(period_tiers)

        # For flat rate all months/hours map to period 0
        # (summer = months 5-9 per Duke NC convention, winter = rest)
        # Use two periods if summer/winter differ, else one
        if len(structure) == 2:
            wd = _flat_seasonal_schedule(summer_period=0, winter_period=1)
            we = _flat_seasonal_schedule(summer_period=0, winter_period=1)
        else:
            wd = _uniform_schedule(0)
            we = _uniform_schedule(0)
        return structure, wd, we

    else:
        # TOU rate — build period per tou_period value
        tou_order = ["on_peak", "off_peak", "discount"]
        by_period: dict[str, list[dict]] = {}
        for c in charges:
            p = c.get("tou_period") or "off_peak"
            by_period.setdefault(p, []).append(c)

        structure = []
        period_idx_map: dict[str, int] = {}
        for idx, period_name in enumerate(tou_order):
            if period_name not in by_period:
                continue
            period_idx_map[period_name] = len(structure)
            period_charges = sorted(by_period[period_name],
                                    key=lambda x: x.get("tier_min") or 0)
            period_tiers = [{"rate": _normalize_rate(c), "unit": "$/kWh"}
                            for c in period_charges]
            structure.append(period_tiers)

        # Build 12-month × 24-hour schedule using Duke NC TOU hour ranges
        wd = _build_tou_weekday_schedule(period_idx_map, state)
        we = _uniform_schedule(period_idx_map.get("off_peak", 0))
        return structure, wd, we


def _build_demand_structure(
    charges: list[dict],
) -> tuple[list[list[dict]], list[list[int]], list[list[int]]]:
    """Build URDB demandratestructure + schedules for demand charges."""
    if not charges:
        return [], [], []

    structure = []
    for c in charges:
        rate = _normalize_rate(c)
        tier: dict[str, Any] = {"rate": rate, "unit": "$/kW"}
        if c.get("tier_max") is not None:
            tier["max"] = c["tier_max"]
        structure.append([tier])

    wd = _uniform_schedule(0)
    we = _uniform_schedule(0)
    return structure, wd, we


# ---------------------------------------------------------------------------
# Schedule array builders
# ---------------------------------------------------------------------------

def _uniform_schedule(period_idx: int) -> list[list[int]]:
    """12 months × 24 hours, all pointing to period_idx."""
    return [[period_idx] * 24 for _ in range(12)]


def _flat_seasonal_schedule(summer_period: int, winter_period: int) -> list[list[int]]:
    """Duke NC seasons: summer = May–Sep (months 5–9), winter = rest."""
    schedule = []
    for month in range(1, 13):
        p = summer_period if 5 <= month <= 9 else winter_period
        schedule.append([p] * 24)
    return schedule


def _build_tou_weekday_schedule(
    period_idx_map: dict[str, int],
    state: str,
) -> list[list[int]]:
    """Build a 12-month × 24-hour weekday schedule for Duke NC TOU.

    All months use the same hour assignments for Duke NC (no seasonal TOU
    variation — on-peak hours are constant year-round).
    """
    on_idx  = period_idx_map.get("on_peak", 0)
    off_idx = period_idx_map.get("off_peak", 0)
    disc_idx = period_idx_map.get("discount", off_idx)

    # Build one 24-hour array for a weekday
    # Duke NC: on_peak 14-21, discount 0-6 + 21-24, off_peak 6-14
    hour_map = []
    for h in range(24):
        if 14 <= h < 21:
            hour_map.append(on_idx)
        elif h < 6 or h >= 21:
            hour_map.append(disc_idx)
        else:
            hour_map.append(off_idx)

    return [list(hour_map) for _ in range(12)]


# ---------------------------------------------------------------------------
# Rate normalization helpers
# ---------------------------------------------------------------------------

def _normalize_rate(charge: dict) -> float | None:
    """Convert cents/kWh → $/kWh if needed; return None if rate missing."""
    rate = charge.get("rate_value")
    if rate is None:
        return None
    unit = (charge.get("rate_unit") or "").lower()
    if "cents" in unit:
        return round(rate / 100, 6)
    return rate


def _group_by_season(charges: list[dict]) -> dict[str, list[dict]]:
    """Group charges by season, combining all_year into both."""
    seasons: dict[str, list[dict]] = {}
    for c in charges:
        s = c.get("season") or "all_year"
        if s == "all_year":
            seasons.setdefault("summer", []).append(c)
            seasons.setdefault("winter", []).append(c)
        else:
            seasons.setdefault(s, []).append(c)
    # Deduplicate: if summer == winter, collapse to one period
    if (seasons.get("summer") == seasons.get("winter")
            and "summer" in seasons and "winter" in seasons):
        return {"all_year": seasons["summer"]}
    return seasons


# ---------------------------------------------------------------------------
# Quality / curation helpers
# ---------------------------------------------------------------------------

def _infer_sector(family: dict, charges: list[dict]) -> str | None:
    """Infer URDB sector from customer_class on charges or title."""
    classes = {c.get("customer_class") for c in charges if c.get("customer_class")}
    if classes:
        # Use most common
        primary = next(iter(classes))
        return _SECTOR_MAP.get(primary.lower(), primary.title())
    title = (family.get("title") or "").lower()
    if "residential" in title:
        return "Residential"
    if "lighting" in title:
        return "Lighting"
    if "industrial" in title:
        return "Industrial"
    if "general service" in title or "commercial" in title:
        return "Commercial"
    return None


def _check_missing(family: dict, version: dict, charges: list[dict]) -> list[str]:
    missing = []
    if not family.get("state"):
        missing.append("state")
    if not family.get("schedule_code"):
        missing.append("schedule_code")
    if not version.get("effective_start"):
        missing.append("effective_start")
    if not any(c["charge_type"] in ("energy_block", "tou_energy") for c in charges):
        missing.append("energy_charges")
    return missing


def _curation_notes(
    family: dict,
    version: dict,
    charges: list[dict],
    rider_keys: list[str],
) -> list[str]:
    notes = [
        "Curation aid only — human review required before URDB submission.",
        "Verify effective dates and source URL against the official Duke tariff leaf.",
    ]
    if rider_keys:
        notes.append(
            f"{len(rider_keys)} rider(s) listed in _rider_family_keys. "
            "URDB does not support rider references directly — adjust rates accordingly "
            "or note in the URDB description."
        )
    if any((c.get("confidence_score") or 0) < 0.8 for c in charges):
        notes.append(
            "Some charges have confidence < 0.80 — cross-check against source PDF."
        )
    is_tou = any(c.get("tou_period") for c in charges)
    if is_tou:
        notes.append(
            "TOU schedule: weekday period map uses Duke NC standard hours "
            "(on-peak 2-9 PM, discount midnight-6 AM + 9 PM-midnight, off-peak remainder). "
            "Verify these match the schedule version being submitted."
        )
    return notes
