"""NCUC document discovery: multi-strategy acquisition of docket leads."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import httpx
from bs4 import BeautifulSoup

from duke_rates.config import Settings
from duke_rates.historical.ncuc.metadata import (
    classify_filing,
    extract_docket_from_text,
    extract_docket_from_url,
    extract_leaf_nos,
    extract_rider_codes,
    extract_schedule_codes,
    is_duke_progress_related,
    normalize_filing_date,
    score_relevance,
)
from duke_rates.models.ncuc import (
    NcucAcquisitionMethod,
    NcucDiscoveryRecord,
    NcucDocketSeed,
    NcucFetchStatus,
    NcucFilingClassification,
    NcucSearchQuery,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NCUC portal URL patterns (discovered empirically via live probing)
# ---------------------------------------------------------------------------

# Primary portal - uses Cloudflare bot protection; requires Playwright or session cookies
NCUC_PORTAL_BASE = "https://starw1.ncuc.gov/NCUC"
NCUC_DOCKET_SEARCH = f"{NCUC_PORTAL_BASE}/page/Dockets/portal.aspx"
NCUC_DOCUMENT_SEARCH = f"{NCUC_PORTAL_BASE}/page/DocumentsParameterSearch/portal.aspx"
NCUC_EDOCKET_SEARCH = f"{NCUC_PORTAL_BASE}/page/DocumentsParameterSearch/portal.aspx"
NCUC_DOCKET_DETAILS = f"{NCUC_PORTAL_BASE}/PSC/DocketDetails.aspx"
NCUC_VIEW_FILE = f"{NCUC_PORTAL_BASE}/ViewFile.aspx"
NCUC_ORDERS_PAGE = f"{NCUC_PORTAL_BASE}/page/Orders/portal.aspx"

# Public-facing website with Zoom search (accessible without Cloudflare)
NCUC_PUBLIC_BASE = "https://www.ncuc.gov"
NCUC_ZOOM_SEARCH = f"{NCUC_PUBLIC_BASE}/search/search.php"

# Legacy redirect target
NCUC_LEGACY_BASE = "http://www.ncuc.net"  # redirects to ncuc.gov

# Known Duke Energy Progress docket ranges (E-2 series)
# Sub-docket numbers sourced from NCUC annual orders reports (orders2016-2020.pdf)
DUKE_PROGRESS_E2_DOCKETS: list[NcucDocketSeed] = [
    NcucDocketSeed(
        docket_number="E-2",
        proceeding_type="rate_case",
        description="Duke Energy Progress (formerly Progress Energy Carolinas) general rate cases",
        referenced_schedule_codes=["501", "503", "504"],
        notes=["Primary rate case docket for Duke Energy Progress NC"],
    ),
    # --- General Rate Cases ---
    NcucDocketSeed(
        docket_number="E-2, Sub 1142",
        proceeding_type="rate_case",
        description="Duke Energy Progress 2012 general rate case",
        referenced_schedule_codes=["501", "503", "504", "571", "572"],
        notes=[
            "2012 rate case - produced many rider updates",
            "DocketId=6ea9dcdc-5417-4c84-b6a0-90fa464d8995",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1190",
        proceeding_type="rate_case",
        description="Duke Energy Progress 2017 general rate case",
        referenced_schedule_codes=["501", "503", "504", "571", "572"],
        notes=["2017 rate case", "DocketId=f0788333-d94a-4dd8-ab59-68fee75b1df3"],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1219",
        proceeding_type="rate_case",
        description="Duke Energy Progress 2019 general rate case",
        referenced_schedule_codes=["501", "503", "504", "571", "572"],
        notes=["2019 rate case", "DocketId=4d7d376e-6330-4b16-b949-9a2fe24c4cfc"],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1354",
        proceeding_type="rider",
        description="Duke Energy Progress 2025 Joint Agency Asset Cost Recovery Rider",
        referenced_schedule_codes=["602", "609"],
        notes=["2025 JAAR filing; DocketId=9b3614b6-11d6-4703-8d18-5e2e2ef3d705"],
    ),
    # --- Fuel Charge Adjustment ---
    NcucDocketSeed(
        docket_number="E-2, Sub 1107",
        proceeding_type="rider",
        description="DEP Fuel Charge Adjustment (2016)",
        referenced_schedule_codes=["501"],
        notes=[
            "Annual fuel charge adjustment order, 11/07/2016",
            "DocketId=4cfe112c-ab2c-42ca-bb2a-6e8e236cd0ec",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1146",
        proceeding_type="rider",
        description="DEP Fuel Charge Adjustment (2017)",
        referenced_schedule_codes=["501"],
        notes=[
            "Annual fuel charge adjustment order, 11/17/2017",
            "DocketId=eb843cf7-6b43-4b2c-8817-7a7c81e065ea",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1173",
        proceeding_type="rider",
        description="DEP Fuel Charge Adjustment (2018)",
        referenced_schedule_codes=["501"],
        notes=[
            "Annual fuel charge adjustment order, 11/08/2018",
            "DocketId=bbd156d1-48f4-4a50-9eb8-23375e928622",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1204",
        proceeding_type="rider",
        description="DEP Interim Fuel Charge Adjustment (2019)",
        referenced_schedule_codes=["501"],
        notes=[
            "Interim fuel charge adjustment order, 2019",
            "DocketId=b942d538-5ffd-4dbf-b48c-26122190ef2f",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1250",
        proceeding_type="rider",
        description="DEP Fuel Charge Adjustment (2020)",
        referenced_schedule_codes=["501"],
        notes=[
            "Annual fuel charge adjustment order, 11/30/2020",
            "DocketId=bbd522e0-9653-4d43-b3c7-c864b06e0b87",
        ],
    ),
    # --- DSM/EE Rider (family 610/611/613/672) ---
    NcucDocketSeed(
        docket_number="E-2, Sub 1108",
        proceeding_type="rider",
        description="DEP DSM/EE Rider approval and filing (2016)",
        referenced_schedule_codes=["610", "611", "613", "672"],
        notes=[
            "DSM/EE rider annual order, 2016",
            "DocketId=4ea13050-8330-4af4-ad66-33f85ec92c05",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1141",
        proceeding_type="rider",
        description="Duke Energy Progress EDIT rider",
        referenced_rider_codes=["EDIT"],
        notes=["EDIT clause / income tax adjustment"],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1145",
        proceeding_type="rider",
        description="DEP DSM/EE Rider approval and filing (2017)",
        referenced_schedule_codes=["610", "611", "613", "672"],
        notes=[
            "DSM/EE rider annual order, 2017",
            "DocketId=6b3418ba-df97-438c-9615-ed70764f6d6e",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1174",
        proceeding_type="rider",
        description="DEP DSM/EE Rider approval and filing (2018)",
        referenced_schedule_codes=["610", "611", "613", "672"],
        notes=[
            "DSM/EE rider annual order, 2018",
            "DocketId=55c98c5a-88dd-4dc8-b96f-1c545bce1c0d",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1206",
        proceeding_type="compliance",
        description="DEP DSM/EE compliance filings (2019)",
        referenced_schedule_codes=["610", "611", "613", "672"],
        notes=["DSM/EE compliance filing", "DocketId=f98470e9-2cb0-495c-b969-f6b092ff1fd1"],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1252",
        proceeding_type="rider",
        description="DEP DSM/EE Rider approval and filing (2020)",
        referenced_schedule_codes=["610", "611", "613", "672"],
        notes=[
            "DSM/EE rider annual order, 2020",
            "DocketId=733c14dc-793c-4247-830f-6ca4711de5b3",
        ],
    ),
    # --- REPS / Renewable Energy Rider (family 604/605) ---
    NcucDocketSeed(
        docket_number="E-2, Sub 1109",
        proceeding_type="rider",
        description="DEP REPS and REPS EMF Rider (2016)",
        referenced_schedule_codes=["604", "605"],
        notes=[
            "REPS/REPS EMF annual rider order, 2016",
            "DocketId=03ef260a-18d9-4415-828e-af0f1c51f4a6",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1175",
        proceeding_type="rider",
        description="DEP REPS and REPS EMF Rider (2018)",
        referenced_schedule_codes=["604", "605"],
        notes=[
            "REPS/REPS EMF annual rider order, 2018",
            "DocketId=a134c304-8f8f-4d65-9823-1185f99a0764",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1251",
        proceeding_type="rider",
        description="DEP REPS and REPS EMF Riders (2020)",
        referenced_schedule_codes=["604", "605"],
        notes=[
            "REPS/REPS EMF annual rider order, 2020",
            "DocketId=819dd92b-7213-45c8-8b39-813ccf05d849",
        ],
    ),
    # --- Joint Agency Asset Rider / JAAR (family 602/609) ---
    NcucDocketSeed(
        docket_number="E-2, Sub 1110",
        proceeding_type="rider",
        description="DEP Joint Agency Asset Rider Adjustment (2016)",
        referenced_schedule_codes=["602", "609"],
        notes=["JAAR annual order, 2016", "DocketId=aac1d4be-2011-492f-9d1c-16ce8888db8b"],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1143",
        proceeding_type="rider",
        description="DEP Joint Agency Asset Rider order (2017)",
        referenced_schedule_codes=["602", "609"],
        notes=["JAAR order, 2017", "DocketId=c38082cb-98ed-413e-bed6-2d6e75a7ec0e"],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1167",
        proceeding_type="rider",
        description="DEP Joint Agency Asset Rider modification (2018/2019)",
        referenced_schedule_codes=["602", "609"],
        notes=[
            "JAAR modification order, shared with E-7 Sub 1166",
            "DocketId=268382a4-7f1c-4a90-9f8e-37ad2c347488",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1176",
        proceeding_type="rider",
        description="DEP Joint Agency Asset Rider Adjustment (2018)",
        referenced_schedule_codes=["602", "609"],
        notes=["JAAR annual order, 2018", "DocketId=232752f6-4d2e-4967-a0e4-668c20ae568c"],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1207",
        proceeding_type="rider",
        description="DEP Joint Agency Asset Rider Adjustment (2019)",
        referenced_schedule_codes=["602", "609"],
        notes=["JAAR annual order, 2019", "DocketId=c2a9e89a-2bfe-4368-b983-416e34da38ad"],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1253",
        proceeding_type="rider",
        description="DEP Joint Agency Asset Rider (2020)",
        referenced_schedule_codes=["602", "609"],
        notes=[
            "JAAR annual order, 11/30/2020",
            "DocketId=68197d43-0811-4b5f-a30e-bdea79fab25f",
        ],
    ),
    # --- CPRE Rider / Clean Power (family 640) ---
    NcucDocketSeed(
        docket_number="E-2, Sub 1254",
        proceeding_type="rider",
        description="DEP CPRE Rider and CPRE Program Compliance (2020)",
        referenced_schedule_codes=["640"],
        notes=[
            "CPRE (Clean Power Rate Enhancement) rider order, 2020",
            "DocketId=f1d71ac6-47fa-43ee-8f7c-a9d5b85a16ba",
        ],
    ),
    # --- Storm Cost Recovery (family 607) ---
    NcucDocketSeed(
        docket_number="E-2, Sub 1106",
        proceeding_type="rider",
        description="DEP Storm Cost Recovery Rider (2018)",
        referenced_schedule_codes=["607"],
        notes=[
            "Storm cost recovery rider approval, shared with E-7 Sub 1113",
            "DocketId=d704316a-d870-4f17-b9e0-cc3638ac4c58",
        ],
    ),
    NcucDocketSeed(
        docket_number="E-2, Sub 1190",
        proceeding_type="rider",
        description="Duke Energy Progress storm cost recovery",
        referenced_schedule_codes=["607"],
        notes=["Storm cost recovery rider"],
    ),
    # --- Storm Recovery Bonds / STS securitization (family 607/613) ---
    NcucDocketSeed(
        docket_number="E-2, Sub 1262",
        proceeding_type="rider",
        description="DEP Storm Recovery Bonds financing order (2021)",
        referenced_schedule_codes=["607", "613"],
        notes=[
            "Financing Order issued May 10, 2021 granting DEP right to issue Storm Recovery Bonds",
            "Storm Recovery Bonds issued November 24, 2021",
            "Nonbypassable Storm Recovery Charge repaid via STS rider",
            "Source: NC Public Staff 2022 Annual Report",
        ],
    ),
    # --- 2022 General Rate Case ---
    NcucDocketSeed(
        docket_number="E-2, Sub 1300",
        proceeding_type="rate_case",
        description="Duke Energy Progress 2022 general rate case",
        referenced_schedule_codes=["501", "503", "504", "571", "572"],
        notes=[
            "DEP filed application for Adjustment of Rates October 6, 2022",
            "Source: NC Public Staff 2022 Annual Report",
        ],
    ),
    # --- DSM/EE biennial adjustment (referenced in NCUC Cost Allocation Report 2023) ---
    NcucDocketSeed(
        docket_number="E-2, Sub 1322",
        proceeding_type="rider",
        description="DEP DSM/EE rider rate adjustment (biennial, ~2023)",
        referenced_schedule_codes=["610", "611", "613", "672"],
        notes=[
            "Referenced in NCUC Biennial Report on DSM/EE Programs to the Governor (2023)",
            "Contains DEP forward-looking DSM/EE rider rates: Residential ~0.1963 cents/kWh, SGS ~0.1704 cents/kWh, LGS ~0.1475 cents/kWh",
            "Source: https://www.ncuc.gov/reports/CostAllocationReport23.pdf",
        ],
    ),
]

# ---------------------------------------------------------------------------
# Search engine query templates for NCUC document discovery
# ---------------------------------------------------------------------------

SEARCH_QUERY_TEMPLATES: list[str] = [
    # Targeted eDocket URL patterns
    'site:edocket.ncuc.net "Duke Energy Progress"',
    'site:edocket.ncuc.net "Progress Energy Carolinas"',
    # Tariff schedule specific
    'site:edocket.ncuc.net "rate schedule" "Duke Energy Progress"',
    'site:edocket.ncuc.net rider "Duke Energy Progress"',
    # Specific families from priority list
    'site:edocket.ncuc.net "schedule 501" OR "schedule 503" OR "schedule 504"',
    'site:edocket.ncuc.net "schedule 571" OR "schedule 572"',
    'site:edocket.ncuc.net "schedule 602" OR "schedule 604" OR "schedule 605"',
    # NCUC PDF pattern
    '"ncuc.net" "Duke Energy Progress" tariff filetype:pdf',
    # Legacy
    'site:ncuc.net "Progress Energy Carolinas" tariff',
]


@dataclass
class DiscoveryResult:
    record: NcucDiscoveryRecord
    relevance_score: float = 0.0
    notes: list[str] = field(default_factory=list)


class NcucHttpClient:
    """Thin httpx wrapper tuned for NCUC portal navigation."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        )

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        time.sleep(self.settings.rate_limit_seconds)
        return self._client.get(url, params=params)

    def close(self) -> None:
        self._client.close()


class NcucDiscoveryService:
    """
    Multi-strategy NCUC document discovery.

    Strategies (tried in order of reliability):
    1. Manual seed docket inputs with automated follow-up
    2. eDocket portal direct navigation (docket-number lookup)
    3. eDocket portal keyword search
    4. Search-engine-discovered viewer/document URLs (seeded externally)
    5. Playwright browser navigation (fallback for JS-heavy pages)
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = NcucHttpClient(settings)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Strategy 1: Seed docket traversal
    # ------------------------------------------------------------------

    def discover_from_seed_dockets(
        self,
        seeds: list[NcucDocketSeed] | None = None,
        *,
        max_per_docket: int = 50,
    ) -> Iterator[DiscoveryResult]:
        """Yield discovery results for each seeded docket."""
        seeds = seeds or DUKE_PROGRESS_E2_DOCKETS
        for seed in seeds:
            logger.info("Seeded docket discovery: %s", seed.docket_number)
            yield from self._discover_docket(seed, max_results=max_per_docket)

    def _discover_docket(
        self,
        seed: NcucDocketSeed,
        *,
        max_results: int = 50,
    ) -> Iterator[DiscoveryResult]:
        """Attempt to load filing list for a known docket number."""
        docket_str = seed.docket_number
        # Parse "E-2, Sub 1142" → utility=E, case_sub=2, sub=1142
        m = re.match(r"([A-Z])-(\d+)(?:,\s*[Ss]ub\s*(\d+))?", docket_str)
        if not m:
            logger.warning("Cannot parse docket: %s", docket_str)
            return

        utility = m.group(1)
        case_num = m.group(2)
        sub_num = m.group(3)

        # Try eDocket search API
        try:
            yield from self._edocket_docket_search(
                utility=utility,
                case_num=case_num,
                sub_num=sub_num,
                seed=seed,
                max_results=max_results,
            )
        except Exception as exc:
            logger.warning("eDocket docket search failed for %s: %s", docket_str, exc)
            # Emit a pending discovery record with no URL so operators can manually follow up
            record = NcucDiscoveryRecord(
                docket_number=docket_str,
                utility=seed.utility,
                proceeding_type=seed.proceeding_type,
                referenced_schedule_codes=seed.referenced_schedule_codes,
                referenced_rider_codes=seed.referenced_rider_codes,
                acquisition_method=NcucAcquisitionMethod.MANUAL_SEED,
                fetch_status=NcucFetchStatus.FAILED,
                provenance_notes=[f"edocket_search_failed: {exc}"] + seed.notes,
                error_detail=str(exc),
            )
            yield DiscoveryResult(record=record, relevance_score=0.5, notes=["seed_fallback"])

    def _edocket_docket_search(
        self,
        *,
        utility: str,
        case_num: str,
        sub_num: str | None,
        seed: NcucDocketSeed,
        max_results: int,
    ) -> Iterator[DiscoveryResult]:
        """Navigate eDocket search results for a specific docket."""
        params: dict[str, str] = {
            "Utility": utility,
            "CaseSub": case_num,
        }
        if sub_num:
            params["Sub"] = sub_num

        logger.info("Fetching NCUC docket: %s with params %s", NCUC_DOCKET_SEARCH, params)
        resp = self._client.get(NCUC_DOCKET_SEARCH, params=params)

        if resp.status_code == 403:
            logger.warning("NCUC portal returned 403 - Cloudflare protection; use Playwright")
            record = NcucDiscoveryRecord(
                docket_number=f"{utility}-{case_num}" + (f", Sub {sub_num}" if sub_num else ""),
                utility=seed.utility,
                proceeding_type=seed.proceeding_type,
                referenced_schedule_codes=seed.referenced_schedule_codes,
                referenced_rider_codes=seed.referenced_rider_codes,
                acquisition_method=NcucAcquisitionMethod.DOCKET_SCRAPE,
                fetch_status=NcucFetchStatus.REQUIRES_BROWSER,
                discovered_url=str(resp.url),
                provenance_notes=[
                    "edocket_403_requires_browser",
                    f"params={params}",
                ] + seed.notes,
                error_detail="HTTP 403 - requires browser or session cookie",
            )
            yield DiscoveryResult(record=record, relevance_score=0.5, notes=["requires_browser"])
            return

        if resp.status_code != 200:
            logger.warning("eDocket returned %s for docket query", resp.status_code)
            return

        yield from self._parse_edocket_results_page(
            html=resp.text,
            page_url=str(resp.url),
            seed=seed,
            max_results=max_results,
        )

    def _parse_edocket_results_page(
        self,
        *,
        html: str,
        page_url: str,
        seed: NcucDocketSeed,
        max_results: int,
    ) -> Iterator[DiscoveryResult]:
        """Parse filing rows from eDocket search results HTML."""
        soup = BeautifulSoup(html, "lxml")
        count = 0

        # eDocket table rows: look for links to ViewFile or GetDocument
        for link in soup.find_all("a", href=True):
            if count >= max_results:
                break
            href = link["href"]
            abs_url = urljoin(page_url, href)

            is_doc_link = any(
                pattern in href.lower()
                for pattern in ["viewfile", "getdocument", ".pdf", "docid=", "fileid="]
            )
            if not is_doc_link:
                continue

            title = link.get_text(strip=True)
            row_text = ""
            parent = link.find_parent("tr")
            if parent:
                row_text = parent.get_text(" ", strip=True)
            elif link.find_parent("td"):
                row_text = link.find_parent("td").get_text(" ", strip=True)

            # Extract docket from URL or text
            docket, sub = extract_docket_from_url(abs_url)
            if not docket:
                docket, sub = extract_docket_from_text(row_text)

            schedule_codes = extract_schedule_codes(row_text + " " + title)
            rider_codes = extract_rider_codes(row_text + " " + title)
            leaf_nos = extract_leaf_nos(row_text + " " + title)

            # Extract filing date from row
            date_m = re.search(r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})", row_text)
            filing_date = normalize_filing_date(date_m.group(1)) if date_m else None

            classification = classify_filing(title + " " + row_text)

            # Determine if PDF direct link or viewer
            viewer_url = None
            download_url = None
            if ".pdf" in href.lower():
                download_url = abs_url
            elif "viewfile" in href.lower() or "getdocument" in href.lower():
                viewer_url = abs_url

            record = NcucDiscoveryRecord(
                docket_number=docket or seed.docket_number,
                sub_number=sub,
                utility=seed.utility,
                filing_title=title or None,
                filing_date=filing_date,
                proceeding_type=seed.proceeding_type,
                filing_classification=classification,
                referenced_schedule_codes=schedule_codes or seed.referenced_schedule_codes,
                referenced_rider_codes=rider_codes or seed.referenced_rider_codes,
                referenced_leaf_nos=leaf_nos,
                family_keys=seed.family_keys,
                discovered_url=abs_url,
                viewer_url=viewer_url,
                download_url=download_url,
                acquisition_method=NcucAcquisitionMethod.DOCKET_SCRAPE,
                fetch_status=NcucFetchStatus.PENDING,
                provenance_notes=[f"source_page={page_url}", f"seed={seed.docket_number}"],
                page_title=title,
            )

            relevance = score_relevance(title, docket, schedule_codes, rider_codes)
            yield DiscoveryResult(record=record, relevance_score=relevance, notes=[])
            count += 1

        if count == 0:
            logger.info(
                "No document links found on page %s (may require Playwright)", page_url
            )

    # ------------------------------------------------------------------
    # Strategy 2: eDocket keyword search
    # ------------------------------------------------------------------

    def search_edocket_keyword(
        self,
        query: NcucSearchQuery,
        *,
        max_results: int = 100,
    ) -> Iterator[DiscoveryResult]:
        """Search eDocket portal by keyword and yield discovered records."""
        params: dict[str, str] = {"SearchText": query.query_text}
        if query.docket_hint:
            params["DocketHint"] = query.docket_hint

        logger.info("eDocket keyword search: %s", query.query_text)
        try:
            resp = self._client.get(NCUC_EDOCKET_SEARCH, params=params)
        except Exception as exc:
            logger.warning("eDocket keyword search failed: %s", exc)
            return

        if resp.status_code != 200:
            logger.warning(
                "eDocket keyword search HTTP %s for query: %s",
                resp.status_code,
                query.query_text,
            )
            return

        yield from self._parse_edocket_results_page(
            html=resp.text,
            page_url=str(resp.url),
            seed=NcucDocketSeed(
                docket_number=query.docket_hint or "unknown",
                utility="Duke Energy Progress",
                referenced_schedule_codes=[query.schedule_code_hint]
                if query.schedule_code_hint
                else [],
                referenced_rider_codes=[query.rider_code_hint] if query.rider_code_hint else [],
                family_keys=[query.family_key_hint] if query.family_key_hint else [],
            ),
            max_results=max_results,
        )

    def search_public_site(
        self,
        query: NcucSearchQuery,
        *,
        max_results: int = 100,
    ) -> Iterator[DiscoveryResult]:
        """Compatibility shim for callers still using the older public-site name."""
        yield from self.search_edocket_keyword(query, max_results=max_results)

    # ------------------------------------------------------------------
    # Strategy 3: Ingest externally discovered viewer / document URLs
    # ------------------------------------------------------------------

    def ingest_discovered_url(
        self,
        url: str,
        *,
        title: str | None = None,
        docket_hint: str | None = None,
        notes: list[str] | None = None,
        acquisition_method: NcucAcquisitionMethod = NcucAcquisitionMethod.SEARCH_ENGINE,
    ) -> NcucDiscoveryRecord:
        """
        Accept an externally sourced URL (e.g. from search engine results)
        and create a discovery record.  Does NOT download; returns the record
        for the caller to persist and then fetch.
        """
        docket, sub = extract_docket_from_url(url)
        if not docket and docket_hint:
            docket, sub = extract_docket_from_text(docket_hint)

        combined_text = " ".join(filter(None, [title, docket_hint, url]))
        schedule_codes = extract_schedule_codes(combined_text)
        rider_codes = extract_rider_codes(combined_text)
        leaf_nos = extract_leaf_nos(combined_text)
        classification = classify_filing(combined_text)

        viewer_url = None
        download_url = None
        parsed = urlparse(url)
        if parsed.path.lower().endswith(".pdf"):
            download_url = url
        elif any(p in url.lower() for p in ["viewfile", "getdocument", "viewer"]):
            viewer_url = url
        else:
            download_url = url  # assume direct

        return NcucDiscoveryRecord(
            docket_number=docket,
            sub_number=sub,
            filing_title=title,
            referenced_schedule_codes=schedule_codes,
            referenced_rider_codes=rider_codes,
            referenced_leaf_nos=leaf_nos,
            discovered_url=url,
            viewer_url=viewer_url,
            download_url=download_url,
            acquisition_method=acquisition_method,
            fetch_status=NcucFetchStatus.PENDING,
            filing_classification=classification,
            provenance_notes=(notes or []),
        )

    # ------------------------------------------------------------------
    # Strategy 4: Playwright-driven navigation (when HTTP fails)
    # ------------------------------------------------------------------

    def discover_with_playwright(
        self,
        url: str,
        *,
        seed: NcucDocketSeed | None = None,
        max_results: int = 50,
    ) -> list[DiscoveryResult]:
        """
        Use Playwright to render a JavaScript-heavy NCUC page and extract
        document links.  Falls back gracefully if Playwright is not installed.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright not available; install duke-rates[browser]")
            return []

        logger.info("Playwright navigation: %s", url)
        results: list[DiscoveryResult] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(user_agent=self.settings.user_agent)
            try:
                response = page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=int(self.settings.request_timeout * 1000),
                )
                status = response.status if response else 0
                html = page.content()
                final_url = page.url
            finally:
                browser.close()

        if status and status >= 400:
            logger.warning("Playwright got HTTP %s for %s", status, url)
            return []

        effective_seed = seed or NcucDocketSeed(
            docket_number="unknown",
            utility="Duke Energy Progress",
        )
        for dr in self._parse_edocket_results_page(
            html=html,
            page_url=final_url,
            seed=effective_seed,
            max_results=max_results,
        ):
            dr.notes.append("playwright")
            results.append(dr)

        return results

    def discover_with_authenticated_playwright(
        self,
        search_text: str,
        *,
        seed: NcucDocketSeed | None = None,
        max_results: int = 50,
    ) -> list[DiscoveryResult]:
        """
        Use Playwright to perform authenticated NCUC portal searches.
        Navigates to document search page and uses JavaScript to submit form.
        Bypasses Cloudflare by using Chrome browser with proper headers.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright not available; install duke-rates[browser]")
            return []

        logger.info("Playwright search: %s", search_text)
        results: list[DiscoveryResult] = []
        html = None
        final_url = None
        status = 200

        try:
            with sync_playwright() as pw:
                # Launch Chrome with Cloudflare-friendly settings
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                    ]
                )

                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 720},
                )
                page = context.new_page()

                try:
                    # Step 1: Navigate to document search page
                    search_page_url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
                    logger.info("Navigating to search page: %s", search_page_url)

                    response = page.goto(
                        search_page_url,
                        wait_until="domcontentloaded",
                        timeout=60000
                    )

                    # Wait for JavaScript to load the search form
                    page.wait_for_timeout(3000)  # Give JavaScript time to render form
                    status = response.status if response else 200

                    # Step 2: Find search box and enter query
                    logger.info("Entering search text: %s", search_text)

                    # Try multiple selectors for search box
                    search_selectors = [
                        'input[name="SearchText"]',
                        'input[placeholder*="Search"]',
                        'input[id*="search"]',
                        'input[id*="Search"]',
                    ]

                    search_box_found = False
                    for selector in search_selectors:
                        try:
                            if page.locator(selector).count() > 0:
                                page.fill(selector, search_text)
                                logger.info("Found search box with selector: %s", selector)
                                search_box_found = True
                                break
                        except:
                            continue

                    if not search_box_found:
                        logger.warning("Search box not found; trying JavaScript injection")
                        # Use JavaScript to find and populate search field
                        script = f"""
                        var inputs = document.querySelectorAll('input');
                        for (var i = 0; i < inputs.length; i++) {{
                            if (inputs[i].name && inputs[i].name.toLowerCase().includes('search')) {{
                                inputs[i].value = '{search_text}';
                                inputs[i].dispatchEvent(new Event('change', {{ bubbles: true }}));
                                console.log('Populated search field');
                                break;
                            }}
                        }}
                        """
                        page.evaluate(script)

                    # Step 3: Submit form
                    logger.info("Submitting search form")
                    try:
                        # Try to find and click submit button
                        submit_selectors = [
                            'button[type="submit"]',
                            'input[type="submit"]',
                            'button:has-text("Search")',
                            'button:has-text("Submit")',
                        ]

                        submit_found = False
                        for selector in submit_selectors:
                            try:
                                if page.locator(selector).count() > 0:
                                    page.click(selector)
                                    logger.info("Clicked submit button with selector: %s", selector)
                                    submit_found = True
                                    break
                            except:
                                continue

                        if not submit_found:
                            logger.info("Submit button not found; trying JavaScript form submission")
                            page.evaluate("document.querySelectorAll('form')[0].submit();")

                    except Exception as e:
                        logger.warning("Could not submit form: %s", e)

                    # Step 4: Wait for results to load
                    logger.info("Waiting for search results...")
                    try:
                        page.wait_for_load_state("networkidle", timeout=30000)
                    except:
                        logger.info("Timeout on networkidle, proceeding with current content")
                        page.wait_for_timeout(2000)

                    # Step 5: Get page content
                    html = page.content()
                    final_url = page.url
                    logger.info("Page loaded, URL: %s", final_url)

                except Exception as e:
                    logger.error("Playwright search failed: %s", e)
                    try:
                        html = page.content()
                        final_url = page.url
                    except:
                        pass
                finally:
                    context.close()
                    browser.close()

        except Exception as e:
            logger.error("Playwright error: %s", e)
            return []

        if not html or not final_url:
            logger.warning("No content retrieved from search")
            return []

        # Parse results
        effective_seed = seed or NcucDocketSeed(
            docket_number="unknown",
            utility="Duke Energy Progress",
        )
        try:
            for dr in self._parse_edocket_results_page(
                html=html,
                page_url=final_url,
                seed=effective_seed,
                max_results=max_results,
            ):
                dr.notes.append("playwright_search")
                results.append(dr)
            logger.info("Parsed %d results from search", len(results))
        except Exception as e:
            logger.error("Failed to parse results: %s", e)

        return results

    # ------------------------------------------------------------------
    # Strategy 5: Probe a viewer URL and resolve actual download URL
    # ------------------------------------------------------------------

    def resolve_viewer_to_download_url(self, viewer_url: str) -> str | None:
        """
        Attempt to follow an NCUC viewer URL and find the direct PDF/document
        download link embedded in the page.
        """
        logger.info("Resolving viewer URL: %s", viewer_url)
        try:
            resp = self._client.get(viewer_url)
        except Exception as exc:
            logger.warning("Failed to fetch viewer URL %s: %s", viewer_url, exc)
            return None

        if resp.status_code != 200:
            logger.warning("Viewer URL returned %s", resp.status_code)
            return None

        # Check if the response itself is a PDF
        content_type = resp.headers.get("content-type", "")
        if "pdf" in content_type.lower():
            return str(resp.url)

        # Parse HTML for iframe src, embed src, or direct PDF links
        soup = BeautifulSoup(resp.text, "lxml")

        for tag in soup.find_all(["iframe", "embed", "object"]):
            src = tag.get("src") or tag.get("data")
            if src and ".pdf" in src.lower():
                return urljoin(viewer_url, src)

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" in href.lower() or "download" in href.lower():
                return urljoin(viewer_url, href)

        return None

    # ------------------------------------------------------------------
    # Utility: build search queries for priority families
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Strategy 6: NCUC public Zoom search (ncuc.gov/search/search.php)
    # ------------------------------------------------------------------

    def search_ncuc_public(
        self,
        query: NcucSearchQuery,
        *,
        max_results: int = 50,
    ) -> Iterator[DiscoveryResult]:
        """
        Use the NCUC public Zoom search engine (no Cloudflare protection)
        to find document pages, then emit discovery records for each result.
        """
        params = {"zoom_query": query.query_text, "zoom_sort": "0"}
        logger.info("NCUC public search: %s", query.query_text)
        try:
            resp = self._client.get(NCUC_ZOOM_SEARCH, params=params)
        except Exception as exc:
            logger.warning("NCUC public search failed: %s", exc)
            return

        if resp.status_code != 200:
            logger.warning("NCUC public search HTTP %s", resp.status_code)
            return

        soup = BeautifulSoup(resp.text, "lxml")
        count = 0
        for a in soup.find_all("a", href=True):
            if count >= max_results:
                break
            href = a["href"]
            text = a.get_text(strip=True)
            # Skip internal navigation links
            if not any(
                p in href
                for p in ["DocketDetails", "ViewFile", "PSC", ".pdf", "document", "Order"]
            ):
                continue
            if not href.startswith("http"):
                href = urljoin(NCUC_PUBLIC_BASE, href)

            title = text or None
            docket, sub = extract_docket_from_url(href)
            schedule_codes = extract_schedule_codes((title or "") + " " + (query.query_text or ""))
            rider_codes = extract_rider_codes((title or "") + " " + (query.query_text or ""))
            leaf_nos = extract_leaf_nos(title or "")
            classification = classify_filing((title or "") + " " + href)

            record = NcucDiscoveryRecord(
                docket_number=docket,
                sub_number=sub,
                filing_title=title,
                referenced_schedule_codes=schedule_codes,
                referenced_rider_codes=rider_codes,
                referenced_leaf_nos=leaf_nos,
                family_keys=[query.family_key_hint] if query.family_key_hint else [],
                discovered_url=href,
                viewer_url=href if "ViewFile" in href or "DocketDetails" in href else None,
                download_url=href if ".pdf" in href.lower() else None,
                acquisition_method=NcucAcquisitionMethod.SEARCH_ENGINE,
                fetch_status=NcucFetchStatus.PENDING,
                filing_classification=classification,
                search_query=query.query_text,
                provenance_notes=[f"ncuc_zoom_search={query.query_text}"],
                page_title=title,
            )
            relevance = score_relevance(title, docket, schedule_codes, rider_codes)
            yield DiscoveryResult(record=record, relevance_score=relevance, notes=["ncuc_zoom"])
            count += 1

    # ------------------------------------------------------------------
    # Strategy 7: Wayback Machine NCUC docket URL discovery
    # ------------------------------------------------------------------

    def discover_via_wayback(
        self,
        docket_id_pattern: str | None = None,
        *,
        limit: int = 50,
    ) -> Iterator[DiscoveryResult]:
        """
        Query Wayback CDX API for indexed NCUC DocketDetails pages to find
        known docket IDs, then emit discovery records.
        """
        cdx_url = "http://web.archive.org/cdx/search/cdx"
        url_pattern = docket_id_pattern or "starw1.ncuc.gov/NCUC/PSC/DocketDetails.aspx*"
        params = {
            "url": url_pattern,
            "output": "json",
            "limit": str(limit),
            "fl": "original,timestamp,statuscode",
            "filter": "statuscode:200",
            "collapse": "original",
        }
        logger.info("Wayback CDX query for NCUC dockets")
        try:
            resp = self._client.get(cdx_url, params=params)
        except Exception as exc:
            logger.warning("Wayback CDX query failed: %s", exc)
            return

        if resp.status_code != 200:
            return

        try:
            import json as _json
            rows = _json.loads(resp.text)
        except Exception:
            return

        for row in rows[1:]:  # skip header row
            original_url, timestamp, status = row[0], row[1], row[2]
            docket, sub = extract_docket_from_url(original_url)
            # Construct Wayback snapshot URL for probing
            wayback_url = f"http://web.archive.org/web/{timestamp}/{original_url}"
            record = NcucDiscoveryRecord(
                docket_number=docket,
                sub_number=sub,
                utility="Duke Energy Progress",
                discovered_url=original_url,
                viewer_url=original_url,
                acquisition_method=NcucAcquisitionMethod.DOCKET_SCRAPE,
                fetch_status=NcucFetchStatus.PENDING,
                provenance_notes=[
                    f"wayback_snapshot={wayback_url}",
                    f"wayback_timestamp={timestamp}",
                ],
                metadata_json=None,
            )
            yield DiscoveryResult(record=record, relevance_score=0.3, notes=["wayback"])

    @staticmethod
    def build_family_queries(family_keys: list[str]) -> list[NcucSearchQuery]:
        """Build NCUC search queries for a list of family keys (e.g. '605', '670')."""
        queries = []
        for fk in family_keys:
            # Family key is typically the leaf/schedule number
            queries.append(
                NcucSearchQuery(
                    query_text=f'"Duke Energy Progress" "schedule {fk}"',
                    schedule_code_hint=fk,
                    family_key_hint=fk,
                )
            )
            queries.append(
                NcucSearchQuery(
                    query_text=f'"Progress Energy Carolinas" "schedule {fk}"',
                    schedule_code_hint=fk,
                    family_key_hint=fk,
                )
            )
        return queries
