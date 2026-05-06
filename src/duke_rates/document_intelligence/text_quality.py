from __future__ import annotations

import re
from dataclasses import dataclass


_SUSPICIOUS_TOKEN_PATTERNS: tuple[tuple[str, str], ...] = (
    ("dollar_as_s", r"\b\d+\.\d+\s*S/kWh\b"),
    ("cent_as_cv", r"\b\d+\.\d+\s*cV[kK][wW][hH]\b"),
    ("garbled_percent_or_symbol", r"[Ž^®]{1,}"),
    ("merged_decimal_values", r"\d+\.\d{2}\d+\.\d{2}"),
    ("run_together_leaf", r"LeafNo\s*\d"),
)

_REDLINE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("redline_marker", r"\bredline\b"),
    ("blackline_marker", r"\bblackline\b"),
    ("tracked_changes_marker", r"\btracked changes\b"),
    ("strikeout_marker", r"\bstrike[- ]?through\b|\bstrikethrough\b"),
    ("insert_delete_marker", r"\bdeleted text\b|\binserted text\b"),
)


@dataclass(frozen=True)
class TextQualitySignal:
    suspicious_codes: list[str]
    redline_codes: list[str]

    @property
    def suspicious_hit_count(self) -> int:
        return len(self.suspicious_codes)

    @property
    def redline_hit_count(self) -> int:
        return len(self.redline_codes)


def analyze_text_quality(text: str) -> TextQualitySignal:
    normalized = text or ""
    suspicious_codes = [
        code for code, pattern in _SUSPICIOUS_TOKEN_PATTERNS if re.search(pattern, normalized)
    ]
    redline_codes = [
        code for code, pattern in _REDLINE_PATTERNS if re.search(pattern, normalized, flags=re.IGNORECASE)
    ]
    return TextQualitySignal(
        suspicious_codes=suspicious_codes,
        redline_codes=redline_codes,
    )
