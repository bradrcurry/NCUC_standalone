# DEC NC — Official Tariff Sheet Download Targets

**Last updated:** 2026-04-07  
**Purpose:** Prioritized DEC-specific document harvest queue for the next authenticated NCUC portal session.
Focus on highest-confidence missing or still-useful documents so the next pass can maximize
downloads first, then leave registration/mining/parsing as follow-on work.

---

## Document Quality Tiers

| Tier | Type | Confidence | Where Found |
|------|------|-----------|-------------|
| T1 | Official current DEC tariff sheet | Highest | `p-cd.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/` |
| T2 | NCUC compliance tariff exhibit / standalone leaf | High | NCUC portal docket attachment |
| T3 | Large compliance tariff book with bounded schedule pages | Medium | NCUC portal multi-schedule filing |
| T4 | Procedural / application / testimony with embedded rate table | Low | NCUC portal only if no T1–T3 exists |
| — | Orders, cover letters, redlines, service regulations | Skip unless needed for lineage | |

**Rule:** Prefer T1 standalone leaves first, then T2 standalone compliance leaves, then T3 large books.

---

## Priority 1 — Direct T1 / Current Standalone Leaves

These should usually be downloaded even if partial historical coverage already exists, because they
give clean text-layer anchors and canonical current formatting.

| Family | Rider / Schedule | Likely Filename / URL Pattern | Why |
|--------|------------------|-------------------------------|-----|
| `nc-carolinas-rider-STS` | Storm Securitization Rider | `ncridersts.pdf` | Current canonical storm leaf; semiannual updates matter |
| `nc-carolinas-rider-EDPR` | Existing DSM Program Costs Adjustment Rider | `ncrideredpr.pdf` | Current canonical EDPR anchor |
| `nc-carolinas-rider-EDIT4` | Excess Deferred Income Tax Rider #4 | `ncrideredit4*.pdf` | Clean single-value rider anchor |
| `nc-carolinas-rider-RDM` | Residential Decoupling Mechanism | `nc-rider-rdm*.pdf` | Current single-value rider anchor |
| `nc-carolinas-rider-PIM` | Performance Incentive Mechanism | `nc-rider-pim*.pdf` | Current single-value rider anchor |
| `nc-carolinas-schedule-BC` | Business Customer | `ncscheduleBC*.pdf` | Billing-relevant schedule omitted from focused matrix but populated in DB |
| `nc-carolinas-schedule-RE` | Residential Experimental | `ncscheduleRE*.pdf` | Billing-relevant schedule omitted from focused matrix; historical mining still weak |
| `nc-carolinas-schedule-PP` | Purchase Power | `ncschedulePP*.pdf` | Existing file is on disk but not yet mined cleanly |

---

## Priority 2 — High-Value NCUC Compliance Leaves (T2)

These are the strongest portal targets because they are likely to be standalone tariff sheets
or rider-specific attachments.

### DEC Rider STS — Storm Securitization

| Target | Docket | Filing Type | What to look for |
|--------|--------|-------------|------------------|
| Current STS leaf | `E-7 Sub 1243` | Compliance tariff / revised tariff | Standalone Rider STS leaf attachment |
| Storm Debby STS filings | `E-7 Sub 1321` | Compliance tariff / revised tariff | Separate storm-specific STS attachment if one exists |
| Storm Helene STS filings | `E-7 Sub 1325` | Compliance tariff / revised tariff | Separate storm-specific STS attachment if one exists |

**Search terms:** `"Rider STS"` | `"Storm Securitization"` | `"Leaf 133"` | `"compliance tariff"`

### DEC Rider EDPR — Existing DSM Program Costs Adjustment Rider

| Target | Docket | Filing Type | What to look for |
|--------|--------|-------------|------------------|
| Current EDPR annual filing | `E-7 Sub 1276` | Annual compliance tariff | Standalone EDPR leaf attachment |
| Historical EDPR annual filings | `E-7 Sub 487`, `828`, `1026`, `1146`, `1165` | Annual compliance tariff / revised tariff | Annual EDPR tariff leaf by sub-docket/year |

**Search terms:** `"Rider EDPR"` | `"Existing DSM"` | `"Leaf No. 64"` | `"annual compliance"`

### DEC Rider EDIT-4

| Target | Docket | Filing Type | What to look for |
|--------|--------|-------------|------------------|
| Historical EDIT-4 leaves | `E-7 Sub 1213`, `1214`, `1187`, `1152`, `1146` | Compliance tariff | Standalone Leaf 131 or rider-specific attachment |

**Search terms:** `"EDIT-4"` | `"Leaf 131"` | `"Excess Deferred Income Tax Rider #4"`

### DEC Rider PM

| Target | Docket | Filing Type | What to look for |
|--------|--------|-------------|------------------|
| PM rider tariff leaf | `E-7 Sub 1168` | Compliance tariff / revised tariff | Rider PM leaf separated from the 42-page modification filing |

**Search terms:** `"Rider PM"` | `"Performance Mechanism"` | `"compliance tariff"`

### DEC Rider CAR / GS / ED

These remain ambiguous or mislinked and should be treated as discovery targets, not parser-only work.

| Family | Docket Hint | What to look for |
|--------|-------------|------------------|
| `nc-carolinas-rider-CAR` | `E-7` various | True Customer Assistance Recovery standalone leaf, not carbon-offset docs |
| `nc-carolinas-rider-GS` | `E-7` various | True Green Source rider leaf, if that family is real and still active |
| `nc-carolinas-rider-ED` | `E-7` various | Economic Development rider leaf, if it exists separately from EV program docs |

**Search terms:** `"Rider CAR"` | `"Customer Assistance Recovery"` | `"Rider GS"` | `"Green Source"` | `"Rider ED"` | `"Economic Development"`

---

## Priority 3 — Large Compliance Books Worth Bounded Mining (T3)

These are already useful sources even if the portal yields no standalone leaf attachment.

| Family / Use | Best Known Source | Why it matters |
|--------------|-------------------|----------------|
| `nc-carolinas-rider-CEI` | `e-7-nodate-dec-dep-compliance-tariffs-rider-clean-energy-impact.pdf` (45p) | Joint DEC/DEP filing likely contains actual CEI rate pages |
| `nc-carolinas-schedule-RE` | `e31d1ef2-78de-4dab-8f84-5b6b6cb94c30.pdf` (146p, 2021-12-16) | Likely clean RE schedule pages inside bounded compliance book |
| `nc-carolinas-schedule-BC` | `e-7-nodate-duke-energy-carolinas-llc-s-revisions-to-rate-compliance-filing-of-approved-tar*.pdf` (300p, 2013-10-23) | Likely clean BC schedule pages inside large 2013 compliance filing |
| `nc-carolinas-schedule-RS` / `RT` | Same 300-page and 146-page books | Useful if additional residential historical cleanup is needed |

**Note:** These should usually be mined/bounded before spending time on lower-confidence procedural filings.

---

## Priority 4 — Existing On-Disk Files To Check Before Re-Downloading

These may already exist locally and simply need page mining, re-linking, or confirmation.

| Family | Existing File Clue | Next step |
|--------|--------------------|-----------|
| `nc-carolinas-rider-STS` | `d9c03aa1-*.pdf`, `aa8985bf-*.pdf` | Verify whether they are only cover letters; if yes, still download the real attachment |
| `nc-carolinas-rider-EDPR` | `8182204e`, `1bf9bf84`, `f807656f` | Check page artifacts and whether any are actual rate leaves |
| `nc-carolinas-schedule-PP` | `pp-media-pdfs-for-your-home-rates-electric-nc-ncschedulepp-p-*.pdf` | Mine current T1 file before searching more |
| `nc-carolinas-rider-CEI` | 45-page joint compliance filing | Bound likely rider pages before more discovery |

---

## Priority 5 — Historical Rate Books / Legacy Families

Only after the higher-confidence targets above.

| Source | Use |
|--------|-----|
| `e-7-nodate-duke-s-rate-schedule.pdf` (484p) | Legacy fallback for schedule/rider sections still trapped in malformed `doc-*` keys |
| `e-7-nodate-duke-s-revised-nc-rate-schedule-and-riders.pdf` (272p) | Legacy fallback / historical reconstruction |
| `e-7-nodate-duke-power-s-rate-schedule-riders.pdf` (98p) | Older Duke Power era rider/schedule reference |

These are better for bounded mining and re-linking than for first-pass harvesting.

---

## NCUC Portal Search Terms (DEC)

Use these in parameter search or text search:

- `"compliance tariff"`
- `"revised tariff"`
- `"annual adjustment"`
- `"Rider STS"`
- `"Storm Securitization"`
- `"Rider EDPR"`
- `"Existing DSM"`
- `"EDIT-4"`
- `"Leaf 131"`
- `"Rider CEI"`
- `"Clean Energy Impact"`
- `"Schedule RE"`
- `"Schedule BC"`
- `"Schedule PP"`
- `"Rider CAR"`
- `"Green Source"`
- `"Economic Development"`

Key docket anchors:

- `E-7 Sub 1243`
- `E-7 Sub 1321`
- `E-7 Sub 1325`
- `E-7 Sub 1276`
- `E-7 Sub 1146`
- `E-7 Sub 1213`
- `E-7 Sub 1214`
- `E-7 Sub 1187`
- `E-7 Sub 1152`
- `E-7 Sub 1168`
- `E-7 Sub 487`
- `E-7 Sub 828`
- `E-7 Sub 1026`
- `E-7 Sub 1165`

---

## Harvest Workflow

1. Prefer T1 DEC website leafs first.
2. Use authenticated NCUC portal search for T2 compliance/revised tariff attachments.
3. Download first; avoid deep parsing decisions in the portal session.
4. Avoid duplicates by checking existing filenames and checksums where possible.
5. After the harvest pass:

```powershell
python -m duke_rates ncuc import-pipeline --all-downloaded
python -m duke_rates bootstrap-missing-versions-nc
python -m duke_rates extract-rates-nc
python -m duke_rates export nc-anomaly-audit
python -m duke_rates export nc-schedule-inventory-audit
```

---

## Interpretation

This list is intentionally biased toward:

- DEC families still known to be incomplete, mislinked, or under-mined
- billing-relevant schedules outside the focused matrix
- rider leaves that change annually or mid-cycle
- documents that are high-yield for the next portal session

It is not a full DEC family inventory. Use:
- [gap_analysis_dec_nc.md](/c:/Python/Duke/Standalone/docs/gap_analysis_dec_nc.md)
- [hq_document_discovery_catalog.md](/c:/Python/Duke/Standalone/docs/hq_document_discovery_catalog.md)
- [nc_schedule_inventory_audit.md](/c:/Python/Duke/Standalone/docs/reports/nc_schedule_inventory_audit/nc_schedule_inventory_audit.md)

for the broader context.
