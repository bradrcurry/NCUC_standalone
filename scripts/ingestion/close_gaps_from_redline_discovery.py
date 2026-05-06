"""
Gap closure script driven by redline cross-reference discovery.

Three tiers of work, run in priority order:

TIER 1 — Bootstrap tariff_versions for registered-but-unversioned docs (immediate wins).
  86 DEP families have local PDFs in historical_documents with no tariff_version.
  These are downloaded but never processed. Run BulkExtractor on each.

TIER 2 — Portal harvest for 6 dockets completely absent from local corpus.
  E-2 Sub 938, 950, 1060 (DEP DSM/EE programs)
  E-7 Sub 1055, 1093, 1272 (DEC DSM/EE programs)
  Requires authenticated NCUC portal session.

TIER 3 — Download 2 pending discovery records that have URLs but weren't fetched.
  id=2135 (Sub 927, LC rider), id=2136 (Sub 952, NESB rider).
  No portal auth needed — direct download_url available.

Run:
    python scripts/ingestion/close_gaps_from_redline_discovery.py --tier 1
    python scripts/ingestion/close_gaps_from_redline_discovery.py --tier 2
    python scripts/ingestion/close_gaps_from_redline_discovery.py --tier 3
    python scripts/ingestion/close_gaps_from_redline_discovery.py  (all tiers)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

DB_PATH = str(ROOT / "data" / "db" / "duke_rates.db")
NOW = datetime.now(timezone.utc).isoformat()

DOWNLOAD_ROOT = ROOT / "data" / "historical" / "ncuc"


# ===========================================================================
# Tier 1 — Bootstrap tariff_versions for unversioned docs and re-extract
# ===========================================================================

def tier1_bootstrap_unversioned():
    print("=" * 60)
    print("TIER 1: Bootstrapping tariff_versions for unversioned docs")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)

    # All historical_documents with local PDFs but no tariff_version
    unversioned = conn.execute("""
        SELECT hd.id, hd.family_key, hd.local_path, hd.effective_start,
               hd.title, hd.leaf_no, hd.category, hd.company
        FROM historical_documents hd
        LEFT JOIN tariff_versions tv ON tv.historical_document_id = hd.id
        WHERE hd.local_path IS NOT NULL
          AND (hd.family_key LIKE 'nc-progress-leaf-%'
               OR hd.family_key LIKE 'nc-carolinas-%')
          AND tv.id IS NULL
        ORDER BY hd.family_key, hd.effective_start
    """).fetchall()

    conn.close()

    print(f"Found {len(unversioned)} unversioned docs with local PDFs")

    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
    extractor = BulkExtractor(DB_PATH)

    total_inserted = 0
    results_by_family: dict[str, list] = defaultdict(list)

    for hd_id, fk, local_path, eff, title, leaf_no, category, company in unversioned:
        if not Path(local_path).exists():
            print(f"  MISSING FILE: {local_path}")
            continue

        doc = {
            "id": hd_id,
            "family_key": fk,
            "local_path": local_path,
            "effective_start": eff,
            "version_id": None,
            "title": title or "",
            "company": company or ("DEP" if "progress" in fk else "DEC"),
            "state": "NC",
            "content_hash": None,
            "revision_label": None,
            "supersedes_label": None,
            "leaf_no": leaf_no,
            "start_page": None,
            "end_page": None,
            "discovery_record_id": None,
            "docket_number": None,
            "acquisition_method": "manual_registration",
            "discovery_doc_quality_tier": "T1",
        }

        try:
            _, _, n = extractor.process_document(doc)
            total_inserted += n
            results_by_family[fk].append(n)
            status = f"{n} charges"
        except Exception as e:
            results_by_family[fk].append(0)
            status = f"ERROR: {e}"

        print(f"  hd={hd_id:5}  {fk:38}  eff={str(eff):15}  {status}")

    print(f"\nTotal charges inserted: {total_inserted}")
    print("\nSummary by family:")

    families_with_charges = 0
    for fk in sorted(results_by_family):
        counts = results_by_family[fk]
        total = sum(counts)
        if total > 0:
            families_with_charges += 1
        print(f"  {fk:40}  {len(counts)} docs  {total} charges")

    print(f"\n{families_with_charges} families now have charges")


# ===========================================================================
# Tier 2 — Portal harvest for 6 absent dockets
# ===========================================================================

# Dockets completely absent from local corpus, identified from redline cross-refs
ABSENT_DOCKETS = [
    # DEP DSM/EE program dockets (E-2)
    ("E-2 Sub 938", "Duke Energy Progress", "dep",
     "DEP HEIP program historical",
     ("heip", "home energy improvement", "residential home energy", "leaf 719",
      "compliance tariff", "tariff", "modification", "program")),
    ("E-2 Sub 950", "Duke Energy Progress", "dep",
     "DEP SBES program historical",
     ("sbes", "small business energy saver", "smart business", "leaf 701",
      "compliance tariff", "tariff", "modification", "program")),
    ("E-2 Sub 1060", "Duke Energy Progress", "dep",
     "DEP residential program historical",
     ("residential", "leaf 70", "compliance tariff", "tariff", "program",
      "rider", "modification", "click")),
    # DEC DSM/EE program dockets (E-7)
    ("E-7 Sub 1055", "Duke Energy Carolinas", "dec",
     "DEC DSM/EE program historical",
     ("dsm", "energy efficiency", "rider", "compliance tariff", "tariff",
      "leaf", "program", "modification", "click")),
    ("E-7 Sub 1093", "Duke Energy Carolinas", "dec",
     "DEC DSM/EE program historical",
     ("dsm", "energy efficiency", "rider", "compliance tariff", "tariff",
      "rider pm", "program", "click")),
    ("E-7 Sub 1272", "Duke Energy Carolinas", "dec",
     "DEC Net Energy Metering / program",
     ("net energy metering", "nem", "compliance tariff", "tariff",
      "rider", "solar", "leaf", "click")),
]

EXCLUDE_TERMS = (
    "cover letter", "redlined", "customer notice", "notice of", "notice ",
    "confidential", "motion", "brief", "testimony", "certificate",
    "procedural", "service list", "data request", "discovery request",
    "application", "petition", "order",
)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def is_excluded(text: str) -> bool:
    lowered = norm(text)
    return any(term in lowered for term in EXCLUDE_TERMS)


def matches_terms(text: str, terms: tuple) -> bool:
    lowered = norm(text)
    return any(term in lowered for term in terms)


def slugify(text: str, limit: int = 90) -> str:
    cleaned = re.sub(r"[^\w\s\-\.]", "_", text or "")
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned[:limit] or "document"


def tier2_portal_harvest():
    print()
    print("=" * 60)
    print("TIER 2: Portal harvest for 6 absent dockets")
    print("=" * 60)

    try:
        from duke_rates.historical.ncuc.session import (
            create_authenticated_context, close_authenticated_context,
            download_view_file, get_docket_documents, resolve_docket_ids,
        )
        from duke_rates.db.duplicate_detector import calculate_file_checksum, find_duplicate_by_checksum
        from duke_rates.config import Settings
    except ImportError as e:
        print(f"Portal dependencies not available: {e}")
        print("Skipping Tier 2.")
        return

    conn = sqlite3.connect(DB_PATH)
    downloaded = []
    failed = []
    settings = Settings()

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except Exception as e:
        print(f"Portal authentication failed: {e}")
        conn.close()
        return

    try:
        for docket, company, slug, focus, terms in ABSENT_DOCKETS:
            print(f"\n  === {docket} | {focus} ===")

            try:
                from duke_rates.historical.ncuc.document_param_search import DocumentParamSearcher
                searcher = DocumentParamSearcher(settings)
                rows = searcher.search(page, company_name=company, docket_number=docket, max_results=50)
            except Exception as e:
                print(f"  Search failed: {e}")
                failed.append((docket, str(e)))
                continue

            print(f"  Found {len(rows)} filing rows — enriching detail pages...")

            # Pre-filter to rows that look relevant before hitting detail pages
            candidate_rows = []
            PORTAL_PLACEHOLDER = "click the to view the document"
            for row in rows:
                combined = row.description or ""
                if is_excluded(combined):
                    continue
                # Accept if terms match, description is very short, or it's the
                # portal placeholder text (means the filing has no text description)
                is_placeholder = PORTAL_PLACEHOLDER in norm(combined)
                if matches_terms(combined, terms) or len(combined.strip()) < 30 or is_placeholder:
                    candidate_rows.append(row)
            print(f"  {len(candidate_rows)} candidate rows after pre-filter")

            # Enrich with detail page URLs (this navigates each detail page)
            from duke_rates.historical.ncuc.document_param_search import fetch_document_detail
            matched = 0

            for row in candidate_rows:
                if not row.document_detail_url:
                    continue

                try:
                    detail = fetch_document_detail(page, row.document_detail_url)
                    labels = detail["view_file_labels"]
                    urls = detail["view_file_urls"]
                except Exception as e:
                    print(f"    Detail page error for {row.description[:50]}: {e}")
                    continue

                if not urls:
                    print(f"    No ViewFile links on detail page: {row.description[:50]}")
                    continue

                for label, url in zip_longest(labels, urls, fillvalue=""):
                    if not url:
                        continue
                    combined = f"{row.description} {label}".strip()
                    if is_excluded(combined):
                        continue

                    matched += 1
                    title = label or row.description or focus

                    # Build destination path
                    docket_slug = slugify(docket.replace(" ", "_"), 40)
                    date_slug = slugify((row.date_filed or "undated").replace("/", "-"), 20)
                    view_id_m = re.search(r"Id=([0-9a-f\-]{36})", url, re.I)
                    view_id = view_id_m.group(1)[:8] if view_id_m else "unk"
                    fname = f"{date_slug}_{view_id}_{slugify(title)}.pdf"

                    dest = DOWNLOAD_ROOT / slug / docket_slug / fname
                    dest.parent.mkdir(parents=True, exist_ok=True)

                    if dest.exists():
                        print(f"    SKIP (exists): {fname}")
                        continue

                    try:
                        nbytes = download_view_file(page, url, dest)
                        if nbytes < 1000:
                            print(f"    FAIL (empty): {title[:50]}")
                            dest.unlink(missing_ok=True)
                            failed.append((docket, f"empty response for {title[:40]}"))
                            continue

                        checksum = calculate_file_checksum(str(dest))

                        # Record in ncuc_discovery_records
                        conn.execute("""
                            INSERT OR IGNORE INTO ncuc_discovery_records
                                (docket_number, sub_number, utility, filing_title,
                                 filing_date, fetch_status, local_path,
                                 file_size_bytes, content_hash, download_url,
                                 acquisition_method, created_at, fetched_at)
                            VALUES (?, ?, ?, ?, ?, 'downloaded', ?, ?, ?, ?, 'portal_harvest', ?, ?)
                        """, (
                            docket.split(" Sub ")[0].strip(),
                            docket.split(" Sub ")[1].strip() if " Sub " in docket else None,
                            company,
                            title,
                            row.date_filed,
                            str(dest),
                            nbytes,
                            checksum,
                            url,
                            NOW, NOW,
                        ))
                        conn.commit()

                        print(f"    DOWNLOADED ({nbytes//1024}KB): {title[:60]}")
                        downloaded.append({
                            "docket": docket, "title": title,
                            "path": str(dest), "date": row.date_filed,
                        })

                    except Exception as e:
                        print(f"    ERROR downloading {title[:50]}: {e}")
                        failed.append((docket, str(e)))

            print(f"  Matched {matched} attachments for {docket}")

    finally:
        close_authenticated_context(pw, ctx)
        conn.close()

    print(f"\nTier 2 complete: {len(downloaded)} files downloaded, {len(failed)} failures")
    if downloaded:
        print("\nDownloaded files:")
        for d in downloaded:
            print(f"  {d['docket']:20}  {d['date']:12}  {d['title'][:55]}")
    if failed:
        print("\nFailures:")
        for docket, err in failed:
            print(f"  {docket}  {err[:80]}")


# ===========================================================================
# Tier 3 — Download 2 pending discovery records
# ===========================================================================

PENDING_IDS = [2135, 2136]   # Sub 927 LC rider, Sub 952 NESB rider


def tier3_download_pending():
    print()
    print("=" * 60)
    print("TIER 3: Downloading 2 pending discovery records")
    print("=" * 60)

    try:
        from duke_rates.historical.ncuc.session import (
            create_authenticated_context, close_authenticated_context, download_view_file,
        )
        from duke_rates.db.duplicate_detector import calculate_file_checksum
        from duke_rates.config import Settings
    except ImportError as e:
        print(f"Portal dependencies not available: {e}")
        return

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, docket_number, sub_number, filing_title, download_url, viewer_url
        FROM ncuc_discovery_records
        WHERE id IN ({})
    """.format(",".join(str(i) for i in PENDING_IDS))).fetchall()

    if not rows:
        print("  No matching pending records found.")
        conn.close()
        return

    settings = Settings()
    try:
        pw, ctx, page = create_authenticated_context(settings)
    except Exception as e:
        print(f"Portal authentication failed: {e}")
        conn.close()
        return

    downloaded = 0
    try:
        for rec_id, docket, sub, title, dl_url, viewer_url in rows:
            detail_url = dl_url or viewer_url
            if not detail_url:
                print(f"  id={rec_id}: no URL, skipping")
                continue

            print(f"  id={rec_id}  sub={sub}  {(title or '')[:55]}")
            print(f"    Navigating detail page: ...{detail_url[-60:]}")

            try:
                # Navigate to the document detail page
                page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                # Find all ViewFile.aspx links on the detail page
                view_links = page.query_selector_all("a[href*='ViewFile']")
                if not view_links:
                    # Try broader search for PDF links
                    view_links = page.query_selector_all("a[href*='.pdf'], a[href*='ViewFile'], a[href*='view']")

                if not view_links:
                    print(f"    No ViewFile links found on detail page")
                    continue

                docket_slug = slugify(
                    f"{docket or 'e-2'}_sub_{sub or 'unk'}".replace(" ", "_").replace(",", ""), 40
                )
                dest_dir = DOWNLOAD_ROOT / "dep" / docket_slug
                dest_dir.mkdir(parents=True, exist_ok=True)

                for link in view_links[:3]:  # at most 3 attachments per filing
                    href = link.get_attribute("href") or ""
                    if not href:
                        continue
                    if not href.startswith("http"):
                        href = "https://starw1.ncuc.gov" + href

                    link_text = (link.inner_text() or title or "attachment").strip()
                    if is_excluded(link_text):
                        continue

                    view_id_m = re.search(r"[Ii]d=([0-9a-f\-]{8,36})", href)
                    view_id = view_id_m.group(1)[:8] if view_id_m else "unk"
                    fname = f"{slugify(link_text)[:60]}_{view_id}.pdf"
                    dest = dest_dir / fname

                    if dest.exists():
                        print(f"    SKIP (exists): {fname}")
                        continue

                    try:
                        nbytes = download_view_file(page, href, dest)
                        if nbytes < 1000:
                            dest.unlink(missing_ok=True)
                            print(f"    FAIL empty: {fname}")
                            continue

                        conn.execute("""
                            UPDATE ncuc_discovery_records
                            SET fetch_status = 'downloaded', local_path = ?,
                                file_size_bytes = ?, fetched_at = ?
                            WHERE id = ?
                        """, (str(dest), nbytes, NOW, rec_id))
                        conn.commit()

                        print(f"    DOWNLOADED ({nbytes//1024}KB): {fname}")
                        downloaded += 1

                    except Exception as e:
                        print(f"    DL ERROR {fname}: {str(e)[:80]}")

            except Exception as e:
                print(f"    PAGE ERROR: {str(e)[:120]}")

    finally:
        close_authenticated_context(pw, ctx)
        conn.close()

    print(f"\nTier 3 complete: {downloaded} files downloaded")


# ===========================================================================
# Main
# ===========================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Close tariff coverage gaps identified from redline cross-reference discovery"
    )
    parser.add_argument(
        "--tier", choices=["1", "2", "3", "all"], default="all",
        help="Which tier to run (default: all). Tier 1 is safe/offline; Tiers 2-3 require portal auth."
    )
    args = parser.parse_args()

    if args.tier in ("1", "all"):
        tier1_bootstrap_unversioned()

    if args.tier in ("2", "all"):
        tier2_portal_harvest()

    if args.tier in ("3", "all"):
        tier3_download_pending()

    print()
    print("Done.")
