# Phase 0/1 Closeout (Phase 1C)

**Date:** 2026-10-27
**Branch:** `stabilize/phase0-docling-rider-fixes` (local only; not pushed or merged)
**Method:** Production (`data/db/duke_rates.db`) was opened read-only throughout. Its SHA-256 before and after this phase is recorded in §9. Corrections, live tests and the baseline ran on scratch copies made with the SQLite backup API.

---

## 1. Summary

| Definition of done | Status |
|---|---|
| Verified billing baseline | **Done.** 12 bills are frozen in `tests/fixtures/billing_baseline/progress_nc_12_bills.json`. Every bill's actual is its **printed** electric total, cross-checked against the front page. Self-consistency and live-reproduction tests are in `tests/test_billing_baseline.py`. |
| Stable extraction benchmark | **Done (Phase 0).** The v0 corpus has 26 documents (`bench/`), and its raw outputs are now committed (`reports/phase0/*.json`). |
| Reviewed correction manifest | **Done.** C1–C9 are each re-verified against the source PDF, the stored value and the effective date by a reproducible verifier (`scripts/corrections/verify_manifest.py`). Apply and revert reproduce production byte-for-byte. F1–F5 and new findings are in a separate **proposal** manifest (§4). |
| Remaining test failures explained | **Done.** The 7 live-engine failures are classified in §5. Two are genuine engine defects; the rest are data defects and outdated expectations. |
| Non-destructive extraction plan | **Done.** `docs/design/2026-10-27_provenance_implementation_plan.md` gives the minimum viable schema, migration order M1–M7, gates and rollback. |

No production data was modified. No bulk ingestion, new PDF engines or LLM models were used.

## 2. Implemented fixes (this branch, Phase 1B/1C)

| Commit | Fix | Test |
|---|---|---|
| `e7ca035` | `parse_effective_date` reads ISO dates. `_history_version_sort_key` compares naive and aware `retrieved_at` as UTC. This was the root cause of the 7-bill regression. | `test_progress_nc_historical_tariff_selector_reads_iso_stored_dates`, `test_history_version_sort_key_orders_mixed_naive_and_aware_timestamps`, `test_parse_effective_date_reads_every_stored_format` |
| `7dfa902` | The bill parser follows "Billing details – *section* continued" across a page break. This restores the $31.99 dropped from the 2026-03-20 TOU bill. | `test_parse_bill_text_follows_continued_electric_section` (fails without the fix), `…_does_not_duplicate_lines_when_continuation_is_contiguous` |
| `8ba2de0` | `bill_accuracy._comparison_status`: a bill whose "actual" is a sum of parsed lines (no printed total) is `needs_review`, never `good_match`. | `test_line_item_fallback_total_is_never_a_match` |
| `c1acc1c` | Provenance scripts for the June 2026 web-tariff ingest, the provenance repair and the DEC RS 2021–22 backfill are committed. These ran against production but had never been committed. | n/a (dry-run by default) |

Re-parsing the 12 stored bill texts with the fixed parser changes **only bill 12**. Its new parse is identical to correction C9, so C9 can be retired in favour of re-importing the bill. Bills 1–11 parse identically to what is stored.

Earlier Phase 0 commits are listed in `docs/reports/2026-10-27_billing_integrity_investigation.md` §10.

## 3. Billing baseline

| Bill | Rate | kWh | Printed electric total | Front-page electric | Estimate | Δ | Base tariff | Storm / Clean Energy estimate source |
|---|---|---|---|---|---|---|---|---|
| 2025-04-21 | RES | 821 | 121.15 | 121.15 | 121.14 | +0.01 | hd 29 (historical, 2024-10-01) | observed_history / observed_history |
| 2025-05-20 | RES | 1460 | 215.67 | 215.67 | 215.67 | +0.00 | hd 29 (historical, 2024-10-01) | observed_history / observed_history |
| 2025-06-19 | RES | 1316 | 195.94 | 195.94 | 195.94 | +0.00 | hd 29 (historical, 2024-10-01) | observed_history / observed_history |
| 2025-07-21 | RES | 1520 | 223.61 | 223.61 | 223.59 | +0.02 | hd 29 (historical, 2024-10-01) | observed_history / observed_history |
| 2025-08-20 | RES | 1379 | 204.08 | 204.08 | 204.07 | +0.01 | hd 29 (historical, 2024-10-01) | tariff / observed_history |
| 2025-09-19 | RES | 1130 | 170.01 | 170.01 | 170.02 | -0.01 | hd 29 (historical, 2024-10-01) | tariff / observed_history |
| 2025-10-21 | RES | 876 | 136.94 | 136.94 | 136.95 | -0.01 | doc 343 (current, October 1, 2025) | tariff / observed_history |
| 2025-11-19 | RES | 813 | 131.22 | 131.22 | 131.19 | +0.03 | doc 343 (current, October 1, 2025) | tariff / observed_history |
| 2025-12-18 | RES | 1234 | 188.78 | 188.78 | 189.23 | -0.45 | doc 343 (current, October 1, 2025) | tariff / observed_history |
| 2026-01-21 | RES | 1300 | 198.74 | 198.74 | 199.14 | -0.40 | doc 343 (current, October 1, 2025) | tariff / tariff |
| 2026-02-18 | RES | 2405 | 348.57 | 348.57 | 348.57 | +0.00 | doc 343 (current, October 1, 2025) | tariff / tariff |
| 2026-03-20 | R-TOU-CPP | 1290.58 | 177.97 | 177.97 | 177.98 | -0.01 | doc 346 (current, October 1, 2025) | tariff / tariff |

Result: **12/12 within $0.45**, line for line identical to the Phase 1B fixed-code run and the 2026-03-21 report for bills 1–11. Bill 12 is now compared against its real printed total, $177.97 vs $177.98, instead of the partial $145.98. Storm and Clean Energy estimates come from `observed_history` on 4 and 9 bills respectively; those matches are not independent evidence.

How the baseline guards against a shared omission:

- **Independent actual.** Each bill's actual is the electric section's printed "Total Current Charges". It must equal the front page's "Current Electric Charges", a second printed figure. The test fails if the total is missing (`null`) or the two disagree. Bill 12 previously had a `null` total, and its "actual" was the sum of the lines that happened to be parsed.
- **Component closure.** The billed components must sum to the printed total. Every billed component must also be estimated, and vice versa: no `actual_only` or `estimated_only` rows, and the five required DEP residential components must be present. A rider missing from both sides now fails closure against the printed total.
- **Selection.** Bills for Apr–Sep 2025 must select the historical leaf-500 sheet effective 2024-10-01 (12.119 ¢). Nov 2025–Feb 2026 bills select the current sheet. Bill 12 selects leaf-503.
- **Circular evidence is labelled.** Each estimated component records `estimate_source`: `tariff`, `observed_history` (amounts from *other* saved bills) or `same_bill_observed`. Base energy, BCC and the summary rider must be `tariff`. A Storm or Clean Energy match that is `observed_history` is not independent evidence (§8).
- **Live reproduction.** With `DUKE_RATES_TEST_LIVE_DB` set, `test_live_reconciliation_reproduces_frozen_baseline` re-runs all 12 reconciliations and requires the same version selection and component estimates to ±$0.005. `test_live_stored_effective_dates_are_readable` fails if any stored effective date stops parsing. That guard would have caught the original regression in CI.

Regenerate with `python bench/billing_baseline.py --db <scratch copy> --out tests/fixtures/billing_baseline/progress_nc_12_bills.json`. Expect about 10 minutes on the full DB; it is I/O-bound (§6 of the plan).

## 4. Correction manifest review

### 4.1 C1–C9 (`scripts/corrections/2026-10-27_billing_integrity_corrections.json`)

Each item now carries an `evidence` block (pages, printed effective-date phrase, value tokens). `verify_manifest.py` checks these read-only against the PDF and the DB (`reports/phase1c/manifest_verification.json`). A tampered manifest (wrong value, wrong date, stale precondition) is rejected. That negative check was run.

| ID | Source PDF evidence | Stored value (production) | Proposed | Effective date | Reversible | Downstream consumers |
|---|---|---|---|---|---|---|
| C1 | leaf-502 2024 p1–2: BCC $14.00; 28.821 / 10.911 / 7.105 ¢ (same both seasons) | hd 33 JSON has a `$1.00` BCC, a `0.02` energy charge and an unperioded on-peak entry | remove the 3 artifacts | "from October 1, 2024 through September 30, 2025" = hd 33 | yes (prior JSON in receipt) | `ProgressNCHistoricalTariffSelector` → `BillingEngine` (R-TOU estimate would bill $1.00 BCC) |
| C2 | leaf-502 2025 p1–2: $14.00; 29.905 / 11.321 / 7.372 ¢ | hd 34, same artifacts | remove | "on and after October 1, 2025" = hd 34 | yes | same |
| C3 | as C1, plus "plus $9.00" three-phase | vid 5306 has 1 row (on-peak) | insert BCC, off-peak, discount, three-phase | 2024-10-01 = vid 5306 | yes (tagged rows deleted) | `TariffBillingEngine` (R-TOU), rate comparison, URDB export |
| C4 | as C2 | vid 5307 has 1 row | insert 4 rows | 2025-10-01 | yes | same |
| C5 | leaf-503 p2: one non-seasonal rate set, 41.002 / 21.952 / 11.000 / 8.274 ¢ (no "May through September" on the rate page) | 4 rows `season='summer'` | `all_year` | 2025-10-01 | yes | `TariffBillingEngine` (R-TOUD/R-TOU-CPP), live tests |
| C6 | leaf-503 p1–2: BCC $14.00, three-phase $9.00 | vid 5313 has 4 rows, no fixed | insert 2 rows | 2025-10-01 | yes | same |
| C7 | leaf-571 p1 row "LED 420 420 51.09 142" | 1.09 | 51.09 | 2025-10-01 | yes | `TariffBillingEngine` (lighting), URDB export |
| C8 | leaf-656 p1: "Customer Charge in addition to the Basic Customer Charge …: $55.00" | 5.00 | 55.00 (label follow-up = P8) | 2023-10-01 | yes | `TariffBillingEngine` rider path |
| C9 | bill 2026-03-20 p4: Storm $4.93, Summary 27.06, Total $177.97 | statement without those lines; total `null` | append + total | n/a | yes | `bill_accuracy`, observed-component history. **Superseded:** re-import bill 12 with the fixed parser, which gives an identical result. |

Scratch validation: C1–C9 applied to a fresh scratch copy left the 12-bill reconciliation unchanged apart from bill 12's actual becoming 177.97. The live-engine tests went from 11 to 7 failures. Reverting via the receipt restored every touched row and the charge count (5,198) to match production's SHA-256 (`reports/phase1c/corrections_revert_check.json`).

**Recommendation:** approve C1–C8 for production promotion through `apply_corrections.py` (with receipt). Replace C9 with a bill-12 re-import.

### 4.2 Open and new issues: proposal only (`scripts/corrections/2026-10-27_phase1c_proposed_corrections.json`)

| ID | Issue | Independently verifiable? | Evidence | Plan |
|---|---|---|---|---|
| P1 (F1) | DEC OL Metal Halide Area New Pole stored 6.05 | **Value yes; date no** | Rendered p22 of the E-7 filing: 56.05 | `update_charges` 6.05→56.05. The 2001-07-01 effective date is from an undated filing and is still unverified. |
| P2 (F3) | RDM 2025-04-01 holds the 2026 rate (0.252 ¢) on the ry1 PDF | **Yes** | hd 48 clean sheet and hd 1846 bundle: "April 1, 2025", 0.232 ¢; leaf-600 hd 40 and hd 41: "RDM 0.232 4/1/25" | Rebind vid 5414 → hd 48, value 0.00232; re-date hd 368 → 2026-04-01. Needs new apply kinds. |
| P3 (F4) | PIM 2025-04-01 holds the 2026 rate (0.005 ¢) | **Summary only** | hd 370 says 2026; leaf-600 hd 40/41 "PIM 0.002 4/1/25"; hd 1850 redline strikes 0.002 | Value 0.00002; re-date hd 370. No clean leaf-610 2025 sheet is held, so acquire it or accept the summary as the source. |
| P4 (F5) | JAA 2024-12-01 (vid 6649) holds the **2021** sheet's values | **No** (residential 0.538 ¢ only, from leaf-600) | hd 5365 prints "December 1, 2021" | Do not insert values. Flag vid 6649 and obtain the 2024-12-01 leaf-602 class table. |
| P5 (new) | JAA vid 5391, dated 2024-07-01: sign-flipped, misdated duplicate of the 2025-12-01 rates | **Yes** | hd 880 is the order for "December 1, 2025": 0.00518 / (0.00054) / 0.00464 | Retire vid 5391 (status `superseded`; keep its rows). |
| P6 (new) | leaf-504 R-TOU-EV: duplicate rows; BCC missing | **Yes** | hd 336 p8: BCC $14.00, 13.096 / 6.548 ¢ | Delete 2 duplicates; insert BCC. |
| P7 (new) | leaf-520 SGS 2025-10-01: $22 BCC missing | **Yes** | hd 337 p1 | Insert BCC (`commercial_small`). |
| P8 | leaf-656 row labelled "Basic Customer Charge" | **Yes** | Rider 68 p1 | Relabel; the engine treats the first fixed row as the BCC. |
| P9 (F2) | hd 40 title says 2024 | **Yes** (cosmetic) | PDF "April 1, 2025" | Retitle. |
| P10 | Six effective-date moves from §5 of the Phase 1B report | Mixed | Printed dates checked | 2519 and 2520 are now correct. 532, 2498 and 5313 are wrong both before and after (they look like filing dates on redline bundles). 2503 is unresolved. None are on the bill path. |
| P11 | leaf-611 and leaf-605 2025-01-01 versions deleted in the late-May dedup | **No** (needs re-verification) | Rows exist in the 2026-05-11 snapshot | Re-verify against the source PDFs, then restore. |

**Historical OCR.** The leading-5 audit (Phase 1B §6) covers about 85% of charges, those whose text is held locally. Its two confirmed hits are C7 and C8, and F1 is now confirmed as P1. No further OCR candidates were found. Rider line items still keep no source snippets, so they cannot be audited this way until provenance (§7) lands.

## 5. Live-engine tests (`tests/test_tariff_engine_live.py`)

| State | Result |
|---|---|
| Scratch copy of production | 11 failed / 13 passed |
| + C1–C9 | 7 failed / 17 passed (`reports/phase1c/tariff_engine_live_after_C1_C9.txt`) |

The Phase 1B report listed the remaining 7 as "five SGS + two TOU". The correct split is below.

| Test | Classification | Evidence |
|---|---|---|
| `TestRTouLive::test_on_peak_rate_is_highest_tou_rate` | **Outdated test expectation** | The engine is correct: $14 + 29.91 + 11.32 + 7.37 = $62.60, with on-peak > off-peak > discount. The test looks amounts up by the literal labels "Energy Charge - On-Peak/…". The stored labels are "On Peak Energy Charge", so every lookup returns 0. Key on period or rate instead. |
| `TestCrossScheduleSanity::test_partial_tou_coverage_warns_and_reduces_confidence` | **Unsupported tariff structure + engine defect + data defect** | The test's premise ("leaf-504 only has a discount period parsed") is outdated. The R-TOU-EV rows are `charge_type='energy'` with `tou_period` `standard`/`discount`. The engine's dispatcher has no branch for `energy`, so it **drops the rows silently** and returns $0 with no warning. The version is also effective 2026-01-01 while the test bills Nov 2025, and `_select_version` silently returns a future version. Data: duplicate rows and missing BCC (P6). |
| `TestCrossScheduleSanity::test_all_tou_schedules_return_results` | **Outdated test expectation** (+ data defect) | leaf-521/523 rows are `commercial_small`, but the test bills with the default `customer_class='residential'`, so the total is 0. With `commercial_small`, leaf-521 = $270.68, exactly the hand-computed value. leaf-523 also has generic `Tou_Energy` labels and no discount row (not in a manifest). |
| `TestSgsToueLive::test_tou_with_full_period_breakdown`, `test_sgs_toue_fixed_is_22_dollars` | **Outdated test expectation** | The service date 2025-08-01 predates the 2025-10-01 version the expected rates come from, and the class is residential (Phase 0 triage). With the class and a date ≥ 2025-10-01, the result matches to the cent. |
| `TestSgsLive::test_winter_energy_charge_applied`, `test_no_service_date_uses_all_year_charges_only` | **Outdated expectation + data defect** | The test assumes a winter-only energy rate. The 2025-10-01 sheet is non-seasonal (13.188 / 11.166 / 10.616 ¢ blocks), so the stored rows are correct and the seasonal warning can never fire. BCC $22 is missing (P7). |

**Genuine billing-engine defects (not fixed; changing billing logic was out of scope):**

1. `TariffBillingEngine.calculate` silently ignores any `charge_type` outside {fixed, energy_block, tou_energy, demand, adjustment, minimum, credit}. In production, 260 charges across 26 families use other values. 145 of these are `adjustment_total`, probably intentional. The rest (`energy`, `Rider Adjustment`, `Energy Charge`, `Fixed Monthly Charge`, …) are dropped with no warning. Recommendation: warn on unknown types and normalise charge types at write time.
2. `_select_version` returns the earliest **future** version when every version postdates the service date, without a warning.
3. The engine's TOU path has no `standard` period (R-TOU-EV), so this structure is unsupported.

**Recommended test changes (not made):** convert these tests to frozen-fixture tests seeded with documented versions and charges, as Phase 0 triage proposed. Key TOU assertions on period, pass `customer_class='commercial_small'` for SGS, and use service dates within the expected version.

## 6. Test results

| Run | Result |
|---|---|
| Full suite, no live DB (`reports/phase1c/pytest_full.txt`) | **1474 passed, 92 skipped, 4 xfailed, 0 failed.** Phase 1B was 1438 passed; the new tests are the parser, baseline and date-format tests. |
| Billing baseline, live (`DUKE_RATES_TEST_LIVE_DB` = scratch copy of production) | **2 passed** (reproduces all 12 frozen bills; all stored effective dates parse). Took 36 min, I/O-bound. |
| Mutation: restore the old bill-12 parse in the frozen file | 3 baseline tests fail, as intended |
| Live engine (`tests/test_tariff_engine_live.py`) | 11 failed on a production copy; 7 with C1–C9 (§5) |

## 7. Extraction benchmark (Phase 0, unchanged)

Baseline `8b77aa4` → repaired. The raw runs are now committed under `reports/phase0/`.

| Metric | Baseline → repaired |
|---|---|
| Value accuracy (recall) | 0.598 → 0.761 |
| Precision | 0.757 → 0.981 |
| Unit accuracy | 0.975 → 1.000 |
| Rate-class accuracy | 0.943 → 1.000 |
| Total reconciliation | 0.526 → 0.718 |
| Effective-date accuracy | 0.65 → 0.65 |
| Zero-doc false positives | 0 |

Phase 1B and 1C changed no extraction code, so the benchmark was not re-run.

## 8. Database integrity findings and remaining known defects

- **Date contract:** all 608 `historical_documents.effective_start`, 9 `effective_end`, 906 `tariff_versions.effective_start` and 2 `effective_end` values parse. This is now guarded by a live test.
- **Circular evidence in the baseline:** Storm Recovery and Clean Energy Rider on most 2025 bills are estimated from other saved bills (`observed_history`). The baseline file records this per component. Those matches do not independently validate those tariffs.
- **Summary-rider proration:** bills 2025-12-18 and 2026-01-21 differ by −$0.45 and −$0.40 because the engine applies only the latest leaf-600 summary to a period that straddles a rate change. This is an engine limitation, frozen as-is in the baseline.
- **Uncontrolled writers:** 31 ad-hoc scripts under `scripts/` write core tables directly. The provenance plan (M5) gates them.
- **TOU season tags:** only leaf-503 was checked for the 2026-05-29 re-tagging. Other TOU families are unchecked.
- **Unaudited OCR:** about 15% of charges have no local text, and rider line items have no snippets.
- The engine defects in §5.
- Open data issues P1–P11.

## 9. Corrections awaiting approval

1. **C1–C8:** promote to production via `apply_corrections.py --manifest … --receipt …` after a backup.
2. **C9:** do not apply. Re-import bill 12 with the fixed parser (`parse-bill`); the result is identical to C9.
3. **P2, P5, P6, P7, P8:** verified and ready once `apply_corrections.py` supports the needed kinds (`update_version`, `update_historical_document`, `delete_charges`, each with precondition, receipt and revert).
4. **P1:** the value is verified; approve the value fix with the date left as-is.
5. **P3:** decide whether the leaf-600 summaries are acceptable evidence, or acquire the leaf-610 2025 sheet.
6. **P4, P11:** blocked on source documents.

Production DB SHA-256: `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08` both before and after this phase (unchanged).

## 10. Git branch status

`stabilize/phase0-docling-rider-fixes`, local only, 16 commits ahead of `main` including the closeout commit. Not pushed, not merged.

Most recent branch commits (newest first; Phase 1C begins at `e7ca035`; the closeout docs commit follows):

- `2e1a28b corrections: reviewed C1-C9 manifest, read-only verifier, Phase 1C proposal`
- `8ba2de0 test(billing): freeze the source-verified 12-bill reconciliation baseline`
- `1afc7d6 docs: preserve Phase 0 benchmark outputs and Phase 1B investigation evidence`
- `7dfa902 fix(bill-parser): follow "Billing details - <section> continued" across pages`
- `c1acc1c chore(ingestion): record provenance scripts for the June 2026 DEP/DEC data changes`
- `e7ca035 fix(billing): read ISO historical effective dates; compare mixed-tz timestamps`
- `d6cb584 bench: v0 extraction benchmark (26 hand-verified docs) + harnesses; persistence design proposal`
- `6400551 fix(ocr-normalization): stop rewriting a leading digit 5 as a dollar sign`

Not committed, deliberately:
- about 60 `reports/_*.txt` / `scripts/_*.py` scratch files from earlier sessions;
- `fake.db` (0 bytes);
- DB backups under `data/` (gitignored);
- scratch DBs (session scratchpad only).

`CLAUDE.md` and `AGENTS.md` were restored after `gitnexus analyze` rewrote their GitNexus block. The index itself was refreshed (17,716 symbols). GitNexus resolves no callers for the private functions changed here, so blast radius was established by text search: `parse_bill_text` is called by the `cli` and `billing` bill-import commands, and `_comparison_status` only within `bill_accuracy`.

## 11. Next phase

`docs/design/2026-10-27_provenance_implementation_plan.md`. The first PR is M1+M2: additive tables, plus a legacy backfill verified by byte-for-byte rematerialization on a scratch copy, gated by the billing baseline and the benchmark.
