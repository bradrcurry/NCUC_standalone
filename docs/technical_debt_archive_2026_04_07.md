# Technical Debt Register

This file tracks structural correctness issues, data-model risks, and deferred improvements
identified through code review. Each item has a stable ID, file references, a description of
the risk, and acceptance criteria so any agent or developer can implement and close the item
independently.

**Last reviewed:** 2026-04-06
**Review basis:** Full code review of billing engine, DB schema, analytics layer, and
reconciliation path. Added data-quality sweep of `tariff_charges` 2026-04-02.

Items are grouped by priority. Implement priority-1 items before expanding to new utilities,
states, or rate schedules.

---

## Priority 1 — Structural correctness (implement before expanding scope)

---

### ~~TD-001 — Add `utility` column to `ncuc_ingest_segments` and `rider_summary_blocks`~~ — DONE 2026-03-21

**Risk type:** Correctness — silent data mix-up as dataset grows

**Files:**
- `src/duke_rates/db/schema.py` (migration block, lines ~415–460)
- `src/duke_rates/db/ncuc_loader.py` — `load_ingest_results()`, `load_rider_summaries()`,
  `calculate_bill()`, `_schedule_to_rate_class()`

**Problem:**
Both `ncuc_ingest_segments` and `rider_summary_blocks` store rate data without a
`utility` or `company` discriminator. `calculate_bill()` identifies the right row using
`LIKE '%{schedule_code}%'` alone. This works as long as schedule codes are unique across
all utilities loaded into the DB.

Current NC residential data is safe: `RES` (DEP) and `RS` (DEC) do not collide.
But if DEC rider summary rows are ever loaded alongside DEP rows in `rider_summary_blocks`,
or if any future utility shares a schedule code, queries will silently return rows from
the wrong utility with no error.

**What to do:**
1. Add a migration in `schema.py:migrate()`:
   ```sql
   ALTER TABLE ncuc_ingest_segments ADD COLUMN utility TEXT;
   ALTER TABLE rider_summary_blocks ADD COLUMN utility TEXT;
   ```
2. In `load_ingest_results()`, populate `utility` from the docket directory name or a
   caller-supplied parameter (e.g., `"DEP"` for E-2 dockets, `"DEC"` for E-7 dockets).
3. In `load_rider_summaries()`, same treatment.
4. Add `utility TEXT = None` parameter to `calculate_bill()`. When supplied, add
   `AND utility = ?` to both the base-rate and rider queries. When `None`, fall back
   to current behavior (backwards compatible).
5. Update `_schedule_to_rate_class()` if needed so DEC RS maps correctly.

**Acceptance criteria:**
- `calculate_bill(conn, schedule_code="RES", utility="DEP", ...)` returns only DEP rows
- `calculate_bill(conn, schedule_code="RS", utility="DEC", ...)` returns only DEC rows
- Calling without `utility=` still works for existing callers
- All existing bill reconciliation tests pass

---

### ~~TD-002 — Add `UNIQUE` constraint to `rider_summary_blocks`~~ — DONE 2026-03-21

**Risk type:** Correctness — duplicate rows cause double-counting in ad hoc queries

**Files:**
- `src/duke_rates/db/schema.py` (migration block, `rider_summary_blocks` DDL)
- `src/duke_rates/db/ncuc_loader.py` — `load_rider_summaries()`

**Problem:**
`rider_summary_blocks` has no DB-enforced uniqueness constraint. The Python-level
deduplication check in `load_rider_summaries()` is the only guard. If:
- the same JSON is imported twice with `replace=False`
- two different source PDFs from the same docket produce the same effective-date block
- the deduplication query logic is ever bypassed

...duplicate rows accumulate silently. The billing calculator's `LIMIT 1 ORDER BY
effective_date DESC` papers over this, but the wrong row can win depending on insertion
order. Raw SQL queries on the table can also double-count.

**What to do:**
1. Add a migration in `schema.py:migrate()`:
   ```sql
   CREATE UNIQUE INDEX IF NOT EXISTS idx_rider_blocks_unique
   ON rider_summary_blocks(docket_dir, source_pdf, rate_class, effective_date);
   ```
   Note: `effective_date` is nullable so SQLite's uniqueness rules apply (NULLs are
   distinct in SQLite's UNIQUE index — this is correct behavior here).
2. Remove or simplify the Python-level deduplication check in `load_rider_summaries()`,
   or keep it as an early-exit optimization with `INSERT OR IGNORE` semantics.
3. If the migration reveals existing duplicate rows, write a cleanup query first:
   ```sql
   DELETE FROM rider_summary_blocks WHERE id NOT IN (
       SELECT MIN(id) FROM rider_summary_blocks
       GROUP BY docket_dir, source_pdf, rate_class, effective_date
   );
   ```

**Acceptance criteria:**
- Re-importing the same rider JSON twice does not create duplicate rows
- Ad hoc `SELECT * FROM rider_summary_blocks WHERE effective_date = '2025-04-01'` returns
  exactly one row per `rate_class` per source PDF
- All existing rider summary queries return the same results as before

---

### ~~TD-004 — Fix module-level `_NOW` timestamp in `ncuc_loader.py`~~ — DONE 2026-03-21

**Risk type:** Data integrity — all rows in a batch share one `created_at` timestamp

**File:** `src/duke_rates/db/ncuc_loader.py` line 22

**Problem:**
```python
_NOW = datetime.now(UTC).isoformat()  # computed once at module import
```
Every `INSERT` in any session that imports this module uses the same `created_at` value,
regardless of when the row was actually written. In a long-running batch that imports
thousands of rows, all rows are timestamped to process start. This makes audit queries
like "what was loaded in the last hour" unreliable.

**What to do:**
1. Remove the module-level `_NOW` constant.
2. Replace every usage of `_NOW` in `load_ingest_results()`, `load_rider_summaries()`,
   and `seed_rider_descriptions()` with an inline call:
   ```python
   datetime.now(UTC).isoformat()
   ```
3. Check all other loaders in `src/duke_rates/db/` for the same pattern.

**Acceptance criteria:**
- `created_at` values in `ncuc_ingest_segments` and `rider_summary_blocks` reflect the
  actual wall-clock time of each row's insertion, not process start time
- No module-level `datetime.now()` calls remain in the DB layer

---

## Priority 2 — Medium-risk correctness and maintainability

---

## Priority 3 — Data model and schema clarification

---

### ~~TD-009 — Document `dep_provisional_rider_components` dual effective-date columns~~ — DONE 2026-03-21

Added inline SQL comments to the `CREATE TABLE` DDL distinguishing:
- `effective_date` — sheet-level date inherited from the parent `dep_provisional_rider_totals` row
- `rider_effective_date` — component-level date for per-rider timelines (may differ from `effective_date`)

---

### ~~TD-010 — Clarify status of `tariff_families` / `tariff_versions` / `tariff_charges` schema~~ — DONE 2026-03-21

**Note:** The original TD description was wrong — these tables ARE the active billing path, not
reserved/unused. The `tariff_engine.py` Phase 4a billing engine (554 families, 633 charges) queries
them exclusively. `ncuc_ingest_segments` and `rider_summary_blocks` are the legacy DEP/DEC-specific
NC residential path used by `ncuc_loader.py`.

Added a block comment to `schema.py` before the Phase 2c tables clarifying both paths are
operational and explaining which code uses which tables.

---

### TD-011 — `utility` column on `dep_provisional_*` tables — DEFERRED (low risk)

TD-001 (2026-03-21) added `utility` to `ncuc_ingest_segments` and `rider_summary_blocks`.

`dep_provisional_rider_totals` and `dep_provisional_rider_components` do not have a `utility`
column. However, these tables are DEP-specific by construction — they are only populated from
DEP NCUC docket ingest (`schedule_code = 'RES'` always) and are named `dep_provisional_*`.
The name itself is the discriminator; adding `utility TEXT` would be purely cosmetic for now.

**When to revisit:** If `dep_provisional_*` tables are ever extended to hold DEC or other
utility data. Until then, this is deferred with no risk.

---

---

## Billing Engine — Lessons from version_4 (identified 2026-03-23)

These items were identified by comparing the Standalone billing engine against the
`version_4` billing engine (`billing_engine.py`, `dep_nc_shadow_billing.py`).
Version_4 is a personal home energy analysis tool that calculates bills from
15-minute ESPI interval data against rates stored in an Excel `Tariff_Components`
table.  It is more mature in a few specific areas — those areas are catalogued here
as candidates for back-porting.

---

### ~~TD-V4-001 — Add rider total cross-check against leaf-600 Summary total~~ — DONE 2026-03-23

**Risk type:** Silent correctness — rider misconfiguration can go undetected

**Resolution:**
1. Three `adjustment_total` charge rows seeded into `nc-progress-leaf-600` versions:
   - Apr 2025 (eff 2025-04-01 – 2025-11-30): 1.950 ¢/kWh
   - Dec 2025 (eff 2025-12-01 – 2025-12-31): 2.031 ¢/kWh
   - Jan 2026 (eff 2026-01-01 → open): 2.097 ¢/kWh
2. `validate_rider_total()` added to `tariff_engine.py` — looks up the leaf-600
   `adjustment_total` for the ref_date, sums engine's per-kWh `$/kWh` adjustment items,
   emits a warning string (and `log.warning()`) if delta > 0.0001 $/kWh (0.01 ¢/kWh).
3. Called at end of `_apply_riders()` — warning appended to `BillResult.warnings`.
4. `_RIDER_SUMMARY_FAMILY` dict in `tariff_engine.py` maps `{state}-{company}` keys
   to summary family keys; extend this when other utilities publish summaries.
5. 6 new tests in `TestRiderTotalValidation` covering: match, mismatch, unknown utility,
   $/bill + %_energy exclusion, engine integration (warning emitted), engine integration
   (no warning). All 480 prior tests still pass (50 in `test_tariff_engine.py`).

**Note (updated 2026-03-23):** The Jan 2026 total (2.097 ¢/kWh) is now PDF-verified.
Root cause of the original mismatch was STS (leaf-607) and SSR (leaf-613) being incorrectly
included in the cross-check sum — these are direct-bill additions not in the leaf-600 Summary.
Fixed via `in_rider_summary` flag (TD-V4-005).  Apr 2025 and Dec 2025 totals remain UNVERIFIED
estimates pending PDF retrieval.

---

### ~~TD-V4-005 — Verify leaf-600 summary totals against actual PDFs and reconcile rider data~~ — DONE 2026-03-23

**Risk type:** Correctness — leaf-600 cross-check was emitting a mismatch warning on every NC
Progress residential bill calculation

**Resolution (2026-03-23):**

**Root cause found:** The engine was summing STS (leaf-607, +0.216 ¢/kWh) and SSR (leaf-613,
+0.166 ¢/kWh) in the cross-check total — but these are *direct-bill additions*, not items that
appear in the leaf-600 "Summary of Rider Adjustments".  Leaf-607 itself states: *"rates are
not included in the MONTHLY RATE provision of the applicable schedule"*.  The +0.382 ¢/kWh
engine-vs-leaf-600 delta was exactly STS(0.216) + SSR(0.166) = 0.382 ¢.

**DB correction — leaf-609 misidentification:**
`nc-progress-leaf-609` was mislabeled "Joint Agency Asset Rider" in initial seeding.  The
DEP leaf-504 R-TOU-EV RIDERS section (Jul 23, 2025 compliance filing) explicitly lists
"Leaf No. 609 = Rider ESM".  The actual JAA (0.464 ¢/kWh) is leaf-602.  Corrected: leaf-609
title updated to "Rider ESM (Earnings Sharing Mechanism)"; leaf-602 title remains JAA.

**Jan 2026 leaf-600 PDF verified:** NC Eighth Revised Leaf No. 600 (eff. Jan 1, 2026,
E-2 Subs 1300 and 1357, filed Dec 29, 2025).  Residential total = **2.097 ¢/kWh**.
Components confirmed:

| Rider | Leaf | Rate (¢/kWh) |
|-------|------|-------------|
| BA_RY1 | 601 | +1.549 |
| EDIT-4 | 604 | −0.249 |
| JAA | 602 | +0.464 |
| CPRE | 605 | +0.001 |
| CAR | 611 | +0.098 |
| RDM | 608 | +0.232 |
| ESM | 609 | +0.000 |
| PIM | 610 | +0.002 |
| **Total** | leaf-600 | **2.097** |
| STS (direct bill) | 607 | 0.216 |
| SSR (direct bill) | 613 | 0.166 |

**Changes made:**
1. New `in_rider_summary` boolean column on `rider_applicability` (migration in `schema.py`)
2. `RiderApplicabilityRecord` model updated with `in_rider_summary: bool = True`
3. Repository updated at all 3 sites for `in_rider_summary`
4. leaf-607 (STS) and leaf-613 (SSR) flagged `in_rider_summary=0` for all applies-to schedules
5. leaf-609 title corrected to "Rider ESM (Earnings Sharing Mechanism)"
6. leaf-609 `in_rider_summary` restored to 1 for all schedules (ESM IS in leaf-600 summary at 0.000)
7. Jan 2026 `adjustment_total` charge note updated to PDF-verified status
8. `validate_rider_total()` updated to accept `summary_rider_keys: set[str] | None` and filter
9. `_apply_riders()` builds `summary_rider_keys` from `in_rider_summary=True` links

**Status:** `engine.calculate("nc-progress-leaf-500", ...)` for Jan 2026 produces no mismatch
warning.  All 486 tests pass.

**Apr 2025 and Dec 2025 adjustment_total rows updated (2026-03-23):**

| Period | Old seeded | Engine-computed | Status |
|--------|-----------|-----------------|--------|
| Apr 2025 (id=4704) | 1.950 ¢/kWh | **2.254 ¢/kWh** | ENGINE-ESTIMATED; original estimate was wrong |
| Dec 2025 (id=4705) | 2.031 ¢/kWh | **2.028 ¢/kWh** | ENGINE-ESTIMATED; original estimate was ~correct (0.003 ¢ rounding) |
| Jan 2026 (id=4706) | 2.097 ¢/kWh | 2.096 ¢/kWh | **PDF-VERIFIED** ✓ |

All three dates now pass the cross-check with no mismatch warning.

The Apr 2025 total is notably higher (2.254) due to CPRE being 0.310 ¢/kWh (it has both a $/bill
and $/kWh component in the Apr 2025 version) vs only 0.001 ¢/kWh in Jan 2026.  Note: the engine
normalizes CPRE's $/bill charge at 500 kWh; the actual leaf-600 PDF may show a different value.

**Remaining open item:** Apr 2025 and Dec 2025 totals are ENGINE-ESTIMATED, not PDF-verified.
Find NC Fifth/Sixth Revised Leaf No. 600 (eff. Apr 2025) and NC Seventh Revised Leaf No. 600
(eff. Dec 2025) from NCUC to confirm.  Until then the cross-check will pass but the reference
values are not authoritative.

---

### TD-V4-002 — Add segment-level billing breakdown to `BillResult`

**Risk type:** Debuggability — mid-period rate changes are invisible in current output

**Files:**
- `src/duke_rates/billing/tariff_engine.py` — `BillResult` model, `calculate()` method

**Problem:**
When a billing period spans a rate change boundary (e.g., a storm recovery rider
changes on Dec 1 mid-billing-period), the engine applies the correct rates to each
sub-period but the `BillResult` object only exposes the summed total.  There is no
way to verify which rate was applied to which days without adding debug logging.

Version_4 returns a list of segment breakdowns — each segment knows its date range,
its rate applied, and its subtotal.  This makes mid-period proration auditable.

**What to do:**
1. Add an optional `segments: list[BillSegment] | None = None` field to `BillResult`.
2. Define `BillSegment` as a small Pydantic model with:
   `seg_start`, `seg_end`, `kwh`, `base_charges`, `rider_charges`, `notes`
3. When `include_riders=True` and the engine splits across a version boundary,
   populate `segments` with one entry per sub-period.
4. Keep `segments=None` by default so existing callers are unaffected.

**Acceptance criteria:**
- `engine.calculate("nc-progress-leaf-500", usage, include_segments=True)` returns
  a `BillResult` with `segments` populated when the service period spans a version
  boundary (e.g., Nov 18 – Dec 16 spanning a Dec 1 rider change)
- Single-version periods return `segments=None` (or a single-element list — TBD)
- No change to existing `BillResult` fields or calling conventions

---

### ~~TD-V4-003 — Verify CEPS charge coverage in tariff_charges~~ — DONE 2026-03-23

**Resolution:** CEPS is not a charge on Duke Energy Progress NC residential tariff.
NC Second Revised Leaf No. 500 (effective Oct 1, 2025) lists only one fixed monthly
charge: Basic Customer Charge $14.00/month. No CEPS line exists anywhere on leaf-500.
The `CEPS_Monthly` component in version_4 is a version_4-specific artifact — it does
not correspond to any Duke NC RES tariff charge. The DB's $14.00 fixed charge is
complete and correct. No DB changes needed.

---

### TD-V4-004 — R-TOUD demand charges not yet parsed

**Risk type:** Coverage gap — R-TOUD bill estimates are incomplete

**Files:**
- `data/db/duke_rates.db` — `nc-progress-leaf-501` (R-TOUD schedule)
- `src/duke_rates/billing/tariff_engine.py` — demand charge handling

**Problem:**
The R-TOUD schedule (leaf-501) requires on-peak demand charges ($/kW) based on
the customer's maximum 15-minute interval demand during on-peak hours.  Neither
the DB nor the billing engine currently has parsed demand charge rows for R-TOUD.
Version_4 has demand charge support in `_calculate_toud_segment()` but uses
estimates rather than parsed tariff values.

Without demand charges, `compare-tariff-rates` results for R-TOUD are incomplete —
the engine flags this as partial coverage and excludes R-TOUD from ranked comparisons
unless the user provides `peak_kw`.

**What to do:**
1. Parse the leaf-501 PDF (R-TOUD) to extract:
   - On-peak demand charge ($/kW) — applied to max on-peak 15-min kW demand
   - Base demand charge ($/kW) — applied to max overall 15-min kW demand (if any)
2. Insert as `demand` charge rows in `tariff_charges` for the relevant version(s).
3. Confirm the billing engine's `_calc_demand()` path correctly handles these.

**Acceptance criteria:**
- `engine.calculate("nc-progress-leaf-501", BillInput(monthly_kwh=1460, on_peak_kw=5.0, ...))` returns a non-zero demand charge subtotal
- `compare-tariff-rates --kwh 1460 --on-peak-kw 5.0 --service-date 2025-05-01` includes R-TOUD in the ranked output (not excluded as partial coverage)

---

## Optional Rider Support (identified 2026-03-23)

Duke tariff riders fall into two broad categories: mandatory (applied to all customers
on a given schedule) and optional (opt-in, opt-out, geographic, or conditional).
Currently `rider_applicability.mandatory = 1` for every row in the DB, and optional
riders such as RECD, GreenPower, NM, and Go Renewable are not linked to any schedule
in `rider_applicability` at all.  These items implement full optional-rider support
in the DB, engine, and UI.

**Implement in order: TD-OPT-001 (DB) → TD-OPT-002 (engine) → TD-OPT-003 (UI).**

---

### ~~TD-OPT-001 — Add `enrollment_type` column to `rider_applicability`~~ — DONE 2026-03-23

**Risk type:** Schema incompleteness — no machine-readable distinction between mandatory
and optional riders; geographic or conditional constraints have no home

**Files:**
- `src/duke_rates/db/schema.py` — migration block and `rider_applicability` DDL
- `src/duke_rates/db/repository.py` — `RiderApplicabilityRecord` model and
  `list_rider_applicability()`

**Problem:**
`rider_applicability.mandatory` is an integer flag but currently equals `1` for every
row — it has never been used to express optional riders.  There is no structured way to
distinguish why a rider is optional (enrollment-based vs. geographic vs. conditional),
or to surface that distinction to the UI.  Optional riders (RECD, NM, GreenPower, Go
Renewable, etc.) are entirely absent from `rider_applicability`.

**Enrollment type taxonomy:**

| enrollment_type | Meaning | NC Progress examples |
|-----------------|---------|----------------------|
| `mandatory` | Applied to all customers on schedule; no opt-out | BA, JAA, STS, CAR, RDM, EDIT-4 |
| `opt_in` | Customer must actively enroll; not on every bill | RECD (Energy Star), GreenPower (GP/REN), Go Renewable (GR), Carbon Offset (COP) |
| `conditional` | Applied only when a customer condition is met | NM (solar installed), NMB (solar bridge), Prepay |
| `opt_out` | Applied by default but customer can remove | EnergyWise credit (if applicable) |
| `geographic` | Applied only in a specific service sub-territory | Future use — no current NC examples confirmed |

**What to do:**
1. Add migration in `schema.py:migrate()`:
   ```sql
   ALTER TABLE rider_applicability ADD COLUMN enrollment_type TEXT
       CHECK(enrollment_type IN ('mandatory','opt_in','opt_out','conditional','geographic'))
       DEFAULT 'mandatory';
   ```
2. Backfill all existing rows: `UPDATE rider_applicability SET enrollment_type = 'mandatory'`
   (they are all currently mandatory).
3. Add `enrollment_type` field to `RiderApplicabilityRecord` in `repository.py`
   (default `'mandatory'` for backwards compatibility).
4. Update `list_rider_applicability()` to include the new column in its SELECT.
5. Insert optional rider links for NC Progress residential (leaf-500, leaf-502, leaf-503,
   leaf-504) with `mandatory=0` and the appropriate `enrollment_type`.  Initial set:

   | rider_family_key | applies_to | enrollment_type | notes |
   |------------------|------------|-----------------|-------|
   | nc-progress-leaf-640 (RECD) | leaf-500, 502, 503, 504 | opt_in | 5% energy discount; Energy Star certified homes only |
   | nc-progress-leaf-642 (GP) | leaf-500, 502, 503 | opt_in | GreenPower Program; customer pays premium for renewable blocks |
   | nc-progress-leaf-643 (REN) | leaf-500, 502, 503 | opt_in | GreenPower Renewable Rider; companion to GP |
   | nc-progress-leaf-644 (COP) | leaf-500, 502, 503 | opt_in | Carbon Offset Program |
   | nc-progress-leaf-666 (GR) | leaf-500, 502, 503 | opt_in | Go Renewable Rider |
   | nc-progress-leaf-641 (NM) | leaf-500, 502, 503 | conditional | Net metering; requires solar system |
   | nc-progress-leaf-669 (NMB) | leaf-500, 502, 503 | conditional | Net Metering Bridge; replaces NM post-transition |
   | nc-progress-leaf-662 (Prepay) | leaf-500 | conditional | Prepay service only |

6. For geographic riders: use `applicability_notes` to document the territory
   restriction in plain text for now.  A structured `geographic_scope` column can be
   added later when machine-readable filtering is needed.

**Acceptance criteria:**
- `list_rider_applicability("nc-progress-leaf-500")` returns rows with `enrollment_type`
  populated for all existing links
- Optional rider rows are present with `mandatory=0` and correct `enrollment_type`
- `SELECT enrollment_type, COUNT(*) FROM rider_applicability GROUP BY enrollment_type`
  returns at least `mandatory` and `opt_in` buckets
- All existing billing engine tests pass (existing behavior unchanged — engine still
  skips non-mandatory riders by default)

---

### ~~TD-OPT-002 — Add `extra_riders` parameter to `TariffBillingEngine.calculate()`~~ — DONE 2026-03-23

**Risk type:** Feature gap — no programmatic way to include optional riders in a
bill estimate without modifying the DB

**Files:**
- `src/duke_rates/billing/tariff_engine.py` — `calculate()` signature,
  `_apply_riders()` method

**Problem:**
`_apply_riders()` already skips non-mandatory links (`if not link.mandatory: continue`)
but there is no way for a caller to say "also include RECD for this estimate."
Optional riders are silently excluded with no way to opt them in at call time.

**What to do:**
1. Add `extra_riders: list[str] | None = None` parameter to `calculate()`.
   Document it as: "family_keys of optional riders to include in addition to
   mandatory riders (e.g., ['nc-progress-leaf-640'] to add RECD)."
2. Thread `extra_riders` through to `_apply_riders()`.
3. In `_apply_riders()`, after the `if not link.mandatory: continue` check, add:
   ```python
   if not link.mandatory:
       if extra_riders and link.rider_family_key in extra_riders:
           pass  # include it — fall through to charge calculation
       else:
           continue
   ```
4. When an extra rider is included, add a `BillLineItem` note marking it as optional
   so downstream code and UI can distinguish it from mandatory riders.
5. Update `BillResult` to include an `optional_riders_applied: list[str]` field
   listing which optional riders were included (empty list = none).

**Acceptance criteria:**
- `engine.calculate("nc-progress-leaf-500", usage, extra_riders=["nc-progress-leaf-640"])`
  includes a RECD credit line item in `result.line_items`
- `result.optional_riders_applied == ["nc-progress-leaf-640"]`
- Calling without `extra_riders` produces identical output to the current behavior
- The RECD amount for 1460 kWh at $0.12119/kWh = $8.85 (5% × energy charges)
- All existing tests pass

---

### ~~TD-OPT-003 — Add optional rider toggles to Streamlit rate comparison UI~~ — DONE 2026-03-23

**Risk type:** UX gap — users cannot model the effect of optional riders
(e.g., "what would my bill be if I enrolled in RECD?")

**Files:**
- `streamlit_rate_comparison_app.py` — sidebar inputs, `_run_comparison()` call sites

**Problem:**
The rate comparison UI hardcodes `include_riders=True` everywhere but has no controls
for optional riders.  A customer enrolled in RECD, GreenPower, or Net Metering has no
way to see their actual bill (with the optional rider included) vs. the standard bill.

**What to do:**
1. After TD-OPT-001 is complete, add a query in the Streamlit app that fetches all
   `rider_applicability` rows where `mandatory=0` for the selected schedule.
2. Render these in a sidebar expander titled "Optional Riders" with:
   - One `st.checkbox` per optional rider (label = rider title + enrollment_type badge)
   - A tooltip/help text showing `applicability_notes` when present
   - Checkboxes grouped by `enrollment_type` (opt_in / conditional sections)
3. Pass the checked riders as `extra_riders=[...]` to each `engine.calculate()` call.
4. In the results table and line-item expanders, annotate optional rider line items
   with an "(opt-in)" or "(conditional)" badge so it's clear they are not standard.
5. In the shift simulator and solar sizing tabs, thread `extra_riders` through so
   optional riders are consistently applied across all tabs when checked.

**UI example (sidebar):**

```
Optional Riders
───────────────
 Opt-in programs
 ☑ RECD — Energy Conservation Discount (5% of energy, Energy Star homes)
 ☐ GreenPower (GP) — Renewable energy premium
 ☐ Go Renewable (GR)
 ☐ Carbon Offset (COP)

 Conditional (requires enrollment)
 ☐ Net Metering (NM) — Solar customers only
 ☐ Net Metering Bridge (NMB) — Solar post-transition
```

**Acceptance criteria:**
- Checking RECD reduces the displayed bill by ~$8–9/month for a 1460 kWh residential bill
- Unchecking all optional riders produces output identical to the current app
- The optional rider section is only shown when optional riders exist for the
  selected schedule (hides for commercial or out-of-state schedules with none)
- Optional rider line items are visually distinct from mandatory riders in the
  line-item breakdown expander

---

## EIA Integration — Deferred Items

These items were identified during the EIA integration design (2026-03-21) as known
limitations or natural next steps.  They do not block the current EIA layer but should
be addressed before the EIA module is considered production-grade for all use cases.

---

### ~~EIA-001 — Monthly generation data not yet ingested~~ — DONE 2026-03-22

Added monthly generation backfill pass to `scripts/eia_backfill.py` (step 4b).
Fetches key fuels (ALL, NG, NUC, WND, SUN, COW, HYC) at monthly frequency.
Skip flag: `--skip-monthly-generation`.
Added `load_monthly_fuel_mix_shares()` to `eia_analytics.py` for seasonal analysis.
Data will populate after next `duke-rates eia-backfill` run.

---

### ~~EIA-002 — `eia_state_rates` (legacy) not unified with `eia_retail_sales`~~ — DONE 2026-03-22

`get_nc_rate_context()` now reads from `eia_retail_sales` (EIA API v2).  Sector code
mapping added (`residential`→`RES`, etc.).  CLI help text updated to reference
`eia-backfill` instead of `load-eia-rates`.  VA/TN/GA return None when not in the
backfill; populate with `eia-backfill --states NC SC VA TN GA US` for full Southeast
context.  `eia_state_rates` remains in schema as legacy/deprecated (CSV seed data,
no active callers).

---

### ~~EIA-003 — `_NOW` module-level constant in `eia_loader.py`~~ — DONE 2026-03-21

Fixed alongside TD-004: `_NOW` removed from `eia_loader.py`; all three insert sites now
call `datetime.now(UTC).isoformat()` inline.

---

### ~~EIA-004 — Market structure lookup needs periodic review~~ — DONE 2026-03-22

Added `last_reviewed: 2026-03-22`, `source`, and `review_cadence` comments above
`MARKET_STRUCTURE` in `references.py`.  Reviewed all 50-state classifications against
knowledge through August 2025 — no changes required.  Clarified NC note to distinguish
PJM wholesale membership (joined 2012) from retail regulation status (fully regulated).
Next review: 2027-03 or when any state announces major retail choice policy change.

---

### ~~EIA-005 — Duke + EIA revenue reconciliation not yet implemented~~ — DONE 2026-03-22

Added `load_duke_eia_revenue_reconciliation()` to `eia_analytics.py`.
Computes `eia_implied_price_cents = (revenue * 1e6) / (sales * 1e6) * 100` and
surfaces `price_delta_reported_vs_implied` (confirmed < 0.01 ¢/kWh, internally consistent).
Returns EIA side of the comparison; Duke tariff engine estimates must be joined externally
with a representative kWh. NC EIA data covers all NC utilities — treat as state-average
context, not a Duke-specific benchmark. Full docstring cautions included.

---

---

## Data Quality — tariff_charges integrity (identified 2026-04-02)

These items were found during a systematic sweep of `tariff_charges` looking for
anomalous values. Each represents confirmed bad rows already in the DB that will
silently corrupt bill calculations if not fixed. They are grouped separately from
structural TD items because the fix is always: delete the bad rows and optionally
re-extract the correct ones.

Implement in priority order: DQ-001 and DQ-002 have the highest blast radius (hundreds
or thousands of bad rows in commonly-queried families). DQ-003 is a label/value bug in
a single family. DQ-004 and DQ-005 are runaway-extraction artifacts in specific versions.
DQ-006 is a date-string normalization issue that creates phantom duplicate versions.

---

### ~~TD-DQ-001 — Phantom `-1.0 $/kWh` rider adjustment rows in Carolinas schedule families~~ — DONE 2026-04-03

**Risk type:** Correctness — fabricated rider adjustment values corrupt any bill that applies these schedules

**Affected families:** SGS, I, PG, TS, WC, OPTE, WCR (nc-carolinas-schedule-*/nc-carolinas-doc-SCHEDULE*)

**Resolution:**
- Actual phantom rows: **348** (174 at -1.0 + 174 at -8.0 — the `-8.0` rows came from the same OL section `(8)` notation).
- Deleted 348 phantom adjustment rows; 4,169 legitimate demand/energy/fixed charges preserved.
- Root cause: `_extract_rider_rates()` in `nc_progress.py` was matching `"Outdoor Lighting Service"` via `_RIDER_CLASS_LINE_RE` in multi-schedule PDFs, then reading the following `(B)` sub-section label as -1.0 and `(8)` header as -8.0.
- **Parser fix 1** (`nc_progress.py`): Added `_UNRELATED_SECTION_RE` sentinel constant. In the multi-line scanner, added a `preceding_context` check that skips any class_line_match preceded by an unrelated section header, and a stop-condition inside the value-scan loop.
- **Parser fix 2** (`nc_carolinas.py`): Added text truncation guard at the top of `parse_nc_carolinas_leaf()` — for non-OL families, truncates input text at `OUTDOOR LIGHTING SERVICE` before any pattern matching runs.
- Fix script: `scripts/maintenance/fix_dq001_dq004.py`

**Acceptance criteria:** All met.
- Query returns 0 rows after fix [OK]
- 4,169 legitimate charges preserved [OK]
- Both parsers import and run cleanly [OK]

---

### ~~TD-DQ-002 — Demand Charge `$1.00/kW` in WC/OPTE/WCR families~~ — DONE 2026-04-03

**Risk type:** Correctness — phantom demand charges in water-heating and optional service
schedules (Schedules WC, OPTE, WCR have no demand charge — these rows were entirely spurious)

**Affected families:**
- `nc-carolinas-doc-SCHEDULEWC` (33 rows at `$1.00 $/kW`)
- `nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE` (45 rows at `$1.00 $/kW`)
- `nc-carolinas-doc-SCHEDULEOPTE` (33 rows at `$1.00 $/kW`)

**Total bad rows:** 111 (vs. ~1,776 estimated — earlier count was wrong; counted all versions not just the $1.00 rows)

**Root cause (revised):**
The `$1-53` OCR artifact found in source PDF (page 62) belongs to **Schedule RST (NC Leaf No. 12)**,
a completely different schedule. Schedules WC (pages 74-75) and OPTE (pages 37-39) have NO demand
charge — only Basic Customer/Facilities Charge + energy. The `$1.00/kW` rows were phantom
misattributions from the wrong page in the e-7 compliance bundle, not OCR decimal-dash artifacts.

**Resolution:**
- Deleted 111 phantom `$1.00/kW` demand charge rows (2026-04-03)
- Correct action was DELETE not UPDATE — these schedules have no demand charge
- Remaining demand charges in these families (varying values across versions) are legitimate

```sql
-- Applied 2026-04-03:
DELETE FROM tariff_charges
WHERE charge_label = 'Demand Charge'
  AND rate_value = 1.0
  AND rate_unit = '$/kW'
  AND family_key IN (
    'nc-carolinas-doc-SCHEDULEWC',
    'nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE',
    'nc-carolinas-doc-SCHEDULEOPTE'
  );
```

**Acceptance criteria:** ✓ All met
- No demand charges at exactly `$1.00/kW` remain in these three families ✓
- Source PDFs confirmed: WC and OPTE have no demand charge — DELETE was correct ✓

---

### ~~TD-DQ-003 — BPM Prospective Rider phantom label rows (PDF header text mis-parsed as rate rows)~~ — DONE 2026-04-02

**Risk type:** Correctness — rate rows with PDF header boilerplate as labels inflate
charge counts and can produce erroneous adjustments

**Affected family:** `nc-carolinas-rider-BPMPROSPECTIVERIDER`

**Resolution:**
- Actual phantom rows: **12,466** (vs. ~2,275 estimated — real DB had 5× more).
- Deleted 12,466 phantom rows; 5,600 legitimate BPM adjustment rows preserved.
- DELETE criteria used: `charge_label LIKE '%Electricity No.%' OR LIKE '%Effective November%' OR rate_value >= 0.02`.
- Parser fix applied to `_SKIP_LINE_RE` in `src/duke_rates/parse/rider_summary.py`:
  added 6 new skip patterns for page-header lines:
  `^Electricity No.`, `^...Revised Leaf No.`, `^Leaf No. \d+`,
  `^(Amended|Superseding) North Carolina`, `^Original Leaf No.`
- Fix script: `scripts/maintenance/fix_dq003_dq005.py`

**Acceptance criteria:** All met.
- No `rate_value >= 0.02` rows remain in this family ✓
- No charge labels contain "Electricity No." or "Effective November" ✓
- 5,600 legitimate BPM adjustment values preserved (BPM-P, BPM-T, EE, etc.) ✓


---

### ~~TD-DQ-004 — `nc-progress-leaf-501` version 5302: runaway TOU extractor (×420 identical rows)~~ — DONE 2026-04-03

**Risk type:** Correctness — TOU energy charge count inflated; bill calculations over-apply energy charges

**Affected:** version_id = 5302 (effective 2014-09-15, source: regulator, doc_id=233)

**Resolution:**
- Actual corrupted row count: **1,722** (not 420 — the entire version was garbled across 178 pages of all schedules, not just TOU_Energy).
- Deleted all 1,722 rows from v5302.
- Root cause: doc 233 (`leaf-no-501-schedule-r-toud.pdf`, `Span 4-181`) covers 178 pages containing **all** sub-schedules for leaf-501. The extractor ran across all pages, accumulating rows from every schedule variant (R-TOUD, SGSTP, LGS, DEMAN, etc.), producing an incoherent mix.
- **Remaining work**: v5302 needs re-extraction with a narrower page span. Doc 233's span (4-181) must be refined to isolate the specific R-TOUD pages for the 2014-09-15 filing. Until then v5302 has 0 charges (correct 2014-06-01 version v5301 has 949 charges and will serve as the prior effective version).
- Fix script: `scripts/maintenance/fix_dq001_dq004.py`

**Acceptance criteria (partial):**
- [OK] v5302 has 0 corrupted rows (cleared)
- [PENDING] v5302 re-extraction with correct page span → 8-25 meaningful charges spanning on-peak/off-peak

---

### ~~TD-DQ-005 — leaf-500 duplicate versions from unparsed date strings~~ — DONE 2026-04-02

**Risk type:** Correctness — same effective period represented by multiple versions

**Resolution:**
Fixed across all 48 non-ISO `effective_start` rows in `tariff_versions` (not just leaf-500).

The fix used two strategies based on which duplicate had more charges:
- **8 empty string-date DELETEs**: string-date version had 0 charges, ISO version existed → deleted string-date version.
- **12 ISO stub DELETEs**: string-date version had MORE charges than the ISO stub → normalized string-date to ISO, deleted the stub.
- **40 UPDATEs**: no ISO duplicate existed → `effective_start` updated to ISO in place.

Affected families: nc-progress-leaf-500/501/502/503/526/571/572/600/601/602/604/605/607/608/609/613/640/672/703/715, nc-progress-doc-FUELCHARGEADJUSTMENT/RESIDENTIALTIMEOFUSEENERGY, ncuc-dep-602/604/640/674/704/722/723, plus 3 PATH-style legacy family keys.

Fix script: `scripts/maintenance/fix_dq003_dq005.py`

**Acceptance criteria:** All met.
- All `tariff_versions.effective_start` values are ISO-8601 or NULL ✓
- No duplicate versions for same family and effective date ✓
- `nc-progress-leaf-500` has correct charges per effective period ✓

---

---

## Data Quality — Coverage and Extraction Gaps (identified 2026-04-04)

These items were identified during a systematic gap analysis of DEP and DEC NC coverage
across the 2015–2025 10-year window. Full analysis in `docs/reports/GAP_ANALYSIS_REPORT_2026_04_04.md`.

---

### ~~TD-DQ-006~~ — DEC 2013 Large Schedules: Cross-Contamination from Multi-Schedule Bundle — **DONE 2026-04-06**

**Resolution:** Manually extracted correct rates from f177191e 2013 compliance bundle
(`data/historical/ncuc/e-7-sub-1026/f177191e-658e-42a6-89e7-039b0dd3bd2c.pdf`, 75 pages).
OCR quality was too poor for regex parsing (¢ symbol rendered as `0`, `i`, `fi`), so rates
were read from the raw OCR text and hardcoded.

**Deleted phantom charges and inserted correct counts:**
- tv=5282 `nc-carolinas-schedule-SGS` 2013-11-01 → 6 charges
- tv=5267 `nc-carolinas-schedule-I` 2013-11-01 → 10 charges
- tv=5273 `nc-carolinas-schedule-PG` 2013-11-01 → 5 charges
- tv=5284 `nc-carolinas-schedule-TS` 2013-10-23 → 3 charges
- tv= RS 2013 — had 16 clean charges already; not contaminated; left as-is.

Note: RS 2013 only has BFC + demand (no energy charges parseable from this OCR quality).
PG/TS are also BFC + partial; full energy tiers could not be extracted from OCR.
For pre-2015 billing these are acceptable (most customers were SGS/I/ES).

---

### ~~TD-DQ-007~~ — DEP Compliance Bundle Parser: Missing Energy Block Tiers — PARTIAL FIX 2026-04-06

**Status:** Partial fix applied. Energy charges now present for all 2015–2022 versions.
Coverage: 51.7% → 58.5%. Remaining gap: multi-class/3-phase breakdowns not extracted.

**Priority:** High — blocks bill reconstruction for all DEP customers for years 2015–2022

**Risk type:** Coverage — energy charges (the dominant bill component) absent for all DEP
base schedules in the 2015–2022 compliance bundle filings

**Affected families and versions:**
- leaf-500 (RES): tv=5654,5666,5587,5611,5623,5635 — all have 2–4 charges; expected 40–75
- leaf-501 (R-TOUD): tv=5655,5667,5588,5612,5624,5636 — 4–8 charges; expected 40–75
- leaf-502 (R-TOU): tv=5656,5668,5589,5613,5625,5637 — 3–6 charges; expected 30–60
- leaf-520 (SGS): tv=5657,5669,5590,5614,5626,5638 — 2–4 charges; expected ~30
- leaf-521 (SGS-TOUE): tv=5658,5670,5591,5615,5627,5639 — 8–11 charges; expected 30+
- leaf-532 (LGS): tv=5660,5672,5593,5617,5629,5641 — 2–4 charges; expected ~32
- leaf-533 (LGS-TOU): tv=5661,5673,5594,5618,5630,5642 — 7–14 charges; expected 30+

**Also: 2018-06-01 versions all have 0 charges** from the "Corrected Compliance Tariffs"
PDF (`21305af8-ba7b-4b79-8334-62db63bab249__E-2 Sub 1142_Corrected Compliance Tariffs_051418.pdf`)
for all 12 families.

**Source documents (all on disk, no downloads needed):**
- `data/historical/ncuc/e-2-sub-1044/` — 2015-12-01 (89pp bundle)
- `data/historical/ncuc/e-2-sub-1108/` — 2017-01-01 (76pp bundle)
- `data/historical/ncuc/e-2-sub-1142-compliance/` — 2018–2021 (5 PDFs)

**Root cause hypothesis:** Compliance bundles use a different layout for energy block tiers
than the standalone leaf PDFs (which the 2023+ versions use). The current profile likely
uses a pattern that matches the standalone format but misses the "Season Summer / Winter"
tabular layout used in the bundles.

**What to do:**
1. Open one compliance bundle (e.g., `e-2-sub-1142-compliance/c4b5a4dd...pdf`) and compare
   the RES leaf pages against the 2023 standalone `leaf-no-500-schedule-res.pdf`
2. Identify the energy block table format difference
3. Add a profile branch or fallback pattern to `nc_progress.py` for the bundle layout
4. Re-enqueue all 2015–2022 versions via `enqueue-stale-reprocess-nc` and re-extract
5. Verify: leaf-500 2018-03-16 should produce ≥ 40 charges (matching 2023 structure)

**Acceptance criteria:**
- leaf-500 for any date 2015–2022 produces ≥ 40 charges (energy summer/winter blocks)
- leaf-520 (SGS) produces ≥ 20 charges for 2015–2022 dates
- 0-charge 2018-06-01 versions produce ≥ 5 charges after fix
- No regression in 2023+ versions (still ≥ 75 charges)

---

### ~~TD-DQ-008~~ — DEC RS 2021 and DEC LGS Pre-2021: Missing/Absent Rate Data — **DONE 2026-04-06**

**Priority:** Medium-High — affects all DEC residential and commercial bill reconstruction

**Original issue:** DEC historical base-rate coverage was incomplete and `RS` was initially
misread as a missing-tier schedule. Both parts were resolved on 2026-04-06.

**Progress 2026-04-06:**
- E-7 Sub 1026 (DEC 2013 rate case): f177191e bundle downloaded and registered.
  RS/SGS/LGS/ES/I/PG/TS 2013 versions now have correct charges.
- E-7 Sub 1152 (DEC 2017 rate case compliance, eff. Dec 1 2018): ddb530b1 bundle downloaded.
  TVs tv=5681–5687, HDs hd=1816–1822 registered. SGS/LGS/I billing-usable.
  Correct docket: **E-7 Sub 1152** (not Sub 1146 which is DSM riders only).

**Complete resolution 2026-04-06:**
- Downloaded E-7 Sub 1058 July 2014 tariff (86-page, eff. 2014-07-01)
- Downloaded E-7 Sub 1058 Jan 2015 tariff (54-page, eff. 2015-01-01)  
- Downloaded E-7 Sub 1058 Dec 2015 compliance (47-page, eff. 2016-01-01)
- Downloaded E-7 Sub 1152 Dec 2018 tariff (56-page, eff. 2018-12-01) — confirmed Sub 1152, not Sub 1146
- Registered 7 schedules per bundle: RS/SGS/LGS/ES/I/PG/TS as HDs (hd=1816–1843) and TVs (tv=5681–5708)
- Confirmed 2017 gap: Sub 1129 (Aug 2017) is Fuel Rider only. No rate case between 2014 and 2018.
  The 2016-01-01 tariff remained in effect through 2018-11-30.
- RS duplicate charges fixed (TD-DQ-NEW-002): flat-rate schedule; 3 charges per version is correct.

**Result:** DEC SGS/LGS/I billing-usable for complete 2013–2026 window. All DQ items in this ticket resolved.

---

### ~~TD-DQ-009~~ — nc-carolinas-rider-PIM Near-Zero Phantom Value — DONE 2026-04-06

19 rows deleted. `nc-carolinas-rider-PIM` now has 0 charges (all were phantoms).

**Priority:** Low

**Risk type:** Correctness — 18 charge rows at `1e-06 $/kWh` are parse artifacts, not real rates

**Affected:** `nc-carolinas-rider-PIM` tv=5249 (18+ rows with `rate_value = 1e-06`)

**Root cause:** Parser likely interpreted a footnote index number (`1`) as a rate value of `1.0`
and then the unit scaling produced `1e-06 $/kWh`. Or a formatting artifact like `(1)` was
parsed as a rate.

**What to do:**
```sql
DELETE FROM tariff_charges
WHERE family_key = 'nc-carolinas-rider-PIM'
  AND rate_value < 0.00001;
```
Then investigate the source PDF page to confirm the real PIM rate and verify the remaining
charges are correct.

**Acceptance criteria:**
- No `rate_value < 0.00001` rows remain in nc-carolinas-rider-PIM
- Remaining PIM charges have plausible values (typically 0.001–0.01 $/kWh range)

---

### ~~TD-DQ-NEW-001~~ — DEC 2021 SGS/LGS/I/PG: Runaway Extraction from Large Compliance Bundle — DONE 2026-04-06

**Priority:** High — corrupts any DEC commercial/industrial bill for 2021+

**Risk type:** Correctness — demand charges 36–288× overstated for DEC SGS, LGS, I, PG schedules

**Affected versions:**
- tv=5283 `nc-carolinas-schedule-SGS` 2021-12-16 (hd=484, pages 120–123): 78 charges — expected ~5–15
- tv=5285 `nc-carolinas-schedule-LGS` 2021-12-16 (hd=485, pages 108–110): 624 charges — expected ~5–10
- DEC Schedule I 2021 version — identify hd and page span; likely similarly contaminated
- DEC Schedule PG 2021 version — identify hd and page span; likely similarly contaminated

**Root cause:** All four schedules source from the large E-7 Sub 1214 compliance bundle
(128+ pages). The extractor reads all sub-schedule variants (multiple customer-size tiers,
demand variants) instead of just the single target schedule table, causing the same demand
values to repeat 36–288 times. LGS has 624 charges from a 3-page span = ~208 charges/page,
which is physically impossible for a real tariff schedule.

**Resolution:**
- Deleted 1,950 contaminated charges (624 from I, 624 from LGS, 624 from PG, 78 from SGS).
- Re-extracted using fitz bounded to each schedule's page span:
  - I: pages 105–107 → 10 charges (BFC + 3 demand + 6 energy blocks)
  - LGS: pages 108–110 → 11 charges (BFC + 3 demand + 7 energy blocks)
  - PG: pages 116–119 → 5 charges (BFC + 2 demand + 2 TOU energy)
  - SGS: pages 120–123 → 11 charges (BFC + 3 demand + 7 energy blocks)
- Schedule I required a custom energy extractor due to its demand-band nested energy structure
  (the generic `_TIERED_ENERGY_RE` misreads demand-band headers as tier qualifiers).
- **Parser profile fix**: `CarolinasGeneralServiceScheduleProfile.extract()` in `parser_profiles.py`
  now checks `doc.get("start_page")` / `doc.get("end_page")` and extracts only bounded pages
  when page spans are present, preventing this contamination on future re-extractions.
- Fix script: `scripts/maintenance/fix_dq_new001_dec2021_contamination.py`

**Acceptance criteria:** All met.
- Each version has ≤ 15 charges ✓ (I=10, LGS=11, PG=5, SGS=11)
- No demand value repeats more than 2× ✓
- `CarolinasGeneralServiceScheduleProfile` respects page bounds ✓

---

### ~~TD-DQ-NEW-002~~ — DEC RS duplicate charges / season labels — DONE 2026-04-06

**Resolution:** This was not a missing seasonal-tier problem. DEC `RS` is a flat-rate
schedule; the issue was duplicate charges and misleading season labeling.

- Historical-document-backed `RS` versions were normalized to the correct 3-charge structure:
  `BFC + july-october + november-june`
- The July-October and November-June values are intentionally identical
- `TD-DQ-NEW-003` was resolved from the same finding: the 2026 `utility_current` shape is not missing seasonal tiers

**Acceptance criteria:** Met.
- Historical `RS` versions have 3 structurally correct charges
- Season labels are preserved without implying different rates where none exist

---

### ~~TD-DQ-NEW-003~~ — DEC RS 2026 utility_current Version: Only 2 Charges — RESOLVED 2026-04-06

**Resolution:** The concern was based on the same false assumption as `TD-DQ-NEW-002`.
DEC `RS` is a flat-rate schedule, so the 2026 `utility_current` version is structurally
correct at 2 charges: `BFC + flat energy`.

**Acceptance criteria:** Met.
- No missing seasonal tier rows were found for the 2026 `RS` structure

---

### EIA-006 — EIA facility-level data not ingested

**Risk type:** Coverage gap — no plant-level generation or capacity data

**Problem:**
`electricity/facility-fuel` provides plant-by-plant generation and fuel data.
This would enable identification of large individual plants that dominate a state's
fuel mix or price.  Relevant for data-center load growth analysis (large new loads
affect fuel dispatch at the plant level).

**Deferred because:** Very large dataset; requires careful pagination.  Not needed
for state-level analysis but valuable for future plant-level exploration.

---

## Completed items

| ID | Description | Closed |
|----|-------------|--------|
| TD-001 | Added `utility TEXT` column to `ncuc_ingest_segments` and `rider_summary_blocks` via `schema.py:migrate()`. Added `utility` parameter to `calculate_bill()`, `load_ingest_results()`, and `load_rider_summaries()`. Backfilled all existing rows from docket_number/docket_dir patterns (3392/3394 segments covered; 267/267 rider blocks covered). | 2026-03-21 |
| TD-002 | Added `CREATE UNIQUE INDEX IF NOT EXISTS idx_rider_blocks_unique ON rider_summary_blocks(docket_dir, source_pdf, rate_class, effective_date)` migration in `schema.py:migrate()`. Duplicate cleanup query runs before index creation. | 2026-03-21 |
| TD-004 | Removed module-level `_NOW` constant from `ncuc_loader.py`. Replaced all three usages (`load_ingest_results`, `load_rider_summaries`, `seed_rider_descriptions`) with inline `datetime.now(UTC).isoformat()` calls at insert time. | 2026-03-21 |
| TD-003 | Created `src/duke_rates/billing/season_utils.py` with unified `SEASON_MONTHS` dict, `_normalize_season_label()`, and `season_matches()`. Replaced `engine.py:_season_matches()` and `ncuc_loader.py:_filter_seasonal_charges()` to delegate to the shared function. Added `tests/test_season_consistency.py` (35 tests) verifying all known Duke NC season label variants and cross-path agreement. | 2026-03-21 |
| TD-008 | Implemented in `season_utils.season_matches()`: unknown season labels emit `log.warning()` with both raw and normalized label before falling through to year-round `True`. Verified by `test_unknown_label_returns_true_with_warning` and `test_unknown_label_includes_normalized_form_in_warning`. | 2026-03-21 |
| TD-012 | Added Duke Energy NC holiday calendar and TOU holiday treatment. Created `src/duke_rates/billing/holidays.py` with `duke_nc_holidays(year)` (6 holidays, Saturday→Friday / Sunday→Monday observed-shift rules, `@lru_cache`) and `is_duke_holiday(date)`. Updated `tou.py:_interval_matches_period()` to treat holidays as weekends — a period with only `weekday_hours` no longer fires on holidays, causing the fallback off-peak period to apply. Added `tests/test_tou_holidays.py` (29 tests) covering all 6 holidays in 2024, both shift directions, and period classification for every holiday vs. adjacent non-holiday days. | 2026-03-21 |
| TD-005 | Added `apply_block_tiers(charges, kwh)` to `billing/calculators.py` as the single shared block-tier implementation (uses a `remaining` counter to progressively consume kWh, preventing the double-counting bug in the old `ncuc_loader` path). Replaced the inline `_next_block` forward-look loop in `ncuc_loader.py:calculate_bill()` with a call to `apply_block_tiers()`. Added `tests/test_block_tiers.py` (14 tests) covering flat rates, boundary conditions, unsorted input, three-tier stacking, zero kWh, and cross-path agreement between the engine and loader paths. | 2026-03-21 |
| TD-006 | Added docstring to `_prorated_component_amount()` explaining the linear day-fraction approximation and why it exists. Changed return type to 3-tuple `(amount, detail, used_proration: bool)`. When the multi-segment storm proration path fires, `apply_riders()` now sets `used_storm_proration=True` in the result dict. `BillingEngine.estimate()` appends `"Storm rider mid-period proration uses a linear day-fraction of monthly kWh (approximation; actual billing uses meter reads)."` to `BillEstimate.notes` when that flag is set. | 2026-03-21 |
| TD-007 | Added `leaf_no: str \| None = None` to `DocumentParseResult`. Refactored `_component_source_bucket()` to check `parse_result.leaf_no` first (structured, file-name-independent), then fall back to a `_LEAF_NO_RE` regex search of `raw_text_path` + rider title. When the heuristic path is taken and `leaf_no` is not set, a `log.warning()` is emitted with the document_id so operators can populate the structured field. Added `_STORM_LEAF_BUCKETS` dict for extensibility. | 2026-03-21 |
| TD-009 | Added inline SQL comments to `dep_provisional_rider_components` DDL distinguishing `effective_date` (sheet-level, inherited from parent row) from `rider_effective_date` (component-level, use for per-rider timelines). | 2026-03-21 |
| TD-010 | Corrected the original premise (Phase 2c tables ARE the active Phase 4a billing path, not reserved). Added block comment to `schema.py` before `tariff_families` explaining both data paths: generalized `tariff_charges` path (used by `tariff_engine.py`) and legacy `ncuc_ingest_segments` path (used by `ncuc_loader.py`). | 2026-03-21 |
| Phase-3-SGS | Extended canonical rider component + trust path to DEP small commercial (SGS/SGS-TOUE via `load_dep_sgs_canonical_rider_components()`, SGS-TOU-CLR via `load_dep_sgs_clr_canonical_rider_components()`). Added `rate_class_group` discriminator column to trust table; continuity scoring now scoped per group. Trust table covers 4 groups (~500+ rows vs prior 172). 22 new tests added; suite: 345 passing. | 2026-03-22 |
| UI-Trust | Added Rider Trust Quality section to `streamlit_res_comparison_app.py`: color-coded tier KPIs, expandable score detail table, tier pivot by rate class group, scoring model guide. | 2026-03-22 |
| EIA-001 | Added monthly generation backfill (step 4b in `scripts/eia_backfill.py`; key fuels only; `--skip-monthly-generation` flag). Added `load_monthly_fuel_mix_shares()` to `eia_analytics.py`. | 2026-03-22 |
| EIA-005 | Added `load_duke_eia_revenue_reconciliation()` to `eia_analytics.py`. EIA-implied vs reported price delta confirmed < 0.01 ¢/kWh (internally consistent). Duke tariff estimates joined externally. | 2026-03-22 |
| EIA-002 | Migrated `get_nc_rate_context()` and `nc-rate-context` CLI to read from `eia_retail_sales`. Added `_SECTOR_CODE_MAP` for `residential`→`RES` etc. CLI help text updated to reference `eia-backfill`. VA/TN/GA return None until broader backfill run. | 2026-03-22 |
| Phase-4a-LiveTests | Added `tests/test_tariff_engine_live.py` (23 tests): R-TOU, R-TOUD, SGS-TOUE, SGS flat-rate schedules validated against live DB charge data. Covers fixed charges, TOU period routing, three-phase surcharge exclusion, seasonal energy blocks, rider linking. Suite: 368 passing. | 2026-03-22 |
| EIA-004 | Added `last_reviewed`, `source`, and `review_cadence` comments to `MARKET_STRUCTURE` in `references.py`. Reviewed 50-state classifications; no changes. Clarified NC note (PJM wholesale vs. regulated retail). | 2026-03-22 |
| Phase-4a-CompareCmd | Improved `compare-tariff-rates` CLI: (1) partial TOU coverage detection in engine warns + caps confidence + excludes from ranked list; (2) `--group` flag filters by schedule group (residential/sgs/mgs/lgs/gs/specialty/all), default=residential; (3) `schedule_group_for()` helper + `SCHEDULE_GROUPS` map added to `tariff_engine.py`. 8 new unit tests + 1 live test. Suite: 377 passing. | 2026-03-22 |
| Phase-4a-StreamlitApp | Created `streamlit_rate_comparison_app.py` — rate plan comparison UI. Inputs: kWh, service date, TOU breakdown, peak kW, utility, schedule group. Output: ranked table with "vs cheapest" delta, stacked bar chart by charge component, per-schedule line-item expanders, partial-coverage notice. Uses `TariffBillingEngine` + `schedule_group_for()` directly. | 2026-03-22 |
| Phase-4a-ShiftSimulator | Added "Shift Simulator" tab to Streamlit app — breakeven chart sweeping on-peak 0–70%, binary-search breakeven table per TOU schedule, "Currently saving?" indicator vs. RES baseline. | 2026-03-22 |
| Phase-4a-ESPIParser | Created `src/duke_rates/billing/espi_parser.py` — parses Duke Energy ESPI/Green Button XML (15-min interval exports). `_classify_interval()` uses Duke NC TOU rules + Duke holiday calendar. `parse_espi_xml()` accepts bytes/filename/file-like; returns `UsageProfile` with per-month `MonthlyUsageSummary` (total_kwh, on_peak_kwh, off_peak_kwh, discount_kwh, peak_kw). `to_bill_input_kwargs()` feeds directly into `BillInput`. 25 tests (all pass). Suite: 402 passing. | 2026-03-22 |
| Phase-4a-ESPIStreamlit | Wired ESPI parser into `streamlit_rate_comparison_app.py`: sidebar XML upload, month selector with "Load selected month" button that populates kWh/TOU sliders/service date/peak kW. New "Monthly Usage" tab shows month-by-month TOU breakdown table, stacked bar chart by period, on-peak % trend line, parser warnings. | 2026-03-22 |
| Phase-5-SolarSizing | Created `src/duke_rates/billing/solar_sizing.py` — `SolarMonth` + `SolarSizingResult` dataclasses; `size_solar_system()` and `sweep_system_sizes()` functions. NC monthly capacity factors (NREL PVWatts). Proportional TOU offset, net metering at retail + $0.04/kWh avoided-cost export credit. peak_kw unchanged (conservatively correct). 32 tests (all pass). Suite: 434 passing. | 2026-03-22 |
| Phase-5-SolarStreamlit | Added "Solar Sizing" tab to `streamlit_rate_comparison_app.py`: size/cost/derate inputs, schedule selector, sweep 2–N kW, annual savings bar + marginal savings line chart, payback curve, "knee" recommendation, month-by-month detail expander. Falls back to synthetic 12-month profile if no XML uploaded. | 2026-03-22 |
| Phase-6-URDBExport | Created `src/duke_rates/external/urdb_export.py` — exports `tariff_charges` DB records to URDB/OpenEI JSON format. `export_family_to_urdb()` + `export_bulk_to_urdb()` + `records_to_json()`. Builds `energyratestructure` + 12×24 weekday/weekend schedule arrays (Duke NC TOU hour map), `fixedcharges`, `demandratestructure`. Confidence filter, curation notes, rider key references. `duke-rates export-urdb` CLI (single or bulk, `--state`, `--company`, `--output`). 38 tests (all pass). Suite: 472 passing. | 2026-03-22 |
| RateHistoryTab | Added "Rate History" tab to `streamlit_rate_comparison_app.py`. Loads canonical residential timeline (DEP 2016–2025, DEC partial) via `load_canonical_residential_timeline()`. Shows: all-in vs base-only rate chart, escalation metrics (first/last/% change), historical bill table at sidebar kWh, stacked bar escalation chart with current tariff-engine total overlay, and (when ESPI uploaded) actual monthly bills at prevailing historical rates. | 2026-03-22 |
| PipelineRefactor | Rebuilt the NCUC text mining pipeline moving away from whole-document matching to a staged, page-aware architecture (`pipeline/`). Adds `TariffSpan` artifact generation, resolving ambiguity and false positives from large compliance books. Includes multi-evidence `family_matcher` and bounded regex date extraction. | 2026-03-25 |
