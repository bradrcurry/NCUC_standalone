# Proposal: non-destructive, versioned extraction results

**Status:** proposal (not implemented). **Date:** 2026-10-26.
**Context:** Phase 0/1 stabilization added narrow guards against the destructive paths listed below (commit `729034b`). This document proposes the schema and workflow changes that remove the need for those guards. It is meant to be the next persistence milestone, and it should come after the benchmark baseline in `bench/`.

## 1. Problem

Today, every extraction run overwrites the previous run's output in place.

| Path | Behaviour before Phase 0 | Phase 0 guard |
|---|---|---|
| `BulkExtractor.insert_charges` | Ran `DELETE` on every charge for `(version_id, family_key)`, then `INSERT`. Manual and LLM-promoted rows were erased; 55 of 331 promotions survive. | Only rows with empty `notes` that are not in `llm_promoted_charge_audit` are replaced. |
| `Repository.replace_parsed_tariff_data` (`tariff-update --auto-parse`) | Deleted **all** versions of a family, including historical ones. | Raises `HistoricalDataLossError` when historical versions or annotated charges would be lost. |
| Migration TD-002 | Ran on every connection. Deleted duplicate blocks without deleting their line items (1,204 orphans). Grouped NULL dates together, which kept deleting NULL-dated blocks. | Runs once and never deletes a block that has line items. |
| `persist_rider_summary_records(replace=True)` | Deletes and re-inserts the line items of a block. | Blocks the parser marks as ambiguous are no longer stored. The delete and re-insert is unchanged. |
| `docling_artifacts` lookup by path | The newest row for a path was used even if the PDF bytes had changed. | Selects by hash, or by verified content (D3). |

The guards work by refusing operations or narrowing deletes. They do not give us:

- the ability to compare or roll back runs,
- charge-level provenance,
- reproducibility.

## 2. Goals

1. **Every extraction run is retained.** We can answer "what did parser version X extract from document Y" at any time.
2. **Promotion is explicit and reversible.** Billing and analytics read an *active* result set. Promoting a new run and rolling back are pointer flips, not deletes.
3. **Charge-level provenance.** Each charge records its run, page, quote or bbox, text source, and parser version.
4. **Curated rows are first-class.** Manual corrections and LLM promotions are their own result sources. They are never collateral damage of a deterministic rerun.
5. **Content-addressed artifacts.** Documents, text and Docling/OCR artifacts are keyed by SHA-256 of the source bytes plus the conversion options. Full text is stored once.

## 3. Schema sketch

```sql
-- One row per extraction run over one document (deterministic, LLM, manual).
CREATE TABLE extraction_runs (
  id INTEGER PRIMARY KEY,
  historical_document_id INTEGER,
  document_sha256 TEXT NOT NULL,        -- bytes actually read
  text_artifact_id INTEGER,             -- -> text_artifacts.id
  source_kind TEXT NOT NULL,            -- deterministic | llm | manual | sibling_copy
  parser_name TEXT, parser_version TEXT NOT NULL,   -- e.g. git sha + profile name
  code_fingerprint TEXT,                -- hash of parser_profiles.py + rider_summary.py
  started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL,                 -- ok | empty | failed | skipped_*
  metrics_json TEXT                     -- checksum pass rate, charge count, ... (no full text)
);

-- Immutable charges produced by a run.
CREATE TABLE extracted_charges (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES extraction_runs(id),
  version_id INTEGER NOT NULL,
  family_key TEXT NOT NULL,
  charge_type TEXT, charge_label TEXT, rate_value REAL, rate_unit TEXT,
  tier_min REAL, tier_max REAL, tou_period TEXT, season TEXT, customer_class TEXT,
  rate_class TEXT, applicability_json TEXT,
  page_number INTEGER, bbox_json TEXT, source_quote TEXT,
  unit_ambiguous INTEGER DEFAULT 0, checksum_ok INTEGER,
  confidence_score REAL
);

-- Which result set is live for each version (billing reads through this).
CREATE TABLE active_result_sets (
  version_id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES extraction_runs(id),
  promoted_at TEXT NOT NULL, promoted_by TEXT, reason TEXT,
  previous_run_id INTEGER              -- one-step rollback; full history in promotion_log
);
CREATE TABLE promotion_log (... version_id, from_run_id, to_run_id, at, by, reason, diff_json ...);

-- Curated overlays apply on top of the active deterministic set.
CREATE TABLE charge_overrides (
  id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL,
  action TEXT NOT NULL,                 -- add | replace | suppress
  target_extracted_charge_id INTEGER,   -- for replace/suppress (by natural key fallback)
  natural_key TEXT,                     -- (rate_class, charge_type, label, tou, season, tier)
  payload_json TEXT, source_kind TEXT NOT NULL,  -- manual | llm
  evidence_quote TEXT, created_at TEXT, created_by TEXT, retired_at TEXT
);

-- Content-addressed artifacts; full text stored once.
CREATE TABLE text_artifacts (
  id INTEGER PRIMARY KEY,
  document_sha256 TEXT NOT NULL,
  producer TEXT NOT NULL,               -- pdfplumber | pymupdf | docling | tesseract | glm_ocr
  producer_version TEXT NOT NULL,       -- package version, not a constant
  options_hash TEXT NOT NULL,           -- OCR on/off, tables on/off, chunking, page range
  degraded INTEGER NOT NULL DEFAULT 0,
  text_sha256 TEXT NOT NULL, text BLOB, -- zstd-compressed
  UNIQUE (document_sha256, producer, producer_version, options_hash)
);
```

`tariff_charges` becomes a **view**: the active run's `extracted_charges`, with non-retired `charge_overrides` applied. Existing readers keep working during the migration.

```sql
CREATE VIEW tariff_charges_v AS
  SELECT ... FROM extracted_charges ec
  JOIN active_result_sets a ON a.run_id = ec.run_id
  WHERE NOT EXISTS (SELECT 1 FROM charge_overrides o WHERE o.action='suppress' AND ...)
  UNION ALL
  SELECT ... FROM charge_overrides WHERE action IN ('add','replace') AND retired_at IS NULL;
```

Rider summaries follow the same pattern. `rider_summary_blocks` and `rider_line_items` gain `run_id`. Blocks keep `column_layout`, `checksum_ok`, `ambiguous_rate_class` and `warnings_json` from the parser. Ambiguous blocks are stored, so they can be inspected, but they are excluded from the active views.

## 4. Workflow

1. **Extract.** Create an `extraction_runs` row and insert `extracted_charges`. Nothing is deleted.
2. **Validate.** Compute the run metrics: checksum pass rate, unit-range checks, continuity against the previous active run, and a bill-reconciliation delta where one applies.
3. **Promote**, either automatically or by review. Promotion is allowed when the new run is no worse on validation metrics. Otherwise the run is queued for review with a diff against the active run. It writes `active_result_sets` and `promotion_log`.
4. **Rollback.** Point `active_result_sets.run_id` back at `previous_run_id`.
5. **Curate.** Manual fixes and LLM promotions become `charge_overrides`. A deterministic rerun can never touch them. An override whose natural key disappears from the new active run is flagged, not dropped.

## 5. Migration plan (non-destructive)

1. Create the new tables. Backfill one `extraction_runs` row per version, with `source_kind` set to `legacy` and `parser_version` set to `historical_bulk_v2@unknown`, and copy the current `tariff_charges` into `extracted_charges`. Rows with notes, or with an audit link, become `charge_overrides`.
2. Point the readers (billing engine, analytics, Streamlit) at `tariff_charges_v`, behind a flag. Run the 12-bill reconciliation and the benchmark on a scratch copy with the flag on and with it off; results must be identical.
3. Switch the writers (`BulkExtractor`, LLM promotion, manual import) to append-only.
4. Keep the legacy `tariff_charges` table read-only for one release, then retire it.
5. Move `metadata_json.text_metrics.full_text` into `text_artifacts` and keep only references. This is an estimated 2.5 GB saving, which should be verified on a copy first. `VACUUM` runs only on a copy, followed by a swap with a backup in place.

## 6. Parser versioning

- `parser_version` = git commit + dirty flag, plus a `code_fingerprint` (a hash of the profile and parser modules). The constant `historical_bulk_v2` is retired.
- Docling cache key = `document_sha256`, the Docling **package** version, and `options_hash` (OCR, tables, chunking). Degraded conversions are stored with `degraded=1` and are never used to satisfy a full-quality request.

## 7. Open questions

- Granularity of overrides: should matching use the natural key or a stable charge identity across runs? The proposal starts with the natural key, plus a review queue for keys that no longer match.
- Whether `rider_summary_blocks` should be merged into `extracted_charges`, which would give one charge model with rider rows as charges and TOTAL rows as checksum facts.
- Retention: should every run be kept forever, or should non-promoted runs be pruned after N days, keeping metrics only?
