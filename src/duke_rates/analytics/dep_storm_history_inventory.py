from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

try:
    import fitz
except Exception:  # pragma: no cover - optional at import time
    fitz = None

_DEFAULT_OUTPUT_DIR = Path("docs/reports/dep_storm_history_inventory")
_CANONICAL_FAMILIES: dict[str, str] = {
    "nc-progress-leaf-607": "STS-607",
    "nc-progress-leaf-613": "STS-613",
    "nc-progress-doc-STORMRECOVERYRIDER": "doc-STORMRECOVERYRIDER",
}
_TARGET_DOCKETS = ("1204", "1262", "1300", "1333")
_STATUS_PRIORITY = {
    "historical_leaf_candidate": 0,
    "storm_bundle_candidate": 1,
    "historical_bundle_candidate": 2,
    "weak_candidate": 3,
    "procedural_noise": 4,
    "modern_canonical_source": 5,
}
_LEAF_607_RE = re.compile(r"leaf\s+no\.?\s*607\b|leaf\s+607\b", re.I)
_LEAF_613_RE = re.compile(r"leaf\s+no\.?\s*613\b|leaf\s+613\b", re.I)
_STS2_RE = re.compile(r"\bsts\s*-?\s*2\b|storm\s+securitization\s+rider\s+sts\s*-?\s*2", re.I)
_STORM_RE = re.compile(
    r"\brider\s+sts\b|storm\s+securitization|storm\s+recovery\s+rider|storm\s+transition\s+rider",
    re.I,
)
_TARIFF_RE = re.compile(
    r"compliance\s+tariff|revised\s+tariff|tariff\s+sheet|annual\s+adjustment",
    re.I,
)
_FUEL_RE = re.compile(r"fuel\s+and\s+fuel-related|fuel\s+charge|emf", re.I)


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def build_dep_storm_history_inventory(database_path: Path | None = None) -> dict[str, Any]:
    conn = _connect(database_path)
    try:
        canonical_rows = _load_canonical_rows(conn)
        candidate_rows = _load_candidate_rows(conn)
    finally:
        conn.close()

    return {
        "generated_at": date.today().isoformat(),
        "canonical_family_count": len(canonical_rows),
        "candidate_count": len(candidate_rows),
        "candidate_status_counts": dict(
            sorted(Counter(str(row["candidate_status"]) for row in candidate_rows).items())
        ),
        "recommended_action_counts": dict(
            sorted(Counter(str(row["recommended_action"]) for row in candidate_rows).items())
        ),
        "canonical_rows": canonical_rows,
        "candidate_rows": candidate_rows,
    }


def export_dep_storm_history_inventory(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_dep_storm_history_inventory(database_path)

    rows_csv = output_dir / "dep_storm_history_inventory_rows.csv"
    summary_json = output_dir / "dep_storm_history_inventory_summary.json"
    markdown_path = output_dir / "dep_storm_history_inventory.md"

    _write_csv(rows_csv, report["candidate_rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _load_canonical_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in _CANONICAL_FAMILIES)
    query = f"""
        SELECT
            tf.family_key,
            tf.title,
            COUNT(DISTINCT tv.id) AS version_count,
            COUNT(DISTINCT CASE WHEN COALESCE(vcs.charge_count, 0) > 0 THEN tv.id END) AS versions_with_charges,
            COUNT(DISTINCT tv.historical_document_id) AS historical_doc_count,
            COUNT(DISTINCT CASE WHEN hd.start_page IS NOT NULL AND hd.end_page IS NOT NULL THEN hd.id END) AS bounded_doc_count,
            MIN(tv.effective_start) AS earliest_effective_start,
            MAX(tv.effective_start) AS latest_effective_start
        FROM tariff_families tf
        LEFT JOIN tariff_versions tv
          ON tv.family_key = tf.family_key
        LEFT JOIN v_version_charge_summary vcs
          ON vcs.version_id = tv.id
        LEFT JOIN historical_documents hd
          ON hd.id = tv.historical_document_id
        WHERE tf.family_key IN ({placeholders})
        GROUP BY tf.family_key, tf.title
        ORDER BY tf.family_key
    """
    rows = conn.execute(query, tuple(_CANONICAL_FAMILIES)).fetchall()
    return [
        {
            "family_key": str(row["family_key"]),
            "rider_label": _CANONICAL_FAMILIES[str(row["family_key"])],
            "title": row["title"],
            "version_count": int(row["version_count"] or 0),
            "versions_with_charges": int(row["versions_with_charges"] or 0),
            "historical_doc_count": int(row["historical_doc_count"] or 0),
            "bounded_doc_count": int(row["bounded_doc_count"] or 0),
            "earliest_effective_start": row["earliest_effective_start"],
            "latest_effective_start": row["latest_effective_start"],
        }
        for row in rows
    ]


def _load_candidate_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    query = """
        SELECT
            id,
            docket_number,
            utility,
            filing_title,
            filing_date,
            filing_classification,
            family_keys_json,
            local_path,
            fetch_status
        FROM ncuc_discovery_records
        WHERE utility = 'Duke Energy Progress'
          AND docket_number LIKE 'E-2%'
          AND (
            docket_number LIKE 'E-2%1204%'
            OR docket_number LIKE 'E-2%1262%'
            OR docket_number LIKE 'E-2%1300%'
            OR docket_number LIKE 'E-2%1333%'
          )
        ORDER BY id DESC
    """
    rows = conn.execute(query).fetchall()
    analyzed = [_analyze_candidate_row(row) for row in rows]
    return sorted(
        analyzed,
        key=lambda row: (
            _STATUS_PRIORITY.get(str(row["candidate_status"]), 99),
            -int(row["candidate_score"]),
            str(row["docket_number"]),
            -int(row["id"]),
        ),
    )


def _analyze_candidate_row(row: sqlite3.Row) -> dict[str, Any]:
    filing_title = str(row["filing_title"] or "")
    family_keys_json = str(row["family_keys_json"] or "[]")
    local_path = str(row["local_path"] or "")
    searchable = "\n".join(
        [
            filing_title,
            family_keys_json,
            _extract_searchable_text(local_path),
        ]
    )
    lower = searchable.lower()
    classification = str(row["filing_classification"] or "").lower()
    docket_number = str(row["docket_number"] or "")
    docket_hint = _normalize_docket_hint(docket_number)
    filing_year = _extract_year(str(row["filing_date"] or ""))

    explicit_607 = bool(_LEAF_607_RE.search(searchable))
    explicit_613 = bool(_LEAF_613_RE.search(searchable) or _STS2_RE.search(searchable))
    storm_hit = bool(_STORM_RE.search(searchable))
    tariff_hit = bool(_TARIFF_RE.search(searchable))
    fuel_hit = bool(_FUEL_RE.search(searchable))
    family_tag_607 = "607" in family_keys_json
    family_tag_613 = "613" in family_keys_json
    family_tag_legacy = "STORMRECOVERYRIDER" in family_keys_json
    has_local_file = bool(local_path)

    signal_notes: list[str] = []
    if explicit_607:
        signal_notes.append("explicit Leaf 607 marker")
    if explicit_613:
        signal_notes.append("explicit Leaf 613 / STS-2 marker")
    if storm_hit and not (explicit_607 or explicit_613):
        signal_notes.append("storm rider wording present")
    if tariff_hit:
        signal_notes.append("tariff/compliance wording present")
    if fuel_hit and not (explicit_607 or explicit_613):
        signal_notes.append("fuel wording present on sampled pages")
    if family_tag_607 or family_tag_613 or family_tag_legacy:
        signal_notes.append("storm family tag present in discovery record")

    candidate_family = "unknown"
    if explicit_613 or family_tag_613:
        candidate_family = "nc-progress-leaf-613"
    elif explicit_607 or family_tag_607:
        candidate_family = "nc-progress-leaf-607"
    elif family_tag_legacy:
        candidate_family = "nc-progress-doc-STORMRECOVERYRIDER"

    score = 0
    if explicit_607 or explicit_613:
        score += 90
    elif storm_hit:
        score += 45
    elif docket_hint == "E-2 Sub 1204":
        score += 20
    if tariff_hit:
        score += 20
    if family_tag_607 or family_tag_613 or family_tag_legacy:
        score += 15
    if has_local_file:
        score += 5
    if fuel_hit and not (explicit_607 or explicit_613):
        score -= 25
    if classification in {"order", "testimony", "application", "notice"} and not tariff_hit:
        score -= 45

    is_modern_source = docket_hint in {"E-2 Sub 1262", "E-2 Sub 1300", "E-2 Sub 1333"} or filing_year >= 2023

    if is_modern_source and (explicit_607 or explicit_613 or storm_hit or family_tag_607 or family_tag_613):
        candidate_status = "modern_canonical_source"
        action = "none"
        reason = "Record belongs to the modern storm timeline already supporting the current canonical storm families."
    elif explicit_607 or explicit_613:
        candidate_status = "historical_leaf_candidate"
        action = "inspect_and_bound_leaf_pages"
        reason = "Downloaded PDF includes an explicit storm leaf marker and is the best candidate for historical mining."
    elif storm_hit and tariff_hit:
        candidate_status = "storm_bundle_candidate"
        action = "mine_bundle_for_storm_leaf_pages"
        reason = "Tariff/compliance wording and storm rider wording suggest the PDF may contain a historical storm leaf within a bundle."
    elif docket_hint == "E-2 Sub 1204" and tariff_hit:
        candidate_status = "historical_bundle_candidate"
        action = "inspect_bundle_for_hidden_storm_leaf"
        reason = "Docket E-2 Sub 1204 bundle looks relevant, but sampled text did not yet confirm a specific storm leaf page."
    elif docket_hint in {"E-2 Sub 1262", "E-2 Sub 1300", "E-2 Sub 1333"} and (family_tag_607 or family_tag_613 or storm_hit):
        candidate_status = "modern_canonical_source"
        action = "none"
        reason = "Modern storm-related bundle or leaf already contributes to the current canonical storm timeline."
    elif classification in {"order", "testimony", "application", "notice"} or score <= 0:
        candidate_status = "procedural_noise"
        action = "deprioritize_for_storm_history"
        reason = "Record looks procedural or fuel-focused rather than like a storm rider leaf or bundle source."
    else:
        candidate_status = "weak_candidate"
        action = "manual_review_if_needed"
        reason = "Record is adjacent to storm-family discovery, but the current signals are too weak to treat it as a priority storm-history source."

    return {
        "id": int(row["id"]),
        "docket_number": docket_number,
        "docket_hint": docket_hint,
        "utility": row["utility"],
        "filing_date": row["filing_date"],
        "filing_classification": row["filing_classification"],
        "filing_title": filing_title,
        "fetch_status": row["fetch_status"],
        "has_local_file": has_local_file,
        "local_path": local_path,
        "candidate_family": candidate_family,
        "candidate_status": candidate_status,
        "candidate_score": score,
        "recommended_action": action,
        "reason": reason,
        "signal_notes": "; ".join(signal_notes) if signal_notes else "no strong storm signals detected",
    }


def _normalize_docket_hint(docket_number: str) -> str:
    normalized = docket_number.replace(",", " ").replace("-", " ").lower()
    for sub in _TARGET_DOCKETS:
        if sub in normalized:
            return f"E-2 Sub {sub}"
    return docket_number or "unknown"


def _extract_year(value: str) -> int:
    match = re.search(r"(20\d{2})", value)
    if not match:
        return 0
    return int(match.group(1))


def _extract_searchable_text(local_path: str) -> str:
    if not local_path:
        return ""
    path = Path(local_path)
    if not path.exists():
        workspace_path = Path.cwd() / local_path
        if workspace_path.exists():
            path = workspace_path
        else:
            return ""
    if fitz is None:
        return ""
    try:
        doc = fitz.open(path)
    except Exception:
        return ""
    try:
        chunks: list[str] = []
        page_limit = min(doc.page_count, 30)
        for index in range(page_limit):
            page_text = doc.load_page(index).get_text()
            if page_text:
                chunks.append(page_text)
        return "\n".join(chunks)
    finally:
        doc.close()


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "id",
        "docket_number",
        "docket_hint",
        "filing_date",
        "filing_classification",
        "candidate_family",
        "candidate_status",
        "candidate_score",
        "recommended_action",
        "signal_notes",
        "filing_title",
        "fetch_status",
        "local_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# DEP Storm History Inventory",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        "This report separates the current canonical DEP storm-rider families from older docket-discovery",
        "records that may or may not represent true predecessor storm leaves.",
        "",
        f"- Canonical families tracked: {report['canonical_family_count']}",
        f"- Historical/related discovery records reviewed: {report['candidate_count']}",
        "",
        "Candidate status counts:",
    ]
    for status, count in report["candidate_status_counts"].items():
        lines.append(f"- `{status}`: {count}")
    lines.extend(
        [
            "",
            "Recommended action counts:",
        ]
    )
    for action, count in report["recommended_action_counts"].items():
        lines.append(f"- `{action}`: {count}")
    historical_leaf_count = int(report["candidate_status_counts"].get("historical_leaf_candidate", 0))
    canonical_607 = next(
        (r for r in report["canonical_rows"] if r["family_key"] == "nc-progress-leaf-607"), None
    )
    leaf_607_versions = canonical_607["version_count"] if canonical_607 else 0
    leaf_607_first = canonical_607["earliest_effective_start"] if canonical_607 else None
    if historical_leaf_count == 0:
        if leaf_607_versions >= 10:
            lines.extend(
                [
                    "",
                    "Current interpretation:",
                    f"- `nc-progress-leaf-607` now has {leaf_607_versions} versions spanning"
                    f" {leaf_607_first} through the current period — storm rate history is substantially complete.",
                    "- E-2 Sub 1262 (semiannual true-up filings) was the source for the 2021–2025 backfill and is fully mined.",
                    "- E-2 Sub 1204 remains the only unresolved `historical_bundle_candidate`; it is a Nov 2019 DEP compliance"
                    " tariff bundle and may not contain any pre-2021 storm leaf content.",
                    "- No pre-2021 storm rider history was found in the reviewed docket set.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "Current interpretation:",
                    "- No explicit pre-modern `Leaf 607` / `Leaf 613` storm-leaf hits were confirmed in the reviewed older docket set.",
                    "- The best remaining historical lead is a single `E-2 Sub 1204` compliance bundle that still needs page-level inspection.",
                ]
            )

    lines.extend(
        [
            "",
            "## Canonical Family Timeline",
            "",
            "```text",
            "Rider                  Versions  Charged  Docs  Bound  First       Latest",
        ]
    )
    for row in report["canonical_rows"]:
        lines.append(
            f"{row['rider_label']:<22} {row['version_count']:>8}  "
            f"{row['versions_with_charges']:>7}  {row['historical_doc_count']:>4}  "
            f"{row['bounded_doc_count']:>5}  {str(row['earliest_effective_start'] or '-'):>10}  "
            f"{str(row['latest_effective_start'] or '-'):>10}"
        )
    lines.extend(
        [
            "```",
            "",
            "## Ranked Discovery Candidates",
            "",
            "```text",
            "ID    Docket        Status                      Score  Family                  Action                              Date         Class",
        ]
    )
    for row in report["candidate_rows"][:30]:
        lines.append(
            f"{row['id']:<5} {row['docket_hint']:<12} {row['candidate_status']:<27} "
            f"{row['candidate_score']:>5}  {row['candidate_family']:<22} "
            f"{row['recommended_action']:<34} {str(row['filing_date'] or '-'):>10}  {str(row['filing_classification'] or '-')}"
        )
    lines.extend(
        [
            "```",
            "",
            "## Notes",
            "",
            "- `historical_leaf_candidate`: explicit leaf marker found; strongest target for page-bounding and mining.",
            "- `storm_bundle_candidate`: bundle with storm + tariff signals; likely worth manual inspection for hidden rider pages.",
            "- `historical_bundle_candidate`: relevant docket bundle with tariff signals but no explicit storm leaf marker yet.",
            "- `modern_canonical_source`: modern storm source already supporting the current canonical timeline.",
            "- `procedural_noise`: mostly order/testimony/fuel residue, not a priority storm-history source.",
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "build_dep_storm_history_inventory",
    "export_dep_storm_history_inventory",
    "_DEFAULT_OUTPUT_DIR",
]
