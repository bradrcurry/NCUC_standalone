"""Repair provenance + timeline gaps from the June-2026 DEP/DEC web-update ingest.

Root cause: DocumentDownloader uses a deterministic archive path keyed on
title+URL, so the June downloads overwrote the prior-revision PDFs that shared
the same path. The original content survived on disk under alternate filenames
(the '-pdf-' doubled-suffix captures), so no data was lost.

This script:
  1. Repoints the two clobbered DB references to the surviving original files:
       - documents.id=40        (DEC Sixty-Sixth, Jan 2026)  -> DEC_JAN file
       - historical_documents.id=2591 (DEP Ninth, Apr 2026)  -> DEP_APR file
  2. Backfills tariff_charges for v5081 (DEC Sixty-Sixth) from the REAL January
     file (an earlier backfill accidentally read the overwritten June content).
  3. Adds the DEP Ninth Revised (eff 2026-04-01) rider_summary_block so the
     Streamlit DEP timeline shows the April->June step (it previously jumped
     Jan->June).

Run with --commit to write; default prints planned actions.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from duke_rates.db.ncuc_loader import persist_rider_summary_records
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.parse.nc_progress import extract_pdf_text
from duke_rates.parse.rider_summary import parse_rider_summary

DB_PATH = ROOT / "data" / "db" / "duke_rates.db"

DEC_JAN_HASH = "a559a1045b22ad409f22f5f393f36f3adcda926d205bcc9e30edb37317b79b4f"
DEP_APR_HASH = "1081b1797915e179ae80478e8376de6896e2649a7d64f81259362e81eb609caa"
DEC_JAN = ROOT / "data/raw/nc/carolinas/rider/ncridersummary-pdf-media-pdfs-for-your-home-rates-electric-nc-ncridersummary-pdf.pdf"
DEP_APR = ROOT / "data/raw/nc/progress/rider/leaf-no-600-summary-of-riders-pdf-media-pdfs-for-your-home-rates-dep-nc-leaf-no-.pdf"

V5081 = 5081  # DEC Sixty-Sixth tariff_version (nc-carolinas-rider-SUMMARY, 2026-01-01)


def _verify_hashes() -> None:
    for path, want in ((DEC_JAN, DEC_JAN_HASH), (DEP_APR, DEP_APR_HASH)):
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != want:
            raise SystemExit(f"Hash mismatch for {path.name}: got {got[:12]}, want {want[:12]}")
    print("Verified both surviving original files match their recorded hashes.")


def run(commit: bool) -> None:
    _verify_hashes()
    tag = "COMMIT" if commit else "DRY-RUN"
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    # 1. Repoint clobbered references --------------------------------------
    print(f"\n[1] Repoint clobbered file references ({tag})")
    rel_dec = str(DEC_JAN.relative_to(ROOT))
    rel_dep = str(DEP_APR.relative_to(ROOT))
    if commit:
        conn.execute("UPDATE documents SET local_path=? WHERE id=40", (rel_dec,))
        conn.execute(
            "UPDATE historical_documents SET local_path=?, raw_text_path=NULL WHERE id=2591",
            (rel_dep,),
        )
    print(f"  documents.id=40 local_path -> {rel_dec}")
    print(f"  historical_documents.id=2591 local_path -> {rel_dep}")

    # 2. Backfill v5081 charges from the REAL January file -----------------
    print(f"\n[2] Backfill v5081 (DEC Sixty-Sixth, Jan 2026) from real January file ({tag})")
    if commit:
        ext = BulkExtractor(str(DB_PATH))
        doc = {
            "id": 40, "family_key": "nc-carolinas-rider-SUMMARY",
            "title": "Summary of Rider Adjustments", "company": "carolinas", "state": "NC",
            "local_path": str(DEC_JAN), "leaf_no": "99",
            "start_page": None, "end_page": None, "effective_start": "2026-01-01",
            "content_hash": "", "docket_number": None, "version_id": V5081,
        }
        _d, _f, inserted, status, profile = ext.process_document(doc, force_clear=True)
        print(f"  v5081 charges={inserted} status={status} profile={profile}")
    else:
        print("  (dry-run) would extract ~52 charges (residential total 0.4951 c/kWh)")

    # 3. Add DEP Ninth Revised (Apr 2026) rider_summary_block --------------
    print(f"\n[3] Add DEP April Ninth rider_summary_block (eff 2026-04-01) ({tag})")
    result = parse_rider_summary(extract_pdf_text(DEP_APR), source_pdf=str(DEP_APR), leaf_no="600")
    res = next((b for b in result.rate_classes if b.rate_class == "Residential Service Schedules"), None)
    print(f"  parsed rate_classes={len(result.rate_classes)} residential_total={res.total_cents_per_kwh if res else None}")
    if commit:
        record = asdict(result)
        record["source_pdf"] = str(DEP_APR)
        record["effective_date"] = "2026-04-01"
        record["leaf_no"] = "600"
        ins, skip = persist_rider_summary_records(conn, [record], replace=True, utility="DEP")
        print(f"  rider_summary_blocks: {ins} inserted, {skip} skipped")
    else:
        print("  (dry-run) would persist 7 DEP blocks at 2026-04-01")

    conn.close()
    print(f"\n{tag} complete.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    run(ap.parse_args().commit)


if __name__ == "__main__":
    main()
