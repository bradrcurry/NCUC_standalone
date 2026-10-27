# Billing Integrity Investigation (Phase 1B)

**Date:** 2026-10-27
**Branch:** `stabilize/phase0-docling-rider-fixes` (HEAD `d6cb584` plus uncommitted changes listed in §10)
**Method:** Read-only SQL against `data/db/duke_rates.db` and its backups, opened `mode=ro&immutable=1`. Reconciliation and corrections ran only on scratch copies in the session scratchpad. The production DB, the backups, the 12 bill PDFs and `data/processed/bill_accuracy/*` were not modified. Scripts and raw outputs are in `reports/phase1b/`.

---

## 1. Executive summary

- **The regression is a date-format contract break, not bad tariff values.** Between 2026-05-11 and 2026-06-05, all 38 long-form NC `historical_documents` dates (`"October 1, 2024"`) were rewritten to ISO (`"2024-10-01"`). The fingerprint matches the `normalize-nc-effective-dates` CLI command (`src/duke_rates/cli.py:5201`). The billing selector's date reader, `parse_effective_date` (`src/duke_rates/parse/normalization.py:45`), only understood `"%B %d, %Y"` and `"%B %Y"`, so it now read every ISO-dated historical version as **undated**.
- The current-site RES document (doc 343) still carries `"October 1, 2025"` from its text metadata. It therefore outscored the now-"undated" historical 2024-10-01 version (hd 29) for **every** service date. Bills for service before 2025-10-01 were priced at **12.623 ¢/kWh instead of the billed 12.119 ¢/kWh**. The 0.504 ¢/kWh excess, less the 5% RECD credit (net 0.479 ¢/kWh), reproduces each of the six poor bills and the partial October bill to the cent.
- The original and repaired code gave identical results because neither change touched `parse_effective_date`.
- **Fix (code, 2 lines plus 2 regression tests):** accept ISO in `parse_effective_date`. Fixing that exposed a latent naive/aware `datetime` comparison in `lineage._history_version_sort_key`, which is now fixed too. With both fixes, the current DB, the 2026-06-05 backup and the 2026-05-11 snapshot all produce **line-for-line identical** estimates to the 2026-03-21 baseline (§7). The full suite passes: 1438 passed, 90 skipped, 4 xfailed, 0 failed.
- **The old 12/12 was itself overstated by one bill.** The 2026-03-20 R-TOU-CPP bill's electric section continues on page 4 (Storm $4.93, Summary of Rider Adjustments $27.06, **total $177.97**). The bill parser stopped at the page break. Both the "actual" (145.98, a line-item fallback) and the estimate (145.99) therefore omitted $31.99, and the match is an artifact. With the page restored (correction C9 on scratch), that bill reconciles at **$177.97 actual vs $177.98 estimated (−0.01)**, with every component tariff-derived.
- **Separate data defects** found while tracing, none of which affect the 12 bills:
  - leaf-502 (R-TOU) charges missing, and spurious entries in its parsed JSON;
  - leaf-503 rows mis-tagged `summer`;
  - two confirmed OCR leading-5 corruptions (leaf-571 LED 420, leaf-656);
  - a late-May version dedup that dropped the correct 2025-04-01 RDM value (0.232 ¢);
  - misdated 2026 "ry1" rider PDFs and a misbound JAA version.

  Eight corrections are in a reviewable, reversible manifest, plus C9 for the bill parse; five more are flagged for review (§6).

## 2. Baseline reconstruction (Task 1)

| Question | Finding |
|---|---|
| Report | `data/processed/bill_accuracy/progress_nc_actual_bill_accuracy{.json,.csv,_summary.json}` (12/12 `good_match`, max \|Δ\| $0.45). Cited by `docs/known_issues.md:37` and `docs/roadmap.md:37`. |
| Generated | 2026-03-21 11:31 −04:00 (file mtimes). The same folder's 2026-03-20 15:56 rollup (`progress_nc_bill_validation_rollup.csv`) shows an earlier run that day with bill 1 at −6.72 (poor), so the 12/12 came after same-day fixes. |
| Code | Predates git: the repo starts at `09b83db` on 2026-05-06, so the March code revision cannot be checked out. |
| DB state | Not archived. There is no backup from March. The oldest full snapshot is `data/db/duke_rates_eval_20260511_162535.db` (2026-05-11). The other sibling DBs (`data_api_nc*`, `data_eval`, `data_browser`, `data/duke_rates.db`) are small March/April fixtures with no bill statements. |
| Reproducible? | **Yes, functionally.** Current code on the 2026-05-11 snapshot reproduces every one of the 12 March estimates to the cent (`reports/phase1b/dumps/dump_may11.json`). |
| Versions used | **Base:** hd 29 (leaf-500, "October 1, 2024", 12.119/11.119 ¢) for bills 1–6. Current doc 343 (leaf-500, Oct 1 2025, 12.623/11.623 ¢) for bills 7–11, with bill 7 split by billed sub-period rows. Current doc 346 (leaf-503 R-TOU-CPP) for bill 12. **Summary of Rider Adjustments:** hd 40 (eff 2025-04-01, 1.950 ¢), hd 41 (2025-12-01, 2.031 ¢), hd 42 (2026-01-01, 2.097 ¢). **RECD:** doc 269, 5% of energy charges. **Storm and Clean Energy:** mostly the *observed-history fallback* (amounts from other saved bills), with tariff-derived storm from 2025-11 onward (see §8). |

## 3. Bill-by-bill reconciliation (Task 2)

"Actual" is the bill's electric Total Current Charges, which excludes taxes and the lighting section. Δ = actual − estimate.

| Bill | Rate | kWh | Actual | Mar-21 report | 2026-05-11 DB | Current DB (before fix) | Current DB + code fix | Base version (fixed) |
|---|---|---|---|---|---|---|---|---|
| 2025-04-21 | RES | 821 | 121.15 | 121.14 (+0.01) | 121.14 (+0.01) | 125.07 (**−3.92**) | 121.14 (+0.01) | hd 29, 2024-10-01 |
| 2025-05-20 | RES | 1460 | 215.67 | 215.67 (0.00) | 215.67 (0.00) | 222.66 (**−6.99**) | 215.67 (0.00) | hd 29 |
| 2025-06-19 | RES | 1316 | 195.94 | 195.94 (0.00) | 195.94 (0.00) | 202.23 (**−6.29**) | 195.94 (0.00) | hd 29 |
| 2025-07-21 | RES | 1520 | 223.61 | 223.59 (+0.02) | 223.59 (+0.02) | 230.87 (**−7.26**) | 223.59 (+0.02) | hd 29 |
| 2025-08-20 | RES | 1379 | 204.08 | 204.07 (+0.01) | 204.07 (+0.01) | 210.68 (**−6.60**) | 204.07 (+0.01) | hd 29 |
| 2025-09-19 | RES | 1130 | 170.01 | 170.02 (−0.01) | 170.02 (−0.01) | 175.44 (**−5.43**) | 170.02 (−0.01) | hd 29 |
| 2025-10-21 | RES | 876 | 136.94 | 136.95 (−0.01) | 136.95 (−0.01) | 138.76 (**−1.82**) | 136.95 (−0.01) | doc 343 + split rows |
| 2025-11-19 | RES | 813 | 131.22 | 131.19 (+0.03) | 131.19 (+0.03) | 131.19 (+0.03) | 131.19 (+0.03) | doc 343, 2025-10-01 |
| 2025-12-18 | RES | 1234 | 188.78 | 189.23 (−0.45) | 189.23 (−0.45) | 189.23 (−0.45) | 189.23 (−0.45) | doc 343 |
| 2026-01-21 | RES | 1300 | 198.74 | 199.14 (−0.40) | 199.14 (−0.40) | 199.14 (−0.40) | 199.14 (−0.40) | doc 343 |
| 2026-02-18 | RES | 2405 | 348.57 | 348.57 (0.00) | 348.57 (0.00) | 348.57 (0.00) | 348.57 (0.00) | doc 343 |
| 2026-03-20 | R-TOU-CPP | 1290.58 | 145.98 *(partial; true 177.97)* | 145.99 (−0.01) | 145.99 (−0.01) | 145.99 (−0.01) | 145.99 (−0.01) | doc 346 |

The 2026-06-05 backup gives exactly the "before fix" column. The June 5 web-tariff ingest did not move any bill.

### Components that differ (current DB before fix, vs actual)

| Bill | Component | Actual | Est. before fix | Est. after fix | Cause |
|---|---|---|---|---|---|
| 2025-04-21 | Energy (800 @ 12.119 + 21 @ 11.119) | 99.28 | 103.42 | 99.28 | 2025-10-01 rates applied |
| 2025-04-21 | Energy Conservation Credit (5%) | −4.96 | −5.17 | −4.96 | 5% of the inflated energy |
| 2025-05-20 | Energy (1460 @ 12.119) | 176.94 | 184.29 | 176.94 | same |
| 2025-05-20 | ECC | −8.85 | −9.21 | −8.85 | same |
| 2025-06-19 | Energy / ECC | 159.49 / −7.97 | 166.12 / −8.31 | 159.49 / −7.97 | same |
| 2025-07-21 | Energy / ECC | 184.21 / −9.21 | 191.87 / −9.59 | 184.21 / −9.21 | same |
| 2025-08-20 | Energy / ECC | 167.12 / −8.36 | 174.07 / −8.70 | 167.12 / −8.36 | same |
| 2025-09-19 | Energy / ECC | 136.94 / −6.85 | 142.64 / −7.13 | 136.94 / −6.85 | same |
| 2025-10-21 | Energy (379 kWh Sep 18–30) / ECC | 107.90 / −5.40 | 109.81 / −5.49 | 107.90 / −5.39 | September split row priced at the October rate |

Check: excess = kWh × (0.12623 − 0.12119) × 0.95. Bill 1: 821 × 0.00504 × 0.95 = 3.93 (observed 3.92). Bill 7: 379 × 0.00504 × 0.95 = 1.81 (observed 1.82).

Components that **agree** on all 11 RES bills, before and after the fix: Basic Customer Charge (including the 17-day proration on bill 1, $7.93), Storm Recovery, Clean Energy Rider, and Summary of Rider Adjustments to within $0.03, except bills 9 and 10.

### Residual differences after the fix (explained, not data defects)

| Bill | Δ | Component | Explanation |
|---|---|---|---|
| 2025-12-18 | −0.45 | Summary of Rider Adjustments: est 25.06 vs billed 9.69 + 6.44 + 8.48 = 24.61 | Leaf-600 changed on 2025-12-01, mid-period. Duke splits at the rate-change date; the engine applies only the latest summary (2.031 ¢, hd 41) to the whole period (`reconciliation._load_additional_rider_parse_results` keeps `latest_summary`). Engine limitation, documented in `docs/known_issues.md`. |
| 2026-01-21 | −0.40 | Summary: est 27.26 vs billed 15.99 + 4.88 + 5.99 = 26.86 | Same mechanism at the 2026-01-01 change (2.097 ¢, hd 42). |
| 2025-11-19 | +0.03 | Summary 15.85 vs 15.88 | Rounding (1.950 ¢ × 813 kWh). |
| 2026-03-20 | see §7 | Storm + Summary missing from the parsed bill | Bill-parser page-break defect (C9). |

## 4. Root causes (Task 3)

### Confirmed: R1, the billing selector misreads ISO dates (cause of all 7 regressed bills)

Chain: **source PDF → published rate → version → DB record → calculation**

1. **Source PDF:** `data/historical/raw/nc/progress/rate/residential-service-leaf-no-500-eff-2024-10-01-data-historical-manual-leaf-no-50.pdf`. It reads "NC First Revised Leaf No. 500 … Effective for service rendered from October 1, 2024 through September 30, 2025", with 12.119 ¢ per kWh for the first 800 kWh (all kWh May–Sep) and 11.119 ¢ for additional kWh.
2. **Bills:** every bill from April through September 2025 prints `@ $0.12119000`.
3. **Version:** `historical_documents` id 29, family `nc-progress-leaf-500`. Its `parsed_result_json` is unchanged between snapshots (12.119/11.119, BCC $14).
4. **DB record change:** `effective_start` `"October 1, 2024"` → `"2024-10-01"` and `effective_end` `"September 30, 2025"` → `"2025-09-30"`, some time between the 2026-05-11 snapshot and the 2026-06-05 backup. All 38 non-ISO NC rows were converted (ids 4–1853), and no NC rows were left non-ISO. That matches `normalize-nc-effective-dates` (`NOT GLOB '????-??-??*'`, NC-wide). No receipt, log or transcript records the run, so the exact time is unrecoverable.
5. **Calculation:** `ProgressNCHistoricalTariffSelector._selection_score` calls `parse_effective_date`, which returned `None` for ISO. hd 29 scored `(0, …)`; doc 343 (start parsed from its long-form text) scored `(1, …)` and won for every date.
6. **Classification:** a billing-interpretation defect (reader/writer format mismatch), triggered by a data normalization. Extraction and persistence of the values were correct.

**Blast radius:** `parse_effective_date` has 41 references in 7 modules (`tariff_selector`, `lineage`, `reconciliation`, `riders`, `bill_rider_date_audit`, `bill_relevant_gaps`, `schedule_parser`). The GitNexus index is 22 commits stale and resolved no callers, so this was counted by text search. With ISO unreadable:
- **Rider selection:** `_rider_version_is_not_future` treated ISO-dated rider versions as undated.
- **Effective-date checks:** `_effective_on_or_before` treated them as always effective.

The fix restores dated behaviour on all of these paths.

### Confirmed: R2, latent naive/aware datetime comparison (exposed by the R1 fix)

Once ISO versions are dated, versions sharing an effective date tie-break on `retrieved_at`. Stored timestamps mix naive and UTC-aware values, so `build_chains` raised `TypeError` (`lineage.py:55`). The fix treats naive values as UTC in `_history_version_sort_key`, whose only callers are in `lineage.py`.

### Confirmed: R3, bill PDF parser drops page-continued sections (bill 12)

`../version_3/Actual Duke Bills/2026-03-20.pdf`, page 4: "Billing details - Electric continued / Storm Recovery Charge $4.93 / Summary of Rider Adjustments 27.06 / Total Current Charges $177.97". The stored statement (`bill_statements` id 12) stops at the Energy Conservation Credit, and its `total_current_charges` is NULL. The other 11 bills have no continuation and their parsed totals match the PDFs. Classification: bill-statement extraction.

### DEP leaf 502 / 503 (prioritized suspects)

They don't affect the 12 bills: bill 12 prices its energy from the billed TOU quantities, and no saved bill is on R-TOU. They do affect `TariffBillingEngine` and the live tests.

| Item | Stored | Published (source PDF) | Version | Origin | Classification |
|---|---|---|---|---|---|
| leaf-502 2024-10-01 `tariff_charges` | only on-peak 0.28821 | BCC $14.00; on 28.821, off 10.911, discount 7.105 ¢ (same both seasons); 3-phase +$9.00. `residential-time-of-use-energy-leaf-no-502-eff-2024-10-01-…pdf` p2 | vid 5306 / hd 33 | Only on-peak existed in 2026-05-11 too; recreated on 2026-05-29 | extraction / persistence |
| leaf-502 2025-10-01 `tariff_charges` | only on-peak 0.29905 | BCC $14.00; 29.905 / 11.321 / 7.372 ¢; +$9.00. `…eff-2025-10-01-…pdf` p2 | vid 5307 / hd 34 | same | extraction / persistence |
| leaf-502 `parsed_result_json` (hd 33, 34) | extra `Basic Customer Charge 1.0` (first in list), `Energy Charge 0.02`, unperioded `Energy Charge` = on-peak | numbering "1." / "2. kWh Energy Charge" is not a rate | hd 33, 34 | present in both snapshots | extraction. `BillingEngine` uses `fixed_charges[0]`, so an R-TOU estimate would bill $1.00 |
| leaf-503 2025-10-01 | 4 TOU rows `season='summer'`, no BCC | 41.002 / 21.952 / 11.000 / 8.274 ¢, one non-seasonal set; BCC $14.00. `leaf-no-503-schedule-r-tou-cpp-…pdf` p2; bill 2026-03-20 (March) prints the same four rates | vid 5313 / hd 335 | 2026-05-11 rows were `all_year`; the 2026-05-29 re-extraction re-tagged them `summer` | classification |

### Other confirmed findings (`tariff_charges` / lineage; not in the 12-bill path)

| ID | Finding | Evidence |
|---|---|---|
| F3 | RDM (leaf-608) 2025-04-01: correct version lost in the late-May dedup | hd 48 PDF "on and after April 1, 2025", 0.232 ¢. The leaf-600 2025-04-01 summary shows "RDM 0.232 4/1/25". On 2026-05-11 vid 5415 (hd 48, 0.00232) and vid 5414 (hd 368, 0.00252) coexisted. hd 368 is the current-site ry1 PDF, whose text says "on and after April 1, **2026**". Only vid 5414 survives. |
| F4 | PIM (leaf-610) ry1 PDF misdated | hd 370 text "April 1, 2026", 0.005 ¢, stored 2025-04-01. The published 2025-04-01 PIM is 0.002 ¢. Already wrong on 2026-05-11. |
| F5 | JAA (leaf-602) 2024-12-01 misbound | vid 6649 now points to hd 5365 (E-2 Sub 1275, "on and after December 1, 2021") with residential 0.00482. The published 12/1/24 JAA is 0.538 ¢ (leaf-600 summary). On 2026-05-11 it pointed to a Sub 1354 bundle with 0.00464. |
| F2 | hd 40 label | Title and filename say "eff. 2024-04-01"; the PDF says "on and after April 1, 2025" with TOTAL 1.950 ¢. Values are correct; the label is cosmetic. |

## 5. Historical database change analysis (Task 5)

| Snapshot | `tariff_charges` | `tariff_versions` | `historical_documents` (all / DEP) | DEP hd non-ISO dates |
|---|---|---|---|---|
| 2026-05-11 eval | 26,638 | 1,667 | 938 / 689 | 38 |
| 2026-06-05 pre-web-updates | 4,966 | 985 | 678 / 460 | 0 |
| current (2026-10-26) | 5,198 | 988 | 682 / 462 | 0 |

- **May 27–29 cleanup** (`fix_misattributed_tariff_versions`, `fix_cross_attribution_general`, `retire_noise_shingle_families`, `dedup_tariff_versions_nc`, the customer-class backfill, then the 2026-05-29 re-extraction that created 4,334 of the current rows):
  - Most of the drop is intended removal of cross-attributed noise, e.g. 18,904 leaf-600 rows produced by one bundle (session notes, 2026-05-27).
  - For the 16 bill-relevant DEP families, 2024-01-01 onward, labels and values are unchanged for leaf-500, 501 and 601 (only `customer_class` was filled in).
  - Legitimate values were lost or altered for **leaf-608 2025-04-01 (F3)**, **leaf-611 2025-01-01** (version deleted; published CAR 0.034 ¢ eff 1/1/25), **leaf-605 2025-01-01** (version deleted), **leaf-602 2024-12-01 (F5)** and **leaf-503 seasons**. Full diff: `reports/phase1b/family_diff_may11_vs_current.txt`.
- **Effective-date normalization:** 109 NC `historical_documents` rows changed dates between snapshots. 38 were format-only (R1). The rest filled NULL dates, and a few moved existing dates:
  - hd 532: 2024-01-21 → 2024-01-27
  - hd 2498: leaf-600 2025-01-21 → 2025-01-27
  - hd 2519: leaf-640 2019-01-01 → 2018-03-16
  - hd 2503, 2520, 5313: also moved

  These moves aren't verified here and aren't bill-relevant.
- **Manually corrected charges:** the 2026-05-11 snapshot had **no** annotated (`notes`) charges, so there are no notes-marked manual fixes that could have been lost. Since `729034b`, annotated rows survive re-extraction.
- **June 2026 web-tariff ingest** (+236 charges on 2026-06-05): the bill results on the 2026-06-05 backup and the current DB are identical, so the ingest did not affect the 12 bills.
- **Did a cleanup remove a legitimate billing component for the 12 bills?** No. The bill path reads `historical_documents.parsed_result_json` and `parse_results`, which are unchanged for every version the bills use. The only change that mattered was the date-string rewrite.

## 6. OCR leading-5 corruption audit (Task 4)

Defect (fixed in `6400551`): `(?<!\w)5(?=\d+\.\d+)` → `$`, so `$55.00`→`$$5.00`, `51.09`→`$1.09`.

Method:
- For every non-zero `tariff_charges` value, search all held text for the PDF (raw sidecars, Docling plain text and tables JSON, page artifacts) for a corrupting token `5<value>` and for clean `<value>` tokens.
- Also scan the 50 `parsed_result_json` documents.
- Verify candidates against the PDF via PyMuPDF.

Scripts: `reports/phase1b/scripts/ocr5_audit*.py`.

| Charge id | Family / eff | Label | Stored | Raw text | PDF check | Confidence |
|---|---|---|---|---|---|---|
| 753649 | leaf-571 2025-10-01 | Street Lighting Monthly Charge – LED 420 | 1.09 | `51.09` (×5), no clean `1.09` | p1 row "LED 420 \| 420 \| 51.09 \| 142" | **Confirmed** (C7) |
| 755536 | leaf-656 2023-10-01 | "Basic Customer Charge" | 5.00 | `55.00` | p1 "Customer Charge in addition to the Basic Customer Charge …: $55.00." | **Confirmed** (C8; label also wrong) |
| 752064 | DEC OL 2001-07-01 | Metal Halide – Area – New Pole | 6.05 | `56.05` (×7), no clean `6.05` | p22 OCR table garbled; row binding not confirmed | **Probable** (F1) |
| 753007 / 753073 | leaf-500 / 501 2017-12-01 | Three-Phase Surcharge | 8.71 | both forms present | PDF "plus $8.71" | Not corrupted |
| 755518 | leaf-640 2023-01-01 | RECD % | 5.0 | collision | PDF "5% times" | Not corrupted |

No candidates in `parsed_result_json`. Coverage gap: about 15% of charges have no text held locally, and `rider_line_items` keeps no snippets, so it could not be audited this way. Rider-summary values are below 50 ¢, where this defect cannot occur unless the value is ≥ 50.

## 7. Corrections and scratch validation (Tasks 6–7)

### Code (uncommitted, on the branch)

| File | Change | Test |
|---|---|---|
| `src/duke_rates/parse/normalization.py` | `parse_effective_date` also accepts `%Y-%m-%d` | `test_progress_nc_historical_tariff_selector_reads_iso_stored_dates` (fails without the fix, passes with it) |
| `src/duke_rates/historical/lineage.py` | `_history_version_sort_key` compares `retrieved_at` as UTC | `test_history_version_sort_key_orders_mixed_naive_and_aware_timestamps` |

No billing formula was changed.

### Data (manifest only; applied on scratch, not promoted)

- Manifest: `scripts/corrections/2026-10-27_billing_integrity_corrections.json`.
- Apply script: `scripts/corrections/apply_corrections.py`. It refuses the production DB, checks each item's precondition (the stored "before" value or row count, or exact JSON matches), skips rather than forces, writes a receipt with the prior state, and `--revert` replays the receipt. Inserted rows carry `notes='correction:<manifest>:<id>'`, so they survive re-extraction under the `729034b` guard. This fits the planned immutable-extraction design: corrections are a separate, append-only, evidence-carrying layer.

| ID | Correction |
|---|---|
| C1, C2 | Remove the three leaf-502 `parsed_result_json` artifacts (hd 33, 34) |
| C3, C4 | Insert leaf-502 BCC, off-peak, discount and three-phase rows (vid 5306, 5307) |
| C5 | leaf-503 four rows `summer` → `all_year` |
| C6 | Insert leaf-503 BCC and three-phase rows (vid 5313) |
| C7, C8 | OCR leading-5 value fixes |
| C9 | Restore the page-4 electric continuation for bill 12 (Storm 4.93, Summary 27.06, total 177.97) |

Flagged, not applied (they need a source document or re-dating review): **F1–F5**.

### Results

| Run | good / close / poor | max \|Δ\| | Notes |
|---|---|---|---|
| Current DB, original code (reported) | 5 / 1 / 6 | 7.26 | |
| Current DB + code fix | 12 / 0 / 0 | 0.45 | Identical, line for line, to the 2026-05-11 run and the 2026-03-21 report |
| 2026-06-05 backup + code fix | 12 / 0 / 0 | 0.45 | identical |
| 2026-05-11 snapshot + code fix | 12 / 0 / 0 | 0.45 | identical (no new errors from the fix) |
| Current DB + code fix + C1–C8 | 12 / 0 / 0 | 0.45 | Identical to the row above; C1–C8 don't touch the bill path |
| … + C9 (bill 12 statement restored) | 12 / 0 / 0 | 0.45 | Bill 12 now compared against its real total: **177.97 vs 177.98 (−0.01)** |

Bill 12 after C9, component by component (actual / estimate):

| Component | Actual | Estimate |
|---|---|---|
| BCC | 14.00 | 14.00 |
| TOU energy | 137.03 | 137.03 (CPP 0 @ 41.002 ¢, on-peak 91.573 kWh @ 21.952 ¢, off-peak 650.145 kWh @ 11.000 ¢, discount 548.865 kWh @ 8.274 ¢) |
| ECC | −6.86 | −6.85 |
| Storm | 4.93 | 4.93 (STS + STS-2 tariffs) |
| Clean Energy | 1.81 | 1.81 (leaf-601) |
| Summary | 27.06 | 27.06 (leaf-600 2026-01-01, 2.097 ¢) |

The fix introduced no new billing error. Every estimate line on every bill is identical to the 2026-05-11 original-code run, across all three snapshots (`reports/phase1b/dumps/`).

`TariffBillingEngine` live tests (`tests/test_tariff_engine_live.py`, `DUKE_RATES_TEST_LIVE_DB` = scratch copy):
- **Current DB:** 11 failed / 13 passed.
- **After C1–C8:** 7 failed / 17 passed.
- **Fixed:** all four R-TOU / R-TOUD period-breakdown and $14-fixed tests.
- **Still failing:**
  - five SGS / SGS-TOUE (leaf-521) tests: customer-class and expectation drift, already triaged in Phase 0 and out of scope here;
  - `TestRTouLive::test_on_peak_rate_is_highest_tou_rate`: off-peak and discount amounts are 0 for its usage input; not investigated;
  - `test_partial_tou_coverage_warns_and_reduces_confidence`: the expected warning is missing; not investigated.

**Revert:** replaying both receipts with `--revert` restored the scratch copy. SHA-256 of every touched row (leaf-502/503/571/656 charges, hd 33/34 JSON, bill 12 statement) and the total charge count (5,198) are identical to the untouched copy. Re-running the manifest after it was applied skips every item on its precondition, so it is idempotent.

## 8. Remaining uncertainties

1. **When R1 happened.** It falls between 2026-05-11 16:25 and 2026-06-05 15:06. Attribution to `normalize-nc-effective-dates` is by fingerprint (all 38 rows, NC-wide, ISO-only); no run record exists.
2. **The March code.** It isn't in git. Equivalence is shown by output (12/12 identical to the cent), not by diff.
3. **Circular evidence.** Storm Recovery and Clean Energy Rider for most 2025 bills come from the observed-history fallback, i.e. amounts from *other saved bills*, not from tariffs. Those two components therefore aren't independently validated by the reconciliation. Base energy, BCC, ECC and Summary of Rider Adjustments are tariff-derived.
4. **The proration residuals** on bills 9 and 10 are an engine limitation (no mid-period split for the summary rider). Fixing them changes billing logic and was out of scope.
5. **F1 and the other date moves.** F1 (DEC OL 56.05) needs a rendered-page check. The 6 non-format date moves in §5 aren't verified.
6. **Audit coverage.** The OCR audit covers only charges whose text we hold, about 85% of rows.

## 9. Recommended next steps

1. Review and commit the two code fixes. Add a guard test that every `historical_documents.effective_start` the selector sees parses to a date, so a future format change fails CI instead of mispricing.
2. Fix the bill PDF parser to follow "Billing details – Electric continued", then re-parse bill 12 from its PDF rather than applying C9 by hand.
3. Mark `normalize-nc-effective-dates` (and any date rewriter) as requiring a reader-compatibility check. Record runs in `workflow_action_receipts`.
4. Review C1–C8 and promote them through the apply script, keeping the receipt.
5. Resolve F3–F5: re-date the ry1 rider PDFs (hd 368, 370) to 2026-04-01, restore RDM 2025-04-01 from hd 48, and find the 2024-12-01 JAA sheet. Then re-check leaf-605/611 2025-01-01, whose versions were deleted.
6. Make the reconciliation report flag bills whose "actual" is a line-item fallback (NULL total). That would have exposed bill 12.
7. Decide whether the summary-rider mid-period proration is worth implementing (bills 9 and 10, about $0.40 each).

## 10. Phase 0/1 close-out

### Branch and history

`stabilize/phase0-docling-rider-fixes`, 9 commits ahead of `main`:

| Commit | Change |
|---|---|
| `8b77aa4` | Keep NULL-code real riders in Leaf-600 components; text fallback when the coordinate parser yields no classes |
| `8c092b5` | Guard the production DB in tests; route live-data tests to a snapshot (`DUKE_RATES_TEST_LIVE_DB`) |
| `d473c02` | Docling: include table cells in the page miner, repair progressive OCR, verify artifacts |
| `28a8185`, `5ce3fbe` | Rider summary: close sections at TOTAL rows, assign units by column, isolate missed headers, recognize the fragmented June 2026 HP header, choose parse by checksum |
| `6f2a5d8` | Update tests outdated by intentional changes |
| `729034b` | DB safeguards: re-extraction keeps annotated / manual / LLM-promoted charges; `replace_parsed_tariff_data` raises `HistoricalDataLossError`; TD-002 made one-shot and non-destructive |
| `6400551` | OCR normalization: stop rewriting a leading 5 as `$` |
| `d6cb584` | v0 extraction benchmark (26 hand-verified docs), harnesses, non-destructive extraction proposal |

### Benchmark (`reports/phase0/bench_scores.md`, baseline `8b77aa4` → repaired)

| Metric | Baseline → repaired |
|---|---|
| value accuracy (recall) | 0.598 → 0.761 |
| precision | 0.757 → 0.981 |
| unit accuracy | 0.975 → 1.000 |
| rate-class accuracy | 0.943 → 1.000 |
| total reconciliation | 0.526 → 0.718 |
| effective-date accuracy | 0.65 (unchanged) |
| zero-doc false positives | 0 |

### Tests

- **Phase 0 start:** 23 failures plus 1 collection error (`reports/phase0/test_failure_triage.md`).
- **Phase 0 end:** 1436 passed, 90 skipped, 4 xfailed.
- **Now:** 1438 passed, 90 skipped, 4 xfailed, 0 failed (`reports/phase1b/pytest_full_after_fix_tail.txt`). The two additions are the new regression tests.
- **Live-DB tests** are skipped without a snapshot. Against the current scratch copy: 11 failing, of which 4 are fixed by C1–C8 and 7 remain (§7).
- The Phase 0 `sklearn` collection error doesn't occur in the current run (no collection errors).

### Database safeguards in place

- `729034b`: guarded re-extraction.
- `8c092b5`: test-time production guard.
- `bench/billing_check.py` and `scripts/corrections/apply_corrections.py` refuse the production DB.
- This investigation wrote nothing to production or the backups.

### Remaining risks

- The 7 live-test failures.
- F1–F5.
- About 15% of charges have no locally held text (unaudited for OCR).
- The date writer/reader contract has no CI guard yet (next step 1).
- The 2026-05-29 re-extraction's season tagging may have mis-tagged other TOU families beyond leaf-503; only leaf-503 was checked.

### Uncommitted work

- **Tracked, modified:** `src/duke_rates/parse/normalization.py`, `src/duke_rates/historical/lineage.py`, `tests/test_historical_tariff_selector.py`, `tests/test_history_lineage.py`.
- **New:** `scripts/corrections/`, `reports/phase1b/`, this report.
- **Pre-existing, never committed:**
  - Phase 0 raw outputs: `reports/phase0/*.json`, `pytest_*_raw.txt`. The `.md` summaries are committed.
  - The June ingestion scripts `scripts/ingestion/ingest_dep_dec_web_updates_2026_06.py` and `repair_dep_dec_web_update_provenance_2026_06.py`, and `scripts/backfill_dec_rs_2021_2022.py`. These are the provenance for the June 5 data changes and should be committed.
  - About 60 `reports/_*.txt` and `scripts/_*.py` scratch files from earlier sessions, and `fake.db`.

  All left untouched.
- `gitnexus_detect_changes` must run before committing; the index is 22 commits stale (`npx gitnexus analyze`).
