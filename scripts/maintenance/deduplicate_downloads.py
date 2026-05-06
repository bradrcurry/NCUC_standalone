#!/usr/bin/env python3
"""
Deduplicate and checksum all downloaded documents.

This script:
1. Scans both ncuc_discovery_records and historical_documents tables
2. Calculates checksums for any documents with local_path but no content_hash
3. Identifies and reports duplicate files (same content, different records)
4. Optionally deduplicates by keeping only the highest-quality version

Usage:
    python scripts/maintenance/deduplicate_downloads.py --check    # Just report
    python scripts/maintenance/deduplicate_downloads.py --fix      # Fix checksums
    python scripts/maintenance/deduplicate_downloads.py --remove   # Remove duplicates
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from collections import defaultdict

from duke_rates.config import Settings
from duke_rates.db.sqlite import connect
from duke_rates.db.duplicate_detector import (
    calculate_file_checksum,
    find_duplicate_by_checksum,
    update_checksum_in_ncuc_discovery,
    update_checksum_in_historical_docs,
    get_all_checksums_in_database,
)


def check_and_report_duplicates(conn: sqlite3.Connection) -> None:
    """Scan database and report all duplicate files."""
    cursor = conn.cursor()

    # Get all records with local_path
    cursor.execute(
        """
        SELECT 'ncuc_discovery' as source, id, local_path, filing_title, content_hash
        FROM ncuc_discovery_records
        WHERE local_path IS NOT NULL
        UNION ALL
        SELECT 'historical_documents', id, local_path, title, content_hash
        FROM historical_documents
        WHERE local_path IS NOT NULL
        """
    )

    duplicates_by_hash = defaultdict(list)
    missing_checksums = []

    for row in cursor.fetchall():
        source, rec_id, local_path, title, content_hash = row
        file_path = Path(local_path)

        if not file_path.exists():
            print(f"[WARNING] File not found: {local_path}")
            continue

        if not content_hash:
            missing_checksums.append((source, rec_id, local_path, title))
        else:
            duplicates_by_hash[content_hash].append(
                (source, rec_id, local_path, title)
            )

    # Report duplicates
    duplicate_count = 0
    for checksum, records in duplicates_by_hash.items():
        if len(records) > 1:
            duplicate_count += len(records) - 1
            print(f"\n[DUPLICATE] {len(records)} copies of same content (hash: {checksum[:12]}...)")
            for source, rec_id, local_path, title in records:
                print(f"  - {source} ID {rec_id}: {title[:60]}")
                print(f"    Path: {local_path}")

    # Report missing checksums
    if missing_checksums:
        print(f"\n[MISSING CHECKSUMS] {len(missing_checksums)} files need hashing:")
        for source, rec_id, local_path, title in missing_checksums[:10]:
            print(f"  - {source} ID {rec_id}: {title[:60]}")
        if len(missing_checksums) > 10:
            print(f"  ... and {len(missing_checksums) - 10} more")

    print(f"\n[SUMMARY]")
    print(f"  Total duplicate files: {duplicate_count}")
    print(f"  Records missing checksums: {len(missing_checksums)}")


def update_all_checksums(conn: sqlite3.Connection) -> None:
    """Calculate and store checksums for all documents without them."""
    cursor = conn.cursor()

    # Get ncuc_discovery_records without checksums
    cursor.execute(
        """
        SELECT id, local_path FROM ncuc_discovery_records
        WHERE local_path IS NOT NULL AND content_hash IS NULL
        """
    )
    ncuc_records = cursor.fetchall()

    for rec_id, local_path in ncuc_records:
        file_path = Path(local_path)
        if file_path.exists():
            try:
                update_checksum_in_ncuc_discovery(conn, rec_id, file_path)
                print(f"[OK] Updated checksum for ncuc_discovery ID {rec_id}")
            except Exception as e:
                print(f"[ERROR] Failed to hash ncuc_discovery ID {rec_id}: {e}")

    # Get historical_documents without checksums
    cursor.execute(
        """
        SELECT id, local_path FROM historical_documents
        WHERE local_path IS NOT NULL AND content_hash IS NULL
        """
    )
    hist_records = cursor.fetchall()

    for rec_id, local_path in hist_records:
        file_path = Path(local_path)
        if file_path.exists():
            try:
                update_checksum_in_historical_docs(conn, rec_id, file_path)
                print(f"[OK] Updated checksum for historical_documents ID {rec_id}")
            except Exception as e:
                print(f"[ERROR] Failed to hash historical_documents ID {rec_id}: {e}")

    print(f"\n[COMPLETE] Updated checksums for {len(ncuc_records) + len(hist_records)} documents")


def remove_duplicate_files(conn: sqlite3.Connection, keep_quality: bool = True) -> None:
    """
    Remove duplicate files, optionally keeping highest-quality version.

    If keep_quality=True, keeps the document with doc_quality_tier='T1' or 'T2'
    """
    cursor = conn.cursor()

    # Get all checksums and their records
    cursor.execute(
        """
        SELECT content_hash, COUNT(*) as count
        FROM (
            SELECT content_hash FROM ncuc_discovery_records WHERE content_hash IS NOT NULL
            UNION ALL
            SELECT content_hash FROM historical_documents WHERE content_hash IS NOT NULL
        )
        GROUP BY content_hash
        HAVING count > 1
        """
    )

    duplicate_hashes = [row[0] for row in cursor.fetchall()]
    print(f"Found {len(duplicate_hashes)} checksums with multiple records")

    for checksum in duplicate_hashes:
        # Get all records with this hash
        cursor.execute(
            """
            SELECT 'ncuc_discovery' as source, id, content_hash, doc_quality_tier
            FROM ncuc_discovery_records
            WHERE content_hash = ?
            UNION ALL
            SELECT 'historical_documents', id, content_hash, NULL
            FROM historical_documents
            WHERE content_hash = ?
            """,
            (checksum, checksum),
        )

        records = cursor.fetchall()
        if len(records) < 2:
            continue

        print(f"\n[DEDUP] {len(records)} records with hash {checksum[:12]}...")

        if keep_quality:
            # Keep the highest quality tier
            tiers = {"T1": 3, "T2": 2, "T3": 1, None: 0}
            records = sorted(
                records,
                key=lambda r: tiers.get(r[3], 0),
                reverse=True,
            )

        keep_source, keep_id = records[0][0], records[0][1]
        print(f"  Keeping: {keep_source} ID {keep_id}")

        # Remove others (just mark as duplicate reference)
        for source, rec_id, _, _ in records[1:]:
            if source == "ncuc_discovery":
                cursor.execute(
                    """
                    UPDATE ncuc_discovery_records
                    SET fetch_status = 'duplicate'
                    WHERE id = ?
                    """,
                    (rec_id,),
                )
            else:
                cursor.execute(
                    """
                    UPDATE historical_documents
                    SET metadata_json = json_set(
                        COALESCE(metadata_json, '{}'),
                        '$.duplicate_of',
                        ?
                    )
                    WHERE id = ?
                    """,
                    (f"{keep_source}:{keep_id}", rec_id),
                )
            print(f"  Marked as duplicate: {source} ID {rec_id}")

    conn.commit()
    print("\n[COMPLETE] Deduplication markers added")


def main():
    parser = argparse.ArgumentParser(description="Deduplicate downloaded documents")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check and report duplicates (default)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Calculate and store checksums",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Mark duplicate records",
    )

    args = parser.parse_args()

    settings = Settings()
    conn = connect(settings.database_path)

    try:
        if args.fix:
            print("[UPDATING] Calculating checksums for documents without them...\n")
            update_all_checksums(conn)
        elif args.remove:
            print("[DEDUPING] Marking duplicate records...\n")
            remove_duplicate_files(conn)
        else:
            print("[CHECKING] Scanning for duplicates...\n")
            check_and_report_duplicates(conn)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
