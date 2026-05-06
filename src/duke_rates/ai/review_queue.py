from __future__ import annotations

from duke_rates.models.parse_result import DocumentParseResult


def needs_review(result: DocumentParseResult) -> bool:
    return result.status != "parsed" or bool(result.review_flags)
