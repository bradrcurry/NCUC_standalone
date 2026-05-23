#!/usr/bin/env python
"""
Register harvested authenticated NCUC portal PDFs into ncuc_discovery_records.

This consumes the manifest produced by `harvest_target_ncuc_documents.py`,
skips known non-tariff artifacts, and creates success-state discovery records
that can be ingested by `ncuc import-pipeline --all-downloaded`.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.models.ncuc import (
    NcucAcquisitionMethod,
    NcucDiscoveryRecord,
    NcucFetchStatus,
    NcucFilingClassification,
)

DEFAULT_MANIFEST_PATH = Path("data/ncuc_target_harvest_2026_04_07.json")

SKIP_PATTERNS = (
    "motion",
    "appeal",
    "underwriter certification",
    "pricing of storm recovery bonds",
    "cuca letter",
    "pwh",
)


def should_skip(item: dict) -> bool:
    text = " ".join(
        str(item.get(key, ""))
        for key in ("focus", "attachment_label", "description", "path")
    ).lower()
    return any(pattern in text for pattern in SKIP_PATTERNS)


def parse_docket(docket: str) -> tuple[str, str | None]:
    match = re.match(r"\s*([A-Z]-\d+)(?:\s+Sub\s+(\d+))?\s*$", docket or "", re.I)
    if not match:
        return docket, None
    base = match.group(1).upper()
    sub = match.group(2)
    return base, sub


def infer_classification(item: dict) -> NcucFilingClassification:
    text = " ".join(
        str(item.get(key, ""))
        for key in ("focus", "attachment_label", "description")
    ).lower()
    if "schedule" in text or "rider" in text or "tariff" in text or "compliance" in text:
        return NcucFilingClassification.TARIFF_SHEETS
    return NcucFilingClassification.ATTACHMENT


def infer_leaf_nos(item: dict) -> list[str]:
    values = []
    for key in ("focus", "attachment_label", "description"):
        text = str(item.get(key, ""))
        values.extend(re.findall(r"\bleaf\s*(?:no\.?|#)?\s*(\d{3,4})\b", text, re.I))
        values.extend(re.findall(r"\bleaf[-_\s](\d{3,4})\b", text, re.I))
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", code.upper())


def infer_family_keys(item: dict, utility: str) -> list[str]:
    label = str(item.get("attachment_label", "") or "")
    description = str(item.get("description", "") or "")
    focus = str(item.get("focus", "") or "")
    combined = " ".join((label, description, focus))
    upper = combined.upper()
    focus_upper = focus.upper()

    dep_leaf_match = re.search(r"\bDEP\s+LEAF-(\d{3,4})\b", focus_upper)
    if dep_leaf_match:
        return [f"nc-progress-leaf-{dep_leaf_match.group(1)}"]

    dec_rider_match = re.search(r"\bDEC\s+RIDER\s+([A-Z0-9\-]+)\b", focus_upper)
    if dec_rider_match:
        return [f"nc-carolinas-rider-{normalize_code(dec_rider_match.group(1))}"]

    if utility == "Duke Energy Carolinas":
        schedule_match = re.search(r"DEC-NC\s+SCHEDULE\s+([A-Z0-9\-]+)", upper)
        if schedule_match:
            return [f"nc-carolinas-schedule-{normalize_code(schedule_match.group(1))}"]

        rider_match = re.search(r"DEC-NC\s+RIDER\s+([A-Z0-9\-]+)", upper)
        if rider_match:
            code = normalize_code(rider_match.group(1))
            if code == "SUMMARY":
                return ["nc-carolinas-rider-SUMMARY"]
            return [f"nc-carolinas-rider-{code}"]

        generic_rider_match = re.search(r"\bRIDER\s+([A-Z0-9\-]+)\b", upper)
        if generic_rider_match:
            return [f"nc-carolinas-rider-{normalize_code(generic_rider_match.group(1))}"]

        if "EXISTING DSM" in upper or "EDPR" in upper:
            return ["nc-carolinas-rider-EDPR"]
        if "STORM SECURITIZATION" in upper or re.search(r"\bSTS\b", upper):
            return ["nc-carolinas-rider-STS"]
        if "SUMMARY OF RIDER ADJUSTMENTS" in upper or "RIDER SUMMARY" in upper:
            return ["nc-carolinas-rider-SUMMARY"]

    if utility == "Duke Energy Progress":
        if "JOINT AGENCY" in upper or "JAAR" in upper or re.search(r"\bJAA\b", upper):
            return ["nc-progress-leaf-602"]
        if "BILLING ADJUSTMENT" in upper or re.search(r"\bRIDER\s+BA\b", upper):
            return ["nc-progress-leaf-601"]
        if "STS-2" in upper:
            return ["nc-progress-leaf-613"]
        if "STORM SECURITIZATION" in upper or re.search(r"\bSTS\b", upper):
            return ["nc-progress-leaf-607"]
        if "REVENUE DECOUPLING" in upper or re.search(r"\bRDM\b", upper):
            return ["nc-progress-leaf-608"]

    leaf_nos = infer_leaf_nos(item)
    if leaf_nos and utility == "Duke Energy Progress":
        return [f"nc-progress-leaf-{leaf_nos[0]}"]

    return []


def infer_utility(item: dict, local_path: Path) -> str:
    combined = " ".join(
        str(item.get(key, "") or "")
        for key in ("attachment_label", "description", "focus")
    ).upper()
    if "DUKE ENERGY PROGRESS" in combined or "E-100" in combined:
        return "Duke Energy Progress"
    if "DUKE ENERGY CAROLINAS" in combined or "DEC-NC" in combined:
        return "Duke Energy Carolinas"
    return "Duke Energy Carolinas" if "\\dec\\" in str(local_path).lower() else "Duke Energy Progress"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Harvest manifest JSON to register.",
    )
    parser.add_argument(
        "--focus",
        action="append",
        default=[],
        help="Restrict registration to one or more manifest focus labels.",
    )
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="Register rows even when the manifest marks them as duplicates of existing DB content.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview rows that would be registered without writing discovery records.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest_path = args.manifest
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    docs = payload.get("documents", [])
    selected_focus = {value.strip() for value in args.focus if value and value.strip()}

    settings = Settings()
    repo = Repository(settings.database_path)

    registered = 0
    skipped = 0
    duplicate_skipped = 0
    dry_run = 0

    for item in docs:
        if item.get("status") not in {"downloaded", "exists"}:
            continue
        if selected_focus and item.get("focus", "") not in selected_focus:
            continue
        if should_skip(item):
            skipped += 1
            continue
        if item.get("duplicate_of") and not args.allow_duplicates:
            duplicate_skipped += 1
            continue

        local_path = Path(item["path"])
        if not local_path.exists():
            continue

        docket_number, sub_number = parse_docket(item.get("docket", ""))
        utility = infer_utility(item, local_path)
        filing_date = item.get("date_filed", "")
        if filing_date:
            try:
                filing_date = datetime.strptime(filing_date, "%m/%d/%Y").date().isoformat()
            except ValueError:
                pass

        record = NcucDiscoveryRecord(
            docket_number=docket_number,
            sub_number=sub_number,
            utility=utility,
            filing_title=item.get("attachment_label") or item.get("description") or local_path.name,
            filing_date=filing_date or None,
            proceeding_type="tariff_adjustment",
            filing_classification=infer_classification(item),
            referenced_leaf_nos=infer_leaf_nos(item),
            family_keys=infer_family_keys(item, utility),
            discovered_url=item.get("view_url"),
            viewer_url=item.get("view_url"),
            download_url=item.get("view_url"),
            acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
            fetch_status=NcucFetchStatus.SUCCESS,
            local_path=str(local_path),
            content_hash=item.get("content_hash"),
            content_type="application/pdf",
            file_size_bytes=item.get("size_bytes") or local_path.stat().st_size,
            provenance_notes=[
                "authenticated_portal_harvest",
                f"focus={item.get('focus', '')}",
                f"docket={item.get('docket', '')}",
            ],
            search_query=f"authenticated_portal_harvest:{item.get('focus', '')}",
            doc_quality_tier="T2",
            search_confidence_score=0.9,
            search_ideality="probable",
            fetched_at=datetime.now(UTC),
            metadata_json=json.dumps(
                {
                    "harvest_manifest": str(manifest_path),
                    "focus": item.get("focus"),
                    "description": item.get("description"),
                    "duplicate_of": item.get("duplicate_of"),
                }
            ),
        )
        if args.dry_run:
            dry_run += 1
        else:
            repo.upsert_ncuc_discovery_record(record)
            registered += 1

    print(f"registered={registered}")
    print(f"skipped={skipped}")
    print(f"duplicate_skipped={duplicate_skipped}")
    print(f"dry_run={dry_run}")


if __name__ == "__main__":
    main()
