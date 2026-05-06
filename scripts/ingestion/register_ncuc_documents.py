"""
Register downloaded NCUC compliance tariff PDFs in ncuc_discovery_records.
"""
import json
import sqlite3
import hashlib
import re
from pathlib import Path
from datetime import datetime

DB_PATH = "data/db/duke_rates.db"

# Load the scraped file metadata
with open("data/ncuc_tariff_filings.json") as f:
    all_files = json.load(f)

# High-priority name patterns (same as download_ncuc_tariffs.py)
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
    fn_lower = filename.lower()
    for pat in HIGH_PRIORITY_NAMES:
        if re.search(pat, fn_lower):
            return True
    return False

# Build mapping from ViewFile ID -> downloaded path
download_base = Path("data/downloads/ncuc_tariff")
downloaded_files = {}
for family_dir in download_base.iterdir():
    if family_dir.is_dir():
        for f in family_dir.glob("*.pdf"):
            # Filename is: {file_id}_{clean_name}.pdf
            # Extract file_id from filename
            parts = f.name.split("_", 1)
            if len(parts) == 2:
                # file_id is the ViewFile GUID
                file_id_part = parts[0]
                # But our file_id is the ViewFile Id GUID, not doc GUID
                # We stored it as the first 36 chars of the filename before first underscore
                downloaded_files[file_id_part] = f

# Build mapping from ViewFile URL Id -> path
viewfile_to_path = {}
for item in all_files:
    if not is_high_value_file(item["filename"]):
        continue
    file_id_match = re.search(r'Id=([0-9a-f\-]{36})', item["view_url"], re.I)
    if not file_id_match:
        continue
    file_id = file_id_match.group(1)

    family_dir = item["family"].replace("nc-", "").replace("-", "_")
    clean_name = re.sub(r'[^\w\s\-\.]', '_', item["filename"])[:60].strip()
    dest_path = download_base / family_dir / f"{file_id}_{clean_name}.pdf"

    viewfile_to_path[item["view_url"]] = {"path": dest_path, "item": item}

print(f"ViewFile URLs mapped to paths: {len(viewfile_to_path)}")

# Register in DB
conn = sqlite3.connect(DB_PATH)

registered = 0
skipped = 0
errors = []

now = datetime.utcnow().isoformat()

for view_url, data in viewfile_to_path.items():
    item = data["item"]
    path = data["path"]

    if not path.exists():
        print(f"  MISSING: {path.name}")
        continue

    # Compute hash
    with open(path, "rb") as f:
        content_hash = hashlib.sha256(f.read()).hexdigest()
    file_size = path.stat().st_size

    # Check if already registered
    existing = conn.execute(
        "SELECT id FROM ncuc_discovery_records WHERE content_hash = ? OR (local_path = ? AND local_path IS NOT NULL)",
        (content_hash, str(path))
    ).fetchone()

    if existing:
        print(f"  SKIP (already in DB): {path.name[:60]}")
        skipped += 1
        continue

    # Parse docket into components
    # E.g. "E-2 Sub 1354" -> utility="E", docket_number="E-2", sub_number="1354"
    docket_str = item["docket"]
    docket_match = re.match(r'(E-\d+)\s+Sub\s+(\d+)', docket_str)
    if docket_match:
        docket_number = docket_match.group(1)
        sub_number = docket_match.group(2)
        utility = "E"
    else:
        docket_number = docket_str
        sub_number = ""
        utility = "E"

    # Parse date
    date_filed = item.get("date_filed", "")
    if date_filed:
        # Convert MM/DD/YYYY to YYYY-MM-DD
        m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', date_filed)
        if m:
            date_filed = f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"

    # Determine filing classification
    fn_lower = item["filename"].lower()
    if "compliance tariff" in fn_lower or "nc rider" in fn_lower or "compliance tarif" in fn_lower:
        filing_classification = "compliance_tariff"
    elif "summary of riders" in fn_lower:
        filing_classification = "compliance_tariff"
    elif "application" in fn_lower:
        filing_classification = "application"
    else:
        filing_classification = "compliance_tariff"

    # Metadata
    provenance = {
        "source": "ncuc_portal_playwright",
        "docket": docket_str,
        "label": item["label"],
        "doc_title": item.get("doc_title", ""),
        "priority": item.get("priority", ""),
        "doc_id": item.get("doc_id", ""),
    }

    try:
        conn.execute("""
            INSERT INTO ncuc_discovery_records (
                docket_number, sub_number, utility,
                filing_title, filing_date,
                proceeding_type, filing_classification,
                family_keys_json,
                viewer_url, download_url,
                acquisition_method, fetch_status,
                local_path, content_hash, content_type,
                file_size_bytes,
                provenance_notes_json, search_query,
                created_at, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            docket_number,
            sub_number,
            utility,
            item["filename"],
            date_filed,
            "tariff_adjustment",
            filing_classification,
            json.dumps([item["family"]]),
            view_url,
            view_url,
            "playwright_download",
            "downloaded",
            str(path),
            content_hash,
            "application/pdf",
            file_size,
            json.dumps(provenance),
            f"docket:{docket_str}",
            now,
            now,
        ))
        conn.commit()
        registered += 1
        print(f"  REGISTERED: {path.name[:70]}")
    except Exception as e:
        errors.append({"file": path.name, "error": str(e)})
        print(f"  ERROR: {path.name[:50]}: {e}")

conn.close()

print(f"\n=== SUMMARY ===")
print(f"  Registered: {registered}")
print(f"  Skipped (already in DB): {skipped}")
print(f"  Errors: {len(errors)}")

# Show current discovery records count
conn = sqlite3.connect(DB_PATH)
total = conn.execute("SELECT COUNT(*) FROM ncuc_discovery_records").fetchone()[0]
downloaded_count = conn.execute(
    "SELECT COUNT(*) FROM ncuc_discovery_records WHERE fetch_status = 'downloaded'"
).fetchone()[0]
print(f"\n  Total ncuc_discovery_records: {total}")
print(f"  Downloaded (have local file): {downloaded_count}")
conn.close()
