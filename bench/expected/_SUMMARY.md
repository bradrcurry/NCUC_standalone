# Benchmark ground truth v0: summary

26 documents (`bench/corpus.yaml`), 804 expected charge records, 114 checksum checks.

**How it was made.** Values were transcribed from the source PDFs only. Native PDFs were read through PyMuPDF word and character coordinates. Every scanned page, and every value whose text layer was ambiguous, was rendered to PNG and checked by eye. Nothing was taken from `tariff_charges`, `rider_line_items` or `rider_summary_blocks`, and no repo parser was run. The DB snapshot (opened read-only) was used only to find paths, page spans, titles and family keys.

**Unverified records: 0.**

## Conventions

- **Sign:** values keep the printed string, with parentheses converted to a minus sign: `(0.012)` becomes `-0.012`.
- **Rider summary rows:**
  - Every row is recorded, including `TOTAL` rows (`is_total=true`).
  - DEP `Annual Billing Adjustments Rider BA - Net Adjustment` rows are subtotals (`is_total=true`, `subtotal=true`). They are excluded from the TOTAL components and checked separately against the BA sub-rows. Sub-rows carry `group`.
  - Multi-column sections (HP Baseline/Incremental) carry `column`.
  - Checksums are computed per (rate_class, unit, column).
- **Extra fields beyond the base schema:** `optional` (late-payment %, SSI discount caps; scorers may exclude these), `season_text`, `phase`, `category`, `column`, `group`, `subtotal`, `sign`.
- **Extended units** (not in the base enum, flagged here): `dollars_per_occurrence` (PPS), `dollars_one_time` and `dollars_per_year` (Rider LC incentives).
- **Seasons:** the complementary season ("October - April", "November - June", "October through May") is recorded as `non_summer`. The exact printed text is in `season_text`.
- **Docs with no tariff sheet** can still contain proposed or exhibit rates. These are kept out of `charges`, under `exhibit_rates` or `non_tariff_values_mentioned`. Extracting them as tariff charges should count as a false positive.

## Per document

| doc_id | cat | charges (incl. totals) | totals/subtotals checked | checksum fails | notes |
|---|---|---|---|---|---|
| dep_leaf600_2026-06-01 | A | 135 | 27 | 0 | 11 sections. $/kW only for JAA in Demand MGS (0.92), LGS (3.03) and HP/LGS-RTP (3.03). Sections split across pages. Residential TOTAL 3.144. |
| dep_leaf600_2026-04-01 | A | 135 | 27 | 0 | Same layout. Residential TOTAL 2.108. |
| dec_leaf99_2026-06-01 | A | 90 | 8 | 0 | 6 sections, all ¢/kWh. Residential TOTAL 1.6937. |
| dec_blk307_rev63 | B | 90 | 8 | **1** | See below. The printed tariff total is wrong. |
| dec_blk304_sub1285 | B | 72 | 8 | 0 | Clean pages 3-4 only. Pages 1-2 are a letter and certificate. Pages 5-6 are a redline of the same leaf, not transcribed. |
| dep_blk13_sub1300 | B | 135 | 27 | 0 | Whole file is a redline. Post-revision (Sixth Rev, eff 2025-04-01) values are decoded from strike-line drawings. |
| dec_blk361_sub1146_fca | B | 72 | 9 | 0 | Summary pages 4-5 only. Not summary content: p1 letter, p2-3 FCA rider (redline and clean), p6-9 service list. |
| hd0029_dep_res | C | 7 | - | - | BCC $14.00. Summer 12.119¢ flat. Non-summer 12.119¢ for the first 800 kWh, then 11.119¢. Three-phase +$9.00. |
| hd7378_dep_rtoud_2014 | C | 0 (expected_zero) | - | - | Span 59-67 holds workpapers and testimony. The file contains no R-TOUD at all. |
| hd3140_dep_rtoud44 | C | 11 | - | - | Substitute for hd 3199, which is not in the snapshot. R-TOUD-44 TOU demand and energy charges, plus REPS $0.55. |
| hd7380_dec_rs_scan | C,G | 31 | - | - | 2001 scan, 4 categories x 2 seasons x 3 blocks. The OCR layer turns ¢ into a trailing 0. Values read from the image. |
| hd1875_dec_rs | C | 5 | - | - | BCC $14.00, energy 11.8504¢, SSI 10.6654¢. |
| hd0179_jaa | D | 0 (expected_zero) | - | - | Proof of publication (scanned). 10 PROPOSED values are listed separately. |
| hd7365_jaa | D | 0 (expected_zero) | - | - | JAA calculation exhibits. The Summary Rate exhibit (24 values, A+B=C holds) is under `exhibit_rates`. |
| hd5324_jaa | D | 0 (expected_zero) | - | - | Same as hd7365, but scanned. 24 exhibit values read from the image. One combined rate is negative. |
| hd1882_jaa_rev3 | D | 8 | - | - | Added as a passing JAA tariff. Exactly matches the Leaf 600 June JAA rows after converting $ to ¢. |
| hd0378_cm | E | 0 (expected_zero) | - | - | Rider CM prints no values. |
| hd0379_r28 | E | 0 (expected_zero) | - | - | Rider 28 prints no values. |
| hd7770_dec_riderlc | G | 12 | - | - | Incentive credits in a bordered table. Proposed sheet (order date blank). Clean text layer, so it is not OCR-mangled. |
| hd0396_ssr | H | 0 (expected_zero) | - | - | All prices are site-specific and not printed. |
| hd7295_car | I | 0 (expected_zero) | - | - | Letter plus rate-calculation attachment, not an order. 6 proposed CAR rates are under `exhibit_rates`. |
| hd0392_pps | J | **1** | - | - | **This negative control has a charge:** an Administrative Fee of $50 per occurrence for a customer-requested test. |
| hd0403_cei | J | 0 | - | - | Expected zero as planned. The "not to exceed 20%" fee is a cap, not a charge. |
| hd0408_rsnes | J | 0 | - | - | Expected zero as planned. |
| hd0182_jaa_order | J | 0 | - | - | A Public Staff motion for extension of time, not an order. |
| hd7819_edit4_term | J | 0 | - | - | EDIT-4 terminated 2026-06-01. |

### blk307: DEC Leaf 99 Sixty-Third Revised, eff 2025-01-01

There are 6 rate-class sections.

Page 1 (the residential page) has three sections:
1. "Residential Schedules RS, RE, ES, RT, RSTC, RETC": TOTAL **0.1125** ¢/kWh.
2. "General Service Schedules SGS, BC, LGS, TS, S, HLF, OPT-V, PG, SGSTC": TOTAL **0.2487**.
3. "Lighting Schedules PL, OL, NL": TOTAL **-1.0898**.

Page 2 has the other three:
4. "Industrial Schedules I, HLF, OPT-V, PG": TOTAL **1.2148**.
5. "Schedule HP – General Service": Baseline **0.2487**, Incremental **-0.0230**.
6. "Schedule HP – Industrial": Baseline **1.2982**, Incremental **0.0452**.

**The prior suspicion is confirmed.** The second ¢/kWh section on page 1 is General Service, with its own heading and schedule list. It is not a second residential rate period. The whole leaf has one effective date.

**The printed tariff does not add up in one place.** The HP–Industrial Baseline TOTAL is printed as 1.2982. Its components are identical to the Industrial section's and sum to 1.2148. The difference is +0.0834, as if the NPTC credit of -0.0834 had been added with the wrong sign. I re-checked this against the rendered page, so the transcription is correct and `checksum_ok=false` is the expected result. Also as printed, the HP–Industrial EE row is dated 1/1/24, where every other EE row reads 1/1/25.

### blk304: E-7 Sub 1285, Leaf 99 Fifty-Ninth Revised, eff 2024-01-01

There are 6 sections on the clean pages.

Page 3 (the residential page) has three:
1. "Residential Schedules RS, RE, ES, RT, RSTC, RETC": TOTAL **2.1812**.
2. "General Service Schedules SGS, BC, LGS, TS, S, OPT-E, OPT-V, PG, SGSTC": TOTAL **1.8812**.
3. "Lighting Schedules PL, OL, NL": TOTAL **0.4901**.

Page 4 has the other three:
4. "Industrial Schedules I, OPT-E, OPT-V, PG": TOTAL **1.7811**.
5. "HP – General Service": Baseline **1.8812**, Incremental **0.1349**.
6. "HP – Industrial": Baseline **1.7811**, Incremental **0.2018**.

There is only one residential section. All checksums pass.

### blk13: units by column header

The values in the "dollars /kW" column are only the JAA rows and their "TOTAL dollars/kW" in three sections: Demand: Medium General Service (1.12), Large General Service (1.72) and Schedule HP & Schedule LGS-RTP (1.72). Every other value is ¢/kWh. HP/LGS-RTP has two ¢/kWh columns, Baseline and Incremental.

The text layer concatenates old and new digits, which will break naive extractors. For example:
- RDM `0.000232` means old 0.000, new 0.232.
- Residential TOTAL `1.716950` means old 1.716, new 1.950.
- Dates `104/1/235` mean old 10/1/23, new 4/1/25.

## Surprises and gaps

- **No tariff sheets in the category D docs.** hd 179, 7365 and 5324, the "JAA empty-profile" docs, contain no tariff sheet at all: one is a proof of publication, two are calculation exhibits. The empty extraction there is correct behaviour, and their family attribution is misleading.
- **hd 7378 is mis-bounded.** The file has no R-TOUD content.
- **Category E has no values.** Both "single-value riders" (hd 378, 379) print no charge values.
- **One negative control is not zero.** hd 392 (PPS) carries a $50 fee.
- **Three requested items were unavailable:**
  - hd 3199 is absent from the snapshot; hd 3140 is the substitute.
  - `ncriderlc-pdf.pdf` is not in the repo; hd 7770 is used instead.
  - No pre-2000 Carolinas scan was found. hd 7380 (2001) is the oldest, and it is the better OCR-mangling sample.
- **PDFs over 3 MB are not copied.** They are referenced by `source_path`: hd7378 (3.6 MB), hd5324 (3.5 MB), hd7365 (10 MB) and hd378 (15.7 MB).
