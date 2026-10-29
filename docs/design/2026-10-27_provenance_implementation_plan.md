# Implementation plan: immutable extraction results and charge provenance

**Status:** plan for Phase 2; nothing here is implemented. **Date:** 2026-10-27.
**Builds on:** `docs/design/2026-10-26_non_destructive_extraction_proposal.md` (the target schema). This plan narrows that proposal to a minimum viable first cut, orders the work, and adds three requirements the Phase 1B/1C billing investigation exposed.

## 1. What Phase 1B/1C changed about the design

The proposal treats `tariff_charges` as the thing to protect. The billing investigation showed three other paths that need the same treatment:

1. **The 12-bill reconciliation does not read `tariff_charges`.** `ProgressNCHistoricalTariffSelector` prices base rates from `historical_documents.parsed_result_json` and current `documents` parse results. Riders are priced from `rider_line_items` via the leaf-600 summary. The plan therefore versions `parsed_result_json` too. The C1/C2 corrections had to patch JSON in place for exactly this reason.
2. **Metadata rewrites break readers silently.** The regression was a rewrite of `historical_documents.effective_start` to ISO by `normalize-nc-effective-dates`. No tariff value changed. That rewrite left no receipt, so its timing could only be bracketed between two snapshots. Effective dates, bindings (`tariff_versions.historical_document_id`) and version status must be revisioned like charges.
3. **Bill statements are extraction output too.** Bill 12 was mis-parsed, and the stored statement carried no parser version. A re-parse with fixed code silently differed from what was stored. Bill statements need the same run and version fields.

Phase 1C also counted the writers that a schema change has to cover:

| Table | Writers in `src/` | Ad-hoc scripts that write core tables directly |
|---|---|---|
| `tariff_charges` | `Repository` (13 statements), `BulkExtractor` (2), LLM promotion, proposed-tariff promoter, export-audit | 31 scripts under `scripts/` write one or more of `tariff_charges`, `tariff_versions`, `historical_documents`, `rider_*` |
| `historical_documents.parsed_result_json` | `Repository` (4) | |
| effective dates | `cli.py` (normalize), `cli_commands/lineage.py`, `effective_date_remediation.py`, `missing_doc_remediation.py` | |
| `rider_line_items` | `ncuc_loader.py` (2) | |

The 31 scripts are the largest uncontrolled risk. The plan routes them through one write API instead of rewriting them.

## 2. Minimum viable schema (Phase 2a)

These are additive tables only. No existing column is dropped or renamed, and existing readers are untouched in 2a.

```sql
-- One row per run of any producer over one source document.
CREATE TABLE extraction_runs (
  id INTEGER PRIMARY KEY,
  target_kind TEXT NOT NULL,          -- tariff_version | historical_document | bill_statement | rider_block
  target_id INTEGER NOT NULL,
  document_sha256 TEXT NOT NULL,      -- bytes actually read
  producer TEXT NOT NULL,             -- bulk_extractor | parser_profile:<name> | rider_summary | bill_parser | manual | llm | correction:<manifest>
  producer_version TEXT NOT NULL,     -- git sha (+'-dirty') ; 'legacy' for backfilled rows
  code_fingerprint TEXT,              -- sha256 of the producer's source modules
  text_source TEXT,                   -- pdfplumber | pymupdf | docling | ocr_sidecar | ...
  started_at TEXT NOT NULL,
  status TEXT NOT NULL,               -- ok | empty | failed
  metrics_json TEXT
);

-- Immutable output rows of a run (charges). Mirrors tariff_charges columns + provenance.
CREATE TABLE extracted_charges (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES extraction_runs(id),
  version_id INTEGER NOT NULL,
  family_key TEXT NOT NULL,
  charge_type TEXT, charge_label TEXT, rate_value REAL, rate_unit TEXT,
  tier_min REAL, tier_max REAL, tou_period TEXT, season TEXT, customer_class TEXT,
  page_number INTEGER, source_quote TEXT, confidence_score REAL,
  legacy_tariff_charge_id INTEGER     -- backfill link, NULL for new runs
);

-- Immutable JSON outputs (parsed_result_json, bill statement_json) keyed by run.
CREATE TABLE extracted_documents (
  run_id INTEGER PRIMARY KEY REFERENCES extraction_runs(id),
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL
);

-- Which run is live for each target, plus the full promotion history.
CREATE TABLE active_results (
  target_kind TEXT NOT NULL, target_id INTEGER NOT NULL,
  run_id INTEGER NOT NULL REFERENCES extraction_runs(id),
  promoted_at TEXT NOT NULL, promoted_by TEXT NOT NULL, reason TEXT,
  PRIMARY KEY (target_kind, target_id)
);
CREATE TABLE promotion_log (
  id INTEGER PRIMARY KEY, target_kind TEXT, target_id INTEGER,
  from_run_id INTEGER, to_run_id INTEGER, at TEXT, by TEXT, reason TEXT, diff_json TEXT
);

-- Append-only metadata revisions (dates, bindings, status, labels).
CREATE TABLE metadata_revisions (
  id INTEGER PRIMARY KEY,
  table_name TEXT NOT NULL, row_id INTEGER NOT NULL, column_name TEXT NOT NULL,
  old_value TEXT, new_value TEXT,
  changed_at TEXT NOT NULL, changed_by TEXT NOT NULL,   -- command / script / manifest id
  evidence TEXT, reverted_by INTEGER REFERENCES metadata_revisions(id)
);

-- Curated layer: corrections manifests, manual fixes, LLM promotions.
CREATE TABLE charge_overrides (
  id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL,
  action TEXT NOT NULL,               -- add | replace | suppress
  target_extracted_charge_id INTEGER, natural_key TEXT,
  payload_json TEXT, source_kind TEXT NOT NULL,   -- correction | manual | llm
  source_ref TEXT,                    -- e.g. '2026-10-27_billing_integrity:C3'
  evidence_pdf_sha256 TEXT, evidence_page INTEGER, evidence_quote TEXT,
  approved_by TEXT, approved_at TEXT, retired_at TEXT
);

-- Content-addressed source bytes (the files stay where they are).
CREATE TABLE document_blobs (
  sha256 TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL,
  first_seen_path TEXT NOT NULL, first_seen_at TEXT NOT NULL
);
```

Deferred to 2b or later: `text_artifacts` (moving full text out of `metadata_json`), merging `rider_summary_blocks` into `extracted_charges`, and the Docling cache re-key.

**Why `metadata_json` matters operationally.** The 12-bill baseline run read about 450 MB/s from the 5.8 GB DB, and over 100 GB in its first four minutes, because selection repeatedly scans rows carrying full text. Moving full text into `text_artifacts` is the main performance win. It is deferred only because it is not needed for correctness.

## 3. Write path

A single module, `duke_rates/db/provenance.py`, will own every write:

- `start_run(target, sha256, producer, version) -> run_id`
- `record_charges(run_id, rows)`
- `record_document(run_id, payload)`
- `promote(target, run_id, by, reason)`. This validates first; see §5.
- `revise_metadata(table, row_id, column, new_value, by, evidence)`. This writes `metadata_revisions`, then updates the column.
- `add_override(...)`

`producer_version` is `git rev-parse HEAD` plus a dirty flag. `code_fingerprint` hashes the producer's module files, so uncommitted parser edits are distinguishable.

## 4. Read path (compatibility)

The legacy tables stay the read model during migration. They are kept in sync from the active run:

- `tariff_charges` = the active run's `extracted_charges`, with live `charge_overrides` applied. It is materialized into the existing table by `promote()`, not a view at first. That keeps all ~19 reader modules and every live test unchanged.
- `historical_documents.parsed_result_json` and `bill_statements.statement_json` = the active run's `extracted_documents.payload_json`, also materialized on promote.
- After the materialized copies match for a full release, introduce the `tariff_charges_v` view behind a flag, as in the proposal's §5.

This keeps historical billing compatibility by construction: readers see byte-identical rows until a promotion changes them, and every promotion is logged and reversible.

## 5. Approval and rollback

Promotion gates, run on a scratch copy of the DB:

1. **Bill gate.** `tests/test_billing_baseline.py` with `DUKE_RATES_TEST_LIVE_DB`, i.e. the frozen 12-bill reconciliation must reproduce. Any change to a frozen estimate needs a reviewed baseline update in the same change.
2. **Benchmark gate.** `bench/score.py` on the 26-document corpus must not regress value accuracy, precision or TOTAL reconciliation.
3. **Date-contract gate.** Every stored `effective_start` and `effective_end` parses (`test_live_stored_effective_dates_are_readable`).
4. **Diff review.** For runs that change rows on bill-relevant families, the `promotion_log.diff_json` is reviewed by a human.

Rollback is `promote(target, previous_run_id)`. For metadata, it is a revision with `reverted_by`. Corrections manifests become `charge_overrides` with `source_ref`. `apply_corrections.py` receipts map one-to-one onto them.

## 6. Migration (safe order)

All steps run first on a scratch copy, followed by the gates above. Production is migrated only after the scratch run is reviewed and a backup is taken.

| Step | Change | Reversible by | Gate |
|---|---|---|---|
| M1 | Create the new tables (empty). | Drop the tables. | Full test suite. |
| M2 | Backfill. One `legacy` run per `tariff_versions` row, plus copies of `tariff_charges` into `extracted_charges` (`legacy_tariff_charge_id` set). One `legacy` run per non-null `parsed_result_json` and per bill statement. `active_results` points at the legacy runs. `document_blobs` from hashing local files. Rows with `notes` become `charge_overrides(source_kind='manual'/'correction')`. | Delete the backfilled rows; the legacy tables are untouched. | Materializing from the new tables reproduces `tariff_charges`, `parsed_result_json` and `statement_json` byte-for-byte (row count + per-row SHA). |
| M3 | Route `BulkExtractor`, `Repository.replace_*`, LLM promotion and the bill parser import through `provenance.py`. Writes become start_run → record → promote. | Revert the code; data is consistent either way. | The bill, benchmark and date gates. |
| M4 | Route the date and binding writers (normalize-nc-effective-dates, lineage commands, remediation modules) through `revise_metadata`. | Revert the code. | The date gate. |
| M5 | Add a guard that fails any connection-level `UPDATE`/`DELETE` on the legacy tables outside `provenance.py`. This is a SQLite authorizer callback enabled in `Repository`, so the 31 ad-hoc scripts must go through the API or fail loudly. | Disable the authorizer. | Full suite. |
| M6 | Apply the reviewed corrections (C1–C9 and approved P-items) as overrides via the new path. | Retire the override. | All gates. |
| M7 | (2b) `text_artifacts` move, then `VACUUM` on a copy and swap. | Restore from the backup. | All gates, plus size and timing check. |

## 7. Effort and order

M1 and M2, with the byte-for-byte materialization check, are the first PR (about 2–3 days). M3 and M4 are the second; that is where most risk sits, because it touches the extractor and CLI commands. M5 is small but should follow a release of M3 and M4. M6 is a data change and needs explicit approval. M7 is independent.

## 8. Open questions for review

- Should overrides match by `natural_key` only, or also by a stable identity carried across runs? The proposal starts with the natural key.
- Retention of non-promoted runs (forever vs. N days).
- Should the 31 ad-hoc scripts be ported, or archived once M5 lands?
