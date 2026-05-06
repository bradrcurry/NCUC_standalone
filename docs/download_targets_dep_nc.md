# DEP NC — Official Tariff Sheet Download Targets

**Last updated:** 2026-03-28
**Purpose:** Prioritized list of official tariff sheets to download to fill extraction gaps.
Focus on highest-confidence documents only: official DEP website tariff sheets and
NCUC compliance tariff exhibits (not rate case testimony, not applications, not orders).

---

## Document Quality Tiers

| Tier | Type | Confidence | Where Found |
|------|------|-----------|-------------|
| T1 | Official DEP current tariff sheet | Highest | `/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-XXX-*.pdf` |
| T2 | NCUC compliance tariff exhibit | High | NCUC portal → docket → "Compliance Tariff" filing attachment |
| T3 | Rate case stipulation tariff exhibit | Medium | NCUC portal → rate case docket → "Revised Tariff" attachment |
| T4 | Application with embedded rate table | Low | NCUC portal → sub-docket application, exhibit attachment |
| — | Commission orders, testimony, procedural | Skip | Not rate data — skip unless no T1/T2/T3 exists |

**Rule:** Do not spend time mining T4 or below until T1–T3 have been exhausted.

---

## Priority 1 — DEP Website Direct Downloads (T1)

These are official current tariff sheets with known URL patterns. Check DEP website first.

URL pattern: `https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/FILENAME.pdf`

| Family | Rider/Schedule | Likely Filename | Why Needed | Status |
|--------|---------------|----------------|------------|--------|
| leaf-602 | JAA — Joint Agency Asset | `leaf-no-602-rider-jaa-ry1.pdf` or `leaf-no-602-rider-jaa-ry2.pdf` | Current rate needed; standalone on disk is image-based | ❌ Not downloaded |
| leaf-663 | SRR — Solar Rebate Rider | `leaf-no-663-rider-srr.pdf` | No tariff sheet on disk at all; 7 procedural docs only | ❌ Not downloaded |
| leaf-606 | DSM | `leaf-no-606-rider-dsm.pdf` | No documents found at all | ❌ Not downloaded |

---

## Priority 2 — NCUC Portal Compliance Tariff Exhibits (T2)

These are annual rate filings where the utility files a standalone tariff leaf as an attachment.
Each docket sub-number typically has a different rate (annual adjustment).

**Search approach:** NCUC portal → Docket search → Enter docket → filter "compliance tariff" or "revised tariff" → look for PDF attachments titled "Rider XXX" or "Leaf No. XXX".

### leaf-602 JAA — Joint Agency Asset Rider (ALL HISTORICAL VERSIONS)

| Target | Docket | Filing Type | Approx Rate Period | Notes |
|--------|--------|-------------|-------------------|-------|
| JAA current standalone leaf | E-2 Sub 1143 | Annual compliance tariff | 2024–2025 | Latest version; current rate |
| JAA 2023 | E-2 Sub 1143 | Annual compliance tariff | 2023–2024 | |
| JAA 2022 | E-2 Sub 1143 | Annual compliance tariff | 2022–2023 | |
| JAA 2021 | E-2 Sub 1143 | Annual compliance tariff | 2021–2022 | |
| JAA 2020 | E-2 Sub 1143 | Annual compliance tariff | 2020–2021 | |
| JAA 2018/2019 | E-2 Sub 1143 | Annual compliance tariff | 2018–2020 | 3 standalone PDFs on disk but IMAGE-BASED (135KB–2.3MB, 0 pages) — need text-layer versions from portal |
| JAA 2017 | E-2 Sub 1143 | Annual compliance tariff | 2017–2018 | Rate table found in exhibit: 0.00476–0.00542 $/kWh by class |

**Known rate data from 2017 annual filing (E-2 Sub 1143, page 7):**
```
Rate Class              Schedule(s)                 Rate ($/kWh or $/kW)
Residential             RES, R-TOUD, R-TOUE, R-TOU  0.00476 $/kWh
Small General Service   SGS, SGS-TOUE               0.00542 $/kWh
Medium General Service  CH-TOUE, CSE, CSG            0.00433 $/kWh
Seasonal/Intermittent   SI                           0.00694 $/kWh
Traffic Signal Service  TSS, TFS                     0.00261 $/kWh
Outdoor Lighting        ALS, SLS, SLR, SFLS          0.00000 $/kWh
Medium General (demand) MGS, GS-TES, AP-TES, SGS-TOU 1.42 $/kW
Large General (demand)  LGS, LGS-TOU                 1.47 $/kW
```
This is an OCR table from the 246-page annual filing — useful as reference but parser extracts wrong unit from multi-class tables in procedural docs. **Get standalone leaf instead.**

### leaf-601 BA — Billing Adjustment (Historical Versions)

| Target | Docket | Filing Type | Approx Rate Period | Notes |
|--------|--------|-------------|-------------------|-------|
| BA 2016 | E-2 Sub 1142 | Annual adjustment compliance | 2016–2017 | |
| BA 2015 | E-2 Sub 1142 | Annual adjustment compliance | 2015–2016 | |

### leaf-604 EDIT-4 (Historical Versions)

| Target | Docket | Filing Type | Approx Rate Period | Notes |
|--------|--------|-------------|-------------------|-------|
| EDIT-4 2016–2020 | E-2 Sub 1196 | Compliance tariff | 2016–2020 | Multiple years — each sub filing has new rate |

### leaf-607 STS — Storm Securitization (Historical)

| Target | Docket | Filing Type | Approx Rate Period | Notes |
|--------|--------|-------------|-------------------|-------|
| STS 2015–2022 | E-2 Sub 1204 | Compliance tariff | 2015–2022 | Annual adjustments |

### leaf-608 RDM — Revenue Decoupling (Historical)

| Target | Docket | Filing Type | Approx Rate Period | Notes |
|--------|--------|-------------|-------------------|-------|
| RDM 2015–2022 | E-2 Sub 1294 | Compliance tariff | 2015–2022 | Annual adjustments |

### leaf-613 STS2 — Storm Securitization 2 (Historical)

| Target | Docket | Filing Type | Approx Rate Period | Notes |
|--------|--------|-------------|-------------------|-------|
| STS2 2022–2024 | E-2 Sub 1204 | Compliance tariff | 2022–2024 | |

---

## Priority 3 — Rate Schedule Historical Versions (T2/T3)

Current versions are on disk. These are earlier versions needed for bill reconstruction timeline completeness.

| Family | Schedule | Target Period | Docket | Notes |
|--------|---------|--------------|--------|-------|
| leaf-500 | RES | 2015–2022 | E-2 (rate cases) | Only 2024/2025 on disk |
| leaf-501 | R-TOUD | 2015–2022 | E-2 (rate cases) | Strong history exists; check if older versions needed |
| leaf-532 | LGS | 2015–2022 | E-2 (rate cases) | |

---

## Do NOT Mine — Confirmed Non-Rate Documents

These families have been investigated and confirmed to contain no extractable rates.
**Do not spend time trying to build profiles for these.**

| Family | Content | Why No Rate |
|--------|---------|------------|
| leaf-640 RECD | "5% of stated charges" | Percentage credit, not $/kWh |
| leaf-645 Rider-18 | Public Housing eligibility terms | Service terms, no rate modifier |
| leaf-646 Rider-CM | Campground/Marina eligibility | Service terms, no rate modifier |
| leaf-647 Rider-28 | Military base service terms | Service terms, no rate modifier |
| leaf-648 Rider-TR | Transition eligibility criteria | No rate table on tariff sheet |
| leaf-649 Rider-US | Unmetered service kWh table | kWh equivalences, not a rate |
| leaf-650 Rider-9 | $0.41/kVa for fluctuating load | Non-standard kVa unit, 1 doc |
| leaf-651 Rider-7 | $1.30–$1.60/kW standby demand | Complex demand penalty, not $/kWh |
| leaf-652 Rider-57 | $0.97/kW interruptible penalty | Penalty rate, not standard adjustment |
| leaf-659 ERD | Company-discretion rate | No published fixed rate |
| leaf-660 PPS | Contract service terms + warranty | No rate table |
| leaf-662 EPPWP | Avoided-cost formula | Formula-based, no fixed rate |
| leaf-591 T&C | Purchase power terms | Terms document |
| leaf-592 PPBE | Contract-formula purchase power | No fixed rate |
| leaf-534 LGS-RTP | Real-time pricing formula | External index-referenced |
| leaf-664 SSR | Program description | No rate |
| leaf-666 GR | Grid Resilience program terms | No rate |
| leaf-667 EC | Energy Credits program terms | No rate |
| leaf-663 SRR | 7 procedural docs only | Need to download actual tariff sheet |

---

## NCUC Portal Search Instructions

```
1. Go to: https://www.docket.ncuc.org/
2. Search → Docket Number → Enter: E-2 (for Progress riders)
3. Sub-docket filter: enter sub number (e.g., 1143 for JAA)
4. Look for filings titled: "Compliance Tariff" or "Revised Tariff" or "Annual Adjustment"
5. In filing attachments, look for PDF labeled: "Rider JAA Leaf No. 602" or similar
6. Download the standalone leaf PDF — NOT the cover letter or order
```

**Key tip:** The standalone leaf PDF is typically a 1–3 page document titled
`"Duke Energy Progress, LLC NC [Nth] Revised Leaf No. [NNN]"` at the top.
Cover letters from attorneys are NOT the tariff — they just reference it.

---

## File Naming Conventions

DEP website tariff sheets follow this pattern:
- Current: `leaf-no-602-rider-jaa-ry1.pdf` (ry = rate year, 1 = first filing)
- Historical: `leaf-no-602-rider-jaa-ry2.pdf`, `leaf-no-602-rider-jaa-ry3.pdf`, etc.

NCUC portal exhibits are typically named generically (like `exhibit-a.pdf`) — rename on download to include leaf number and effective date.
