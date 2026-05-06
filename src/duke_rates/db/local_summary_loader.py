from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from duke_rates.db.ncuc_loader import load_rider_summaries
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.rider_summary import parse_rider_summary


def load_local_nc_rider_summaries(
    conn: sqlite3.Connection,
    *,
    company: str,
    replace: bool = True,
) -> dict[str, object]:
    company_key = company.lower()
    if company_key not in {"progress", "carolinas"}:
        raise ValueError("company must be 'progress' or 'carolinas'")

    leaf_no = "600" if company_key == "progress" else "99"
    selected_paths = _select_summary_paths(company_key, leaf_no)
    records = []
    for path in selected_paths:
        text = extract_pdf_text(path)
        result = parse_rider_summary(text, source_pdf=str(path), leaf_no=leaf_no)
        records.append(
            {
                "source_pdf": str(path),
                "leaf_no": leaf_no,
                "effective_date": result.effective_date,
                "docket_number": result.docket_number,
                "order_date": result.order_date,
                "supersedes": result.supersedes,
                "rate_classes": [
                    {
                        "rate_class": block.rate_class,
                        "applicable_schedules": block.applicable_schedules,
                        "total_cents_per_kwh": block.total_cents_per_kwh,
                        "total_dollars_per_kw": block.total_dollars_per_kw,
                        "line_items": [
                            {
                                "label": item.label,
                                "rider_code": item.rider_code,
                                "cents_per_kwh": item.cents_per_kwh,
                                "dollars_per_kw": item.dollars_per_kw,
                                "effective_date": item.effective_date,
                                "is_section_header": item.is_section_header,
                                "is_subtotal": item.is_subtotal,
                                "is_total": item.is_total,
                            }
                            for item in block.line_items
                        ],
                    }
                    for block in result.rate_classes
                ],
            }
        )

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)
        temp_path = Path(fh.name)

    try:
        inserted, skipped = load_rider_summaries(conn, temp_path, replace=replace)
    finally:
        temp_path.unlink(missing_ok=True)

    return {
        "company": company_key,
        "source_count": len(selected_paths),
        "sources": [str(path) for path in selected_paths],
        "blocks_inserted": inserted,
        "blocks_skipped": skipped,
    }


def load_nc_rider_summaries_from_paths(
    conn: sqlite3.Connection,
    *,
    paths: list[Path],
    leaf_no: str,
    replace: bool = True,
) -> dict[str, object]:
    records = []
    for path in paths:
        text = extract_pdf_text(path)
        result = parse_rider_summary(text, source_pdf=str(path), leaf_no=leaf_no)
        records.append(
            {
                "source_pdf": str(path),
                "leaf_no": leaf_no,
                "effective_date": result.effective_date,
                "docket_number": result.docket_number,
                "order_date": result.order_date,
                "supersedes": result.supersedes,
                "rate_classes": [
                    {
                        "rate_class": block.rate_class,
                        "applicable_schedules": block.applicable_schedules,
                        "total_cents_per_kwh": block.total_cents_per_kwh,
                        "total_dollars_per_kw": block.total_dollars_per_kw,
                        "line_items": [
                            {
                                "label": item.label,
                                "rider_code": item.rider_code,
                                "cents_per_kwh": item.cents_per_kwh,
                                "dollars_per_kw": item.dollars_per_kw,
                                "effective_date": item.effective_date,
                                "is_section_header": item.is_section_header,
                                "is_subtotal": item.is_subtotal,
                                "is_total": item.is_total,
                            }
                            for item in block.line_items
                        ],
                    }
                    for block in result.rate_classes
                ],
            }
        )

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)
        temp_path = Path(fh.name)

    try:
        inserted, skipped = load_rider_summaries(conn, temp_path, replace=replace)
    finally:
        temp_path.unlink(missing_ok=True)

    return {
        "leaf_no": leaf_no,
        "source_count": len(paths),
        "sources": [str(path) for path in paths],
        "blocks_inserted": inserted,
        "blocks_skipped": skipped,
    }


def _select_summary_paths(company: str, leaf_no: str) -> list[Path]:
    current_root = Path("data") / "raw" / "nc" / company / "rider"
    historical_root = Path("data") / "historical" / "raw" / "nc" / company / "rider"

    candidates: list[tuple[int, str, Path]] = []
    for root_label, root in (("historical", historical_root), ("current", current_root)):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.pdf")):
            lowered = path.name.lower()
            if f"leaf-no-{leaf_no}" not in lowered and "summary-of-rider" not in lowered and "ridersummary" not in lowered:
                continue
            if leaf_no == "600" and "leaf-no-600" not in lowered and "summary-of-rider-adjustments" not in lowered:
                continue
            if leaf_no == "99" and "leaf-no-99" not in lowered and "ncriders" not in lowered and "ridersummary" not in lowered:
                continue
            priority = 0 if root_label == "historical" else 1
            candidates.append((priority, path.name.lower(), path))

    chosen_by_effective_name: dict[str, Path] = {}
    for _, _, path in candidates:
        name = path.name.lower()
        effect_key = _effect_key_from_name(name)
        if effect_key not in chosen_by_effective_name:
            chosen_by_effective_name[effect_key] = path

    return list(chosen_by_effective_name.values())


def _effect_key_from_name(name: str) -> str:
    marker = "-eff-"
    if marker in name:
        return name.split(marker, maxsplit=1)[1][:10]
    return "current"
