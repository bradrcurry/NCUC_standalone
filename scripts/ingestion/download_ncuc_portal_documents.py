#!/usr/bin/env python
"""
Targeted downloader for the small enhanced-search document set.

This is not the default portal intake workflow. It exists for a narrow,
predefined enhanced-search batch that is loaded from ENHANCED_SEARCH_FILE.
"""

import json
import sqlite3
from pathlib import Path
import hashlib
import logging
from datetime import datetime

from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import (
    create_authenticated_context,
    close_authenticated_context,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

settings = Settings()
DB_PATH = settings.database_path or "data/db/duke_rates.db"

ENHANCED_SEARCH_FILE = "data/dep_gap_search_enhanced.json"

def load_enhanced_docs():
    """Load the 11 documents from enhanced search."""
    with open(ENHANCED_SEARCH_FILE) as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get('documents', [])

def find_pdf_links_on_detail_page(page, detail_url: str) -> list[str]:
    """
    Navigate to document detail page and extract all PDF download links.
    Returns list of ViewFile URLs.
    """
    logger.info(f"Opening detail page: {detail_url}")
    try:
        page.goto(detail_url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)

        # Extract all links that might be PDF downloads
        links = page.locator("a").evaluate_all("""
            els => els
              .map(e => ({text: (e.innerText || '').trim(), href: e.href}))
              .filter(x => x.href && (x.href.includes('ViewFile') || x.href.includes('GetFile')))
        """)

        pdf_links = [link["href"] for link in links if link["href"]]
        logger.info(f"Found {len(pdf_links)} ViewFile links on detail page")
        return pdf_links

    except Exception as e:
        logger.error(f"Error extracting links from detail page: {e}")
        return []

def download_from_url(page, url: str, dest_path: Path) -> bool:
    """
    Download a PDF from a ViewFile URL using expect_download().
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Downloading from: {url}")

    try:
        with page.expect_download(timeout=60000) as download_info:
            try:
                page.goto(url, wait_until="commit", timeout=30000)
            except Exception as e:
                # "Download is starting" is expected
                if "Download is starting" not in str(e):
                    raise
                logger.debug("Download started...")

        download = download_info.value
        download.save_as(str(dest_path))
        size = dest_path.stat().st_size

        if size > 1000:  # Sanity check
            logger.info(f"Saved {size:,} bytes")
            return True
        else:
            logger.warning(f"Downloaded file too small ({size} bytes)")
            return False

    except Exception as e:
        logger.error(f"Download failed: {e}")
        return False

def register_download(doc, file_path: Path, doc_id: str):
    """Register the downloaded document in the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        with open(file_path, 'rb') as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()

        metadata = {
            "title": doc.get("title", ""),
            "document_id": doc_id,
            "confidence_score": doc.get("quality_confidence", 0),
            "quality_tier": doc.get("quality_tier", ""),
            "docket": doc.get("docket", ""),
            "family": doc.get("family", ""),
            "filing_type": doc.get("filing_type", ""),
        }

        cursor.execute("""
            INSERT INTO ncuc_discovery_records
            (filing_title, filing_date, filing_classification, acquisition_method,
             local_path, content_hash, file_size_bytes, metadata_json,
             created_at, fetched_at, search_confidence_score, doc_quality_tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            metadata["title"],
            doc.get("date_filed", ""),
            "tariff_sheets",
            "playwright",
            str(file_path),
            file_hash,
            file_path.stat().st_size,
            json.dumps(metadata),
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat(),
            doc.get("quality_confidence", 0.5),
            doc.get("quality_tier", "unknown"),
        ))
        conn.commit()
        logger.info(f"Registered in database")
    except Exception as e:
        logger.error(f"Failed to register: {e}")
    finally:
        conn.close()

def get_existing_records(doc_id: str) -> bool:
    """Check if document already registered."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT 1 FROM ncuc_discovery_records WHERE metadata_json LIKE ? LIMIT 1",
            (f'%{doc_id}%',)
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()

def main():
    """Download all 11 enhanced search documents."""
    docs = load_enhanced_docs()
    logger.info(f"Loaded {len(docs)} documents")

    if not docs:
        logger.error("No documents found")
        return

    logger.info("Authenticating with NCUC portal...")
    pw, ctx, page = create_authenticated_context(settings)

    downloaded = []
    errors = []

    try:
        for i, doc in enumerate(docs, 1):
            doc_id = doc.get("doc_id")
            detail_url = doc.get("href")
            if not doc_id or not detail_url:
                logger.warning(f"Missing doc_id or href for: {doc.get('title', 'unknown')}")
                continue

            if get_existing_records(doc_id):
                logger.info(f"[{i}/{len(docs)}] SKIP - Already registered: {doc.get('title', 'unknown')[:60]}")
                continue

            logger.info(f"\n[{i}/{len(docs)}] {doc.get('title', 'unknown')[:60]}")

            # Get ViewFile links from detail page
            view_file_links = find_pdf_links_on_detail_page(page, detail_url)

            if not view_file_links:
                logger.warning("No ViewFile links found on detail page")
                errors.append({"doc": doc, "error": "No ViewFile links found"})
                continue

            # Try each ViewFile link
            success = False
            for view_url in view_file_links:
                family = doc.get("family", "unknown").replace("nc-", "").replace("-", "_")
                dest_dir = Path(f"data/downloads/ncuc_tariff/{family}")

                import re
                clean_title = re.sub(r'[^\w\s\-\.]', '_', doc.get('title', 'document'))[:50].strip()
                dest_path = dest_dir / f"{doc_id}_{clean_title}.pdf"

                if download_from_url(page, view_url, dest_path):
                    if dest_path.stat().st_size > 1000:  # Ensure it's a real PDF
                        register_download(doc, dest_path, doc_id)
                        downloaded.append({"doc": doc, "path": str(dest_path), "size": dest_path.stat().st_size})
                        success = True
                        break

            if not success:
                errors.append({"doc": doc, "error": "Failed to download from all ViewFile links"})

    finally:
        close_authenticated_context(pw, ctx)

    # Summary
    print("\n" + "="*70)
    print("DOWNLOAD SUMMARY")
    print("="*70)
    print(f"Downloaded: {len(downloaded)}")
    print(f"Errors: {len(errors)}")
    print(f"Total: {len(docs)}")

    if downloaded:
        print("\nSuccessfully downloaded:")
        for d in downloaded:
            print(f"  [{d['size']:,} bytes] {d['doc'].get('title', 'unknown')[:70]}")

    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  {e['doc'].get('title', 'unknown')[:60]}: {e['error']}")

if __name__ == "__main__":
    main()
