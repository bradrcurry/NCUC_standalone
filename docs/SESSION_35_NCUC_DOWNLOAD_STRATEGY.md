# Session 35 — NCUC Portal Download Strategy

> **HISTORICAL SESSION LOG — DO NOT FOLLOW.** Strategy notes from a single
> 2026-04-20 session. For current portal work see
> [NCUC_PORTAL_WORKING_METHOD.md](NCUC_PORTAL_WORKING_METHOD.md).

**Date:** 2026-04-20  
**Status:** Ready to execute  
**Objective:** Identify and download missing tariff documents using the proven NCUC portal method

---

## Authentication Method (PROVEN WORKING)

**Source:** NCUC_PORTAL_WORKING_METHOD.md (verified 2026-03-31)

**Requirements:**
1. ✅ Chrome or Edge installed (NOT bundled Chromium — Cloudflare blocks it)
2. ✅ `.env` file with credentials:
   ```
   DUKE_RATES_NCID_USERNAME=<username>
   DUKE_RATES_NCID_PASSWORD=<password>
   ```
3. ✅ Python packages: `pip install playwright`

**Key Implementation:**
- Use `create_authenticated_context(settings)` from `src/duke_rates/historical/ncuc/session.py`
- Navigate with exact ASP.NET field selectors
- Download via `page.expect_download()` with Chrome's PDF-as-download setting
- **Critical:** Always include `--docket-number` parameter when creating discovery records

**Reference:** `scripts/discovery/search_dep_gaps.py` (proven working example, 11 docs downloaded successfully 2026-03-29)

---

## Missing Documents — Priority Queue

### Tier 1: CRITICAL (Extractable rates, easy download)

| Family | Issue | Source | Docket | Action |
|--------|-------|--------|--------|--------|
| **DEP leaf-602** (JAA) | Image PDFs on disk (0 page artifacts); parser ready | Official DEP website + NCUC | E-2 Sub 1143 | Download `leaf-no-602-rider-jaa-ry*.pdf` from DEP website; also fetch E-2 Sub 1143 annual filings (2017+) |
| **DEP leaf-663** (SRR) | Only procedural docs, NO tariff sheet | Official DEP website | E-2 Sub ?? | Download `leaf-no-663-rider-srr.pdf` from Duke Energy website |
| **DEP leaf-606** (DSM) | Completely missing | Official DEP website | E-2 series | Check DEP website for `leaf-no-606-rider-dsm.pdf`; may be retired |
| **DEC rider-STS** (Storm Securitization) | Only cover letters, standalone sheet undownloaded | NCUC compliance filing | E-7 Sub 1243 | Search E-7 Sub 1243; download "Compliance Tariffs" attachment with Rider STS leaf |
| **DEC rider-EDIT4** | Has extractable rate; confirm high-confidence docs | NCUC compliance filing | E-7 Subs 1213, 1214, 1187, 1152, 1146 | Mine/extract pages 2+ from `c7aa9e9f-*.pdf` (6p, 2021-06-01) |

### Tier 2: HIGH (Partial data, need historical versions)

| Family | Issue | Source | Docket | Action |
|--------|-------|--------|--------|--------|
| DEP leaf-604 (EDIT-4) | Only 2026 version; missing 2016-2020 | NCUC + DEP | E-2 Sub 1196 | Search E-2 Sub 1196 for 2016-2020 compliance filings |
| DEP leaf-607 (STS) | Only 2025-2026; missing 2015-2022 | NCUC | E-2 Sub 1204 | Search E-2 Sub 1204 for 2015-2022 compliance filings |
| DEP leaf-608 (RDM) | Only current; missing 2015-2022 | NCUC | E-2 Sub 1294 | Search E-2 Sub 1294 for 2015-2022 compliance filings |
| DEC rider-EDPR | 14 procedural docs; needs proper rate extraction | NCUC compliance | E-7 Subs 487, 828, 1026, 1146, 1165 | Mine pages from compliance filings; confirm rate structure |
| DEC rider-CEI | 45-page compliance filing; rate on pages 5-15 | NCUC compliance | E-7 (CEI program) | Extract/mine pages 5-15 from `e-7-nodate-dec-dep-compliance-tariffs-rider-clean-energy-impact.pdf` |

### Tier 3: MEDIUM (Formula/complex, need page-bounded mining)

| Family | Issue | Profile Needed | Next Action |
|--------|-------|-----------------|-------------|
| DEP leaf-534 (LGS-RTP) | Formula-based, not simple rate | `formula_based_tariff` | Mark as formula-only; document rate formula structure |
| DEP leaf-570, 572, 574 | Per-lamp/fixture flat fees | `progress_lighting_flat_fee` | Create profile; document $/lamp/month structure |
| DEC schedule-RE, BC | In 300-page 2013 compliance filing | `page_bounded_schedule` | Mine pages for RE (p3-8) and BC (p6-12) from compliance book |

---

## Correct Download Procedure

### Step 1: Verify Authentication (One-time)

```bash
python -m duke_rates ncuc-login-test
# Expected: "Login successful" + authenticated session created
```

### Step 2: Search Docket and Download

For each Tier 1 docket, use the **proper procedure**:

```bash
# OPTION A: Via CLI (if command exists)
python -m duke_rates ncuc-docket-fetch \
  <DOCKET_GUID> \
  --docket-number "E-2, Sub 1143" \
  --download

# OPTION B: Via script (scripts/discovery/search_dep_gaps.py as template)
python scripts/discovery/search_docket_tariffs.py \
  --docket "E-2 Sub 1143" \
  --target-families "leaf-602,leaf-607,leaf-608" \
  --download
```

### Step 3: Verify Discovery Records

```bash
python -m duke_rates ncuc-list \
  --docket-number "E-2, Sub 1143" \
  --limit 50
# Verify: docket_number and sub_number are POPULATED (not NULL)
```

### Step 4: Run Import Pipeline

```bash
python -m duke_rates ncuc-import-pipeline --all-downloaded
python -m duke_rates bootstrap-missing-versions-nc
python -m duke_rates extract-rates-nc
```

---

## Starting Point — Tier 1 Execution

### Immediate Actions (This Session)

1. **DEP leaf-602 (JAA)** — Simplest: Direct download from DEP website
   - URL: `https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-602-rider-jaa-ry1.pdf`
   - Save to: `data/historical/dep/leaf-no-602-rider-jaa-ry1.pdf`
   - Then register with `add-historical-document-nc` command

2. **DEP leaf-663 (SRR)** — Check DEP website
   - Try: `https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-663-rider-srr.pdf`
   - If 404: Search NCUC portal E-2 dockets for "Solar Rebate Rider"

3. **DEC rider-STS** — Portal search required
   - Docket: E-7 Sub 1243
   - Search term: "Rider STS" OR "Storm Securitization"
   - Target: "Compliance Tariffs" filing with STS schedule attachment
   - Expected date range: 2020-2025

4. **DEP leaf-606 (DSM)** — Desktop search
   - Check DEP website: `leaf-no-606-rider-dsm.pdf`
   - If missing, check NCUC: E-2 series dockets for "Demand Side Management"

### Expected Outcomes

- **Tier 1 completion:** 5 critical families with proper tariff sheets
- **Tier 2 start:** Historical versions of leaf-604, 607, 608
- **Pipeline state:** provisional_families should remain <20 (with proper docket metadata)

---

## Anti-patterns to Avoid (Lessons from Session 34)

❌ **WRONG:**
```bash
python -m duke_rates ncuc-docket-fetch 9b3614b6-11d6-4703-8d18-5e2e2ef3d705 --download
# Results in NULL docket_number/sub_number, breaks pipeline
```

✅ **CORRECT:**
```bash
python -m duke_rates ncuc-docket-fetch 9b3614b6-11d6-4703-8d18-5e2e2ef3d705 \
  --docket-number "E-2, Sub 1143" \
  --download
# Metadata properly populated, family matching works
```

---

## Monitoring

After each fetch batch, check:

```bash
python -m duke_rates show-workflow-status-nc
# Watch for: provisional_families should stay <25
#            null_effective_start should not increase
#            coverage should increase (if new tariff sheets extracted)

python -m duke_rates list-provisional-families --state NC
# If >25: review for garbage (no_-extracted charges) and retire
```

---

## Reference Materials

- **NCUC_PORTAL_WORKING_METHOD.md** — Complete working code patterns
- **hq_document_discovery_catalog.md** — Document quality tiers and URL patterns
- **gap_analysis_dep_nc.md** — DEP missing documents (lines 67-102 for critical families)
- **gap_analysis_dec_nc.md** — DEC missing documents (lines 85-111 for critical families)
- **CORRECT_NCUC_DOCKET_FETCH_PROCEDURE.md** — Proper fetch procedure (from Session 35)
- **scripts/discovery/search_dep_gaps.py** — Proven working example

---

## Success Criteria

1. ✅ All Tier 1 documents downloaded with proper metadata
2. ✅ Discovery records have docket_number/sub_number populated
3. ✅ Import pipeline runs without creating garbage provisional families
4. ✅ At least 50 new charges extracted from Tier 1 documents
5. ✅ Coverage increases from 72.3% toward target 75%+
6. ✅ Provisional families remain <25 (isolated garbage only)
