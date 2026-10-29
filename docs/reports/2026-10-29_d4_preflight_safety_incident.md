# D4 preflight safety incident

2026-10-29, approximately 02:35 America/New_York.

During initial reconnaissance the agent ran the canonical static preflight
documented in `docs/NCUC_PORTAL_WORKING_METHOD.md`:

```
.venv312/Scripts/python.exe -m duke_rates run-continuous-loop-nc --dry-run --max-cycles 0
```

It printed “Dry run — no writes, no portal calls”, but opened the configured
production database through `duke_rates.db.sqlite.connect`, which executes
schema DDL and `migrate()` even for a read-oriented operation. This violated
the user's production-write boundary. The agent disclosed it promptly and
stopped using this preflight. No restoration or further write was attempted.

## Measured evidence

- File: `data/db/duke_rates.db`, 5,845,692,416 bytes, size unchanged.
- Before mtime: 2026-10-26 12:36:11.791924300 -0400.
- After mtime: 2026-10-29 02:35:17.771976700 -0400.
- Before SHA-256: `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08`.
- After SHA-256: `0f6f8f3551f21908533019c5f568d49e254fdd0149fa65ceedef9aa37031b6ca`.
- A read-only (`mode=ro`, `query_only=ON`) row-by-row ordered hash comparison
  against the preserved D1 research copy matched all nine checked tables:
  `tariff_charges` (5,198), `tariff_versions` (988), `historical_documents` (682),
  `rider_summary_blocks` (365), `rider_line_items` (8,003),
  `rider_applicability` (1,798), `ncuc_discovery_records` (4,428),
  `documents` (1,709), `document_types` (14).
- Schema comparison: no current-only objects and no different definitions among
  common named objects. D1 has additional provenance/clue objects as expected.
  Current schema cookie is 456; D1's is 592. These are not before/after cookies.
- Broader streaming comparison completed: **all 79 shared application tables
  match the D1 reference row for row**, including large extraction logs and page
  artifacts. The only differing shared table is SQLite's `sqlite_sequence`
  (eight ordered positions); D1 has additional research tables and migrations,
  so sequence differences are not an exact before/after delta. Evidence:
  `reports/phase2e_d4/full_reference_row_audit.json`.

These comparisons establish agreement with a historical research reference,
**not** byte-identical production preservation or an exhaustive before/after
audit. The pre-command whole-file hash is preserved, but there is no exact
pre-command byte snapshot in this session. Do not present this as harmless or
as proof that every production row remained unchanged.

## Containment and follow-up

All further CLI investigations must explicitly set the database to a scratch
copy, or call source routines that avoid the normal migration-capable helper.
The test suite already guards against write-capable production connections in
`tests/conftest.py`; the full baseline suite runs with that guard.

Investigate a regression guard for the zero-cycle dry-run path and correct the
canonical preflight instructions after required GitNexus impact analysis. Do
not restore production or reset filesystem timestamps without separate user
authorization. D4 must not claim its production-hash acceptance gate passed.

Evidence files: `reports/phase2e_d4/production_before.sha256`,
`production_before.stat`, `production_after_preflight.sha256`,
`portal_static_preflight.txt`, `preflight_logical_audit.json`, and
`preflight_schema_comparison.json`.
