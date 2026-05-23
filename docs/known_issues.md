# Known Issues

**Last reviewed:** 2026-04-08 (four DEC canonicalization passes landed)

## Scope / maturity

- The repo-wide vision is still broader than the currently mature implementation.
- The strongest path is North Carolina residential:
  - DEP `RES`
  - DEC `RS`
- Other states and many non-residential schedules still exist mostly as crawl/archive/parser scaffolding rather than validated analytical products.

## NC residential data caveats

- DEP pre-`2023-10-01` rider history is provisional.
  - It is persisted in `dep_provisional_rider_components` and surfaced via
    `load_dep_res_canonical_rider_components()` with `source_kind = "provisional_ingest"`.
  - Suitable for analysis and charting, but lower confidence than the clean `Leaf 600` path.
  - Trust scores for provisional rows are `medium` (0.45–0.65); clean rows typically reach `high` (0.80–1.00).

- Raw summary tables may still contain multiple source rows for the same effective date.
  - `rider_summary_blocks` has a DB-enforced UNIQUE index on `(docket_dir, source_pdf, rate_class, effective_date)`.
  - Multiple rows for the same `effective_date` from different source PDFs are intentional (several PDFs filed for the same date).
  - The canonical analytics functions dedup to one row per effective date.

## Rider-date caveats

- Component-level rider dates (`rider_effective_date`) are expected to differ from the sheet-level `effective_date`.
- The `dep_provisional_rider_components` DDL now documents this distinction with inline SQL comments.
- Current DEP rider-date audit result:
  - `97.0%` overall component-date completeness
  - `99.0%` excluding aggregate `BA` rows

## Billing caveats

- Current saved-bill validation is strong:
  - `12/12 good_match`
  - max absolute delta `$0.45`
- Remaining differences are small summary-rider rounding / aggregation differences, not major tariff-selection failures.
- The billing engine is now good for the validated NC residential path.
- It is still not a universal all-states billing engine.
- Storm rider mid-period proration uses a linear day-fraction of `monthly_kwh` (approximation).
  - Actual Duke billing uses meter reads at the rate-change date.
  - Empirical error ≤ $0.45 on validated bills.
  - `BillEstimate.notes` surfaces this when proration fires.

## TOU caveats

- DEP `R-TOU-CPP` parsing and bill validation are now usable.
- Duke-recognized holidays (New Year's, Memorial Day, Independence Day, Labor Day, Thanksgiving, Christmas) are treated as off-peak all day, matching Duke's billing practice.
  - Saturday→Friday and Sunday→Monday observed-shift rules implemented.
  - Holiday calendar in `src/duke_rates/billing/holidays.py`.
- The current reconciliation path uses billed TOU period quantities for validation when appropriate.
- Still narrower than a fully generalized interval-native TOU engine for all schedules.

## Storm rider caveats

- Storm rider logic is materially improved and works in current reconciliation.
- DEP storm family state is now much cleaner:
  - `leaf-607 / STS` is canonical and charged from `2021-12-01` through `2026-01-01`
  - `leaf-613 / STS-2` is canonical and charged from `2025-11-01`
  - `doc-STORMRECOVERYRIDER` should be treated as a legacy duplicate family, not as canonical storm history
- Stacked storm rider handling and mid-bill proration are implemented in reconciliation.
- Historical storm support is now substantially better for DEP from `2021-12-01` forward.
- The remaining historical storm question is narrow:
  - [dep_storm_history_inventory.md](/c:/Python/Duke/Standalone/docs/reports/dep_storm_history_inventory/dep_storm_history_inventory.md)
    found no confirmed pre-2021 storm leaf in the reviewed older docket set
  - `E-2 Sub 1204` remains one bundle candidate worth manual inspection before concluding that older storm-leaf history is absent
- Storm leaf identification uses `DocumentParseResult.leaf_no` (structured field) first, then
  a regex heuristic on `raw_text_path` + rider title as fallback. The fallback emits a `log.WARNING`.

## Streamlit / app caveats

- The comparison app should load from the cached canonical CSV by default.
- Rebuilding the full canonical timeline from SQLite on every page load is too slow for practical interactive use.
- If the app feels slow again, check whether it is reading the cached CSV or recomputing from DB.

## NCUC / portal caveats

- NCUC portal search and document retrieval work, including authenticated NCID flows.
- Public search remains noisy and is best treated as a fallback or reconnaissance tool.
- Structured portal search is useful, but not always better for very narrow schedule-specific hunts.
- The extraction pipeline now segments large multi-leaf compliance books into bounded spans, resolving previous whole-document false positive issues.
- The large authenticated-portal harvest wave is no longer the main bottleneck.
  The current bottleneck is intake quality after import:
  stale stage debt, provisional `doc-*` / `program-*` families, null-effective rows,
  and canonicalization of misclassified historical spans.
- `retire-provisional-garbage-nc --execute` is now a standard session-start command.
  It safely bulk-retires provisional families with no charged content.
  Families with real charge rows are always preserved. Run `--dry-run` first to preview.
  351 families were retired on 2026-04-08; `provisional_families` is now `0` in live workflow metrics.
- We still do not persist enough intermediate evidence from every downloaded document.
  - Many PDFs likely contain additional useful data beyond the currently extracted rows.
  - Future work should preserve richer mined artifacts so already-downloaded files can be re-analyzed without rediscovery.
  - An initial document-intelligence layer is now live and should be used for the next pass:
    [document_intelligence_architecture.md](/c:/Python/Duke/Standalone/docs/document_intelligence_architecture.md)
    and
    [nc_document_intelligence_audit.md](/c:/Python/Duke/Standalone/docs/reports/nc_document_intelligence_audit/nc_document_intelligence_audit.md)
  - First operational follow-through is now in place:
    `canonicalize-historical-family-key` provides a reusable way to move malformed historical families
    into canonical keys without one-off DB edits.

## Documentation / handoff caveats

- Older notes in the repo may still describe the project as more "early-stage" than the current NC residential implementation really is.
- Another model should treat the processed exports and validation reports as the fastest way to understand present state.
- `tariff_families` / `tariff_versions` / `tariff_charges` ARE the active Phase 4a billing path — not reserved or unused. See `docs/architecture.md` for the two-path explanation.

## tariff_charges data quality issues (identified 2026-04-02, updated 2026-04-08)

Confirmed parsing bugs and coverage gaps in the DB. Active follow-up lives in
[technical_debt.md](technical_debt.md); completed DQ ticket history lives in
[technical_debt_archive_2026_04_07.md](/c:/Python/Duke/Standalone/docs/technical_debt_archive_2026_04_07.md).

| ID | Description | Bad rows | Status | Impact |
|----|-------------|----------|--------|--------|
| TD-DQ-001 | `-1.0 $/kWh` phantom rider adjustments in 7 Carolinas schedule families | 348 | **DONE 2026-04-03** | — |
| TD-DQ-002 | `$1.00/kW` phantom demand charge in WC/OPTE/WCR families | 111 | **DONE 2026-04-03** | — |
| TD-DQ-003 | BPM Prospective Rider PDF-header text parsed as rate labels | 12,466 | **DONE 2026-04-02** | — |
| TD-DQ-004 | leaf-501 v5302 runaway extractor (1,722 identical rows) | 1,722 | **DONE 2026-04-03** (v5302 now empty; re-extract pending span-narrowing) | — |
| TD-DQ-005 | Unparsed date strings in `tariff_versions.effective_start` | 48 | **DONE 2026-04-02** | — |
| TD-DQ-006 | DEC 2013 SGS/I/PG/TS: 250 charges each from bundle cross-contamination | ~750 | **DONE 2026-04-06** | — |
| TD-DQ-007 | DEP 2015–2022 compliance bundles: energy block tiers not extracted | ~49 versions | **PARTIAL 2026-04-06** | Low — energy unit rates now present; single-phase billing usable |
| TD-DQ-008 | DEC historical base-rate coverage gap (later resolved by 2013/2014/2015/2016/2018 bundle registration) | Structural | **DONE 2026-04-06** (2013/2014/2015/2016/2018 bundles all registered; no rate case 2017) | — |
| TD-DQ-009 | nc-carolinas-rider-PIM: 18 rows at `1e-06 $/kWh` (parse artifact) | 19 | **DONE 2026-04-06** | — |
| TD-DQ-NEW-001 | DEC 2021 SGS/LGS/I/PG: 78–624 charges each from runaway extractor | ~1,800 | **DONE 2026-04-06** | — |
| TD-DQ-NEW-002 | DEC RS: duplicate charges and incorrect season labels in all versions | 16+5+2 dupes | **DONE 2026-04-06** | — |
| TD-DQ-NEW-003 | DEC RS 2026 utility_current reported as incomplete; confirmed correct flat-rate 2-charge structure | 0 missing | **RESOLVED** — RS schedule is flat-rate; 2 charges (BFC + flat energy) is correct for utility_current | — |
| TD-DQ-NEW-004 | DEC SGS/LGS 2018-12-01 energy tiers missing: `_TIERED_ENERGY_RE` failed when PDF has space before `¢` | 7 missing per version | **DONE 2026-04-08** — added `\s*` before cent character class in `nc_carolinas.py`; both versions now extract 11 charges | — |

## Historical coverage gaps (identified 2026-04-02, updated 2026-04-08)

Full gap analysis with quality heatmaps: `docs/reports/GAP_ANALYSIS_REPORT_2026_04_06.md`
Regenerated DB-driven matrix export: `python -m duke_rates export-nc-coverage-assessment`
Outputs land in `docs/reports/nc_coverage_assessment/`.
Ranked anomaly audit export: `python -m duke_rates export-nc-anomaly-audit`
Outputs land in `docs/reports/nc_anomaly_audit/`.
Schedule inventory audit export: `python -m duke_rates export-nc-schedule-inventory-audit`
Outputs land in `docs/reports/nc_schedule_inventory_audit/`.
Focused leaf-503 audit export: `python -m duke_rates export-dep-leaf-503-audit`
Outputs land in `docs/reports/dep_leaf_503_audit/`.
Use [docs/reports/README.md](/c:/Python/Duke/Standalone/docs/reports/README.md) when navigating older reports; March 2026 session-status snapshots and older analysis/action-plan reports were archived because they contain projected, superseded, or historically scoped status text.

### 10-year bill reconstruction: actual quality assessment

The canonical per-schedule matrix is now generated from SQLite instead of maintained by hand:
- Markdown matrix: [nc_coverage_assessment.md](/c:/Python/Duke/Standalone/docs/reports/nc_coverage_assessment/nc_coverage_assessment.md)
- Cell-level data: [dep_coverage_cells.csv](/c:/Python/Duke/Standalone/docs/reports/nc_coverage_assessment/dep_coverage_cells.csv) and [dec_coverage_cells.csv](/c:/Python/Duke/Standalone/docs/reports/nc_coverage_assessment/dec_coverage_cells.csv)
- Ranked anomaly queue: [nc_anomaly_audit.md](/c:/Python/Duke/Standalone/docs/reports/nc_anomaly_audit/nc_anomaly_audit.md) and [nc_anomaly_audit_rows.csv](/c:/Python/Duke/Standalone/docs/reports/nc_anomaly_audit/nc_anomaly_audit_rows.csv)
- Full schedule inventory: [nc_schedule_inventory_audit.md](/c:/Python/Duke/Standalone/docs/reports/nc_schedule_inventory_audit/nc_schedule_inventory_audit.md) and [nc_schedule_inventory_rows.csv](/c:/Python/Duke/Standalone/docs/reports/nc_schedule_inventory_audit/nc_schedule_inventory_rows.csv)
- Document-intelligence canonicalization queue: [nc_document_intelligence_audit.md](/c:/Python/Duke/Standalone/docs/reports/nc_document_intelligence_audit/nc_document_intelligence_audit.md) and [nc_document_intelligence_audit_rows.csv](/c:/Python/Duke/Standalone/docs/reports/nc_document_intelligence_audit/nc_document_intelligence_audit_rows.csv)
- Regenerate with `python -m duke_rates export-nc-coverage-assessment`
  and `python -m duke_rates export-nc-anomaly-audit`
  and `python -m duke_rates export-nc-schedule-inventory-audit`
  and `python -m duke_rates export-nc-document-intelligence-audit --limit 60`
  and use `python -m duke_rates canonicalize-historical-family-key <source> <target> --all-historical --dry-run`
  before executing canonical family cleanup

Interpretation notes:
- The generated matrix is a calendar-year snapshot as of July 1 for each year, with carry-forward cells shown as `(=YY)`.
- This is intentionally different from older hand-maintained tables that mixed effective-start-year logic with coverage-through-the-year logic.
- Current matrix scope summary (as of 2026-04-08):
  - `99` NC `rate_schedule` families in SQLite
  - `15` currently in the focused billing matrix
  - `23` populated core billing families omitted from that matrix
  - `17` legacy/malformed schedule-family keys still present in SQLite
- Current anomaly audit summary (as of 2026-04-08):
  - `131` versions scanned
  - `56` flagged versions
  - `85` anomaly rows
  - top anomaly types:
    - `weak_latest_parse`: `33`
    - `missing_demand_rows`: `32`
    - `sparse_vs_family_peak`: `16`
- `DEP RES` remains billing-usable across the 2015–2025 window.
- `DEC RS` is billing-usable across all registered years; historical-document-backed versions are correctly 3-charge flat-rate structures and the 2026 `utility_current` row is correctly 2 charges.
- `DEC SGS` / `LGS` / `I` are billing-usable across the full 2014–2026 window.
- `TD-DQ-007` remains the main open historical-coverage caveat: DEP 2015–2022 bundle-era schedules are usable for the validated residential path but still miss richer multi-class and 3-phase detail.

Key historical facts that still matter:
- Effective dates with registered DEC bundles: `2013-11-01`, `2014-07-01`, `2015-01-01`, `2016-01-01`, `2018-12-01`, `2021-12-16`, `2026-01-01`
- 2017 DEC gap explanation: no rate case between 2014 and 2018; Sub 1129 (Aug 2017) is only a Fuel Rider, so the 2016-01-01 tariff remained in effect through 2018-11-30
- `TD-DQ-006` fixed 2013 DEC contamination
- `TD-DQ-NEW-001` fixed 2021 DEC contamination
- `TD-DQ-008` closed the 2014/2015/2016/2018 historical DEC coverage gap

**What is well-covered (no action needed):**
- DEP residential (RES): billing-usable for all of 2015–2025 (10-year window complete).
- DEP TOU/commercial 2023–2025: fully extracted.
- DEC `RS`: billing-usable for all registered years; 3 charges is the correct historical structure and 2 charges is the correct 2026 `utility_current` structure.
- DEC `SGS` / `LGS` / `I`: billing-usable across the full 2014–2026 window.
- DEP Rider/adjustment history: main riders present.
- R-TOU-CPP (leaf-503): all 3 versions from Mar 2022 onward fully extracted.
- DEP residential compliance-bundle rider families `604/605/608/609/610/611` are now healthy in the dedicated compliance audit.
- DEP storm riders are now in a clean operational state:
  - `leaf-607 / STS` is healthy and canonical from `2021-12-01`
  - `leaf-613 / STS-2` is healthy and canonical from `2025-11-01`

Recent resolved DEC items:
- `TD-DQ-006`: 2013 contamination removed.
- `TD-DQ-008`: 2014/2015/2016/2018 bundle coverage registered; 2017 confirmed as a no-rate-case year.
- `TD-DQ-NEW-001`: 2021 `SGS` / `LGS` / `I` / `PG` contamination removed with bounded page extraction.
- `TD-DQ-NEW-002` and `TD-DQ-NEW-003`: `RS` duplicate-charge concerns were resolved; `RS` is a flat-rate schedule.

Open coverage caveats still worth tracking:
- `TD-DQ-007`: DEP 2015–2022 compliance bundle versions remain partial. Energy rates are present and billing-usable for the validated residential path, but multi-class and 3-phase detail is still missing.
- DEP residential rider queue is no longer a broad blocker.
  The current action queue shows only 1 low-priority item:
  `leaf-602` on `R-TOU-EV`.
- The bigger operational issue is now NC historical intake quality:
  the 17 remaining `doc-*` families need canonical key promotion (not deletion).
  `provisional_families` is now `0`.
- The new document-intelligence audit is the better starting queue for this work:
  - `29` current `canonicalize_family_key`
  - `20` `inspect_and_reparse`
  - first canonicalization pass already completed:
    `SCHEDULESGSMALLGENERALSERVICE -> nc-carolinas-schedule-SGS`
  - second canonicalization pass already completed:
    `SCHEDULEYLYARDLIGHTINGSERVICE -> nc-carolinas-schedule-YL`
  - third canonicalization pass already completed:
    `FUELCOSTADJRDR -> nc-carolinas-rider-FCAR`
  - fourth canonicalization pass already completed:
    `SCHEDULEFLFLOODLIGHTINGSERVICE -> nc-carolinas-schedule-FL`
  - reusable duplicate cleanup is now available:
    `deduplicate-tariff-charges` was used to reduce `FL` version duplicates
    (`4410: 529 -> 19`, `4344: 36 -> 18`, `4395: 29 -> 20`)
  - strongest DEC candidates now include `GOVERNMENTALLIGHTINGSERVICE` and
    `SCHEDULEPLSTREETANDPUBLICLIGHTINGSERVICE`, followed by the zero-charge rider cluster
    (`PM`, `EDPR`, `EE`, `FCAR`, `CEI`, `ER`, `ESM`, `GS`, `NM`, `PROSPECTIVERIDER`, `SSR`, `US`)

## High-value remaining work

**Immediate:**
- Use queue-driven workflow summaries and the anomaly/inventory audits for the next cleanup pass:
  - `show-workflow-status-nc`
  - `reprocess show-stale-historical-nc`
  - `reprocess show-queue-nc`
  - `parse-review-summary`
- Clean the remaining high-signal zero-charge historical rows from the late-session stale pass.
- Decide whether `TD-DQ-007` remains an active debt item or should be downgraded to a known partial-coverage caveat.
- Expand DEC bill validation beyond `RS` after the current intake/canonicalization backlog is reduced.

**DEP rider audit state:**
- [dep_residential_rider_action_queue.md](/c:/Python/Duke/Standalone/docs/reports/dep_residential_rider_action_queue/dep_residential_rider_action_queue.md)
  now shows `1` low-priority action item (`leaf-602` on `R-TOU-EV`).
- [dep_residential_rider_repair_plan.md](/c:/Python/Duke/Standalone/docs/reports/dep_residential_rider_repair_plan/dep_residential_rider_repair_plan.md)
  now shows no repair items.
- [dep_compliance_bundle_audit.md](/c:/Python/Duke/Standalone/docs/reports/dep_compliance_bundle_audit/dep_compliance_bundle_audit.md)
  now shows all six audited DEP residential rider families healthy.
- Treat earlier `leaf-601` / `leaf-602` structural-gap notes as historical context rather than current active blockers for the residential path.

**Medium-term:**
- Extend the canonical rider component path and trust scoring to NC TOU residential and NC small commercial schedules.
- Continue polishing the Streamlit comparison app with trust-tier annotations.
- Decide whether to expand the focused NC matrix beyond its current 15-family scope or keep relying on
  [nc_schedule_inventory_audit.md](/c:/Python/Duke/Standalone/docs/reports/nc_schedule_inventory_audit/nc_schedule_inventory_audit.md)
  for the omitted-family control layer.
- EIA integration deferred items and plant-level backlog are tracked in [technical_debt.md](/c:/Python/Duke/Standalone/docs/technical_debt.md).

**Long-term:**
- Extend the initial document-intelligence layer:
  - richer layout/table normalization
  - broader document-type coverage
  - labeled regression corpus
  - stronger validation / confidence heuristics
  - parser-profile recommendation and fallback logic

## Structural correctness issues — ALL RESOLVED 2026-03-21

All items below were identified in the 2026-03-21 code review and have since been fixed.
See [technical_debt_archive_2026_04_07.md](/c:/Python/Duke/Standalone/docs/technical_debt_archive_2026_04_07.md) for the full completed history.

| Issue | Resolution |
|-------|-----------|
| Duplicate season-matching tables in `engine.py` and `ncuc_loader.py` | Unified in `billing/season_utils.py` (TD-003) |
| No utility/company discriminator in `ncuc_ingest_segments` / `rider_summary_blocks` | `utility TEXT` column added, all rows backfilled (TD-001) |
| `rider_summary_blocks` had no DB-enforced uniqueness | UNIQUE index `idx_rider_blocks_unique` added (TD-002) |
| Two independent block-tier energy charge implementations | Unified in `billing/calculators.apply_block_tiers()` (TD-005) |
| Storm rider proration not documented | Docstring + `BillEstimate.notes` flag added (TD-006) |
| `_NOW` module-level constant in `ncuc_loader.py` | Replaced with inline `datetime.now(UTC)` calls (TD-004) |
| `_component_source_bucket()` relied on file-path string matching | Refactored to use `DocumentParseResult.leaf_no` with regex fallback and `log.WARNING` (TD-007) |
| Unknown season strings silently applied year-round | `season_matches()` now emits `log.warning()` for unknown labels (TD-008) |
| TOU engine did not handle Duke NC holidays | `holidays.py` + `is_duke_holiday()` wired into `tou.py` (TD-012) |
| `dep_provisional_rider_components` dual date columns undocumented | Inline SQL comments added (TD-009) |
| `tariff_families/versions/charges` schema status unclear | Block comment in `schema.py` clarifies both active paths (TD-010) |
| cents/$/kWh unit mismatch in `tariff_engine.py` calculation sites | `_rate_in_dollars()` applied to all calc functions |
| Cascade delete in `repository.py` missed rider-family rows | `DELETE … OR rider_family_key = ?` added to both call sites |
