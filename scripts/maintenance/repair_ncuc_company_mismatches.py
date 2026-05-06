from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.historical.ncuc.importer import NcucPipelineImporter
from duke_rates.models.ncuc import NcucDiscoveryRecord


BAD_DOC_SQL = """
SELECT
    hd.id AS historical_id,
    hd.family_key,
    hd.title,
    hd.local_path,
    nd.id AS discovery_id
FROM historical_documents hd
LEFT JOIN ncuc_discovery_records nd ON nd.local_path = hd.local_path
WHERE hd.family_key LIKE 'nc-progress-%'
  AND (
    (
      lower(coalesce(hd.title, '')) LIKE '%duke energy carolinas%'
      AND lower(coalesce(hd.title, '')) NOT LIKE '%duke energy progress%'
    )
    OR lower(coalesce(hd.title, '')) LIKE '%duke power%'
    OR (
      lower(coalesce(hd.local_path, '')) LIKE '%duke-energy-carolinas%'
      AND lower(coalesce(hd.local_path, '')) NOT LIKE '%duke-energy-progress%'
    )
    OR lower(coalesce(hd.local_path, '')) LIKE '%duke-power%'
  )
ORDER BY hd.family_key, hd.id
"""


def _load_bad_docs(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(BAD_DOC_SQL).fetchall()
    finally:
        conn.close()


def _purge_bad_docs(db_path: Path, historical_ids: list[int]) -> None:
    if not historical_ids:
        return
    placeholders = ",".join("?" for _ in historical_ids)
    conn = sqlite3.connect(db_path)
    try:
        version_ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM tariff_versions WHERE historical_document_id IN ({placeholders})",
                historical_ids,
            ).fetchall()
        ]
        if version_ids:
            version_placeholders = ",".join("?" for _ in version_ids)
            conn.execute(
                f"DELETE FROM tariff_charges WHERE version_id IN ({version_placeholders})",
                version_ids,
            )
            conn.execute(
                f"DELETE FROM tariff_versions WHERE id IN ({version_placeholders})",
                version_ids,
            )
        conn.execute(
            f"DELETE FROM historical_documents WHERE id IN ({placeholders})",
            historical_ids,
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair nc-progress historical mappings contaminated by Duke Power/DEC filings."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete contaminated nc-progress mappings and re-import affected discovery records.",
    )
    args = parser.parse_args()

    settings = get_settings()
    repo = Repository(settings.database_path)
    importer = NcucPipelineImporter(settings, repo)

    bad_docs = _load_bad_docs(settings.database_path)
    if not bad_docs:
        print("No contaminated nc-progress historical mappings found.")
        return

    unique_discovery_ids = sorted({row["discovery_id"] for row in bad_docs if row["discovery_id"] is not None})

    print(f"Found {len(bad_docs)} contaminated historical_documents rows")
    print(f"Affected discovery records: {len(unique_discovery_ids)}")
    for row in bad_docs:
        print(
            f"- historical_id={row['historical_id']} family={row['family_key']} "
            f"discovery_id={row['discovery_id']} path={row['local_path']}"
        )

    if not args.apply:
        print("\nDry run only. Re-run with --apply to purge and re-import.")
        return

    historical_ids = [int(row["historical_id"]) for row in bad_docs]
    _purge_bad_docs(settings.database_path, historical_ids)
    print(f"\nPurged {len(historical_ids)} contaminated historical_documents rows.")

    for discovery_id in unique_discovery_ids:
        with repo._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ncuc_discovery_records WHERE id = ?",
                (discovery_id,),
            ).fetchone()
        if not row:
            print(f"- discovery_id={discovery_id}: missing discovery record, skipped")
            continue
        record = NcucDiscoveryRecord(
            id=row["id"],
            docket_number=row["docket_number"],
            sub_number=row["sub_number"],
            utility=row["utility"],
            filing_title=row["filing_title"],
            filing_date=row["filing_date"],
            proceeding_type=row["proceeding_type"],
            filing_classification=row["filing_classification"],
            exhibit_label=row["exhibit_label"],
            referenced_schedule_codes=[],
            referenced_rider_codes=[],
            referenced_leaf_nos=[],
            family_keys=[],
            discovered_url=row["discovered_url"],
            viewer_url=row["viewer_url"],
            attachment_url=row["attachment_url"],
            download_url=row["download_url"],
            acquisition_method=row["acquisition_method"],
            fetch_status=row["fetch_status"],
            local_path=row["local_path"],
            content_hash=row["content_hash"],
            content_type=row["content_type"],
            file_size_bytes=row["file_size_bytes"],
            provenance_notes=[],
            search_query=row["search_query"],
            page_title=row["page_title"],
            created_at=None,
            fetched_at=None,
            error_detail=row["error_detail"],
            metadata_json=row["metadata_json"],
        )
        created = importer.mine_discovery_record_spans(record)
        print(f"- discovery_id={discovery_id}: re-imported {len(created)} spans")


if __name__ == "__main__":
    main()
