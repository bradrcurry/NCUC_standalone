from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class CoverageFamily:
    utility: str
    label: str
    family_key: str
    full_threshold: int
    partial_threshold: int


_DEP_FAMILIES: tuple[CoverageFamily, ...] = (
    CoverageFamily("DEP", "RES", "nc-progress-leaf-500", full_threshold=5, partial_threshold=4),
    CoverageFamily("DEP", "R-TOUD", "nc-progress-leaf-501", full_threshold=30, partial_threshold=6),
    CoverageFamily("DEP", "R-TOU", "nc-progress-leaf-502", full_threshold=30, partial_threshold=3),
    CoverageFamily("DEP", "R-TOU-CPP", "nc-progress-leaf-503", full_threshold=5, partial_threshold=4),
    CoverageFamily("DEP", "SGS", "nc-progress-leaf-520", full_threshold=20, partial_threshold=5),
    CoverageFamily("DEP", "SGS-TOUE", "nc-progress-leaf-521", full_threshold=30, partial_threshold=8),
    CoverageFamily("DEP", "LGS", "nc-progress-leaf-532", full_threshold=30, partial_threshold=6),
    CoverageFamily("DEP", "LGS-TOU", "nc-progress-leaf-533", full_threshold=30, partial_threshold=7),
)

_DEC_FAMILIES: tuple[CoverageFamily, ...] = (
    CoverageFamily("DEC", "RS", "nc-carolinas-schedule-RS", full_threshold=6, partial_threshold=2),
    CoverageFamily("DEC", "SGS", "nc-carolinas-schedule-SGS", full_threshold=8, partial_threshold=5),
    CoverageFamily("DEC", "LGS", "nc-carolinas-schedule-LGS", full_threshold=8, partial_threshold=5),
    CoverageFamily("DEC", "ES", "nc-carolinas-schedule-ES", full_threshold=8, partial_threshold=3),
    CoverageFamily("DEC", "I", "nc-carolinas-schedule-I", full_threshold=8, partial_threshold=5),
    CoverageFamily("DEC", "PG", "nc-carolinas-schedule-PG", full_threshold=5, partial_threshold=3),
    CoverageFamily("DEC", "TS", "nc-carolinas-schedule-TS", full_threshold=5, partial_threshold=3),
)

_DEFAULT_OUTPUT_DIR = Path("docs/reports/nc_coverage_assessment")
_SOURCE_PRIORITY = {
    "historical_document": 5,
    "regulator": 4,
    "compliance_bundle": 3,
    "historical": 3,
    "utility_current": 2,
}


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _pick_best_version(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    if not rows:
        return None
    latest_effective_start = max(str(row["effective_start"] or "") for row in rows)
    candidates = [row for row in rows if str(row["effective_start"] or "") == latest_effective_start]
    return sorted(
        candidates,
        key=lambda row: (
            int(row["charge_count"] or 0),
            _SOURCE_PRIORITY.get((row["source_type"] or "").lower(), 0),
            1 if row["historical_document_id"] is not None else 0,
        ),
        reverse=True,
    )[0]


def _quality_symbol(charge_count: int, *, full_threshold: int, partial_threshold: int) -> str:
    if charge_count <= 0:
        return "X"
    if charge_count >= full_threshold:
        return "F"
    if charge_count >= partial_threshold:
        return "P"
    return "p"


def _carry_forward_label(selected_start_year: int | None, target_year: int, symbol: str) -> str:
    if selected_start_year is None or selected_start_year == target_year:
        return symbol
    yy = str(selected_start_year)[-2:]
    return f"(={yy})"


def get_nc_coverage_families() -> dict[str, tuple[CoverageFamily, ...]]:
    return {
        "dep": _DEP_FAMILIES,
        "dec": _DEC_FAMILIES,
    }


def build_nc_coverage_assessment(
    database_path: Path | None = None,
    *,
    dep_years: range = range(2015, 2026),
    dec_years: range = range(2013, 2026),
) -> dict[str, object]:
    conn = _connect(database_path)
    try:
        dep_rows = _build_rows_for_families(conn, _DEP_FAMILIES, dep_years)
        dec_rows = _build_rows_for_families(conn, _DEC_FAMILIES, dec_years)
        inventory_scope = _build_inventory_scope_summary(conn)
    finally:
        conn.close()

    dep_matrix = _matrix_from_rows(dep_rows, dep_years)
    dec_matrix = _matrix_from_rows(dec_rows, dec_years)
    summary = {
        "generated_at": date.today().isoformat(),
        "dep_years": list(dep_years),
        "dec_years": list(dec_years),
        "dep_rows": dep_rows,
        "dec_rows": dec_rows,
        "dep_matrix": dep_matrix,
        "dec_matrix": dec_matrix,
        "inventory_scope": inventory_scope,
    }
    return summary


def export_nc_coverage_assessment(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_nc_coverage_assessment(database_path)

    dep_csv = output_dir / "dep_coverage_cells.csv"
    dec_csv = output_dir / "dec_coverage_cells.csv"
    summary_json = output_dir / "nc_coverage_assessment_summary.json"
    markdown_path = output_dir / "nc_coverage_assessment.md"

    _write_csv(dep_csv, report["dep_rows"])
    _write_csv(dec_csv, report["dec_rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "dep_csv": dep_csv,
        "dec_csv": dec_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _build_rows_for_families(
    conn: sqlite3.Connection,
    families: tuple[CoverageFamily, ...],
    years: range,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for family in families:
        for year in years:
            as_of = date(year, 7, 1).isoformat()
            versions = conn.execute(
                """
                SELECT
                    tv.id,
                    tv.family_key,
                    tv.effective_start,
                    tv.effective_end,
                    tv.source_type,
                    tv.historical_document_id,
                    COUNT(tc.id) AS charge_count
                FROM tariff_versions tv
                LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
                WHERE tv.family_key = ?
                  AND tv.effective_start IS NOT NULL
                  AND tv.effective_start <= ?
                  AND (tv.effective_end IS NULL OR tv.effective_end >= ?)
                GROUP BY tv.id
                ORDER BY tv.effective_start
                """,
                (family.family_key, as_of, as_of),
            ).fetchall()
            selected = _pick_best_version(versions)
            if selected is None:
                rows.append(
                    {
                        "utility": family.utility,
                        "schedule_label": family.label,
                        "family_key": family.family_key,
                        "target_year": year,
                        "as_of_date": as_of,
                        "display": "—",
                        "quality_symbol": "—",
                        "charge_count": 0,
                        "selected_version_id": None,
                        "selected_effective_start": None,
                        "selected_source_type": None,
                        "selected_historical_document_id": None,
                        "is_carried_forward": False,
                    }
                )
                continue

            charge_count = int(selected["charge_count"] or 0)
            symbol = _quality_symbol(
                charge_count,
                full_threshold=family.full_threshold,
                partial_threshold=family.partial_threshold,
            )
            selected_start = selected["effective_start"]
            selected_start_year = int(str(selected_start)[:4]) if selected_start else None
            display = _carry_forward_label(selected_start_year, year, symbol)
            rows.append(
                {
                    "utility": family.utility,
                    "schedule_label": family.label,
                    "family_key": family.family_key,
                    "target_year": year,
                    "as_of_date": as_of,
                    "display": display,
                    "quality_symbol": symbol,
                    "charge_count": charge_count,
                    "selected_version_id": int(selected["id"]),
                    "selected_effective_start": selected_start,
                    "selected_source_type": selected["source_type"],
                    "selected_historical_document_id": selected["historical_document_id"],
                    "is_carried_forward": bool(selected_start_year and selected_start_year != year),
                }
            )
    return rows


def _matrix_from_rows(rows: list[dict[str, object]], years: range) -> list[dict[str, object]]:
    by_schedule: dict[str, dict[int, str]] = {}
    family_key_by_schedule: dict[str, str] = {}
    for row in rows:
        label = str(row["schedule_label"])
        by_schedule.setdefault(label, {})
        by_schedule[label][int(row["target_year"])] = str(row["display"])
        family_key_by_schedule[label] = str(row["family_key"])

    matrix = []
    for label, year_map in by_schedule.items():
        item: dict[str, object] = {
            "schedule_label": label,
            "family_key": family_key_by_schedule[label],
        }
        for year in years:
            item[str(year)] = year_map.get(year, "—")
        matrix.append(item)
    return matrix


def _build_inventory_scope_summary(conn: sqlite3.Connection) -> dict[str, object]:
    matrix_scope = {
        family.family_key
        for families in get_nc_coverage_families().values()
        for family in families
    }
    rows = conn.execute(
        """
        SELECT
            tf.family_key,
            tf.company,
            tf.title,
            COUNT(DISTINCT tv.id) AS version_count,
            COUNT(DISTINCT CASE WHEN vcs.charge_count > 0 THEN tv.id END) AS versions_with_charges
        FROM tariff_families tf
        LEFT JOIN tariff_versions tv
          ON tv.family_key = tf.family_key
        LEFT JOIN v_version_charge_summary vcs
          ON vcs.version_id = tv.id
        WHERE tf.state = 'NC'
          AND LOWER(tf.company) IN ('progress', 'carolinas')
          AND tf.family_type = 'rate_schedule'
        GROUP BY tf.family_key, tf.company, tf.title
        ORDER BY tf.company, tf.family_key
        """
    ).fetchall()

    total_families = len(rows)
    core_missing: list[dict[str, object]] = []
    legacy_families: list[dict[str, object]] = []
    for row in rows:
        family_key = str(row["family_key"])
        versions_with_charges = int(row["versions_with_charges"] or 0)
        if family_key not in matrix_scope and _is_legacy_family(family_key):
            legacy_families.append(
                {
                    "utility": "DEP" if str(row["company"]).lower() == "progress" else "DEC",
                    "family_key": family_key,
                    "title": row["title"],
                    "versions_with_charges": versions_with_charges,
                }
            )
        if family_key in matrix_scope or versions_with_charges <= 0:
            continue
        if _billing_class(family_key) == "core_billing_schedule":
            core_missing.append(
                {
                    "utility": "DEP" if str(row["company"]).lower() == "progress" else "DEC",
                    "family_key": family_key,
                    "title": row["title"],
                    "versions_with_charges": versions_with_charges,
                    "version_count": int(row["version_count"] or 0),
                }
            )

    return {
        "total_rate_schedule_families": total_families,
        "matrix_scope_size": len(matrix_scope),
        "core_billing_missing_from_matrix_count": len(core_missing),
        "legacy_schedule_family_count": len(legacy_families),
        "core_billing_missing_from_matrix": core_missing,
        "legacy_schedule_families": legacy_families,
    }


def _is_legacy_family(family_key: str) -> bool:
    return "-doc-" in family_key


def _billing_class(family_key: str) -> str:
    if _is_legacy_family(family_key):
        return "legacy_or_malformed_family"
    if family_key.startswith("nc-progress-leaf-"):
        suffix = family_key.rsplit("-", 1)[-1]
        if suffix.isdigit():
            leaf_num = int(suffix)
            if 500 <= leaf_num <= 599:
                return "core_billing_schedule"
        return "other_schedule"
    if family_key.startswith("nc-carolinas-schedule-"):
        code = family_key.split("schedule-", 1)[1]
        if code in {"RS", "RT", "SGS", "LGS", "ES", "I", "PG", "TS", "HP", "HLF", "PP", "PPBE", "RE", "BC"}:
            return "core_billing_schedule"
    return "other_schedule"


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
    dep_years = [str(y)[-2:] for y in report["dep_years"]]  # type: ignore[index]
    dec_years = [str(y)[-2:] for y in report["dec_years"]]  # type: ignore[index]
    dep_matrix = report["dep_matrix"]  # type: ignore[assignment]
    dec_matrix = report["dec_matrix"]  # type: ignore[assignment]
    inventory_scope = report["inventory_scope"]  # type: ignore[assignment]

    lines = [
        "# NC Coverage Assessment",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        "This report is a focused billing-coverage matrix, not a full inventory of all NC `rate_schedule` families in SQLite.",
        "",
        "Legend:",
        "- `F` = full coverage by charge-count threshold for that schedule",
        "- `P` = partial but materially populated",
        "- `p` = sparse but non-zero",
        "- `X` = version exists but has zero charges",
        "- `(=YY)` = no new version that calendar year; July 1 coverage is carried forward from year `YY`",
        "- `—` = no active version found for July 1 of that year",
        "",
        "Scope summary:",
        f"- NC rate_schedule families in SQLite: {inventory_scope['total_rate_schedule_families']}",
        f"- Focused matrix families: {inventory_scope['matrix_scope_size']}",
        f"- Populated core billing families omitted from the matrix: {inventory_scope['core_billing_missing_from_matrix_count']}",
        f"- Legacy/malformed schedule families detected: {inventory_scope['legacy_schedule_family_count']}",
        "",
        "## DEP",
        "",
        _render_table(dep_matrix, dep_years, report["dep_years"]),  # type: ignore[arg-type]
        "",
        "## DEC",
        "",
        _render_table(dec_matrix, dec_years, report["dec_years"]),  # type: ignore[arg-type]
        "",
        "## Inventory Exceptions",
        "",
        "Populated core billing families currently omitted from the focused matrix:",
        "",
        _render_inventory_list(inventory_scope["core_billing_missing_from_matrix"]),  # type: ignore[arg-type]
        "",
        "Legacy or malformed schedule-family keys detected in SQLite:",
        "",
        _render_inventory_list(inventory_scope["legacy_schedule_families"], limit=12),  # type: ignore[arg-type]
        "",
        "For the full classification and CSV export, use `python -m duke_rates export nc-schedule-inventory-audit`.",
        "",
    ]
    return "\n".join(lines)


def _render_table(matrix: list[dict[str, object]], short_years: list[str], years: list[int]) -> str:
    header = "Schedule       " + "  ".join(f"{yy:>4}" for yy in short_years)
    body = []
    for row in matrix:
        values = [str(row[str(year)]).rjust(4) for year in years]
        body.append(f"{str(row['schedule_label']):<14}" + "  ".join(values))
    return "```text\n" + "\n".join([header, *body]) + "\n```"


def _render_inventory_list(rows: list[dict[str, object]], *, limit: int = 20) -> str:
    if not rows:
        return "_None._"
    lines = []
    for row in rows[:limit]:
        lines.append(
            f"- `{row['utility']}` `{row['family_key']}`"
            f" ({row.get('title') or 'untitled'})"
        )
    remaining = len(rows) - min(len(rows), limit)
    if remaining > 0:
        lines.append(f"- ... and {remaining} more")
    return "\n".join(lines)


__all__ = [
    "CoverageFamily",
    "build_nc_coverage_assessment",
    "export_nc_coverage_assessment",
    "get_nc_coverage_families",
    "_DEFAULT_OUTPUT_DIR",
]
