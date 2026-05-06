"""
Three-part ingestion script:

1. Parse the "Riders" section from every DEP rate schedule PDF and populate
   rider_applicability with version-dated links.

2. Scan all DEP historical_documents for redline signals and update
   document_fingerprints.is_redline_candidate / redline_confidence.

3. Re-extract leaf-601 (Rider BA) using an improved parser that handles
   its multi-column table + footnote prose structure.

Run:
    python scripts/ingestion/populate_rider_applicability.py
"""
import sys
import sqlite3
import fitz
import re
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from duke_rates.parse.schedule_riders import parse_schedule_riders
from duke_rates.parse.redline_detector import detect_redline

DB_PATH = str(ROOT / "data" / "db" / "duke_rates.db")
NOW = datetime.now(timezone.utc).isoformat()


# ===========================================================================
# Part 1 — Rider applicability from schedule PDFs
# ===========================================================================

def part1_populate_rider_applicability():
    print("=" * 60)
    print("PART 1: Parsing rider sections from rate schedule PDFs")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)

    # Get all DEP rate schedule versions with local PDFs
    # Use most recent version per family_key
    schedule_rows = conn.execute("""
        SELECT hd.family_key, hd.leaf_no, hd.local_path,
               hd.effective_start, tv.id as version_id, hd.id as hd_id
        FROM historical_documents hd
        JOIN tariff_versions tv ON tv.historical_document_id = hd.id
        WHERE hd.family_key LIKE 'nc-progress-leaf-%'
          AND hd.category = 'rate'
          AND hd.local_path IS NOT NULL
          AND hd.leaf_no IS NOT NULL
        ORDER BY hd.family_key, hd.effective_start DESC
    """).fetchall()

    # Group by family_key — process all versions per schedule
    from collections import defaultdict
    by_family: dict[str, list] = defaultdict(list)
    for row in schedule_rows:
        by_family[row[0]].append(row)

    inserted = 0
    skipped = 0
    errors = 0

    for fk, versions in by_family.items():
        for fk, leaf_no, local_path, eff_start, version_id, hd_id in versions:
            if not local_path or not Path(local_path).exists():
                continue

            try:
                doc = fitz.open(local_path)
                text = ""
                for pg in range(min(5, len(doc))):
                    text += doc[pg].get_text("text")
                doc.close()
            except Exception as e:
                print(f"  ERROR reading {local_path}: {e}")
                errors += 1
                continue

            result = parse_schedule_riders(
                text,
                schedule_family_key=fk,
                schedule_leaf_no=leaf_no,
                effective_start=eff_start,
                utility_prefix="nc-progress",
            )

            if not result.riders:
                continue

            for rider in result.riders:
                # Check if already exists
                existing = conn.execute("""
                    SELECT id FROM rider_applicability
                    WHERE rider_family_key = ?
                      AND applies_to_family_key = ?
                      AND (effective_start = ? OR (effective_start IS NULL AND ? IS NULL))
                """, (
                    rider.rider_family_key, fk,
                    eff_start, eff_start,
                )).fetchone()

                if existing:
                    skipped += 1
                    continue

                conn.execute("""
                    INSERT INTO rider_applicability
                        (rider_family_key, applies_to_family_key, mandatory,
                         applicability_notes, effective_start, effective_end,
                         source_type, confidence_score, created_at,
                         enrollment_type, in_rider_summary)
                    VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """, (
                    rider.rider_family_key,
                    fk,
                    1 if rider.mandatory else 0,
                    f"Parsed from {fk} schedule text (section: {rider.source_section}). "
                    f"Rider {rider.rider_code} Leaf {rider.rider_leaf_no}."
                    + (f" Note: {rider.asterisk_note}." if rider.asterisk_note else ""),
                    eff_start,
                    "schedule_text",
                    0.92,
                    NOW,
                    "mandatory",
                    1,
                ))
                inserted += 1

        if inserted % 50 == 0 and inserted > 0:
            conn.commit()

    conn.commit()

    # Summary
    dep_count = conn.execute("""
        SELECT COUNT(*) FROM rider_applicability
        WHERE applies_to_family_key LIKE 'nc-progress-leaf-%'
    """).fetchone()[0]

    print(f"\nInserted: {inserted}  Skipped (existing): {skipped}  Errors: {errors}")
    print(f"Total DEP rider_applicability entries now: {dep_count}")

    # Show coverage
    print("\nRider coverage per schedule family:")
    coverage = conn.execute("""
        SELECT applies_to_family_key,
               COUNT(DISTINCT rider_family_key) as rider_count,
               GROUP_CONCAT(rider_family_key, ', ') as riders
        FROM rider_applicability
        WHERE applies_to_family_key LIKE 'nc-progress-leaf-%'
        GROUP BY applies_to_family_key
        ORDER BY applies_to_family_key
    """).fetchall()
    for row in coverage:
        leaf_riders = row[2].replace('nc-progress-leaf-', 'L')
        print(f"  {row[0]:35} {row[1]:2} riders: {leaf_riders[:80]}")

    conn.close()


# ===========================================================================
# Part 2 — Redline detection scan
# ===========================================================================

def part2_redline_scan():
    print()
    print("=" * 60)
    print("PART 2: Scanning DEP documents for redline signals")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)

    # Get all DEP historical_documents with local PDFs
    rows = conn.execute("""
        SELECT DISTINCT hd.id, hd.family_key, hd.local_path
        FROM historical_documents hd
        WHERE hd.family_key LIKE 'nc-progress-leaf-%'
          AND hd.local_path IS NOT NULL
        ORDER BY hd.family_key
    """).fetchall()

    redlines_found = []
    index_reds = []
    clean = 0
    errors = 0

    for hd_id, fk, local_path in rows:
        if not Path(local_path).exists():
            continue
        try:
            sig = detect_redline(local_path, max_pages=3)

            # Update document_fingerprints if record exists
            existing_fp = conn.execute(
                "SELECT id FROM document_fingerprints WHERE source_pdf = ?",
                (local_path,)
            ).fetchone()

            if existing_fp:
                conn.execute("""
                    UPDATE document_fingerprints
                    SET is_redline_candidate = ?,
                        redline_confidence = ?
                    WHERE source_pdf = ?
                """, (
                    1 if sig.is_redline else 0,
                    sig.confidence,
                    local_path,
                ))

            if sig.is_redline:
                redlines_found.append((fk, local_path, sig.confidence, sig.signals))
            elif sig.red_is_index_only:
                index_reds.append(fk)
            else:
                clean += 1

        except Exception as e:
            errors += 1

    conn.commit()
    conn.close()

    print(f"\nDocuments scanned: {len(rows)}")
    print(f"Redline candidates: {len(redlines_found)}")
    print(f"Index-red only (not redlines): {len(index_reds)}")
    print(f"Clean documents: {clean}")
    print(f"Errors: {errors}")

    if redlines_found:
        print("\nRedline candidates:")
        for fk, path, conf, signals in redlines_found:
            print(f"  conf={conf:.2f}  {fk}")
            print(f"    signals: {signals}")
            print(f"    path: {path}")

    print("\nExplanation of redline detection methods:")
    print("  1. Red text in body (RGB r>150, g<100, b<100) — strongest signal")
    print("  2. Strikethrough flag (span flags & 8) — direct PDF encoding")
    print("  3. PDF annotations (StrikeOut, Highlight) — tracked changes")
    print("  4. Thin horizontal line drawings over text — manual strikethroughs")
    print("  5. Filename contains 'redline' or 'markup'")
    print("  Index red (#c00000 in TOC entries) is NOT counted as redline signal.")


# ===========================================================================
# Part 3 — leaf-601 improved extraction
# ===========================================================================

def part3_reextract_leaf601():
    print()
    print("=" * 60)
    print("PART 3: Re-extracting leaf-601 (Rider BA) with improved parser")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)

    versions = conn.execute("""
        SELECT hd.id, hd.family_key, hd.local_path, hd.effective_start,
               tv.id as version_id, hd.title
        FROM historical_documents hd
        JOIN tariff_versions tv ON tv.historical_document_id = hd.id
        WHERE hd.family_key = 'nc-progress-leaf-601'
          AND hd.local_path IS NOT NULL
        ORDER BY hd.effective_start
    """).fetchall()
    conn.close()

    print(f"Found {len(versions)} leaf-601 versions")

    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
    extractor = BulkExtractor(DB_PATH)

    total = 0
    for hd_id, fk, local_path, eff, version_id, title in versions:
        print(f"  [hd={hd_id}] eff={eff}... ", end="", flush=True)
        doc = {
            "id": hd_id,
            "family_key": fk,
            "local_path": local_path,
            "effective_start": eff,
            "version_id": version_id,
            "title": title or "",
            "company": "DEP",
            "state": "NC",
            "content_hash": None,
            "revision_label": None,
            "supersedes_label": None,
            "leaf_no": "601",
            "start_page": None,
            "end_page": None,
            "discovery_record_id": None,
            "docket_number": None,
            "acquisition_method": "manual_registration",
            "discovery_doc_quality_tier": "T1",
        }
        try:
            _, _, n = extractor.process_document(doc)
            total += n
            print(f"{n} charges")
        except Exception as e:
            print(f"ERROR: {e}")

    print(f"\nTotal leaf-601 charges inserted: {total}")
    print()
    print("leaf-601 structure notes:")
    print("  - Multi-column table: Rate Class | Fuel Rate | EMF | DSM/EE Rate | EMF | Net Adj")
    print("  - Values include parenthesized negatives (0.123)")
    print("  - Footnotes 1-6 explain each column — stored as rider_descriptions context")
    print("  - Also contains CEPS per-customer $/month charges by revenue class")
    print("  - Opt-out provisions (commercial >1M kWh) — not extractable as rates")


# ===========================================================================
# Main
# ===========================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Populate rider applicability, scan redlines, re-extract leaf-601")
    parser.add_argument("--part", choices=["1", "2", "3", "all"], default="all",
                        help="Which part to run (default: all)")
    args = parser.parse_args()

    if args.part in ("1", "all"):
        part1_populate_rider_applicability()

    if args.part in ("2", "all"):
        part2_redline_scan()

    if args.part in ("3", "all"):
        part3_reextract_leaf601()

    print()
    print("Done.")
