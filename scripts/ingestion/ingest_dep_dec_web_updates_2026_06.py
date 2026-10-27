"""Ingest the June-2026 DEP/DEC tariff-sheet updates published on duke-energy.com.

Identified via a content-hash sweep of all 151 NC DEP+DEC current sheets
(reports/_duke_web_hash_sweep.json). Of 7 byte-changed sheets:

  NEW VERSIONS (register historical_document + tariff_version + extract charges):
    - nc-progress-leaf-600       Summary of Riders  -> NC Tenth Revised, eff 2026-06-01 (~132 charges)
    - nc-carolinas-rider-SUMMARY Summary of Riders  -> NC Sixty-Seventh Revised Leaf 99, eff 2026-06-01 (~52)

  TERMINATIONS (register termination version w/ 0 charges + close prior active version):
    - nc-progress-leaf-604       Rider EDIT-4 -> NC First Revised Leaf 604, terminated 2026-06-01
    - nc-carolinas-rider-EDIT4   Rider EDIT-4 -> NC Third Revised Leaf 131, terminated 2026-06-01

  ALREADY CURRENT (rates already in DB at 2026-04-01; only refresh stale documents pointer):
    - nc-progress-leaf-608/609/610  Second Revised RDM/ESM/PIM, eff 2026-04-01

All 7 also get their tariff_families.current_document_id refreshed to the live PDF.

Non-destructive: prior dated versions are preserved. Run with --commit to write;
default is a dry run that prints the planned actions.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.download.downloader import DocumentDownloader
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.tariff import TariffVersionRecord
from duke_rates.parse.nc_progress import extract_pdf_text
from duke_rates.utils.dates import utc_now

DB_PATH = ROOT / "data" / "db" / "duke_rates.db"
EFF = "2026-06-01"
TERM_PRIOR_END = "2026-05-31"

BASE = "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates"

# kind: "new_version" | "termination" | "pointer_only"
SHEETS = [
    {
        "family_key": "nc-progress-leaf-600",
        "company": "progress",
        "leaf_no": "600",
        "title": "Summary of Rider Adjustments",
        "url": f"{BASE}/dep-nc/leaf-no-600-summary-of-riders.pdf",
        "kind": "new_version",
        "effective_start": EFF,
        "revision_label": "NC Tenth Revised Leaf No. 600",
        "supersedes_label": "NC Ninth Revised Leaf No. 600",
        "end_page": 5,
    },
    {
        "family_key": "nc-carolinas-rider-SUMMARY",
        "company": "carolinas",
        "leaf_no": "99",
        "title": "Summary of Rider Adjustments",
        "url": f"{BASE}/electric-nc/ncridersummary.pdf",
        "kind": "new_version",
        "effective_start": EFF,
        "revision_label": "NC Sixty-Seventh Revised Leaf No. 99",
        "supersedes_label": "NC Sixty-Sixth Revised Leaf No. 99",
        "end_page": 3,
    },
    {
        "family_key": "nc-progress-leaf-604",
        "company": "progress",
        "leaf_no": "604",
        "title": "Excess Deferred Income Tax Rider EDIT-4 (Terminated)",
        "url": f"{BASE}/dep-nc/leaf-no-604-rider-edit-4-ry1.pdf",
        "kind": "termination",
        "effective_start": EFF,
        "revision_label": "NC First Revised Leaf No. 604",
        "supersedes_label": "NC Original Leaf No. 604",
        "end_page": 1,
        "close_prior_version_id": 5399,
    },
    {
        "family_key": "nc-carolinas-rider-EDIT4",
        "company": "carolinas",
        "leaf_no": "131",
        "title": "Excess Deferred Income Tax Rider EDIT-4 (Terminated)",
        "url": f"{BASE}/electric-nc/ncrideredit4.pdf",
        "kind": "termination",
        "effective_start": EFF,
        "revision_label": "NC Third Revised Leaf No. 131",
        "supersedes_label": "NC Second Revised Leaf No. 131",
        "end_page": 1,
        "close_prior_version_id": 5061,
        # Stale bootstrap row already sitting at 2026-06-01 with 4 old credits;
        # the authoritative sheet terminates the rider, so clear it and relabel.
        "reconcile_version_id": 5229,
    },
    {
        "family_key": "nc-progress-leaf-608",
        "company": "progress",
        "leaf_no": "608",
        "title": "Residential Decoupling Mechanism Rider RDM",
        "url": f"{BASE}/dep-nc/leaf-no-608-rider-rdm-ry1.pdf",
        "kind": "pointer_only",
    },
    {
        "family_key": "nc-progress-leaf-609",
        "company": "progress",
        "leaf_no": "609",
        "title": "Earnings Sharing Mechanism Rider ESM",
        "url": f"{BASE}/dep-nc/leaf-no-609-rider-esm-ry1.pdf",
        "kind": "pointer_only",
    },
    {
        "family_key": "nc-progress-leaf-610",
        "company": "progress",
        "leaf_no": "610",
        "title": "Performance Incentive Mechanism Rider PIM",
        "url": f"{BASE}/dep-nc/leaf-no-610-rider-pim-ry1.pdf",
        "kind": "pointer_only",
    },
]


def _discovery_record(sheet: dict) -> DiscoveryRecord:
    return DiscoveryRecord(
        title=sheet["title"],
        source_page_url="https://www.duke-energy.com/home/billing/rates/index-of-rate-schedules?jur=NC",
        document_url=sheet["url"],
        state="NC",
        company=sheet["company"],
        category=DocumentCategory.RIDER,
        kind=DocumentKind.PDF,
        effective_date=None,
        retrieval_timestamp=utc_now(),
        notes=["source=duke-energy.com", "ingest=dep_dec_web_updates_2026_06"],
    )


def _existing_version_at(conn: sqlite3.Connection, family_key: str, eff: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM tariff_versions WHERE family_key=? AND effective_start=? ORDER BY id DESC LIMIT 1",
        (family_key, eff),
    ).fetchone()
    return int(row[0]) if row else None


def run(commit: bool) -> None:
    settings = Settings()
    repo = Repository(str(DB_PATH))
    downloader = DocumentDownloader(settings)
    # Autocommit (isolation_level=None) so our direct-SQL writes commit
    # immediately and never hold a lock against the Repository's separate
    # connections (which would otherwise raise "database is locked").
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    now = datetime.now(UTC)
    tag = "COMMIT" if commit else "DRY-RUN"

    try:
        for sheet in SHEETS:
            fk = sheet["family_key"]
            print(f"\n=== {fk} [{sheet['kind']}] ({tag}) ===")

            # --- 1. Download live PDF + refresh documents pointer (all sheets) ---
            record = _discovery_record(sheet)
            downloaded = downloader.download(record)
            local_path = Path(downloaded.local_path)
            print(f"  downloaded -> {local_path.name}  hash={downloaded.content_hash[:16]}")

            if commit:
                document_id = repo.upsert_document(downloaded)
                repo.attach_current_document_to_family(fk, document_id=document_id)
                print(f"  documents row id={document_id}; family pointer refreshed")
            else:
                document_id = None
                print("  (dry-run) would upsert documents row + refresh family pointer")

            if sheet["kind"] == "pointer_only":
                print("  rates already current at 2026-04-01 (verified identical) - no version change")
                continue

            # --- 2. New historical_document for the updated sheet ---
            # Save a REVISION-VERSIONED copy. DocumentDownloader writes to a
            # deterministic path keyed on title+URL, so a later web update would
            # overwrite this revision's PDF in place. Copying to a path that
            # embeds the effective date preserves per-revision provenance.
            txt = extract_pdf_text(local_path)
            version_path = local_path.parent / (
                f"leaf-{sheet['leaf_no']}-eff-{sheet['effective_start']}{local_path.suffix}"
            )
            if commit and version_path != local_path:
                version_path.write_bytes(local_path.read_bytes())
            sidecar = version_path.with_suffix(version_path.suffix + ".txt")
            archived_url = f"duke-energy://{'dep' if sheet['company']=='progress' else 'dec'}-nc/leaf-{sheet['leaf_no']}#{sheet['effective_start']}"

            existing_v = _existing_version_at(conn, fk, sheet["effective_start"])

            if not commit:
                print(f"  (dry-run) would register historical_document (rev='{sheet['revision_label']}', "
                      f"eff={sheet['effective_start']}, pages=1-{sheet['end_page']})")
                if sheet["kind"] == "new_version":
                    print(f"  (dry-run) would upsert tariff_version + extract charges "
                          f"(existing version at {sheet['effective_start']}: {existing_v})")
                else:  # termination
                    print(f"  (dry-run) termination version (0 charges); "
                          f"close prior version id={sheet.get('close_prior_version_id')} end={TERM_PRIOR_END}")
                    if sheet.get("reconcile_version_id"):
                        print(f"  (dry-run) reconcile stale version id={sheet['reconcile_version_id']} "
                              f"(clear charges + relabel termination)")
                continue

            sidecar.write_text(txt, encoding="utf-8")
            hd = HistoricalDocumentRecord(
                family_key=fk,
                title=sheet["title"],
                state="NC",
                company=sheet["company"],
                category="rider",
                kind="pdf",
                canonical_url=sheet["url"],
                archived_url=archived_url,
                snapshot_timestamp=now,
                local_path=version_path,
                raw_text_path=sidecar,
                content_hash=downloaded.content_hash,
                content_type="application/pdf",
                direct_status_code=downloaded.status_code,
                direct_downloadable=True,
                revision_label=sheet["revision_label"],
                supersedes_label=sheet["supersedes_label"],
                leaf_no=sheet["leaf_no"],
                start_page=1,
                end_page=sheet["end_page"],
                evidence_json=None,
                effective_start=sheet["effective_start"],
                effective_end=None,
                retrieved_at=now,
                metadata_json=None,
                notes=["source=duke-energy.com", "ingest=dep_dec_web_updates_2026_06"],
                status="approved",
            )
            hd_id = repo.upsert_historical_document(hd)
            print(f"  historical_document id={hd_id}")

            if sheet["kind"] == "termination":
                _handle_termination(repo, conn, sheet, fk, hd_id, document_id, now)
            else:
                _handle_new_version(repo, conn, sheet, fk, hd_id, version_path, existing_v)

        if commit:
            conn.commit()
        print(f"\n{tag} complete.")
    finally:
        conn.close()
        downloader.close()


def _handle_new_version(repo, conn, sheet, fk, hd_id, local_path, existing_v) -> None:
    if existing_v is not None:
        print(f"  version already exists at {sheet['effective_start']} (id={existing_v}); skipping create")
        version_id = existing_v
    else:
        version_id = repo.upsert_tariff_version(
            TariffVersionRecord(
                family_key=fk,
                historical_document_id=hd_id,
                effective_start=sheet["effective_start"],
                revision_label=sheet["revision_label"],
                supersedes_label=sheet["supersedes_label"],
                leaf_no=sheet["leaf_no"],
                source_type="utility_current",
                confidence_score=0.95,
                source_pdf=str(local_path),
                notes="Ingested from duke-energy.com (June 2026 update).",
            )
        )
        print(f"  tariff_version id={version_id} eff={sheet['effective_start']}")

    ext = BulkExtractor(str(DB_PATH))
    doc = {
        "id": hd_id,
        "family_key": fk,
        "title": sheet["title"],
        "company": sheet["company"],
        "state": "NC",
        "local_path": str(local_path),
        "leaf_no": sheet["leaf_no"],
        "start_page": None,
        "end_page": None,
        "effective_start": sheet["effective_start"],
        "content_hash": "",
        "docket_number": None,
        "version_id": version_id,
    }
    _doc_id, _fk, inserted, status, profile = ext.process_document(doc, force_clear=True)
    print(f"  extracted charges={inserted} status={status} profile={profile}")

    # Also populate the rider_summary_blocks / rider_line_items layer that feeds
    # the Streamlit canonical residential timeline + trend (a separate read path
    # from tariff_charges). Uses the text parser, which handles the new layout.
    _persist_rider_summary_blocks(conn, sheet, local_path)


def _persist_rider_summary_blocks(conn, sheet, local_path) -> None:
    from dataclasses import asdict

    from duke_rates.db.ncuc_loader import persist_rider_summary_records
    from duke_rates.parse.rider_summary import parse_rider_summary

    utility = "DEP" if sheet["company"] == "progress" else "DEC"
    text = extract_pdf_text(local_path)
    result = parse_rider_summary(text, source_pdf=str(local_path), leaf_no=sheet["leaf_no"])
    record = asdict(result)
    record["source_pdf"] = str(local_path)
    record["effective_date"] = sheet["effective_start"]
    record["leaf_no"] = sheet["leaf_no"]
    inserted, skipped = persist_rider_summary_records(conn, [record], replace=True, utility=utility)
    res_class = "Residential Service Schedules" if utility == "DEP" else "Residential Schedules"
    res = next((b for b in result.rate_classes if b.rate_class == res_class), None)
    res_total = res.total_cents_per_kwh if res else None
    print(f"  rider_summary_blocks: {inserted} blocks ({utility}), residential total={res_total} c/kWh")


def _handle_termination(repo, conn, sheet, fk, hd_id, document_id, now) -> None:
    iso = now.isoformat()
    reconcile_id = sheet.get("reconcile_version_id")
    if reconcile_id is not None:
        # Reuse the stale 2026-06-01 row as the termination version.
        n = conn.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id=?", (reconcile_id,)).fetchone()[0]
        conn.execute("DELETE FROM tariff_charges WHERE version_id=?", (reconcile_id,))
        conn.execute(
            """UPDATE tariff_versions
               SET historical_document_id=?, revision_label=?, supersedes_label=?, leaf_no=?,
                   source_type='utility_current', confidence_score=0.95,
                   notes='Rider terminated 2026-06-01 (duke-energy.com First/Third Revised sheet).'
               WHERE id=?""",
            (hd_id, sheet["revision_label"], sheet["supersedes_label"], sheet["leaf_no"], reconcile_id),
        )
        version_id = reconcile_id
        print(f"  reconciled stale version id={reconcile_id}: cleared {n} old charges, relabeled termination")
    else:
        existing = _existing_version_at(conn, fk, sheet["effective_start"])
        if existing is not None:
            conn.execute(
                """UPDATE tariff_versions
                   SET historical_document_id=?, revision_label=?, supersedes_label=?, leaf_no=?,
                       source_type='utility_current', confidence_score=0.95,
                       notes='Rider terminated 2026-06-01 (duke-energy.com First/Third Revised sheet).'
                   WHERE id=?""",
                (hd_id, sheet["revision_label"], sheet["supersedes_label"], sheet["leaf_no"], existing),
            )
            conn.execute("DELETE FROM tariff_charges WHERE version_id=?", (existing,))
            version_id = existing
            print(f"  termination tariff_version id={version_id} already present; updated metadata (0 charges)")
        else:
            version_id = repo.upsert_tariff_version(
                TariffVersionRecord(
                    family_key=fk,
                    historical_document_id=hd_id,
                    effective_start=sheet["effective_start"],
                    revision_label=sheet["revision_label"],
                    supersedes_label=sheet["supersedes_label"],
                    leaf_no=sheet["leaf_no"],
                    source_type="utility_current",
                    confidence_score=0.95,
                    notes="Rider terminated 2026-06-01 (duke-energy.com First/Third Revised sheet).",
                )
            )
            print(f"  termination tariff_version id={version_id} eff={sheet['effective_start']} (0 charges)")

    prior = sheet.get("close_prior_version_id")
    if prior is not None:
        cur = conn.execute("SELECT effective_start, effective_end FROM tariff_versions WHERE id=?", (prior,)).fetchone()
        if cur is None:
            print(f"  WARNING: prior version id={prior} not found")
        elif cur["effective_end"] is not None:
            print(f"  prior version id={prior} already has effective_end={cur['effective_end']}; left unchanged")
        else:
            conn.execute("UPDATE tariff_versions SET effective_end=? WHERE id=?", (TERM_PRIOR_END, prior))
            print(f"  closed prior version id={prior} (eff_start={cur['effective_start']}) -> effective_end={TERM_PRIOR_END}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="Write changes (default: dry run)")
    args = ap.parse_args()
    run(commit=args.commit)


if __name__ == "__main__":
    main()
