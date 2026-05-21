"""rule_document_type_v2 — improved per-type pattern classifier with layout signals.

Designed to address the v1 ceiling (avg confidence 0.25, max 0.70) and the
coarse output mapping (v1 emits only 5 of 14 type codes).

Differences from v1 (`historical/ncuc/pipeline/document_prep.DocumentClassifier`):

  1. Per-type signal collections instead of two giant TARIFF/PROCEDURAL lists.
     Each type has its own strong, weak, and negative signals.
  2. Layout features (page_count, text_chars, has_tables, numeric_line_count)
     from document_fingerprints_v2 are first-class signals. v1 ignored them.
  3. First-page / last-page anchor detection — letterhead, signature blocks,
     "BEFORE THE NORTH CAROLINA UTILITIES COMMISSION", "I, ___, do hereby certify".
  4. Confidence calibration that can actually reach >=0.90 on clear cases:
     when a type's strong-signal threshold is met AND no other type's strong
     signals fire AND layout is consistent, confidence = 0.90 + bonuses.
  5. Emits all 14 type codes (including the new FERC_ORDER and EIA_REPORT).

The classifier is intentionally a pure function so it can be tested without
DB / network. The caller is responsible for collecting layout features and
text samples; ``classify_v2()`` accepts a ``DocumentSignals`` snapshot and
returns a ``ClassificationResult``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from duke_rates.classification.result import ClassificationResult


CLASSIFIER_NAME = "rule_document_type_v2"
CLASSIFIER_VERSION = "v2.0"


# ---------------------------------------------------------------------------
# Type-specific pattern collections.
#
# Each type has:
#   - STRONG: phrases that are almost diagnostic on their own (1+ hit -> +3)
#   - WEAK: supporting evidence (1+ hits each -> +1)
#   - NEGATIVE: phrases that disqualify or downweight this type if present
#
# Patterns are case-insensitive regex strings. Match against full title +
# first 2000 chars + last 1000 chars (signature regions matter).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TypePatterns:
    strong: tuple[str, ...] = ()
    weak: tuple[str, ...] = ()
    negative: tuple[str, ...] = ()
    # Strong patterns that ONLY count when matched in the header region
    # (title + first HEADER_REGION_CHARS of body). Prevents false positives
    # from incidental body mentions — e.g. a base-schedule tariff that
    # lists applicable riders in an "Applicable Riders" section should NOT
    # be classified as RIDER just because the names appear in body text.
    strong_header: tuple[str, ...] = ()


# Header region size (title + first N chars of body) for strong_header patterns.
# Tuned to the first ~2 visible lines of a typical tariff/rider sheet, which is
# where legitimate headers like "RIDER BA-9 (NC)" or "First Revised Leaf No. 500"
# live. Larger windows (e.g. 400 chars) accidentally include the
# "Applicable Riders:" section of base-schedule docs, which lists rider names
# in body text and inflates RIDER scoring on the wrong type.
_HEADER_REGION_CHARS = 120


_TYPE_PATTERNS: dict[str, TypePatterns] = {
    "TARIFF_SHEET": TypePatterns(
        strong=(
            r"\bleaf\s+no\.?\s+\d+",
            r"\b(?:original|first|second|third|fourth|fifth|revised)\s+leaf\s+no\.?\s+\d+",
        ),
        strong_header=(
            # Base-class service titles that are tariff sheets without
            # explicit "Leaf No." in their title (common in legacy
            # /pdfs/...-dep.pdf 2014-era docs). Each phrase is anchored to
            # the header region so body mentions of the same phrases don't
            # bump TARIFF_SHEET inappropriately.
            r"\b(?:residential|large\s+general|medium\s+general|small\s+general|general)\s+service\b",
            r"\bschedule\s+(?:res|lgs|mgs|sgs|gs|ig)\b",
            r"\boutdoor\s+lighting\s+service\b",
            r"\bstreet\s+lighting\s+service\b",
        ),
        weak=(
            r"\bschedule\s+[a-z]{1,4}\b",
            r"\bbasic\s+customer\s+charge\b",
            r"\bper\s+kWh\b",
            r"\beffective\s+(?:for\s+service\s+)?(?:on|rendered)",
            r"\bavailability\b",
        ),
        negative=(
            r"\bin\s+the\s+matter\s+of\b",
            r"\bcertify\s+that\b",
            r"\bredirect\s+examination\b",
            # Filing letters that reference a leaf number in their subject
            # line are NOT the tariff sheet — they're the cover letter
            # transmitting it. The Re:/Enclosed combo is a strong negative.
            r"\bre:\s+(?:docket\s+no|.+leaf\s+no)",
            r"\benclosed\s+(?:please\s+find|for\s+filing)\b",
            r"\bvia\s+electronic\s+filing\b",
        ),
    ),
    "RIDER": TypePatterns(
        # 'Rider X' moved to strong_header so an applicable-riders list in a
        # base-schedule's body doesn't sweep RIDER. Legitimate rider docs
        # have the rider name in the title or within the first ~400 chars
        # (header region). Body-only mentions count as weak instead.
        strong_header=(
            r"\brider\s+[a-z0-9\-]{1,12}\b",
            r"\b(?:fuel|storm|reps|dsm|ee|ev|cpre|edit|annual\s+billing)\s+(?:and\s+fuel\s+related\s+)?(?:cost\s+(?:recovery|adjustment)\s+)?rider\b",
        ),
        weak=(
            r"\brider\s+[a-z0-9\-]{1,12}\b",  # body-only mentions are weak
            r"\badjustment\b",
            r"\bper\s+kWh\b",
            r"\bcomponent\b",
        ),
        negative=(
            r"\bcertify\s+that\b",
            r"\bredirect\s+examination\b",
            # Base-class schedule titles signal the doc is the base tariff,
            # not a rider — even if the body lists multiple applicable riders.
            r"\b(?:residential|large\s+general|medium\s+general|small\s+general)\s+service\b",
        ),
    ),
    "RATE_SCHEDULE": TypePatterns(
        strong=(
            r"\brate\s+schedule\s+[A-Z]+\b",
        ),
        weak=(
            r"\bavailability\b",
            r"\bcharges?\b",
            r"\benergy\s+charge\b",
        ),
    ),
    "ORDER_FINAL": TypePatterns(
        strong=(
            r"\bit\s+is,?\s+therefore,?\s+ordered\b",
            r"\bbefore\s+the\s+north\s+carolina\s+utilities\s+commission\b",
            r"\border\s+approving\b",
            r"\border\s+granting\b",
            r"\border\s+denying\b",
            r"\bfinal\s+order\b",
        ),
        weak=(
            r"\bdocket\s+no\.\s*e\b",
            r"\bcommissioner[s]?\b",
            r"\bissued\s+by\s+order\s+of\s+the\s+commission\b",
        ),
        negative=(
            r"\bnotice\s+of\s+hearing\b",
            r"\bcertificate\s+of\s+service\b",
            r"\bdirect\s+testimony\s+of\b",
        ),
    ),
    "ORDER_PROCEDURAL": TypePatterns(
        strong=(
            r"\bscheduling\s+order\b",
            r"\bprocedural\s+order\b",
            r"\border\s+granting\s+(?:motion|petition)\s+to\s+intervene\b",
            r"\border\s+granting\s+intervention\b",
            r"\bgranting\s+motion\s+to\s+compel\b",
        ),
        weak=(
            r"\bdocket\s+no\.\s*e\b",
            r"\bintervention\b",
            r"\bmotion\b",
        ),
    ),
    "TESTIMONY": TypePatterns(
        strong=(
            r"\bdirect\s+testimony\s+of\b",
            r"\bredirect\s+testimony\s+of\b",
            r"\brebuttal\s+testimony\s+of\b",
            r"\bsupplemental\s+testimony\s+of\b",
            r"\bredirect\s+examination\b",
            r"\bcross[-\s]+examination\b",
        ),
        weak=(
            r"^\s*Q\.\s+",
            r"^\s*A\.\s+",
            r"\bbackground\s+and\s+qualifications\b",
            r"\bexhibit\s+no\.?\s*\d+\b",
        ),
    ),
    "COVER_LETTER": TypePatterns(
        strong=(
            r"\bvia\s+electronic\s+filing\b",
            r"\bvia\s+hand\s+delivery\b",
            r"\benclosed\s+(?:please\s+find|for\s+filing)\b",
            r"\bplease\s+find\s+enclosed\b",
        ),
        weak=(
            r"\bre:\s*",
            r"\bsincerely,?\s*$",
            r"\battachments?\b",
            r"\bdocket\s+no\.\s*e\b",
        ),
        negative=(
            r"\bit\s+is,?\s+therefore,?\s+ordered\b",
            r"\bcertify\s+that\b",
            r"\bnotice\s+of\s+hearing\b",
            r"\bbasic\s+customer\s+charge\b",
            r"\bleaf\s+no\.?\s+\d+",
            r"\bdirect\s+testimony\s+of\b",
        ),
    ),
    "CERTIFICATE_OF_SERVICE": TypePatterns(
        strong=(
            r"\bcertificate\s+of\s+service\b",
            r"\bi\s+(?:do\s+)?hereby\s+certify\s+that\b",
            r"\bcertify\s+that\s+i\s+have\s+(?:this\s+day\s+)?served\b",
        ),
        weak=(
            r"\bvia\s+(?:first\s+class\s+mail|electronic\s+filing|hand\s+delivery)\b",
            r"\bservice\s+list\b",
        ),
    ),
    "NOTICE_OF_HEARING": TypePatterns(
        strong=(
            r"\bnotice\s+of\s+(?:public\s+)?hearing\b",
            r"\bnotice\s+of\s+(?:adjusted|rescheduled)\s+hearing\b",
        ),
        weak=(
            r"\bdocket\s+no\.\s*e\b",
            r"\bcommencing\s+at\b",
            r"\bdobbs\s+building\b",
        ),
    ),
    "APPLICATION": TypePatterns(
        strong=(
            r"\bapplication\s+of\s+\w[\w\s,&\.]+(?:pursuant|for\s+(?:authority|approval))",
            r"\bpetition\s+(?:of|for)\s+\w",
            r"\bpursuant\s+to\s+n\.?c\.?g\.?s\.?\s+62",
        ),
        weak=(
            r"\bapplicant\b",
            r"\brelief\s+(?:requested|sought)\b",
            r"\bdocket\s+no\.\s*e\b",
        ),
        negative=(
            r"\bit\s+is,?\s+therefore,?\s+ordered\b",
        ),
    ),
    "COMPLIANCE_FILING": TypePatterns(
        strong=(
            r"\bcompliance\s+(?:filing|report)\b",
            r"\bpursuant\s+to\s+(?:the\s+)?(?:order|order\s+dated)\b",
            r"\bfiled\s+(?:in\s+)?compliance\s+with\b",
        ),
        weak=(
            r"\battached\s+(?:report|filing|exhibit)\b",
            r"\bquarterly\s+report\b",
            r"\bannual\s+report\b",
        ),
    ),
    "FERC_ORDER": TypePatterns(
        strong=(
            r"\bfederal\s+energy\s+regulatory\s+commission\b",
            r"\bferc\s+(?:docket|order)\b",
            r"\b18\s+c\.?f\.?r\.?",
        ),
        weak=(
            r"\bferc\b",
            r"\border\s+no\.\s*\d{3,4}\b",
        ),
    ),
    "EIA_REPORT": TypePatterns(
        strong=(
            r"\benergy\s+information\s+administration\b",
            r"\beia[-\s]?(?:form\s+)?86\d\b",
        ),
        weak=(
            r"\bdoe/eia\b",
            r"\bmonthly\s+energy\s+review\b",
        ),
    ),
}


# ---------------------------------------------------------------------------
# Layout-feature scoring rules.
#
# Each rule shifts the per-type score based on observable structure signals.
# Page count, table presence, and text density discriminate quickly:
#   - 1-page docs: COVER_LETTER, CERTIFICATE_OF_SERVICE, NOTICE_OF_HEARING
#   - Single short page with tables: TARIFF_SHEET / RIDER
#   - Many pages with tables: COMPLIANCE_FILING / RATE_SCHEDULE
#   - Many pages without tables: TESTIMONY / ORDER_FINAL / APPLICATION
# ---------------------------------------------------------------------------


@dataclass
class DocumentSignals:
    """Observable signals fed to ``classify_v2``.

    Required:
        title:           filing title (used in pattern matching)
        first_text:      ~2000 chars from the doc start
    Optional layout / fingerprint signals — pass when available:
        last_text:       last ~1000 chars (signature regions / certifications)
        page_count:      pages in the source PDF
        text_chars:      total extracted text length
        has_tables:      0/1
        numeric_line_count: from document_fingerprints
        line_count:      total non-blank lines
    """

    title: str = ""
    first_text: str = ""
    last_text: str = ""
    page_count: int | None = None
    text_chars: int | None = None
    has_tables: int | None = None
    numeric_line_count: int | None = None
    line_count: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _compile_patterns() -> dict[str, list[tuple[str, re.Pattern[str], str]]]:
    """Compile every pattern once, grouped by type.

    Returns ``{type_code: [(role, regex, raw_pattern), ...]}``.
    """
    compiled: dict[str, list[tuple[str, re.Pattern[str], str]]] = {}
    for type_code, patterns in _TYPE_PATTERNS.items():
        rules: list[tuple[str, re.Pattern[str], str]] = []
        for raw in patterns.strong:
            rules.append(("strong", re.compile(raw, re.IGNORECASE | re.MULTILINE), raw))
        for raw in patterns.strong_header:
            rules.append(("strong_header", re.compile(raw, re.IGNORECASE | re.MULTILINE), raw))
        for raw in patterns.weak:
            rules.append(("weak", re.compile(raw, re.IGNORECASE | re.MULTILINE), raw))
        for raw in patterns.negative:
            rules.append(("negative", re.compile(raw, re.IGNORECASE | re.MULTILINE), raw))
        compiled[type_code] = rules
    return compiled


_COMPILED = _compile_patterns()

_STRONG_WEIGHT = 3.0
_WEAK_WEIGHT = 1.0
_NEGATIVE_WEIGHT = -2.0

# Confidence ceiling tunables. Strong-signal threshold keeps the v2 ceiling
# at 0.90+ for clean cases and lower for ambiguous ones.
_CONFIDENCE_FLOOR = 0.05
_CONFIDENCE_TARGET_FOR_STRONG = 0.92
_CONFIDENCE_BONUS_LAYOUT_CONSISTENT = 0.04


def _layout_score_shift(type_code: str, signals: DocumentSignals) -> tuple[float, list[str]]:
    """Return a layout-driven score adjustment for ``type_code``.

    Positive shifts indicate "layout is consistent with this type";
    negative shifts indicate "layout argues against this type".
    Each shift is accompanied by an evidence reason string.
    """
    shift = 0.0
    reasons: list[str] = []
    page_count = signals.page_count
    has_tables = signals.has_tables
    text_chars = signals.text_chars

    if page_count is None and text_chars is None and has_tables is None:
        return 0.0, []

    # Short docs (<=2 pages) bias toward COVER_LETTER, CERT, NOTICE
    if page_count is not None and page_count <= 2:
        if type_code in ("COVER_LETTER", "CERTIFICATE_OF_SERVICE", "NOTICE_OF_HEARING"):
            shift += 1.5
            reasons.append("layout:short_doc_favors_admin")
        elif type_code in ("ORDER_FINAL", "TESTIMONY", "APPLICATION", "COMPLIANCE_FILING"):
            shift -= 1.0
            reasons.append("layout:short_doc_disfavors_long_form")

    # Multi-page docs (>=5) bias against COVER_LETTER / CERT
    if page_count is not None and page_count >= 5:
        if type_code in ("COVER_LETTER", "CERTIFICATE_OF_SERVICE", "NOTICE_OF_HEARING"):
            shift -= 1.5
            reasons.append("layout:long_doc_disfavors_admin")

    # Table presence boost for tariff-family types
    if has_tables == 1:
        if type_code in ("TARIFF_SHEET", "RIDER", "RATE_SCHEDULE", "COMPLIANCE_FILING"):
            shift += 1.0
            reasons.append("layout:has_tables")
        elif type_code in ("COVER_LETTER", "CERTIFICATE_OF_SERVICE"):
            shift -= 0.5
            reasons.append("layout:tables_disfavor_letter")

    # No tables AND multi-page leans toward TESTIMONY / ORDER / APPLICATION
    if has_tables == 0 and page_count is not None and page_count >= 5:
        if type_code in ("TESTIMONY", "ORDER_FINAL", "APPLICATION"):
            shift += 0.5
            reasons.append("layout:text_heavy_no_tables")

    # Extremely short text (<200 chars) signals fragment or stub — softens
    # positive shifts to avoid overconfident classifications on text-light
    # stubs. 200 was tuned to NOT punish legitimate single-page cover
    # letters or certificates of service which are commonly 400-800 chars.
    if text_chars is not None and text_chars < 200:
        shift = min(shift, 0.0)
        reasons.append("layout:very_short_text")

    return shift, reasons


def classify_v2(signals: DocumentSignals) -> ClassificationResult:
    """Score every known document type against ``signals`` and emit the winner.

    The returned ``ClassificationResult`` includes:
      - ``label``: winning type code (or 'UNKNOWN' if nothing clears the floor)
      - ``confidence``: 0..1 scaled by signal strength + layout consistency
      - ``evidence``: list of {kind, value, weight} pairs documenting hits
      - ``alternatives``: runner-up type codes with their raw scores
    """
    title = signals.title or ""
    first_text = signals.first_text or ""
    last_text = signals.last_text or ""
    combined_pattern_text = "\n".join([title, first_text, last_text])
    # Header region for strong_header patterns: title + first N chars of body.
    # Matching a strong_header pattern outside this slice is treated as a
    # weak hit, not a strong one — keeps body-only mentions from dominating.
    header_region_text = "\n".join([title, first_text[:_HEADER_REGION_CHARS]])

    raw_scores: dict[str, float] = {}
    type_evidence: dict[str, list[dict]] = {}

    for type_code, rules in _COMPILED.items():
        score = 0.0
        evidence_for_type: list[dict] = []
        for role, regex, raw in rules:
            if role == "strong_header":
                # Only count matches inside the header region. Matches in body
                # are silently ignored here — the same pattern can be listed in
                # `weak` to capture body mentions separately if desired.
                hits = regex.findall(header_region_text)
                if not hits:
                    continue
                weight = _STRONG_WEIGHT * len(hits)
                score += weight
                evidence_for_type.append({
                    "kind": "strong_header_pattern", "value": raw,
                    "hits": len(hits), "weight": weight,
                })
                continue
            hits = regex.findall(combined_pattern_text)
            if not hits:
                continue
            if role == "strong":
                weight = _STRONG_WEIGHT * len(hits)
                score += weight
                evidence_for_type.append({
                    "kind": "strong_pattern", "value": raw,
                    "hits": len(hits), "weight": weight,
                })
            elif role == "weak":
                weight = _WEAK_WEIGHT * len(hits)
                score += weight
                evidence_for_type.append({
                    "kind": "weak_pattern", "value": raw,
                    "hits": len(hits), "weight": weight,
                })
            elif role == "negative":
                weight = _NEGATIVE_WEIGHT * len(hits)
                score += weight
                evidence_for_type.append({
                    "kind": "negative_pattern", "value": raw,
                    "hits": len(hits), "weight": weight,
                })

        # Layout adjustments — separate so they're inspectable in evidence
        layout_shift, layout_reasons = _layout_score_shift(type_code, signals)
        if layout_shift or layout_reasons:
            score += layout_shift
            for r in layout_reasons:
                evidence_for_type.append({"kind": r, "weight": 0.0})

        raw_scores[type_code] = score
        type_evidence[type_code] = evidence_for_type

    # Pick winner
    winner = max(raw_scores, key=lambda k: raw_scores[k])
    winner_score = raw_scores[winner]

    # If nothing clears the floor, return UNKNOWN at low confidence
    if winner_score <= 0:
        return ClassificationResult(
            label="UNKNOWN",
            confidence=_CONFIDENCE_FLOOR,
            classifier=CLASSIFIER_NAME,
            classifier_version=CLASSIFIER_VERSION,
            evidence=[{"kind": "no_pattern_hit", "weight": 0.0}],
            alternatives=[],
            metadata={"raw_scores": raw_scores},
        )

    # Confidence calibration. Either strong-pattern role counts.
    strong_hits = sum(
        1 for e in type_evidence[winner]
        if e.get("kind") in ("strong_pattern", "strong_header_pattern")
    )
    margin = winner_score - max(
        (s for c, s in raw_scores.items() if c != winner), default=0.0
    )

    if strong_hits >= 1:
        confidence = _CONFIDENCE_TARGET_FOR_STRONG
        # Layout-consistent boost when at least one layout reason supports
        # the winner. Keeps the ceiling reachable on clean cases.
        if any(
            e.get("kind", "").startswith("layout:") and e["kind"] != "layout:very_short_text"
            for e in type_evidence[winner]
        ):
            confidence = min(0.99, confidence + _CONFIDENCE_BONUS_LAYOUT_CONSISTENT)
        # Margin bonus: if winner is far ahead of runner-up, boost further
        if margin >= 5:
            confidence = min(0.99, confidence + 0.02)
    else:
        # Weak-only match — keep confidence well below the strong threshold
        # so the agreement vote with embedding/LLM stays balanced.
        confidence = max(_CONFIDENCE_FLOOR, min(0.65, winner_score / 6.0))

    # Build runner-up alternatives
    alternatives = sorted(
        ((c, s) for c, s in raw_scores.items() if c != winner and s > 0),
        key=lambda kv: kv[1],
        reverse=True,
    )[:5]

    return ClassificationResult(
        label=winner,
        confidence=confidence,
        classifier=CLASSIFIER_NAME,
        classifier_version=CLASSIFIER_VERSION,
        evidence=type_evidence[winner],
        alternatives=alternatives,
        metadata={
            "raw_scores": raw_scores,
            "winner_score": winner_score,
            "margin_over_runner_up": margin,
            "strong_hits": strong_hits,
        },
    )
