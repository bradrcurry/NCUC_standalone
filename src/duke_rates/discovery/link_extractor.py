from __future__ import annotations

import json
import re
from collections.abc import Iterable
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from duke_rates.models.document import DocumentCategory, DocumentKind
from duke_rates.utils.text import normalize_whitespace

JSS_STATE_RE = re.compile(
    r'<script type="application/json" id="__JSS_STATE__">(.*?)</script>',
    re.S,
)
PDF_LIKE_RE = re.compile(r"\.pdf($|\?)", re.I)
RATE_PAGE_RE = re.compile(r"/(billing/rates|tariff|rider|public-notices)", re.I)
STATE_COMPANY_RE = re.compile(r"\b(carolinas|progress|florida|indiana|kentucky|ohio)\b", re.I)
DATE_RE = re.compile(r"\b(?:effective|eff\.)[:\s]+([A-Za-z]+\s+\d{1,2},\s+\d{4})", re.I)


def _is_same_domain(url: str, *, base_url: str) -> bool:
    return urlparse(url).netloc == urlparse(base_url).netloc


def _iter_jss_strings(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_jss_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_jss_strings(child)
    elif isinstance(value, str):
        yield value


def _dedupe_results(
    documents: list[dict],
    related_pages: list[str],
) -> tuple[list[dict], list[str]]:
    dedup_docs: dict[str, dict] = {}
    for item in documents:
        if "facebook.com" in item["url"] or "linkedin.com" in item["url"]:
            continue
        existing = dedup_docs.get(item["url"])
        if existing is None:
            dedup_docs[item["url"]] = item
            continue
        existing_title = existing.get("title", "")
        new_title = item.get("title", "")
        if existing_title.lower().endswith(".pdf") and not new_title.lower().endswith(".pdf"):
            dedup_docs[item["url"]] = item
    dedup_pages = list(
        dict.fromkeys(url for url in related_pages if "sitecore/content/metadata" not in url)
    )
    return list(dedup_docs.values()), dedup_pages


def _extract_links_from_fragment(fragment: str, base_url: str) -> tuple[list[dict], list[str]]:
    documents: list[dict] = []
    related_pages: list[str] = []

    if "<" in fragment and ">" in fragment:
        soup = BeautifulSoup(fragment, "lxml")
        for anchor in soup.find_all("a", href=True):
            href, _ = urldefrag(urljoin(base_url, anchor["href"]))
            if not href.startswith("http"):
                continue
            title = normalize_whitespace(anchor.get_text(" ", strip=True))
            title = title or href.rsplit("/", 1)[-1]
            category = guess_category(title, href)
            kind = guess_kind(href)
            if kind == DocumentKind.PDF:
                documents.append(
                    {
                        "title": title,
                        "url": href,
                        "category": category,
                        "kind": kind,
                    }
                )
            elif _is_same_domain(href, base_url=base_url) and RATE_PAGE_RE.search(href):
                related_pages.append(href)

    direct_patterns = re.findall(
        r'(https?://[^\s"<>\']+|/[^\s"<>\']+(?:\.pdf[^\s"<>\']*|billing/rates[^\s"<>\']*))',
        fragment,
        re.I,
    )
    for candidate in direct_patterns:
        href, _ = urldefrag(urljoin(base_url, candidate))
        title = href.rsplit("/", 1)[-1]
        category = guess_category(title, href)
        kind = guess_kind(href)
        if kind == DocumentKind.PDF:
            documents.append(
                {
                    "title": title,
                    "url": href,
                    "category": category,
                    "kind": kind,
                }
            )
        elif _is_same_domain(href, base_url=base_url) and RATE_PAGE_RE.search(href):
            related_pages.append(href)

    return documents, related_pages


def extract_jss_state(html: str) -> dict | None:
    match = JSS_STATE_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def extract_links_from_jss_payload(payload: dict, base_url: str) -> tuple[list[dict], list[str]]:
    route = payload.get("sitecore", {}).get("route") or payload
    placeholders = route.get("placeholders", {})
    main_content = placeholders.get("jss-public-main")
    roots: list[object] = []
    if main_content:
        roots.append(main_content)
    roots.append(route.get("fields", {}))

    documents: list[dict] = []
    related_pages: list[str] = []
    for root in roots:
        for value in _iter_jss_strings(root):
            if not any(
                token in value.lower()
                for token in (
                    "<a ",
                    ".pdf",
                    "/-/media/",
                    "/billing/rates",
                    "public-notices",
                    "rate schedule",
                    "rider",
                )
            ):
                continue
            fragment_docs, fragment_pages = _extract_links_from_fragment(value, base_url)
            documents.extend(fragment_docs)
            related_pages.extend(fragment_pages)
    return _dedupe_results(documents, related_pages)


def guess_category(title: str, url: str) -> DocumentCategory:
    probe = f"{title} {url}".lower()
    if "rider" in probe:
        return DocumentCategory.RIDER
    if "notice" in probe:
        return DocumentCategory.PUBLIC_NOTICE
    if "index" in probe:
        return DocumentCategory.INDEX
    if "tariff" in probe:
        return DocumentCategory.TARIFF
    if "rate" in probe or "schedule" in probe:
        return DocumentCategory.RATE
    return DocumentCategory.OTHER


def guess_kind(url: str, content_type: str | None = None) -> DocumentKind:
    if PDF_LIKE_RE.search(url) or (content_type and "pdf" in content_type.lower()):
        return DocumentKind.PDF
    if (content_type and "html" in content_type.lower()) or RATE_PAGE_RE.search(url):
        return DocumentKind.HTML
    return DocumentKind.OTHER


def infer_company(text: str) -> str | None:
    matches = {match.group(1).lower() for match in STATE_COMPANY_RE.finditer(text)}
    return next(iter(matches)) if len(matches) == 1 else None


def infer_effective_date(text: str) -> str | None:
    match = DATE_RE.search(text)
    return match.group(1) if match else None


def extract_links_from_html(html: str, base_url: str) -> tuple[list[dict], list[str]]:
    soup = BeautifulSoup(html, "lxml")
    documents: list[dict] = []
    related_pages: list[str] = []

    for anchor in soup.find_all("a", href=True):
        href, _ = urldefrag(urljoin(base_url, anchor["href"]))
        if not href.startswith("http"):
            continue
        title = normalize_whitespace(anchor.get_text(" ", strip=True)) or href.rsplit("/", 1)[-1]
        category = guess_category(title, href)
        kind = guess_kind(href)
        if kind == DocumentKind.PDF:
            documents.append(
                {
                    "title": title,
                    "url": href,
                    "category": category,
                    "kind": kind,
                }
            )
        elif _is_same_domain(href, base_url=base_url) and RATE_PAGE_RE.search(href):
            related_pages.append(href)

    jss_state = extract_jss_state(html)
    if jss_state:
        payload_docs, payload_pages = extract_links_from_jss_payload(jss_state, base_url)
        documents.extend(payload_docs)
        related_pages.extend(payload_pages)

    return _dedupe_results(documents, related_pages)
