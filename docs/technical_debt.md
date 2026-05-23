# Technical Debt Register

Active structural debt, unresolved data-quality gaps, and deferred improvements.

This file is now intentionally short. Completed implementation history was archived to
[technical_debt_archive_2026_04_07.md](/c:/Python/Duke/Standalone/docs/technical_debt_archive_2026_04_07.md)
so this register can stay focused on work that is still open.

**Last reviewed:** 2026-04-08 (provisional garbage cleanup + DEC energy parser fix)
**Scope:** active items only

---

## How To Use This File

- Use this file for items that are still open, partial, or explicitly deferred.
- Use [known_issues.md](/c:/Python/Duke/Standalone/docs/known_issues.md) for current-state caveats and coverage status.
- Use [technical_debt_archive_2026_04_07.md](/c:/Python/Duke/Standalone/docs/technical_debt_archive_2026_04_07.md) for completed ticket history, prior acceptance criteria, and implementation notes.

---

## Highest Priority

### TD-DQ-007 — DEP Compliance Bundle Parser: Remaining bundle-era incompleteness

**Status:** Partial fix applied on 2026-04-06. Leave open only if richer historical DEP schedule detail is still a project goal.

**Risk type:** Coverage

**Current state:**
- 2015–2022 DEP compliance-bundle versions now have usable energy rates instead of 0–2 rows.
- The validated residential billing path is usable.
- Multi-class and 3-phase detail is still missing from many bundle-era schedules.
- This now appears to be the main remaining active DEP historical coverage debt item.

**Why it is still open:**
- Historical `DEP RES` is good enough for the validated path.
- Broader schedule completeness for `R-TOUD`, `R-TOU`, `SGS`, `SGS-TOUE`, `LGS`, and `LGS-TOU`
  is still below modern standalone-leaf quality.

**Decision to make:**
- Either keep this as active parser debt, or downgrade it to a known partial-coverage caveat if no near-term work depends on fuller 2015–2022 DEP schedule detail.

**If pursued:**
1. Compare one bundle-era sheet against the later standalone leaf layout.
2. Add parser branches for the bundle table shapes.
3. Re-extract affected versions.
4. Re-run `python -m duke_rates export nc-coverage-assessment`.

---

### TD-DQ-010 — NC historical intake canonicalization backlog

**Status:** Partially resolved (2026-04-08) — provisional sprawl substantially reduced; `doc-*` canonicalization still open

**Risk type:** Data quality / maintainability

**Current state (as of 2026-04-08):**
- `retire-provisional-garbage-nc --execute` retired 351 provisional families; only 7 remain (all have real charges)
- `show-workflow-status-nc` now reports:
  - `stale_historical=236` (was 513)
  - `provisional_families=7` (was 351)
  - `null_effective_start=286` (was 565)
- `export nc-schedule-inventory-audit` reports:
  - `105` NC `rate_schedule` families in SQLite (was 264 — the reduction is because most provisionals lacked `rate_schedule` classification)
  - `23` legacy / malformed `doc-*` families (was 182)
- The 7 remaining provisional families with real charges need `promote-provisional-family` calls.
- The 23 `doc-*` families need canonical key promotion (e.g. `nc-carolinas-doc-SCHEDULEFLFLOODLIGHTINGSERVICE` → `nc-carolinas-schedule-FL`).
- Two large `doc-*` families also have duplicate charges (`SCHEDULEFLFLOODLIGHTINGSERVICE` 529 charges at 35x duplication; `SCHEDULEWC` 534 charges at 33x duplication) — deduplication needed before promotion.

**Why it matters:**
- Misclassified `doc-*` / `program-*` families inflate issue counts and obscure real gaps.
- Null-effective rows make the anomaly and coverage reports noisier than they should be.
- Historical schedule rows can remain stranded under bad lineage even when the underlying PDF is real tariff content.

**What to do:**
1. Run `retire-provisional-garbage-nc --execute` at the start of each session (pipeline adds new ones).
2. Promote the 7 remaining provisional families with `promote-provisional-family FAMILY_KEY`.
3. Deduplicate charges in the large `doc-*` families, then promote them to canonical keys.
4. Continue the recover-vs-retire pass on high-signal zero-charge historical rows.
5. Re-run:
   - `python -m duke_rates show-workflow-status-nc`
   - `python -m duke_rates export nc-anomaly-audit`
   - `python -m duke_rates export nc-schedule-inventory-audit`

---

## Medium Priority

### TD-V4-002 — Add segment-level billing breakdown to `BillResult`

**Risk type:** Debuggability

**Problem:**
When a billing period spans a version boundary, the engine computes the correct prorated result
but does not expose the segment-by-segment breakdown in the returned object.

**Why it matters:**
- Mid-period rider or tariff changes are harder to audit.
- Manual reconciliation requires logs or custom debugging.

**What to do:**
1. Add an optional `segments` field to `BillResult`.
2. Populate it when the engine splits a bill across multiple tariff segments.
3. Keep existing callers backward-compatible.

---

### TD-V4-004 — R-TOUD demand charges not yet parsed

**Risk type:** Coverage gap

**Problem:**
`nc-progress-leaf-501` is still incomplete for demand-charge-aware billing. Historical rows exist,
but the engine path for a fully realistic `R-TOUD` comparison still depends on parsed demand charges.

**What to do:**
1. Confirm the relevant leaf-501 demand rows from the tariff text.
2. Ensure they are represented as `demand` charges in `tariff_charges`.
3. Verify the billing engine produces a non-zero demand subtotal when `peak_kw` is provided.

---

## Deferred / Low Priority

### TD-011 — `utility` column on `dep_provisional_*` tables

**Status:** Deferred

`dep_provisional_rider_totals` and `dep_provisional_rider_components` are DEP-specific by design.
Adding a `utility` column is cosmetic unless those tables are ever generalized beyond DEP.

**Revisit when:**
- DEC or another utility starts using the same provisional table family.

---

### EIA-006 — EIA facility-level data not ingested

**Risk type:** Coverage gap

**Problem:**
Plant-level generation and capacity data is still not in the local EIA dataset.

**Deferred because:**
- The dataset is large.
- Current tariff and validation work does not require plant-level resolution.

**Revisit when:**
- A future analysis needs plant-level dispatch, concentration, or data-center load impact work.

---

## Recently Closed

Closed items remain documented in the archive:
- [technical_debt_archive_2026_04_07.md](/c:/Python/Duke/Standalone/docs/technical_debt_archive_2026_04_07.md)

Notable recent closures:
- `TD-DQ-006`
- `TD-DQ-008`
- `TD-DQ-009`
- `TD-DQ-NEW-001`
- `TD-DQ-NEW-002`
- `TD-DQ-NEW-003`
- `TD-DQ-NEW-004` — DEC SGS/LGS 2018-12-01 energy tiers: `\s*` added before cent character class in `nc_carolinas.py`; resolved 2026-04-08

---

## Verification

After changing any active item above, update:
- [known_issues.md](/c:/Python/Duke/Standalone/docs/known_issues.md)
- [NEXT_SESSION_START_HERE.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_START_HERE.md)
- `python -m duke_rates export nc-coverage-assessment`
