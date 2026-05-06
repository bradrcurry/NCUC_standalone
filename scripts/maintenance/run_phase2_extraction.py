#!/usr/bin/env python
"""
Legacy phase-2 orchestration for the older Docling/HQ ingest flow.

This script still calls `python -m duke_rates ingest-ncuc`, so it is not part
of the current sanctioned page-aware historical workflow.
"""

import subprocess
import sys
import time

def run_command(cmd, description):
    """Run a command and report results."""
    print(f"\n{'='*70}")
    print(f"{description}")
    print(f"{'='*70}")
    print(f"Command: {cmd}\n")

    result = subprocess.run(cmd, shell=True)
    return result.returncode == 0

def main():
    start_time = time.time()

    print("\n" + "="*70)
    print("PHASE 2: CHARGE EXTRACTION FROM DOCLING ARTIFACTS")
    print("="*70)
    print("Pipeline: Fingerprint → Filter → Extract → Validate")

    # Step 0: Fingerprint all artifacts (quality + redline detection)
    print(f"\n{'='*70}")
    print(f"STEP 0: Fingerprint Docling artifacts for quality")
    print(f"{'='*70}\n")

    success = run_command(
        "python scripts/analysis/fingerprint_docling_artifacts.py",
        "Running quality fingerprinting on all Docling artifacts"
    )

    if not success:
        print("\nWARNING: Fingerprinting had issues, but proceeding with extraction.")

    # Step 1: Extract charges from HQ documents
    success = run_command(
        "python -m duke_rates ingest-ncuc --persist --replace",
        "STEP 1: Extract charges from HQ documents"
    )

    if not success:
        print("\nERROR: Charge extraction failed.")
        sys.exit(1)

    # Step 2: Validate results
    run_command(
        "python scripts/debug/check_new_charges.py",
        "STEP 2: Validate extraction - Check new charges"
    )

    run_command(
        "python scripts/analysis/validate_enhanced_search.py",
        "STEP 3: Validate extraction - Enhanced search results"
    )

    # Step 3: Final summary
    run_command(
        "python scripts/debug/final_charge_summary.py",
        "STEP 4: Final charge summary"
    )

    elapsed = time.time() - start_time
    minutes = int(elapsed / 60)
    seconds = int(elapsed % 60)

    print(f"\n{'='*70}")
    print(f"PHASE 2 COMPLETE")
    print(f"{'='*70}")
    print(f"Total time: {minutes}m {seconds}s")
    print(f"\nNext steps:")
    print(f"  1. Review charge extraction results above")
    print(f"  2. Analyze quality of extracted charges")
    print(f"  3. Decide on Tier 2 (fill historical gaps) based on coverage")
    print(f"  4. Update docs/gap_analysis_dep_nc.md with new numbers")
    print(f"  5. Update docs/gap_analysis_dec_nc.md with new numbers")

if __name__ == "__main__":
    main()
