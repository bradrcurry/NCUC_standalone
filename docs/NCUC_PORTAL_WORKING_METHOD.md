# NCUC Portal Authentication - The Working Method

**Last Updated:** 2026-05-04
**Status:** ✅ PROVEN WORKING (live-validated 2026-05-04: smoke test passed in 33s, 65 documents returned)
**Critical Knowledge:** This is THE single source of truth for NCUC portal work.
Older docs (`NCUC_PORTAL_AUTH_SOLUTION.md`, `CORRECT_NCUC_DOCKET_FETCH_PROCEDURE.md`,
`SESSION_35_NCUC_DOWNLOAD_STRATEGY.md`) are session-specific historical notes and
must not be followed for new work.

---

## 60-Second Preflight (Run This First)

Before any new portal work, run these two commands. They take ~35 seconds total
and prove the entire portal flow is operational.

```powershell
# 1. Static prerequisite check (no network calls). ~1s.
python -m duke_rates run-continuous-loop-nc --dry-run --max-cycles 0 2>&1 | head -15

# 2. Live login + resolve + DocketDetails + inventory probe. ~30s.
python -m duke_rates ncuc portal-smoke-test
```

Expected output of (1): `NCID auth: YES`, `Real browser: YES (path)`, `Portal fetch: YES`.
Expected output of (2): `Smoke test SUCCESS — authenticated portal workflow is healthy.`

If either fails, see the **Troubleshooting** section. Do NOT proceed to fetch
work until both pass.

The autonomous loop (`run-continuous-loop-nc --execute`) runs probe (2)
automatically at startup unless you pass `--skip-portal-precheck`. A failed
smoke test there auto-disables portal fetch for that run, so the loop won't
burn timeouts on a broken portal.

---

## Executive Summary

**The NCUC portal works with this exact approach:**

1. Use installed Chrome or Edge (NOT bundled Playwright Chromium)
2. Use `create_authenticated_context()` from `src/duke_rates/historical/ncuc/session.py`
3. Navigate to portal login page
4. Submit ASP.NET form with exact field selectors
5. Search for dockets using specific CSS selectors
6. Extract document links via regex from rendered HTML
7. Download files using `page.expect_download()`

**Why this works:**
- Real Chrome passes Cloudflare's bot detection
- Bundled Chromium is flagged as bot and blocked (HTTP 403)
- ASP.NET authentication is preserved in session cookies
- PDF downloads work via Chrome's download interception

## Canonical CLI Workflow

Use these commands instead of reconstructing the process from scratch:

```powershell
python -m duke_rates ncuc portal-smoke-test
python -m duke_rates ncuc portal-search --docket-number "E-2, Sub 1354"
python -m duke_rates ncuc portal-search --company "Duke Energy Progress" --types TARIFF,RATESCED --after 11/01/2025 --before 12/31/2025 --max 20 --top 10
```

What each command means:
- `ncuc portal-smoke-test`: canonical smoke test. Verifies login, resolve, authenticated DocketDetails access, and docket inventory.
- `ncuc portal-search --docket-number ...`: canonical exact-docket search. Resolves the docket and inventories documents through the authenticated portal.
- `ncuc portal-search` without `--docket-number`: canonical structured authenticated search for company/date/type filtering.

Lower-level commands still available when exact control is needed:
- `ncuc login-test`
- `ncuc resolve-docket-ids`
- `ncuc docket-fetch`
- `search doc-param`

Do not confuse the two docket formats:
- `ncuc resolve-docket-ids`: use `E-2, Sub 1354`
- `search doc-param --docket`: use `E-2 Sub 1354`

Do not confuse the search surfaces:
- authenticated portal is canonical
- public Zoom search is fallback only
- a zero-result `search doc-param --docket ...` query does not prove the docket is empty

## Brief Validation On 2026-04-21

The following live checks were run successfully in this workspace on 2026-04-21:
- `python -m duke_rates ncuc login-test`
  Result: authenticated access succeeded with Chrome; DocketDetails returned HTTP 200.
- `python -m duke_rates ncuc resolve-docket-ids --docket-number "E-2, Sub 1354"`
  Result: returned the expected exact GUID `9b3614b6-11d6-4703-8d18-5e2e2ef3d705`.
- `python -m duke_rates ncuc docket-fetch 9b3614b6-11d6-4703-8d18-5e2e2ef3d705 --docket-number "E-2, Sub 1354" --dry-run`
  Result: listed 64 docket documents.
- `python -m duke_rates search doc-param --company "Duke Energy Progress" --types TARIFF,RATESCED --after 11/01/2025 --before 12/31/2025 --max 20 --top 10`
  Result: returned 6 authenticated portal results.

Observed limitation:
- docket-scoped `search doc-param` can return zero rows even when the docket is real and `ncuc docket-fetch` lists documents
- therefore exact docket work should use `ncuc resolve-docket-ids` then `ncuc docket-fetch`, not only `search doc-param`

New wrapper behavior:
- `ncuc portal-search --docket-number ...` avoids the brittle structured-docket search path and always uses authenticated exact-docket resolve + inventory.
- `ncuc portal-smoke-test` bundles the previously separate login + resolve + DocketDetails + inventory checks into one command.

---

## Prerequisites

### 1. Installed Chrome or Edge

The NCUC portal **blocks bundled Playwright Chromium**. You must have a real browser installed:

```
C:\Program Files\Google\Chrome\Application\chrome.exe
  OR
C:\Program Files (x86)\Google\Chrome\Application\chrome.exe
  OR
C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
  OR
C:\Program Files\Microsoft\Edge\Application\msedge.exe
```

**Check if Chrome is installed:**
```bash
where chrome.exe
```

**If not installed:**
- Download Chrome: https://www.google.com/chrome/
- Install normally (default location works)

### 2. NCUC Credentials in `.env`

File: `.env` (in repository root)

```
DUKE_RATES_NCID_USERNAME=<your_ncuc_username>
DUKE_RATES_NCID_PASSWORD=<your_ncuc_password>
```

Example:
```
DUKE_RATES_NCID_USERNAME=bradrcurry
DUKE_RATES_NCID_PASSWORD=<your_ncuc_password>
```

### 3. Playwright Installed

```bash
pip install playwright
playwright install chromium  # (optional, not used for NCUC)
```

---

## The Working Code Pattern

### Step 1: Create Authenticated Context

```python
from duke_rates.historical.ncuc.session import (
    create_authenticated_context,
    close_authenticated_context,
)
from duke_rates.config import Settings

settings = Settings()  # Reads credentials from .env

pw, ctx, page = create_authenticated_context(settings)
# At this point: page is logged in and ready to use
```

### Step 2: Search for Dockets

```python
# URL of docket search page
DOCKET_SEARCH_URL = "https://starw1.ncuc.gov/NCUC/page/Dockets/portal.aspx"

page.goto(DOCKET_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)

# ASP.NET field names (these are EXACT, don't guess)
DOCKET_FIELD = 'input[name="ctl00$ContentPlaceHolder1$PortalPageControl1$ctl86$DocketSearchControlNCUC1$docketNumberTextBox"]'
DOCKET_BUTTON = 'input[name="ctl00$ContentPlaceHolder1$PortalPageControl1$ctl86$DocketSearchControlNCUC1$searchButton"]'

# Fill in docket number (must use "E-2 Sub 1354" format, not "E-2, Sub 1354")
page.fill(DOCKET_FIELD, "E-2 Sub 1354")

# Submit search
page.click(DOCKET_BUTTON)
page.wait_for_load_state("domcontentloaded", timeout=30000)
```

### Step 3: Extract Document Links

```python
import re

# Get HTML from rendered page
html = page.content()

# Use regex to find document links
link_pattern = re.compile(
    r'href=["\']([^"\']*PSCDocumentDetailsPageNCUC\.aspx\?DocumentId=([0-9a-f\-]{36})&amp;Class=(\w+))["\']',
    re.I,
)

documents = []
for match in link_pattern.finditer(html):
    href = match.group(1).replace("&amp;", "&")
    doc_id = match.group(2)
    doc_class = match.group(3)

    documents.append({
        "doc_id": doc_id,
        "href": f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={doc_id}&Class={doc_class}",
    })
```

### Step 4: Handle Pagination

```python
# After extracting documents from first page, look for next page link
next_page_links = page.query_selector_all("a[href*='__doPostBack']")

for link in next_page_links:
    page_num_text = link.inner_text().strip()
    try:
        page_num = int(page_num_text)
        # Click next page link
        link.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        # Re-extract documents from new page
        # ...
    except ValueError:
        continue
```

### Step 5: Download Documents

```python
from pathlib import Path

document_href = "https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={id}&Class={class}"
dest_path = Path("data/downloads/document.pdf")

dest_path.parent.mkdir(parents=True, exist_ok=True)

# Chrome's PDF-as-download setting (set in create_authenticated_context)
# makes this work: when you navigate to a PDF, it downloads instead of rendering
with page.expect_download(timeout=30000) as download_info:
    try:
        page.goto(document_href, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass  # "Download is starting" error is expected

download = download_info.value
download.save_as(str(dest_path))
print(f"Downloaded: {dest_path}")
```

### Step 6: Clean Up

```python
close_authenticated_context(pw, ctx)
# Temp user data directory is automatically cleaned up
```

---

## Complete Working Example

See: `scripts/discovery/search_dep_gaps.py` (lines 98-250)

This script was proven to work on 2026-03-29:
- Searched 11 dockets
- Found 11 high-quality documents
- All searches succeeded (100% success rate)
- No HTTP 403 errors

**Key excerpts:**

```python
def search_docket(page, docket_str):
    """Search for documents in a docket."""
    search_url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"

    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    # Fill docket number using exact selector
    docket_input = page.query_selector(
        "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber"
    )
    docket_input.fill(docket_str)

    # Submit
    submit = page.query_selector("input[value='Search']")
    submit.click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)

    # Extract results...
    # ...
```

---

## Common Pitfalls and Solutions

### Pitfall 1: Using Bundled Chromium

**Problem:**
```python
browser = pw.chromium.launch()  # WRONG - uses bundled Chromium
page = context.new_page()
page.goto("https://starw1.ncuc.gov/...")  # Returns HTTP 403
```

**Solution:**
```python
pw, ctx, page = create_authenticated_context(settings)  # CORRECT
# Automatically uses installed Chrome, not bundled Chromium
```

### Pitfall 2: Guessing ASP.NET Field Names

**Problem:**
```python
page.fill('input[name="username"]', user)  # WRONG field name
page.click('button:has-text("Login")')     # WRONG button selector
```

**Solution:**
Use exact selectors from `session.py`:
```python
page.fill('input[name="ctl00$ContentPlaceHolder1$PortalPageControl1$ctl81$userNameTextBox"]', user)
page.click('input[name="ctl00$ContentPlaceHolder1$PortalPageControl1$ctl81$loginButton"]')
```

### Pitfall 3: Calling ViewFile.aspx Directly

**Problem:**
```python
# WRONG - navigating directly to download PDF
page.goto("https://starw1.ncuc.gov/NCUC/ViewFile.aspx?FileId=...")
# Returns 403 because session not in right context
```

**Solution:**
```python
# CORRECT - navigate to detail page first
detail_url = "https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={id}&Class={class}"
with page.expect_download() as download_info:
    page.goto(detail_url, wait_until="domcontentloaded")

download = download_info.value
download.save_as(str(dest_path))
```

### Pitfall 4: Wrong Docket Format

**Problem:**
```python
page.fill(docket_field, "E-2, Sub 1354")  # WRONG - has comma and space
```

**Solution:**
```python
page.fill(docket_field, "E-2 Sub 1354")  # CORRECT - no comma, one space
```

### Pitfall 5: Not Waiting for Page Load

**Problem:**
```python
page.fill(docket_field, "E-2 Sub 1354")
page.click(search_button)
html = page.content()  # TOO FAST - gets old content
```

**Solution:**
```python
page.fill(docket_field, "E-2 Sub 1354")
page.click(search_button)
page.wait_for_load_state("domcontentloaded", timeout=30000)
time.sleep(2)  # Give JavaScript time to render
html = page.content()  # NOW we get fresh content
```

---

## Troubleshooting

### "HTTP 403 Forbidden"

**Cause:** Using bundled Chromium instead of installed Chrome

**Fix:**
1. Verify Chrome is installed: `where chrome.exe`
2. Use `create_authenticated_context()` which auto-detects Chrome
3. Don't call `pw.chromium.launch()` directly

### "Just a moment... checking your browser"

**Cause:** Cloudflare challenge not passed

**Fix:**
- Close all other Chrome windows
- Verify `--disable-blink-features=AutomationControlled` flag is set (it is in session.py)
- Use headless=True (it's set in session.py)

### "Page not responding / timeout"

**Cause:** Page taking too long to load or render

**Fix:**
```python
# Use longer timeout
page.wait_for_load_state("domcontentloaded", timeout=60000)  # 60 sec instead of 30

# Add wait for JavaScript
page.wait_for_timeout(3000)  # Let JS render content
```

### "No documents found"

**Cause:**
1. Wrong docket format
2. Docket doesn't exist in NCUC
3. Search form not filled correctly

**Fix:**
```python
# Verify field was filled
filled_value = page.input_value(DOCKET_FIELD)
print(f"Field contains: {filled_value}")

# Try searching for known docket
page.fill(DOCKET_FIELD, "E-2 Sub 1354")  # Known to have results
```

---

## URLs and Endpoints

| Purpose | URL |
|---------|-----|
| Docket Search | `https://starw1.ncuc.gov/NCUC/page/Dockets/portal.aspx` |
| Document Search | `https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx` |
| Document Details | `https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={id}&Class={class}` |
| Docket Details | `https://starw1.ncuc.gov/NCUC/PSC/DocketDetails.aspx?DocketId={id}` |
| View/Download File | `https://starw1.ncuc.gov/NCUC/ViewFile.aspx?FileId={id}` |

**All require authentication. Don't try calling them directly without logging in first.**

---

## Session Management Notes

### How Session Persistence Works

1. `create_authenticated_context()` creates a temp user data directory
2. Sets Chrome preference: `plugins.always_open_pdf_externally = true`
3. Logs in via the official portal login page
4. Session cookie (`GOVNCUC_SessionId`) is stored in the temp profile
5. All subsequent `page.goto()` calls use the authenticated session
6. On `close_authenticated_context()`, temp directory is deleted

### How PDF Download Works

Chrome's `plugins.always_open_pdf_externally` setting means:
- When you navigate to a PDF URL, Chrome treats it as a download
- Instead of rendering the PDF in the browser, it downloads to disk
- `page.expect_download()` intercepts this and lets us save it to a custom path

Without this setting, navigating to a PDF would render it in the browser, and we couldn't capture the raw bytes.

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `src/duke_rates/historical/ncuc/session.py` | Core authentication implementation |
| `scripts/discovery/search_dep_gaps.py` | Proven working example |
| `docs/NCUC_PORTAL_WORKING_METHOD.md` | This guide |
| `.env` | Credentials (NCID_USERNAME, NCID_PASSWORD) |

---

## Summary

**Use this pattern and it will work:**

```python
from duke_rates.historical.ncuc.session import (
    create_authenticated_context,
    close_authenticated_context,
)
from duke_rates.config import Settings

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)  # Logs in automatically

try:
    # Use page for searches and downloads
    # It's already authenticated with NCUC credentials
    # ...
finally:
    close_authenticated_context(pw, ctx)  # Cleans up
```

**That's it. This works. Use it.**

---

*Document updated: 2026-03-31*
*Source: Successful 2026-03-29 DEP gap search session (11/11 searches succeeded)*
*Compiled from: session.py implementation + search_dep_gaps.py working example*
