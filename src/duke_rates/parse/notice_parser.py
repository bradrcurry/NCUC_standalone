from __future__ import annotations

from pathlib import Path

from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.public_notice import PublicNoticeData
from duke_rates.parse.heuristics import (
    extract_docket_numbers,
    extract_notice_customer_classes,
    extract_notice_filing_date,
    extract_notice_rider_codes,
    extract_notice_schedule_codes,
    summarize_text,
)
from duke_rates.parse.normalization import build_tariff_id, normalize_company


def parse_notice_text(
    *,
    document_id: int,
    title: str,
    state: str | None,
    company: str | None,
    text: str,
    raw_text_path: Path | None = None,
) -> DocumentParseResult:
    normalized_company = normalize_company(title, text, fallback=company, state=state)
    probe = f"{title}\n{text}"
    notice = PublicNoticeData(
        notice_id=build_tariff_id(state, normalized_company, None, title),
        title=title,
        state=state,
        company=normalized_company,
        filing_date=extract_notice_filing_date(probe),
        docket_numbers=extract_docket_numbers(probe),
        related_rider_codes=extract_notice_rider_codes(probe),
        related_schedule_codes=extract_notice_schedule_codes(probe),
        customer_classes=extract_notice_customer_classes(probe),
        summary=summarize_text(text),
    )

    review_flags: list[str] = []
    if not notice.docket_numbers:
        review_flags.append("No docket numbers extracted")
    if not notice.related_rider_codes and not notice.related_schedule_codes:
        review_flags.append("No related rider or schedule references extracted")

    return DocumentParseResult(
        document_id=document_id,
        status=ParseStatus.PARSED if len(review_flags) <= 1 else ParseStatus.PARTIAL,
        parser_name="heuristic_notice_parser",
        raw_text_path=str(raw_text_path) if raw_text_path else None,
        notice=notice,
        review_flags=review_flags,
    )
