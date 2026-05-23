"""NC Document Gap Audit — identify where higher-quality source documents could
replace or supplement thin historical coverage.

Three complementary signals are combined into a ranked opportunity list:

1. **Temporal gap** — consecutive versions of the same family are more than
   ``GAP_THRESHOLD_DAYS`` apart with no carry-forward explanation.  One
   compliance-bundle filing typically covers *all* schedules for that period,
   so a gap in RS implies the same docket-period gap exists in SGS/LGS/I/PG/ES.

2. **Ordinal gap** — the ``revision_label`` field encodes ordinal revision
   numbers ("NC Forty-Fifth Revised Leaf No. 60").  The gap between the current
   revision ordinal and the number of versions we hold estimates how many tariff
   revisions are unrepresented.  High ordinal + few versions = large opportunity.

3. **Source-quality floor** — a version whose charge count is below the family
   peak AND whose source_type is a thin single-sheet source (historical_document,
   utility_current) when a compliance bundle contemporaneous with that period
   exists elsewhere in the DB.

Outputs:
    nc_document_gap_audit_rows.csv  — one row per (family, gap opportunity)
    nc_document_gap_audit.md        — human-readable ranked summary
    nc_document_gap_audit.json      — full structured report

CLI: ``python -m duke_rates export nc-document-gap-audit``
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

_DEFAULT_OUTPUT_DIR = Path("docs/reports/nc_document_gap_audit")

# Gaps longer than this between consecutive versions are flagged as temporal gaps.
_GAP_THRESHOLD_DAYS = 548  # ~18 months

# Source types ranked by quality (higher = better).
_SOURCE_QUALITY: dict[str, int] = {
    "historical_document": 5,   # individual targeted sheet from a named docket
    "regulator": 4,             # NCUC portal regulator filing
    "compliance_bundle": 3,     # multi-schedule compliance tariff book
    "historical": 3,
    "historical_documents": 2,  # bulk docling-bridge scrape
    "ncuc_mined": 2,
    "utility_current": 1,       # snapshot of utility website
    "docling_bridge": 1,
}

# Thin source types — versions from these alone warrant a quality-floor flag.
_THIN_SOURCES: frozenset[str] = frozenset({
    "utility_current",
    "historical_documents",
    "docling_bridge",
    "ncuc_mined",
})

# Map English ordinals to integers for revision-label parsing.
_ORDINAL_MAP: dict[str, int] = {
    "original": 0,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20,
    "twenty-first": 21, "twenty-second": 22, "twenty-third": 23,
    "twenty-fourth": 24, "twenty-fifth": 25, "twenty-sixth": 26,
    "twenty-seventh": 27, "twenty-eighth": 28, "twenty-ninth": 29,
    "thirtieth": 30,
    "thirty-first": 31, "thirty-second": 32, "thirty-third": 33,
    "thirty-fourth": 34, "thirty-fifth": 35, "thirty-sixth": 36,
    "thirty-seventh": 37, "thirty-eighth": 38, "thirty-ninth": 39,
    "fortieth": 40,
    "forty-first": 41, "forty-second": 42, "forty-third": 43,
    "forty-fourth": 44, "forty-fifth": 45, "forty-sixth": 46,
    "forty-seventh": 47, "forty-eighth": 48, "forty-ninth": 49,
    "fiftieth": 50,
    "fifty-first": 51, "fifty-second": 52, "fifty-third": 53,
    "fifty-fourth": 54, "fifty-fifth": 55, "fifty-sixth": 56,
    "fifty-seventh": 57, "fifty-eighth": 58, "fifty-ninth": 59,
    "sixtieth": 60,
    "sixty-first": 61, "sixty-second": 62, "sixty-third": 63,
    "sixty-fourth": 64, "sixty-fifth": 65, "sixty-sixth": 66,
    "sixty-seventh": 67, "sixty-eighth": 68, "sixty-ninth": 69,
    "seventieth": 70,
}

# Known DEC docket → effective_start mapping so we can suggest which docket
# sub-number might cover an unrepresented period.
_DEC_KNOWN_DOCKETS: list[tuple[str, str]] = [
    # (docket_label, approx_effective_date)  — add as discovered
    ("E-7 Sub 1058", "2014-07-01"),
    ("E-7 Sub 1152", "2018-12-01"),
    ("E-7 Sub 1214", "2021-12-16"),
]

# DEP (Progress) known compliance bundles
_DEP_KNOWN_DOCKETS: list[tuple[str, str]] = [
    ("E-2 Sub 1044", "2015-12-01"),
    ("E-2 Sub 1108", "2017-01-01"),
    ("E-2 Sub 1142", "2018-03-16"),
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GapOpportunity:
    """One detected gap opportunity for a single tariff family."""
    utility: str                        # DEP | DEC
    schedule_label: str                 # RS, SGS, leaf-500, etc.
    family_key: str
    gap_type: str                       # temporal | ordinal | quality_floor | cross_schedule
    priority_score: int                 # 0–100, higher = more valuable to address
    gap_start: str | None               # ISO date — start of uncovered period
    gap_end: str | None                 # ISO date — end of uncovered period
    gap_days: int | None                # length of temporal gap
    revision_current: int | None        # highest ordinal we parsed from revision_label
    revision_have: int                  # number of versions we hold
    revision_missing_est: int | None    # estimated missing revisions
    source_type_affected: str | None    # source_type of the thin/sparse version
    charge_count_affected: int | None   # charge count of the thin version
    family_peak_charges: int            # best charge count in this family
    suggested_action: str              # what to look for
    suggested_docket: str | None        # nearest known docket label if estimable
    note: str                           # human-readable explanation


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_all_nc_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Load all NC tariff versions with charge counts and revision info."""
    return conn.execute(
        """
        SELECT
            tv.id              AS version_id,
            tv.family_key,
            tv.effective_start,
            tv.effective_end,
            tv.source_type,
            tv.revision_label,
            tv.supersedes_label,
            tv.docket_number,
            tv.historical_document_id,
            hd.title           AS historical_title,
            hd.local_path      AS historical_local_path,
            hd.start_page,
            hd.end_page,
            tf.company,
            tf.title           AS family_title,
            COUNT(tc.id)       AS charge_count
        FROM tariff_versions tv
        JOIN tariff_families tf ON tf.family_key = tv.family_key
        LEFT JOIN historical_documents hd ON hd.id = tv.historical_document_id
        LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
        WHERE tf.state = 'NC'
        AND tf.family_type IN ('rate_schedule', 'rider')
        AND tv.effective_start IS NOT NULL
        GROUP BY tv.id
        ORDER BY tv.family_key, tv.effective_start
        """
    ).fetchall()


def _load_family_metadata(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Return family_key → family row for all NC families."""
    rows = conn.execute(
        "SELECT * FROM tariff_families WHERE state = 'NC'"
    ).fetchall()
    return {str(r["family_key"]): r for r in rows}


# ---------------------------------------------------------------------------
# Ordinal parsing
# ---------------------------------------------------------------------------

def _parse_revision_ordinal(label: str | None) -> int | None:
    """Extract the integer revision number from a revision_label string.

    Examples:
        "NC Forty-Fifth Revised Leaf No. 60"  -> 45
        "NC Original Leaf No. 328"             -> 0
        "NC Seventeenth Revised Leaf No. 68"  -> 17
    """
    if not label:
        return None
    lower = label.lower()
    # Try longest match first to avoid "first" matching inside "forty-first"
    for word, num in sorted(_ORDINAL_MAP.items(), key=lambda kv: -len(kv[0])):
        if word in lower:
            return num
    return None


def _leaf_no_from_label(label: str | None) -> str | None:
    """Extract leaf number string from revision_label, e.g. '60' from 'Leaf No. 60'."""
    if not label:
        return None
    m = re.search(r"leaf\s+no\.?\s+(\d+)", label, re.IGNORECASE)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Docket suggestion
# ---------------------------------------------------------------------------

def _nearest_docket(
    utility: str,
    gap_start: str,
    gap_end: str,
) -> str | None:
    """Return the label of a known docket whose effective date falls inside or
    just before the gap, suggesting it might cover the missing period."""
    dockets = _DEC_KNOWN_DOCKETS if utility == "DEC" else _DEP_KNOWN_DOCKETS
    best: tuple[str, str] | None = None
    for label, eff in dockets:
        if gap_start <= eff <= gap_end:
            best = (label, eff)
    if best:
        return best[0]
    # Check if there's a docket just before the gap that might have amendments
    candidates = [(label, eff) for label, eff in dockets if eff < gap_start]
    if candidates:
        return max(candidates, key=lambda x: x[1])[0]
    return None


# ---------------------------------------------------------------------------
# Utility / schedule extraction
# ---------------------------------------------------------------------------

def _parse_utility_and_label(family_key: str, company: str | None) -> tuple[str, str]:
    """Return (utility_short, schedule_label) from family_key and company."""
    if "carolinas" in family_key:
        utility = "DEC"
    elif "progress" in family_key:
        utility = "DEP"
    else:
        utility = "NC"

    # Extract schedule label from family_key suffix
    # nc-carolinas-schedule-SGS  -> SGS
    # nc-progress-leaf-501       -> leaf-501
    # nc-carolinas-rider-FCAR    -> FCAR
    m = re.search(
        r"nc-(?:carolinas|progress)-(?:schedule|rider|leaf)-(.+)$",
        family_key,
    )
    label = m.group(1) if m else family_key
    return utility, label


# ---------------------------------------------------------------------------
# Core gap detection
# ---------------------------------------------------------------------------

def _detect_temporal_gaps(
    versions: list[sqlite3.Row],
    utility: str,
    schedule_label: str,
    family_key: str,
    family_peak: int,
) -> list[GapOpportunity]:
    """Detect gaps > GAP_THRESHOLD_DAYS between consecutive versions."""
    opportunities: list[GapOpportunity] = []
    dated = [v for v in versions if v["effective_start"]]
    dated.sort(key=lambda v: str(v["effective_start"]))

    for i in range(len(dated) - 1):
        a = dated[i]
        b = dated[i + 1]
        try:
            dt_a = datetime.fromisoformat(str(a["effective_start"]))
            dt_b = datetime.fromisoformat(str(b["effective_start"]))
        except ValueError:
            continue
        gap_days = (dt_b - dt_a).days
        if gap_days < _GAP_THRESHOLD_DAYS:
            continue

        gap_start = str(a["effective_start"])
        gap_end = str(b["effective_start"])
        suggested = _nearest_docket(utility, gap_start, gap_end)

        # Score: longer gap + more important family = higher priority
        # Base: years of gap × 10, capped at 40
        gap_years = gap_days / 365
        score = min(40, int(gap_years * 10))
        # Bonus: residential/commercial core schedules
        if schedule_label in ("RS", "RES", "SGS", "ES", "leaf-500", "leaf-520"):
            score += 20
        elif schedule_label in ("LGS", "I", "PG", "TS", "leaf-532", "leaf-533"):
            score += 15
        elif "leaf-50" in schedule_label:
            score += 10

        opportunities.append(GapOpportunity(
            utility=utility,
            schedule_label=schedule_label,
            family_key=family_key,
            gap_type="temporal",
            priority_score=min(100, score),
            gap_start=gap_start,
            gap_end=gap_end,
            gap_days=gap_days,
            revision_current=None,
            revision_have=len(versions),
            revision_missing_est=None,
            source_type_affected=str(a["source_type"] or ""),
            charge_count_affected=int(a["charge_count"] or 0),
            family_peak_charges=family_peak,
            suggested_action="search_ncuc_portal_for_docket",
            suggested_docket=suggested,
            note=(
                f"{gap_years:.1f}-year gap between {gap_start} ({a['source_type']}) "
                f"and {gap_end} ({b['source_type']}). "
                + (f"Nearest known docket: {suggested}." if suggested else
                   "No known docket in range — search portal by schedule name and year.")
            ),
        ))
    return opportunities


def _detect_ordinal_gap(
    versions: list[sqlite3.Row],
    utility: str,
    schedule_label: str,
    family_key: str,
    family_peak: int,
) -> GapOpportunity | None:
    """Detect when the current revision ordinal >> versions we hold."""
    # Find highest revision ordinal across all versions
    max_ordinal: int | None = None
    max_label: str | None = None
    for v in versions:
        n = _parse_revision_ordinal(str(v["revision_label"] or ""))
        if n is not None and (max_ordinal is None or n > max_ordinal):
            max_ordinal = n
            max_label = str(v["revision_label"])

    if max_ordinal is None or max_ordinal < 5:
        # Not enough ordinal signal (Original / First / Second = likely new schedule)
        return None

    versions_held = len(versions)
    # Estimate missing: ordinal is 0-based revision count, versions_held counts
    # distinct DB entries.  Missing = ordinal + 1 - versions_held (approx).
    missing_est = max(0, (max_ordinal + 1) - versions_held)

    if missing_est < 3:
        return None  # Not worth flagging — well covered

    # Score: missing revisions × 3, capped at 50; bonus for core schedules
    score = min(50, missing_est * 3)
    if schedule_label in ("RS", "RES", "SGS", "ES", "leaf-500", "leaf-520"):
        score += 20
    elif schedule_label in ("LGS", "I", "PG", "TS", "leaf-532", "leaf-533"):
        score += 15
    elif "FCAR" in schedule_label or "NM" in schedule_label:
        score += 10

    leaf_no = _leaf_no_from_label(max_label)
    return GapOpportunity(
        utility=utility,
        schedule_label=schedule_label,
        family_key=family_key,
        gap_type="ordinal",
        priority_score=min(100, score),
        gap_start=None,
        gap_end=None,
        gap_days=None,
        revision_current=max_ordinal,
        revision_have=versions_held,
        revision_missing_est=missing_est,
        source_type_affected=None,
        charge_count_affected=None,
        family_peak_charges=family_peak,
        suggested_action="search_ncuc_portal_by_leaf_number",
        suggested_docket=f"Leaf No. {leaf_no}" if leaf_no else None,
        note=(
            f"Current revision is {max_label!r} (ordinal {max_ordinal}) "
            f"but we hold only {versions_held} version(s). "
            f"~{missing_est} revisions unrepresented. "
            + (f"Search NCUC portal for Leaf No. {leaf_no} history." if leaf_no else
               "Search NCUC portal for historical tariff leaves.")
        ),
    )


def _detect_quality_floor(
    versions: list[sqlite3.Row],
    utility: str,
    schedule_label: str,
    family_key: str,
    family_peak: int,
) -> list[GapOpportunity]:
    """Flag versions where source_type is thin and charge_count is well below peak."""
    if family_peak <= 0:
        return []
    opportunities: list[GapOpportunity] = []
    for v in versions:
        src = str(v["source_type"] or "")
        charges = int(v["charge_count"] or 0)
        if src not in _THIN_SOURCES:
            continue
        if charges <= 0:
            continue  # zero-charge is handled by document_intelligence_audit
        # Sparse: < 50% of family peak from a thin source
        if charges >= family_peak * 0.5:
            continue

        score = min(40, int((1 - charges / family_peak) * 40))
        if schedule_label in ("RS", "RES", "SGS", "ES", "leaf-500", "leaf-520"):
            score += 15
        elif schedule_label in ("LGS", "I", "PG", "TS"):
            score += 10

        opportunities.append(GapOpportunity(
            utility=utility,
            schedule_label=schedule_label,
            family_key=family_key,
            gap_type="quality_floor",
            priority_score=min(100, score),
            gap_start=str(v["effective_start"] or ""),
            gap_end=str(v["effective_end"] or ""),
            gap_days=None,
            revision_current=None,
            revision_have=len(versions),
            revision_missing_est=None,
            source_type_affected=src,
            charge_count_affected=charges,
            family_peak_charges=family_peak,
            suggested_action="locate_compliance_bundle_for_period",
            suggested_docket=None,
            note=(
                f"Version {v['effective_start']} from thin source '{src}' has "
                f"{charges} charges vs family peak {family_peak}. "
                f"A compliance bundle or historical leaf for this period "
                f"may contain the complete rate table."
            ),
        ))
    return opportunities


# ---------------------------------------------------------------------------
# Cross-schedule correlation
# ---------------------------------------------------------------------------

def _build_cross_schedule_index(
    opportunities: list[GapOpportunity],
) -> dict[tuple[str, str, str], list[str]]:
    """Return a mapping of (utility, gap_start, gap_end) → [schedule_labels]
    for temporal gaps, to show which schedules share the same docket gap."""
    index: dict[tuple[str, str, str], list[str]] = {}
    for opp in opportunities:
        if opp.gap_type != "temporal" or not opp.gap_start or not opp.gap_end:
            continue
        key = (opp.utility, opp.gap_start, opp.gap_end)
        index.setdefault(key, []).append(opp.schedule_label)
    return index


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

def build_nc_document_gap_audit(
    database_path: Path | None = None,
) -> dict[str, object]:
    conn = _connect(database_path)
    try:
        all_versions = _load_all_nc_versions(conn)
        family_meta = _load_family_metadata(conn)
    finally:
        conn.close()

    # Group versions by family_key
    by_family: dict[str, list[sqlite3.Row]] = {}
    for v in all_versions:
        fk = str(v["family_key"])
        by_family.setdefault(fk, []).append(v)

    opportunities: list[GapOpportunity] = []

    for family_key, versions in by_family.items():
        if not versions:
            continue
        meta = family_meta.get(family_key)
        company = str(meta["company"] or "") if meta else ""
        utility, schedule_label = _parse_utility_and_label(family_key, company)

        family_peak = max(int(v["charge_count"] or 0) for v in versions)

        # Signal 1: temporal gaps
        opportunities.extend(
            _detect_temporal_gaps(versions, utility, schedule_label, family_key, family_peak)
        )

        # Signal 2: ordinal gap
        ordinal_opp = _detect_ordinal_gap(versions, utility, schedule_label, family_key, family_peak)
        if ordinal_opp:
            opportunities.append(ordinal_opp)

        # Signal 3: quality floor
        opportunities.extend(
            _detect_quality_floor(versions, utility, schedule_label, family_key, family_peak)
        )

    # Build cross-schedule index and annotate temporal gaps
    cross_index = _build_cross_schedule_index(opportunities)
    for opp in opportunities:
        if opp.gap_type == "temporal" and opp.gap_start and opp.gap_end:
            key = (opp.utility, opp.gap_start, opp.gap_end)
            co_schedules = cross_index.get(key, [])
            if len(co_schedules) > 1:
                # Upgrade priority: one docket covers all these schedules
                opp.priority_score = min(100, opp.priority_score + 10 * len(co_schedules))
                others = [s for s in co_schedules if s != opp.schedule_label]
                opp.note += (
                    f" SAME GAP appears in {len(co_schedules)} schedules "
                    f"({', '.join(sorted(co_schedules))}): one docket filing covers all."
                )

    # Sort: priority descending, then utility, then schedule, then gap_start
    opportunities.sort(
        key=lambda o: (
            -o.priority_score,
            o.utility,
            o.schedule_label,
            o.gap_start or "",
        )
    )

    # Summarise by gap_type
    type_counts: dict[str, int] = {}
    for opp in opportunities:
        type_counts[opp.gap_type] = type_counts.get(opp.gap_type, 0) + 1

    # Cross-schedule summary: unique docket opportunities (temporal gaps shared across 2+ schedules)
    unique_docket_gaps: dict[tuple[str, str, str], list[str]] = {}
    for opp in opportunities:
        if opp.gap_type == "temporal" and opp.gap_start and opp.gap_end:
            key = (opp.utility, opp.gap_start, opp.gap_end)
            unique_docket_gaps.setdefault(key, []).append(opp.schedule_label)
    multi_schedule_gaps = {k: v for k, v in unique_docket_gaps.items() if len(v) >= 2}

    rows = [_opp_to_dict(o) for o in opportunities]

    return {
        "generated_at": date.today().isoformat(),
        "total_opportunities": len(opportunities),
        "gap_type_counts": type_counts,
        "multi_schedule_docket_gaps": len(multi_schedule_gaps),
        "multi_schedule_gap_detail": {
            f"{k[0]} {k[1]} → {k[2]}": sorted(v)
            for k, v in sorted(multi_schedule_gaps.items())
        },
        "rows": rows,
    }


def _opp_to_dict(o: GapOpportunity) -> dict[str, object]:
    return {
        "priority_score": o.priority_score,
        "utility": o.utility,
        "schedule_label": o.schedule_label,
        "family_key": o.family_key,
        "gap_type": o.gap_type,
        "gap_start": o.gap_start,
        "gap_end": o.gap_end,
        "gap_days": o.gap_days,
        "revision_current": o.revision_current,
        "revision_have": o.revision_have,
        "revision_missing_est": o.revision_missing_est,
        "source_type_affected": o.source_type_affected,
        "charge_count_affected": o.charge_count_affected,
        "family_peak_charges": o.family_peak_charges,
        "suggested_action": o.suggested_action,
        "suggested_docket": o.suggested_docket,
        "note": o.note,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_nc_document_gap_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_nc_document_gap_audit(database_path)

    rows_csv = output_dir / "nc_document_gap_audit_rows.csv"
    summary_json = output_dir / "nc_document_gap_audit.json"
    markdown_path = output_dir / "nc_document_gap_audit.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _write_csv(path: Path, rows: object) -> None:
    items = list(rows)  # type: ignore[arg-type]
    if not items:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(items[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(items)


def _render_markdown(report: dict[str, object]) -> str:
    rows: list[dict[str, object]] = list(report["rows"])  # type: ignore[arg-type]
    multi: dict[str, list[str]] = dict(report["multi_schedule_gap_detail"])  # type: ignore[arg-type]

    lines = [
        "# NC Document Gap Audit",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "Identifies where a higher-quality NCUC document (compliance bundle, tariff book,",
        "or targeted docket filing) could fill temporal gaps, represent untracked revisions,",
        "or replace thin single-sheet sources with more complete rate tables.",
        "",
        "## Summary",
        "",
        f"- Total opportunities: **{report['total_opportunities']}**",
        f"- Multi-schedule docket gaps (one filing covers N schedules): **{report['multi_schedule_docket_gaps']}**",
        "",
        "Gap type breakdown:",
    ]
    for gtype, cnt in sorted(dict(report["gap_type_counts"]).items()):  # type: ignore[arg-type]
        lines.append(f"- `{gtype}`: {cnt}")

    if multi:
        lines.extend(["", "## High-Value Docket Opportunities", "",
                       "Each row below represents a single NCUC docket filing that would",
                       "simultaneously cover multiple schedule families:", ""])
        lines.append("| Utility | Gap Period | Schedules Covered | Suggested Docket |")
        lines.append("|---|---|---|---|")
        for period_key, schedules in sorted(multi.items()):
            # period_key is like "DEC 2016-01-01 → 2018-12-01"
            parts = period_key.split(" ", 1)
            util = parts[0]
            period = parts[1] if len(parts) > 1 else period_key
            # Find suggested docket from first matching temporal row
            docket = ""
            for r in rows:
                if (r["utility"] == util and r["gap_type"] == "temporal"
                        and r.get("suggested_docket")):
                    gs = r.get("gap_start", "")
                    ge = r.get("gap_end", "")
                    if gs and ge and f"{gs} → {ge}" in period_key:
                        docket = str(r["suggested_docket"])
                        break
            lines.append(
                f"| {util} | {period} | {', '.join(schedules)} | {docket or '—'} |"
            )

    lines.extend(["", "## Top Opportunities (by priority score)", ""])
    top = rows[:40]
    lines.append(
        "| Score | Utility | Schedule | Gap Type | Gap Start | Gap End | Gap Days | "
        "Rev Current | Rev Have | Missing Est | Source | Charges | Peak | Suggested Action |"
    )
    lines.append("|---:|---|---|---|---|---|---:|---:|---:|---:|---|---:|---:|---|")
    for r in top:
        lines.append(
            f"| {r['priority_score']} "
            f"| {r['utility']} "
            f"| {r['schedule_label']} "
            f"| {r['gap_type']} "
            f"| {r['gap_start'] or '—'} "
            f"| {r['gap_end'] or '—'} "
            f"| {r['gap_days'] or '—'} "
            f"| {r['revision_current'] if r['revision_current'] is not None else '—'} "
            f"| {r['revision_have']} "
            f"| {r['revision_missing_est'] if r['revision_missing_est'] is not None else '—'} "
            f"| {r['source_type_affected'] or '—'} "
            f"| {r['charge_count_affected'] if r['charge_count_affected'] is not None else '—'} "
            f"| {r['family_peak_charges']} "
            f"| {r['suggested_action']} |"
        )

    lines.extend(["", "## Notes", "",
                   "> **temporal** — consecutive versions >18 months apart; likely an intermediate docket filing exists.",
                   "> **ordinal** — current revision ordinal >> versions held; many rate changes not represented.",
                   "> **quality_floor** — existing version from thin source with <50% of family peak charges; a compliance bundle may contain the full rate table.",
                   ""])
    lines.extend(["", "## Opportunity Notes", ""])
    for r in top:
        lines.append(
            f"- **{r['utility']} {r['schedule_label']}** ({r['gap_type']}, score={r['priority_score']}): "
            f"{r['note']}"
        )

    return "\n".join(lines)


__all__ = [
    "build_nc_document_gap_audit",
    "export_nc_document_gap_audit",
    "_DEFAULT_OUTPUT_DIR",
]
