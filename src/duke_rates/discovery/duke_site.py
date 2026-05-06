from __future__ import annotations

import logging
import re
import time
from collections import deque
from urllib.parse import quote, urlparse

import httpx

from duke_rates.config import Settings
from duke_rates.discovery.jurisdictions import select_jurisdictions
from duke_rates.discovery.link_extractor import (
    extract_jss_state,
    extract_links_from_html,
    extract_links_from_jss_payload,
    guess_category,
    guess_kind,
    infer_company,
    infer_effective_date,
)
from duke_rates.discovery.navigators import HttpNavigator, PlaywrightNavigator
from duke_rates.models.document import DiscoveryRecord, DocumentKind
from duke_rates.models.jurisdiction import JurisdictionQuery, JurisdictionSeed
from duke_rates.utils.dates import utc_now

_MEDIA_PDF_RE = re.compile(r'/-/media/[\w/.:\-]+\.pdf', re.I)

logger = logging.getLogger(__name__)

_VALID_COMPANIES_BY_STATE = {
    "NC": {"carolinas", "progress"},
    "SC": {"carolinas", "progress"},
    "FL": {"florida"},
    "IN": {"indiana"},
    "KY": {"kentucky"},
    "OH": {"ohio"},
}
JURISDICTION_SET_URL = "https://www.duke-energy.com/api/JurisdictionSelector/setJurisdictionPerServiceKey"
API_HEADERS = {
    "authorization": "Bearer authorized",
    "cdxp-session": "no-session",
    "auth0-token-storage": "",
    "content-type": "application/json",
}


class DukeDiscoveryService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = HttpNavigator(settings)
        self.playwright = PlaywrightNavigator(settings) if settings.use_playwright else None

    def close(self) -> None:
        self.http.close()

    def crawl(self, query: JurisdictionQuery) -> list[DiscoveryRecord]:
        discoveries: list[DiscoveryRecord] = []
        for jurisdiction in select_jurisdictions(query):
            discoveries.extend(self._crawl_jurisdiction(jurisdiction))
        return discoveries

    def _crawl_jurisdiction(self, jurisdiction: JurisdictionSeed) -> list[DiscoveryRecord]:
        logger.info("Crawling jurisdiction %s (%s)", jurisdiction.label, jurisdiction.key)
        seed_urls = sorted(
            (str(url) for url in jurisdiction.seed_urls),
            key=self._page_priority,
        )
        queue: deque[tuple[str, int]] = deque((url, 0) for url in seed_urls)
        seen_pages: set[str] = set()
        discovered_urls: set[str] = set()
        records: list[DiscoveryRecord] = []

        # Run API discovery once per jurisdiction (not per page)
        api_documents, api_related_pages = self._discover_api_variant(jurisdiction)
        api_discovered: set[str] = set()
        for item in api_documents:
            doc_url = item["url"]
            if doc_url in discovered_urls:
                continue
            discovered_urls.add(doc_url)
            api_discovered.add(doc_url)
            company = self._validated_company(jurisdiction.state, jurisdiction.company)
            records.append(
                DiscoveryRecord(
                    title=item["title"],
                    source_page_url=item.get("source_page_url", doc_url),
                    document_url=doc_url,
                    state=jurisdiction.state,
                    company=company,
                    category=item["category"],
                    kind=item["kind"],
                    effective_date=None,
                    retrieval_timestamp=utc_now(),
                    notes=[
                        f"seed={jurisdiction.key}",
                        f"market={jurisdiction.market}",
                        *item.get("notes", []),
                    ],
                )
            )
        for related in api_related_pages:
            if related not in seen_pages:
                queue.appendleft((related, 0))

        while queue and len(seen_pages) < self.settings.max_pages_per_jurisdiction:
            page_url, depth = queue.popleft()
            if page_url in seen_pages or depth > self.settings.max_crawl_depth:
                continue

            seen_pages.add(page_url)
            page = self.http.fetch(page_url)
            if page.status_code >= 400 and self.playwright:
                page = self.playwright.fetch(page_url)

            documents, related_pages = extract_links_from_html(page.text, page.url)
            state_payload = extract_jss_state(page.text)
            page_title = self._page_title(page.url, state_payload)

            if page.url not in discovered_urls:
                discovered_urls.add(page.url)
                page_company = self._validated_company(
                    jurisdiction.state,
                    jurisdiction.company or infer_company(page.text[:4000]),
                )
                records.append(
                    DiscoveryRecord(
                        title=page_title,
                        source_page_url=page.url,
                        document_url=page.url,
                        state=jurisdiction.state,
                        company=page_company,
                        category=guess_category(page_title, page.url),
                        kind=DocumentKind.HTML,
                        effective_date=infer_effective_date(page.text),
                        retrieval_timestamp=utc_now(),
                        notes=[
                            f"seed={jurisdiction.key}",
                            f"market={jurisdiction.market}",
                            "source=page-archive",
                        ],
                    )
                )

            for item in documents:
                doc_url = item["url"]
                if doc_url in discovered_urls:
                    continue
                discovered_urls.add(doc_url)
                company = self._validated_company(
                    jurisdiction.state,
                    jurisdiction.company or infer_company(f"{page.text[:4000]} {doc_url}"),
                )
                records.append(
                    DiscoveryRecord(
                        title=item["title"],
                        source_page_url=page.url,
                        document_url=doc_url,
                        state=jurisdiction.state,
                        company=company,
                        category=item["category"],
                        kind=item["kind"],
                        effective_date=infer_effective_date(page.text),
                        retrieval_timestamp=utc_now(),
                        notes=[
                            f"seed={jurisdiction.key}",
                            f"market={jurisdiction.market}",
                            "source=html+jss" if state_payload else "source=html",
                            *item.get("notes", []),
                        ],
                    )
                )

            for related in sorted(set(related_pages), key=self._page_priority):
                if related not in seen_pages and depth < self.settings.max_crawl_depth:
                    queue.append((related, depth + 1))

            time.sleep(self.settings.rate_limit_seconds)

        return records

    @staticmethod
    def _page_priority(url: str) -> tuple[int, str]:
        parsed = urlparse(url)
        path = parsed.path.lower()
        if "index-of-rate-schedules" in path:
            return (0, url)
        if "public-notices" in path:
            return (1, url)
        if "/billing/rates" in path:
            return (2, url)
        return (3, url)

    @staticmethod
    def _page_title(page_url: str, state_payload: dict | None) -> str:
        if state_payload:
            route = state_payload.get("sitecore", {}).get("route") or {}
            title = route.get("displayName") or route.get("name")
            if title:
                return str(title)
        return page_url.rstrip("/").rsplit("/", 1)[-1] or "duke-page"

    @staticmethod
    def _validated_company(state: str | None, company: str | None) -> str | None:
        if not state or not company:
            return company
        allowed = _VALID_COMPANIES_BY_STATE.get(state.upper())
        if not allowed:
            return company
        return company if company in allowed else None

    def _discover_api_variant(
        self,
        jurisdiction: JurisdictionSeed,
    ) -> tuple[list[dict], list[str]]:
        if not jurisdiction.jurisdiction_code or not jurisdiction.service_key:
            return [], []
        if not jurisdiction.api_content_path:
            return [], []

        path = jurisdiction.api_content_path.strip("/")
        item = quote(f"/{path}", safe="")
        api_url = (
            "https://www.duke-energy.com/cdxp/api/core/content/jsspublic//"
            f"{path}/en/{jurisdiction.jurisdiction_code}?item={item}"
        )
        base_url = f"https://www.duke-energy.com/{path}"

        session: httpx.Client | None = None
        try:
            session = httpx.Client(
                follow_redirects=True,
                timeout=self.settings.request_timeout,
                headers={
                    "User-Agent": self.settings.user_agent,
                    **API_HEADERS,
                },
            )
            session.post(
                JURISDICTION_SET_URL,
                json={
                    "stateAbbreviation": jurisdiction.state,
                    "serviceKey": jurisdiction.service_key,
                },
            ).raise_for_status()
            response = session.get(api_url)
            response.raise_for_status()
            payload = response.json()

            docs, pages = extract_links_from_jss_payload(payload, base_url)

            # Also scan raw JSON text for /-/media/ PDF paths that JSS parser may miss
            raw_text = response.text
            for pdf_path in _MEDIA_PDF_RE.findall(raw_text):
                pdf_url = f"https://www.duke-energy.com{pdf_path}"
                if not any(d["url"] == pdf_url for d in docs):
                    title = pdf_path.rsplit("/", 1)[-1]
                    docs.append(
                        {
                            "title": title,
                            "url": pdf_url,
                            "category": guess_category(title, pdf_url),
                            "kind": guess_kind(pdf_url),
                            "source_page_url": base_url,
                        }
                    )

            for doc in docs:
                doc.setdefault("notes", [])
                doc["notes"] = [
                    f"api_url={api_url}",
                    f"jurisdiction_code={jurisdiction.jurisdiction_code}",
                    "source=rate-options-api",
                ]
                doc.setdefault("source_page_url", base_url)
            logger.info(
                "API discovery for %s (%s): %d PDFs",
                jurisdiction.key,
                jurisdiction.jurisdiction_code,
                sum(1 for d in docs if d.get("kind") == DocumentKind.PDF),
            )
            return docs, pages
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Failed Duke jurisdiction API discovery for %s (%s): %s",
                jurisdiction.key,
                jurisdiction.jurisdiction_code,
                exc,
            )
            return [], []
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
