import re
from typing import Dict, List, Optional
from duke_rates.classification.result import ClassificationResult
from duke_rates.models.pipeline import TariffSpan

# Bumped when scoring weights or evidence categories change. Persisted with
# every ClassificationResult so disagreement reports can filter by version.
FAMILY_MATCHER_CLASSIFIER = "find_best_family_for_span"
FAMILY_MATCHER_VERSION = "v1"

# Maximum theoretical score this scorer can produce (sum of all positive
# evidence weights). Used to normalize raw score → 0..1 confidence.
# Keep in sync with score_span_against_family below.
#   explicit_leaf_hit (40)
# + schedule_code_hit (20)
# + heading_alias_similarity (cap 20)
# + tariff_vocab_density (8)
# + summary_sheet_bonus (30)
_MAX_FAMILY_SCORE = 40 + 20 + 20 + 8 + 30  # = 118


_CODE_NORMALIZATION_REPLACEMENTS = (
    ("ADJRDR", "ADJUSTMENTRIDER"),
    ("ADJ", "ADJUSTMENT"),
    ("RDR", "RIDER"),
    ("SCH", "SCHEDULE"),
    ("SVC", "SERVICE"),
    ("SRVC", "SERVICE"),
)


def _normalize_family_token(text: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", "", (text or "").upper())
    token = re.sub(r"[^A-Z0-9]", "", cleaned)
    for old, new in _CODE_NORMALIZATION_REPLACEMENTS:
        token = token.replace(old, new)
    return token


def _alias_matches_text(alias: str, texts: list[str]) -> bool:
    alias_upper = re.sub(r"\s+", " ", (alias or "").upper()).strip()
    if not alias_upper:
        return False

    boundary_pattern = re.compile(
        rf"(?<![A-Z0-9]){re.escape(alias_upper)}(?![A-Z0-9])"
    )
    normalized_texts = [re.sub(r"\s+", " ", (text or "").upper()) for text in texts]
    if any(boundary_pattern.search(text) for text in normalized_texts):
        return True

    normalized_alias = _normalize_family_token(alias_upper)
    if len(normalized_alias) < 10:
        return False

    condensed_texts = [_normalize_family_token(text) for text in texts]
    return any(normalized_alias in text for text in condensed_texts)


def _span_has_summary_heading(span: TariffSpan) -> bool:
    texts = [*span.header_footer_snippets, *span.extracted_schedule_titles]
    normalized_texts = [re.sub(r"\s+", " ", (text or "").upper()).strip() for text in texts]
    return any(
        "SUMMARY OF RIDER ADJUSTMENTS" in text or "SUMMARY OF RIDERS" in text
        for text in normalized_texts
    )


def _family_is_summary_family(
    family_aliases: list[str],
    target_leaf_no: str | None,
    target_code: str | None,
) -> bool:
    normalized_code = _normalize_family_token(target_code or "")
    if normalized_code in {"SUMMARYOFRIDERS", "SUMMARYOFRIDERADJUSTMENTS"}:
        return True

    normalized_aliases = [re.sub(r"\s+", " ", (alias or "").upper()).strip() for alias in family_aliases]
    if any("SUMMARY OF RIDER ADJUSTMENTS" in alias or "SUMMARY OF RIDERS" in alias for alias in normalized_aliases):
        return True

    if target_leaf_no in {"600", "99"} and any("SUMMARY" in alias for alias in normalized_aliases):
        return True

    return False

def score_span_against_family(span: TariffSpan, family_id: str, family_aliases: List[str], target_leaf_no: Optional[str] = None, target_code: Optional[str] = None) -> float:
    """
    Score a TariffSpan against a specific family definition using a multi-evidence point system.
    Updates span.evidence_score_breakdown and returns the total score.
    
    Expected logic:
    - explicit leaf hit: +40
    - heading alias similarity: +20
    - tariff vocabulary density: base score multiplier or fixed points
    - ambiguous code penalty: -10 if context lacks tariff features
    - procedural-doc penalty: -20
    """
    score = 0.0
    breakdown = {}
    
    # 1. Explicit Leaf Number match
    if target_leaf_no and target_leaf_no in span.extracted_leaf_nos:
        score += 40.0
        breakdown["explicit_leaf_hit"] = 40.0
        
    # 2. Schedule Code Hit
    normalized_titles = {_normalize_family_token(title) for title in span.extracted_schedule_titles}
    normalized_target_code = _normalize_family_token(target_code or "")
    if target_code and (
        target_code in span.extracted_schedule_titles
        or (normalized_target_code and normalized_target_code in normalized_titles)
    ):
        # short ambiguous codes need protection
        if len(target_code) <= 3 and span.doc_type != "tariff":
            score -= 10.0
            breakdown["ambiguous_code_penalty"] = -10.0
        else:
            score += 20.0
            breakdown["schedule_code_hit"] = 20.0
            
    # 3. Document Type Context
    if span.doc_type == "procedural":
        score -= 20.0
        breakdown["procedural_doc_penalty"] = -20.0
    elif span.doc_type == "Unknown":
        score -= 5.0
        breakdown["unknown_doc_penalty"] = -5.0
        
    # 4. Heading Alias Similarity 
    # (Simplified string match for this implementation, can be expanded to fuzz/embeddings)
    alias_hits = 0
    alias_texts = [*span.header_footer_snippets, *span.extracted_schedule_titles]
    for alias in family_aliases:
        if _alias_matches_text(alias, alias_texts):
            alias_hits += 1
            
    if alias_hits > 0:
        val = min(20.0, alias_hits * 10.0)
        score += val
        breakdown["heading_alias_similarity"] = val
        
    # 5. Tariff vocabulary bonus
    # Since we lack the density here (it is stored in PageEvidence), we assume doc_type="tariff" gives a baseline bonus
    if span.doc_type == "tariff":
        score += 8.0
        breakdown["tariff_vocab_density"] = 8.0

    # 6. Summary-sheet preference
    # Summary-of-rider pages often contain specific rider names like "Rider BA"
    # plus multiple leaf numbers, which can otherwise cause a specific rider
    # family to outrank the intended rider-summary family.
    if _span_has_summary_heading(span):
        if _family_is_summary_family(family_aliases, target_leaf_no, target_code):
            score += 30.0
            breakdown["summary_sheet_bonus"] = 30.0
        else:
            score -= 15.0
            breakdown["summary_sheet_mismatch_penalty"] = -15.0

    span.evidence_score_breakdown[family_id] = breakdown
    return score


def classify_span_against_families(
    span: TariffSpan,
    supported_families: List[Dict],
) -> Optional[ClassificationResult]:
    """Score every candidate family for ``span`` and return a ClassificationResult.

    Returns None when no family scored above the minimum threshold (20.0).
    The result includes the chosen label, its normalized confidence, the
    score breakdown that produced it, and the runner-up alternatives —
    everything an audit/disagreement report needs.

    The caller can persist the result via
    ``duke_rates.classification.record_classification``. The thin wrapper
    :func:`find_best_family_for_span` returns just the label for legacy
    callers that don't yet record classifications.
    """
    all_scores: dict[str, float] = {}
    best_family: Optional[str] = None
    best_score: float = 0.0

    for fam in supported_families:
        family_id = fam.get("family_id")
        if not family_id:
            continue
        score = score_span_against_family(
            span=span,
            family_id=family_id,
            family_aliases=fam.get("aliases", []),
            target_leaf_no=fam.get("leaf_no"),
            target_code=fam.get("code"),
        )
        all_scores[family_id] = score
        if score > best_score and score >= 20.0:
            best_score = score
            best_family = family_id

    if not best_family:
        return None

    span.confidence = best_score
    breakdown = span.evidence_score_breakdown.get(best_family, {})
    return ClassificationResult.from_score_breakdown(
        label=best_family,
        score=best_score,
        score_breakdown=breakdown,
        all_scores=all_scores,
        classifier=FAMILY_MATCHER_CLASSIFIER,
        classifier_version=FAMILY_MATCHER_VERSION,
        score_to_confidence=_MAX_FAMILY_SCORE,
    )


def find_best_family_for_span(span: TariffSpan, supported_families: List[Dict]) -> Optional[str]:
    """Legacy thin wrapper — returns just the family_id label, no observability.

    Prefer :func:`classify_span_against_families` for new code so the
    classification result can be persisted and audited.
    """
    result = classify_span_against_families(span, supported_families)
    return result.label if result else None
