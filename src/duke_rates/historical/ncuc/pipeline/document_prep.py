"""
Document Preparation Module

Cleans up and enriches historical_documents metadata:
1. Extract effective dates from document text
2. Classify documents (tariff vs procedural)
3. Identify actual rate tables vs procedural content
"""

import re
from typing import Optional, Tuple
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class DateExtractor:
    """Extract effective dates from tariff document text."""

    # Patterns for date strings
    DATE_PATTERNS = [
        # Standard formats: "January 1, 2023", "Dec 1, 2023"
        r'(?i)(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(\d{4})',

        # Format: "2023-01-01", "01/01/2023"
        r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})',

        # Format: "1/1/2023"
        r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})',
    ]

    EFFECTIVE_KEYWORDS = [
        r'(?i)effective\s+(?:as\s+of|on|date)?:?\s*',
        r'(?i)applicable\s+(?:on|beginning)?:?\s*',
        r'(?i)service\s+rendered\s+on\s+or\s+after:?\s*',
        r'(?i)this\s+(?:schedule|rate|rider)\s+(?:is\s+)?effective:?\s*',
    ]

    MONTH_MAP = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
        'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'jun': 6, 'jul': 7,
        'aug': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    }

    def __init__(self):
        self.date_regexes = [re.compile(p) for p in self.DATE_PATTERNS]
        self.effective_regexes = [re.compile(p) for p in self.EFFECTIVE_KEYWORDS]

    def extract_from_text(self, text: str) -> Optional[str]:
        """
        Extract effective date from document text.
        Returns ISO format date string (YYYY-MM-DD) or None.
        """
        if not text or len(text) < 50:
            return None

        # Look for "Effective ..." patterns
        for line in text.split('\n')[:50]:  # Check first 50 lines
            for eff_regex in self.effective_regexes:
                match = eff_regex.search(line)
                if match:
                    # Found "Effective" keyword, look for date after it
                    remaining = line[match.end():]
                    date_str = self._parse_date_string(remaining)
                    if date_str:
                        return date_str

        # Fall back to looking for any date in first 20 lines
        for line in text.split('\n')[:20]:
            date_str = self._parse_date_string(line)
            if date_str:
                return date_str

        return None

    def _parse_date_string(self, text: str) -> Optional[str]:
        """Parse date from text string."""
        for regex in self.date_regexes:
            match = regex.search(text)
            if match:
                try:
                    groups = match.groups()

                    # Handle Month Day, Year format
                    if len(groups) == 3 and isinstance(groups[0], str) and groups[0].isalpha():
                        month_name = groups[0].lower()
                        month = self.MONTH_MAP.get(month_name)
                        if month:
                            day = int(groups[1])
                            year = int(groups[2])
                            if 1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2099:
                                return f"{year:04d}-{month:02d}-{day:02d}"

                    # Handle YYYY-MM-DD or MM/DD/YYYY formats
                    elif len(groups) == 3:
                        parts = [int(g) for g in groups]

                        # YYYY-MM-DD format
                        if parts[0] > 1900:
                            year, month, day = parts
                        # MM/DD/YYYY format
                        elif parts[2] > 1900:
                            month, day, year = parts
                        else:
                            continue

                        if 1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2099:
                            return f"{year:04d}-{month:02d}-{day:02d}"

                except (ValueError, TypeError):
                    continue

        return None


class DocumentClassifier:
    """Classify documents as tariff vs procedural."""

    TARIFF_INDICATORS = [
        r'(?i)schedule\s+[a-z0-9\-]+',
        r'(?i)leaf\s+no\.?\s+\d+',
        r'(?i)rate\s+schedule',
        r'(?i)availability',
        r'(?i)charges?',
        r'(?i)customer\s+charge',
        r'(?i)energy\s+charge',
        r'(?i)per\s+kwh',
        r'(?i)determination\s+of',
        r'(?i)monthly\s+charge',
    ]

    PROCEDURAL_INDICATORS = [
        r'(?i)comment',
        r'(?i)testimony',
        r'(?i)affidavit',
        r'(?i)brief',
        r'(?i)motion',
        r'(?i)certificate\s+of\s+service',
        r'(?i)order\s+(?:granting|approving|denying)',
        r'(?i)docket\s+no',
        r'(?i)in\s+the\s+matter\s+of',
    ]

    def __init__(self):
        self.tariff_regexes = [re.compile(p) for p in self.TARIFF_INDICATORS]
        self.procedural_regexes = [re.compile(p) for p in self.PROCEDURAL_INDICATORS]

    def classify(self, filing_title: str, text_sample: str) -> str:
        """
        Classify document as 'tariff', 'procedural', or 'unknown'.

        Args:
            filing_title: Original filing title from NCUC record
            text_sample: First 2000 characters of document text

        Returns:
            'tariff', 'procedural', 'testimony', 'order', or 'unknown'
        """
        legacy_label, _ = self._score(filing_title, text_sample)
        return legacy_label

    def classify_with_result(self, filing_title: str, text_sample: str):
        """Classify and return both the legacy string label and a
        ``ClassificationResult`` for the Phase 2 ``document_type`` stage.

        The legacy string is preserved unchanged so downstream code that
        short-circuits on ``doc_type != 'tariff'`` keeps working. The
        ``ClassificationResult`` carries a normalized confidence and
        evidence list suitable for ``document_classifications``.
        """
        from duke_rates.classification.result import ClassificationResult

        legacy_label, scoring = self._score(filing_title, text_sample)
        document_type, alternatives = self._map_legacy_to_document_type(
            legacy_label, scoring
        )
        result = ClassificationResult(
            label=document_type,
            confidence=scoring["confidence"],
            classifier="rule_document_type_v1",
            classifier_version="v1",
            evidence=scoring["evidence"],
            alternatives=alternatives,
            metadata={"legacy_label": legacy_label},
        )
        return legacy_label, result

    # -- internals -----------------------------------------------------------

    def _score(self, filing_title: str, text_sample: str) -> Tuple[str, dict]:
        """Run the underlying regex scoring once. Used by both ``classify``
        and ``classify_with_result`` so the two paths cannot diverge.

        Returns the legacy string label plus a scoring dict carrying the
        evidence list, normalized confidence, and the raw counts needed to
        derive runner-up alternatives.
        """
        combined_text = (filing_title + " " + text_sample).lower()

        tariff_hits = [
            regex.pattern for regex in self.tariff_regexes
            if regex.search(combined_text)
        ]
        procedural_hits = [
            regex.pattern for regex in self.procedural_regexes
            if regex.search(combined_text)
        ]
        tariff_score = len(tariff_hits)
        procedural_score = len(procedural_hits)

        # Legacy decision logic, byte-for-byte preserved.
        legacy_label: str
        if procedural_hits:
            if 'testimony' in combined_text or 'affidavit' in combined_text:
                legacy_label = 'testimony'
            elif 'order' in combined_text or 'approving' in combined_text:
                legacy_label = 'order'
            else:
                legacy_label = 'procedural'
        elif tariff_score >= 2:
            legacy_label = 'tariff'
        elif tariff_score == 1:
            legacy_label = 'unknown'
        else:
            legacy_label = 'unknown'

        # Confidence: max(tariff_score, procedural_score) normalized by the
        # larger of the two indicator-set sizes. Real distribution, not 1.0.
        denom = max(len(self.TARIFF_INDICATORS), len(self.PROCEDURAL_INDICATORS))
        winning_score = max(tariff_score, procedural_score)
        confidence = max(0.0, min(1.0, winning_score / denom)) if denom else 0.0

        evidence = []
        for hit in tariff_hits:
            evidence.append({"kind": "tariff_indicator", "value": hit, "weight": 1.0})
        for hit in procedural_hits:
            evidence.append({"kind": "procedural_indicator", "value": hit, "weight": 1.0})

        return legacy_label, {
            "tariff_score": tariff_score,
            "procedural_score": procedural_score,
            "confidence": confidence,
            "evidence": evidence,
        }

    @staticmethod
    def _map_legacy_to_document_type(
        legacy_label: str, scoring: dict
    ) -> Tuple[str, list]:
        """Map legacy string → seeded ``document_types.code``.

        Defaults are intentionally coarse — Phase 3 flag classifiers will
        refine (e.g. distinguish ``RIDER`` from ``TARIFF_SHEET`` via
        rate-table presence). Returns ``(document_type_code, alternatives)``
        where alternatives carry the runner-up label with its raw count.
        """
        tariff_score = float(scoring["tariff_score"])
        procedural_score = float(scoring["procedural_score"])

        if legacy_label == 'tariff':
            return 'TARIFF_SHEET', [('UNKNOWN', max(0.0, procedural_score))]
        if legacy_label == 'testimony':
            return 'TESTIMONY', [('UNKNOWN', tariff_score)]
        if legacy_label == 'order':
            return 'ORDER_FINAL', [('ORDER_PROCEDURAL', procedural_score)]
        if legacy_label == 'procedural':
            return 'COVER_LETTER', [('UNKNOWN', tariff_score)]
        return 'UNKNOWN', [('TARIFF_SHEET', tariff_score)]


def prepare_document(filing_title: str, text_content: str) -> Tuple[Optional[str], str]:
    """
    Prepare a document by extracting date and classifying type.

    Args:
        filing_title: Document title from NCUC filing
        text_content: Full document text

    Returns:
        Tuple of (effective_start_date, document_type)
    """
    date_extractor = DateExtractor()
    classifier = DocumentClassifier()

    # Extract effective date
    effective_date = date_extractor.extract_from_text(text_content)

    # Classify document
    text_sample = text_content[:2000]
    doc_type = classifier.classify(filing_title, text_sample)

    return effective_date, doc_type
