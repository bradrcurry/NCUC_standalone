"""
Authenticated NCUC portal session via portal login form.

Flow (confirmed via live probing 2026-03-15):
- starw1.ncuc.gov uses Cloudflare bot protection on all /NCUC/page/* and /NCUC/PSC/* paths
- portal.aspx is CF-whitelisted; the login page (NCIDLogin/portal.aspx) is NOT
- Bundled Playwright Chromium fails CF's JS challenge; installed Chrome/Edge passes it
- The NCIDLogin page is an ASP.NET WebForms form (not an OAuth redirect to NCID)
- After login, the session cookie grants access to DocketDetails and ViewFile endpoints

Usage:
    pw, ctx, page = create_authenticated_context(settings)
    try:
        # use page for authenticated scraping
    finally:
        close_authenticated_context(pw, ctx)
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

from duke_rates.config import Settings

logger = logging.getLogger(__name__)

PORTAL_URL = "https://starw1.ncuc.gov/NCUC/portal.aspx"
LOGIN_URL = "https://starw1.ncuc.gov/NCUC/page/NCIDLogin/portal.aspx"
DOCKET_SEARCH_URL = "https://starw1.ncuc.gov/NCUC/page/Dockets/portal.aspx"
DOCKET_DOCS_URL = "https://starw1.ncuc.gov/NCUC/page/docket-docs/PSC/DocketDetails.aspx"

# Real Chrome/Edge paths — required to pass CF's bot detection
# Bundled Playwright Chromium fails the challenge; installed browsers pass it
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\bradr\AppData\Local\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# ASP.NET field names on the NCIDLogin portal page
_USERNAME_FIELD = 'input[name="ctl00$ContentPlaceHolder1$PortalPageControl1$ctl81$userNameTextBox"]'
_PASSWORD_FIELD = 'input[name="ctl00$ContentPlaceHolder1$PortalPageControl1$ctl81$passwordTextBox"]'
_LOGIN_BTN = 'input[name="ctl00$ContentPlaceHolder1$PortalPageControl1$ctl81$loginButton"]'
_DOCKET_SEARCH_FIELD = (
    'input[name="ctl00$ContentPlaceHolder1$PortalPageControl1$ctl86$'
    'DocketSearchControlNCUC1$docketNumberTextBox"]'
)
_DOCKET_SEARCH_BTN = (
    'input[name="ctl00$ContentPlaceHolder1$PortalPageControl1$ctl86$'
    'DocketSearchControlNCUC1$searchButton"]'
)


class NcucSessionError(Exception):
    pass


def _find_chrome() -> str | None:
    """Return path to an installed Chrome or Edge executable, or None."""
    for path in CHROME_PATHS:
        if Path(path).exists():
            return path
    return None


def create_authenticated_context(settings: Settings):
    """
    Launch a Playwright browser context logged into the NCUC portal.

    Uses an installed Chrome/Edge executable (required to pass CF bot detection).
    Uses launch_persistent_context with a temp user data directory so we can set
    the 'plugins.always_open_pdf_externally' preference — this makes Chrome treat
    ViewFile.aspx PDF responses as downloads rather than rendering them inline,
    allowing page.expect_download() to capture the raw bytes.

    Returns (pw, ctx, page) — caller must call ctx.close() / pw.stop().
    The temp user data directory is cleaned up automatically when ctx.close() is called
    (we stash its path in ctx._ncuc_user_data_dir for the cleanup helper).

    Raises NcucSessionError if credentials missing, no browser found, or login fails.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise NcucSessionError("Playwright not installed. Run: pip install 'duke-rates[browser]'")

    if not settings.ncid_username or not settings.ncid_password:
        raise NcucSessionError(
            "NCID credentials not configured. "
            "Set DUKE_RATES_NCID_USERNAME and DUKE_RATES_NCID_PASSWORD in .env"
        )

    chrome_path = _find_chrome()
    if not chrome_path:
        raise NcucSessionError(
            "No installed Chrome or Edge found. "
            "Install Chrome at one of: " + ", ".join(CHROME_PATHS)
        )
    logger.info("Using browser: %s", chrome_path)

    # Create a temp user data dir with the PDF-as-download preference
    user_data_dir = Path(tempfile.mkdtemp(prefix="ncuc_chrome_"))
    prefs_dir = user_data_dir / "Default"
    prefs_dir.mkdir(parents=True)
    (prefs_dir / "Preferences").write_text(
        json.dumps({"plugins": {"always_open_pdf_externally": True}})
    )

    pw = sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=True,
            executable_path=chrome_path,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            accept_downloads=True,
        )
        # Stash for cleanup
        ctx._ncuc_user_data_dir = user_data_dir  # type: ignore[attr-defined]
    except Exception:
        shutil.rmtree(user_data_dir, ignore_errors=True)
        pw.stop()
        raise

    try:
        page = ctx.new_page()
        _do_login(page, settings)
        return pw, ctx, page
    except Exception:
        ctx.close()
        shutil.rmtree(user_data_dir, ignore_errors=True)
        pw.stop()
        raise


def close_authenticated_context(pw, ctx) -> None:
    """Close the browser context and clean up the temp user data directory."""
    user_data_dir = getattr(ctx, "_ncuc_user_data_dir", None)
    try:
        ctx.close()
    finally:
        if user_data_dir:
            shutil.rmtree(user_data_dir, ignore_errors=True)
        pw.stop()


def _do_login(page: "Page", settings: Settings) -> None:
    """
    Log into the NCUC portal using the NCIDLogin form.
    After this returns, the page session has an authenticated GOVNCUC_SessionId cookie.
    """
    # Prime CF cookies on the whitelisted portal.aspx
    logger.info("Loading NCUC portal...")
    page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)
    time.sleep(2)

    # Navigate to login page — real Chrome passes CF challenge instantly
    logger.info("Navigating to login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)

    title = page.title()
    if "Just a moment" in title or "checking your browser" in page.content().lower():
        raise NcucSessionError(
            f"CF challenge not solved on login page (title={title!r}). "
            "Ensure an installed Chrome/Edge is available."
        )

    if "NCIDLogin" not in title and "login" not in title.lower():
        raise NcucSessionError(f"Unexpected page after login navigation: title={title!r} url={page.url}")

    logger.info("Login page loaded: %r", title)

    # Fill and submit the portal login form
    page.wait_for_selector(_USERNAME_FIELD, timeout=10000)
    page.fill(_USERNAME_FIELD, settings.ncid_username)
    page.fill(_PASSWORD_FIELD, settings.ncid_password)
    logger.info("Submitting credentials for user: %s", settings.ncid_username)
    page.click(_LOGIN_BTN)
    page.wait_for_load_state("domcontentloaded", timeout=20000)
    time.sleep(2)

    # Verify login succeeded
    html = page.content()
    final_title = page.title()
    final_url = page.url

    if "logout" in html.lower():
        logger.info("Login successful. URL=%s", final_url[:80])
    elif "invalid" in html.lower() or "incorrect" in html.lower() or "NCIDLogin" in final_title:
        raise NcucSessionError(
            f"Login failed — invalid credentials or login page returned. "
            f"title={final_title!r}"
        )
    else:
        logger.warning("Login state unclear, proceeding. title=%r url=%s", final_title, final_url[:80])


def get_docket_documents(page: "Page", docket_id: str) -> list[dict]:
    """
    Fetch the Documents tab for a docket and return all document entries.

    Returns list of dicts with keys:
        doc_type, description, date_filed, document_url, view_file_urls
    """
    import re
    from bs4 import BeautifulSoup

    url = f"{DOCKET_DOCS_URL}?DocketId={docket_id}"
    logger.info("Fetching docket documents: %s", url)
    resp = page.goto(url, wait_until="networkidle", timeout=30000)
    time.sleep(2)

    if resp and resp.status == 403:
        raise NcucSessionError(f"403 Forbidden on docket docs — session may have expired")

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    documents = []

    # Documents are in a grid table — rows alternate between header and data
    # Each document row: Type | Description (with links) | Date Filed
    grid_table = None
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "Document Type" in headers or "Description" in headers:
            grid_table = table
            break

    # Fall back to scanning all tables for ViewFile links if no header-based table found
    if not grid_table:
        # Use the table that contains the most ViewFile links
        best_table = None
        best_count = 0
        for table in soup.find_all("table"):
            count = len(table.find_all("a", href=re.compile(r"ViewFile", re.I)))
            if count > best_count:
                best_count = count
                best_table = table
        grid_table = best_table

    if not grid_table:
        logger.warning("No document grid table found for docket %s", docket_id)
        return documents

    rows = grid_table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        # We need at least 2 cells with actual content; skip header/boilerplate rows
        if len(cells) < 2:
            continue

        # Identify doc_type cell — a short cell (< 20 chars) with no links
        # and description cell — a longer cell with links
        view_file_urls = []
        doc_detail_url = None
        doc_type = ""
        description = ""
        date_filed = ""

        for cell in cells:
            cell_text = cell.get_text(" ", strip=True)
            cell_links = cell.find_all("a", href=True)
            vf_links = [a["href"] for a in cell_links if "ViewFile" in a["href"]]
            doc_links = [a["href"] for a in cell_links if "PSCDocumentDetails" in a["href"]]

            if vf_links:
                view_file_urls.extend(vf_links)
            if doc_links:
                doc_detail_url = doc_links[0]

            # Classify the cell role by content
            if not vf_links and not doc_links and len(cell_text) < 25 and cell_text:
                # Short plain cell — could be doc type or date
                if re.match(r"\d{1,2}/\d{1,2}/\d{4}", cell_text):
                    date_filed = cell_text
                elif not doc_type:
                    doc_type = cell_text
            elif cell_text and len(cell_text) > 10 and not view_file_urls and not doc_detail_url:
                # Longer text — likely description
                clean = re.sub(r"\bFiles:\s*", "", cell_text).strip()
                if clean and not description:
                    description = clean[:300]

        # If description still empty, try extracting from the description cell
        if not description and doc_detail_url:
            for cell in cells:
                doc_links = [a for a in cell.find_all("a", href=True) if "PSCDocumentDetails" in a["href"]]
                if doc_links:
                    raw = cell.get_text(" ", strip=True)
                    description = re.sub(r"\bFiles:\s*", "", raw).strip()[:300]
                    break

        if not view_file_urls and not doc_detail_url and not description:
            continue
        # Skip the big header/boilerplate row (contains entire grid as text)
        if len(description) > 500:
            continue
        normalized_description = description.lower()
        if (
            "to view a document or form" in normalized_description
            or normalized_description.startswith("details documents service list subscribe")
            or normalized_description.startswith("docket documents")
        ):
            continue

        documents.append({
            "doc_type": doc_type,
            "description": description,
            "date_filed": date_filed,
            "document_url": doc_detail_url,
            "view_file_urls": view_file_urls,
        })

    # Deduplicate by doc_detail_url
    seen = set()
    deduped = []
    for d in documents:
        key = d["document_url"] or d["description"][:60]
        if key and key not in seen:
            seen.add(key)
            deduped.append(d)
    return deduped


def download_view_file(page: "Page", view_file_url: str, dest_path: Path) -> int:
    """
    Download a ViewFile.aspx document into dest_path using the authenticated browser session.
    Returns file size in bytes.

    Requires the context to have been created with create_authenticated_context(), which sets
    plugins.always_open_pdf_externally=True in the Chrome user profile — this makes Chrome
    treat application/pdf responses as file downloads rather than rendering them inline,
    so page.expect_download() captures the raw server bytes.

    page.goto() raises "Download is starting" when the download triggers; we swallow that
    error and wait for the download event which fires concurrently.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    with page.expect_download(timeout=30000) as download_info:
        try:
            page.goto(view_file_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass  # "Download is starting" error is expected and harmless

    download = download_info.value
    download.save_as(str(dest_path))
    size = dest_path.stat().st_size
    logger.info("Downloaded %d bytes: %s", size, dest_path.name)
    return size


def resolve_docket_ids(page: "Page", docket_number: str) -> list[dict[str, str]]:
    """
    Search the authenticated docket portal and return matching DocketId results.

    The portal search expects ``E-2 Sub 1354`` formatting rather than
    ``E-2, Sub 1354``. Results include the visible docket label and full URL.
    """
    import re

    normalized_query = re.sub(r"\s+", " ", docket_number.replace(",", " ")).strip()
    normalized_target = normalized_query.lower()
    target_compact = re.sub(r"[^a-z0-9]+", "", normalized_target)

    page.goto(DOCKET_SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(_DOCKET_SEARCH_FIELD, timeout=15000)
    page.fill(_DOCKET_SEARCH_FIELD, normalized_query)
    page.click(_DOCKET_SEARCH_BTN)
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    time.sleep(3)

    links = page.locator("a").evaluate_all(
        """
        els => els
          .map(e => ({text: (e.innerText || '').trim(), href: e.href}))
          .filter(x => x.text || x.href)
        """
    )

    results: list[dict[str, str]] = []
    for link in links:
        href = link["href"]
        text = re.sub(r"\s+", " ", link["text"]).strip()
        if "DocketDetails.aspx?DocketId=" not in href:
            continue
        normalized_text = text.lower()
        compact_text = re.sub(r"[^a-z0-9]+", "", normalized_text)
        docket_id_match = re.search(r"DocketId=([a-f0-9-]{36})", href, re.I)
        if not docket_id_match:
            continue
        match_type = None
        if normalized_text == normalized_target:
            match_type = "exact"
        elif compact_text == target_compact:
            match_type = "normalized_exact"
        elif normalized_target in normalized_text or normalized_text in normalized_target:
            match_type = "partial"
        else:
            target_sub = re.search(r"\bsub\s+(\d+)\b", normalized_target, re.I)
            text_sub = re.search(r"\bsub\s+(\d+)\b", normalized_text, re.I)
            target_base = re.search(r"\b([a-z]-\d+)\b", normalized_target, re.I)
            text_base = re.search(r"\b([a-z]-\d+)\b", normalized_text, re.I)
            if (
                target_sub
                and text_sub
                and target_base
                and text_base
                and target_sub.group(1) == text_sub.group(1)
                and target_base.group(1) == text_base.group(1)
            ):
                match_type = "same_base_and_sub"
        if match_type is None:
            continue
        results.append(
            {
                "docket_number": text,
                "docket_id": docket_id_match.group(1),
                "href": href,
                "match_type": match_type,
            }
        )
    match_rank = {
        "exact": 0,
        "normalized_exact": 1,
        "same_base_and_sub": 2,
        "partial": 3,
    }
    results.sort(key=lambda item: (match_rank.get(str(item.get("match_type") or ""), 9), item["docket_number"]))
    return results


def test_authenticated_access(page: "Page", docket_id: str) -> dict:
    """Test whether the authenticated session can access a DocketDetails page."""
    import re

    url = f"https://starw1.ncuc.gov/NCUC/PSC/DocketDetails.aspx?DocketId={docket_id}"
    logger.info("Testing authenticated access: %s", url[:80])
    resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)

    html = page.content()
    title = page.title()
    is_cf = "Just a moment" in title or "checking your browser" in html.lower()
    doc_links = re.findall(
        r'href=["\']([^"\']*(?:ViewFile|GetFile|DocumentId|PSCDocument)[^"\']*)["\']',
        html, re.I,
    )
    return {
        "accessible": not is_cf,
        "status_code": resp.status if resp else None,
        "html_length": len(html),
        "cf_blocked": is_cf,
        "title": title,
        "doc_links": doc_links[:20],
    }
