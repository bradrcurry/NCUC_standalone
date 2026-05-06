# Correct NCUC Docket Fetch Procedure

> **PARTIAL — see [NCUC_PORTAL_WORKING_METHOD.md](NCUC_PORTAL_WORKING_METHOD.md)
> for the canonical end-to-end portal process.** This doc covers ONE specific
> rule (always pass `--docket-number` to `ncuc-docket-fetch`) and is still
> correct for that. The full preflight + login + resolve + fetch flow lives
> in the working-method doc.

**Date:** 2026-04-20  
**Context:** Session 34-35 identified and cleaned up 397 broken discovery records created by `ncuc-docket-fetch` without the `--docket-number` parameter.

## Problem

The `ncuc-docket-fetch` command creates discovery records with metadata populated from the `--docket-number` parameter:

```python
# From src/duke_rates/cli.py
sub_m = re.search(r"Sub\s+(\d+)", docket_number or "", re.I)
sub_number = sub_m.group(1) if sub_m else None

rec = NcucDiscoveryRecord(
    docket_number=docket_number or None,
    sub_number=sub_number,
    ...
)
```

**If `--docket-number` is omitted (defaults to ""):**
- `docket_number` becomes `None`
- `sub_number` becomes `None`
- Discovery records cannot be properly matched to families during import
- This causes broken provisional families and artifacts to accumulate

## Cleanup Applied (Session 35)

- Deleted 397 broken discovery records with NULL docket_number/sub_number
- Deleted 14,697 dependent artifact rows (span, page, docling artifacts)
- Ran `retire-provisional-garbage-nc --execute` to clean up 449 garbage provisional families
- System regression fixed: provisional_families 462 → 14, null_effective_start 433 → 146

## Correct Procedure Going Forward

### 1. Resolve Docket IDs to GUIDs

```bash
python -m duke_rates ncuc-resolve-docket-ids --docket-number "E-2, Sub 1076"
python -m duke_rates ncuc-resolve-docket-ids --docket-number "E-2, Sub 1086"
python -m duke_rates ncuc-resolve-docket-ids --docket-number "E-2, Sub 1094"
# ... etc for each target docket
```

This outputs the GUID for each docket number.

### 2. Fetch WITH Docket Number Parameter

**CRITICAL:** Always include `--docket-number` when fetching.

```bash
# CORRECT: With docket-number metadata
python -m duke_rates ncuc-docket-fetch \
  9b3614b6-11d6-4703-8d18-5e2e2ef3d705 \
  --docket-number "E-2, Sub 1076" \
  --download

python -m duke_rates ncuc-docket-fetch \
  a1234567-89ab-cdef-0123-456789abcdef \
  --docket-number "E-2, Sub 1086" \
  --download

# WRONG: Without docket-number (creates NULL metadata)
python -m duke_rates ncuc-docket-fetch \
  9b3614b6-11d6-4703-8d18-5e2e2ef3d705 \
  --download
```

### 3. Verify Discovery Records Have Metadata

```bash
python -m duke_rates ncuc-list --state NC --limit 20
# Verify docket_number and sub_number are populated
```

### 4. Run Import Pipeline

```bash
python -m duke_rates ncuc-import-pipeline --all-downloaded
python -m duke_rates bootstrap-missing-versions-nc
python -m duke_rates extract-rates-nc
```

## Recommended Workflow Script

For batch fetching multiple dockets:

```bash
#!/bin/bash

# Define dockets to fetch (GUID, human-readable name)
dockets=(
  "9b3614b6-11d6-4703-8d18-5e2e2ef3d705:E-2, Sub 1076"
  "a1234567-89ab-cdef-0123-456789abcdef:E-2, Sub 1086"
  "b2345678-90ab-cdef-0123-456789abcdef:E-2, Sub 1094"
)

for docket_entry in "${dockets[@]}"; do
  guid="${docket_entry%:*}"
  docket_num="${docket_entry#*:}"
  
  echo "Fetching $docket_num (GUID: $guid)"
  python -m duke_rates ncuc-docket-fetch "$guid" \
    --docket-number "$docket_num" \
    --download
done

# After all fetches complete:
python -m duke_rates ncuc-import-pipeline --all-downloaded
python -m duke_rates bootstrap-missing-versions-nc
python -m duke_rates extract-rates-nc
```

## Key Takeaway

**The `--docket-number` parameter is not optional.** It must be provided so that discovery records can be:
1. Properly metadata-tagged
2. Matched to tariff families during import (Step 4 of ncuc_pipeline_overview.md)
3. Successfully integrated into the extraction pipeline

Omitting it results in broken discovery records that waste storage, create garbage provisional families, and block the pipeline.
