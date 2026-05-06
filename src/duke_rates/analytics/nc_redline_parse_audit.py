from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from duke_rates.parse.redline_detector import detect_redline

_DEFAULT_OUTPUT_DIR = Path("docs/reports/nc_redline_parse_audit")


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def build_nc_redline_parse_audit(
    database_path: Path | None = None,
) -> dict[str, Any]:
    rows = _load_version_rows(database_path)
    detectors = [_run_detector_for_row(row) for row in rows]
    clean_companions = _build_clean_companion_index(rows, detectors)
    audit_rows = [
        _build_audit_row(
            row,
            detector,
            clean_companion=clean_companions.get(
                (str(row["family_key"]), str(row["effective_start"] or ""))
            ),
        )
        for row, detector in zip(rows, detectors, strict=False)
    ]
    audit_rows.sort(
        key=lambda item: (
            int(item["priority_score"]),
            int(item["charge_count"]),
            str(item["family_key"]),
            str(item["effective_start"] or ""),
        ),
        reverse=True,
    )

    action_counts = Counter(str(row["recommended_action"]) for row in audit_rows)
    return {
        "generated_at": date.today().isoformat(),
        "version_count": len(audit_rows),
        "parsed_versions_count": len([row for row in audit_rows if int(row["charge_count"]) > 0]),
        "recommended_action_counts": dict(sorted(action_counts.items())),
        "rows": audit_rows,
    }


def export_nc_redline_parse_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_nc_redline_parse_audit(database_path)

    rows_csv = output_dir / "nc_redline_parse_audit_rows.csv"
    summary_json = output_dir / "nc_redline_parse_audit_summary.json"
    markdown_path = output_dir / "nc_redline_parse_audit.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _load_version_rows(database_path: Path | None) -> list[sqlite3.Row]:
    conn = _connect(database_path)
    try:
        rows = conn.execute(
            """
            WITH matched_fingerprints AS (
                SELECT
                    hd.id AS historical_document_id,
                    COALESCE(df.is_redline_candidate, 0) AS stored_is_redline,
                    COALESCE(df.redline_confidence, 0.0) AS stored_redline_confidence,
                    ROW_NUMBER() OVER (
                        PARTITION BY hd.id
                        ORDER BY
                            CASE
                                WHEN df.page_start IS hd.start_page AND df.page_end IS hd.end_page THEN 2
                                WHEN df.page_start IS NULL AND df.page_end IS NULL THEN 1
                                ELSE 0
                            END DESC,
                            df.id DESC
                    ) AS rn
                FROM historical_documents hd
                LEFT JOIN document_fingerprints df
                  ON df.source_pdf = hd.local_path
                 AND (
                    (df.page_start IS hd.start_page AND df.page_end IS hd.end_page)
                    OR (df.page_start IS NULL AND df.page_end IS NULL)
                 )
            )
            SELECT
                tv.id AS version_id,
                tv.family_key,
                tv.effective_start,
                tv.source_type,
                hd.id AS historical_document_id,
                hd.title,
                hd.local_path,
                hd.start_page,
                hd.end_page,
                COALESCE(mf.stored_is_redline, 0) AS stored_is_redline,
                COALESCE(mf.stored_redline_confidence, 0.0) AS stored_redline_confidence,
                COALESCE(vcs.charge_count, 0) AS charge_count
            FROM tariff_versions tv
            JOIN historical_documents hd
              ON hd.id = tv.historical_document_id
            LEFT JOIN matched_fingerprints mf
              ON mf.historical_document_id = hd.id
             AND mf.rn = 1
            LEFT JOIN v_version_charge_summary vcs
              ON vcs.version_id = tv.id
            WHERE hd.state = 'NC'
              AND LOWER(hd.company) IN ('progress', 'carolinas')
            ORDER BY tv.family_key, tv.effective_start, tv.id
            """
        ).fetchall()
    finally:
        conn.close()
    return list(rows)


def _run_detector(path: str, *, start_page: int | None = None, end_page: int | None = None) -> dict[str, Any]:
    try:
        result = detect_redline(path, max_pages=5, start_page=start_page, end_page=end_page)
        return {
            "corrected_is_redline": bool(result.is_redline),
            "corrected_confidence": float(result.confidence),
            "signals": list(result.signals),
            "red_text_samples": list(result.red_text_samples),
            "strikethrough_samples": list(result.strikethrough_samples),
            "red_is_index_only": bool(result.red_is_index_only),
            "error": None,
        }
    except Exception as exc:
        return {
            "corrected_is_redline": False,
            "corrected_confidence": 0.0,
            "signals": [],
            "red_text_samples": [],
            "strikethrough_samples": [],
            "red_is_index_only": False,
            "error": str(exc),
        }


def _run_detector_for_row(row: sqlite3.Row) -> dict[str, Any]:
    return _run_detector(
        str(row["local_path"] or ""),
        start_page=int(row["start_page"]) if row["start_page"] is not None else None,
        end_page=int(row["end_page"]) if row["end_page"] is not None else None,
    )


def _build_audit_row(
    row: sqlite3.Row,
    detector: dict[str, Any],
    *,
    clean_companion: dict[str, Any] | None,
) -> dict[str, Any]:
    charge_count = int(row["charge_count"] or 0)
    stored_is_redline = bool(int(row["stored_is_redline"] or 0))
    corrected_is_redline = bool(detector["corrected_is_redline"])
    action, reason, priority = _classify(
        charge_count=charge_count,
        stored_is_redline=stored_is_redline,
        corrected_is_redline=corrected_is_redline,
        clean_companion=clean_companion,
    )
    return {
        "priority_score": priority,
        "version_id": int(row["version_id"]),
        "historical_document_id": int(row["historical_document_id"]),
        "family_key": row["family_key"],
        "effective_start": row["effective_start"],
        "source_type": row["source_type"],
        "title": row["title"],
        "local_path": row["local_path"],
        "charge_count": charge_count,
        "stored_is_redline": int(stored_is_redline),
        "stored_redline_confidence": round(float(row["stored_redline_confidence"] or 0.0), 4),
        "corrected_is_redline": int(corrected_is_redline),
        "corrected_redline_confidence": round(float(detector["corrected_confidence"] or 0.0), 4),
        "corrected_signals": json.dumps(detector["signals"]),
        "red_text_samples": json.dumps(detector["red_text_samples"][:5]),
        "strikethrough_samples": json.dumps(detector["strikethrough_samples"][:5]),
        "red_is_index_only": int(bool(detector["red_is_index_only"])),
        "clean_companion_version_id": int(clean_companion["version_id"]) if clean_companion else None,
        "clean_companion_historical_document_id": (
            int(clean_companion["historical_document_id"]) if clean_companion else None
        ),
        "clean_companion_path": clean_companion["local_path"] if clean_companion else None,
        "clean_companion_charge_count": int(clean_companion["charge_count"]) if clean_companion else 0,
        "recommended_action": action,
        "reason": reason,
        "detector_error": detector["error"],
    }


def _classify(
    *,
    charge_count: int,
    stored_is_redline: bool,
    corrected_is_redline: bool,
    clean_companion: dict[str, Any] | None,
) -> tuple[str, str, int]:
    if corrected_is_redline and charge_count > 0 and clean_companion:
        return (
            "prefer_clean_companion_version",
            "A non-redline exact-date companion with extracted charges already exists for this family/date.",
            95,
        )
    if corrected_is_redline and charge_count > 0:
        return (
            "review_parsed_redline_version",
            "Corrected detector still says this source is a redline and the version has extracted charges.",
            100,
        )
    if stored_is_redline and not corrected_is_redline and charge_count > 0:
        return (
            "refresh_stale_false_positive_fingerprint",
            "Stored fingerprint says redline, but corrected detector does not; parsed charges may be fine after refresh.",
            70,
        )
    if stored_is_redline and not corrected_is_redline:
        return (
            "refresh_stale_false_positive_fingerprint",
            "Stored fingerprint says redline, but corrected detector does not.",
            40,
        )
    if corrected_is_redline and charge_count == 0:
        return (
            "leave_redline_unparsed_or_find_clean_companion",
            "Corrected detector says redline and there are no extracted charges.",
            30,
        )
    return (
        "likely_ok",
        "No active redline conflict detected for this parsed source.",
        0,
    )


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


def _render_markdown(report: dict[str, Any]) -> str:
    rows = list(report["rows"])
    lines = [
        "# NC Redline Parse Audit",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        f"- Version rows scanned: {report['version_count']}",
        f"- Parsed versions (charge_count > 0): {report['parsed_versions_count']}",
        "",
        "Recommended action counts:",
    ]
    for action, count in dict(report["recommended_action_counts"]).items():
        lines.append(f"- `{action}`: {count}")
    lines.extend(["", "## Highest Priority Rows", "", _render_table(rows[:40]), ""])
    return "\n".join(lines)


def _render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No rows._"
    header = "Score  Version  Family                         Start        Charges  Stored  Corrected  Action"
    body = []
    for row in rows:
        body.append(
            f"{int(row['priority_score']):>5}  "
            f"{int(row['version_id']):>7}  "
            f"{str(row['family_key']):<29}  "
            f"{str(row['effective_start'] or '-'): <10}  "
            f"{int(row['charge_count']):>7}  "
            f"{int(row['stored_is_redline']):>6}  "
            f"{int(row['corrected_is_redline']):>9}  "
            f"{str(row['recommended_action'])}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


def _build_clean_companion_index(
    rows: list[sqlite3.Row],
    detectors: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    companions: dict[tuple[str, str], dict[str, Any]] = {}
    for row, detector in zip(rows, detectors, strict=False):
        path = str(row["local_path"] or "")
        if bool(detector.get("corrected_is_redline")):
            continue
        charge_count = int(row["charge_count"] or 0)
        if charge_count <= 0:
            continue
        key = (str(row["family_key"]), str(row["effective_start"] or ""))
        candidate = {
            "version_id": int(row["version_id"]),
            "historical_document_id": int(row["historical_document_id"]),
            "local_path": path,
            "charge_count": charge_count,
        }
        current = companions.get(key)
        if current is None or int(candidate["charge_count"]) > int(current["charge_count"]):
            companions[key] = candidate
    return companions


__all__ = [
    "build_nc_redline_parse_audit",
    "export_nc_redline_parse_audit",
    "_DEFAULT_OUTPUT_DIR",
]
