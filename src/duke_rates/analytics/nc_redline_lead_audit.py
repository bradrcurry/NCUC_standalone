from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from duke_rates.analytics.nc_confidence_audit import build_nc_confidence_audit
from duke_rates.parse.redline_page_parser import parse_redline_page
from duke_rates.parse.redline_crossref import scan_redlines_for_crossrefs

_DEFAULT_OUTPUT_DIR = Path("docs/reports/nc_redline_lead_audit")
_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}\b"
)
_LEAF_CONTEXT_RE = re.compile(r"(?i)\b(?:Superseding\s+)?NC\b.*?\bLeaf\s+No\.?\s*\d+\b")
_REVISION_CONTEXT_RE = re.compile(r"(?i)\b(?:Original|First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth|Thirteenth|Fourteenth|Fifteenth|Sixteenth|Seventeenth|Eighteenth|Nineteenth|Twentieth|Twenty-First|Twenty Second|Twenty-Second)\b")
_REVISION_GLUE_RE = re.compile(
    r"\b\d+(Original|First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth|Thirteenth|Fourteenth|Fifteenth|Sixteenth|Seventeenth|Eighteenth|Nineteenth|Twentieth)\b"
)


def build_nc_redline_lead_audit(
    database_path: Path | None = None,
) -> dict[str, Any]:
    confidence_report = build_nc_confidence_audit(database_path)
    source_rows = list(confidence_report["rows"])
    crossrefs_by_family = _load_crossrefs_by_family(database_path, source_rows)
    page_clues_by_family = _load_page_clues_by_family(database_path, source_rows)
    rows = _build_rows(
        source_rows,
        crossrefs_by_family=crossrefs_by_family,
        page_clues_by_family=page_clues_by_family,
    )
    return {
        "generated_at": date.today().isoformat(),
        "family_count": len(rows),
        "families_with_actionable_clues": len(
            [row for row in rows if int(row.get("actionable_clue_count") or 0) > 0]
        ),
        "recommended_action_counts": dict(
            sorted(Counter(str(row["recommended_action"]) for row in rows).items())
        ),
        "rows": rows,
    }


def export_nc_redline_lead_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_nc_redline_lead_audit(database_path)

    rows_csv = output_dir / "nc_redline_lead_audit_rows.csv"
    summary_json = output_dir / "nc_redline_lead_audit_summary.json"
    markdown_path = output_dir / "nc_redline_lead_audit.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _load_crossrefs_by_family(
    database_path: Path | None,
    source_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    db_path = str(Path(database_path or "data/db/duke_rates.db"))
    family_keys = [
        str(row["family_key"])
        for row in source_rows
        if int(row.get("redline_doc_count") or 0) > 0
    ]
    by_family: dict[str, list[dict[str, Any]]] = {}
    for family_key in family_keys:
        by_family[family_key] = scan_redlines_for_crossrefs(
            db_path,
            family_key_pattern=family_key,
            max_pages=3,
        )
    return by_family


def _load_page_clues_by_family(
    database_path: Path | None,
    source_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    import sqlite3

    family_keys = [
        str(row["family_key"])
        for row in source_rows
        if int(row.get("redline_doc_count") or 0) > 0
    ]
    if not family_keys:
        return {}

    conn = sqlite3.connect(str(Path(database_path or "data/db/duke_rates.db")))
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in family_keys)
        rows = conn.execute(
            f"""
            WITH matched_fingerprints AS (
                SELECT
                    hd.id AS historical_document_id,
                    hd.family_key,
                    hd.local_path,
                    hd.start_page,
                    hd.end_page,
                    COALESCE(df.is_redline_candidate, 0) AS is_redline_candidate,
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
                WHERE hd.family_key IN ({placeholders})
                  AND hd.local_path IS NOT NULL
                  AND TRIM(hd.local_path) <> ''
            )
            SELECT
                historical_document_id,
                family_key,
                local_path,
                start_page,
                end_page
            FROM matched_fingerprints
            WHERE rn = 1
              AND is_redline_candidate = 1
            ORDER BY family_key, local_path, start_page, end_page, historical_document_id
            """,
            family_keys,
        ).fetchall()
    finally:
        conn.close()

    by_family: dict[str, dict[str, Any]] = {}
    for row in rows:
        family_key = str(row["family_key"])
        page_clue_report = _extract_page_level_clues(
            pdf_path=str(row["local_path"]),
            start_page=int(row["start_page"]) if row["start_page"] is not None else None,
            end_page=int(row["end_page"]) if row["end_page"] is not None else None,
        )
        if not page_clue_report["actionable_clues"]:
            continue
        bucket = by_family.setdefault(
            family_key,
            {
                "page_clue_doc_count": 0,
                "actionable_clue_count": 0,
                "top_actionable_clues": [],
            },
        )
        bucket["page_clue_doc_count"] = int(bucket["page_clue_doc_count"]) + 1
        merged = list(bucket["top_actionable_clues"]) + list(page_clue_report["actionable_clues"])
        deduped: list[str] = []
        seen: set[str] = set()
        for item in merged:
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        bucket["top_actionable_clues"] = deduped[:8]
        bucket["actionable_clue_count"] = len(deduped)
    return by_family


def _build_rows(
    source_rows: list[dict[str, Any]],
    *,
    crossrefs_by_family: dict[str, list[dict[str, Any]]],
    page_clues_by_family: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in source_rows:
        redline_doc_count = int(row.get("redline_doc_count") or 0)
        clue_doc_count = int(row.get("redline_clue_doc_count") or 0)
        if redline_doc_count <= 0 and clue_doc_count <= 0:
            continue

        family_key = str(row["family_key"])
        crossrefs = crossrefs_by_family.get(family_key, [])
        page_clues = page_clues_by_family.get(family_key, {})
        actionable_clue_count = int(page_clues.get("actionable_clue_count") or 0)
        page_clue_doc_count = int(page_clues.get("page_clue_doc_count") or 0)
        top_actionable_clues = list(page_clues.get("top_actionable_clues") or [])
        recommended_action = _recommended_action(
            row,
            actionable_clue_count=actionable_clue_count,
        )
        docket_numbers = sorted(
            {
                str(docket)
                for item in crossrefs
                for docket in item.get("docket_numbers", [])
                if docket
            }
        )
        leaf_nos = sorted(
            {
                str(leaf)
                for item in crossrefs
                for leaf in item.get("leaf_nos", [])
                if leaf
            }
        )
        supersedes_leaf_nos = sorted(
            {
                str(leaf)
                for item in crossrefs
                for leaf in item.get("supersedes_leaf_nos", [])
                if leaf
            }
        )
        filing_dates = sorted(
            {
                str(item.get("filing_date"))
                for item in crossrefs
                if item.get("filing_date")
            }
        )
        lead_row = {
                "utility": row["utility"],
                "family_key": family_key,
                "title": row["title"],
                "confidence_score": row["confidence_score"],
                "confidence_tier": row["confidence_tier"],
                "timeline_status": row["timeline_status"],
                "gap_opportunity_count": row["gap_opportunity_count"],
                "anomaly_count": row["anomaly_count"],
                "redline_doc_count": redline_doc_count,
                "corroborated_redline_doc_count": int(row.get("corroborated_redline_doc_count") or 0),
                "unpaired_redline_doc_count": int(row.get("unpaired_redline_doc_count") or 0),
                "redline_clue_doc_count": clue_doc_count,
                "page_clue_doc_count": page_clue_doc_count,
                "actionable_clue_count": actionable_clue_count,
                "dual_rate_pair_doc_count": int(row.get("dual_rate_pair_doc_count") or 0),
                "comparative_phrase_doc_count": int(row.get("comparative_phrase_doc_count") or 0),
                "insert_delete_marker_doc_count": int(row.get("insert_delete_marker_doc_count") or 0),
                "supersession_clue_doc_count": int(row.get("supersession_clue_doc_count") or 0),
                "max_dual_rate_pair_count": int(row.get("max_dual_rate_pair_count") or 0),
                "crossref_pdf_count": len(crossrefs),
                "docket_numbers": json.dumps(docket_numbers),
                "leaf_nos": json.dumps(leaf_nos),
                "supersedes_leaf_nos": json.dumps(supersedes_leaf_nos),
                "filing_dates": json.dumps(filing_dates),
                "top_actionable_clues": json.dumps(top_actionable_clues),
                "recommended_action": recommended_action,
                "search_hint": _search_hint(
                    row,
                    docket_numbers=docket_numbers,
                    leaf_nos=leaf_nos,
                    top_actionable_clues=top_actionable_clues,
                ),
                "notes": _notes(
                    row,
                    recommended_action,
                    docket_numbers=docket_numbers,
                    leaf_nos=leaf_nos,
                    filing_dates=filing_dates,
                    actionable_clue_count=actionable_clue_count,
                    top_actionable_clues=top_actionable_clues,
                ),
        }
        lead_row["priority_score"] = _priority_score(lead_row, recommended_action)
        lead_row["priority_band"] = _priority_band(int(lead_row["priority_score"]))
        rows.append(lead_row)

    rows.sort(
        key=lambda item: (
            int(item["priority_score"]),
            int(item["actionable_clue_count"]),
            int(item["unpaired_redline_doc_count"]),
            -float(item["confidence_score"]),
            str(item["family_key"]),
        ),
        reverse=True,
    )
    return rows


def _recommended_action(
    row: dict[str, Any],
    *,
    actionable_clue_count: int = 0,
) -> str:
    clue_doc_count = max(
        int(row.get("redline_clue_doc_count") or 0),
        int(actionable_clue_count or 0),
    )
    unpaired_redline_doc_count = int(row.get("unpaired_redline_doc_count") or 0)
    confidence_score = float(row.get("confidence_score") or 0.0)
    anomaly_count = int(row.get("anomaly_count") or 0)
    gap_count = int(row.get("gap_opportunity_count") or 0)

    if clue_doc_count > 0 and unpaired_redline_doc_count > 0:
        return "use_redline_clues_to_find_clean_companions"
    if unpaired_redline_doc_count > 0:
        return "link_redlines_to_clean_companions"
    if clue_doc_count > 0 and (anomaly_count > 0 or gap_count > 0):
        return "compare_redlines_against_clean_versions"
    if confidence_score >= 80.0:
        return "redline_corroboration_only"
    return "monitor_redline_family"


def _priority_score(row: dict[str, Any], recommended_action: str) -> int:
    base = 0
    base += max(
        int(row.get("redline_clue_doc_count") or 0),
        int(row.get("actionable_clue_count") or 0),
    ) * 20
    base += int(row.get("unpaired_redline_doc_count") or 0) * 15
    base += int(row.get("supersession_clue_doc_count") or 0) * 8
    base += int(row.get("comparative_phrase_doc_count") or 0) * 5
    base += min(20, int(row.get("gap_opportunity_count") or 0) * 2)
    base += min(15, int(row.get("anomaly_count") or 0))
    if recommended_action == "use_redline_clues_to_find_clean_companions":
        base += 20
    elif recommended_action == "link_redlines_to_clean_companions":
        base += 10
    return base


def _priority_band(priority_score: int) -> str:
    if priority_score >= 80:
        return "high"
    if priority_score >= 35:
        return "medium"
    return "low"


def _search_hint(
    row: dict[str, Any],
    *,
    docket_numbers: list[str],
    leaf_nos: list[str],
    top_actionable_clues: list[str],
) -> str:
    family_key = str(row.get("family_key") or "")
    schedule_code = str(row.get("schedule_code") or "")
    title = str(row.get("title") or "")
    clue_tokens = []
    for clue in top_actionable_clues[:2]:
        parts = str(clue).split("|", 1)
        clue_tokens.append(parts[1].strip() if len(parts) == 2 else parts[0].strip())
    tokens = [
        token
        for token in [*docket_numbers[:2], *leaf_nos[:2], *clue_tokens, schedule_code, title, family_key.split("-")[-1]]
        if token
    ]
    return " | ".join(tokens[:5])


def _notes(
    row: dict[str, Any],
    recommended_action: str,
    *,
    docket_numbers: list[str],
    leaf_nos: list[str],
    filing_dates: list[str],
    actionable_clue_count: int,
    top_actionable_clues: list[str],
) -> str:
    fragments = [
        f"timeline={row['timeline_status']}",
        f"redlines={row['redline_doc_count']}",
        f"unpaired={row['unpaired_redline_doc_count']}",
        f"clue_docs={row['redline_clue_doc_count']}",
    ]
    if actionable_clue_count > 0:
        fragments.append(f"actionable_clues={actionable_clue_count}")
    if int(row.get("supersession_clue_doc_count") or 0) > 0:
        fragments.append(f"supersession_clues={row['supersession_clue_doc_count']}")
    if int(row.get("comparative_phrase_doc_count") or 0) > 0:
        fragments.append(f"comparative_clues={row['comparative_phrase_doc_count']}")
    if int(row.get("dual_rate_pair_doc_count") or 0) > 0:
        fragments.append(f"dual_rate_docs={row['dual_rate_pair_doc_count']}")
    if docket_numbers:
        fragments.append(f"dockets={','.join(docket_numbers[:3])}")
    if leaf_nos:
        fragments.append(f"leafs={','.join(leaf_nos[:3])}")
    if filing_dates:
        fragments.append(f"filing_dates={','.join(filing_dates[:2])}")
    if top_actionable_clues:
        fragments.append(f"top_clue={top_actionable_clues[0]}")
    fragments.append(f"action={recommended_action}")
    return "; ".join(fragments)


def _extract_page_level_clues(
    *,
    pdf_path: str,
    start_page: int | None,
    end_page: int | None,
    max_pages: int = 3,
) -> dict[str, Any]:
    if start_page is None:
        return {"actionable_clues": []}

    actionable_clues: list[str] = []
    last_page = end_page if end_page is not None else start_page
    pages_to_scan = range(start_page, min(last_page, start_page + max_pages - 1) + 1)
    for page_number in pages_to_scan:
        try:
            parsed = parse_redline_page(pdf_path, page_number=page_number, max_clues=16)
        except Exception:
            continue
        for clue in parsed.clues:
            clue_text = _normalize_actionable_clue(clue.context_text or clue.text)
            if clue_text:
                actionable_clues.append(f"p{page_number} | {clue_text}")
    deduped: list[str] = []
    seen: set[str] = set()
    for clue in actionable_clues:
        if clue in seen:
            continue
        seen.add(clue)
        deduped.append(clue)
    return {"actionable_clues": deduped[:8]}


def _normalize_actionable_clue(text: str) -> str | None:
    normalized = _clean_clue_text(" ".join(str(text or "").split()))
    if not normalized:
        return None
    lowered = normalized.lower()
    if "@" in normalized:
        return None
    if not re.search(r"[A-Za-z]", normalized):
        return None
    if lowered in {"rate", "year", "availability (north carolina only)", "annual capacity (kw ac)"}:
        return None

    leaf_match = _LEAF_CONTEXT_RE.search(normalized)
    if leaf_match and _REVISION_CONTEXT_RE.search(normalized):
        return leaf_match.group(0)
    if leaf_match and "superseding" in lowered:
        return leaf_match.group(0)

    date_match = _DATE_RE.search(normalized)
    if date_match and ("effective" in lowered or "service rendered" in lowered):
        return normalized

    if "superseding nc" in lowered and "leaf no" in lowered:
        return normalized
    if "revised leaf no" in lowered or "original leaf no" in lowered:
        return normalized
    return None


def _clean_clue_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = cleaned.replace("North CarolinaNC", "North Carolina ")
    cleaned = re.sub(r"\b(E-\d+)\.\s+Sub\b", r"\1, Sub", cleaned)
    cleaned = re.sub(r"\bNC\s+Program\s+[A-Za-z0-9$-]+(?=Original Leaf No\.)", "NC ", cleaned)
    cleaned = _REVISION_GLUE_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


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
        "# NC Redline Lead Audit",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        f"- Families with any redline evidence: {report['family_count']}",
        f"- Families with actionable redline clues: {report['families_with_actionable_clues']}",
        "",
        "Recommended action counts:",
    ]
    for action, count in dict(report["recommended_action_counts"]).items():
        lines.append(f"- `{action}`: {count}")
    lines.extend(
        [
            "",
            "## Ranked Redline Leads",
            "",
            _render_table(rows[:40]),
            "",
        ]
    )
    if rows:
        lines.extend(
            [
                "## Top Actionable Clues",
                "",
            ]
        )
        for row in rows[:15]:
            clues = json.loads(str(row.get("top_actionable_clues") or "[]"))
            if not clues:
                continue
            lines.append(
                f"- `{row['family_key']}`: " + "; ".join(str(clue) for clue in clues[:3])
            )
        lines.append("")
    return "\n".join(lines)


def _render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No redline leads detected._"
    header = "Score  Band    Utility  Family                         Redlines  Clues  Unpaired  Supers  Comp  Action"
    body = []
    for row in rows:
        body.append(
            f"{int(row['priority_score']):>5}  "
            f"{str(row['priority_band']):<6}  "
            f"{str(row['utility']):<7}  "
            f"{str(row['family_key']):<29}  "
            f"{int(row['redline_doc_count']):>8}  "
            f"{int(row['actionable_clue_count']):>5}  "
            f"{int(row['unpaired_redline_doc_count']):>8}  "
            f"{int(row['supersession_clue_doc_count']):>6}  "
            f"{int(row['comparative_phrase_doc_count']):>4}  "
            f"{str(row['recommended_action'])}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


def _split_garbled_sub(sub_str: str) -> list[int]:
    """Split a concatenated sub number from redline OCR (e.g. '12941300' → [1294, 1300])."""
    s = sub_str.strip()
    if not s.isdigit():
        return []
    digits = len(s)
    if digits <= 4:
        return [int(s)]
    if digits == 8:
        return [int(s[:4]), int(s[4:])]
    if digits == 7:
        return [int(s[:4]), int(s[4:])]
    if digits == 6:
        return [int(s[:3]), int(s[3:])]
    return [int(s)]


def _clean_docket_numbers(raw_list: list[str]) -> list[str]:
    """Expand garbled docket numbers from redline OCR into canonical form."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_list:
        m = re.match(r"^([A-Za-z]-\d+),\s*Sub\s+(\d+)$", raw.strip())
        if not m:
            if raw not in seen:
                seen.add(raw)
                result.append(raw)
            continue
        prefix = m.group(1).upper()
        for sub in _split_garbled_sub(m.group(2)):
            cleaned = f"{prefix}, Sub {sub}"
            if cleaned not in seen:
                seen.add(cleaned)
                result.append(cleaned)
    return result


def suggest_redline_portal_fetches(
    database_path: "Path | None" = None,
) -> list[dict[str, Any]]:
    """Return dockets found in redline crossrefs that have not yet been fetched from portal.

    Each result has: docket, fetch_count, source_families, priority_band, search_hint
    """
    import sqlite3

    db_path = str(Path(database_path or "data/db/duke_rates.db"))
    report = build_nc_redline_lead_audit(database_path)

    # Collect dockets from all actionable rows
    docket_families: dict[str, list[str]] = {}
    docket_hints: dict[str, str] = {}
    docket_bands: dict[str, str] = {}

    for row in report["rows"]:
        if not row.get("docket_numbers"):
            continue
        raw_dockets = json.loads(row["docket_numbers"])
        cleaned = _clean_docket_numbers(raw_dockets)
        for d in cleaned:
            if d not in docket_families:
                docket_families[d] = []
                docket_hints[d] = str(row.get("search_hint") or "")
                docket_bands[d] = str(row.get("priority_band") or "low")
            docket_families[d].append(str(row["family_key"]))

    # Known valid NC NCUC docket prefixes for Duke utilities
    _VALID_PREFIXES = {"E-2", "E-7"}

    # Check DB counts for each docket
    suggestions: list[dict[str, Any]] = []
    with sqlite3.connect(db_path) as conn:
        for docket, families in docket_families.items():
            m = re.match(r"^([A-Za-z]-\d+),\s*Sub\s+(\d+)$", docket)
            if not m:
                continue
            dn, sub = m.group(1).upper(), int(m.group(2))
            # Skip unrecognized or implausible docket/sub combinations
            if dn not in _VALID_PREFIXES or not (100 <= sub <= 9999):
                continue
            row = conn.execute(
                "SELECT COUNT(*) FROM ncuc_discovery_records WHERE docket_number=? AND sub_number=?",
                (dn, sub),
            ).fetchone()
            count = row[0] if row else 0
            suggestions.append(
                {
                    "docket": docket,
                    "fetch_count": count,
                    "source_families": families,
                    "priority_band": docket_bands[docket],
                    "search_hint": docket_hints[docket],
                }
            )

    suggestions.sort(key=lambda r: (r["fetch_count"], r["priority_band"] != "high"))
    return suggestions


__all__ = [
    "build_nc_redline_lead_audit",
    "export_nc_redline_lead_audit",
    "suggest_redline_portal_fetches",
    "_DEFAULT_OUTPUT_DIR",
]
