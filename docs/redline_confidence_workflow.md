# Redline And Confidence Workflow

**Purpose:** Permanent operator/agent guidance for using the NC confidence, redline, and parsed-redline audits together.

This workflow exists because "did we find the right tariff documents and parse them correctly?" is not answered by charge counts alone.

## Core Rule

- Clean compliance leaves and official tariff sheets remain the canonical source of truth.
- Redlines are evidence and discovery aids.
- Mixed filings can contain both clean and redlined pages, so redline checks must respect `historical_documents.start_page` / `end_page` when possible.

## Main Commands

```bash
python -m duke_rates refresh-nc-redline-fingerprints
python -m duke_rates export nc-confidence-audit
python -m duke_rates export nc-redline-lead-audit
python -m duke_rates export nc-redline-parse-audit
```

## Operator Repair Commands

These are the main operational commands for fixing bounded historical rows without dropping to raw SQL:

```bash
python -m duke_rates rebind-historical-page-range <hd-id> --start-page X --end-page Y --requeue
python -m duke_rates reprocess enqueue-nc --hd-id <hd-id>
python -m duke_rates reprocess enqueue-nc --from-needs-review
python -m duke_rates clear-redline-fingerprint --hd-id <hd-id> --force
python -m duke_rates retire-tariff-version --version-id <version-id> --execute
```

Use them when:
- a parsed-redline review case is really a bad `start_page` / `end_page` binding
- a newly bounded or newly registered historical row needs direct queueing
- a needs-review backlog slice should be requeued intentionally instead of piggybacking on a direct `--hd-id` rerun
- a stored redline verdict is stale after detector improvements
- a redline-backed tariff version should be retired while keeping the historical PDF row

## What Each Report Answers

### `export nc-confidence-audit`

Backed by:
- [nc_confidence_audit.py](/c:/Python/Duke/Standalone/src/duke_rates/analytics/nc_confidence_audit.py)

Use it for:
- family-level completeness confidence
- continuity and gap pressure
- parse anomaly pressure
- quality-tier mix
- whether redline evidence is corroborated by clean companions

Primary outputs:
- [nc_confidence_audit.md](/c:/Python/Duke/Standalone/docs/reports/nc_confidence_audit/nc_confidence_audit.md)
- `confidence_score`
- `confidence_tier`
- `recommended_action`

### `export nc-redline-lead-audit`

Backed by:
- [nc_redline_lead_audit.py](/c:/Python/Duke/Standalone/src/duke_rates/analytics/nc_redline_lead_audit.py)

Use it for:
- ranking families where redline clues can help find clean companions
- surfacing docket numbers, leaf numbers, filing dates, page-level revised/superseding leaf clues, and search hints

Primary outputs:
- [nc_redline_lead_audit.md](/c:/Python/Duke/Standalone/docs/reports/nc_redline_lead_audit/nc_redline_lead_audit.md)

### `export nc-redline-parse-audit`

Backed by:
- [nc_redline_parse_audit.py](/c:/Python/Duke/Standalone/src/duke_rates/analytics/nc_redline_parse_audit.py)

Use it for:
- determining whether already parsed versions still depend on redline-backed sources
- separating true redline dependency from stale fingerprint noise
- separating true redline dependency from cases where an exact-date clean companion already exists

Primary outputs:
- [nc_redline_parse_audit.md](/c:/Python/Duke/Standalone/docs/reports/nc_redline_parse_audit/nc_redline_parse_audit.md)

Key action states:
- `review_parsed_redline_version`
  - parsed version still appears to depend on a true redline-backed page slice
- `prefer_clean_companion_version`
  - a non-redline exact-date companion with extracted charges already exists; prefer that version operationally
- `leave_redline_unparsed_or_find_clean_companion`
  - redline evidence exists but no extracted charges; usually a discovery task
- `likely_ok`
  - no active redline conflict detected

For the lead audit specifically, the important action states are:
- `use_redline_clues_to_find_clean_companions`
  - unpaired redlines exist and the bounded page slice exposes searchable before/after clues
- `compare_redlines_against_clean_versions`
  - clue-rich redlines exist, but they are already corroborated and are best used to validate clean versions rather than hunt missing ones
- `link_redlines_to_clean_companions`
  - redline evidence exists but the slice did not yield enough structured clues yet

## Important Nuance

Path-level PDF scoring is not enough.

Mixed compliance filings may contain:
- a clean page
- a redlined page
- both for the same leaf in the same PDF

That is why the parsed-redline audit now uses page-bounded historical slices when evaluating redline status.
The stored fingerprint layer now does the same: `document_fingerprints` can carry slice-specific redline verdicts keyed by `source_pdf`, `page_start`, and `page_end`, plus detector evidence (`signals`, red text samples, strikethrough samples, detector version).

Example:
- `nc-progress-leaf-611` was initially over-flagged because the PDF path contained both clean and redlined pages.
- The actual linked historical row was already page-bounded to the clean page.

## When To Hunt New Documents vs Repair Existing Ones

Hunt new clean documents when:
- `export nc-redline-lead-audit` shows clue-bearing redlines with no clean companion
- `export nc-redline-parse-audit` shows `review_parsed_redline_version` and no clean companion
- `export nc-confidence-audit` still shows continuity gaps or unpaired redlines

Repair existing rows when:
- the clean PDF is already on disk
- the exact-date version already exists
- the row has missing or wrong `start_page` / `end_page`
- the clean version is present but `charge_count = 0`

Recent example:
- [repair_dec_eb_clean_versions.py](/c:/Python/Duke/Standalone/scripts/ingestion/repair_dec_eb_clean_versions.py) repaired exact-date clean DEC Rider EB versions already on disk and re-extracted them instead of re-downloading anything.

## Suggested Triage Order

1. Run `show-workflow-status-nc`.
2. Refresh redline fingerprints.
3. Run the confidence audit.
4. Run the redline lead audit.
5. Run the redline parse audit.
6. For any `prefer_clean_companion_version`, prefer the clean exact-date source first.
7. For remaining `review_parsed_redline_version`, inspect whether the PDF is mixed clean/redline and whether the page bounds are too broad.
8. Only then do new docket hunting for unresolved families.

## Clean-Companion Intake Note

`scripts/ingestion/close_gaps_from_redline_discovery.py` exists and is still useful as a targeted historical cleanup script, but it is not yet the canonical generic intake path for "I found a clean companion, register this PDF slice."

Current recommendation:
- use the CLI repair commands above for bounded-row fixes and version cleanup
- use the existing targeted registration scripts when a clean companion needs a new historical row
- treat `close_gaps_from_redline_discovery.py` as a tiered legacy helper until it is generalized around explicit PDF+page-range registration inputs

## Current State (2026-04-15)

Latest generated counts after the slice-aware refresh:
- confidence audit tiers: `high=53`, `medium=39`, `low=60`, `weak=21`
- confidence audit actions: `search_for_missing_clean_tariffs=97`, `backfill_zero_charge_versions=30`, `inspect_profile_and_reparse=21`, `link_redlines_to_clean_companions=9`, `likely_ok=16`
- redline lead audit: `family_count=46`, `families_with_actionable_clues=34`, `use_redline_clues_to_find_clean_companions=15`
- parsed-redline audit: `prefer_clean_companion_version=13`, `review_parsed_redline_version=5`, `leave_redline_unparsed_or_find_clean_companion=8`, `refresh_stale_false_positive_fingerprint=6`

Remaining parsed-redline review queue:
- `nc-carolinas-rider-RIDEREDIT3`
- `nc-carolinas-rider-STS`
- `nc-progress-leaf-670`
- `nc-progress-leaf-674`
- `nc-progress-leaf-610`

Current process implication:
- coarse redline fingerprinting is no longer the main blocker
- bounded redline slices now produce structured lead clues, so the next material improvement is clue normalization and direct operator follow-through on the new clue-driven queue

## Related Files

- [redline_detector.py](/c:/Python/Duke/Standalone/src/duke_rates/parse/redline_detector.py)
- [redline_page_parser.py](/c:/Python/Duke/Standalone/src/duke_rates/parse/redline_page_parser.py)
- [nc_confidence_audit.py](/c:/Python/Duke/Standalone/src/duke_rates/analytics/nc_confidence_audit.py)
- [nc_redline_lead_audit.py](/c:/Python/Duke/Standalone/src/duke_rates/analytics/nc_redline_lead_audit.py)
- [nc_redline_parse_audit.py](/c:/Python/Duke/Standalone/src/duke_rates/analytics/nc_redline_parse_audit.py)
- [nc_redline_fingerprint_refresh.py](/c:/Python/Duke/Standalone/src/duke_rates/analytics/nc_redline_fingerprint_refresh.py)
