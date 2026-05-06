#!/usr/bin/env python
"""
Legacy orchestration wrapper for an older multi-phase Docling/HQ ingest flow.

This script still calls `python -m duke_rates ingest-ncuc`, so it is not part
of the current sanctioned page-aware historical workflow.
"""

import subprocess
import sys
import time
import argparse
import sqlite3
from pathlib import Path

def wait_for_docling_completion(log_file: str = "docling_batch_processing.log", check_interval: int = 30):
    """Wait for Phase 1 (Docling) to complete by monitoring the log file."""
    print("\n" + "="*70)
    print("MONITORING PHASE 1: DOCLING BATCH PROCESSING")
    print("="*70)
    print(f"Watching: {log_file}")
    print("Press Ctrl+C to stop monitoring (Phase 1 will continue in background)")
    print()

    log_path = Path(log_file)
    last_pos = 0
    last_line = ""
    check_count = 0

    while True:
        check_count += 1

        if not log_path.exists():
            print(f"[Check {check_count}] Log file not found yet...")
            time.sleep(check_interval)
            continue

        # Read new lines
        with open(log_path, "r") as f:
            f.seek(last_pos)
            new_lines = f.readlines()
            last_pos = f.tell()

        if new_lines:
            last_line = new_lines[-1].strip()
            # Check for progress indicator [N/610]
            if "[" in last_line and "/610]" in last_line:
                import re
                match = re.search(r"\[(\d+)/610\]", last_line)
                if match:
                    current = int(match.group(1))
                    progress = round(100 * current / 610, 1)
                    print(f"[Check {check_count}] Progress: {current}/610 ({progress}%)")

        # Check if complete
        if new_lines and any("complete" in line.lower() for line in new_lines[-5:]):
            print("\n[PHASE 1 COMPLETE] Docling batch processing finished.")
            return True

        if new_lines and any("error" in line.lower() or "fail" in line.lower() for line in new_lines[-5:]):
            print(f"\n[WARNING] Error in log: {last_line}")

        time.sleep(check_interval)

def check_phase1_artifacts(min_expected: int = 600):
    """Verify Phase 1 completed by checking docling_artifacts count."""
    try:
        conn = sqlite3.connect("data/db/duke_rates.db")
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM docling_artifacts")
        count = cursor.fetchone()[0]
        conn.close()

        print(f"Docling artifacts in DB: {count}")
        return count >= min_expected
    except Exception as e:
        print(f"Could not verify artifacts: {e}")
        return False

def run_command(cmd: str, description: str):
    """Run a command and report results."""
    print(f"\n{'='*70}")
    print(f"{description}")
    print(f"{'='*70}")
    print(f"Command: {cmd}\n")

    result = subprocess.run(cmd, shell=True)
    return result.returncode == 0

def run_phase2_extraction():
    """Run Phase 2: Fingerprinting and charge extraction."""
    print("\n" + "="*70)
    print("PHASE 2: CHARGE EXTRACTION FROM DOCLING ARTIFACTS")
    print("="*70)
    print("Pipeline: Fingerprint -> Filter -> Extract -> Validate")

    # Step 0: Fingerprint all artifacts
    success = run_command(
        "python scripts/analysis/fingerprint_docling_artifacts.py",
        "STEP 0: Fingerprint artifacts for quality and redlines"
    )

    if not success:
        print("\nWARNING: Fingerprinting had issues, proceeding with extraction.")

    # Step 1: Extract charges
    success = run_command(
        "python -m duke_rates ingest-ncuc --persist --replace",
        "STEP 1: Extract charges from Docling artifacts"
    )

    if not success:
        print("\nERROR: Charge extraction failed.")
        return False

    # Step 2: Validate
    run_command(
        "python scripts/debug/check_new_charges.py",
        "STEP 2: Validate - Check new charges"
    )

    run_command(
        "python scripts/analysis/validate_enhanced_search.py",
        "STEP 3: Validate - Enhanced search results"
    )

    return True

def run_phase3_summary():
    """Run Phase 3: Validation and summary."""
    print("\n" + "="*70)
    print("PHASE 3: VALIDATION AND SUMMARY")
    print("="*70)

    success = run_command(
        "python scripts/debug/final_charge_summary.py",
        "Final charge summary and gap analysis"
    )

    return success

def main():
    parser = argparse.ArgumentParser(description="Full extraction pipeline automation")
    parser.add_argument(
        "--phase2-only",
        action="store_true",
        help="Skip Phase 1 monitoring, go straight to Phase 2"
    )
    parser.add_argument(
        "--skip-monitor",
        action="store_true",
        help="Skip Phase 1 monitoring, assume it's already running"
    )
    args = parser.parse_args()

    start_time = time.time()

    if not args.phase2_only and not args.skip_monitor:
        # Monitor Phase 1
        try:
            wait_for_docling_completion()
        except KeyboardInterrupt:
            print("\n[Monitoring stopped] Phase 1 continues in background.")
            print("Run this script again with --phase2-only when Phase 1 completes.")
            return

        # Verify completion
        if not check_phase1_artifacts():
            print("\nWARNING: Phase 1 may not have completed successfully.")
            response = input("Continue to Phase 2 anyway? (y/n): ")
            if response.lower() != "y":
                return

    # Run Phase 2
    try:
        if not run_phase2_extraction():
            print("\nPhase 2 failed. Stopping.")
            sys.exit(1)
    except UnicodeEncodeError:
        # Windows CP1252 encoding issue - try with ASCII output
        print("\n" + "="*70)
        print("PHASE 2: CHARGE EXTRACTION FROM DOCLING ARTIFACTS")
        print("="*70)
        print("Pipeline: Fingerprint -> Filter -> Extract -> Validate")

        success = run_command(
            "python scripts/analysis/fingerprint_docling_artifacts.py",
            "STEP 0: Fingerprint artifacts for quality and redlines"
        )

        success = run_command(
            "python -m duke_rates ingest-ncuc --persist --replace",
            "STEP 1: Extract charges from HQ documents"
        )

        if not success:
            print("\nERROR: Charge extraction failed.")
            sys.exit(1)

        run_command(
            "python scripts/debug/check_new_charges.py",
            "STEP 2: Validate - Check new charges"
        )

        run_command(
            "python scripts/analysis/validate_enhanced_search.py",
            "STEP 3: Validate - Enhanced search results"
        )

    # Run Phase 3
    if not run_phase3_summary():
        print("\nPhase 3 had issues, but extraction may have succeeded.")

    elapsed = time.time() - start_time
    hours = int(elapsed / 3600)
    minutes = int((elapsed % 3600) / 60)
    seconds = int(elapsed % 60)

    print(f"\n{'='*70}")
    print(f"FULL PIPELINE COMPLETE")
    print(f"{'='*70}")
    print(f"Total time: {hours}h {minutes}m {seconds}s")
    print(f"\nNext steps:")
    print(f"  1. Review extracted charges and quality fingerprint results")
    print(f"  2. Decide on Tier 2 (historical gaps) based on coverage")
    print(f"  3. Update gap_analysis_dep_nc.md and gap_analysis_dec_nc.md")

if __name__ == "__main__":
    main()
