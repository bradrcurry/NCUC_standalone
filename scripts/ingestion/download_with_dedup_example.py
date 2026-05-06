#!/usr/bin/env python3
"""
Example: Download NCUC documents with automatic duplicate detection.

This script demonstrates the complete workflow:
1. Authenticate to NCUC portal
2. Search for documents
3. Check for duplicates before downloading
4. Download files with checksum calculation
5. Report statistics

Usage:
    python scripts/ingestion/download_with_dedup_example.py

Reference:
    - DUPLICATE_DETECTION_GUIDE.md
    - NCUC_PORTAL_WORKING_METHOD.md
    - SmartNcucDownloader class
"""

from __future__ import annotations

import logging
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.session import (
    create_authenticated_context,
    close_authenticated_context,
    resolve_docket_ids,
    get_docket_documents,
    download_view_file,
)
from duke_rates.historical.ncuc.smart_downloader import SmartNcucDownloader

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    """Download documents with deduplication."""
    settings = Settings()
    conn = connect(settings.database_path)

    # Initialize downloader
    download_dir = settings.downloads_dir / "ncuc_tariff"
    downloader = SmartNcucDownloader(conn, download_dir)

    # Authenticate to portal
    logger.info("Authenticating to NCUC portal...")
    pw, ctx, page = create_authenticated_context(settings)

    try:
        # Example: Search for documents in dockets
        docket_numbers = [
            "E-2 Sub 1354",  # Joint Agency Asset Rider
            "E-2 Sub 1143",  # Environmental Improvement Rider
        ]

        for docket_number in docket_numbers:
            logger.info(f"\n{'='*60}")
            logger.info(f"Searching docket: {docket_number}")
            logger.info(f"{'='*60}")

            # Step 1: Resolve docket ID
            docket_results = resolve_docket_ids(page, docket_number)
            if not docket_results:
                logger.warning(f"No dockets found for: {docket_number}")
                continue

            docket_id = docket_results[0]["docket_id"]
            logger.info(f"Resolved to docket ID: {docket_id}")

            # Step 2: Get documents in docket
            documents = get_docket_documents(page, docket_id)
            logger.info(f"Found {len(documents)} documents in docket")

            # Step 3: Download each document with deduplication
            for idx, doc in enumerate(documents, 1):
                doc_type = doc.get("doc_type", "Unknown").strip()
                description = doc.get("description", "Untitled")[:60]
                view_file_urls = doc.get("view_file_urls", [])

                logger.info(f"\n[{idx}/{len(documents)}] {description}")
                logger.info(f"  Type: {doc_type}")

                if not view_file_urls:
                    logger.warning("  No download URL available")
                    continue

                view_file_url = view_file_urls[0]

                # Define download function for this session
                def download_func(url: str, dest_path: Path) -> int:
                    return download_view_file(page, url, dest_path)

                # Download with deduplication checking
                result = downloader.download_with_dedup(
                    document_url=view_file_url,
                    document_title=description,
                    docket_number=docket_number,
                    download_func=download_func,
                )

                logger.info(f"  Result: {result}")

                if result.success and result.duplicate_of:
                    logger.warning(f"  Duplicate of: {result.duplicate_of['source']} ID {result.duplicate_of['id']}")

        # Print summary
        logger.info(f"\n{'='*60}")
        downloader.print_summary()

    finally:
        close_authenticated_context(pw, ctx)
        conn.close()


if __name__ == "__main__":
    main()
