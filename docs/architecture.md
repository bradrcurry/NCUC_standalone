# Architecture

**Last reviewed:** 2026-03-25

## Overview

The project started as a broad Duke multi-state crawl and recovery platform. The architecture that matters most today is the North Carolina residential stack:

1. Source acquisition
2. Catalog persistence
3. Parsing and normalization
4. Historical base/rider timeline assembly
5. Billing and reconciliation
6. Validation, analytics, and trust scoring
7. Visualization and app layer

That is the path another model should understand first.

## Source Acquisition

The project uses several source classes, kept separate on purpose:

1. Current Duke-hosted documents
- stored under `data/raw/`

2. Historical Duke / manually staged documents
- stored under `data/historical/raw/`

3. NCUC portal and docket downloads
- stored under `data/historical/ncuc/`
- includes authenticated NCID portal workflows

4. Discovery-only leads
- search results
- portal search metadata
- URL archaeology clues
- regulator attachment leads

5. Actual customer bills
- parsed into `bill_statements`
- turned into `bill_component_observations`

The architecture keeps discovery clues separate from authoritative documents. A search hit is not silently treated as tariff coverage.

## Persistence Layer

SQLite at [data/db/duke_rates.db](c:/Python/Duke/Standalone/data/db/duke_rates.db) is the operational source of truth.

### Two active data paths

The DB contains two active billing paths. Do not confuse them:

**Path A — Generalized multi-state tariff schema (Phase 4a)**
Used by `src/duke_rates/billing/tariff_engine.py` and the `calculate-bill` / `compare-tariff-rates` CLI commands.
Populated by the multi-state PDF parsers (`src/duke_rates/parse/`).

| Table | Purpose |
|-------|---------|
| `tariff_families` | One row per rate schedule / rider across all 7 state/company combos (554 rows) |
| `tariff_versions` | Versioned snapshots of each family (554 rows) |
| `tariff_charges` | Individual charge records: energy, demand, fixed, TOU, adjustment (633 rows) |
| `rider_applicability` | Which riders apply to which base schedules (1,242 links) |

**Path B — NC residential DEP/DEC-specific analytics path (legacy)**
Used by `src/duke_rates/db/ncuc_loader.py`, analytics functions in `src/duke_rates/analytics/`, and the Streamlit apps.
Populated by NCUC docket ingest.

| Table | Purpose |
|-------|---------|
| `ncuc_ingest_segments` | Raw parsed rate segments from DEP/DEC NCUC filings (3,394 rows) |
| `rider_summary_blocks` | Normalized DEP/DEC rider-summary sheets (267 rows, `utility` column set) |
| `rider_line_items` | Per-component rows within summary sheets, with `rider_code` and `line_effective_date` |
| `dep_provisional_rider_totals` | Provisional DEP pre-2023 rider totals (12 rows) |
| `dep_provisional_rider_components` | Provisional DEP pre-2023 per-component rows (32 rows) |

Both paths are operational and serve different purposes. The `utility` column on Path B tables (added 2026-03-21) prevents cross-utility contamination.

### Legacy ingest note

Path B still relies in part on a legacy JSON handoff workflow:

1. `ingest-ncuc` parses NCUC PDFs and writes JSON artifacts
2. `load-ncuc-ingest` reads those JSON artifacts into SQLite tables

That workflow is still functional and still feeds active analytics tables, but it
should be treated as a transitional interface rather than the long-term target.

The preferred end state is:
- parse directly into structured results
- write directly to SQLite
- emit JSON only when explicitly requested for debugging, audit, or export

### Other key tables

| Table | Purpose |
|-------|---------|
| `documents` | Current Duke-hosted document catalog |
| `historical_documents` | Recovered historical PDFs and staged/manual imports |
| `bill_statements` | Parsed Duke bill storage (12 bills) |
| `bill_component_observations` | Normalized bill-derived component observations |
| `eia_retail_sales` | EIA API v2 retail electricity sales, revenue, price, customers |
| `eia_generation_by_fuel` | EIA annual generation by fuel type |

## Parsing Layer

Parsing is intentionally not one monolith. The current NC path is split into:

- schedule/base-rate parsing
- rider-summary parsing
- rider document parsing
- bill parsing
- heuristic normalization

Important parser families:

- [schedule_parser.py](c:/Python/Duke/Standalone/src/duke_rates/parse/schedule_parser.py)
- [rider_summary.py](c:/Python/Duke/Standalone/src/duke_rates/parse/rider_summary.py)
- [rider_parser.py](c:/Python/Duke/Standalone/src/duke_rates/parse/rider_parser.py)
- [bill_parser.py](c:/Python/Duke/Standalone/src/duke_rates/parse/bill_parser.py)
- [heuristics.py](c:/Python/Duke/Standalone/src/duke_rates/parse/heuristics.py)

### Historical Pipeline Parsing (Page-Aware)

The historical pipeline at `src/duke_rates/historical/ncuc/pipeline/` uses a staged, page-aware extraction architecture (added 2026-03-25):
- **Stage A (Triage)**: PyMuPDF for fast textual density and OCR-routing heuristics.
- **Stage B & C (Page Miner & Segmenter)**: `pdfplumber` extraction that segments large multi-leaf compliance books into explicit, bounded `TariffSpan` objects.
- **Stage D (Family Matcher)**: Multi-evidence scoring isolating schedules from ambiguous procedural noise.
- **Stage E (Metadata)**: Layered regex date extraction from bounded header and footer snippets.

The next intended layer is document intelligence:
- persistent document and span fingerprints
- parser-profile selection informed by those fingerprints
- parse-attempt logging and review outcomes
- retention of richer evidence from already-downloaded documents so the same PDFs can be re-mined later without starting from zero
- optional structured document-conversion artifacts (for example from Docling)
  so layout, table, and confidence information can be retained alongside plain
  OCR/text artifacts
- a document-relationship map that links non-tariff filings back to tariff families,
  rider codes, dockets, revisions, time windows, and explanatory evidence topics
- an optional LLM-assisted analysis layer that can explain document purpose,
  enrich fingerprinting, suggest relationships, and identify potential future
  extraction targets

An initial reusable scaffold for that layer now exists in:
- [document_intelligence_architecture.md](/c:/Python/Duke/Standalone/docs/document_intelligence_architecture.md)
- [src/duke_rates/document_intelligence/](/c:/Python/Duke/Standalone/src/duke_rates/document_intelligence)

The first live integration point is the historical bulk extractor, where
document representations, fingerprint results, schema-mapped extractions,
validation summaries, and ML-ready training records are now generated as an
additive sidecar flow without changing charge extraction semantics.

That layer now also includes a pluggable normalization router with:
- native text/page-artifact reuse
- optional Paddle `PP-Structure` OCR/layout normalization
- optional Ollama `glm-ocr` page fallback

The intent is to improve normalization quality for difficult PDFs while keeping
the existing deterministic parser-profile system intact.

That layer is what should allow this pipeline to expand beyond NCUC while still
supporting individualized parsing rules for specific document classes.

The longer-term goal is for this page-aware historical pipeline to bypass the
legacy JSON handoff entirely during normal operation.

Multi-state PDF parsers (Path A, Phase 3):
- [nc_progress.py](c:/Python/Duke/Standalone/src/duke_rates/parse/nc_progress.py)
- [nc_carolinas.py](c:/Python/Duke/Standalone/src/duke_rates/parse/nc_carolinas.py)
- [fl_florida.py](c:/Python/Duke/Standalone/src/duke_rates/parse/fl_florida.py)
- [in_indiana.py](c:/Python/Duke/Standalone/src/duke_rates/parse/in_indiana.py)
- [ky_kentucky.py](c:/Python/Duke/Standalone/src/duke_rates/parse/ky_kentucky.py)
- [oh_ohio.py](c:/Python/Duke/Standalone/src/duke_rates/parse/oh_ohio.py)

### Important design choice

For rider summaries, the top-of-sheet effective date and component-level effective dates are not the same thing.

That is intentional.

Examples:

- `Leaf 600` may be effective `2025-04-01`
- but components inside it can carry dates such as:
  - `12/1/24`
  - `1/1/25`
  - `4/1/25`

The system preserves those component dates in `rider_line_items.line_effective_date` where available.
The `dep_provisional_rider_components` table documents the distinction between its `effective_date`
(sheet-level) and `rider_effective_date` (component-level) with inline SQL comments.

### Planned document-intelligence loop

The longer-term parsing model should be:

1. fingerprint the document or span
2. link it into a relationship map even if it is not yet directly parsed
3. optionally enrich it with LLM-assisted explanation / relationship suggestions
4. choose a parser profile based on features
5. log what was attempted and what failed
6. preserve intermediate evidence, not only final extracted rows
7. use a labeled evaluation set to decide whether to:
   - improve a profile
   - add a new profile
   - adjust routing thresholds
   - add retention for new evidence types

This is the mechanism by which the pipeline can improve over time without relying
on manual memory of past failures.

### Guardrail for LLM use

If an LLM-assisted layer is added, it should be treated as an interpretive and
discovery aid, not the default source of authoritative tariff facts.

Good uses:
- explaining what a filing does
- identifying likely relevance to riders, rates, dockets, and topics
- surfacing documents worth re-digesting later
- proposing parser or extraction opportunities
- using structured document artifacts such as Docling chunks/layout/table
  exports as better LLM input than flat OCR text alone

Bad uses:
- silently writing tariff charges into the DB without evidence
- replacing deterministic parsing where reliable rules already exist
- storing unsupported conclusions without traceable evidence snippets

## Historical Timeline Assembly

The working NC residential timeline is assembled in the analytics layer rather than from a single universal tariff-version graph.

### DEP path

- base history from `ncuc_ingest_segments`
- clean rider history from `rider_summary_blocks` + `rider_line_items` (2023-10+)
- provisional older rider history from `dep_provisional_rider_components` (2016-12 – 2022-12)
- carry-forward logic for periods where the base date has no exact same-day rider filing

Main files:

- [dep_progress.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/dep_progress.py)
- [dep_provisional_riders.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/dep_provisional_riders.py)
- [dep_validation.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/dep_validation.py)
- [dep_rider_date_audit.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/dep_rider_date_audit.py)

### DEC path

- base history from `ncuc_ingest_segments`
- rider history from `rider_summary_blocks` + `rider_line_items` (2018-08+)
- explicit carry-forward coverage surfaced in exports

Main files:

- [dec_carolinas.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/dec_carolinas.py)
- [dec_validation.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/dec_validation.py)

## Canonical Rider Components

A unified per-component DataFrame for DEP and DEC residential riders, with a `source_kind` discriminator:

- `clean_leaf600` — from `rider_line_items` + `rider_summary_blocks` (highest confidence)
- `provisional_ingest` — from `dep_provisional_rider_components` (pre-2023 DEP only)

Main file:

- [canonical_rider_components.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/canonical_rider_components.py)

Functions:

- `load_dep_res_canonical_rider_components()` — DEP RES, 104 rows spanning 2016–2026
- `load_dec_rs_canonical_rider_components()` — DEC RS, 68 rows spanning 2018–2025

## Trust / QA Layer

Rider-family trust scores for DEP and DEC residential schedules. Four scoring dimensions:

| Dimension | Weight | Description |
|-----------|--------|-------------|
| source_quality | 0.40 | `clean_leaf600` = 0.40; `provisional_ingest` = 0.20 |
| date_completeness | 0.25 | `rider_effective_date` populated |
| bill_support | 0.25 | Rider code appears in `rider_line_items` from filed tariff documents |
| continuity | 0.10 | No >6-month gap in rider timeline |

Trust tiers: `high` (≥0.80), `medium` (≥0.50), `low` (≥0.25), `unverified` (<0.25).

Current results (2026-03-21): 147 high, 23 medium, 2 low (SCR, STS — provisional-only).

Main file:

- [rider_trust.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/rider_trust.py)

Functions:

- `load_rider_trust_table()` — full trust-scored DataFrame
- `trust_summary()` — compact QA handoff dict
- `export_rider_trust_table(path)` — CSV export

## Canonical Residential Layer

The main downstream abstraction is the canonical residential timeline:

- [canonical_residential.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/canonical_residential.py)
- [canonical_residential_timeline.csv](c:/Python/Duke/Standalone/data/processed/canonical_residential/canonical_residential_timeline.csv)

This is the stable interface for:

- DEP vs DEC comparison
- charts
- Streamlit app
- exports for external analysis

Key fields include:

- `base_cents_per_kwh`
- `rider_cents_per_kwh`
- `all_in_cents_per_kwh`
- `rider_effective_date`
- `rider_coverage_status`
- `bill_coverage_status`
- `rider_source_kind`
- `rider_quality_flag`

## Billing Architecture

### Path A — Phase 4a generalized tariff engine

- [tariff_engine.py](c:/Python/Duke/Standalone/src/duke_rates/billing/tariff_engine.py) — queries `tariff_charges` + `rider_applicability`; handles all 7 state/company combos
- Exposed via `calculate-bill` and `compare-tariff-rates` CLI commands

All rates stored as dollars (e.g. `0.12623 $/kWh`). The `_rate_in_dollars()` helper normalizes
cents-denominated rates at parse time.

### Path B — NC residential reconciliation engine

Main components:

- [engine.py](c:/Python/Duke/Standalone/src/duke_rates/billing/engine.py)
- [calculators.py](c:/Python/Duke/Standalone/src/duke_rates/billing/calculators.py) — includes `apply_block_tiers()` (shared block-tier logic)
- [riders.py](c:/Python/Duke/Standalone/src/duke_rates/billing/riders.py) — storm leaf identification, proration
- [tou.py](c:/Python/Duke/Standalone/src/duke_rates/billing/tou.py) — TOU period matching with holiday support
- [holidays.py](c:/Python/Duke/Standalone/src/duke_rates/billing/holidays.py) — Duke NC holiday calendar (6 holidays, observed-shift rules)
- [season_utils.py](c:/Python/Duke/Standalone/src/duke_rates/billing/season_utils.py) — unified season matching (shared by engine.py and ncuc_loader.py)
- [reconciliation.py](c:/Python/Duke/Standalone/src/duke_rates/billing/reconciliation.py)
- [observations.py](c:/Python/Duke/Standalone/src/duke_rates/billing/observations.py)

### What it currently does well

- fixed charges and proration for partial billing periods
- block-tier energy charges (shared `apply_block_tiers()` prevents double-counting)
- seasonal rate selection
- TOU period matching with holiday off-peak treatment
- mid-bill base-rate transitions
- rider application from parsed rider docs
- storm rider stacking and effective-date proration (linear day-fraction approximation; documented in `BillEstimate.notes`)
- bill-to-engine reconciliation against parsed Duke bills

### What it still does not try to be

- a universal every-state tariff engine
- a fully generic optimization engine

The strong path is still NC residential.

## Bill Validation Layer

Actual bills are a first-class validation source, not just a side utility.

Main exports:

- [progress_nc_actual_bill_accuracy_summary.json](c:/Python/Duke/Standalone/data/processed/bill_accuracy/progress_nc_actual_bill_accuracy_summary.json)
- [progress_nc_bill_rider_date_audit_summary.json](c:/Python/Duke/Standalone/data/processed/bill_accuracy/progress_nc_bill_rider_date_audit_summary.json)
- [progress_nc_bill_validation_summary.json](c:/Python/Duke/Standalone/data/processed/bill_accuracy/progress_nc_bill_validation_summary.json)

These reports validate:

- base-rate selection
- summary-rider effective dates
- storm rider cadence
- clean energy rider cadence
- bill/component alignment

## Visualization Layer

The dashboard layer is built around cached exports, not live heavy recomputation.

Main files:

- [plotly.py](c:/Python/Duke/Standalone/src/duke_rates/charts/plotly.py)
- [app/streamlit_dep_res_app.py](c:/Python/Duke/Standalone/app/streamlit_dep_res_app.py)
- [app/streamlit_res_comparison_app.py](c:/Python/Duke/Standalone/app/streamlit_res_comparison_app.py)

### Important design choice

The Streamlit comparison app now loads from the cached canonical CSV by default.

That avoids rebuilding the full SQLite-derived timeline on every page load and keeps the UI usable.

## Trust Hierarchy

The practical trust order for current NC residential work is:

1. Duke-hosted current and historical PDFs
2. NCUC docket PDFs and compliance sheets
3. Clean normalized rider-summary blocks in the DB (`clean_leaf600` source kind, trust tier `high`)
4. Provisional rider history persisted in dedicated provisional tables (`provisional_ingest` source kind, trust tier `medium`)
5. Actual customer bills as validation and fallback evidence
6. Search leads / discovery metadata

Use `load_rider_trust_table()` to query trust scores programmatically.

## What Another Model Should Use First

If the task is practical rather than architectural, start here:

1. [README.md](c:/Python/Duke/Standalone/README.md)
2. [canonical_residential.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/canonical_residential.py)
3. [canonical_rider_components.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/canonical_rider_components.py)
4. [rider_trust.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/rider_trust.py)
5. [dep_progress.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/dep_progress.py)
6. [dec_carolinas.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/dec_carolinas.py)
7. [reconciliation.py](c:/Python/Duke/Standalone/src/duke_rates/billing/reconciliation.py)
8. the current processed exports under [data/processed](c:/Python/Duke/Standalone/data/processed)
