# High-Quality Document Discovery Catalog
## Duke Energy NC — DEP + DEC Tariff Sheets

**Last updated:** 2026-03-28
**Purpose:** Prioritized catalog of T1/T2 official tariff sheets to locate and download.
Focused on highest-confidence documents only. Skip procedural orders, testimony, redlines.

---

## Document Quality Tiers

| Tier | Type | Where | How to Find |
|------|------|--------|-------------|
| T1 | Official current tariff sheet on utility website | Duke Energy website PDF media library | Known URL patterns below |
| T2 | NCUC compliance tariff exhibit (standalone leaf) | NCUC e-Portal → Docket → compliance filing attachment | Search docket, look for "Rider XXX Leaf No. NNN" attachment |
| T3 | Rate case stipulation tariff exhibit | NCUC e-Portal → rate case docket → "Revised Tariff" attachment | Medium confidence — verify it's a standalone leaf |
| Skip | Commission orders, testimony, procedural filings, redlines | — | Do NOT mine unless no T1/T2/T3 exists |

**Rule:** Find T1 first. If unavailable, search T2. Only fall to T3 if T2 also unavailable.

---

## Duke Energy Website URL Patterns

### DEP NC (Duke Energy Progress)
```
T1: https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/FILENAME.pdf
Filename convention: leaf-no-NNN-rider-XXX-ry1.pdf  (e.g., leaf-no-602-rider-jaa-ry1.pdf)
```

### DEC NC (Duke Energy Carolinas)
```
T1 CONFIRMED DOMAIN: https://p-cd.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/FILENAME.pdf
   Note: DEC NC riders use p-cd subdomain, NOT www.duke-energy.com
Filename convention: ncride{NAME}.pdf  (e.g., ncridersts.pdf, ncrideredpr.pdf)
```

### Confirmed T1 URLs (verified 2026-03-28)

| Family | Confirmed URL | Leaf/Rev | Effective | Docket |
|--------|--------------|----------|-----------|--------|
| DEP leaf-602 JAA | `https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-602-rider-jaa-ry1.pdf` | NC Third Revised Leaf 602 | Dec 1, 2025 | E-2 Sub 1354 |
| DEP leaf-663 SRR | `https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-663-rider-srr-ry1.pdf` | NC Original Leaf 663 | Jul 8, 2021 | E-2 Sub 1167 |
| DEC rider-STS | `https://p-cd.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/ncridersts.pdf` | NC Twelfth Revised Leaf 133 | Jan 1, 2026 | E-7 Sub 1243 |
| DEC rider-EDPR | `https://p-cd.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/ncrideredpr.pdf` | NC Twenty-Second Revised Leaf 64 | Jul 1, 2025 | E-7 Sub 1276 |
| DEP leaf-606 DSM | NOT FOUND — filename unknown; try `p-cd` subdomain with `deprideree.pdf` or similar | — | — | E-2 series |

---

## NCUC e-Portal Search Instructions (Automated — for AI/programmatic use)

> **Important:** starw1.ncuc.gov is protected by Cloudflare. Direct HTTP requests return 403.
> All portal access MUST use Playwright with an authenticated NCID session.
> Credentials are in `.env`: `DUKE_RATES_NCID_USERNAME` / `DUKE_RATES_NCID_PASSWORD`

### URL and Navigation Map (confirmed 2026-03-28)

| Purpose | URL Pattern |
|---------|-------------|
| Login page | `https://starw1.ncuc.gov/NCUC/NCID/NCIDLogin.aspx` |
| Document search form | `https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx` |
| Document detail page | `https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={GUID}&Class=Filing` |
| File download | `https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id={GUID}` |

### Automated Search Pattern (Playwright)

```python
# 1. Create authenticated session
pw, ctx, page = create_authenticated_context(settings)

# 2. Navigate to search form
page.goto("https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx")
page.wait_for_timeout(1500)

# 3. Fill in docket number field
# Field ID: ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber
# Example values: "E-2 Sub 1354", "E-7 Sub 1243"
docket_input = page.query_selector("#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber")
docket_input.fill("E-2 Sub 1354")
page.query_selector("input[value='Search']").click()
page.wait_for_load_state("domcontentloaded")
page.wait_for_timeout(2000)

# 4. Extract document IDs from results (GUID format, 36 chars)
content = page.content()
doc_ids = re.findall(r'DocumentId=([0-9a-f\-]{36})', content, re.I)
# Each doc row also has: title, date filed (MM/DD/YYYY), class (Filing/Order)

# 5. Paginate — next page links are __doPostBack with numeric text
# Find links with inner_text matching integers > current page

# 6. Navigate to document detail to get ViewFile URL
# IMPORTANT: Direct goto() of PSCDocumentDetailsPageNCUC.aspx?DocumentId=GUID returns
# "Object reference not set to an instance of an object." unless &Class= is included.
page.goto(f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={doc_id}&Class=Filing")
view_file_links = page.query_selector_all("a[href*='ViewFile.aspx']")
view_url = view_file_links[0].get_attribute("href")  # e.g., https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=GUID

# 7. Download — ViewFile.aspx triggers direct download
# page.goto() raises "Download is starting" — this is expected, not an error
with page.expect_download(timeout=60000) as download_info:
    try:
        page.goto(view_url, wait_until="commit", timeout=30000)
    except Exception as e:
        if "Download is starting" not in str(e):
            raise
download = download_info.value
download.save_as("/path/to/save.pdf")
```

### Document Row Text Structure

When parsing a row's inner_text, the format is:
```
TITLE_LINE
Filing          ← or "Order"
[whitespace/icon]
Filed In: DOCKET_NUMBER [, DOCKET2, ...]
Date Filed: MM/DD/YYYY
```

To extract the title: skip lines matching `"Filing"`, `"Order"`, `"Filed In:"`, `"Date Filed:"`.

### Filtering by Filing Type

High-value tariff title keywords (case-insensitive):
- `"compliance tariff"` — direct T2 evidence
- `"revised tariff"` — may include leaf sheets
- `"annual adjustment"` — JAA/STS/RDM annual filings
- `"tariff sheet"` — standalone leaf exhibits
- `"revised leaf"` — re-filed leaf exhibits
- `"NC Rider XXX"` / `"Leaf No. NNN"` — individual rider/leaf names

Exclude: `"petition"`, `"notice of appearance"`, `"testimony"`, `"brief"`, `"order"` unless preceded by a tariff keyword.

### Known Docket→Family Mappings

| Docket | Family | Description |
|--------|--------|-------------|
| E-2 Sub 1354 | nc-progress-leaf-602 | DEP JAA (current) |
| E-2 Sub 1143 | nc-progress-leaf-602 | DEP JAA (historical) |
| E-2 Sub 1204 | nc-progress-leaf-607 | DEP STS Storm Securitization |
| E-2 Sub 1294 | nc-progress-leaf-608 | DEP RDM Revenue Decoupling |
| E-2 Sub 1196 | nc-progress-leaf-604 | DEP EDIT-4 (search returns wrong docket — only complaint docs) |
| E-7 Sub 1243 | nc-carolinas-rider-sts | DEC STS (annual filings 2021–2022) |
| E-7 Sub 1321 | nc-carolinas-rider-sts | DEC STS Storm Debby (2024–2025, no tariff leaves yet) |
| E-7 Sub 1325 | nc-carolinas-rider-sts | DEC STS Storm Helene (2025–2026, no tariff leaves yet) |
| E-7 Sub 1276 | nc-carolinas-rider-edpr | DEC EDPR (current — 0 tariff results, try different search) |
| E-7 Sub 1146 | nc-carolinas-rider-edpr | DEC EDPR (historical — 0 tariff results) |

### Search Gaps (known issues as of 2026-03-28)

- **E-2 Sub 1196 (DEP EDIT-4):** Search for "E-2 Sub 1196" returns a complaint docket (Alan Shumard), NOT the EDIT-4 tariff docket. The EDIT-4 compliance filings appear under the E-2 Sub 1143/1144/1354 bundle documents ("DEP Compliance Tariffs for Fuel, REPS and JAAR riders" filed 2017-11-27 includes Sub 1146). Try searching `"Leaf No. 604"` or `"EDIT-4"` as text search.
- **E-7 Sub 1276/1146 (DEC EDPR):** Search returns 100+ procedural filings. EDPR compliance tariffs may be filed under a joint DEC/DEP tariff bundle — try keyword `"EDPR"` or `"Leaf No. 64"` in text search.

### Text Search (alternative when parameter search fails)

URL: `https://starw1.ncuc.gov/NCUC/page/DocumentsTextSearch/portal.aspx`

Input ID: `ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentFullTextSearchControl1_searchPhrase`

**Key difference from parameter search:** Text search shows `ViewFile.aspx?Id=GUID` links **directly** in results — no intermediate detail page needed. Results also include full-text match percentage.

Result HTML structure (parse directly from content):
```html
<span class="documentTitle">TITLE</span>
<!-- Then ViewFile links within same result block -->
<a href="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=GUID">...</a>
```

Use text search when:
- Parameter search for a docket returns wrong results (e.g., E-2 Sub 1196 shows complaint case, not EDIT-4)
- Searching for a specific rider name across all dockets (e.g., "Rider EDPR", "EDIT-4")
- Finding multi-docket joint compliance filings (e.g., "E-7 Subs 487 828 1026 1146 1214 1276")

### Scripts for Automated Scraping

- `scrape_ncuc_tariff_filings.py` — Main scraper: parameter search for target dockets, identifies tariff docs, collects ViewFile URLs → saves to `data/ncuc_tariff_filings.json`
- `extract_text_search_files.py` — Text search scraper: keyword queries, extracts ViewFile URLs directly → saves to `data/ncuc_edpr_edit4_filings.json`
- `download_ncuc_tariffs.py` — Downloads high-value files from ViewFile URLs → `data/downloads/ncuc_tariff/{family_dir}/`
- `download_edpr_edit4.py` — Downloads EDPR + EDIT-4 specific files
- `register_ncuc_downloads.py` — Registers downloaded PDFs in `ncuc_discovery_records` DB table

---

## Priority 1 — CRITICAL GAPS (downloads enable new charge extraction)

Dedicated queue docs:
- DEP: [download_targets_dep_nc.md](/c:/Python/Duke/Standalone/docs/download_targets_dep_nc.md)
- DEC: [download_targets_dec_nc.md](/c:/Python/Duke/Standalone/docs/download_targets_dec_nc.md)

### DEP leaf-602: JAA — Joint Agency Asset Rider

- **T1 CONFIRMED:** `https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-602-rider-jaa-ry1.pdf`
  - NC Third Revised Leaf 602, effective Dec 1, 2025
  - Current rates: Residential 0.00464 $/kWh; MGS 0.92 $/kW; LGS 3.03 $/kW
  - **DOWNLOAD THIS NOW** — text-layer PDF, will extract immediately
- **Why critical:** 64 docs on disk but ALL are image-based PDFs (0 page artifacts). This T1 has a text layer.
- **Historical versions docket:** E-2, Sub 1143 (original), **E-2, Sub 1354** (current — confirmed from Dec 2025 leaf)
- **NCUC search terms:** `"Rider JAA"` | `"Joint Agency Asset"` | `"Leaf No. 602"` | dockets E-2 Sub 1143, Sub 1354
- **Target filings:** Annual compliance tariff, effective ~December each year, 2015–2025
- **Profile ready:** `ProgressSingleValueRiderProfile` (supports `nc-progress-leaf-602`)
- **After download:** Run `mine-tariff-sheets-nc --family nc-progress-leaf-602` then `extract-rates-nc`

### DEC rider-STS: Storm Securitization Rider

- **T1 CONFIRMED:** `https://p-cd.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/ncridersts.pdf`
  - NC Twelfth Revised Leaf 133, effective Jan 1, 2026
  - Rates: Residential 0.0467 ¢/kWh; General Service 0.0141 ¢/kWh; Industrial 0.0085 ¢/kWh; Lighting 0.1469 ¢/kWh
  - Updated semi-annually (Jan 1 and Jul 1)
  - **DOWNLOAD THIS NOW** — text-layer PDF, `progress_single_value_rider` profile should work
- **Docket:** E-7, Sub 1243 (confirmed)
- **Files on disk:** `d9c03aa1` (2025-01-01) — check if text layer present; if so, mine first
- **After download:** Run `mine-tariff-sheets-nc --family nc-carolinas-rider-STS` then `extract-rates-nc`
- **Note:** Additional storm securitization dockets E-7 Sub 1321 (storm Debby) and Sub 1325 (Helene) may have separate riders

---

## Priority 2 — HIGH VALUE (files may exist; verify, download, mine)

### DEP leaf-663: SRR — Solar Rebate Rider

- **T1 CONFIRMED:** `https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-663-rider-srr-ry1.pdf`
  - NC Original Leaf 663, effective Jul 8, 2021 (static — not annually adjusted)
  - Rates: $0.30/W nonresidential, $0.40/W residential, $0.75/W nonprofit (incentive rebates, $/W format)
  - **NOTE:** Rates are $/watt rebates (for solar installations), NOT $/kWh adjustments — CONTENT-TYPE for our extractor
  - Program capacity capped (reached post-2022); leaf remains for existing contract customers only
- **Docket:** E-2, Sub 1167 (original authorization), Sub 1300 (tariff book update)
- **Assessment:** Download for documentation but rates are $/W not $/kWh — no extraction needed

### DEC rider-EDPR: Existing DSM Program Costs Adjustment Rider

- **T1 CONFIRMED:** `https://p-cd.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/ncrideredpr.pdf`
  - NC Twenty-Second Revised Leaf 64, effective Jul 1, 2025
  - Current rate: **-0.0008 ¢/kWh** (credit adjustment, near-zero)
  - **DOWNLOAD THIS NOW** — confirmed text-layer PDF
- **Name clarification:** EDPR = "Existing DSM Program Costs Adjustment Rider" (not "Economic Development Prospective")
- **Docket:** E-7, Sub 1276 (current — confirmed from leaf); Sub 1146 was older reference
- **Annual filing:** Due by April 1 each year, effective July 1
- **Files on disk in Sub 1146:** `8182204e` (2021-06-01), `1bf9bf84` (2024-01-15), `f807656f` (2024-07-01) — these may be standalone leaves; worth checking page counts
- **After download:** Run `mine-tariff-sheets-nc --family nc-carolinas-rider-EDPR` then `extract-rates-nc`
- **NCUC search terms for historical:** `E-7 Sub 1146` | `E-7 Sub 1276` | `"Rider EDPR"` | `"Existing DSM"` | annual compliance

### DEC rider-CEI: Clean Energy Impact Rider

- **Why:** 45-page joint DEC/DEP compliance filing on disk but 0 charges — rate buried in pages 5–15
- **File on disk:** `data/raw/historical/ncuc/e-7/e-7-nodate-dec-dep-compliance-tariffs-rider-clean-energy-impact.pdf`
- **Action:** Run `mine-tariff-sheets-nc --family nc-carolinas-rider-CEI` to see page content; may need targeted page extraction
- **Docket:** E-7 (DEC main docket) | E-2 (DEP main docket) — joint filing
- **NCUC search terms:** `"Rider CEI"` | `"Clean Energy Impact"` | `"compliance tariff"`
- **T1 target:** Look for standalone `nc-rider-cei-pdf.pdf` on DEC website
- **Note:** 45-page joint filing likely contains both DEP and DEC rider tables — pages 5–15 for DEC

### DEP leaf-606: DSM — Demand Side Management

- **Why:** No documents found at all — family exists with 0 charges, 0 docs
- **T1 target:** `https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-606-rider-dsm.pdf`
- **NCUC search terms:** `"Rider DSM"` | `"Demand Side Management"` | `"Leaf No. 606"` | E-2 docket
- **Expected structure:** Multi-class $/kWh adjustment (similar to leaf-607 STS format)

---

## Priority 3 — HISTORICAL VERSIONS (fill timeline gaps for existing families)

### DEP leaf-601: BA — Billing Adjustment (pre-2017)

- **Why:** Current versions extracted. Need 2015–2016 rates for bill reconstruction timeline.
- **Docket:** E-2, Sub 1142 (annual compliance)
- **NCUC search terms:** `E-2 Sub 1142` | `"Rider BA"` | `"Billing Adjustment"` | `"Leaf No. 601"` | 2015–2016
- **Expected:** Standalone 1–2 page leaf with rate table matching DEP BA format
- **Profile ready:** `ProgressBillingAdjustmentsProfile`

### DEP leaf-604: EDIT-4 — Excess Deferred Income Tax (2016–2020)

- **Why:** Only 2026 version on disk. Multi-year historical rates needed.
- **Docket:** E-2, Sub 1196 (annual compliance)
- **NCUC search terms:** `E-2 Sub 1196` | `"EDIT-4"` | `"Excess Deferred"` | `"Leaf No. 604"` | 2016 through 2020
- **Expected:** Single-value $/kWh per class; each annual filing = 1 version
- **Profile ready:** `ProgressSingleValueRiderProfile`

### DEP leaf-607: STS — Storm Securitization (2015–2022)

- **Why:** 3 current versions extracted. Need 2015–2022 historical annual adjustments.
- **Docket:** E-2, Sub 1204
- **NCUC search terms:** `E-2 Sub 1204` | `"Rider STS"` | `"Storm Securitization"` | `"Leaf No. 607"` | annual compliance
- **Expected:** Multi-class $/kWh adjustment, new rate each year
- **Profile ready:** `ProgressStormSecuritizationProfile` / `progress_single_value_rider`

### DEP leaf-608: RDM — Revenue Decoupling (2015–2022)

- **Why:** 3 current versions extracted. Need 2015–2022 historical.
- **Docket:** E-2, Sub 1294
- **NCUC search terms:** `E-2 Sub 1294` | `"Rider RDM"` | `"Revenue Decoupling"` | `"Leaf No. 608"` | annual
- **Profile ready:** `ProgressSingleValueRiderProfile`

### DEP leaf-613: STS2 — Storm Securitization 2 (2022–2024)

- **Why:** Partial coverage; need full annual adjustment series.
- **Docket:** E-2, Sub 1204 (same sub as STS)
- **NCUC search terms:** `E-2 Sub 1204` | `"STS2"` | `"Storm Securitization 2"` | `"Leaf No. 613"` | 2022–2024

---

## Priority 4 — DEC FIX-THEN-DOWNLOAD (mislinked or needs re-investigation)

### DEC rider-CAR: Customer Assistance Recovery

- **Bug:** Currently linked to `nccarbonoffset-*.pdf` (a carbon offset document — wrong file!)
- **Fix needed:** SQL UPDATE to re-link to correct Rider CAR document
- **Where to find:** Look on DEC website for `nc-rider-car-pdf.pdf` or similar
- **NCUC search terms:** `"Rider CAR"` | `"Customer Assistance Recovery"` | E-7 docket
- **After fix:** Determine if Rider CAR has extractable $/kWh rates

### DEC rider-GS: Green Source (or General Service OL Regs?)

- **Bug:** Currently linked to `nc-ol-service-regs-*.pdf` (outdoor lighting service regulations — wrong file!)
- **Clarify:** Is `nc-carolinas-rider-GS` meant to be "Green Source" or "General Service"?
- **Fix needed:** SQL UPDATE to correct the family_key for the OL service regs doc
- **Where to find:** If Green Source rider exists, search DEC website for `nc-rider-gs-pdf.pdf`
- **NCUC search terms:** `"Rider GS"` | E-7 docket | effective 2025-02-18

### DEC rider-ED: Economic Development (Conventional)

- **Bug:** Currently linked to `nc-ev-managed-charging-orig-09012023-*.pdf` (EV managed charging — wrong file!)
- **Fix needed:** SQL UPDATE to re-link EV managed charging to appropriate family (`nc-carolinas-rider-EV` or similar)
- **Then find:** Actual Rider ED document — look on DEC website for `nc-rider-ed-pdf.pdf`
- **NCUC search terms:** `"Rider ED"` | `"Economic Development"` | E-7 docket | 2023

---

## Priority 5 — MINING EXISTING DISK FILES (no download needed)

These files are on disk but have 0 charges. Either need mining or profile assignment.

### DEC schedule-PP: Purchase Power

- **File on disk:** `data/raw/nc/carolinas/rate/pp-media-pdfs-for-your-home-rates-electric-nc-ncschedulepp-p-*.pdf` (2021-10-11)
- **Action:** `python -m duke_rates mine-tariff-sheets-nc --family nc-carolinas-schedule-pp`
- **Check:** Does file have text layer? `SELECT COUNT(*) FROM ncuc_page_artifacts WHERE source_pdf LIKE '%ncschedulepp%'`
- **Expected:** Multi-class rate schedule (non-standard purchase power format — may need custom profile)

### DEC schedule-RE: Residential Experimental

- **File on disk:** `data/historical/ncuc/e-7-sub-1214/e31d1ef2-78de-4dab-8f84-5b6b6cb94c30.pdf` (2021-12-16, 146 pages)
- **Also in:** `e-7-nodate-duke-s-rate-schedule.pdf` (484p), large rate books
- **Action:** Check page artifacts — if mining ran, use `read_dec_riders.py` style script to read pages
- **T1 target:** Look for standalone `ncscheduleRE*.pdf` on DEC website
- **NCUC search terms:** `"Schedule RE"` | `"Residential Experimental"` | `E-7 Sub 1214` | 2021–2022

### DEC rider-STS (existing files need investigation)

- **File 1:** `aa8985bf-4233-4d5c-9d61-7b9af8f10d1d.pdf` (in e-7-sub-1243, no effective date — need to check)
- **File 2:** `d9c03aa1-3e27-4c1f-a967-8a09b6eb1316.pdf` (2025-01-01) — check if page artifacts exist
- **Action:** `SELECT COUNT(*) FROM ncuc_page_artifacts WHERE source_pdf LIKE '%d9c03aa1%'`
- **If no pages:** Run `mine-tariff-sheets-nc --family nc-carolinas-rider-STS`

---

## Priority 6 — DEC HISTORICAL RATE BOOKS (large multi-schedule filings)

These 484-page and 272-page NCUC rate books contain multiple schedules but are hard to mine precisely.
Better strategy: download individual schedule leaves from DEC website.

### DEC current rate schedules — T1 downloads preferred

| Schedule | T1 Filename (likely) | Description |
|---------|---------------------|-------------|
| Schedule BC | `ncscheduleBC*.pdf` | Business Customer schedule |
| Schedule RE | `ncscheduleRE*.pdf` | Residential Experimental |
| Schedule PP | `ncschedulePP*.pdf` | Purchase Power (already on disk) |

**DEC website pattern:** `https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric/nc/FILENAME.pdf`

---

## Do NOT Mine — Confirmed Content-Type (no extractable $/kWh)

| Family | Content | Why No Rate |
|--------|---------|-------------|
| nc-progress-leaf-570 ALS | Streetlight flat fee by fixture | $/lamp/month not $/kWh |
| nc-progress-leaf-572 SLR | Roadway lighting flat fee | $/lamp/month |
| nc-progress-leaf-574 TSS | Traffic signal flat fee | $/signal/month |
| nc-progress-leaf-591 T&C | Purchase power terms | Terms doc |
| nc-progress-leaf-592 PPBE | Contract formula purchase power | No fixed rate |
| nc-carolinas-rider-SSR | Solar Savings program description | No rate |
| nc-carolinas-rider-ESM | Energy Storage program terms | No rate |
| nc-carolinas-schedule-PPBE | Blend & Extend credits | Contract formula |
| nc-progress-leaf-534 LGS-RTP | Real-time pricing external index | No fixed rate |
| (all leaf-640–667 CONTENT-TYPE) | See gap_analysis_dep_nc.md | Service terms |

---

## Download Workflow

### Step 1: Duke Energy Website (T1)
1. Construct URL from pattern above with likely filename
2. Try `curl -L -o <local_path> <URL>` or browser download
3. Store in `data/raw/nc/carolinas/rider/` or `data/raw/nc/progress/rider/`
4. Name file to match family key: `nc-rider-sts-pdf.pdf`, `nc-rider-edpr-pdf.pdf`, etc.

### Step 2: NCUC e-Portal (T2)
1. Go to `https://www.docket.ncuc.org/`
2. Search by docket and sub number (see entries above)
3. Look for "Compliance Tariff" or "Annual Adjustment" filings
4. Inside the filing, find PDF attachment labeled "Rider XXX Leaf No. NNN" (standalone leaf)
5. Download to `data/raw/historical/ncuc/e-7-sub-XXXX/` or `data/raw/historical/ncuc/e-2-sub-XXXX/`
6. Use descriptive filename: `rider-sts-dec-leaf-no-XXX-effective-2025-01-01.pdf`

### Step 3: Register and Mine
After download:
```bash
# Register new doc in DB (if not auto-discovered)
python -m duke_rates register-document --path <path> --family nc-carolinas-rider-STS --effective 2025-01-01

# Mine pages
python -m duke_rates mine-tariff-sheets-nc --family nc-carolinas-rider-STS

# Extract charges
python -m duke_rates extract-rates-nc
```

---

## Extraction Priority Summary

| Priority | Family | Action | Expected Charges |
|---------|--------|--------|-----------------|
| ✅ DOWNLOADED | DEP leaf-602 JAA | T2 from E-2 Sub 1354 (2025) + Sub 1143 (2017) | 8 charges × 2 years = 16+ |
| ✅ DOWNLOADED | DEP leaf-607 STS | T2 bundle from E-2 Sub 1204 (2019) | ~5 charges × 1 bundle |
| ✅ DOWNLOADED | DEP leaf-608 RDM | T2 bundle from E-2 Sub 1294 (2023) | ~4 charges |
| ✅ DOWNLOADED | DEC rider-STS | T2 compliance bundles E-7 Sub 1243 (2021–2022, 6 files) | 5–8 charges per year × 6 |
| ✅ DOWNLOADED | DEC rider-EDPR | Text search "Rider EDPR": 2025, 2024, 2011 compliance tariffs | ~1–2 per year |
| ✅ DOWNLOADED | DEP leaf-604 EDIT-4 | Text search "Leaf No. 604": DEP compliance filing Sep 2023 (×2) | 4 per year |
| 🟠 HIGH | DEP leaf-663 SRR | Download T1 from DEP website | 1–8 charges ($/W format) |
| 🟠 HIGH | DEC rider-CEI | Mine existing 45-page file or download T1 | 4–8 charges |
| 🟠 HIGH | DEC rider-CAR/GS/ED | Fix mislinks + download correct docs | Unknown |
| 🟡 MEDIUM | DEP leaf-606 DSM | Download T1 from DEP website | 4–8 charges |
| 🟡 MEDIUM | DEC schedule-RE | Download T1 standalone leaf or mine Sub 1214 | 10–20 charges |
| 🟡 MEDIUM | DEC schedule-BC | Download T1 standalone leaf or mine Sub 1214 | 5–10 charges |
| 🟢 FILL-IN | DEP leaf-601/604/607/608/613 | Download T2 historical versions from E-2 Subs | 5 charges × multiple years |

### Files Downloaded (2026-03-28)

14 compliance tariff PDFs downloaded to `data/downloads/ncuc_tariff/`:
- `progress_leaf_602/` — 5 files: JAA Rev.3 (Dec 2025), Summary of Riders Rev.7 (Dec 2025), JAA bundle 2017
- `progress_leaf_607/` — 1 file: DEP compliance tariffs bundle 2019 (Sub 1173/1204/1205/1207)
- `progress_leaf_608/` — 1 file: DEP DSM/EE compliance tariffs (Jan 2023)
- `carolinas_rider_sts/` — 7 files: DEC STS compliance bundles Nov 2021, Dec 2021, Apr 2022, Jun 2022, Sep 2022, Dec 2022 + joint petition

All 14 registered in `ncuc_discovery_records` (fetch_status='downloaded').

---

## Families Already Identified as Gaps — No Action Needed

These are malformed keys from procedural NCUC docs (body text used as key). Do not attempt downloads:

- `nc-progress-rider-APPLICATIONOF...` (50+ entries): Commission proceeding docs, not tariff sheets
- `nc-progress-program-...` (10+ entries): DSM program descriptions, not rate tables
- `nc-carolinas-rider-ADUKEENERGY...`, `nc-carolinas-rider-BPMPPT...`: Malformed keys from legacy filings

These need SQL `UPDATE historical_documents SET family_key = NULL` or re-classification, not downloads.
