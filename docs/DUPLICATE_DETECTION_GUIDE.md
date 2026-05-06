# Duplicate Detection Guide

**Last Updated:** 2026-03-31
**Status:** ✅ IMPLEMENTED
**Purpose:** Prevent re-downloading and reprocessing identical documents

---

## Overview

When downloading documents from the NCUC portal, you may encounter:
- The same tariff document filed under multiple dockets
- Different versions of the same document with identical content
- Documents already downloaded in previous sessions
- Copies of documents registered in different database tables

This guide explains how to detect and handle duplicates efficiently using checksums.

---

## How It Works

### 1. Checksum Calculation (SHA256)

Each PDF file has a unique SHA256 checksum based on its contents. Two files are identical if and only if they have the same SHA256 hash.

```python
from duke_rates.db.duplicate_detector import calculate_file_checksum

checksum = calculate_file_checksum("data/downloads/E-2_Sub_1354.pdf")
# Returns: "a7f3c9d8e1b2f4a6..."
```

### 2. Database Storage

Both `ncuc_discovery_records` and `historical_documents` tables have `content_hash` columns:

```sql
-- Check if a checksum exists
SELECT * FROM ncuc_discovery_records WHERE content_hash = 'a7f3c9d8e1b2f4a6...';

-- Find which document has which hash
SELECT id, title, content_hash FROM historical_documents WHERE content_hash IS NOT NULL;
```

### 3. Duplicate Detection

Before downloading, check if the checksum already exists in the database:

```python
from duke_rates.db.duplicate_detector import find_duplicate_by_checksum
import sqlite3

conn = sqlite3.connect("data/db/duke_rates.db")
checksum = "a7f3c9d8e1b2f4a6..."

duplicate = find_duplicate_by_checksum(conn, checksum)
if duplicate:
    print(f"Already have this document: {duplicate['source']} ID {duplicate['id']}")
    print(f"Title: {duplicate['title']}")
    print(f"Path: {duplicate['local_path']}")
```

### 4. Smart Download with Deduplication

The `SmartNcucDownloader` class combines everything:

```python
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.smart_downloader import SmartNcucDownloader
from pathlib import Path

conn = connect(Path("data/db/duke_rates.db"))
downloader = SmartNcucDownloader(conn, Path("data/downloads/ncuc_tariff"))

# When downloading a document
result = downloader.download_with_dedup(
    document_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?FileId=...",
    document_title="E-2 Sub 1354 Filing - January 2024",
    docket_number="E-2 Sub 1354",
    discovery_record_id=42,  # Optional: update this database row
    download_func=my_download_function,  # Callable: (url, path) -> file_size
)

if result.skipped:
    print(f"Skipped: {result.reason}")
elif result.success:
    print(f"Downloaded: {result.file_path}")
    if result.duplicate_of:
        print(f"  (Duplicate of {result.duplicate_of['title']})")
else:
    print(f"Failed: {result.reason}")

# Print statistics
downloader.print_summary()
```

---

## Workflow: Download → Check → Store

### Complete Download Workflow

```python
from playwright.sync_api import sync_playwright
from duke_rates.config import Settings
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context
from duke_rates.historical.ncuc.smart_downloader import SmartNcucDownloader
from pathlib import Path

settings = Settings()
conn = connect(settings.database_path)
downloader = SmartNcucDownloader(conn, settings.downloads_dir)

# Create authenticated session
pw, ctx, page = create_authenticated_context(settings)

try:
    # Define download function that uses the authenticated browser
    def download_via_browser(url: str, dest_path: Path) -> int:
        from duke_rates.historical.ncuc.session import download_view_file
        return download_view_file(page, url, dest_path)

    # Search for documents, then download each one
    for doc_url in document_urls:
        result = downloader.download_with_dedup(
            document_url=doc_url,
            document_title="My Document Title",
            docket_number="E-2 Sub 1354",
            discovery_record_id=doc_id,
            download_func=download_via_browser,
        )
        print(result)

    downloader.print_summary()

finally:
    close_authenticated_context(pw, ctx)
    conn.close()
```

---

## Maintenance: Check Existing Downloads

### List All Checksums in Database

```python
from duke_rates.db.duplicate_detector import get_all_checksums_in_database
import sqlite3

conn = sqlite3.connect("data/db/duke_rates.db")
checksums = get_all_checksums_in_database(conn)
print(f"Database contains {len(checksums)} unique documents (by content hash)")
```

### Find Duplicate Files

```bash
# Run the deduplication utility
python scripts/maintenance/deduplicate_downloads.py --check

# Output example:
# [DUPLICATE] 3 copies of same content (hash: a7f3c9d8...)
#   - ncuc_discovery ID 42: E-2 Sub 1354 Filing
#     Path: data/downloads/ncuc_tariff/E-2_Sub_1354/filing_001.pdf
#   - historical_documents ID 89: E-2 Sub 1354 Filing (Archived)
#     Path: data/downloads/archived/filing_001.pdf
#   - ncuc_discovery ID 156: E-2 Sub 1143 Filing
#     Path: data/downloads/ncuc_tariff/E-2_Sub_1143/filing_001.pdf
```

### Calculate Missing Checksums

If documents don't have checksums yet:

```bash
# Calculate SHA256 for all downloaded files without checksums
python scripts/maintenance/deduplicate_downloads.py --fix

# Output:
# [UPDATING] Calculating checksums for documents without them...
# [OK] Updated checksum for ncuc_discovery ID 42
# [OK] Updated checksum for ncuc_discovery ID 43
# ...
# [COMPLETE] Updated checksums for 127 documents
```

### Mark Duplicate Records

Instead of deleting duplicates, mark them as references:

```bash
# Mark all duplicate records (keeps highest quality version)
python scripts/maintenance/deduplicate_downloads.py --remove

# Updates fetch_status = 'duplicate' for ncuc_discovery_records
# Updates metadata_json with duplicate_of reference for historical_documents
```

---

## Database Queries

### Find All Duplicates

```sql
-- Count how many checksums have multiple files
SELECT
    content_hash,
    COUNT(*) as count
FROM (
    SELECT content_hash FROM ncuc_discovery_records
    WHERE content_hash IS NOT NULL
    UNION ALL
    SELECT content_hash FROM historical_documents
    WHERE content_hash IS NOT NULL
)
GROUP BY content_hash
HAVING count > 1;
```

### Find Files Without Checksums

```sql
-- ncuc_discovery_records without checksums
SELECT id, local_path FROM ncuc_discovery_records
WHERE local_path IS NOT NULL AND content_hash IS NULL;

-- historical_documents without checksums
SELECT id, local_path FROM historical_documents
WHERE local_path IS NOT NULL AND content_hash IS NULL;
```

### Find Duplicate by Checksum

```sql
-- Find all records with same content
SELECT
    'ncuc_discovery' as source,
    id,
    title,
    local_path
FROM ncuc_discovery_records
WHERE content_hash = 'a7f3c9d8e1b2f4a6...'

UNION ALL

SELECT
    'historical_documents',
    id,
    title,
    local_path
FROM historical_documents
WHERE content_hash = 'a7f3c9d8e1b2f4a6...';
```

---

## Performance Considerations

### Checksum Calculation Time

SHA256 checksums are fast but not instant:

```
Small file (50 KB):    ~1 ms
Medium file (500 KB):  ~10 ms
Large file (5 MB):     ~100 ms
```

On a session with 200 downloads: ~2 seconds total for all checksums.

### Batch Operations

For operations involving many files, use batch functions:

```python
from duke_rates.db.duplicate_detector import batch_check_checksums

# Check 100 hashes at once (faster than individual checks)
hashes = ["hash1", "hash2", "hash3", ...]
results = batch_check_checksums(conn, hashes)
for hash_value, exists in results.items():
    if exists:
        print(f"{hash_value} already in database")
```

---

## Common Scenarios

### Scenario 1: Re-downloading After Session

You previously downloaded documents but started a new session. Before downloading again:

```python
from duke_rates.db.duplicate_detector import checksum_exists_in_ncuc_discovery

# Check if already downloaded
if checksum_exists_in_ncuc_discovery(conn, "a7f3c9d8e1b2f4a6..."):
    print("Skip: Already downloaded in previous session")
    skip_this_document = True
```

### Scenario 2: Cross-Docket Duplicates

Same tariff filed in multiple dockets:

```sql
-- Find documents that appear in multiple dockets
SELECT content_hash, COUNT(DISTINCT docket_number) as docket_count
FROM ncuc_discovery_records
WHERE content_hash IS NOT NULL
GROUP BY content_hash
HAVING docket_count > 1;

-- List which dockets
SELECT DISTINCT docket_number FROM ncuc_discovery_records
WHERE content_hash = 'a7f3c9d8e1b2f4a6...';
```

### Scenario 3: Different Registration, Same Content

Document exists in both `ncuc_discovery_records` and `historical_documents`:

```python
duplicate = find_duplicate_by_checksum(conn, checksum)
if duplicate:
    print(f"This document is in {duplicate['source']}")
    # Avoid registering it twice
    # Link the two records instead
```

---

## Best Practices

### 1. Always Calculate After Downloading

```python
# Good:
download_file(url, path)
checksum = calculate_file_checksum(path)
store_checksum_in_db(record_id, checksum)

# Bad:
download_file(url, path)
# (Forget to calculate/store checksum)
```

### 2. Check Before Downloading

```python
# Good:
should_download, reason = downloader.should_download(url, title)
if not should_download:
    print(f"Skip: {reason}")
    continue
download_file(url)

# Bad:
# (Download everything, then check later)
```

### 3. Use SmartNcucDownloader

```python
# Good: Automated duplicate detection
result = downloader.download_with_dedup(url, title, docket, func)
if result.skipped:
    skipped_count += 1

# Bad: Manual duplicate checking
download_file(url)
checksum = calculate_checksum(file)
if checksum_in_db(checksum):
    # (Now it's too late, already downloaded)
```

### 4. Batch Operations

```python
# Good: Fast batch check
results = batch_check_checksums(conn, [hash1, hash2, hash3, ...])

# Bad: Individual checks in a loop
for hash_value in large_list:
    if checksum_exists_in_ncuc_discovery(conn, hash_value):  # SLOW
        ...
```

---

## Troubleshooting

### "File Not Found" When Calculating Checksum

```python
# Problem
checksum = calculate_file_checksum("nonexistent.pdf")
# FileNotFoundError: File not found: nonexistent.pdf

# Solution
from pathlib import Path
file_path = Path("data/downloads/E-2_Sub_1354.pdf")
if file_path.exists():
    checksum = calculate_file_checksum(file_path)
else:
    print(f"File doesn't exist: {file_path}")
```

### "Database Locked" When Updating Checksums

```python
# Problem
update_checksum_in_ncuc_discovery(conn, record_id, file_path)
# sqlite3.OperationalError: database is locked

# Solution
# Close any other connections to the database
# Or wait a moment and retry
import time
time.sleep(1)
try:
    update_checksum_in_ncuc_discovery(conn, record_id, file_path)
except sqlite3.OperationalError:
    print("Database still locked, skipping")
```

### Checksum Mismatch

Two files claim to have the same checksum but are different:

```python
# This shouldn't happen with SHA256, but if it does:
hash1 = calculate_file_checksum("file1.pdf")
hash2 = calculate_file_checksum("file2.pdf")
if hash1 == hash2 and file1 != file2:
    # File corruption or hash calculation error
    # Re-calculate to verify
    hash1_recheck = calculate_file_checksum("file1.pdf")
    if hash1 != hash1_recheck:
        print("ERROR: File is changing (possibly corrupted)")
```

---

## Summary

**Three Core Functions:**

1. **`calculate_file_checksum(path)`** — Get SHA256 of a PDF
2. **`find_duplicate_by_checksum(conn, hash)`** — Find existing document with same content
3. **`SmartNcucDownloader`** — Download with automatic duplicate detection

**Workflow:**
- Before download: Check if checksum exists
- During download: Use authenticated browser to fetch file
- After download: Calculate checksum and store in database
- Duplicate found: Reference the original, skip reprocessing

**Maintenance:**
- Run `deduplicate_downloads.py --check` to find duplicates
- Run `deduplicate_downloads.py --fix` to calculate missing checksums
- Run `deduplicate_downloads.py --remove` to mark duplicates

**Result:** Avoid redundant downloads, reprocessing, and duplicate entries in the extraction pipeline.
