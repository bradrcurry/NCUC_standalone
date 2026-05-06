"""
Multi-dimensional flag classifiers (Phase 3).

Each flag classifier is independently runnable — a small, testable unit that
produces a ``ClassificationResult`` for a single boolean or extraction stage.
None of them bundle; each runs on its own.

All classifiers accept ``(text: str, metadata: dict | None)`` and return
``ClassificationResult``. Metadata carries optional pre-extracted signals
(document_fingerprints fields, existing parse_attempt_logs data, etc.).

Usage:
    result = IsFinalClassifier().classify(text, metadata)
    record_classification(conn, "historical_document", str(doc_id),
                          "flag_is_final", result)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from duke_rates.classification.result import ClassificationResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DOCKET_RE = re.compile(r"(?i)(?:DOCKET|E-[27])\s*(?:NO\.?)?\s*([A-Z]-\d+\s*(?:Sub|sub)\s*\d+)")
_UTILITY_PATTERNS = {
    "DEP": re.compile(r"(?i)Duke\s+Energy\s+Progress"),
    "DEC": re.compile(r"(?i)Duke\s+Energy\s+Carolinas"),
    "DEPC": re.compile(r"(?i)Duke\s+(?:Energy\s+)?Power"),
    "PEP": re.compile(r"(?i)Progress\s+Energy\s+Carolinas"),
}
_EFFECTIVE_DATE_RE = re.compile(
    r"(?i)effective\s*(?:as\s+of|on|date)?:?\s*"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4})"
)
_LEAF_NO_RE = re.compile(r"(?i)leaf\s+no\.?\s*(\d+[A-Za-z]?)")


def _confidence_from_count(count: int, max_hits: int = 6) -> float:
    """Simple 0..1 scaling: *count* hits divided by *max_hits*, capped."""
    if max_hits <= 0:
        return 0.0
    return max(0.0, min(1.0, count / max_hits))


def _ok(label: str, confidence: float, evidence: list[dict]) -> ClassificationResult:
    return ClassificationResult(
        label=label,
        confidence=confidence,
        classifier="",
        classifier_version="v1",
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Boolean flag classifiers
# ---------------------------------------------------------------------------


class IsFinalClassifier:
    """``flag_is_final`` — the document represents a final (not proposed) action."""

    _FINAL_MARKERS = [
        (re.compile(r"(?i)final\s+(?:order|decision|determination)"), 4),
        (re.compile(r"(?i)order\s+(?:granting|approving|denying)"), 3),
        (re.compile(r"(?i)approved\s+(?:by|the)\s+(?:the\s+)?(?:commission|NCUC)"), 3),
        (re.compile(r"(?i)it\s+is\s+(?:hereby\s+)?ordered"), 4),
        (re.compile(r"(?i)so\s+ordered"), 5),
        (re.compile(r"(?i)(?:this|the\s+)(?:order|commission\s+order)\s+(?:is|becomes?)\s+(?:final|effective)"), 3),
    ]
    _PROPOSED_MARKERS = [
        (re.compile(r"(?i)proposed\s+(?:order|rates?|tariff|revision)"), 3),
        (re.compile(r"(?i)(?:notice\s+of\s+)?proposed\s+(?:rule|rulemaking)"), 3),
        (re.compile(r"(?i)draft\s+(?:order|decision)"), 4),
        (re.compile(r"(?i)pending\s+(?:approval|decision|order)"), 2),
        (re.compile(r"(?i)subject\s+to\s+(?:final\s+)?(?:commission\s+)?approval"), 2),
    ]

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        text_sample = (text or "")[:4000].lower()
        final_hits = sum(weight for rx, weight in self._FINAL_MARKERS if rx.search(text_sample))
        proposed_hits = sum(weight for rx, weight in self._PROPOSED_MARKERS if rx.search(text_sample))

        if final_hits > proposed_hits:
            confidence = _confidence_from_count(final_hits, max_hits=15)
            label = "true"
            evidence = [{"kind": "final_marker_score", "value": final_hits}]
        elif proposed_hits > 0:
            confidence = _confidence_from_count(proposed_hits, max_hits=15)
            label = "false"
            evidence = [{"kind": "proposed_marker_score", "value": proposed_hits}]
        else:
            confidence = 0.0
            label = "unknown"
            evidence = [{"kind": "no_evidence", "value": 0}]

        return _ok(label, confidence, evidence)


class IsProposedClassifier:
    """``flag_is_proposed`` — the document represents a proposed (not final) action."""

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        final = IsFinalClassifier().classify(text, metadata)
        if final.label == "false":
            return ClassificationResult(
                label="true", confidence=final.confidence,
                classifier="", classifier_version="v1", evidence=final.evidence,
            )
        if final.label == "true":
            return _ok("false", final.confidence, final.evidence)
        return _ok("unknown", 0.0, [{"kind": "no_evidence", "value": 0}])


class IsRedlineClassifier:
    """``flag_is_redline`` — the document contains redline/strikethrough markup."""

    _REDLINE_MARKERS = [
        (re.compile(r"(?i)(?:marked|redline|red\s*line)"), 2),
        (re.compile(r"(?i)(?:proposed|existing|present)\s+(?:rates?|charges?)"), 1),
        (re.compile(r"(?i)(?:strike|stricken|strikethrough|deleted)"), 3),
        (re.compile(r"(?i)(?:inserted|added|new\s+text|underline)"), 2),
        (re.compile(r"(?i)(?:increase|decrease)\s+(?:from|to)\s+(?:\$|¢)"), 1),
    ]

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        meta = metadata or {}
        # Strongest signal: already fingerprinted as redline candidate
        if meta.get("is_redline_candidate") == 1:
            conf = float(meta.get("redline_confidence", 0.8))
            return _ok(
                "true", max(0.5, conf),
                [{"kind": "fingerprint_redline_candidate", "value": meta.get("redline_confidence", 0.8)}],
            )

        text_sample = (text or "")[:3000].lower()
        hits = sum(weight for rx, weight in self._REDLINE_MARKERS if rx.search(text_sample))
        if hits >= 4:
            return _ok("true", _confidence_from_count(hits, 10), [{"kind": "redline_text_markers", "value": hits}])
        if hits >= 1:
            return _ok("false", 0.3, [{"kind": "redline_text_markers", "value": hits}])
        return _ok("unknown", 0.0, [{"kind": "no_evidence", "value": 0}])


class IsConfidentialClassifier:
    """``flag_is_confidential`` — the document is marked confidential/proprietary."""

    _CONFIDENTIAL_MARKERS = [
        (re.compile(r"(?i)confidential"), 5),
        (re.compile(r"(?i)proprietary"), 4),
        (re.compile(r"(?i)protected\s+(?:material|information)"), 4),
        (re.compile(r"(?i)trade\s+secret"), 4),
        (re.compile(r"(?i)(?:subject\s+to|under)\s+protective\s+(?:order|agreement)"), 3),
        (re.compile(r"(?i)not\s+for\s+(?:public\s+)?disclosure"), 3),
        (re.compile(r"(?i)privileged\s+(?:and|&)\s+confidential"), 5),
    ]

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        text_sample = (text or "")[:2000].lower()
        hits = sum(weight for rx, weight in self._CONFIDENTIAL_MARKERS if rx.search(text_sample))
        if hits >= 5:
            return _ok("true", _confidence_from_count(hits, 20), [{"kind": "confidential_markers", "value": hits}])
        if hits >= 2:
            return _ok("false", 0.2, [{"kind": "confidential_markers", "value": hits}])
        return _ok("unknown", 0.0, [{"kind": "no_evidence", "value": 0}])


class HasRateTablesClassifier:
    """``flag_has_rate_tables`` — the document contains rate/cost tables."""

    _RATE_TABLE_MARKERS = [
        (re.compile(r"¢/kWh|cents\s+per\s+k(?:ilo)?w(?:att)?[-\s]?h(?:our)?"), 4),
        (re.compile(r"(?i)\$\s*\d+\.?\d*\s*(?:per|/)\s*(?:month|kwh|kw|day)"), 3),
        (re.compile(r"(?i)(?:customer|basic\s+facility)\s+(?:charge|fee)"), 3),
        (re.compile(r"(?i)(?:energy|fuel|demand|distribution)\s+charge"), 3),
        (re.compile(r"(?i)rate\s+(?:schedule|class|code)\s*[:;]"), 2),
        (re.compile(r"(?i)(?:monthly|annual|daily)\s+rate"), 2),
        (re.compile(r"(?i)total\s+(?:monthly\s+)?bill"), 2),
    ]

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        text_sample = (text or "")[:5000].lower()
        meta = metadata or {}
        hits = sum(weight for rx, weight in self._RATE_TABLE_MARKERS if rx.search(text_sample))
        # Promote if metadata indicates native tables
        if meta.get("has_native_tables") or meta.get("table_count", 0) > 0:
            hits += 3
        if hits >= 5:
            return _ok("true", _confidence_from_count(hits, 15), [{"kind": "rate_table_markers", "value": hits}])
        if hits >= 2:
            return _ok("false", 0.3, [{"kind": "rate_table_markers", "value": hits}])
        return _ok("unknown", 0.0, [{"kind": "no_evidence", "value": 0}])


class HasLeafNumbersClassifier:
    """``flag_has_leaf_numbers`` — the document references one or more leaf numbers."""

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        text_sample = (text or "")[:3000]
        leaf_nos = _LEAF_NO_RE.findall(text_sample)
        if leaf_nos:
            unique = list(dict.fromkeys(leaf_nos))  # deduplicate preserving order
            return _ok(
                "true", _confidence_from_count(len(unique), 10),
                [{"kind": "leaf_numbers_found", "value": unique[:5]}],
            )
        # Check metadata — often pre-extracted
        meta = metadata or {}
        if meta.get("leaf_no"):
            return _ok("true", 0.9, [{"kind": "metadata_leaf_no", "value": meta["leaf_no"]}])
        return _ok("unknown", 0.0, [{"kind": "no_evidence", "value": 0}])


class IsComplianceFilingClassifier:
    """``flag_is_compliance_filing`` — the document is a compliance filing."""

    _COMPLIANCE_MARKERS = [
        (re.compile(r"(?i)compliance\s+(?:filing|report|submission)"), 5),
        (re.compile(r"(?i)filed\s+(?:in|pursuant\s+to)\s+(?:compliance|order)"), 4),
        (re.compile(r"(?i)pursuant\s+to\s+(?:commission\s+)?order"), 3),
        (re.compile(r"(?i)(?:annual|quarterly|monthly)\s+(?:compliance\s+)?report"), 3),
        (re.compile(r"(?i)required\s+by\s+(?:order|the\s+commission)"), 2),
        (re.compile(r"(?i)in\s+compliance\s+with"), 4),
    ]

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        text_sample = (text or "")[:3000].lower()
        hits = sum(weight for rx, weight in self._COMPLIANCE_MARKERS if rx.search(text_sample))
        if hits >= 5:
            return _ok("true", _confidence_from_count(hits, 15), [{"kind": "compliance_markers", "value": hits}])
        if hits >= 2:
            return _ok("false", 0.3, [{"kind": "compliance_markers", "value": hits}])
        return _ok("unknown", 0.0, [{"kind": "no_evidence", "value": 0}])


# ---------------------------------------------------------------------------
# Deterministic extraction classifiers
# ---------------------------------------------------------------------------


class UtilityClassifier:
    """``utility`` — extract the utility company name from document text."""

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        text_sample = (text or "")[:2000]
        meta = metadata or {}
        scores: dict[str, int] = {}
        for code, rx in _UTILITY_PATTERNS.items():
            m = rx.search(text_sample)
            if m:
                scores[code] = len(m.group(0))

        # Merge DEPC into DEP (historical Duke Power references)
        if "DEPC" in scores:
            scores["DEP"] = max(scores.get("DEP", 0), scores["DEPC"])

        if scores:
            best = max(scores, key=lambda k: scores[k])
            evidence = [{"kind": "utility_name_match", "value": best, "weight": float(scores[best])}]
            return _ok(best, min(1.0, scores[best] / 50.0), evidence)

        # Fall back to metadata
        company = meta.get("company", "")
        if company.upper() in ("DEP", "DEC"):
            return _ok(company.upper(), 0.7, [{"kind": "metadata_company", "value": company}])
        return _ok("unknown", 0.0, [{"kind": "no_evidence", "value": 0}])


class DocketNumberClassifier:
    """``docket_number`` — extract the docket number from document text."""

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        text_sample = (text or "")[:3000]
        m = _DOCKET_RE.search(text_sample)
        if m:
            docket = m.group(1).strip()
            # Normalize: "E-2 Sub 1234" or "E-7 Sub 999"
            return _ok(docket, 0.95, [{"kind": "docket_regex_match", "value": docket}])

        meta = metadata or {}
        docket = meta.get("docket_number", "")
        if docket:
            return _ok(str(docket), 0.7, [{"kind": "metadata_docket", "value": docket}])
        return _ok("unknown", 0.0, [{"kind": "no_evidence", "value": 0}])


class EffectiveDateClassifier:
    """``effective_date`` — extract effective date from document text."""

    _MONTH_MAP = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
        "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        text_sample = (text or "")[:2000]
        m = _EFFECTIVE_DATE_RE.search(text_sample)
        if m:
            raw = m.group(1).strip()
            try:
                parsed = self._parse_month_day_year(raw)
                if parsed:
                    return _ok(parsed, 0.9, [{"kind": "effective_date_keyword", "value": raw}])
            except (ValueError, TypeError):
                pass

        meta = metadata or {}
        eff = meta.get("effective_start", "")
        if eff:
            return _ok(str(eff), 0.5, [{"kind": "metadata_effective_start", "value": eff}])
        return _ok("none", 0.0, [{"kind": "no_evidence", "value": 0}])

    def _parse_month_day_year(self, raw: str) -> str | None:
        parts = raw.replace(",", "").split()
        if len(parts) < 3:
            return None
        month = self._MONTH_MAP.get(parts[0].lower())
        if month is None:
            return None
        day = int(parts[1])
        year = int(parts[2])
        if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2099):
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"


class TariffFamilyClassifier:
    """``tariff_family`` — for documents classified as tariff type, extract
    the likely tariff family key.
    """

    def classify(self, text: str, metadata: dict[str, Any] | None = None) -> ClassificationResult:
        meta = metadata or {}
        # Primary signal: family_key is already set on historical_documents
        family_key = meta.get("family_key", "")
        if family_key:
            return _ok(str(family_key), 0.9, [{"kind": "metadata_family_key", "value": family_key}])

        # Fall back: leaf references in text
        text_sample = (text or "")[:3000]
        leaf_nos = _LEAF_NO_RE.findall(text_sample)
        if leaf_nos:
            return _ok(f"leaf-{leaf_nos[0]}", 0.4, [{"kind": "text_leaf_number", "value": leaf_nos[0]}])

        return _ok("unknown", 0.0, [{"kind": "no_evidence", "value": 0}])


# ---------------------------------------------------------------------------
# Registry — one place to enumerate all flag classifiers
# ---------------------------------------------------------------------------

_FLAG_CLASSIFIERS: dict[str, Any] = {
    "flag_is_final": IsFinalClassifier(),
    "flag_is_proposed": IsProposedClassifier(),
    "flag_is_redline": IsRedlineClassifier(),
    "flag_is_confidential": IsConfidentialClassifier(),
    "flag_has_rate_tables": HasRateTablesClassifier(),
    "flag_has_leaf_numbers": HasLeafNumbersClassifier(),
    "flag_is_compliance_filing": IsComplianceFilingClassifier(),
    "utility": UtilityClassifier(),
    "docket_number": DocketNumberClassifier(),
    "effective_date": EffectiveDateClassifier(),
    "tariff_family": TariffFamilyClassifier(),
}


def get_flag_classifier(stage: str):
    """Return the classifier callable for *stage*, or ``None``."""
    return _FLAG_CLASSIFIERS.get(stage)


def all_flag_stages() -> list[str]:
    return list(_FLAG_CLASSIFIERS.keys())
