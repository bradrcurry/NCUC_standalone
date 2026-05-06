# Historical Parser Architecture

## Current State

The codebase already has two parsing styles:

1. Company-specific current-tariff parsers
   - `parse/nc_progress.py`
   - `parse/nc_carolinas.py`
   - other state/company parsers

2. Generic historical NCUC extraction
   - `historical/ncuc/pipeline/rate_extractor.py`
   - `historical/ncuc/pipeline/bulk_extractor.py`

The historical side has been the main source of parser drift. It currently uses
one mostly generic residential extractor and accumulates special cases over time.

## Target Direction

Historical parsing should be split into parser profiles.

Each profile answers:
- What document family does this apply to?
- What company lineage does this apply to?
- What structural assumptions does it make?
- How should charges be extracted?

## Parser Profile Axes

Profiles should vary along these dimensions:

1. Company lineage
   - `progress`
   - `carolinas`
   - legacy `duke_power` / `cp&l` era, mapped to canonical company where appropriate

2. Document structure
   - single-leaf tariff sheet
   - multi-leaf compliance tariff book span
   - rider summary / adjustment matrix
   - procedural order with embedded tariff span

3. Era / format generation
   - modern utility PDF
   - post-merger but pre-modern NCUC filings
   - older scanned / OCR-derived rate sheets

4. Tariff family cluster
   - residential flat / block
   - residential TOU / CPP / EV
   - general service demand schedules
   - rider tables / adjustment riders

## Minimum Recommended Profile Set

Start with these profiles before adding more regex to the generic extractor:

1. `progress_residential_flat`
2. `progress_residential_tou`
3. `progress_rider_adjustment_matrix`
4. `carolinas_residential_flat`
5. `carolinas_general_service`
6. `legacy_duke_power_sheet`
7. `generic_residential_fallback`

## Current Implementation

`historical/ncuc/pipeline/parser_profiles.py` now provides a registry-based
selection point. `bulk_extractor.py` selects a profile before extraction.

At the moment:
- `progress_residential_tou` exists as an explicit profile
- `progress_residential_flat` now exists for modern flat residential DEP sheets
- `progress_rider_adjustment_matrix` exists for `Leaf 600` summary tables
- `carolinas_residential_flat` exists for RS-style Carolinas sheets
- `carolinas_rider_adjustment_matrix` exists for Carolinas rider summary tables
- `generic_residential` is now a narrower residential-style fallback, not the unconditional sink for every unsupported leaf-family document
- unsupported documents now land on parser profile `unknown`
- fallback recommendation order is now persisted for historical parses
- the bulk extractor will conservatively try alternate supported profiles only
  when the initially selected profile extracts nothing, or when a weak parse has
  a clearly better fallback result
- “clearly better” now considers coverage/completeness signals in addition to
  raw extracted charge count
- title/family fallbacks now let some known formula-only program leaves skip
  cleanly even when OCR/plaintext is missing, instead of lingering as generic
  empty parses
- extractable incentive program leaves still require real text; profile
  selection should not invent charges from title-only hints

This is intentionally a small first step. The main value is architectural:
future parser work can be added as new profiles instead of expanding one shared
regex file.

## Relationship To OCR

OCR should not create a separate parsing architecture.

Instead:
- triage decides whether OCR is needed
- OCR produces normalized text or page artifacts
- parser profile selection happens after OCR just as it does for native-text PDFs

This keeps the extraction model consistent across:
- native text PDFs
- OCR-derived text from scanned PDFs
- mixed or partially scanned filings

GPU-backed OCR should be treated as an optional backend for difficult documents,
not as a replacement for parser profiles.

## Practical Guidance

When a historical extraction issue is found, decide first whether it is:

1. A family-matching / document-linking problem
2. A document segmentation problem
3. A parser-profile problem

Only parser-profile problems should result in new parsing rules.

If the fix depends on company lineage, era, or document table layout, create or
extend a dedicated profile instead of modifying the generic fallback.
