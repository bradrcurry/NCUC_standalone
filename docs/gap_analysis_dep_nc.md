# DEP NC (Duke Energy Progress — North Carolina) Gap Analysis

**Last updated:** 2026-03-28
**DB State:** 8,818 charges | ~55 families with charges | ~100 leaf families total
**Download targets:** See [download_targets_dep_nc.md](download_targets_dep_nc.md) for prioritized official tariff sheet download queue.

## How to read this map

| Column | Meaning |
|--------|---------|
| STATUS | ✅ OK = has charges · ⚠️ PARTIAL = charges exist but incomplete · 🔴 PROFILE-NEEDED = docs+pages exist but profile can't extract · 🟡 CONTENT-TYPE = zero charges by design (no $/kWh rate) · 🔵 NEEDS-MINING = files on disk, 0 page artifacts · ❌ MISSING = no file on disk |
| CH | Charge count in DB as of last extraction |
| EVIDENCE | DR=discovery_records · HD=historical_docs · PA=page_artifacts · TS=official standalone tariff sheet |
| CONFIDENCE | Official tariff sheet (highest) · NCUC compliance filing · Extracted from order/procedural doc (lowest) |
| CLUE | Specific file names, docket numbers, or next action |

---

## Rate Schedules (leaf-500 series)

| Family | Schedule | STATUS | CH | Evidence | Confidence | Clue / Next Action |
|--------|----------|--------|----|----------|------------|---------------------|
| leaf-500 | RES — Residential Service | ✅ OK | 125 | DR+HD+PA+TS | Official tariff sheet | Current + 2024/2025 versions on disk |
| leaf-501 | R-TOUD — TOU Demand | ✅ OK | 1,463 | DR+HD+PA+TS | Official tariff sheet | Multiple historical versions; largest DEP rate family |
| leaf-502 | R-TOU — TOU Energy | ✅ OK | 88 | DR+HD+PA+TS | Official tariff sheet | |
| leaf-503 | R-TOU-CPP — Critical Peak Pricing | ✅ OK | 358 | DR+HD+PA+TS | Official tariff sheet | |
| leaf-504 | R-TOU-EV — EV TOU | ✅ OK | 16 | DR+HD+PA+TS | Official tariff sheet | |
| leaf-520 | SGS — Small General Service | ✅ OK | 28 | DR+HD+PA+TS | Official tariff sheet | |
| leaf-521 | SGS-TOUE | ✅ OK | 24 | TS mined | Official tariff sheet | |
| leaf-522 | SGS-TOU-CLR | ✅ OK | 16 | TS mined | Official tariff sheet | |
| leaf-523 | SGS-TOU-CPP | ✅ OK | 32 | TS mined | Official tariff sheet | |
| leaf-524 | MGS — Medium General Service | ✅ OK | 24 | TS mined | Official tariff sheet | |
| leaf-525 | MGS-TOU | ✅ OK | 48 | TS mined | Official tariff sheet | |
| leaf-526 | SI — Service Interruption | ✅ OK | 48 | TS mined | Official tariff sheet | |
| leaf-527 | CH-TOUE | ✅ OK | 56 | TS mined | Official tariff sheet | |
| leaf-528 | GS-TES | ✅ OK | 48 | TS mined | Official tariff sheet | |
| leaf-529 | APH-TES | ✅ OK | 48 | TS mined | Official tariff sheet | |
| leaf-532 | LGS — Large General Service | ✅ OK | 224 | DR+HD+PA+TS | Official tariff sheet | Multiple versions |
| leaf-533 | LGS-TOU | ✅ OK | 80 | TS mined | Official tariff sheet | |
| leaf-534 | LGS-RTP — Real Time Pricing | 🔴 PROFILE-NEEDED | 0 | TS mined, p=5 | Official tariff sheet | Content is a complex RTP formula referencing external index prices — no simple extractable $/kWh rate. Tariff sheet on disk: `leaf-no-534-schedule-lgs-rtp-*.pdf`. Also 252-page NCUC filing (E-2, hash=56963c43). |
| leaf-535 | HP — High Power | ✅ OK | 30 | TS mined | Official tariff sheet | |
| leaf-536 | LGS-HLF — High Load Factor | ✅ OK | 24 | TS mined | Official tariff sheet | |
| leaf-570 | ALS — Area Lighting Service | 🟡 CONTENT-TYPE | 0 | TS mined, p=5 | Official tariff sheet | Per-fixture-type flat fee ($/lamp/month) — not $/kWh. Format: "100W HPS Overhead Distribution: $X.XX/month per lamp". Would need a `ProgressLightingFlatRateProfile`. File: `leaf-no-570-schedule-als-*.pdf` |
| leaf-571 | SLS — Street Lighting Service | ✅ OK | 483 | DR+PA | Mixed NCUC sources | 483 charges from historical NCUC docs. Note: current tariff sheet shows $0.00 charges (lighting fee schedule). Historic data complete. |
| leaf-572 | SLR — Street Lighting Residential | 🟡 CONTENT-TYPE | 0 | TS mined, p=6 | Official tariff sheet | $/lamp/month per fixture type. Files on disk: `leaf-no-572-schedule-slr-*.pdf`, `street-lighting-service-residential-subdivisions-leaf-no-572-*.pdf`. Also 20-page NCUC doc (hash=88fb66f2). Needs lighting profile. |
| leaf-573 | SFLS — Seasonal Flood Lighting | ✅ OK | 6 | TS mined | Official tariff sheet | |
| leaf-574 | TSS — Traffic Signal Service | 🟡 CONTENT-TYPE | 0 | TS mined, p=3 | Official tariff sheet | Flat fee per signal type, no $/kWh. File: `leaf-no-574-schedule-tss-*.pdf` |
| leaf-575 | TFS — Traffic & Flood Signal | ✅ OK | 16 | TS mined | Official tariff sheet | |
| leaf-590 | PP — Pilot/Purchase Power | ✅ OK | 321 | DR+PA | NCUC discovery filings | Complex historical contract schedule; charges from NCUC filings |
| leaf-591 | T&C Purchase Power Terms | 🟡 CONTENT-TYPE | 0 | TS mined, p=14 | Official tariff sheet | Terms & conditions document, no rate table. File: `leaf-no-591-tandc-for-purchase-power-ry1-*.pdf` |
| leaf-592 | PPBE — Purchase Power By Energy | 🟡 CONTENT-TYPE | 0 | TS mined, p=9 | Official tariff sheet | Contract-based formula pricing, no fixed rate. File: `leaf-no-592-schedule-ppbe-ry-1-*.pdf` |

---

## Rider Summary Sheet (leaf-600)

| Family | Rider | STATUS | CH | Evidence | Confidence | Clue |
|--------|-------|--------|----|----------|------------|------|
| leaf-600 | Summary of Riders | ✅ OK | 3,494 | TS mined | Official tariff sheet | All-rider summary table; most complete DEP source. Versions: 2024/2025/2026 on disk. **Best single reference for current rider rates.** |

---

## Riders (leaf-601 to 674)

| Family | Rider | STATUS | CH | Evidence | Confidence | Clue / Next Action |
|--------|-------|--------|----|----------|------------|---------------------|
| leaf-601 | BA — Billing Adjustment | ⚠️ PARTIAL | 260 | DR+PA+TS | Official tariff sheet | Annual E-2 Sub 1142/1153 filings. Only 2017+ covered. **NCUC gap: E-2 Sub 1142 for pre-2017 BA rates.** |
| leaf-602 | JAA — Joint Agency Asset | 🔴 PROFILE-NEEDED | 0 | 61+ paged docs (all procedural) + 3 standalone sheets (0 pages) | Procedural only — standalone sheets unreadable | **PRIMARY GAP — DOWNLOAD NEEDED**. 3 standalone PDFs on disk are image-based (confirmed: `joint-agency-asset-rider-jaa-*.pdf`, 135KB–2.3MB, 0 page artifacts). Parser updated and tested — correctly extracts multi-class rate table format once text is available. Known 2017 rates: RES=0.00476, SGS=0.00542, MGS=0.00433, SI=0.00694, TSS=0.00261, Lighting=0.000 (all $/kWh), MGS-demand=1.42, LGS-demand=1.47 $/kW. **Action**: Download T1 official leaf from DEP website (`leaf-no-602-rider-jaa-ry*.pdf`) and T2 annual compliance tariff filings from NCUC E-2 Sub 1143 for each rate year. See [download_targets_dep_nc.md](download_targets_dep_nc.md). |
| leaf-604 | EDIT-4 — Excess Deferred Tax | ⚠️ PARTIAL | 14 | TS mined | Official tariff sheet | Only 2026 version (`leaf-no-604-rider-edit-4-ry1.pdf`). **Historical gap**: 2016–2020 versions in E-2 Sub 1196. |
| leaf-605 | CPRE — Competitive Procurement RE | ✅ OK | 4 | TS mined | Official tariff sheet | Multi-class RESIDENTIAL/SGS/MGS/LGS/LIGHTING SERVICE format. Only current version. File: `leaf-no-605-rider-cpre-ry1.pdf`. |
| leaf-606 | DSM | ❌ MISSING | 0 | No docs found | — | Check DEP website for `leaf-no-606-rider-dsm.pdf`. No NCUC discovery records found. May be retired or subsumed into leaf-700 series. |
| leaf-607 | STS — Storm Securitization | ✅ OK | 45 | TS mined | Official tariff sheet | 2025+2026 versions on disk. **Historical gap**: 2015–2022 in E-2 Sub 1204. |
| leaf-608 | RDM — Revenue Decoupling | ✅ OK | 20 | TS mined | Official tariff sheet | **Historical gap**: 2015–2022 in E-2 Sub 1294. |
| leaf-609 | ESM — Energy Storage Mgmt | ✅ OK | 7 | TS mined | Official tariff sheet | Rate present. Content is formula-based; low charge count expected. |
| leaf-610 | PIM — Performance Incentive | ✅ OK | 5 | TS mined | Official tariff sheet | |
| leaf-611 | CAR — Customer Assistance Recovery | ✅ OK | 4 | TS mined | Official tariff sheet | Mixed $/kWh (residential) + $/bill (commercial). File: `leaf-no-611-rider-car-ry1.pdf`. |
| leaf-613 | STS2 — Storm Securitization 2 | ✅ OK | 214 | TS mined | Official tariff sheet | Multiple versions on disk. **Historical gap**: 2022–2024 versions in E-2 Sub 1204. |
| leaf-640 | RECD — Energy Conservation Discount | 🟡 CONTENT-TYPE | 0 | TS mined, p=2 | Official tariff sheet | Rate is **"5% of stated kilowatt and kilowatt-hour charges"** — percentage credit, not a fixed $/kWh value. Zero charges is correct behavior. File: `leaf-no-640-rider-recd-*.pdf`. Also confirmed by 2024 version: `residential-ev-customer-demand-rider-leaf-no-640-*.pdf`. |
| leaf-641 | NM — Net Metering | ⚠️ PARTIAL | 3 | TS mined | Official tariff sheet | Only 3 charges. Check if additional rate components weren't captured. |
| leaf-642 | GP — Green Power | ✅ OK | 19 | TS mined | Official tariff sheet | |
| leaf-643 | REN — Renewable Energy | ✅ OK | 19 | TS mined | Official tariff sheet | |
| leaf-644 | COP — Carbon Offset Program | 🟡 CONTENT-TYPE | 0 | TS mined, p=2 | Official tariff sheet | Voluntary block-purchase program ($/block). Not a per-kWh rate. File: `leaf-no-644-rider-cop-ry1-*.pdf`. |
| leaf-645 | Rider-18 — Public Housing | 🟡 CONTENT-TYPE | 0 | TS mined, p=1 | Official tariff sheet | **INVESTIGATED**: Service eligibility terms for public housing projects using SGS schedule. No rate modifier — allows electricity redistribution to tenants. No extractable $/kWh. |
| leaf-646 | CM — Campground/Marina | 🟡 CONTENT-TYPE | 0 | TS mined, p=1 | Official tariff sheet | **INVESTIGATED**: Service eligibility terms for campground/marina resale. No rate modifier value. |
| leaf-647 | Rider-28 — Military Service | 🟡 CONTENT-TYPE | 0 | TS mined, p=1 | Official tariff sheet | **INVESTIGATED**: Service eligibility terms for military base service redistribution. No rate modifier. |
| leaf-648 | TR — Transition Rider | 🟡 CONTENT-TYPE | 0 | TS mined, p=1 | Official tariff sheet | **INVESTIGATED**: Transition eligibility criteria for customers moving from SGS-TOU/MGS to LGS. No rate value on tariff sheet. Note: other filings linked to this family (coal inventory, JAA application) are misfiled. |
| leaf-649 | US — Unmetered Service | 🟡 CONTENT-TYPE | 0 | TS mined, p=1 | Official tariff sheet | **INVESTIGATED**: kWh equivalence table by wattage rating (e.g., ≤10W = 0 kWh, 11–50W = 15 kWh/month). Not a rate — defines assumed consumption for unmetered connections. |
| leaf-650 | Rider-9 — Fluctuating Load | 🟡 CONTENT-TYPE | 0 | TS mined, p=1 | Official tariff sheet | **INVESTIGATED**: $0.41/kVa for highly fluctuating/intermittent loads (welding, X-ray, elevators). Non-standard kVa unit; single penalty charge for specialized equipment, not a standard $/kWh adjustment. |
| leaf-651 | Rider-7 — Standby Service | 🟡 CONTENT-TYPE | 0 | TS mined, p=3 | Official tariff sheet | **INVESTIGATED**: Complex demand-based charges: $1.30/kW for first 200 kW, $1.00/kW for next 24,800 kW, $1.60/kW additional. Standby/supplementary service modification, not a simple rate adjustment. |
| leaf-652 | Rider-57 — Supp/Interruptible Standby | 🟡 CONTENT-TYPE | 0 | TS mined, p=4 | Official tariff sheet | **INVESTIGATED**: $0.97/kW failure-to-interrupt penalty; complex kWh minimum formula based on supplementary/standby split. Not a standard $/kWh adjustment. |
| leaf-653 | SS — Standby Service | ✅ OK | 95 | TS mined | Official tariff sheet | |
| leaf-654 | NFS — Natural Gas Fuel Supplement | ✅ OK | 48 | TS mined | Official tariff sheet | |
| leaf-655 | LLC | ✅ OK | 46 | TS mined | Official tariff sheet | |
| leaf-656 | Rider-68 | ✅ OK | 8 | TS mined | Official tariff sheet | |
| leaf-657 | IPS | ✅ OK | 8 | TS mined | Official tariff sheet | |
| leaf-658 | ED — Economic Development | ✅ OK | 112 | TS mined | Official tariff sheet | |
| leaf-659 | ERD — Economic Redevelopment | 🟡 CONTENT-TYPE | 0 | TS mined, p=3 | Official tariff sheet | Company-discretion rate — offered case-by-case to qualifying industries. No fixed $/kWh. File: `leaf-no-659-rider-erd-ry1-*.pdf`. |
| leaf-660 | PPS — Premier Power Service | 🟡 CONTENT-TYPE | 0 | TS mined, p=5+13 | Official tariff sheet + NCUC compliance filing | **INVESTIGATED**: Contract-based backup generation service with warranty terms, site access conditions, and contractor provisions. The "Monthly Rate" referenced is a contract-negotiated amount, not a published tariff rate. No extractable fixed $/kWh rate. |
| leaf-661 | MROP — Meter Related Optional | ✅ OK | 139 | TS mined | Official tariff sheet | |
| leaf-662 | EPPWP — Elec from Waste Power | 🟡 CONTENT-TYPE | 0 | TS mined, p=2 | Official tariff sheet | Formula-based avoided-cost rate. File: `leaf-no-662-rider-eppwp-ry1-*.pdf`. |
| leaf-663 | SRR — Solar Rebate Rider | ❌ MISSING | 0 | 7 procedural docs only (all NCUC orders, no rate sheet) | Procedural only | All 7 docs are commission orders from E-2 docket. **Standalone tariff sheet NOT on disk.** Target: DEP website `/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-663-rider-srr.pdf`. |
| leaf-664 | SSR — Solar Savings Rider | 🟡 CONTENT-TYPE | 0 | TS mined, p=2 | Official tariff sheet | Program description, no per-kWh rate. File: `leaf-no-664-rider-ssr-ry1-*.pdf`. |
| leaf-665 | GSA — Green Source Advantage | ✅ OK | 30 | TS mined | Official tariff sheet | |
| leaf-666 | GR — Grid Resilience | 🟡 CONTENT-TYPE | 0 | TS mined, p=2 | Official tariff sheet | Program terms. File: `leaf-no-666-rider-gr-*.pdf`. |
| leaf-667 | EC — Energy Credits | 🟡 CONTENT-TYPE | 0 | TS mined, p=4 | Official tariff sheet | Program terms. File: `leaf-no-667-rider-ec-ry1-*.pdf`. |
| leaf-668 | NSC — New Service Contracts | ✅ OK | 13 | TS mined | Official tariff sheet | |
| leaf-669 | NMB — Net Metering Buyback | ✅ OK | 16 | TS mined | Official tariff sheet | |
| leaf-670 | RSC — Residential Solar Choice | ✅ OK | 206 | TS mined | Official tariff sheet | |
| leaf-671 | GSAC — Green Source Advantage C | ✅ OK | 8 | TS mined | Official tariff sheet | |
| leaf-672 | CEI — Customer Energy Initiative | ✅ OK | 6 | TS mined | Official tariff sheet | |
| leaf-674 | PS — Power Subscription | ✅ OK | 59 | TS mined | Official tariff sheet | |

---

## DSM Programs (leaf-700 series)

These are program descriptions. Zero charges expected unless the program has explicit incentive dollar amounts.

| Family | Program | STATUS | CH | Evidence | Notes |
|--------|---------|--------|----|----------|-------|
| leaf-700 | NSSEE | 🟡 CONTENT-TYPE | 0 | TS on disk, p=3 | Program rebates |
| leaf-701 | SBES | 🟡 CONTENT-TYPE | 0 | TS on disk, p=3 | |
| leaf-702 | SSP | 🟡 CONTENT-TYPE | 0 | TS on disk, p=2 | |
| leaf-703 | RSNES | 🟡 CONTENT-TYPE | 0 | TS on disk, p=2 | |
| leaf-704 | RSSEE | 🟡 CONTENT-TYPE | 0 | TS on disk, p=2 | Also E-2 compliance tariff (6p, 2023) |
| leaf-705 | EEL | 🟡 CONTENT-TYPE | 0 | TS on disk, p=1 | |
| leaf-706 | EWB — EnergyWise for Business | ✅ OK | 124 | TS mined | Has extractable incentive rates |
| leaf-707 | RS-HERP — Residential HVAC | 🟡 CONTENT-TYPE | 0 | TS on disk, p=1 | Also 82p (2018) + 46p (2009) NCUC filings |
| leaf-708 | RNC | 🟡 CONTENT-TYPE | 0 | TS on disk, p=2 | 40-page NCUC doc (2016) exists |
| leaf-709 | EEE | 🟡 CONTENT-TYPE | 0 | TS on disk, p=1 | |
| leaf-710 | MEE | 🟡 CONTENT-TYPE | 0 | TS on disk, p=1 | |
| leaf-711 | REA | 🟡 CONTENT-TYPE | 0 | TS on disk, p=2 | |
| leaf-712 | LWP — Low Income Weatherization | 🟡 CONTENT-TYPE | 0 | TS on disk, p=1 | 68-page NCUC doc (2015) exists |
| leaf-713 | REEAD | 🟡 CONTENT-TYPE | 0 | TS on disk, p=1 | 3-page NCUC doc (2018) exists |
| leaf-714 | LC-WIN | 🟡 CONTENT-TYPE | 0 | TS on disk, p=3 | |
| leaf-715 | LC-IQ | 🟡 CONTENT-TYPE | 0 | TS on disk, p=2 | |
| leaf-716 | SSR — SunSense Solar Rebate | ✅ OK | 57 | TS mined | Has extractable incentive rates |
| leaf-717 | DRA — Demand Response Automation | ✅ OK | 56 | TS mined | |
| leaf-718 | CAP — Customer Assistance Program | ✅ OK | 44 | TS mined | |
| leaf-719 | IWZ | 🟡 CONTENT-TYPE | 0 | TS on disk, p=2 | |
| leaf-720 | PPA — Property-Assessed Loans | 🟡 CONTENT-TYPE | 0 | TS on disk, p=2 | |
| leaf-721 | TOB — Team Orange Bounce Back | 🟡 CONTENT-TYPE | 0 | TS on disk, p=4 | 49-page NCUC doc (2019) + 33-page joint DEC/DEP filing |
| leaf-722 | TOBM | 🟡 CONTENT-TYPE | 0 | TS on disk, p=2 | |
| leaf-723 | TOBR | 🟡 CONTENT-TYPE | 0 | TS on disk, p=3 | 12-page joint DEC/DEP filing (2023) |
| leaf-724 | YFB — Your Fixed Bill | 🟡 CONTENT-TYPE | 0 | TS on disk, p=5 | 2026 flat-fee program |
| leaf-725 | RIQLC | 🟡 CONTENT-TYPE | 0 | TS on disk, p=3 | |
| leaf-740 | EVSB — EV Smart Bounce Back | 🟡 CONTENT-TYPE | 0 | TS on disk, p=2 | EV rebate program |
| leaf-741 | FCS — Fast Charge Station | 🟡 CONTENT-TYPE | 0 | TS on disk, p=1 | |
| leaf-742 | L2EV — Level 2 EV | 🟡 CONTENT-TYPE | 0 | TS on disk, p=1 | |
| leaf-743 | MFEV — Multifamily EV | 🟡 CONTENT-TYPE | 0 | TS on disk, p=1 | |
| leaf-744 | MREV — Medium/Large EV | 🟡 CONTENT-TYPE | 0 | TS on disk, p=6 | 30-page joint DEC/DEP filing exists |
| leaf-745 | EVSE | 🟡 CONTENT-TYPE | 0 | TS on disk, p=4 | |
| leaf-770 | PowerPair — Battery+Solar | ✅ OK | 27 | TS mined | Has incentive rates |

---

## Service Regulations (leaf-800 series)

No extractable tariff rates.

| Family | Doc | STATUS | CH | Evidence | Notes |
|--------|-----|--------|----|---------|-------|
| leaf-800 | Service Regulations | ✅ OK | 2 | TS on disk, p=11 | 2 charges (likely admin fees extracted) |
| leaf-801 | Outdoor Lighting Regs | 🟡 CONTENT-TYPE | 0 | TS on disk, p=4 | Service terms |
| leaf-802 | Line Extension Plan | 🟡 CONTENT-TYPE | 0 | TS on disk, p=10 | Also 2026 new version: `line-extension-plan-lep-ncuc-psc-*.pdf` (0 pages mined yet) |
| leaf-803 | Standard Service Voltages | 🟡 CONTENT-TYPE | 0 | TS on disk, p=3 | Service terms |

---

## Historical Rider Version Gaps (NCUC Portal Targets)

Families with current charges but **missing earlier versions** for complete bill reconstruction timeline.

| Family | Rider | Period Gap | Docket | Known Files | Priority |
|--------|-------|-----------|--------|-------------|----------|
| leaf-601 | BA — Billing Adjustment | 2015–2016 | E-2 Sub 1142, 1153 | Annual adjustment compliance tariff exhibits | HIGH |
| leaf-602 | JAA — Joint Agency Asset | ALL VERSIONS | E-2 Sub 1143 | 3 standalone PDFs on disk with 0 pages (image-based). `e-2-nodate-duke-energy-progress-annual-jaa-cost-recovery-application.pdf` (246p) — rate in attachment exhibits | HIGH |
| leaf-604 | EDIT-4 | 2016–2020 | E-2 Sub 1196 | Only 2026 version extracted | MED |
| leaf-607 | STS | 2015–2022 | E-2 Sub 1204 | Only 2025/2026 extracted | MED |
| leaf-608 | RDM | 2015–2022 | E-2 Sub 1294 | Only 2025 extracted | MED |
| leaf-613 | STS2 | 2022–2024 | E-2 Sub 1204 | Only 2025 extracted | MED |

---

## Actionable Fix Summary

### Priority 1 — Profile/Extraction Fixes (files on disk, no download needed)
| Task | Family | File(s) | Action |
|------|--------|---------|--------|
| Read page content, write profile | leaf-660 PPS | `leaf-no-660-rider-pps-ry1-*.pdf` (5p) + 13p NCUC filing | Determine rate structure; add `ProgressSingleValueRiderProfile` variant |
| Verify expired/zero rate | leaf-645/646/647/648/649/650 | `leaf-no-64X-rider-*.pdf` (1–2p each) | Read page 1; mark CONTENT-TYPE if $0.000 or expired |
| Read page content | leaf-651/652 | `leaf-no-651-rider-7-*.pdf` (3p), `leaf-no-652-rider-57-ry1-*.pdf` (4p) | May have extractable rates |

### Priority 2 — OCR/Docling Needed
| Task | Family | File(s) | Action |
|------|--------|---------|--------|
| OCR image-based PDFs | leaf-602 JAA | `joint-agency-asset-rider-jaa-ncuc-viewfile-aspx-id-*.pdf` (3 files) | Run `mine-docling-nc` or manual Docling on these specific files |

### Priority 3 — Download Needed
| Task | Family | Target | Action |
|------|--------|--------|--------|
| Download tariff sheet | leaf-663 SRR | `/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-663-rider-srr.pdf` | Add to download queue |
| Historical versions | leaf-601/602/607/608/613 | E-2 Sub 1142, 1143, 1204, 1294 | NCUC portal search for compliance tariff exhibits |

### Priority 4 — New Profile Type
| Task | Families | Description |
|------|---------|-------------|
| `ProgressLightingFlatRateProfile` | leaf-570, leaf-572 | Read $/lamp/month per fixture-type tables |

---

## Commands

```bash
# Mine all unprocessed standalone tariff sheets
python -m duke_rates mine-tariff-sheets-nc

# Mine a specific family
python -m duke_rates mine-tariff-sheets-nc --family nc-progress-leaf-602

# Extract charges from newly mined docs
python -m duke_rates extract-rates-nc

# Full charge count by family
python -c "
import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')
rows = conn.execute('''
  SELECT family_key, COUNT(*) as ch FROM tariff_charges
  WHERE family_key LIKE \"nc-progress-leaf-%\" GROUP BY family_key
  ORDER BY CAST(SUBSTR(family_key, 18) AS INTEGER)
''').fetchall()
for r in rows: print(r)
"

# Families with pages but 0 charges (potential extraction gaps)
python -c "
import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')
rows = conn.execute('''
  SELECT DISTINCT hd.family_key,
    (SELECT COUNT(*) FROM ncuc_page_artifacts WHERE source_pdf=hd.local_path) as pages
  FROM historical_documents hd
  WHERE hd.family_key LIKE 'nc-progress-leaf-%'
  AND (SELECT COUNT(*) FROM tariff_charges WHERE family_key=hd.family_key) = 0
  AND pages > 0
  ORDER BY hd.family_key
''').fetchall()
for r in rows: print(r)
"
```
