"""
Stage 4 & 8: Local result scoring and ideality classification.

Scores each SearchResult based on:
- Utility name match (title > snippet)
- Document-type signals (tariff, rider, schedule, sheet)
- Finality signals (superseding, canceling, approved, effective)
- Negative signals (redline, draft, testimony, exhibit, etc.)
- Co-occurrence bonuses (Duke + Progress, tariff + sheet, etc.)
- Query multiplicity bonus (found by multiple queries)
- Query quality weight from optimizer

Produces a ScoredResult with a local_score, ideality_score, and explanation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duke_rates.historical.ncuc.result_harvester import SearchResult

# ---------------------------------------------------------------------------
# Weighted term lists
# ---------------------------------------------------------------------------

# Positive utility signals — higher weight for more specific names
_UTILITY_SIGNALS: list[tuple[re.Pattern, float, str]] = [
    (re.compile(r"\bduke\s+energy\s+progress\b", re.I), 2.5, "DEP_exact"),
    (re.compile(r"\bprogress\s+energy\s+carolinas\b", re.I), 2.0, "PEC_exact"),
    (re.compile(r"\bduke\s+energy\s+carolinas\b", re.I), 2.0, "DEC_exact"),
    (re.compile(r"\bduke\s+progress\b", re.I), 1.5, "duke_progress"),
    (re.compile(r"\bDuke\s+Energy\b", re.I), 1.0, "duke_energy"),
    (re.compile(r"\bDEP\b"), 0.8, "DEP_abbrev"),
    (re.compile(r"\bDEC\b"), 0.8, "DEC_abbrev"),
    (re.compile(r"\bprogress\b", re.I), 0.5, "progress"),
]

# Positive document-type signals
_DOC_TYPE_SIGNALS: list[tuple[re.Pattern, float, str]] = [
    (re.compile(r"\btariff\s+sheet\b", re.I), 3.0, "tariff_sheet"),
    (re.compile(r"\brate\s+schedule\b", re.I), 2.5, "rate_schedule"),
    (re.compile(r"\bindex\s+of\s+(?:schedules|tariffs)\b", re.I), 3.5, "index_of_schedules"),
    (re.compile(r"\bschedule\s+of\s+rates\b", re.I), 3.0, "schedule_of_rates"),
    (re.compile(r"\btariff\b", re.I), 1.5, "tariff"),
    (re.compile(r"\brider\b", re.I), 1.5, "rider"),
    (re.compile(r"\bschedule\b", re.I), 1.0, "schedule"),
    (re.compile(r"\bsheet\b", re.I), 1.0, "sheet"),
    (re.compile(r"\brate\b", re.I), 0.8, "rate"),
    (re.compile(r"\bsurcharge\b", re.I), 0.8, "surcharge"),
    (re.compile(r"\badjustment\s+clause\b", re.I), 1.0, "adjustment_clause"),
    (re.compile(r"\bresidential\s+service\b", re.I), 1.5, "residential_service"),
    (re.compile(r"\bresidential\b", re.I), 0.8, "residential"),
]

# Positive finality signals
_FINALITY_SIGNALS: list[tuple[re.Pattern, float, str]] = [
    (re.compile(r"\bsuperseding\s+sheet\b", re.I), 3.0, "superseding_sheet"),
    (re.compile(r"\bcanceling\s+sheet\b", re.I), 3.0, "canceling_sheet"),
    (re.compile(r"\bsuperseding\b", re.I), 2.0, "superseding"),
    (re.compile(r"\bcanceling\b", re.I), 2.0, "canceling"),
    (re.compile(r"\bapproved\b", re.I), 1.5, "approved"),
    (re.compile(r"\beffective\s+for\s+service\b", re.I), 2.5, "effective_for_service"),
    (re.compile(r"\beffective\b", re.I), 1.0, "effective"),
    (re.compile(r"\bissued\s+by\s+authority\b", re.I), 2.5, "issued_by_authority"),
    (re.compile(r"\bfinal\s+order\b", re.I), 2.0, "final_order"),
    (re.compile(r"\bcommission\s+order\b", re.I), 1.5, "commission_order"),
    (re.compile(r"\border\s+approving\b", re.I), 2.0, "order_approving"),
    (re.compile(r"\bclean\b", re.I), 0.8, "clean"),
    (re.compile(r"\bfinal\b", re.I), 0.8, "final"),
    (re.compile(r"\bissued\b", re.I), 0.8, "issued"),
]

# Negative signals — penalize; strong signals may indicate non-ideal docs
_NEGATIVE_SIGNALS: list[tuple[re.Pattern, float, str]] = [
    (re.compile(r"\bpublic\s+hearings?\b", re.I), -5.0, "public_hearings_page"),
    (re.compile(r"\borders?\d{4}\.pdf\b", re.I), -4.0, "generic_orders_pdf"),
    (re.compile(r"\bncucrules\.pdf\b|\bchapter\d+\.pdf\b", re.I), -5.0, "generic_rules_pdf"),
    (re.compile(r"\blongrange\d+\.pdf\b", re.I), -4.0, "generic_report_pdf"),
    (re.compile(r"\bredline[d]?\b", re.I), -3.0, "redline"),
    (re.compile(r"\bmark-?up\b", re.I), -2.5, "markup"),
    (re.compile(r"\bdraft\b", re.I), -2.0, "draft"),
    (re.compile(r"\bdirect\s+testimony\b", re.I), -3.0, "direct_testimony"),
    (re.compile(r"\btestimony\b", re.I), -2.5, "testimony"),
    (re.compile(r"\bexhibit\b", re.I), -2.0, "exhibit"),
    (re.compile(r"\bdiscovery\b", re.I), -2.0, "discovery_filing"),
    (re.compile(r"\btranscript\b", re.I), -3.0, "transcript"),
    (re.compile(r"\bmotion\b", re.I), -1.5, "motion"),
    (re.compile(r"\bhearing\b", re.I), -1.5, "hearing"),
    (re.compile(r"\bnotice\s+of\s+hearing\b", re.I), -2.0, "notice_of_hearing"),
    (re.compile(r"\bcorrespondence\b", re.I), -2.0, "correspondence"),
    (re.compile(r"\bpetition\b", re.I), -1.0, "petition"),
    (re.compile(r"\bapplication\b", re.I), -0.8, "application"),
    (re.compile(r"\bstipulation\b", re.I), -1.0, "stipulation"),
    (re.compile(r"\bcomments?\b", re.I), -1.0, "comments"),
    (re.compile(r"\bprotective\s+order\b", re.I), -1.5, "protective_order"),
    (re.compile(r"\bcomprehensive\s+settlement\b", re.I), -1.0, "comprehensive_settlement"),
    (re.compile(r"\bprehearing\b", re.I), -2.0, "prehearing"),
]

# Co-occurrence combo bonuses — applied to combined title+snippet text
_COMBO_BONUSES: list[tuple[list[re.Pattern], float, str]] = [
    (
        [re.compile(r"\bduke\b", re.I), re.compile(r"\bprogress\b", re.I)],
        1.5, "combo_duke_progress",
    ),
    (
        [re.compile(r"\btariff\b", re.I), re.compile(r"\bsheet\b", re.I)],
        1.5, "combo_tariff_sheet",
    ),
    (
        [re.compile(r"\bcanceling\b", re.I), re.compile(r"\bsheet\b", re.I)],
        2.0, "combo_canceling_sheet",
    ),
    (
        [re.compile(r"\bsuperseding\b", re.I), re.compile(r"\bsheet\b", re.I)],
        2.0, "combo_superseding_sheet",
    ),
    (
        [re.compile(r"\brider\b", re.I), re.compile(r"\bschedule\b", re.I)],
        1.0, "combo_rider_schedule",
    ),
    (
        [re.compile(r"\bresidential\b", re.I), re.compile(r"\bservice\b", re.I)],
        1.2, "combo_residential_service",
    ),
    (
        [re.compile(r"\beffective\b", re.I), re.compile(r"\bservice\b", re.I)],
        1.0, "combo_effective_service",
    ),
    (
        [re.compile(r"\bduke\b", re.I), re.compile(r"\bcarolinas\b", re.I)],
        1.5, "combo_duke_carolinas",
    ),
    (
        [re.compile(r"\brate\b", re.I), re.compile(r"\bschedule\b", re.I)],
        1.2, "combo_rate_schedule",
    ),
    (
        [re.compile(r"\bindex\b", re.I), re.compile(r"\bschedule\b", re.I)],
        2.0, "combo_index_schedule",
    ),
    (
        [re.compile(r"\bcommission\b", re.I), re.compile(r"\border\b", re.I)],
        1.0, "combo_commission_order",
    ),
    (
        [
            re.compile(r"\bduke\b", re.I),
            re.compile(r"\bprogress\b", re.I),
            re.compile(r"\btariff\b", re.I),
        ],
        3.0, "combo_duke_progress_tariff",
    ),
    (
        [
            re.compile(r"\bduke\b", re.I),
            re.compile(r"\bprogress\b", re.I),
            re.compile(r"\brider\b", re.I),
        ],
        2.5, "combo_duke_progress_rider",
    ),
]

# Content-inspection signals (used when partial doc content is available)
_CONTENT_SIGNALS: list[tuple[re.Pattern, float, str]] = [
    (re.compile(r"\bbefore\s+the\s+north\s+carolina\s+utilities\s+commission\b", re.I), 5.0, "content_ncuc_header"),
    (re.compile(r"\bsheet\s+no[.\s]+\d+", re.I), 3.0, "content_sheet_number"),
    (re.compile(r"\bcanceling\s+sheet\s+no[.\s]+\d+", re.I), 4.0, "content_canceling_sheet_no"),
    (re.compile(r"\bsuperseding\s+sheet\s+no[.\s]+\d+", re.I), 4.0, "content_superseding_sheet_no"),
    (re.compile(r"\beffective\s+for\s+service\s+rendered\s+on\s+and\s+after\b", re.I), 5.0, "content_effective_date"),
    (re.compile(r"\bissued\s+by\s+authority\s+of\b", re.I), 4.0, "content_issued_by"),
    (re.compile(r"\bresidential\s+service\b", re.I), 2.0, "content_residential_service"),
    (re.compile(r"\brate\s+schedule\s+\d{3}\b", re.I), 3.0, "content_rate_schedule_code"),
    (re.compile(r"\btariff\b.*\bschedule\b", re.I), 2.0, "content_tariff_schedule"),
    (re.compile(r"\brider\b.*\bschedule\b", re.I), 2.0, "content_rider_schedule"),
]

# Weights: title matches count more than snippet
_TITLE_WEIGHT = 2.5
_SNIPPET_WEIGHT = 1.0
_URL_WEIGHT = 0.3


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ScoreExplanation:
    """Detailed breakdown of why a document was scored as it was."""
    title_signals: list[tuple[str, float]] = field(default_factory=list)   # (label, pts)
    snippet_signals: list[tuple[str, float]] = field(default_factory=list)
    url_signals: list[tuple[str, float]] = field(default_factory=list)
    combo_bonuses: list[tuple[str, float]] = field(default_factory=list)
    multiplicity_bonus: float = 0.0
    content_signals: list[tuple[str, float]] = field(default_factory=list)
    total_positive: float = 0.0
    total_negative: float = 0.0

    def narrative(self) -> str:
        parts = []
        pos_items = (
            [(f"title:{lbl}", pts) for lbl, pts in self.title_signals if pts > 0]
            + [(f"snippet:{lbl}", pts) for lbl, pts in self.snippet_signals if pts > 0]
            + [(f"url:{lbl}", pts) for lbl, pts in self.url_signals if pts > 0]
            + self.combo_bonuses
            + [(lbl, pts) for lbl, pts in self.content_signals if pts > 0]
        )
        if pos_items:
            pos_str = ", ".join(f"{lbl}(+{pts:.1f})" for lbl, pts in pos_items[:8])
            parts.append(f"positive: {pos_str}")

        neg_items = (
            [(f"title:{lbl}", pts) for lbl, pts in self.title_signals if pts < 0]
            + [(f"snippet:{lbl}", pts) for lbl, pts in self.snippet_signals if pts < 0]
        )
        if neg_items:
            neg_str = ", ".join(f"{lbl}({pts:.1f})" for lbl, pts in neg_items[:5])
            parts.append(f"negative: {neg_str}")

        if self.multiplicity_bonus > 0:
            parts.append(f"multi-query bonus: +{self.multiplicity_bonus:.1f}")

        return "; ".join(parts) or "no strong signals"


@dataclass
class IdealityAssessment:
    """Ideality classification for a search result."""
    is_ideal_candidate: bool
    ideal_reason: str
    nonideal_reason: str
    doc_type_guess: str           # tariff_sheet / rider / schedule / order / index / other
    likely_finality: str          # final / intermediate / redline / procedural / unknown
    confidence: float             # 0–1
    raw_ideality_score: float     # internal numeric score


@dataclass
class ScoredResult:
    """A SearchResult augmented with local scoring and ideality assessment."""
    result: "SearchResult"
    local_score: float
    ideality: IdealityAssessment
    explanation: ScoreExplanation
    content_bonus: float = 0.0    # Added after partial content inspection

    @property
    def combined_score(self) -> float:
        return self.local_score + self.content_bonus

    def explain(self) -> str:
        parts = [
            f"local_score={self.local_score:.2f}",
            f"ideality={self.ideality.doc_type_guess}/{self.ideality.likely_finality}",
            f"is_ideal={self.ideality.is_ideal_candidate}",
            f"conf={self.ideality.confidence:.2f}",
            self.explanation.narrative(),
        ]
        if self.result.found_by_queries and len(self.result.found_by_queries) > 1:
            parts.append(f"found_by={len(self.result.found_by_queries)}_queries")
        return " | ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# The scorer
# ---------------------------------------------------------------------------

class ResultScorer:
    """
    Scores a list of SearchResult objects locally, independently of NCUC
    search ranking.
    """

    def score_all(
        self,
        results: list["SearchResult"],
        query_usefulness: dict[str, float] | None = None,
    ) -> list[ScoredResult]:
        """
        Score all results and return sorted list (highest first).

        Args:
            results: Raw SearchResult objects from the harvester.
            query_usefulness: Optional dict mapping query_text → usefulness_score
                              from the QueryOptimizer.
        """
        scored = [
            self._score_one(r, query_usefulness=query_usefulness)
            for r in results
        ]
        scored.sort(key=lambda s: -s.combined_score)
        return scored

    def score_with_content(
        self,
        scored: ScoredResult,
        content_text: str,
    ) -> ScoredResult:
        """
        Add a content bonus to an already-scored result after partial content
        inspection.  Modifies content_bonus in-place.
        """
        bonus = 0.0
        for pat, weight, label in _CONTENT_SIGNALS:
            if pat.search(content_text):
                bonus += weight
                scored.explanation.content_signals.append((label, weight))
        scored.content_bonus = bonus
        # Recompute ideality with content bonus
        raw_ideality = scored.ideality.raw_ideality_score + bonus * 0.4
        scored.ideality = _assess_ideality(
            scored.result,
            raw_ideality=raw_ideality,
            local_score=scored.local_score + bonus,
            explanation=scored.explanation,
        )
        return scored

    def apply_hq_template_bonus(
        self,
        scored: ScoredResult,
        hq_signals: list[dict],
    ) -> ScoredResult:
        """
        Apply a bonus to *scored* when its title matches signals derived from
        known T1/T2 (high-quality) documents.

        Each entry in *hq_signals* should come from
        ``QueryBuilder._extract_hq_tokens(filing_title)`` and optionally include
        ``leaf_nos`` and ``rider_codes`` from the discovery record itself.

        Bonus applied:
          +2.5  if same leaf_no found in result title/snippet
          +2.0  if same rider_code found in result title/snippet
          +1.5  if same filing_type phrase found
          +1.0  if same year found
        Max total HQ bonus per result: 6.0

        Modifies ``scored.content_bonus`` and ``scored.explanation.content_signals``
        in-place.  Returns the same object.
        """
        title = scored.result.title or ""
        snippet = scored.result.snippet or ""
        combined = f"{title} {snippet}".lower()

        bonus = 0.0
        for sig in hq_signals:
            for leaf in sig.get("leaf_nos") or []:
                if re.search(r'\b' + re.escape(leaf) + r'\b', combined):
                    bonus += 2.5
                    scored.explanation.content_signals.append((f"hq_leaf_{leaf}", 2.5))
                    break  # one leaf match per signal is enough
            for rider in sig.get("rider_codes") or []:
                if re.search(r'\b' + re.escape(rider) + r'\b', combined, re.I):
                    bonus += 2.0
                    scored.explanation.content_signals.append((f"hq_rider_{rider}", 2.0))
                    break
            ft = sig.get("filing_type")
            if ft and ft.lower() in combined:
                bonus += 1.5
                scored.explanation.content_signals.append(("hq_filing_type", 1.5))
            year = sig.get("year_hint")
            if year and year in combined:
                bonus += 1.0
                scored.explanation.content_signals.append((f"hq_year_{year}", 1.0))

        capped_bonus = min(6.0, bonus)
        scored.content_bonus += capped_bonus
        raw_ideality = scored.ideality.raw_ideality_score + capped_bonus * 0.4
        scored.ideality = _assess_ideality(
            scored.result,
            raw_ideality=raw_ideality,
            local_score=scored.local_score + scored.content_bonus,
            explanation=scored.explanation,
        )
        return scored

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _score_one(
        self,
        result: "SearchResult",
        query_usefulness: dict[str, float] | None = None,
    ) -> ScoredResult:
        title = result.title or ""
        snippet = result.snippet or ""
        url = result.url or ""
        combined = f"{title} {snippet}"

        expl = ScoreExplanation()
        total = 0.0

        # --- Utility signals ---
        for pat, weight, label in _UTILITY_SIGNALS:
            if pat.search(title):
                pts = weight * _TITLE_WEIGHT
                expl.title_signals.append((label, pts))
                total += pts
            elif pat.search(snippet):
                pts = weight * _SNIPPET_WEIGHT
                expl.snippet_signals.append((label, pts))
                total += pts
            elif pat.search(url):
                pts = weight * _URL_WEIGHT
                expl.url_signals.append((label, pts))
                total += pts

        # --- Document-type signals ---
        for pat, weight, label in _DOC_TYPE_SIGNALS:
            if pat.search(title):
                pts = weight * _TITLE_WEIGHT
                expl.title_signals.append((label, pts))
                total += pts
            elif pat.search(snippet):
                pts = weight * _SNIPPET_WEIGHT
                expl.snippet_signals.append((label, pts))
                total += pts

        # --- Finality signals ---
        for pat, weight, label in _FINALITY_SIGNALS:
            if pat.search(title):
                pts = weight * _TITLE_WEIGHT
                expl.title_signals.append((label, pts))
                total += pts
            elif pat.search(snippet):
                pts = weight * _SNIPPET_WEIGHT
                expl.snippet_signals.append((label, pts))
                total += pts

        # --- Negative signals ---
        for pat, weight, label in _NEGATIVE_SIGNALS:
            if pat.search(title):
                pts = weight * _TITLE_WEIGHT
                expl.title_signals.append((label, pts))
                total += pts
            elif pat.search(snippet):
                pts = weight * _SNIPPET_WEIGHT
                expl.snippet_signals.append((label, pts))
                total += pts

        # --- Combo bonuses (applied to combined text) ---
        for patterns, bonus, label in _COMBO_BONUSES:
            if all(p.search(combined) for p in patterns):
                expl.combo_bonuses.append((label, bonus))
                total += bonus

        # --- Schedule/rider code bonus ---
        if result.extracted_schedule_codes or result.extracted_rider_codes:
            code_bonus = min(2.0, len(result.extracted_schedule_codes) * 0.5 + len(result.extracted_rider_codes) * 0.5)
            expl.combo_bonuses.append(("schedule_rider_codes", code_bonus))
            total += code_bonus

        # --- Query-target match bonus ---
        target_bonus = 0.0
        if result.schedule_code_hint:
            hinted = result.schedule_code_hint.upper()
            query_target_hit = (
                hinted in {code.upper() for code in result.extracted_schedule_codes}
                or bool(re.search(rf"\b(?:schedule|rate\s+schedule)\s*{re.escape(hinted)}\b", combined, re.I))
                or bool(re.search(rf"\b{re.escape(hinted)}\b", url, re.I))
            )
            if query_target_hit:
                target_bonus += 4.0
            else:
                target_bonus -= 6.0
        if result.rider_code_hint:
            hinted = result.rider_code_hint.upper()
            query_target_hit = (
                hinted in {code.upper() for code in result.extracted_rider_codes}
                or bool(re.search(rf"\brider\s+{re.escape(hinted)}\b", combined, re.I))
                or bool(re.search(rf"\b{re.escape(hinted)}\b", url, re.I))
            )
            if query_target_hit:
                target_bonus += 3.5
            else:
                target_bonus -= 4.0
        if target_bonus:
            expl.combo_bonuses.append(("query_target_match", target_bonus))
            total += target_bonus

        # --- URL-shape priors ---
        if re.search(r"\.pdf(?:$|\?)", url, re.I):
            expl.combo_bonuses.append(("pdf_url", 1.5))
            total += 1.5
        elif re.search(r"\.html?(?:$|\?)", url, re.I):
            expl.combo_bonuses.append(("html_url", -1.5))
            total -= 1.5

        # --- E-2 docket bonus ---
        if result.docket_number and result.docket_number.startswith("E-2"):
            expl.combo_bonuses.append(("E2_docket", 1.5))
            total += 1.5

        # --- Query multiplicity bonus ---
        n_queries = len(result.found_by_queries)
        if n_queries > 1:
            multi_bonus = min(3.0, (n_queries - 1) * 0.8)
            expl.multiplicity_bonus = multi_bonus
            total += multi_bonus

        # --- Query usefulness weight ---
        if query_usefulness:
            usefulness = query_usefulness.get(result.source_query, 0.0)
            if usefulness > 0:
                q_bonus = min(2.0, usefulness * 0.3)
                expl.combo_bonuses.append((f"query_usefulness({usefulness:.2f})", q_bonus))
                total += q_bonus

        expl.total_positive = max(0.0, total)
        expl.total_negative = min(0.0, total)

        local_score = max(0.0, total)

        # Compute ideality
        ideality = _assess_ideality(
            result,
            raw_ideality=total,
            local_score=local_score,
            explanation=expl,
        )

        return ScoredResult(
            result=result,
            local_score=local_score,
            ideality=ideality,
            explanation=expl,
        )


# ---------------------------------------------------------------------------
# Ideality classifier (deterministic rules)
# ---------------------------------------------------------------------------

def _assess_ideality(
    result: "SearchResult",
    raw_ideality: float,
    local_score: float,
    explanation: ScoreExplanation,
) -> IdealityAssessment:
    """
    Rule-based ideality classification.  Returns an IdealityAssessment.
    """
    title = (result.title or "").lower()
    snippet = (result.snippet or "").lower()
    combined = f"{title} {snippet}"

    # --- Detect strong negative signals first ---
    has_redline = bool(re.search(r"\bredline[d]?\b|\bmark-?up\b", combined, re.I))
    has_testimony = bool(re.search(r"\btestimony\b|\bdirect\s+testimony\b", combined, re.I))
    has_draft = bool(re.search(r"\bdraft\b", combined, re.I))
    has_exhibit = bool(re.search(r"\bexhibit\b", combined, re.I))
    has_discovery = bool(re.search(r"\bdiscovery\b", combined, re.I))
    has_transcript = bool(re.search(r"\btranscript\b", combined, re.I))
    has_motion = bool(re.search(r"\bmotion\b|\bhearing\b", combined, re.I))

    # --- Detect positive signals ---
    has_tariff_sheet = bool(re.search(r"\btariff\s+sheet\b", combined, re.I))
    has_rate_schedule = bool(re.search(r"\brate\s+schedule\b|\bschedule\s+of\s+rates\b", combined, re.I))
    has_index = bool(re.search(r"\bindex\s+of\s+(?:schedules|tariffs)\b", combined, re.I))
    has_rider = bool(re.search(r"\brider\b", combined, re.I))
    has_order_approving = bool(re.search(r"\border\s+approving\b|\bfinal\s+order\b|\bcommission\s+order\b", combined, re.I))
    has_superseding = bool(re.search(r"\bsuperseding\b|\bcanceling\b", combined, re.I))
    has_effective = bool(re.search(r"\beffective\s+for\s+service\b|\beffective\b", combined, re.I))
    has_approved = bool(re.search(r"\bapproved\b|\bfinal\b", combined, re.I))
    has_ncuc_label = bool(re.search(r"\bncuc\b|\bnorth\s+carolina\s+utilities\s+commission\b", combined, re.I))

    # --- Classify document type ---
    if has_index:
        doc_type_guess = "index_of_schedules"
    elif has_tariff_sheet:
        doc_type_guess = "tariff_sheet"
    elif has_rate_schedule:
        doc_type_guess = "rate_schedule"
    elif has_rider:
        doc_type_guess = "rider"
    elif has_order_approving:
        doc_type_guess = "order"
    elif has_testimony:
        doc_type_guess = "testimony"
    elif has_exhibit:
        doc_type_guess = "exhibit"
    else:
        doc_type_guess = "other"

    # --- Classify finality ---
    if has_redline:
        likely_finality = "redline"
    elif has_draft:
        likely_finality = "draft"
    elif has_testimony or has_exhibit or has_discovery or has_transcript:
        likely_finality = "procedural"
    elif has_superseding and (has_tariff_sheet or has_rate_schedule):
        likely_finality = "final"
    elif has_approved and (has_tariff_sheet or has_rate_schedule or has_rider):
        likely_finality = "final"
    elif has_effective and (has_tariff_sheet or has_rate_schedule):
        likely_finality = "final"
    elif has_order_approving:
        likely_finality = "final"
    elif has_effective:
        likely_finality = "intermediate"
    else:
        likely_finality = "unknown"

    # --- Compute ideality ---
    is_ideal = False
    ideal_reason = ""
    nonideal_reason = ""
    ideality_score = raw_ideality

    # Hard disqualifiers
    if has_redline or has_draft:
        is_ideal = False
        nonideal_reason = "redline/draft" if has_redline else "draft filing"
        ideality_score -= 5.0
    elif has_testimony and not has_tariff_sheet:
        is_ideal = False
        nonideal_reason = "testimony without tariff sheet"
        ideality_score -= 3.0
    elif has_transcript:
        is_ideal = False
        nonideal_reason = "transcript"
        ideality_score -= 4.0
    # Positive qualifiers
    elif has_index:
        is_ideal = True
        ideal_reason = "index of schedules — high-value document"
    elif has_tariff_sheet and (has_superseding or has_approved or has_effective):
        is_ideal = True
        ideal_reason = f"tariff sheet with finality signal ({likely_finality})"
    elif has_rate_schedule and (has_superseding or has_approved or has_effective):
        is_ideal = True
        ideal_reason = f"rate schedule with finality signal ({likely_finality})"
    elif has_rider and (has_approved or has_effective):
        is_ideal = True
        ideal_reason = f"rider with finality signal ({likely_finality})"
    elif has_order_approving:
        is_ideal = True
        ideal_reason = "order approving rates/tariffs"
    elif local_score >= 12.0:
        is_ideal = True
        ideal_reason = f"high local score ({local_score:.1f}) with multiple positive signals"
    else:
        nonideal_reason = "insufficient finality or document-type signals"

    # --- Confidence ---
    signal_count = sum([
        has_tariff_sheet, has_rate_schedule, has_index, has_rider,
        has_order_approving, has_superseding, has_approved, has_effective,
        has_ncuc_label,
    ])
    negative_count = sum([
        has_redline, has_testimony, has_draft, has_exhibit,
        has_discovery, has_transcript, has_motion,
    ])
    confidence = min(1.0, max(0.0, (signal_count * 0.12) - (negative_count * 0.1)))

    return IdealityAssessment(
        is_ideal_candidate=is_ideal,
        ideal_reason=ideal_reason,
        nonideal_reason=nonideal_reason,
        doc_type_guess=doc_type_guess,
        likely_finality=likely_finality,
        confidence=confidence,
        raw_ideality_score=ideality_score,
    )
