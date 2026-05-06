"""
Three-part ingestion script:

1. Extract docket cross-references from all redline-candidate PDFs and print
   a discovery map — which dockets appear in redlines, which we haven't yet
   downloaded from, and what supersession chains exist.

2. Populate rider_applicability for DEC rate schedule families by parsing the
   "Riders" section from each nc-carolinas-schedule-* PDF.

3. Run redline detection scan across all DEC historical_documents and update
   document_fingerprints.is_redline_candidate / redline_confidence.

Run:
    python scripts/ingestion/populate_redline_crossrefs_and_dec_riders.py
    python scripts/ingestion/populate_redline_crossrefs_and_dec_riders.py --part 1
    python scripts/ingestion/populate_redline_crossrefs_and_dec_riders.py --part 2
    python scripts/ingestion/populate_redline_crossrefs_and_dec_riders.py --part 3
"""
import sys
import sqlite3
import fitz
import re
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from duke_rates.parse.schedule_riders import parse_schedule_riders
from duke_rates.parse.redline_detector import detect_redline
from duke_rates.parse.redline_crossref import scan_redlines_for_crossrefs, extract_crossref

DB_PATH = str(ROOT / "data" / "db" / "duke_rates.db")
NOW = datetime.now(timezone.utc).isoformat()


# ===========================================================================
# Part 1 — Docket cross-references from redline PDFs
# ===========================================================================

def part1_redline_crossrefs():
    print("=" * 60)
    print("PART 1: Extracting docket cross-references from redline PDFs")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)

    # Get all known dockets already in our DB
    known_dockets: set[str] = set()
    try:
        rows = conn.execute("SELECT DISTINCT docket_number FROM historical_documents WHERE docket_number IS NOT NULL").fetchall()
        for (d,) in rows:
            if d:
                known_dockets.add(d.strip().upper())
    except Exception:
        pass

    # Also check folder names — e-2-sub-931 etc
    known_sub_dirs: set[str] = set()
    for p in (ROOT / "data" / "historical" / "ncuc").iterdir():
        if p.is_dir():
            known_sub_dirs.add(p.name.lower())

    conn.close()

    # Scan all redline candidates (both DEP and DEC)
    print("\nScanning redline candidates for docket cross-references...")
    refs = scan_redlines_for_crossrefs(DB_PATH, family_key_pattern="%", max_pages=3)

    print(f"Unique redline PDFs scanned: {len(refs)}")

    # Aggregate all docket numbers found
    all_dockets: dict[str, list[str]] = defaultdict(list)   # docket -> [source_pdfs]
    supersession_chains: list[dict] = []

    for ref in refs:
        for d in ref["docket_numbers"]:
            all_dockets[d].append(ref["source_pdf"])
        if ref["supersedes_leaf_nos"] or ref["old_effective_date"]:
            supersession_chains.append(ref)

    print(f"\nTotal unique docket numbers found across redlines: {len(all_dockets)}")

    # Identify dockets we don't have in our local corpus
    print("\n--- Dockets found in redlines ---")
    novel_dockets = []
    for docket in sorted(all_dockets.keys()):
        # Normalize to folder-name format: "E-2, Sub 931" → "e-2-sub-931"
        folder_guess = docket.lower().replace(", sub ", "-sub-").replace(",", "").replace(" ", "-")
        have_it = folder_guess in known_sub_dirs
        status = "HAVE" if have_it else "MISSING"
        n_pdfs = len(set(all_dockets[docket]))
        print(f"  [{status}]  {docket:25}  found in {n_pdfs} redline PDF(s)")
        if not have_it:
            novel_dockets.append((docket, folder_guess))

    if novel_dockets:
        print(f"\n*** {len(novel_dockets)} dockets referenced in redlines but NOT in local corpus: ***")
        for docket, folder in novel_dockets:
            print(f"  {docket:25}  → expected folder: e-2-sub-*/{folder.split('-sub-')[-1] if '-sub-' in folder else folder}")

    # Supersession chains
    if supersession_chains:
        print(f"\n--- Supersession chains ({len(supersession_chains)} redlines with before/after dates) ---")
        for ref in supersession_chains:
            path_short = Path(ref["source_pdf"]).name
            print(f"  {path_short}")
            if ref["leaf_nos"]:
                print(f"    Leaf(s):        {ref['leaf_nos']}")
            if ref["supersedes_leaf_nos"]:
                print(f"    Supersedes:     {ref['supersedes_leaf_nos']}")
            if ref["old_effective_date"]:
                print(f"    Old eff date:   {ref['old_effective_date']}")
            if ref["new_effective_date"]:
                print(f"    New eff date:   {ref['new_effective_date']}")
            if ref["utility"]:
                print(f"    Utility:        {ref['utility']}")


# ===========================================================================
# Part 2 — DEC rider applicability
# ===========================================================================

def part2_dec_rider_applicability():
    print()
    print("=" * 60)
    print("PART 2: Populating rider_applicability for DEC rate schedules")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)

    # Get all DEC rate schedule versions with local PDFs
    schedule_rows = conn.execute("""
        SELECT hd.family_key, hd.local_path,
               hd.effective_start, tv.id as version_id, hd.id as hd_id
        FROM historical_documents hd
        JOIN tariff_versions tv ON tv.historical_document_id = hd.id
        WHERE hd.family_key LIKE 'nc-carolinas-schedule-%'
          AND hd.category = 'rate'
          AND hd.local_path IS NOT NULL
        ORDER BY hd.family_key, hd.effective_start DESC
    """).fetchall()

    # Group by family_key — process most recent version per schedule
    by_family: dict[str, list] = defaultdict(list)
    for row in schedule_rows:
        by_family[row[0]].append(row)

    print(f"Found {len(by_family)} DEC rate schedule families")

    inserted = 0
    skipped = 0
    errors = 0
    schedule_results = []

    for fk, versions in by_family.items():
        # Use most recent version for rider detection (current schedule)
        fk, local_path, eff_start, version_id, hd_id = versions[0]

        if not local_path or not Path(local_path).exists():
            errors += 1
            continue

        try:
            doc = fitz.open(local_path)
            text = ""
            # Scan more pages since DEC bundles are large (146+ pages)
            # Riders section is typically within first 10% of doc
            scan_pages = min(20, len(doc))
            for pg in range(scan_pages):
                page_text = doc[pg].get_text("text")
                text += page_text
                # Stop scanning once we've passed the riders section
                if "following Riders are applicable" in text and len(text) > 2000:
                    # Get one more page for context
                    if pg + 1 < len(doc):
                        text += doc[pg + 1].get_text("text")
                    break
            doc.close()
        except Exception as e:
            print(f"  ERROR reading {local_path}: {e}")
            errors += 1
            continue

        result = parse_schedule_riders(
            text,
            schedule_family_key=fk,
            schedule_leaf_no=None,
            effective_start=eff_start,
            utility_prefix="nc-carolinas",
        )

        if not result.riders:
            schedule_results.append((fk, 0, "no_riders_found"))
            continue

        schedule_inserted = 0
        for rider in result.riders:
            # Skip unknown placeholder family keys
            if rider.rider_family_key.startswith("nc-carolinas-leaf-"):
                continue

            # Insert for ALL versions of this schedule (not just most recent)
            for fk2, lp2, eff2, vid2, hd2 in versions:
                existing = conn.execute("""
                    SELECT id FROM rider_applicability
                    WHERE rider_family_key = ?
                      AND applies_to_family_key = ?
                      AND (effective_start = ? OR (effective_start IS NULL AND ? IS NULL))
                """, (rider.rider_family_key, fk2, eff2, eff2)).fetchone()

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
                    fk2,
                    1 if rider.mandatory else 0,
                    (f"Parsed from {fk} schedule text (section: {rider.source_section}). "
                     f"DEC Leaf No. {rider.rider_leaf_no} ({rider.rider_code})."),
                    eff2,
                    "schedule_text",
                    0.88,
                    NOW,
                    "mandatory",
                    1,
                ))
                inserted += 1
                schedule_inserted += 1

        schedule_results.append((fk, len(result.riders), f"{schedule_inserted} rows inserted"))
        if inserted % 100 == 0 and inserted > 0:
            conn.commit()

    conn.commit()

    # Summary
    dec_count = conn.execute("""
        SELECT COUNT(*) FROM rider_applicability
        WHERE applies_to_family_key LIKE 'nc-carolinas-%'
    """).fetchone()[0]

    print(f"\nInserted: {inserted}  Skipped (existing): {skipped}  Errors: {errors}")
    print(f"Total DEC rider_applicability entries now: {dec_count}")

    print("\nRider detection per DEC schedule:")
    for fk, n_riders, note in sorted(schedule_results):
        sch = fk.replace("nc-carolinas-schedule-", "SCH-")
        print(f"  {sch:25}  {n_riders:2} riders  {note}")

    # Show unique rider families now covered
    dec_rider_families = conn.execute("""
        SELECT DISTINCT rider_family_key
        FROM rider_applicability
        WHERE applies_to_family_key LIKE 'nc-carolinas-%'
        ORDER BY rider_family_key
    """).fetchall()
    print(f"\nUnique DEC rider families linked: {len(dec_rider_families)}")
    for (rfk,) in dec_rider_families:
        print(f"  {rfk}")

    conn.close()


# ===========================================================================
# Part 3 — Redline detection scan for DEC documents
# ===========================================================================

def part3_dec_redline_scan():
    print()
    print("=" * 60)
    print("PART 3: Scanning DEC documents for redline signals")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)

    rows = conn.execute("""
        SELECT DISTINCT hd.id, hd.family_key, hd.local_path
        FROM historical_documents hd
        WHERE hd.family_key LIKE 'nc-carolinas-%'
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

    print(f"\nDEC documents scanned: {len(rows)}")
    print(f"Redline candidates: {len(redlines_found)}")
    print(f"Index-red only (not redlines): {len(index_reds)}")
    print(f"Clean documents: {clean}")
    print(f"Errors: {errors}")

    if redlines_found:
        print("\nDEC Redline candidates:")
        for fk, path, conf, signals in redlines_found:
            print(f"  conf={conf:.2f}  {fk}")
            print(f"    signals: {signals}")
            print(f"    path: ...{path[-70:]}")

    # Combined summary: all redlines now flagged
    conn2 = sqlite3.connect(DB_PATH)
    total_red = conn2.execute(
        "SELECT COUNT(*) FROM document_fingerprints WHERE is_redline_candidate = 1"
    ).fetchone()[0]
    total_fp = conn2.execute("SELECT COUNT(*) FROM document_fingerprints").fetchone()[0]
    conn2.close()
    print(f"\nAll redline candidates (DEP+DEC): {total_red}/{total_fp} documents with fingerprints")


# ===========================================================================
# Main
# ===========================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Redline cross-refs, DEC rider applicability, DEC redline scan"
    )
    parser.add_argument("--part", choices=["1", "2", "3", "all"], default="all",
                        help="Which part to run (default: all)")
    args = parser.parse_args()

    if args.part in ("1", "all"):
        part1_redline_crossrefs()

    if args.part in ("2", "all"):
        part2_dec_rider_applicability()

    if args.part in ("3", "all"):
        part3_dec_redline_scan()

    print()
    print("Done.")
