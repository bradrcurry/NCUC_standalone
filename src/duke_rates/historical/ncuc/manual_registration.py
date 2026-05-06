from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from duke_rates.db.repository import Repository
from duke_rates.historical.family_targets import build_progress_nc_family_targets
from duke_rates.historical.ncuc.pipeline.metadata_extractor import extract_dates_from_span
from duke_rates.historical.ncuc.pipeline.page_miner import mine_document_pages
from duke_rates.historical.ncuc.pipeline.segmentation import segment_document
from duke_rates.models.pipeline import PageEvidence, TariffSpan
from duke_rates.parse.heuristics import (
    extract_docket_footer,
    extract_effective_date,
    extract_schedule_code,
    extract_supersedes,
)


@dataclass(frozen=True)
class RegistrationTargetHints:
    family_key: str
    leaf_no: str | None = None
    code: str | None = None
    title: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegistrationSuggestion:
    start_page: int
    end_page: int
    title: str | None
    effective_start: str | None
    supersedes_label: str | None
    leaf_no: str | None
    docket_number: str | None
    order_date: str | None
    confidence: float
    match_reasons: tuple[str, ...]


def suggest_registration_metadata(
    repository: Repository,
    *,
    family_key: str,
    pdf_path: str | Path,
) -> RegistrationSuggestion | None:
    path = Path(pdf_path)
    pages = mine_document_pages(str(path))
    spans = segment_document(pages, parent_discovery_id=None)
    hints = _build_target_hints(repository, family_key)
    return _suggest_from_pages(spans=spans, pages=pages, hints=hints)


def _build_target_hints(repository: Repository, family_key: str) -> RegistrationTargetHints:
    family_key_norm = (family_key or "").strip().lower()
    for target in build_progress_nc_family_targets(repository).values():
        if (target.family_key or "").strip().lower() != family_key_norm:
            continue
        aliases = tuple(
            dict.fromkeys(
                alias.strip()
                for alias in [target.title, *(target.aliases or ()), target.code, target.leaf_no]
                if alias and str(alias).strip()
            )
        )
        return RegistrationTargetHints(
            family_key=family_key,
            leaf_no=target.leaf_no,
            code=target.code,
            title=target.title,
            aliases=aliases,
        )

    leaf_match = re.search(r"leaf-(\d+)", family_key_norm)
    code_match = re.search(r"(?:leaf-\d+-|rider-)([a-z0-9-]+)$", family_key_norm)
    code = code_match.group(1).upper() if code_match and not code_match.group(1).isdigit() else None
    aliases = tuple(item for item in (leaf_match.group(1) if leaf_match else None, code) if item)
    return RegistrationTargetHints(
        family_key=family_key,
        leaf_no=leaf_match.group(1) if leaf_match else None,
        code=code,
        aliases=aliases,
    )


def _suggest_from_pages(
    *,
    spans: list[TariffSpan],
    pages: list[PageEvidence],
    hints: RegistrationTargetHints,
) -> RegistrationSuggestion | None:
    page_map = {page.page_number: page for page in pages}
    scored: list[tuple[int, TariffSpan, list[str]]] = []

    for span in spans:
        if span.doc_type != "tariff":
            continue
        score, reasons = _score_span(span, page_map=page_map, hints=hints)
        if score <= 0:
            continue
        scored.append((score, span, reasons))

    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], -(item[1].end_page - item[1].start_page)), reverse=True)
    best_score, best_span, reasons = scored[0]
    full_text_pages = {
        page_no: page_map[page_no].text_content or ""
        for page_no in range(best_span.start_page, best_span.end_page + 1)
        if page_no in page_map
    }
    extract_dates_from_span(best_span, full_text_pages)

    combined_text = "\n".join(full_text_pages.get(page_no, "") for page_no in sorted(full_text_pages))
    docket_number, order_date = extract_docket_footer(combined_text)
    effective_start = best_span.dates[0].date_value if best_span.dates else _normalize_effective_date(combined_text)
    supersedes_label = extract_supersedes(combined_text)
    leaf_no = _pick_leaf_no(best_span, hints)
    title = _pick_title(best_span, combined_text, hints)

    confidence = min(1.0, 0.25 + (best_score / 20.0))
    return RegistrationSuggestion(
        start_page=best_span.start_page,
        end_page=best_span.end_page,
        title=title,
        effective_start=effective_start,
        supersedes_label=supersedes_label,
        leaf_no=leaf_no,
        docket_number=docket_number,
        order_date=order_date,
        confidence=confidence,
        match_reasons=tuple(reasons),
    )


def _score_span(
    span: TariffSpan,
    *,
    page_map: dict[int, PageEvidence],
    hints: RegistrationTargetHints,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    text_blob = _span_text_blob(span, page_map)
    title_blob = " ".join(sorted(span.extracted_schedule_titles | set(span.header_footer_snippets)))
    schedule_code = extract_schedule_code(title_blob, text_blob)

    if hints.leaf_no and hints.leaf_no in span.extracted_leaf_nos:
        score += 10
        reasons.append(f"leaf={hints.leaf_no}")

    if hints.code and schedule_code and schedule_code.upper() == hints.code.upper():
        score += 8
        reasons.append(f"code={schedule_code.upper()}")

    alias_blob = f"{title_blob}\n{text_blob}".lower()
    for alias in hints.aliases:
        normalized = str(alias).strip().lower()
        if not normalized:
            continue
        if normalized in alias_blob:
            score += 3
            reasons.append(f"alias={alias}")

    if extract_supersedes(text_blob):
        score += 2
        reasons.append("supersedes_footer")
    if extract_docket_footer(text_blob)[0]:
        score += 2
        reasons.append("docket_footer")
    if extract_effective_date(text_blob):
        score += 2
        reasons.append("effective_footer")

    return score, list(dict.fromkeys(reasons))


def _span_text_blob(span: TariffSpan, page_map: dict[int, PageEvidence]) -> str:
    parts: list[str] = []
    for page_no in range(span.start_page, span.end_page + 1):
        page = page_map.get(page_no)
        if page and page.text_content:
            parts.append(page.text_content)
    return "\n".join(parts)


def _pick_leaf_no(span: TariffSpan, hints: RegistrationTargetHints) -> str | None:
    if hints.leaf_no:
        return hints.leaf_no
    if span.extracted_leaf_nos:
        return sorted(span.extracted_leaf_nos)[0]
    return None


def _pick_title(span: TariffSpan, combined_text: str, hints: RegistrationTargetHints) -> str | None:
    explicit_titles = [title.strip() for title in sorted(span.extracted_schedule_titles) if title.strip()]
    if hints.code:
        code_re = re.compile(rf"\b(?:schedule|rider|rate)\s+{re.escape(hints.code.upper())}\b", re.I)
        for title in explicit_titles:
            if code_re.search(title):
                return title
    if explicit_titles:
        return explicit_titles[0]
    if hints.title:
        return hints.title
    schedule_code = extract_schedule_code("", combined_text)
    if schedule_code:
        return f"Schedule {schedule_code}"
    return None


def _normalize_effective_date(text: str) -> str | None:
    raw = extract_effective_date(text)
    if not raw:
        return None
    from datetime import datetime

    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
