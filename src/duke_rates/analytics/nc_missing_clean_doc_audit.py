from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from duke_rates.analytics.nc_document_gap_audit import _leaf_no_from_label, _parse_revision_ordinal
from duke_rates.analytics.nc_redline_lead_audit import build_nc_redline_lead_audit
from duke_rates.analytics.tariff_completeness_audit import _build_search_queries

_DEFAULT_OUTPUT_DIR = Path("docs/reports/nc_missing_clean_doc_audit")
_TEMPORAL_GAP_DAYS = 120
_DOCKET_FROM_DIR_RE = re.compile(r"\b([a-z]-\d+)-sub-(\d+)\b", re.I)


def build_nc_missing_clean_doc_audit(
    database_path: Path | None = None,
) -> dict[str, Any]:
    conn = _connect(database_path)
    try:
        families = _load_family_rows(conn)
    finally:
        conn.close()

    redline_report = build_nc_redline_lead_audit(database_path)
    redline_by_family = {
        str(row["family_key"]): row
        for row in redline_report.get("rows", [])
    }

    rows: list[dict[str, Any]] = []
    priority_counts: Counter[str] = Counter()
    missing_kind_counts: Counter[str] = Counter()

    for family_key, payload in families.items():
        row = _build_family_row(payload, redline_by_family.get(family_key))
        if row is None:
            continue
        rows.append(row)
        priority_counts[str(row["priority_band"])] += 1
        missing_kind_counts[str(row["missing_kind"])] += 1

    rows.sort(
        key=lambda item: (
            int(item["priority_score"]),
            int(item["missing_supersedes_count"]),
            int(item["missing_ordinal_count"]),
            int(item["largest_effective_gap_days"] or 0),
            str(item["family_key"]),
        ),
        reverse=True,
    )

    return {
        "generated_at": date.today().isoformat(),
        "total_rows": len(rows),
        "priority_band_counts": dict(sorted(priority_counts.items())),
        "missing_kind_counts": dict(sorted(missing_kind_counts.items())),
        "rows": rows,
    }


def export_nc_missing_clean_doc_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_nc_missing_clean_doc_audit(database_path)

    rows_csv = output_dir / "nc_missing_clean_doc_audit_rows.csv"
    summary_json = output_dir / "nc_missing_clean_doc_audit_summary.json"
    markdown_path = output_dir / "nc_missing_clean_doc_audit.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_family_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        WITH version_charge_counts AS (
            SELECT version_id, COUNT(*) AS charge_count
            FROM tariff_charges
            GROUP BY version_id
        )
        SELECT
            tf.family_key,
            tf.state,
            tf.company,
            tf.family_type,
            tf.title,
            tf.schedule_code,
            tv.id AS version_id,
            tv.effective_start,
            tv.effective_end,
            tv.revision_label,
            tv.supersedes_label,
            tv.docket_number,
            tv.docket_dir,
            tv.leaf_no,
            tv.source_type,
            COALESCE(vcc.charge_count, 0) AS charge_count
        FROM tariff_families tf
        LEFT JOIN tariff_versions tv
          ON tv.family_key = tf.family_key
        LEFT JOIN version_charge_counts vcc
          ON vcc.version_id = tv.id
        WHERE tf.state = 'NC'
          AND LOWER(tf.company) IN ('progress', 'carolinas')
          AND tf.family_type IN ('rate_schedule', 'rider')
        ORDER BY tf.family_key, tv.effective_start, tv.id
        """
    ).fetchall()

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        family_key = str(row["family_key"])
        bucket = grouped.setdefault(
            family_key,
            {
                "family_key": family_key,
                "state": row["state"],
                "company": str(row["company"] or ""),
                "utility": "DEP" if str(row["company"] or "").lower() == "progress" else "DEC",
                "family_type": row["family_type"],
                "title": row["title"],
                "schedule_code": row["schedule_code"],
                "versions": [],
            },
        )
        if row["version_id"] is not None:
            bucket["versions"].append(dict(row))
    return grouped


def _build_family_row(
    payload: dict[str, Any],
    redline_row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    versions = list(payload["versions"])
    if not versions and not redline_row:
        return None

    revisions = {
        str(v["revision_label"])
        for v in versions
        if v.get("revision_label")
    }
    supersedes_children: list[dict[str, Any]] = [
        v for v in versions if v.get("supersedes_label")
    ]
    missing_supersedes = sorted(
        {
            str(v["supersedes_label"])
            for v in supersedes_children
            if str(v["supersedes_label"]) not in revisions
        }
    )

    ordinals = sorted(
        {
            ordinal
            for ordinal in (
                _parse_revision_ordinal(v.get("revision_label"))
                for v in versions
            )
            if ordinal is not None
        }
    )
    missing_ordinals = _missing_ordinals(ordinals)

    dated_versions = [
        v for v in versions
        if v.get("effective_start")
    ]
    dated_versions.sort(key=lambda v: str(v["effective_start"]))
    largest_gap = _largest_effective_gap(dated_versions)

    candidate_dockets = set()
    for version in versions:
        if version.get("docket_number"):
            candidate_dockets.add(str(version["docket_number"]))
        docket_dir_hint = _docket_from_dir(version.get("docket_dir"))
        if docket_dir_hint:
            candidate_dockets.add(docket_dir_hint)

    redline_dockets: list[str] = []
    redline_clues: list[str] = []
    redline_search_hint = ""
    unpaired_redline_doc_count = 0
    redline_clue_doc_count = 0
    if redline_row:
        redline_dockets = list(_loads_json_list(redline_row.get("docket_numbers")))
        candidate_dockets.update(redline_dockets)
        redline_clues = list(_loads_json_list(redline_row.get("top_actionable_clues")))
        redline_search_hint = str(redline_row.get("search_hint") or "")
        unpaired_redline_doc_count = int(redline_row.get("unpaired_redline_doc_count") or 0)
        redline_clue_doc_count = int(
            max(
                int(redline_row.get("redline_clue_doc_count") or 0),
                int(redline_row.get("actionable_clue_count") or 0),
            )
        )

    candidate_dockets = {
        docket.strip()
        for docket in candidate_dockets
        if docket and docket.strip()
    }

    expected_window = _expected_window(largest_gap, supersedes_children, missing_supersedes)
    leaf_no = _best_leaf_no(versions, payload["family_key"])
    latest_revision = _latest_revision_label(versions)
    search_terms = _build_search_queries(
        leaf_no=leaf_no,
        revision_label=latest_revision,
        title=payload.get("title"),
        state="NC",
        company=str(payload.get("company") or ""),
    )
    for label in missing_supersedes[:3]:
        if label not in search_terms:
            search_terms.append(label)
    for clue in redline_clues[:2]:
        if clue not in search_terms:
            search_terms.append(clue)

    versions_with_charges = sum(1 for v in versions if int(v.get("charge_count") or 0) > 0)
    missing_kind = _missing_kind(
        missing_supersedes_count=len(missing_supersedes),
        missing_ordinal_count=len(missing_ordinals),
        largest_gap_days=largest_gap["gap_days"] if largest_gap else None,
        unpaired_redline_doc_count=unpaired_redline_doc_count,
        redline_clue_doc_count=redline_clue_doc_count,
    )
    if missing_kind is None:
        return None

    priority_score = _priority_score(
        missing_supersedes_count=len(missing_supersedes),
        missing_ordinal_count=len(missing_ordinals),
        largest_gap_days=largest_gap["gap_days"] if largest_gap else None,
        unpaired_redline_doc_count=unpaired_redline_doc_count,
        redline_clue_doc_count=redline_clue_doc_count,
    )
    priority_band = _priority_band(priority_score)

    evidence_summary = _evidence_summary(
        missing_supersedes=missing_supersedes,
        missing_ordinals=missing_ordinals,
        largest_gap=largest_gap,
        redline_clue_doc_count=redline_clue_doc_count,
        unpaired_redline_doc_count=unpaired_redline_doc_count,
    )

    return {
        "utility": payload["utility"],
        "family_key": payload["family_key"],
        "title": payload["title"],
        "family_type": payload["family_type"],
        "schedule_code": payload["schedule_code"],
        "version_count": len(versions),
        "versions_with_charges": versions_with_charges,
        "earliest_effective_start": dated_versions[0]["effective_start"] if dated_versions else None,
        "latest_effective_start": dated_versions[-1]["effective_start"] if dated_versions else None,
        "latest_revision_label": latest_revision,
        "leaf_no": leaf_no,
        "missing_kind": missing_kind,
        "missing_supersedes_count": len(missing_supersedes),
        "missing_supersedes_labels": json.dumps(missing_supersedes),
        "missing_ordinal_count": len(missing_ordinals),
        "missing_ordinals": json.dumps(missing_ordinals),
        "largest_effective_gap_days": largest_gap["gap_days"] if largest_gap else None,
        "largest_gap_start": largest_gap["gap_start"] if largest_gap else None,
        "largest_gap_end": largest_gap["gap_end"] if largest_gap else None,
        "redline_clue_doc_count": redline_clue_doc_count,
        "unpaired_redline_doc_count": unpaired_redline_doc_count,
        "redline_docket_count": len(redline_dockets),
        "suggested_dockets": json.dumps(sorted(candidate_dockets)),
        "suggested_query_terms": json.dumps(search_terms),
        "suggested_portal_filing_types": json.dumps(["TARIFF", "RATESCED", "ORDER"]),
        "suggested_date_after": expected_window["date_after"],
        "suggested_date_before": expected_window["date_before"],
        "redline_search_hint": redline_search_hint,
        "evidence_summary": evidence_summary,
        "recommended_next_step": _recommended_next_step(candidate_dockets, expected_window, redline_row),
        "priority_score": priority_score,
        "priority_band": priority_band,
    }


def _loads_json_list(payload: Any) -> list[str]:
    if not payload:
        return []
    if isinstance(payload, list):
        return [str(item) for item in payload if item]
    try:
        parsed = json.loads(str(payload))
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    return []


def _missing_ordinals(ordinals: list[int]) -> list[int]:
    if len(ordinals) < 2:
        return []
    missing: list[int] = []
    for left, right in zip(ordinals, ordinals[1:]):
        if right - left > 1:
            missing.extend(range(left + 1, right))
    return missing


def _largest_effective_gap(dated_versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for left, right in zip(dated_versions, dated_versions[1:]):
        try:
            start = datetime.fromisoformat(str(left["effective_start"]))
            end = datetime.fromisoformat(str(right["effective_start"]))
        except ValueError:
            continue
        gap_days = (end - start).days
        if gap_days < _TEMPORAL_GAP_DAYS:
            continue
        candidate = {
            "gap_days": gap_days,
            "gap_start": str(left["effective_start"]),
            "gap_end": str(right["effective_start"]),
        }
        if best is None or int(candidate["gap_days"]) > int(best["gap_days"]):
            best = candidate
    return best


def _expected_window(
    largest_gap: dict[str, Any] | None,
    supersedes_children: list[dict[str, Any]],
    missing_supersedes: list[str],
) -> dict[str, str | None]:
    if largest_gap:
        return {
            "date_after": largest_gap["gap_start"],
            "date_before": largest_gap["gap_end"],
        }

    missing_set = set(missing_supersedes)
    candidates = [
        v for v in supersedes_children
        if str(v.get("supersedes_label") or "") in missing_set and v.get("effective_start")
    ]
    if not candidates:
        return {"date_after": None, "date_before": None}

    candidates.sort(key=lambda v: str(v["effective_start"]))
    target = datetime.fromisoformat(str(candidates[0]["effective_start"]))
    before = (target + timedelta(days=30)).date().isoformat()
    after = (target - timedelta(days=365)).date().isoformat()
    return {"date_after": after, "date_before": before}


def _best_leaf_no(versions: list[dict[str, Any]], family_key: str) -> str | None:
    for version in versions:
        if version.get("leaf_no"):
            return str(version["leaf_no"])
    labels = [
        str(version["revision_label"])
        for version in versions
        if version.get("revision_label")
    ]
    for label in labels:
        leaf_no = _leaf_no_from_label(label)
        if leaf_no:
            return leaf_no
    match = re.search(r"-leaf-(\d+)$", family_key)
    return match.group(1) if match else None


def _latest_revision_label(versions: list[dict[str, Any]]) -> str | None:
    dated = [v for v in versions if v.get("effective_start")]
    if dated:
        dated.sort(key=lambda v: str(v["effective_start"]))
        return dated[-1].get("revision_label")
    if versions:
        return versions[-1].get("revision_label")
    return None


def _docket_from_dir(raw: Any) -> str | None:
    if not raw:
        return None
    match = _DOCKET_FROM_DIR_RE.search(str(raw))
    if not match:
        return None
    return f"{match.group(1).upper()}, Sub {int(match.group(2))}"


def _missing_kind(
    *,
    missing_supersedes_count: int,
    missing_ordinal_count: int,
    largest_gap_days: int | None,
    unpaired_redline_doc_count: int,
    redline_clue_doc_count: int,
) -> str | None:
    if unpaired_redline_doc_count > 0 and redline_clue_doc_count > 0:
        return "missing_clean_companion"
    if missing_supersedes_count > 0 and missing_ordinal_count > 0:
        return "missing_intermediate_revision"
    if missing_supersedes_count > 0:
        return "missing_superseded_revision"
    if missing_ordinal_count > 0:
        return "missing_revision_chain"
    if largest_gap_days and largest_gap_days >= _TEMPORAL_GAP_DAYS:
        return "missing_temporal_gap_document"
    if redline_clue_doc_count > 0:
        return "redline_indicated_missing_clean_document"
    return None


def _priority_score(
    *,
    missing_supersedes_count: int,
    missing_ordinal_count: int,
    largest_gap_days: int | None,
    unpaired_redline_doc_count: int,
    redline_clue_doc_count: int,
) -> int:
    score = 0
    score += min(40, missing_supersedes_count * 18)
    score += min(30, missing_ordinal_count * 8)
    if largest_gap_days:
        score += min(20, largest_gap_days // 90)
    score += min(25, unpaired_redline_doc_count * 12)
    score += min(25, redline_clue_doc_count * 10)
    return score


def _priority_band(priority_score: int) -> str:
    if priority_score >= 70:
        return "high"
    if priority_score >= 35:
        return "medium"
    return "low"


def _recommended_next_step(
    candidate_dockets: set[str],
    expected_window: dict[str, str | None],
    redline_row: dict[str, Any] | None,
) -> str:
    if candidate_dockets and expected_window["date_after"] and expected_window["date_before"]:
        return "search_portal_by_docket_and_date_window"
    if candidate_dockets:
        return "search_portal_by_docket"
    if redline_row:
        return "search_portal_by_redline_clues"
    return "search_portal_by_leaf_or_revision"


def _evidence_summary(
    *,
    missing_supersedes: list[str],
    missing_ordinals: list[int],
    largest_gap: dict[str, Any] | None,
    redline_clue_doc_count: int,
    unpaired_redline_doc_count: int,
) -> str:
    parts: list[str] = []
    if missing_supersedes:
        parts.append(f"missing supersedes labels: {', '.join(missing_supersedes[:3])}")
    if missing_ordinals:
        preview = ", ".join(str(item) for item in missing_ordinals[:5])
        parts.append(f"revision ordinals missing: {preview}")
    if largest_gap:
        parts.append(
            f"largest effective-date gap {largest_gap['gap_days']} days "
            f"between {largest_gap['gap_start']} and {largest_gap['gap_end']}"
        )
    if redline_clue_doc_count:
        parts.append(f"redline clue docs: {redline_clue_doc_count}")
    if unpaired_redline_doc_count:
        parts.append(f"unpaired redlines: {unpaired_redline_doc_count}")
    return "; ".join(parts) if parts else "family flagged by missing-clean-document heuristics"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# NC Missing Clean Document Audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Total rows: `{report['total_rows']}`",
        f"- Priority bands: `{json.dumps(report['priority_band_counts'], sort_keys=True)}`",
        f"- Missing kinds: `{json.dumps(report['missing_kind_counts'], sort_keys=True)}`",
        "",
        "## Top Leads",
        "",
        "| Priority | Family | Kind | Gap Days | Missing Supersedes | Missing Ordinals | Suggested Dockets | Next Step |",
        "|---|---|---|---:|---:|---:|---|---|",
    ]
    for row in report["rows"][:25]:
        lines.append(
            "| "
            f"{row['priority_band']} ({row['priority_score']}) | "
            f"`{row['family_key']}` | "
            f"{row['missing_kind']} | "
            f"{row['largest_effective_gap_days'] or 0} | "
            f"{row['missing_supersedes_count']} | "
            f"{row['missing_ordinal_count']} | "
            f"{', '.join(_loads_json_list(row['suggested_dockets'])[:3]) or '-'} | "
            f"{row['recommended_next_step']} |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "build_nc_missing_clean_doc_audit",
    "export_nc_missing_clean_doc_audit",
    "_DEFAULT_OUTPUT_DIR",
]
