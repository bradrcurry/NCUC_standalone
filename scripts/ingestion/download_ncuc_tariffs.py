"""
Download high-priority compliance tariff PDFs from NCUC ViewFile URLs.
Registers them in the DB discovery queue for parsing.

Uses data/ncuc_tariff_filings.json as input.
Downloads to data/downloads/ncuc_tariff/ subdirectories by family.
"""
import json
import re
import sqlite3
import hashlib
import os
from pathlib import Path
from datetime import datetime
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()
DB_PATH = "data/db/duke_rates.db"

# Load scraped file links
with open("data/ncuc_tariff_filings.json") as f:
    all_files = json.load(f)

# High-priority files: those with "compliance tariff" or "revised tariff" or actual leaf sheet names
HIGH_PRIORITY_NAMES = [
    r"nc rider jaa",
    r"nc summary of riders",
    r"compliance tariff",
    r"revised compliance tariff",
    r"dsm.*compliance",
    r"sts compliance tariff",
    r"dep compliance tariff",
    r"dec.*compliance tariff",
]

def is_high_value_file(filename):
    """Check if this file is likely to be an actual tariff sheet PDF."""
    fn_lower = filename.lower()
    for pat in HIGH_PRIORITY_NAMES:
        if re.search(pat, fn_lower):
            return True
    return False

# Categorize files
download_queue = []
skip_queue = []

for item in all_files:
    fn = item["filename"]
    if is_high_value_file(fn):
        download_queue.append(item)
    else:
        skip_queue.append(item)

print(f"High-value files to download: {len(download_queue)}")
print(f"Skipping: {len(skip_queue)}")
print()

# Print what we'll download
for item in download_queue:
    print(f"  [{item['priority']}] {item['label']} | {item['date_filed']}")
    print(f"    Filename: {item['filename'][:70]}")
    print(f"    URL: {item['view_url']}")
    print()

print(f"\n{'='*60}")
print(f"Starting downloads...")
print(f"{'='*60}\n")

# Download files using Playwright (Cloudflare protected)
pw, ctx, page = create_authenticated_context(settings)

def download_file(view_url, dest_path):
    """Download a file from a ViewFile URL using authenticated Playwright."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # ViewFile.aspx triggers a direct file download.
    # page.goto raises "Download is starting" — we must catch that and still
    # wait for the download event separately.
    with page.expect_download(timeout=60000) as download_info:
        try:
            page.goto(view_url, wait_until="commit", timeout=30000)
        except Exception as e:
            if "Download is starting" not in str(e):
                raise
            # Expected — the download has started, continue to wait for it

    download = download_info.value
    download.save_as(str(dest_path))
    return dest_path

downloaded = []
errors = []

try:
    for item in download_queue:
        # Create safe filename from the ViewFile GUID + original name hint
        file_id_match = re.search(r'Id=([0-9a-f\-]{36})', item["view_url"], re.I)
        file_id = file_id_match.group(1) if file_id_match else "unknown"

        # Clean filename for filesystem
        clean_name = re.sub(r'[^\w\s\-\.]', '_', item["filename"])[:60].strip()
        family_dir = item["family"].replace("nc-", "").replace("-", "_")

        dest_dir = Path(f"data/downloads/ncuc_tariff/{family_dir}")
        dest_path = dest_dir / f"{file_id}_{clean_name}.pdf"

        if dest_path.exists():
            size = dest_path.stat().st_size
            print(f"  [SKIP already exists] {dest_path.name} ({size} bytes)")
            downloaded.append({"item": item, "path": str(dest_path), "size": size, "status": "exists"})
            continue

        print(f"  Downloading: {item['filename'][:60]}")
        print(f"    -> {dest_path}")
        try:
            download_file(item["view_url"], dest_path)
            size = dest_path.stat().st_size
            print(f"    OK: {size:,} bytes")
            downloaded.append({"item": item, "path": str(dest_path), "size": size, "status": "new"})
        except Exception as e:
            print(f"    ERROR: {e}")
            errors.append({"item": item, "error": str(e)})

finally:
    close_authenticated_context(pw, ctx)

print(f"\n\n=== DOWNLOAD RESULTS ===")
print(f"  Downloaded: {len([d for d in downloaded if d['status'] == 'new'])}")
print(f"  Already existed: {len([d for d in downloaded if d['status'] == 'exists'])}")
print(f"  Errors: {len(errors)}")

for d in downloaded:
    status = d["status"].upper()
    item = d["item"]
    print(f"  [{status}] {item['label']} | {item['date_filed']} | {d['size']:,} bytes")
    print(f"    {d['path']}")

if errors:
    print("\n  Errors:")
    for e in errors:
        print(f"    {e['item']['filename'][:50]}: {e['error']}")
