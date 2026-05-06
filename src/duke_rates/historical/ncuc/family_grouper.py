"""
Stage 7: Document family grouper.

Clusters related versions of the same underlying tariff/rider document
together so that only the most authoritative version is surfaced.

A "family" is a set of results that appear to be different versions of the
same schedule, rider, or tariff sheet: the clean filing, the redline companion,
an earlier revision, testimony that references it, an order approving it, etc.

Within each family, the grouper ranks the most likely authoritative / ideal
version highest.

Clustering signals:
- Normalized title similarity
- Docket number match
- Extracted schedule code overlap
- Extracted rider code overlap
- Redline/clean indicators
- Superseding / canceling language
- Sheet number references
- Utility match
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duke_rates.historical.ncuc.result_harvester import SearchResult
    from duke_rates.historical.ncuc.result_scorer import ScoredResult

# ---------------------------------------------------------------------------
# Title normalization helpers
# ---------------------------------------------------------------------------

_NOISE_WORDS = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "at", "by",
    "with", "from", "on", "its", "this", "that", "as", "is", "are",
}

_STRIP_PAT = re.compile(r"[^a-z0-9\s]")


def _normalize_title(title: str | None) -> str:
    """Return a lowercased, stripped, noise-word-filtered title."""
    if not title:
        return ""
    t = title.lower()
    t = _STRIP_PAT.sub(" ", t)
    words = [w for w in t.split() if w and w not in _NOISE_WORDS]
    return " ".join(words)


def _title_overlap(a: str | None, b: str | None) -> float:
    """Jaccard coefficient on normalized title token sets."""
    na = set(_normalize_title(a).split())
    nb = set(_normalize_title(b).split())
    if not na or not nb:
        return 0.0
    return len(na & nb) / len(na | nb)


# ---------------------------------------------------------------------------
# Signals that indicate a document is a revision/redline vs. the clean version
# ---------------------------------------------------------------------------

_REDLINE_PAT = re.compile(r"\bredline[d]?\b|\bmark-?up\b|\btrack\s+change\b", re.I)
_CLEAN_PAT = re.compile(r"\bclean\b|\bfinal\s+version\b|\bclean\s+version\b", re.I)
_DRAFT_PAT = re.compile(r"\bdraft\b", re.I)
_SUPERSEDE_PAT = re.compile(r"\bsupersed(?:ing|es|ed)\b", re.I)
_CANCEL_PAT = re.compile(r"\bcanceling\b|\bcancel(?:s|ed)\b|\bcancellation\b", re.I)
_REVISED_PAT = re.compile(r"\brevisd?\b|\brevision\b|\brev\b", re.I)
_SHEET_NO_PAT = re.compile(r"\bsheet\s+(?:no\.?\s*)?(\d+[A-Z]?)\b", re.I)

# ---------------------------------------------------------------------------
# Family grouping data models
# ---------------------------------------------------------------------------

@dataclass
class DocumentFamily:
    """A cluster of related search results."""
    family_id: str              # derived key (e.g. "dep_tariff_501_schedule")
    members: list["ScoredResult"] = field(default_factory=list)
    canonical_title: str | None = None
    utility: str | None = None
    schedule_codes: list[str] = field(default_factory=list)
    rider_codes: list[str] = field(default_factory=list)
    docket_numbers: list[str] = field(default_factory=list)

    @property
    def best(self) -> "ScoredResult | None":
        """Highest-scoring, most-ideal member."""
        if not self.members:
            return None
        # Primary sort: ideality (ideal candidates first)
        # Secondary sort: combined_score desc
        return max(
            self.members,
            key=lambda m: (
                1 if m.ideality.is_ideal_candidate else 0,
                m.combined_score,
            ),
        )

    @property
    def best_ideal(self) -> "ScoredResult | None":
        """Best member that is classified as an ideal candidate."""
        ideals = [m for m in self.members if m.ideality.is_ideal_candidate]
        if not ideals:
            return None
        return max(ideals, key=lambda m: m.combined_score)

    @property
    def has_redline(self) -> bool:
        return any(
            _REDLINE_PAT.search((m.result.title or "") + " " + (m.result.snippet or ""))
            for m in self.members
        )

    @property
    def has_clean(self) -> bool:
        return any(m.ideality.likely_finality == "final" for m in self.members)

    def size(self) -> int:
        return len(self.members)

    def member_summary(self) -> list[str]:
        return [
            f"[{m.ideality.likely_finality}/{m.ideality.doc_type_guess}] "
            f"{(m.result.title or 'untitled')[:60]} "
            f"(score={m.combined_score:.1f}, ideal={m.ideality.is_ideal_candidate})"
            for m in self.members
        ]


# ---------------------------------------------------------------------------
# The grouper
# ---------------------------------------------------------------------------

class DocumentFamilyGrouper:
    """
    Groups ScoredResult objects into document families.

    Two results are placed in the same family if any of the following hold:
    1. Their normalized titles have Jaccard overlap >= TITLE_THRESHOLD
    2. They share a non-None docket number
    3. They share at least one extracted schedule code AND at least one utility term
    4. They share at least one extracted rider code AND at least one utility term

    Within a family, members are ranked: ideal > non-ideal, higher local_score first.
    """

    TITLE_THRESHOLD = 0.35      # Jaccard overlap ≥ this → same family
    DOCKET_BONUS = 0.2          # Extra toward threshold when docket matches
    CODE_THRESHOLD = 0.25       # Jaccard overlap of schedule/rider codes → same family

    def group(self, scored_results: list["ScoredResult"]) -> list[DocumentFamily]:
        """
        Cluster scored_results into DocumentFamily objects.
        Returns a list of families, sorted by the score of their best member (desc).
        """
        if not scored_results:
            return []

        # Union-find for clustering
        n = len(scored_results)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        # Compare all pairs
        for i in range(n):
            for j in range(i + 1, n):
                if self._should_merge(scored_results[i], scored_results[j]):
                    union(i, j)

        # Collect clusters
        clusters: dict[int, list[int]] = {}
        for i in range(n):
            root = find(i)
            clusters.setdefault(root, []).append(i)

        # Build DocumentFamily objects
        families: list[DocumentFamily] = []
        for cluster_indices in clusters.values():
            members = [scored_results[i] for i in cluster_indices]
            family = self._build_family(members)
            families.append(family)

        # Sort by best member's score descending
        families.sort(
            key=lambda f: (
                1 if (f.best and f.best.ideality.is_ideal_candidate) else 0,
                f.best.combined_score if f.best else 0.0,
            ),
            reverse=True,
        )

        return families

    def _should_merge(self, a: "ScoredResult", b: "ScoredResult") -> bool:
        """Return True if two results belong to the same document family."""
        ra = a.result
        rb = b.result

        # Docket match (strong signal)
        if (ra.docket_number and rb.docket_number
                and ra.docket_number == rb.docket_number
                and ra.sub_number == rb.sub_number):
            # Same docket + high title overlap → definitely same family
            overlap = _title_overlap(ra.title, rb.title)
            if overlap >= self.TITLE_THRESHOLD - self.DOCKET_BONUS:
                return True

        # Title similarity (primary signal)
        title_sim = _title_overlap(ra.title, rb.title)
        if title_sim >= self.TITLE_THRESHOLD:
            return True

        # Shared schedule code + utility
        codes_a = set(ra.extracted_schedule_codes)
        codes_b = set(rb.extracted_schedule_codes)
        if codes_a and codes_b and codes_a & codes_b:
            # Also require some title overlap to avoid over-merging
            if title_sim >= 0.10:
                return True

        # Shared rider code + title overlap
        riders_a = set(ra.extracted_rider_codes)
        riders_b = set(rb.extracted_rider_codes)
        if riders_a and riders_b and riders_a & riders_b:
            if title_sim >= 0.10:
                return True

        return False

    def _build_family(self, members: list["ScoredResult"]) -> DocumentFamily:
        """Build a DocumentFamily from a list of ScoredResult members."""
        # Derive canonical title from the ideal/highest-scoring member
        sorted_members = sorted(members, key=lambda m: -m.combined_score)
        canonical_title = sorted_members[0].result.title if sorted_members else None

        # Aggregate metadata
        schedule_codes: set[str] = set()
        rider_codes: set[str] = set()
        docket_numbers: set[str] = set()
        utilities: list[str] = []

        for m in members:
            schedule_codes.update(m.result.extracted_schedule_codes)
            rider_codes.update(m.result.extracted_rider_codes)
            if m.result.docket_number:
                docket_numbers.add(m.result.docket_number)
            if m.result.utility_hint:
                utilities.append(m.result.utility_hint)

        utility = max(set(utilities), key=utilities.count) if utilities else None

        # Build family ID
        codes_str = "_".join(sorted(schedule_codes)[:3]) if schedule_codes else "general"
        riders_str = "_".join(sorted(rider_codes)[:2]) if rider_codes else ""
        doc_type = sorted_members[0].ideality.doc_type_guess if sorted_members else "other"
        util_slug = (utility or "dep").lower().replace(" ", "_")[:15]
        id_parts = [util_slug, doc_type, codes_str]
        if riders_str:
            id_parts.append(riders_str)
        family_id = "_".join(p for p in id_parts if p)[:80]

        family = DocumentFamily(
            family_id=family_id,
            members=sorted_members,
            canonical_title=canonical_title,
            utility=utility,
            schedule_codes=sorted(schedule_codes),
            rider_codes=sorted(rider_codes),
            docket_numbers=sorted(docket_numbers),
        )
        return family

    @staticmethod
    def get_best_per_family(
        families: list[DocumentFamily],
        *,
        only_ideal: bool = False,
    ) -> list["ScoredResult"]:
        """Return the best-ranked member from each family."""
        results = []
        for fam in families:
            if only_ideal:
                best = fam.best_ideal
            else:
                best = fam.best
            if best is not None:
                results.append(best)
        return results

    def print_family_report(self, families: list[DocumentFamily], top_n: int = 20) -> None:
        """Print a human-readable family grouping report."""
        print(f"\n=== Document Family Report ({len(families)} families) ===\n")
        for i, fam in enumerate(families[:top_n], 1):
            best = fam.best
            ideal_tag = "[IDEAL]" if (best and best.ideality.is_ideal_candidate) else "[non-ideal]"
            print(
                f"{i:2d}. {ideal_tag} Family: {fam.family_id[:50]}"
                f"  ({fam.size()} member{'s' if fam.size() != 1 else ''})"
            )
            if fam.schedule_codes:
                print(f"    Schedules: {', '.join(fam.schedule_codes)}")
            if fam.rider_codes:
                print(f"    Riders: {', '.join(fam.rider_codes)}")
            if best:
                print(
                    f"    Best: {(best.result.title or 'untitled')[:60]}"
                    f"  score={best.combined_score:.1f}"
                    f"  [{best.ideality.doc_type_guess}/{best.ideality.likely_finality}]"
                )
            for line in fam.member_summary()[:3]:
                print(f"      · {line}")
            if fam.size() > 3:
                print(f"      … and {fam.size() - 3} more")
            print()
