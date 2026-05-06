#!/usr/bin/env python
"""
Register Already-Downloaded Documents for Extraction

This script links the 11 high-quality enhanced search documents
to their already-downloaded PDF files in data/downloads/ncuc_tariff/

This allows extraction to proceed without waiting for portal automation.

Usage: python register_downloaded_documents.py
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime

def main():
    print("=" * 70)
    print("REGISTERING DOWNLOADED DOCUMENTS FOR EXTRACTION")
    print("=" * 70)

    # Load enhanced search results
    with open('data/dep_gap_search_enhanced.json', 'r') as f:
        enhanced_docs = json.load(f)

    # Map document IDs to families
    family_map = {
        'nc-progress-leaf-602': 'progress_leaf_602',  # JAA
        'nc-progress-leaf-607': 'progress_leaf_607',  # STS
        'nc-progress-leaf-608': 'progress_leaf_608',  # RDM
        'nc-progress-leaf-606': 'progress_leaf_606',  # DSM
        'nc-progress-leaf-609': 'progress_leaf_609',  # RES
        'nc-progress-leaf-610': 'progress_leaf_610',  # PPM
    }

    # Check what PDFs are available
    downloads_base = Path('data/downloads/ncuc_tariff')
    available_pdfs = {}

    for family_key, folder_name in family_map.items():
        family_path = downloads_base / folder_name
        if family_path.exists():
            pdfs = list(family_path.glob('*.pdf'))
            available_pdfs[family_key] = pdfs
            print(f"\n{folder_name}: Found {len(pdfs)} PDF(s)")

    # Open database
    db = sqlite3.connect('data/duke_rates.db')
    c = db.cursor()

    # Update each registered document with local_path
    updated = 0
    matched = 0

    print("\n" + "=" * 70)
    print("MATCHING DOCUMENTS TO PDFs")
    print("=" * 70)

    for doc in enhanced_docs:
        family = doc['family']
        title = doc['title']
        date = doc['date_filed']

        print(f"\n{doc['name']} - {family}")
        print(f"  Title: {title[:50]}...")
        print(f"  Date: {date}")

        # Check if we have PDFs for this family
        if family in available_pdfs:
            pdfs = available_pdfs[family]
            print(f"  Available PDFs: {len(pdfs)}")

            # Try to match by date or title keywords
            best_match = None
            for pdf in pdfs:
                pdf_name = pdf.name.lower()

                # Look for year match
                if date and date[-4:] in pdf_name:
                    best_match = pdf
                    print(f"  -> Matched by date: {pdf.name[:60]}...")
                    break

                # Look for title keywords
                title_keywords = [w for w in title.lower().split() if len(w) > 4]
                if any(kw in pdf_name for kw in title_keywords[:2]):
                    best_match = pdf
                    print(f"  -> Matched by keyword: {pdf.name[:60]}...")
                    break

            if best_match:
                # Update database
                local_path = str(best_match)
                c.execute('''
                    UPDATE ncuc_discovery_records
                    SET local_path = ?, fetched_at = ?
                    WHERE filing_title = ? AND filing_date = ?
                ''', (
                    local_path,
                    datetime.utcnow().isoformat() + 'Z',
                    title,
                    date
                ))
                updated += 1
                matched += 1
            else:
                print(f"  -> No PDF match found (may need manual match)")
        else:
            print(f"  -> No PDFs available for {family}")

    db.commit()

    # Verify updates
    c.execute('SELECT COUNT(*) FROM ncuc_discovery_records WHERE local_path IS NOT NULL')
    with_paths = c.fetchone()[0]

    print("\n" + "=" * 70)
    print("REGISTRATION RESULTS")
    print("=" * 70)
    print(f"\nDocuments with local_path: {with_paths}")
    print(f"Newly matched: {matched}")

    # Show status
    c.execute('SELECT filing_title, local_path FROM ncuc_discovery_records WHERE local_path IS NOT NULL')
    results = c.fetchall()
    print(f"\nRegistered documents ready for extraction:")
    for title, path in results:
        exists = "✓" if Path(path).exists() else "✗"
        print(f"  {exists} {title[:50]}...")
        print(f"     {path}")

    db.close()

    print("\n" + "=" * 70)
    print("NEXT STEP")
    print("=" * 70)
    print("""
To extract charges from these documents, run:

  python -m duke_rates extract-rates-nc --limit 20

This will:
  1. Read the 11 documents from their local_path locations
  2. Extract rate charges from each document
  3. Populate tariff_versions and tariff_charges tables
  4. Generate extraction results

Then validate with:
  python analyze_dep_gap_impact.py
""")

if __name__ == '__main__':
    main()
