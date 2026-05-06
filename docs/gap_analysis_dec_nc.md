# DEC NC (Duke Energy Carolinas — North Carolina) Gap Analysis

**Last updated:** 2026-03-28
**DB State:** 13,911 charges | ~40 named families + ~14 malformed doc- families with charges
**Download targets:** See [download_targets_dec_nc.md](/c:/Python/Duke/Standalone/docs/download_targets_dec_nc.md) for the DEC-specific portal harvest queue.

## How to read this map

| Column | Meaning |
|--------|---------|
| STATUS | ✅ OK = has charges · ⚠️ PARTIAL = charges exist but incomplete · 🔴 PROFILE-NEEDED = docs+pages exist but profile can't extract · 🟡 CONTENT-TYPE = zero charges by design · 🔵 NEEDS-MINING = file on disk, 0 page artifacts · ❌ MISSING = no file on disk |
| CH | Charge count in DB as of last extraction |
| EVIDENCE | DR=discovery_records · HD=historical_docs · PA=page_artifacts · TS=standalone tariff sheet from Duke website |
| CONFIDENCE | Official tariff sheet (highest) · NCUC compliance filing (high) · Extracted from order/procedural doc (lower) |
| CLUE | Specific filenames, docket numbers, page text snippets, or next action |

---

## Family Key Structure

DEC NC uses **schedule/rider name keys** (not leaf numbers like DEP):
- `nc-carolinas-schedule-RS` — Residential Service
- `nc-carolinas-schedule-SGS` — Small General Service
- `nc-carolinas-rider-EDPR` — Economic Development Prospective Rider
- `nc-carolinas-rider-SUMMARY` — Summary of Rider Adjustments (like DEP leaf-600)

DEC leaf numbers appear in document content (e.g., "NC Second Revised Leaf No. 131") but are not used as family keys.

---

## Rate Schedules — OK

| Family | Schedule | STATUS | CH | Evidence | Confidence | Notes |
|--------|----------|--------|----|----------|------------|-------|
| nc-carolinas-schedule-RS | Residential Service | ✅ OK | 8 | DR+HD+PA+TS | Official tariff sheet | Low count (8). RS is mostly in large multi-schedule compliance filings (26–300p) where rate table is pages 3–5. Most historical versions linked to malformed `nc-carolinas-doc-SCHEDULERSRESIDENTIALSERVICE` key. See RS/RT section below. |
| nc-carolinas-schedule-RT | Residential TOU | ✅ OK | 20 | DR+HD+PA+TS | Official tariff sheet | Also has malformed-key docs (see SCHEDULERT malformed family). |
| nc-carolinas-schedule-SGS | Small General Service | ✅ OK | 152 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-schedule-LGS | Large General Service | ✅ OK | 471 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-schedule-I | Industrial Service | ✅ OK | 547 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-schedule-PG | Power Grid | ✅ OK | 684 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-schedule-NL | Network Lighting | ✅ OK | 4,112 | DR+HD+PA+TS | Official tariff sheet | Largest DEC schedule family |
| nc-carolinas-schedule-OL | Outdoor Lighting | ✅ OK | 312 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-schedule-PL | Street/Public Lighting | ✅ OK | 195 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-schedule-TS | Traffic Signal | ✅ OK | 171 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-schedule-HP | High Power | ✅ OK | 10 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-schedule-HLF | High Load Factor | ✅ OK | 22 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-schedule-ES | Economic Service | ✅ OK | 8 | DR+HD+PA+TS | Official tariff sheet | |

---

## Rate Schedules — Needs Attention

| Family | Schedule | STATUS | CH | Evidence | Confidence | Clue / Next Action |
|--------|----------|--------|----|----------|------------|---------------------|
| nc-carolinas-schedule-RE | Residential Experimental | 🔴 PROFILE-NEEDED | 0 | 4+ paged docs (procedural cover letters) | Procedural filings | Best source: `e31d1ef2-78de-4dab-8f84-5b6b6cb94c30.pdf` (146p, 2021-12-16) — full compliance tariff book; RE schedule likely pages 3–8. Also in `e-7-nodate-duke-energy-carolinas-llc-s-revisions-to-rate-compliance-filing-*.pdf` (300p, 2013-10-23). Also in 484-page `e-7-nodate-duke-s-rate-schedule.pdf` and 272-page rate book. **Fix path**: `mine-docling-nc` targeting E-7 dockets to capture RE page from large PDFs. |
| nc-carolinas-schedule-BC | Business Customer | 🔴 PROFILE-NEEDED | 0 | 4 paged docs (procedural) | Procedural filings | All cover letters. Best source: 300-page compliance filing `e-7-nodate-duke-energy-carolinas-llc-s-revisions-to-rate-compliance-filing-of-approved-tar.pdf` (start=2013-10-23) — BC schedule likely pages 6–12. Also in 484-page and 272-page rate books. |
| nc-carolinas-schedule-PP | Purchase Power | 🔵 NEEDS-MINING | 0 | 1 doc on disk, 0 pages | Official tariff sheet | File: `pp-media-pdfs-for-your-home-rates-electric-nc-ncschedulepp-p-*.pdf` (start=2021-10-11). File is on disk with 0 page artifacts. **Action**: Run `mine-tariff-sheets-nc --family nc-carolinas-schedule-pp` or check path. |
| nc-carolinas-schedule-PPBE | Purchase Power Blend & Extend | 🟡 CONTENT-TYPE | 0 | TS mined, p=11 | Official tariff sheet | Contract-based — credits paid to Small Power Producers per Appendix A/B. No fixed per-kWh rate to extract. File: `ncscheduleppbe-*.pdf` (2022-10-21). |

---

## Riders — OK

| Family | Rider | STATUS | CH | Evidence | Confidence | Notes |
|--------|-------|--------|----|----------|------------|-------|
| nc-carolinas-rider-SUMMARY | Summary of Riders | ✅ OK | 1,635 | DR+HD+PA+TS | Official tariff sheet | Like DEP leaf-600. Multi-version history. |
| nc-carolinas-rider-BPMPROSPECTIVERIDER | BPM Prospective Rider | ✅ OK | 4,112 | DR+HD+PA+TS | Official tariff sheet | Largest DEC rider family |
| nc-carolinas-rider-EC | Energy Credits | ✅ OK | 50 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-EE | Energy Efficiency | ✅ OK | 20 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-NM | Net Metering | ✅ OK | 30 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-NMB | Net Metering Buyback | ✅ OK | 31 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-NSC | New Service Contracts | ✅ OK | 22 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-IS | Interconnection Service | ✅ OK | 20 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-PS | Power Supply | ✅ OK | 22 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-RSC | Residential Solar Choice | ✅ OK | 85 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-SCG | Small Customer Generator | ✅ OK | 7 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-PROSPECTIVERIDER | Prospective Rider | ✅ OK | 7 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-GSA | Green Source Advantage | ✅ OK | 4 | DR+HD+PA+TS | Official tariff sheet | |
| nc-carolinas-rider-USEOFRIDER | Use of Rider | ✅ OK | 1 | DR+HD+PA+TS | Official tariff sheet | |

---

## Riders — Needs Attention

| Family | Rider | STATUS | CH | Evidence | Confidence | Clue / Next Action |
|--------|-------|--------|----|----------|------------|---------------------|
| nc-carolinas-rider-EDIT4 | Excess Deferred Income Tax Rider #4 | ✅ OK | 8 | 2 paged docs | NCUC compliance tariff (official) | **HIGH PRIORITY — HAS EXTRACTABLE RATE**. Page text: *"Duke Energy Carolinas, LLC NC Second Revised Leaf No. 131 ... RIDER EDIT-4 EXCESS DEFERRED INCOME TAX RIDER #4 ... All service supplied under the Company's rate schedules is subject to a **decrement per kilowatt-hour** as set forth below."* File `c7aa9e9f-2320-4655-ae1e-5aff69a459f6.pdf` (6p, 2021-06-01), page 2. Also standalone `ncrideredit4-*.pdf` (1p, 2026-06-01). **Action**: Apply `ProgressSingleValueRiderProfile` — rate is single ¢/kWh decrement value. Docket: E-7, Subs 1213, 1214, 1187, 1152, 1146. |
| nc-carolinas-rider-EDPR | Econ Dev Prospective Rider | ✅ OK | 1 | 14 paged docs | Mixed — cover letters + partial rate table docs | Multiple docs spanning 2009–2024. Best rate sources: `f807656f-*.pdf` (7p, 2024-07-01 compliance) and `e-7-nodate-duke-energy-carolinas-llcs-compliance-tariff-*.pdf` (9p, 2018-08-01). Older versions in 484-page rate book and 272-page rate book. **Dockets**: E-7 Subs 487, 828, 1026, 1146, 1165 (confirmed from 2024 filing cover page). Rate is prospective adjustment per schedule class (multi-class). |
| nc-carolinas-rider-STS | Storm Securitization | ✅ OK | 8 | 2 paged docs (cover letters only) | Procedural filings only | Both docs are attorney cover letters — `d9c03aa1-*.pdf` (4p, 2025-01-01, E-7 Sub 1243) and `aa8985bf-*.pdf` (8p, date unclear). No rate table in either. **Standalone tariff sheet NOT downloaded.** Target: NCUC E-7 Sub 1243 compliance tariff attachment for Rider STS. |
| nc-carolinas-rider-CEI | Clean Energy Impact Rider | 🔴 PROFILE-NEEDED | 0 | 2 paged docs (same file, 45p) | NCUC compliance filing (joint DEC/DEP) | File: `e-7-nodate-dec-dep-compliance-tariffs-rider-clean-energy-impact.pdf` (45p, 2025-01-23). Joint DEP+DEC compliance filing. Rate table likely pages 5–15. **Action**: Read specific pages of this 45-page doc — rate is there, just needs span-level access. Docket: E-7 (CEI program). |
| nc-carolinas-rider-PM | Performance Mechanism | 🔴 PROFILE-NEEDED | 0 | 4 paged docs (procedural) | NCUC modification filings | Main doc: `e-7-nodate-dec-s-modifications-to-residential-power-manager-load-control-svcs-rider-pm.pdf` (42p, 2020-08-25). Also 5-page amended tariff + 6-page rate schedule from E-7. The 42-page doc is a full modification filing — contains rate structure but buried in proposal text. Docket: E-7 Sub 1168. |
| nc-carolinas-rider-RDM | Revenue Decoupling | ✅ OK | 2 | 1 paged doc | Official tariff sheet | **HAS RATE TABLE.** File: `nc-rider-rdm-*.pdf` (1p, 2025-07-01). Page text: *"Duke Energy Carolinas, LLC NC First Revised Leaf No. 147 ... RIDER RDM RESIDENTIAL DECOUPLING MECHANISM ... The approved rate set forth below is not included in the Rate provision..."* Standard single-value rider format. **Action**: Apply `ProgressSingleValueRiderProfile` — profile format matches DEP single-value riders. |
| nc-carolinas-rider-PIM | Performance Incentive Mechanism | ✅ OK | 2 | 1 paged doc | Official tariff sheet | **HAS RATE TABLE.** File: `nc-rider-pim-*.pdf` (1p, 2025-07-01). Page text: *"Duke Energy Carolinas, LLC NC First Revised Leaf No. 149 ... RIDER PIM PERFORMANCE INCENTIVE MECHANISM ... For Schedule HP, this Rider is only applicable to the Customer Baseline Load. The approved rate set forth below..."* **Action**: Apply `ProgressSingleValueRiderProfile` or multi-class if HP has separate rate. |
| nc-carolinas-rider-MRM | Meter Related Monthly | 🔴 PROFILE-NEEDED | 0 | 1 paged doc | Official tariff sheet | File: `ncridermrm-*.pdf` (1p, start=2018-10-01). Short single-page rider. Likely $/meter/month charge type. **Action**: Read page, apply `ProgressSingleValueRiderProfile`. |
| nc-carolinas-rider-ER | Environmental Rider | 🔴 PROFILE-NEEDED | 0 | 3 paged docs | Legacy rate books (low confidence) | All 3 docs point to `e-7-nodate-duke-s-rate-schedule.pdf` (484p). This rider appears on specific pages of the 484-page rate book. Key doc start=2010-02-09. **Action**: Re-link or mine specific pages from the 484-page rate book for ER rider section. |
| nc-carolinas-rider-CAR | Customer Assistance Recovery | 🔴 PROFILE-NEEDED | 0 | 1 paged doc (WRONG FILE) | MISLINKED — not a tariff rate | **KEY BUG**: The one doc is `nccarbonoffset-*.pdf` (1p, 2021-06-01) — Duke's NC **Carbon Offset program**, not Customer Assistance Recovery. Key `CAR` matched `CARolinas carbon offset` in importer regex. **Action**: Re-link `nccarbonoffset-*.pdf` to correct family. Investigate if a true Rider CAR exists for DEC NC. |
| nc-carolinas-rider-GS | Green Source | 🔴 PROFILE-NEEDED | 0 | 1 paged doc (WRONG FILE) | MISLINKED | Doc is `nc-ol-service-regs-*.pdf` (5p, 2025-02-18) — Outdoor Lighting Service Regulations — not Green Source. **Action**: Re-link this doc to outdoor lighting service regs family. |
| nc-carolinas-rider-ED | Economic Development | 🔴 PROFILE-NEEDED | 0 | 1 paged doc (WRONG FILE) | MISLINKED | Doc is `nc-ev-managed-charging-orig-09012023-*.pdf` (3p, 2023-09-01) — EV managed charging program — not Economic Development rider. **Action**: Re-link to EV program family. Check if Rider ED exists as standalone tariff sheet. |
| nc-carolinas-rider-US | Universal Service | 🔴 PROFILE-NEEDED | 0 | 2 paged docs | NCUC compliance filings | `e-7-nodate-duke-s-rider-us-pursuant-to-commission-order-*.pdf` (6p, 2007-10-25) and `e-7-nodate-dec-compliance-tariff-for-unmetered-service-*.pdf` (9p). 2007-era rider. Rate may be $0 or expired. |
| nc-carolinas-rider-EB | Energy Boost | 🔴 PROFILE-NEEDED | 0 | 1 paged doc | NCUC filing | File: `b60bbae6-*.pdf` (22p). Read pages to determine if rate table present. |
| nc-carolinas-rider-ESM | Energy Storage Mgmt | 🟡 CONTENT-TYPE | 0 | 1 paged doc | Official tariff sheet | File: `ncresmultifamily-*.pdf` (1p, 2025-01-01). Multifamily program terms. |
| nc-carolinas-rider-SSR | Solar Savings Rider | 🟡 CONTENT-TYPE | 0 | 1 paged doc | Official tariff sheet | File: `ncriderssr-*.pdf` (2p). Program description, no extractable rate. |
| nc-carolinas-rider-BPMPPTTRUEUP | BPM PPT True-Up | 🔴 PROFILE-NEEDED | 0 | 1 paged doc | NCUC compliance filing | File: `e-7-nodate-duke-energy-carolinas-llc-s-revised-bpm-ride-*.pdf` (12p). True-up correction rider. May have single-value rate. |
| nc-carolinas-rider-COALINVENTORYRIDER | Coal Inventory | 🔴 PROFILE-NEEDED | 0 | 2 paged docs | NCUC filing + legacy rate book | File: `e9fe8fa3-*.pdf` (6p, 2018-12-01). Likely retired/expired. |
| nc-carolinas-rider-CWIPFINANCINGCOSTSRIDER | CWIP Financing | 🔴 PROFILE-NEEDED | 0 | 1 doc from 484p rate book | Legacy rate book | Likely retired. |
| nc-carolinas-rider-DSMDEFERRALBALANCERIDER | DSM Deferral Balance | 🔴 PROFILE-NEEDED | 0 | 1 doc from 484p rate book | Legacy rate book | Likely retired. |
| nc-carolinas-rider-FUELOVERCOLLECTIONRIDER | Fuel Over-collection | 🔴 PROFILE-NEEDED | 0 | 1 doc from 484p rate book | Legacy rate book | One-time correction rider. |
| nc-carolinas-rider-RIDERCP | Rider CP | 🔴 PROFILE-NEEDED | 0 | 1 doc from 484p rate book | Legacy rate book | Unidentified rider from old rate book. |
| nc-carolinas-rider-ENDOFJOBRETENTIONRECOVERYRIDER | Job Retention Recovery | 🔴 PROFILE-NEEDED | 0 | 1 paged doc | NCUC compliance filing | From `c7aa9e9f-*.pdf` (6p, 2021). Page text: "END OF JOB RETENTION RECOVERY RIDER" — rider has been terminated/closed. |
| nc-carolinas-rider-NANTAHALASRATESCHEDULERIDERS | Nantahala Rate Schedule Riders | 🔴 PROFILE-NEEDED | 0 | 1 doc from Nantahala 22p filing | Legacy Nantahala filing | Pre-Duke merger Nantahala Power entity. Historical/retired. |

---

## Malformed Family Keys (doc- prefix)

~14 malformed families **with charges** (need re-linking) and ~50+ with 0 charges.

### Malformed families WITH charges

| Malformed Key | CH | Likely Proper Family | Key Evidence File |
|---------------|----|---------------------|------------------|
| nc-carolinas-doc-FLOODLIGHTINGSERVICE | 269 | `nc-carolinas-schedule-PL` or new `nc-carolinas-schedule-FL` | Legacy flood lighting schedule |
| nc-carolinas-doc-SCHEDULEWC | 205 | Legacy `nc-carolinas-schedule-WC` (residential water heating) | Old rate book |
| nc-carolinas-doc-SCHEDULEOPTE | 190 | `nc-carolinas-schedule-OPTE` (Optional Power TOU Electric) | Old rate book |
| nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE | 152 | Same as SCHEDULEWC above | Old rate book |
| nc-carolinas-doc-GOVERNMENTALLIGHTINGSERVICE | 105 | `nc-carolinas-schedule-PL` or `nc-carolinas-schedule-GL` | Old rate book |
| nc-carolinas-doc-SCHEDULEPLSTREETANDPUBLICLIGHTINGSERVICE | 92 | `nc-carolinas-schedule-PL` — historical duplicate | Old rate book |
| nc-carolinas-doc-SCHEDULEFLFLOODLIGHTINGSERVICE | 53 | Legacy flood lighting | Old rate book |
| nc-carolinas-doc-SCHEDULEOPTIOPTIONALPOWERSERVICETIMEOFUSEINDUSTR | 28 | Optional Power TOU Industrial | Old rate book |
| nc-carolinas-doc-SCHEDULEYLYARDLIGHTINGSERVICE | 27 | `nc-carolinas-schedule-YL` | Old rate book |
| nc-carolinas-doc-SCHEDULEOPTH | 16 | Optional Power Heat | Old rate book |
| nc-carolinas-doc-SCHEDULEOLOUTDOORLIGHTINGSERVICE | 6 | `nc-carolinas-schedule-OL` — historical duplicate | Old rate book |
| nc-carolinas-doc-SCHEDULEGLGOVERNMENTALLIGHTINGSERVICE | 5 | `nc-carolinas-schedule-GL` | Old rate book |
| nc-carolinas-doc-TYPEOFSERVICE | 3 | Service regulations (not a rate schedule) | Old rate book |
| nc-carolinas-doc-SCHEDULESGSSMALLGENERALSERVICE | 2 | `nc-carolinas-schedule-SGS` — historical duplicate | Old rate book |

### Root Cause — Key Source Files
Most malformed-key docs come from these large NCUC filings where the importer extracted body text as the key:

| File | Pages | Date | Contains |
|------|-------|------|---------|
| `e-7-nodate-duke-s-rate-schedule.pdf` | 484 | Various | All DEC NC schedules + riders (oldest complete rate book) |
| `e-7-nodate-duke-s-revised-nc-rate-schedule-and-riders.pdf` | 272 | Various | Revised rate book — schedules + riders |
| `e-7-nodate-duke-energy-carolinas-llc-s-revisions-to-rate-compliance-filing-*.pdf` | 300 | 2013-10-23 | 2013 compliance filing — RS, RT, BC, LGS, etc. |
| `e-7-nodate-duke-power-s-rate-schedule-riders.pdf` | 98 | Various | Duke Power era schedules + riders |
| `e-7-nodate-nantahala-s-rate-schedule-riders.pdf` | 22 | Various | Nantahala Power (legacy pre-merger entity) |

**Fix approach:**
```sql
-- Find malformed-key docs with page content to identify proper schedule
SELECT hd.family_key, hd.local_path,
       SUBSTR(pa.text_content, 1, 200) as page1_text
FROM historical_documents hd
JOIN ncuc_page_artifacts pa ON pa.source_pdf = hd.local_path
WHERE hd.family_key LIKE 'nc-carolinas-doc-%'
AND pa.page_number = 1
ORDER BY hd.family_key
LIMIT 20;

-- Once proper key identified, re-link charges + docs
UPDATE tariff_charges SET family_key = 'nc-carolinas-schedule-XX' WHERE family_key = 'nc-carolinas-doc-OLDKEY';
UPDATE historical_documents SET family_key = 'nc-carolinas-schedule-XX' WHERE family_key = 'nc-carolinas-doc-OLDKEY';
```

---

## Schedule RS/RT Sparse Coverage

`nc-carolinas-schedule-RS` has only 8 charges (very sparse) despite being the primary residential schedule.

**Root cause:** RS docs are mostly large compliance filings (26–300+ page tariff books). The rate schedule appears on pages 3–8 of these large PDFs. Most historical RS content is in the malformed-key family `nc-carolinas-doc-SCHEDULERSRESIDENTIALSERVICE`.

**Key files containing RS rate tables:**
- `e-7-nodate-duke-energy-carolinas-llc-s-revisions-to-rate-compliance-filing-*.pdf` (300p, 2013-10-23) — RS table ~pages 5–8
- `e-7-nodate-duke-energy-carolinas-llc-s-revisions-to-rat-*.pdf` (300p, start=2013-11-01) — same filing, different version
- `e31d1ef2-78de-4dab-8f84-5b6b6cb94c30.pdf` (146p, 2021-12-16) — compliance filing, RS likely pages 3–6
- `e-7-nodate-duke-s-rate-schedule.pdf` (484p) — legacy rate book, RS on specific pages

**Fix path:** Run `mine-docling-nc --limit 50` targeting E-7 dockets. Or re-link charges from `nc-carolinas-doc-SCHEDULERSRESIDENTIALSERVICE` to `nc-carolinas-schedule-RS`.

---

## NCUC Portal Search Targets for DEC

| Priority | Docket | Rider/Schedule | Target Search Terms | What to Download |
|----------|--------|---------------|---------------------|-----------------|
| HIGH | E-7 Sub 1243 | Rider STS (Storm Securitization) | "compliance tariff" "Rider STS" | Standalone STS tariff leaf PDF attachment |
| HIGH | E-7 Sub 1213/1214 | Rider EDIT-4 (historical) | "EDIT-4" "Leaf 131" historical versions | Pre-2021 EDIT4 rate filings |
| HIGH | E-7 Sub 487, 828, 1026, 1146, 1165 | Rider EDPR | "EDPR compliance tariff" | Annual rate adjustment filings (each sub has new rates) |
| MED | E-7 Sub 1168 | Rider PM | "Rider PM" compliance tariff | PM rider rate table (not buried in 42-page program filing) |
| MED | E-7 (CEI docket) | Rider CEI | "Clean Energy Impact" rate table | Read existing 45-page doc first — rate may already be there |
| MED | E-7 (various) | Schedule RS/RT | "Revised Rate Schedule RS" compliance | Page-specific rate table for RS/RT — or re-link from malformed keys |

---

## Actionable Fix Summary

### Priority 1 — Profile fixes (files on disk, extractable with right profile)
| Task | Family | File | Action |
|------|--------|------|--------|
| Apply `ProgressSingleValueRiderProfile` | rider-EDIT4 | `c7aa9e9f-*.pdf` p.2 (Leaf 131) + `ncrideredit4-*.pdf` | Single ¢/kWh decrement — standard format |
| Apply `ProgressSingleValueRiderProfile` | rider-RDM | `nc-rider-rdm-*.pdf` (1p, 2025) | Explicit "approved rate" per kWh |
| Apply `ProgressSingleValueRiderProfile` | rider-PIM | `nc-rider-pim-*.pdf` (1p, 2025) | Same format as RDM |
| Apply `ProgressSingleValueRiderProfile` | rider-MRM | `ncridermrm-*.pdf` (1p, 2018) | Likely $/meter/month — read and profile |
| Mine specific pages | rider-CEI | `e-7-nodate-dec-dep-compliance-tariffs-rider-clean-energy-impact.pdf` (45p) | Rate on pages 5–15 |

### Priority 2 — Fix mislinked docs
| Task | Family | Action |
|------|--------|--------|
| Re-link `nccarbonoffset-*.pdf` | rider-CAR | Belongs to carbon offset family, not Customer Assistance |
| Re-link `nc-ol-service-regs-*.pdf` | rider-GS | Belongs to OL service regulations, not Green Source |
| Re-link `nc-ev-managed-charging-*.pdf` | rider-ED | Belongs to EV program family, not Economic Development rider |

### Priority 3 — NCUC portal downloads
| Task | Docket | Target |
|------|--------|--------|
| Download Rider STS tariff leaf | E-7 Sub 1243 | Current STS standalone PDF |
| Download EDPR annual filings | E-7 Sub 487, 828, 1026, 1146, 1165 | Each sub has different rates |

### Priority 4 — Re-link malformed doc- families
| Task | Approach | Impact |
|------|---------|--------|
| Re-link 14 malformed families WITH charges (1,085 charges total) | SQL UPDATE family_key after identifying proper schedule | Merges historical DEC lighting/WC/OPT schedules into proper families |
| Re-link 50+ malformed families with 0 charges | Same; lower priority | Enables extraction from large legacy PDFs |

---

## Commands

```bash
# Check DEC charge counts
python -c "
import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')
rows = conn.execute('''
  SELECT family_key, COUNT(*) as ch FROM tariff_charges
  WHERE family_key LIKE 'nc-carolinas-%'
  GROUP BY family_key ORDER BY ch DESC
''').fetchall()
for r in rows: print(r)
"

# Find DEC docs with page content and zero charges
python -c "
import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')
rows = conn.execute('''
  SELECT hd.family_key, hd.effective_start,
    (SELECT COUNT(*) FROM ncuc_page_artifacts WHERE source_pdf=hd.local_path) as pages
  FROM historical_documents hd
  WHERE hd.family_key LIKE \"nc-carolinas-%\"
  AND (SELECT COUNT(*) FROM tariff_charges WHERE family_key=hd.family_key) = 0
  AND pages > 0
  ORDER BY hd.family_key, hd.effective_start DESC
''').fetchall()
for r in rows: print(r)
"

# Find page 1 text for malformed families to identify proper schedule
python -c "
import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')
rows = conn.execute('''
  SELECT DISTINCT hd.family_key, SUBSTR(pa.text_content, 1, 100) as snip
  FROM historical_documents hd
  JOIN ncuc_page_artifacts pa ON pa.source_pdf = hd.local_path
  WHERE hd.family_key LIKE 'nc-carolinas-doc-%'
  AND pa.page_number = 1
  LIMIT 30
''').fetchall()
for r in rows: print(r[0], '|', r[1])
"
```
