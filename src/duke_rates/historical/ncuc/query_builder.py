"""
Stage 2: Safe query builder.

Generates candidate search queries using only syntax patterns that have been
empirically validated by the SearchCompatibilityHarness.  When no compat
report is available, it falls back to the safest possible patterns (bare
space-separated multi-term queries — "implicit AND").

The builder works from controlled vocabulary buckets and generates multiple
narrower searches rather than relying on a single complex Boolean string.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.historical.ncuc.family_search_terms import all_profiles
from duke_rates.historical.ncuc.search_compat import (
    SearchCompatibilityHarness,
    ProbeResult,
)
from duke_rates.historical.ncuc.query_syntax import sanitize_ncuc_query
from duke_rates.models.ncuc import NcucSearchQuery

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Controlled vocabulary buckets
# ---------------------------------------------------------------------------

# Utility name tokens — all variants Duke Energy uses in NC filings
UTILITY_TERMS: list[str] = [
    "Duke Energy Progress",
    "Progress Energy Carolinas",
    "Duke Energy Carolinas",
    "Duke Energy",
    "DEP",
    "DEC",
]

# Core document-type terms — what we're hunting for
DOCUMENT_TERMS: list[str] = [
    "tariff",
    "rider",
    "schedule",
    "rate",
    "sheet",
    "rate schedule",
    "tariff sheet",
    "index of schedules",
    "index of tariffs",
    "rate schedule index",
]

# Finality / official status signals
FINALITY_TERMS: list[str] = [
    "approved",
    "final",
    "issued",
    "effective",
    "superseding",
    "canceling",
    "clean",
    "filed",
    "order",
]

# Non-final / noisy document signals (used for scoring, not query generation)
NOISE_TERMS: list[str] = [
    "redline",
    "redlined",
    "markup",
    "draft",
    "testimony",
    "direct testimony",
    "exhibit",
    "discovery",
    "motion",
    "hearing",
    "transcript",
    "correspondence",
    "attachment",
]

# Residential-specific signals
RESIDENTIAL_TERMS: list[str] = [
    "residential service",
    "residential",
    "residential rate",
]

# Schedule code targets for Duke Energy Progress NC — derived from family_search_terms.py
# Kept as a flat list for backward compatibility with targeted CLI options.
DEP_SCHEDULE_CODES: list[str] = sorted(
    {p.leaf for p in all_profiles() if p.family_key.startswith("nc-progress")},
    key=lambda x: int(x),
)

# Rider/schedule code aliases used for query generation — short codes only.
# Full natural-language phrases are handled by _build_family_profile_queries().
DEP_RIDER_NAMES: list[str] = sorted(
    {
        alias
        for p in all_profiles()
        if p.family_key.startswith("nc-progress")
        for alias in p.aliases
        # Keep only short single-token codes (no spaces, ≤12 chars, uppercase)
        if alias.isupper() and " " not in alias and len(alias) <= 12 and not alias.isdigit()
    }
)

# Duke Energy Carolinas docket series
DEC_SCHEDULE_CODES: list[str] = [
    "101", "104", "110", "130", "140",
    "200", "210", "220", "230",
    "300", "310", "320",
]


# ---------------------------------------------------------------------------
# Query template definitions
# ---------------------------------------------------------------------------

@dataclass
class QuerySpec:
    """A generated query with metadata for scoring and filtering."""
    query_text: str
    template_name: str          # Which template pattern produced this
    utility_hint: str | None    # Which utility this query targets
    doc_type_hint: str | None   # Which doc type this targets (tariff/rider/etc)
    schedule_code_hint: str | None = None
    rider_code_hint: str | None = None
    family_key_hint: str | None = None
    priority: float = 1.0       # Higher = prefer first
    notes: list[str] = field(default_factory=list)

    def to_ncuc_query(self) -> NcucSearchQuery:
        return NcucSearchQuery(
            query_text=self.query_text,
            schedule_code_hint=self.schedule_code_hint,
            rider_code_hint=self.rider_code_hint,
            family_key_hint=self.family_key_hint,
        )


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

class QueryBuilder:
    """
    Generates a prioritized list of safe search queries.

    Reads the compat report to determine which syntax families are available,
    then generates queries from the vocabulary buckets.  Always prefers
    simpler patterns when complex ones failed probing.
    """

    def __init__(
        self,
        settings: Settings,
        compat_harness: SearchCompatibilityHarness | None = None,
    ):
        self.settings = settings
        self._harness = compat_harness or SearchCompatibilityHarness(settings)
        self._safe_types: set[str] = set()
        self._loaded = False

    def _ensure_compat_loaded(self) -> None:
        if self._loaded:
            return
        self._safe_types = self._harness.load_safe_pattern_types()
        if not self._safe_types:
            # No compat report yet — default to safest known subset
            logger.warning(
                "No compat report loaded; defaulting to single_term and two_term patterns only"
            )
            self._safe_types = {"single_term", "two_term"}
        self._loaded = True

    def _can_use(self, pattern_type: str) -> bool:
        self._ensure_compat_loaded()
        return pattern_type in self._safe_types

    def _can_use_quoted(self) -> bool:
        return self._can_use("quoted_phrase")

    def _can_use_boolean_and(self) -> bool:
        return self._can_use("boolean_and")

    def _can_use_boolean_or(self) -> bool:
        return self._can_use("boolean_or")

    def _term(self, text: str) -> str:
        """
        Return a term suitable for the safe syntax level.
        If quoted_phrase is safe and the term contains spaces, wrap in quotes.
        Otherwise return bare (will be treated as implicit AND).
        """
        candidate = f'"{text}"' if " " in text and self._can_use_quoted() else text
        return sanitize_ncuc_query(candidate, safe_pattern_types=self._safe_types)

    def _query(self, text: str) -> str:
        return sanitize_ncuc_query(text, safe_pattern_types=self._safe_types)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_dep_queries(self) -> list[QuerySpec]:
        """
        Generate a comprehensive set of queries targeting Duke Energy Progress NC
        tariff/rider/schedule documents.
        """
        self._ensure_compat_loaded()
        queries: list[QuerySpec] = []

        # 1. Utility × document-type cross product (two_term safe baseline)
        for util in ["Duke Energy Progress", "Progress Energy Carolinas", "Duke Progress"]:
            util_t = self._term(util)
            for doc in ["tariff", "rider", "schedule", "rate", "sheet"]:
                q = self._query(f"{util_t} {doc}")
                queries.append(QuerySpec(
                    query_text=q,
                    template_name="utility_x_doctype",
                    utility_hint=util,
                    doc_type_hint=doc,
                    priority=2.0,
                ))

        # 2. Utility × finality cross product
        for util in ["Duke Energy Progress", "Progress Energy Carolinas"]:
            util_t = self._term(util)
            for fin in ["approved", "effective", "superseding", "canceling"]:
                q = self._query(f"{util_t} {fin}")
                queries.append(QuerySpec(
                    query_text=q,
                    template_name="utility_x_finality",
                    utility_hint=util,
                    doc_type_hint=None,
                    priority=1.5,
                ))

        # 3. Utility × residential
        for util in ["Duke Energy Progress", "Progress Energy Carolinas"]:
            util_t = self._term(util)
            res_t = self._term("residential service")
            q = self._query(f"{util_t} {res_t}")
            queries.append(QuerySpec(
                query_text=q,
                template_name="utility_x_residential",
                utility_hint=util,
                doc_type_hint="residential service",
                priority=1.8,
            ))

        # 4. Schedule code queries (with utility)
        for code in DEP_SCHEDULE_CODES:
            for util in ["Duke Energy Progress", "Progress Energy Carolinas"]:
                util_t = self._term(util)
                # "schedule NNN" or "schedule" + code
                code_phrase = f"schedule {code}"
                code_t = self._term(code_phrase)
                q = self._query(f"{util_t} {code_t}")
                queries.append(QuerySpec(
                    query_text=q,
                    template_name="utility_x_schedule_code",
                    utility_hint=util,
                    doc_type_hint="schedule",
                    schedule_code_hint=code,
                    family_key_hint=code,
                    priority=2.5,
                ))

        # 5. Rider name queries
        for rider in DEP_RIDER_NAMES:
            rider_t = self._term(rider)
            for util in ["Duke Energy Progress", "Progress Energy Carolinas"]:
                util_t = self._term(util)
                q = self._query(f"{util_t} rider {rider_t}")
                queries.append(QuerySpec(
                    query_text=q,
                    template_name="utility_x_rider_name",
                    utility_hint=util,
                    doc_type_hint="rider",
                    rider_code_hint=rider,
                    family_key_hint=rider,
                    priority=2.3,
                ))

        # 6. Tariff-sheet combination (high-value signal)
        ts_t = self._term("tariff sheet")
        for util in ["Duke Energy Progress", "Progress Energy Carolinas"]:
            util_t = self._term(util)
            q = self._query(f"{util_t} {ts_t}")
            queries.append(QuerySpec(
                query_text=q,
                template_name="utility_x_tariff_sheet",
                utility_hint=util,
                doc_type_hint="tariff sheet",
                priority=2.8,
            ))

        # 7. Index-of-schedules queries
        idx_t = self._term("index of schedules")
        for util in ["Duke Energy Progress", "Progress Energy Carolinas"]:
            util_t = self._term(util)
            q = self._query(f"{util_t} {idx_t}")
            queries.append(QuerySpec(
                query_text=q,
                template_name="utility_x_index_schedules",
                utility_hint=util,
                doc_type_hint="index",
                priority=3.0,
            ))

        # 8. Superseding/canceling sheet (strong finality signal)
        for fin_phrase in ["superseding sheet", "canceling sheet"]:
            fin_t = self._term(fin_phrase)
            for util in ["Duke Energy Progress", "Progress Energy Carolinas"]:
                util_t = self._term(util)
                q = self._query(f"{util_t} {fin_t}")
                queries.append(QuerySpec(
                    query_text=q,
                    template_name="utility_x_canceling_superseding",
                    utility_hint=util,
                    doc_type_hint="tariff",
                    priority=2.9,
                ))

        # 9. Order approving rates (strong official document signal)
        order_t = self._term("order approving")
        for util in ["Duke Energy Progress", "Progress Energy Carolinas"]:
            util_t = self._term(util)
            q = self._query(f"{util_t} {order_t} rate")
            queries.append(QuerySpec(
                query_text=q,
                template_name="utility_x_order_approving_rate",
                utility_hint=util,
                doc_type_hint="order",
                priority=1.6,
            ))

        # 10. Duke Energy Carolinas coverage
        for util in ["Duke Energy Carolinas"]:
            util_t = self._term(util)
            for doc in ["tariff", "rider", "schedule", "rate"]:
                q = self._query(f"{util_t} {doc}")
                queries.append(QuerySpec(
                    query_text=q,
                    template_name="dec_utility_x_doctype",
                    utility_hint=util,
                    doc_type_hint=doc,
                    priority=1.5,
                ))

        # 11. Per-family natural-language queries from family_search_terms.py
        queries.extend(self._build_family_profile_queries())

        # Deduplicate by query text, keep highest priority
        seen: dict[str, QuerySpec] = {}
        for qs in queries:
            key = qs.query_text.strip().lower()
            if key not in seen or qs.priority > seen[key].priority:
                seen[key] = qs

        result = sorted(seen.values(), key=lambda x: -x.priority)
        logger.info("QueryBuilder generated %d unique queries for DEP", len(result))
        return result

    def _build_family_profile_queries(self) -> list[QuerySpec]:
        """
        Generate one QuerySpec per ncuc_query string in each FamilySearchProfile.

        These queries use the natural language that actually appears in NCUC
        filing titles and descriptions — e.g. "earnings sharing mechanism" for
        leaf-609 rather than just "schedule 609" — giving the harvester much
        better signal on obscure or renamed riders.

        Priority is set to 2.8 so these rank just below tariff-sheet and
        index-of-schedules queries but above generic utility × doc-type pairs.
        """
        self._ensure_compat_loaded()
        queries: list[QuerySpec] = []
        for profile in all_profiles():
            if not profile.family_key.startswith("nc-progress"):
                continue
            for raw_query in profile.ncuc_queries:
                q = self._query(raw_query)
                queries.append(QuerySpec(
                    query_text=q,
                    template_name="family_profile_query",
                    utility_hint=None,
                    doc_type_hint=None,
                    schedule_code_hint=profile.leaf,
                    family_key_hint=profile.family_key,
                    priority=2.8,
                    notes=[
                        f"leaf={profile.leaf}",
                        f"schedule_code={profile.schedule_code}",
                        f"title={profile.title[:60]}",
                    ],
                ))
        logger.debug(
            "Family profile queries: %d generated for %d profiles",
            len(queries),
            sum(1 for p in all_profiles() if p.family_key.startswith("nc-progress")),
        )
        return queries

    def build_targeted_queries(
        self,
        *,
        utility: str | None = None,
        schedule_codes: list[str] | None = None,
        rider_names: list[str] | None = None,
        doc_types: list[str] | None = None,
        finality_terms: list[str] | None = None,
    ) -> list[QuerySpec]:
        """
        Build a focused query set for specific targets.
        All parameters are optional — whatever is provided is cross-producted.
        """
        self._ensure_compat_loaded()
        queries: list[QuerySpec] = []

        utilities = [utility] if utility else ["Duke Energy Progress", "Progress Energy Carolinas"]
        doc_types = doc_types or ["tariff", "rider", "schedule"]
        schedule_codes = schedule_codes or []
        rider_names = rider_names or []
        finality_terms = finality_terms or []
        focused_mode = bool(schedule_codes or rider_names)

        for util in utilities:
            util_t = self._term(util)

            # Utility × doc_type
            if not focused_mode:
                for doc in doc_types:
                    doc_t = self._term(doc)
                    queries.append(QuerySpec(
                        query_text=self._query(f"{util_t} {doc_t}"),
                        template_name="targeted_util_x_doc",
                        utility_hint=util,
                        doc_type_hint=doc,
                        priority=2.0,
                    ))

            # Utility × schedule codes
            for code in schedule_codes:
                for doc in (doc_types if focused_mode else ["schedule"]):
                    code_t = self._term(f"schedule {code}")
                    doc_t = self._term(doc)
                    queries.append(QuerySpec(
                        query_text=self._query(f"{util_t} {doc_t} {code_t}"),
                        template_name="targeted_util_x_schedule",
                        utility_hint=util,
                        doc_type_hint=doc,
                        schedule_code_hint=code,
                        family_key_hint=code,
                        priority=3.2,
                    ))

            # Utility × rider names
            for rider in rider_names:
                rider_t = self._term(rider)
                for doc in (doc_types if focused_mode else ["rider"]):
                    doc_t = self._term(doc)
                    queries.append(QuerySpec(
                        query_text=self._query(f"{util_t} {doc_t} rider {rider_t}"),
                        template_name="targeted_util_x_rider",
                        utility_hint=util,
                        doc_type_hint=doc,
                        rider_code_hint=rider,
                        priority=3.0,
                    ))

            # Utility × finality
            for fin in finality_terms:
                fin_t = self._term(fin)
                queries.append(QuerySpec(
                    query_text=self._query(f"{util_t} {fin_t}"),
                    template_name="targeted_util_x_finality",
                    utility_hint=util,
                    doc_type_hint=None,
                    priority=1.5,
                ))

        seen: dict[str, QuerySpec] = {}
        for qs in queries:
            key = qs.query_text.strip().lower()
            if key not in seen or qs.priority > seen[key].priority:
                seen[key] = qs

        return sorted(seen.values(), key=lambda x: -x.priority)

    def build_refinement_queries(
        self,
        seed_terms: list[str],
        utility_hint: str | None = None,
    ) -> list[QuerySpec]:
        """
        Generate refinement queries from terms discovered in high-scoring results.
        Used for Stage 6: iterative refinement.
        """
        self._ensure_compat_loaded()
        queries: list[QuerySpec] = []
        utilities = [utility_hint] if utility_hint else [
            "Duke Energy Progress",
            "Progress Energy Carolinas",
        ]

        for util in utilities:
            util_t = self._term(util)
            for term in seed_terms:
                term_t = self._term(term)
                q = self._query(f"{util_t} {term_t}")
                queries.append(QuerySpec(
                    query_text=q,
                    template_name="refinement_seed_term",
                    utility_hint=util,
                    doc_type_hint=None,
                    notes=[f"seed_term={term}"],
                    priority=1.8,
                ))

        return queries

    # ------------------------------------------------------------------
    # HQ-signal-driven queries (Stage X: high-confidence targeted search)
    # ------------------------------------------------------------------

    _LEAF_NO_RE = re.compile(r'(?i)leaf\s+no\.?\s*(\d{1,4})')
    _RIDER_CODE_RE = re.compile(r'(?i)rider\s+([A-Z][A-Z0-9\-]{1,8})')
    _SCHEDULE_CODE_RE = re.compile(r'(?i)schedule\s+([A-Z][A-Z0-9\-]{1,8})')
    _REVISION_RE = re.compile(
        r'(?i)\b(original|first|second|third|fourth|fifth|sixth|seventh|eighth|'
        r'ninth|tenth|\d+(?:st|nd|rd|th))\s+revised\b'
    )
    _YEAR_RE = re.compile(r'\b(20\d{2})\b')
    _FILING_TYPE_RE = re.compile(
        r'(?i)\b(compliance\s+tariff|annual\s+adjustment|annual\s+compliance|'
        r'tariff\s+filing|revised\s+tariff|compliance\s+filing)\b'
    )

    @staticmethod
    def _extract_hq_tokens(title: str) -> dict:
        """
        Extract structured tokens from a known-HQ document title.

        Returns a dict with keys:
            leaf_nos      list[str]  — leaf numbers found ("602", "607")
            rider_codes   list[str]  — rider codes found ("JAA", "STS")
            schedule_codes list[str] — schedule codes found ("RS", "RT")
            revision_label str|None  — "Third Revised" etc.
            year_hint      str|None  — most recent year found
            filing_type    str|None  — "compliance tariff", "annual adjustment" etc.
        """
        leaf_nos = list(dict.fromkeys(
            m.group(1) for m in QueryBuilder._LEAF_NO_RE.finditer(title)
        ))
        rider_codes = list(dict.fromkeys(
            m.group(1).upper() for m in QueryBuilder._RIDER_CODE_RE.finditer(title)
        ))
        schedule_codes = list(dict.fromkeys(
            m.group(1).upper() for m in QueryBuilder._SCHEDULE_CODE_RE.finditer(title)
        ))
        rev_m = QueryBuilder._REVISION_RE.search(title)
        revision_label = rev_m.group(0).strip() if rev_m else None
        years = QueryBuilder._YEAR_RE.findall(title)
        year_hint = max(years) if years else None
        ft_m = QueryBuilder._FILING_TYPE_RE.search(title)
        filing_type = ft_m.group(0).strip().lower() if ft_m else None
        return {
            "leaf_nos": leaf_nos,
            "rider_codes": rider_codes,
            "schedule_codes": schedule_codes,
            "revision_label": revision_label,
            "year_hint": year_hint,
            "filing_type": filing_type,
        }

    def build_hq_signal_queries(
        self,
        hq_docs: list[dict],
    ) -> list[QuerySpec]:
        """
        Build high-confidence targeted queries from known T1/T2 document signals.

        Each entry in *hq_docs* should have at minimum a "filing_title" key.
        Optional keys: "utility", "docket_number", "family_keys" (list),
        "referenced_leaf_nos" (list), "referenced_rider_codes" (list).

        Returns QuerySpec list with priority >= 3.5 — these should be run before
        generic vocabulary queries.
        """
        self._ensure_compat_loaded()
        queries: list[QuerySpec] = []
        seen: set[str] = set()

        for doc in hq_docs:
            title = doc.get("filing_title") or ""
            tokens = self._extract_hq_tokens(title)

            # Merge tokens with explicit referenced codes from the record.
            leaf_nos = list(dict.fromkeys(
                tokens["leaf_nos"] + list(doc.get("referenced_leaf_nos") or [])
            ))
            rider_codes = list(dict.fromkeys(
                tokens["rider_codes"] + list(doc.get("referenced_rider_codes") or [])
            ))

            utility = doc.get("utility") or "Duke Energy Progress"
            util_t = self._term(utility)
            docket = doc.get("docket_number")

            # --- Query 1: Leaf No. + compliance/tariff phrase ---
            for leaf in leaf_nos:
                leaf_phrase = f"Leaf No {leaf}"
                leaf_t = self._term(leaf_phrase)
                q = self._query(f"{util_t} {leaf_t}")
                if q not in seen:
                    seen.add(q)
                    queries.append(QuerySpec(
                        query_text=q,
                        template_name="hq_leaf_no",
                        utility_hint=utility,
                        doc_type_hint="tariff",
                        schedule_code_hint=leaf,
                        notes=[f"from_hq_title={title[:60]}"],
                        priority=4.0,
                    ))
                # Also search bare leaf number + "compliance tariff"
                ct_t = self._term("compliance tariff")
                q2 = self._query(f"{leaf_t} {ct_t}")
                if q2 not in seen:
                    seen.add(q2)
                    queries.append(QuerySpec(
                        query_text=q2,
                        template_name="hq_leaf_no_compliance",
                        utility_hint=utility,
                        doc_type_hint="compliance tariff",
                        schedule_code_hint=leaf,
                        notes=[f"from_hq_title={title[:60]}"],
                        priority=4.5,
                    ))

            # --- Query 2: Rider code + compliance phrase ---
            for rider in rider_codes:
                rider_t = self._term(f"Rider {rider}")
                q = self._query(f"{util_t} {rider_t}")
                if q not in seen:
                    seen.add(q)
                    queries.append(QuerySpec(
                        query_text=q,
                        template_name="hq_rider_code",
                        utility_hint=utility,
                        doc_type_hint="rider",
                        rider_code_hint=rider,
                        notes=[f"from_hq_title={title[:60]}"],
                        priority=3.8,
                    ))
                if tokens["filing_type"]:
                    ft_t = self._term(tokens["filing_type"])
                    q3 = self._query(f"{rider_t} {ft_t}")
                    if q3 not in seen:
                        seen.add(q3)
                        queries.append(QuerySpec(
                            query_text=q3,
                            template_name="hq_rider_filing_type",
                            utility_hint=utility,
                            doc_type_hint=tokens["filing_type"],
                            rider_code_hint=rider,
                            notes=[f"from_hq_title={title[:60]}"],
                            priority=4.2,
                        ))

            # --- Query 3: Revision label + leaf/rider (pinpoints specific version) ---
            if tokens["revision_label"]:
                rev_t = self._term(tokens["revision_label"])
                for leaf in leaf_nos[:1]:  # just the first leaf to keep query count bounded
                    leaf_t = self._term(f"Leaf {leaf}")
                    q = self._query(f"{rev_t} {leaf_t}")
                    if q not in seen:
                        seen.add(q)
                        queries.append(QuerySpec(
                            query_text=q,
                            template_name="hq_revision_leaf",
                            utility_hint=utility,
                            doc_type_hint="revised tariff",
                            schedule_code_hint=leaf,
                            notes=[f"from_hq_title={title[:60]}"],
                            priority=3.5,
                        ))

        return queries
