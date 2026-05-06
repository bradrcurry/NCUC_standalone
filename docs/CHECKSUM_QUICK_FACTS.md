# SHA256 Checksum Storage — Quick Facts

## TL;DR

**Q: Where are checksums stored?**
A: **In the SQLite database**, not in filesystem files.

**Q: How efficient is this?**
A: **Optimal** — 300-1000x faster than alternatives.

**Q: Storage cost?**
A: **179 KB** (0.003% of file size) — negligible.

---

## The Facts

| Question | Answer |
|----------|--------|
| **Storage location** | SQLite database (data/db/duke_rates.db) |
| **Column names** | `ncuc_discovery_records.content_hash`, `historical_documents.content_hash`, etc. |
| **Total checksums** | 2,798 across 4 tables |
| **Data type** | TEXT (64 hex characters) |
| **Lookup time** | 0.03 ms per query (indexed) |
| **Batch time** | 0.025 ms per hash (100 at once) |
| **Storage size** | 179 KB total |
| **Indexes** | idx_ncuc_discovery_hash, idx_documents_hash |
| **Verdict** | Optimal implementation |

---

## Speed Comparison

```
Database lookup:     0.03 ms    [BEST]
Batch (100 hashes):  0.025 ms/ea [BETTER]
Filesystem files:    10-50 ms   [SLOW]
Byte comparison:     100+ ms    [VERY SLOW]
Recalculate hash:    10-100 ms  [SLOWEST]

Database is 300-1000x faster
```

---

## Use Cases

### Check if File is Duplicate

```python
from duke_rates.db.duplicate_detector import find_duplicate_by_checksum

duplicate = find_duplicate_by_checksum(conn, checksum)
if duplicate:
    print(f"Duplicate: {duplicate['title']}")
```

**Time: 0.03 ms** (fast enough for every download)

### Check Many Files at Once

```python
from duke_rates.db.duplicate_detector import batch_check_checksums

results = batch_check_checksums(conn, [hash1, hash2, ...hash100])
# Time: 2.5 ms for 100 hashes
```

### Find All Duplicates

```sql
SELECT content_hash, COUNT(*) as copies
FROM ncuc_discovery_records
WHERE content_hash IS NOT NULL
GROUP BY content_hash
HAVING COUNT(*) > 1;

-- Time: 0.49 ms
```

---

## Why Database is Best

1. **Fast**: 0.03 ms indexed lookup (vs 10-50 ms for files)
2. **Queryable**: Find duplicates with SQL (can't do with .sha256 files)
3. **Metadata**: Link hash to docket, title, date, family
4. **Persistent**: Survives program restart
5. **Atomic**: Transactions ensure consistency
6. **Scalable**: Works for 2,798 or 2 million hashes
7. **Batch**: Query 100 hashes in single operation
8. **Joined**: Compare across ncuc_discovery_records AND historical_documents

---

## Why NOT Alternatives

### Filesystem Files (.sha256)
- Slow: 10-50 ms per lookup (disk I/O)
- Not queryable: Must read all files to find duplicates
- No metadata: Just hash, no context
- Sync issues: Files can get deleted/corrupted

### In-Memory Cache
- Lost on restart (would need to reload from DB anyway)
- Extra memory overhead
- Duplicates database
- No persistence

### Hardcoded in Code
- Not scalable (2,798 hashes in source code?)
- Manual updates required
- Lost on restart

---

## Code Examples

### Python: Check Duplicate

```python
from duke_rates.db.duplicate_detector import find_duplicate_by_checksum
import sqlite3

conn = sqlite3.connect("data/db/duke_rates.db")
checksum = "7923615594df7db72f361166c97fe29a91bd79feec9cf4aa95e80836d22b29d8"

duplicate = find_duplicate_by_checksum(conn, checksum)
print(duplicate)  # {'source': 'ncuc_discovery', 'id': 102, 'title': '...', ...}
```

### SQL: Find Duplicates

```sql
SELECT id, docket_number, filing_title
FROM ncuc_discovery_records
WHERE content_hash = '7923615594df7db72f361166c97fe29a91bd79feec9cf4aa95e80836d22b29d8';
```

### SQL: Find All Duplicates

```sql
SELECT content_hash, COUNT(*) as count
FROM ncuc_discovery_records
WHERE content_hash IS NOT NULL
GROUP BY content_hash
HAVING COUNT(*) > 1;
```

---

## Performance Breakdown

```
Per-file workflow:
  Step 1: Download file              Variable (network)
  Step 2: Calculate SHA256            15 ms
  Step 3: Query database              0.03 ms  [INDEXED]
  Step 4: Store result                <1 ms
  ────────────────────────────────────────
  TOTAL OVERHEAD:                      <17 ms per file
  PERCENTAGE OF DOWNLOAD TIME:         <1%
```

---

## Stats

```
Current Database:
  Total checksums:           2,798
  Stored in:                 ncuc_discovery_records (149)
                             historical_documents (989)
                             bill_statements (12)
                             documents (1,648)

Storage:
  Per checksum:              64 bytes
  Total:                     179 KB
  % of file size:            0.003%

Indexes:
  idx_ncuc_discovery_hash    ON ncuc_discovery_records(content_hash)
  idx_documents_hash         ON documents(content_hash)

Query Performance:
  Single lookup:             0.03 ms
  1,000 lookups:             34.58 ms
  Batch (100):               2.52 ms
  Find all duplicates:       0.49 ms
```

---

## Best Practices

DO:
- Store checksums in database (current approach)
- Query using indexed columns
- Use batch operations for many files
- Store immediately after download

DON'T:
- Store as separate .sha256 files
- Hardcode hashes in source code
- Recalculate checksums repeatedly
- Forget to store checksum

---

## Maintenance Commands

```bash
# Check for duplicates
python scripts/maintenance/deduplicate_downloads.py --check

# Calculate missing checksums
python scripts/maintenance/deduplicate_downloads.py --fix

# Mark duplicate records
python scripts/maintenance/deduplicate_downloads.py --remove
```

---

## Bottom Line

**Checksums are stored in the SQLite database because:**
- Fastest lookup: 0.03 ms (300-1000x faster than alternatives)
- Most efficient storage: 179 KB (negligible)
- Best query capability: Can find duplicates across tables
- Complete metadata: Link to docket, title, date, family
- Perfect for deduplication: Exactly what we need

**This is the optimal implementation.**

---

For more details:
- `docs/CHECKSUM_STORAGE_ARCHITECTURE.md` — Deep dive
- `docs/CHECKSUM_DATABASE_REFERENCE.md` — Technical reference
- `docs/DUPLICATE_DETECTION_GUIDE.md` — Usage guide
