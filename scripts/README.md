# Scripts Directory

Operational and utility scripts organized by function.

**Rule:** All standalone scripts belong here in subdirectories, not in the repository root.

For a full CLI command reference, see [docs/cli_command_reference.md](../docs/cli_command_reference.md).

---

## Directory Structure

### `scripts/ingestion/`
Download and register documents into the system.

- `download_ncuc_portal_documents.py` — Download NCUC portal documents using authenticated Playwright (uses installed Chrome, NCID credentials from `.env`)
- `download_ncuc_tariffs.py` — Original NCUC tariff downloader (reference implementation)
- `harvest_target_ncuc_documents.py` — Curated DEP/DEC authenticated portal harvest for current high-value docket targets
- `download_with_dedup_example.py` — Example showing deduplication during download
- `register_downloads.py` — Register downloaded documents in database
- `register_ncuc_documents.py` — Register NCUC downloads with metadata
- `register_dec_intermediate_gap_slices.py` — Register and extract confirmed DEC 2019-12-01 / 2020-08-24 intermediate-gap schedule slices from downloaded tariff books
- `register_dep_leaf533_sub1023.py` — Register and extract DEP leaf-533 / LGS-TOU from E-2 Sub 1023 pages 41-44 (effective 2014-06-01)
- `repair_dec_eb_clean_versions.py` — Repair page bounds for clean DEC Rider EB tariff leaves already on disk and re-extract exact-date 2015 / 2022 versions
- `register_findings.py` — Register discovery findings
- `register_remaining_findings.py` — Register additional findings
- `mine_ncuc_pdfs.py` — Mine NCUC PDFs for span/page evidence
- `portal_search_phase.py` — Execute a portal search phase
- `eia_backfill.py` — Backfill EIA state electricity price history
- `eia_incremental_update.py` — Incremental EIA data update

**Targeted portal helper:**
```bash
python scripts/ingestion/download_ncuc_portal_documents.py
```

This is a narrow enhanced-search downloader, not the default sanctioned intake path.

**Note:** Most ingestion workflows are now CLI-first. Prefer:
```bash
python -m duke_rates show-workflow-status-nc
python -m duke_rates ncuc-import-pipeline --all-downloaded
python -m duke_rates eia-backfill --states NC SC VA
```

**Targeted clean-companion repair helper:**
```bash
python scripts/ingestion/repair_dec_eb_clean_versions.py
```

Use this when an exact-date clean tariff PDF already exists on disk but the linked
historical row has bad page bounds or never extracted charges.

### `scripts/discovery/`
Search, scrape, and explore regulatory portals and document sources.

- `search_dep_gaps.py` — Identify missing DEP tariff documents (basic)
- `search_dep_gaps_enhanced.py` — Enhanced search with quality filtering (production workflow)
- `search_dep_gaps_streaming.py` — Streaming search with timeout handling
- `search_dep_remaining.py` — Find remaining gaps after initial search
- `search_ncuc_dockets.py` — Search NCUC docket system
- `search_ncuc_text.py` — Text search in NCUC portal
- `explore_ncuc_search.py` — Explore search functionality interactively
- `explore_ncuc_portal.py` — Portal behavior investigation
- `extract_text_search_files.py` — Extract documents from text search results
- `check_text_search_links.py` — Validate search result links
- `discover_target_dockets.py` — Identify docket numbers for target families
- `scrape_ncuc_tariff_filings.py` — Scrape tariff filing documents from NCUC

**Production discovery workflow:**
```bash
python scripts/discovery/search_dep_gaps_enhanced.py
python scripts/ingestion/download_ncuc_portal_documents.py
```

### `scripts/analysis/`
Analyze, validate, and report on extracted data.

- `analyze_dep_gap_impact.py` — Quantify document gaps and measure improvement from enhanced search
- `fingerprint_docling_artifacts.py` — Fingerprint and analyze Docling extraction artifacts
- `validate_enhanced_search.py` — Validate quality of search results

### `scripts/debug/`
Inspection, troubleshooting, and targeted validation helpers.

**Database Inspection:**
- `inspect_db.py` — Run ad hoc SQLite queries against the DB
- `inspect_db_comparison.py` — Compare two DB states (before/after)
- `run_queries.py` — Run a named set of diagnostic queries
- `inspect_doc_detail.py` — Inspect full detail for one document

**Portal and Network:**
- `investigate_ncuc_portal.py` — Investigate NCUC portal behavior
- `link_enhanced_docs_to_pdfs.py` — Link portal documents to local PDF files

**Data Validation:**
- `check_dec_riders.py` — Validate DEC rider family data
- `check_new_charges.py` — Quick charge count by family after extraction
- `check_nptc.py` — Check NPTC data
- `check_page_artifacts.py` — Check for stale page artifacts
- `check_sts_riders.py` — Check STS rider data
- `final_charge_summary.py` — Full summary of extracted charges by family
- `show_downloads.py` — List downloaded documents and status
- `analyze_dates.py` — Analyze effective date distribution
- `explore_coverage.py` / `explore_coverage2.py` — Explore version and charge coverage
- `debug_triage.py` — Triage unclear pipeline issues

**Repairs:**
- `fix_filing_classification.py` — Fix misclassified filing types
- `get_doc_download_links.py` — Extract download links from discovery records
- `read_dec_riders.py` — Read DEC rider data from raw sources
- `test_dec_riders.py` — Test DEC rider parsing logic

### `scripts/exports/`
Export tariff and analysis data in various formats.

- `export_dep_res_history.py` — Export DEP residential rate history
- `export_dec_rs_history.py` — Export DEC residential rate history
- `export_canonical_residential_timeline.py` — Export canonical residential timeline
- `export_bill_validation_summary.py` — Export bill validation results
- `export_bill_rider_date_audit.py` — Export rider date audit
- `export_actual_bill_accuracy.py` — Export bill accuracy metrics
- `export_dep_rider_date_audit.py` — Export DEP rider date audit

### `scripts/maintenance/`
Database and system maintenance tasks.

- `audit_historical_family_mismatches.py` — Find inconsistencies in family assignments. Detects docs where family_key inference from path disagrees with explicit assignment.
- `audit_stranded_ncuc_family_clues.py` — Compatibility wrapper around `python -m duke_rates suggest-family-links-nc`
- `check_docs.py` — Verify document availability and accessibility
- `verify_pdfs.py` — Verify local PDF files are readable and complete
- `deduplicate_downloads.py` — Remove duplicate downloaded files by hash
- `repair_ncuc_company_mismatches.py` — Repair incorrect company assignments on NCUC records
- `fix_cents.py` — Fix rounding/cents errors in extracted charge values
- `fix_math.py` — Fix calculation errors in derived billing fields
- `run_full_extraction_pipeline.py` — Legacy orchestration for an older Docling/HQ ingest flow; not the default page-aware workflow
- `run_phase2_extraction.py` — Legacy phase-2 orchestration for the older `ingest-ncuc` path; not the default page-aware workflow

**Most useful maintenance commands for agents:**
```bash
# Find and fix stranded discovery records (run before extraction)
python -m duke_rates suggest-family-links-nc --limit 50
python -m duke_rates suggest-family-links-nc --apply

# Find family assignment inconsistencies
python -m duke_rates show-lineage-gaps-nc
python scripts/maintenance/audit_historical_family_mismatches.py
```

### Legacy / Narrow Helpers

- `scripts/debug/get_edpr_edit4_docs.py` — Narrow investigation helper for a specific EDPR/EDIT4 document hunt
- `scripts/maintenance/run_full_extraction_pipeline.py` — Legacy orchestration wrapper around the older `ingest-ncuc` path
- `scripts/maintenance/run_phase2_extraction.py` — Legacy extraction wrapper around the older `ingest-ncuc` path

These helpers are not the default operator path. Prefer the CLI and canonical workflow docs first.

---

## Common Workflows

### Import and Extract (Standard Session)

```bash
# 1. Import any pending downloads
python -m duke_rates ncuc-import-pipeline --all-downloaded

# 2. Bootstrap docs that still lack version links
python -m duke_rates bootstrap-missing-versions-nc

# 3. Extract charges
python -m duke_rates extract-rates-nc

# 4. Review
python -m duke_rates parse-review-summary
```

### Targeted Portal Harvest Intake

Use this when a narrow authenticated-portal harvest writes a custom manifest and
you only want to intake those specific discoveries.

```bash
python scripts/ingestion/register_harvest_manifest.py --manifest data/<targeted_manifest>.json --dry-run
python scripts/ingestion/register_harvest_manifest.py --manifest data/<targeted_manifest>.json
python -m duke_rates ncuc-import-pipeline --record-id <record_id>
```

Do not follow a targeted harvest with `python -m duke_rates ncuc-import-pipeline --all-downloaded`
unless you explicitly want to process the full pending download backlog.

### Find and Fix Stranded Records

```bash
# Check for stranded discovery records with recoverable clues
python scripts/maintenance/audit_stranded_ncuc_family_clues.py --limit 50

# Apply recovered clues
python scripts/maintenance/audit_stranded_ncuc_family_clues.py --apply

# Re-run import to pick up newly linked records
python -m duke_rates ncuc-import-pipeline --all-downloaded
```

### Portal Download (NCUC)

```bash
# 1. Authenticate and download (requires NCID credentials in .env + Chrome installed)
python scripts/ingestion/download_ncuc_portal_documents.py

# 2. Register downloads
python scripts/ingestion/register_downloads.py

# 3. Import
python -m duke_rates ncuc-import-pipeline --all-downloaded
```

### Post-Extraction Validation

```bash
python scripts/debug/check_new_charges.py
python scripts/debug/final_charge_summary.py
python -m duke_rates validate-extraction-nc
```

### Generate Reports

```bash
python scripts/analysis/analyze_dep_gap_impact.py
python scripts/exports/export_dep_res_history.py
python scripts/debug/final_charge_summary.py
```

---

## Adding New Scripts

Before adding a new script, ask:

1. **Is it reusable by operators or future agents?**
   - Yes → Add to appropriate subdirectory with a clear descriptive name
   - No → Keep it locally, delete when done

2. **What function does it serve?**
   - Download/ingest → `scripts/ingestion/`
   - Search/discover → `scripts/discovery/`
   - Analyze/validate → `scripts/analysis/`
   - Inspect/debug/check → `scripts/debug/`
   - Export data → `scripts/exports/`
   - System maintenance/repair → `scripts/maintenance/`

3. **Should it be a CLI command instead?**
   - If it answers a question an operator or agent will ask more than once → promote to CLI
   - Update `src/duke_rates/cli.py` and document in `docs/cli_command_reference.md`

4. **After adding a script:**
   - Update this README with a one-line description
   - If it fills a documented tooling gap, update the gap status in `docs/cli_command_reference.md`

---

## Python Version

All scripts require Python 3.12+ (see `pyproject.toml`).

For optional dependencies (Playwright, Docling, OCR):
```bash
pip install -e ".[browser,pdf,ocr,docling,ai,viz,mcp]"
```

**Last Updated:** 2026-04-01
