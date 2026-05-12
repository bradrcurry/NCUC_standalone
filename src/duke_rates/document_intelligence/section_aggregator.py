"""
Section evidence aggregator (Phase 6B of parsing-architecture refactor).

Seeds section boundaries from ``ncuc_span_artifacts`` tariff spans, then
refines them using per-page classification signals from
``ncuc_page_artifacts.metadata_json``. Classifies each section's type and
computes confidence from multiple signal sources.

The aggregator is deterministic and idempotent — ``populate_all()`` can be
called repeatedly.

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §6.4.

Usage::

    from duke_rates.document_intelligence.section_aggregator import (
        DocumentSectionAggregator,
    )
    agg = DocumentSectionAggregator(db_path)
    sections = agg.populate_one(source_pdf)
    print(f"found {len(sections)} sections")
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .document_sections import (
    SectionBundle,
    SectionType,
    RATE_SECTION_TYPES,
    WEIGHT_SECTION_LEAF_MATCH,
    WEIGHT_SECTION_CODE_MATCH,
    WEIGHT_SECTION_TYPE_CLEAR,
    WEIGHT_SECTION_SPAN_AGREE,
    WEIGHT_SECTION_RATE_VALUES,
    ensure_schema,
    fetch_sections,
    upsert_section,
    delete_sections_for_pdf,
)

# ---------------------------------------------------------------------------
# Document-level classification — derived from section mixes
# ---------------------------------------------------------------------------


class DocumentType:
    """Document-level types derived from section composition."""

    COMPLIANCE_TARIFF_BUNDLE = "compliance_tariff_bundle"
    SINGLE_RATE_SCHEDULE = "single_rate_schedule"
    HEARING_EXHIBIT = "hearing_exhibit"
    APPLICATION_FILING = "application_filing"
    DSM_EE_EVALUATION = "dsm_ee_evaluation"
    COVER_LETTER_PACKAGE = "cover_letter_package"
    TERMS_AND_CONDITIONS = "terms_and_conditions"
    MIXED_COMPLIANCE = "mixed_compliance"
    UNKNOWN = "unknown"

    # Types that benefit from LLM boundary analysis (rate-bearing bundles)
    BOUNDARY_ANALYSIS_TYPES: frozenset[str] = frozenset({
        COMPLIANCE_TARIFF_BUNDLE,
        SINGLE_RATE_SCHEDULE,
    })


# Thresholds for document type derivation
_DOC_TYPE_MIN_RATE_FOR_BUNDLE = 3
_DOC_TYPE_PROCEDURAL_DOMINANCE = 0.5
_DOC_TYPE_COVER_LETTER_DOMINANCE = 0.7
_DOC_TYPE_TERMS_DOMINANCE = 0.6

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Small helper dataclass for page-level signals
# ---------------------------------------------------------------------------


@dataclass
class _PageSignals:
    page_number: int
    text_content: str
    has_leaf_header: bool = False
    has_schedule_heading: bool = False
    has_revised_header: bool = False
    has_effective_date_phrase: bool = False
    has_redline_markers: bool = False
    has_dual_rate_pair: bool = False
    has_toc_page: bool = False
    tariff_vocab_density: float = 0.0
    procedural_vocab_density: float = 0.0
    numeric_density: float = 0.0
    table_like_density: float = 0.0
    extracted_leaf_nos: list[str] | None = None
    extracted_schedule_codes: list[str] | None = None


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


class DocumentSectionAggregator:
    """Build section bundles from span + page-level evidence."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        ensure_schema(self._db_path)
        self._known_codes: frozenset[str] | None = None  # lazy-built

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def select_candidates(self, *, limit: int | None = None) -> list[str]:
        """Return source_pdfs that have ncuc_page_artifacts rows.

        Only documents with 3+ pages are considered (single-page docs
        don't benefit from section identification).
        """
        conn = sqlite3.connect(str(self._db_path))
        try:
            sql = """
            SELECT source_pdf
            FROM ncuc_page_artifacts
            WHERE source_pdf IS NOT NULL AND source_pdf != ''
            GROUP BY source_pdf
            HAVING COUNT(DISTINCT page_number) >= 3
            ORDER BY source_pdf
            """
            if limit:
                sql += f" LIMIT {int(limit)}"
            rows = conn.execute(sql).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def build_sections(self, source_pdf: str) -> list[SectionBundle]:
        """Build section bundles for one document without persisting them."""
        pages = self._load_pages(source_pdf)
        if len(pages) < 2:
            return []

        spans = self._load_spans(source_pdf)
        sections = self._seed_from_spans(source_pdf, pages, spans)
        sections = self._refine_boundaries(sections, pages)
        sections = self._merge_adjacent_similar(sections)
        sections = self._classify_and_score(sections, pages, spans)
        # Re-index after potential merges
        for i, s in enumerate(sections):
            s.section_index = i
        return sections

    def populate_all(self, *, limit: int | None = None) -> int:
        """Build and upsert sections for every candidate document.

        Returns the number of source_pdfs processed (not section count).
        """
        pdfs = self.select_candidates(limit=limit)
        total = 0
        for pdf in pdfs:
            try:
                self.populate_one(pdf)
                total += 1
            except Exception:
                logger.warning(
                    "section aggregation failed for %s", pdf, exc_info=True,
                )
        return total

    def populate_one(self, source_pdf: str) -> list[SectionBundle]:
        """Build and persist sections for a single document."""
        sections = self.build_sections(source_pdf)
        delete_sections_for_pdf(self._db_path, source_pdf)
        for s in sections:
            upsert_section(self._db_path, s)

        # Derive and persist document-level classification
        pages = self._load_pages(source_pdf)
        doc_type, confidence, evidence = self._derive_document_type(
            source_pdf, sections, pages,
        )
        self._upsert_document_classification(
            source_pdf, doc_type, confidence, evidence,
        )

        return sections

    # ------------------------------------------------------------------
    # Internal — data loading
    # ------------------------------------------------------------------

    def _load_pages(self, source_pdf: str) -> list[_PageSignals]:
        """Load deduplicated page signals for a document."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT page_number, text_content, metadata_json
                   FROM ncuc_page_artifacts
                   WHERE source_pdf = ?
                   ORDER BY page_number""",
                (source_pdf,),
            ).fetchall()
        finally:
            conn.close()

        seen: set[int] = set()
        pages: list[_PageSignals] = []
        for row in rows:
            pn = row["page_number"]
            if pn in seen:
                continue
            seen.add(pn)
            try:
                meta = json.loads(row["metadata_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            # Filter raw metadata codes — page_miner's _expand_heading_codes
            # historically stored full descriptive phrases as "codes" (e.g.,
            # "APPLICABLE TO ELECTRIC UTILITY SERVICE").  Strip those out at
            # load time so downstream splitting and classification see only
            # real schedule/rider identifiers.
            raw_codes: list[str] = meta.get("extracted_schedule_codes") or []
            clean_codes = [c for c in raw_codes if self._is_known_or_code_like(c)]

            pages.append(_PageSignals(
                page_number=pn,
                text_content=row["text_content"] or "",
                has_leaf_header=bool(meta.get("has_leaf_header")),
                has_schedule_heading=bool(meta.get("has_schedule_heading")),
                has_revised_header=bool(meta.get("has_revised_header")),
                has_effective_date_phrase=bool(meta.get("has_effective_date_phrase")),
                has_redline_markers=bool(meta.get("has_redline_markers")),
                has_dual_rate_pair=bool(meta.get("has_dual_rate_pair")),
                has_toc_page=bool(meta.get("has_toc_page")),
                tariff_vocab_density=float(meta.get("tariff_vocab_density") or 0.0),
                procedural_vocab_density=float(meta.get("procedural_vocab_density") or 0.0),
                numeric_density=float(meta.get("numeric_density") or 0.0),
                table_like_density=float(meta.get("table_like_density") or 0.0),
                extracted_leaf_nos=meta.get("extracted_leaf_nos") or [],
                extracted_schedule_codes=clean_codes,
            ))
        return pages

    def _load_spans(self, source_pdf: str) -> list[dict[str, Any]]:
        """Load ncuc_span_artifacts for a document, deduplicated by span_index."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT span_index, start_page, end_page, doc_type, confidence,
                          extracted_leaf_nos_json, extracted_schedule_titles_json
                   FROM ncuc_span_artifacts
                   WHERE source_pdf = ?
                   ORDER BY span_index""",
                (source_pdf,),
            ).fetchall()
        finally:
            conn.close()

        # Deduplicate by span_index — keep the row with the largest page range
        seen: dict[int, dict[str, Any]] = {}
        for row in rows:
            si = row["span_index"]
            sp = int(row["start_page"])
            ep = int(row["end_page"])
            page_count = ep - sp + 1
            if si in seen:
                existing = seen[si]
                existing_count = existing["end_page"] - existing["start_page"] + 1
                if page_count <= existing_count:
                    continue
            try:
                leaf_nos = json.loads(row["extracted_leaf_nos_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                leaf_nos = []
            try:
                titles = json.loads(row["extracted_schedule_titles_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                titles = []
            seen[si] = {
                "span_index": si,
                "start_page": sp,
                "end_page": ep,
                "doc_type": row["doc_type"],
                "confidence": row["confidence"] or 0.0,
                "extracted_leaf_nos": leaf_nos,
                "extracted_schedule_titles": titles,
            }

        spans = sorted(seen.values(), key=lambda s: s["span_index"])

        # Remove spans fully contained within another (e.g., span 3-12 inside span 3-22)
        filtered: list[dict[str, Any]] = []
        for s in spans:
            contained = False
            for other in spans:
                if other is s:
                    continue
                if (other["start_page"] <= s["start_page"]
                        and other["end_page"] >= s["end_page"]
                        and other["doc_type"] == s["doc_type"]):
                    contained = True
                    break
            if not contained:
                filtered.append(s)

        return filtered

    # ------------------------------------------------------------------
    # Internal — boundary detection
    # ------------------------------------------------------------------

    def _seed_from_spans(
        self,
        source_pdf: str,
        pages: list[_PageSignals],
        spans: list[dict[str, Any]],
    ) -> list[SectionBundle]:
        """Create initial section candidates from tariff spans.

        Non-tariff spans (procedural, etc.) are kept but marked as procedural
        or cover-letter type.
        """
        if not spans:
            # No spans — create a single catch-all section
            return [SectionBundle(
                source_pdf=source_pdf,
                section_index=0,
                start_page=pages[0].page_number,
                end_page=pages[-1].page_number,
                section_type=SectionType.UNKNOWN,
                evidence_log=[{"source": "no_spans_fallback", "page_count": len(pages)}],
            )]

        # Only use tariff spans for seeding rate sections
        tariff_spans = [s for s in spans if s["doc_type"] == "tariff"]
        other_spans = [s for s in spans if s["doc_type"] != "tariff"]

        sections: list[SectionBundle] = []
        idx = 0

        for span in tariff_spans:
            leaf_nos = [str(n) for n in span["extracted_leaf_nos"] if n]
            sections.append(SectionBundle(
                source_pdf=source_pdf,
                section_index=idx,
                start_page=int(span["start_page"]),
                end_page=int(span["end_page"]),
                leaf_numbers=leaf_nos,
                evidence_log=[{
                    "source": "ncuc_span_artifacts",
                    "span_index": span["span_index"],
                    "doc_type": span["doc_type"],
                    "start_page": span["start_page"],
                    "end_page": span["end_page"],
                    "confidence": span["confidence"],
                    "extracted_leaf_nos": leaf_nos,
                    "extracted_schedule_titles": span["extracted_schedule_titles"],
                }],
            ))
            idx += 1

        # Handle non-tariff spans (cover letters, procedural)
        for span in other_spans:
            sections.append(SectionBundle(
                source_pdf=source_pdf,
                section_index=idx,
                start_page=int(span["start_page"]),
                end_page=int(span["end_page"]),
                section_type=(
                    SectionType.PROCEDURAL
                    if span["doc_type"] == "procedural"
                    else SectionType.COVER_LETTER
                ),
                evidence_log=[{
                    "source": "ncuc_span_artifacts",
                    "span_index": span["span_index"],
                    "doc_type": span["doc_type"],
                    "start_page": span["start_page"],
                    "end_page": span["end_page"],
                }],
            ))
            idx += 1

        # Sort by start_page
        sections.sort(key=lambda s: (s.start_page, s.end_page))
        for i, s in enumerate(sections):
            s.section_index = i
        return sections

    def _refine_boundaries(
        self,
        sections: list[SectionBundle],
        pages: list[_PageSignals],
    ) -> list[SectionBundle]:
        """Split sections on hard boundary signals within a span.

        Hard boundaries occur when:
        - A new leaf number appears that differs from the current section's set
        - A new schedule heading with a different code appears
        - has_effective_date_phrase signals a new version
        - has_redline_markers or has_dual_rate_pair signals structure change
        - Text patterns detect \"SCHEDULE <CODE>\" or \"RIDER <CODE>\" headers
        - For large sections (>8 pages), company/electricity headers also split
        """
        pages_by_num = {p.page_number: p for p in pages}
        refined: list[SectionBundle] = []

        for section in sections:
            section_pages = [
                pages_by_num[pn]
                for pn in range(section.start_page, section.end_page + 1)
                if pn in pages_by_num
            ]
            if len(section_pages) < 2:
                refined.append(section)
                continue

            # Track current leaf/schedule set for this section
            current_leaf_set = set(section.leaf_numbers)
            current_code_set: set[str] = set()
            split_points: set[int] = set()

            # Pre-compute leaf-header count for the aggressive-split guard.
            # Hearing exhibits / testimony bundles have schedule headings
            # but ZERO leaf headers, unlike real compliance tariff bundles.
            _section_leaf_header_count = sum(
                1 for p in section_pages if p.has_leaf_header
            )

            # --- Text-based boundary detection ---
            text_boundaries = self._collect_text_boundaries(
                section_pages, section.start_page,
            )
            for tb_idx in text_boundaries:
                # Only split text boundaries that are at least 2 pages into
                # the section (don't split at the section's own first page)
                if tb_idx >= 2:
                    split_points.add(tb_idx)

            for i, pg in enumerate(section_pages):
                pg_leaf_nos = set(pg.extracted_leaf_nos or [])
                # Filter schedule codes through _is_code_like to strip
                # generic labels like "SMALL GENERAL SERVICE" that would
                # cause false overlaps between different schedules
                pg_sched_codes = {
                    c for c in (pg.extracted_schedule_codes or [])
                    if self._is_known_or_code_like(c)
                }

                # Fall back to text-extracted codes when metadata codes are
                # empty but has_schedule_heading is True. Large compliance
                # bundles often have heading signals in metadata but the
                # code text itself wasn't extracted by the page miner.
                if pg.has_schedule_heading and not pg_sched_codes:
                    text_sig = self._detect_text_boundary_signals(
                        pg.text_content,
                    )
                    tcode = (
                        text_sig.get("schedule_heading_text")
                        or text_sig.get("superseding_schedule_code")
                        or text_sig.get("rider_heading_text")
                        or ""
                    ).strip().upper()
                    if tcode and self._is_known_or_code_like(tcode):
                        pg_sched_codes = {tcode}

                # For very large sections (>30 pages) that lack leaf headers:
                # every page with a schedule heading is a hard split boundary,
                # even without extracted codes. Hearing exhibits and testimony
                # bundles pack 30+ schedules into a single span and the page
                # miner's heading detector is the only reliable signal.
                # We require zero leaf-header pages to avoid over-splitting
                # real compliance tariff bundles (which have leaf headers and
                # are handled by the standard code/leaf-based splitting).
                if (
                    len(section_pages) > 30
                    and pg.has_schedule_heading
                    and i >= 2
                    and _section_leaf_header_count == 0
                ):
                    split_points.add(i)
                    current_code_set = pg_sched_codes if pg_sched_codes else set()
                    current_leaf_set = set()
                    continue

                # New leaf numbers that are disjoint from current set
                if pg_leaf_nos and current_leaf_set and not (pg_leaf_nos & current_leaf_set):
                    # Only split if the new leaf numbers are persistent (appear on >= 2 pages)
                    # Single-page leaf changes may be OCR artifacts
                    if i + 1 < len(section_pages):
                        next_pg = section_pages[i + 1]
                        next_leaf_nos = set(next_pg.extracted_leaf_nos or [])
                        if pg_leaf_nos & next_leaf_nos:
                            split_points.add(i)
                            current_leaf_set = pg_leaf_nos
                            current_code_set = set()

                # New schedule codes that are disjoint
                if pg_sched_codes and current_code_set and not (pg_sched_codes & current_code_set):
                    if i + 1 < len(section_pages):
                        next_pg = section_pages[i + 1]
                        next_sched = {
                            c for c in (next_pg.extracted_schedule_codes or [])
                            if self._is_known_or_code_like(c)
                        }
                        if pg_sched_codes & next_sched:
                            split_points.add(i)
                            current_code_set = pg_sched_codes
                            current_leaf_set = set()

                # Strong heading signal: has_schedule_heading=1 with code-like
                # codes that differ from current set — split without persistence
                # check (common for 2-page schedules: heading + continuation)
                if (
                    pg.has_schedule_heading
                    and pg_sched_codes
                    and current_code_set
                    and not (pg_sched_codes & current_code_set)
                    and i >= 2
                ):
                    split_points.add(i)
                    current_code_set = pg_sched_codes
                    current_leaf_set = set()

                # First page with schedule codes — seed the tracking set
                if not current_code_set and pg_sched_codes:
                    current_code_set = pg_sched_codes

                # For large sections: "SCHEDULE" or "RIDER" heading on page
                # is a strong boundary even without a prior code set
                if len(section_pages) > 8 and pg_sched_codes and i >= 2:
                    if not current_code_set:
                        current_code_set = pg_sched_codes
                    elif pg_sched_codes - current_code_set:
                        # New codes appeared — but only split if persistent
                        if i + 1 < len(section_pages):
                            next_pg = section_pages[i + 1]
                            next_sched = set(next_pg.extracted_schedule_codes or [])
                            if pg_sched_codes & next_sched:
                                split_points.add(i)
                                current_code_set = pg_sched_codes
                                current_leaf_set = set()

                # Effective date phrase signals a new version
                if pg.has_effective_date_phrase and i >= 2:
                    # Only split if the previous page was a different structure type
                    prev = section_pages[i - 1]
                    if not prev.has_effective_date_phrase and not prev.has_leaf_header:
                        split_points.add(i)

                # Redline/dual-rate markers signal structure change
                if (pg.has_redline_markers or pg.has_dual_rate_pair) and i >= 2:
                    prev = section_pages[i - 1]
                    if not prev.has_redline_markers and not prev.has_dual_rate_pair:
                        split_points.add(i)

                # Initialize tracking sets from first page with signals
                if not current_leaf_set and pg_leaf_nos:
                    current_leaf_set = pg_leaf_nos

            if not split_points:
                refined.append(section)
                continue

            # Perform splits
            split_points = sorted(set(split_points))
            start = 0
            for sp in split_points:
                sub_pages = section_pages[start:sp]
                if sub_pages:
                    bundle = self._make_sub_section(section, sub_pages)
                    refined.append(bundle)
                start = sp
            # Last segment
            sub_pages = section_pages[start:]
            if sub_pages:
                bundle = self._make_sub_section(section, sub_pages)
                refined.append(bundle)

        refined.sort(key=lambda s: (s.start_page, s.end_page))
        for i, s in enumerate(refined):
            s.section_index = i
        return refined

    # Common English words that are definitely not schedule/rider codes
    _NON_CODE_WORDS: set[str] = frozenset({
        "a", "an", "the", "and", "or", "not", "for", "with", "that", "this",
        "is", "are", "was", "were", "be", "been", "shall", "will", "may",
        "can", "has", "had", "have", "do", "does", "did", "from", "upon",
        "to", "of", "in", "on", "at", "by", "as", "it", "its", "no",
        "if", "so", "we", "he", "she", "they", "all", "any", "each",
        "service", "company", "customer", "program", "electric", "energy",
        "application", "agreement", "certificate", "commission", "carolina",
        "north", "south", "clerk", "chief", "counsel", "general", "mail",
        "pursuant", "compliance", "revised", "modified", "fourth",
        "year", "solar", "rebate", "order", "apply", "modifying", "copy",
        "certify", "address", "mailing", "associate", "box", "please",
        # NCUC domain words that appear in metadata as false codes
        "supersedes", "superseding", "schedule", "rider", "riders",
        "adjustments", "adjustment", "billing", "annual", "factors",
        "asset", "liability", "cost", "recovery", "competitive",
        "procurement", "renewable", "portfolio", "standard", "reps",
        "demand", "side", "management", "dsm", "energywise", "smart",
        "saver", "storm", "securitization", "scr", "joint", "agency",
        "jaar", "fuel", "charge", "fca",
        # Months / days that appear in version headers
        "january", "february", "march", "april", "june", "july",
        "august", "september", "october", "november", "december",
        "monday", "tuesday", "wednesday", "thursday", "friday",
        "holiday", "holidays",
        # Rate description words
        "rate", "basic", "minimum", "monthly", "kwh", "kw", "per",
        "single", "phase", "three", "available", "voluntary",
        "residential", "general", "small", "medium", "large",
        "lighting", "outdoor", "street", "sports", "field",
        "time", "use", "critical", "peak", "pricing", "cppi",
        "seasonal", "intermittent", "cogeneration", "parallel",
        "purchase", "power", "qualifying", "facility", "cqpfs",
        "temporary", "construction", "overhead", "underground",
        "guard", "security", "unmetered", "metered",
        # Hyphenated false codes that slip through after hyphen splitting
        "out", "upgrades", "normally", "voltage", "operations",
        "schedulingschedule",
        # Additional domain words that are not rate codes
        "interconnection", "interconnected", "below",
        "section", "article", "rule", "paragraph", "exhibit", "appendix",
        "attachment", "annex", "addendum", "page", "pages",
        # Common words appearing in OCR on verified pages
        "before", "after", "hereby", "thereof", "hereof", "thereafter",
        "car", "ncuc", "raleigh", "charlotte", "carolinas",
    })

    def _build_known_codes(self) -> frozenset[str]:
        """Build a whitelist of known schedule/rider codes from verified pages.

        Uses pages that have BOTH a leaf header AND a schedule heading —
        these are almost certainly real tariff pages, so the extracted
        schedule codes from these pages are trustworthy.
        """
        import json as _json
        from collections import Counter as _Counter
        counter: _Counter[str] = _Counter()
        conn = sqlite3.connect(str(self._db_path))
        try:
            rows = conn.execute("""
                SELECT metadata_json
                FROM ncuc_page_artifacts
                WHERE json_extract(metadata_json, '$.has_leaf_header') = 1
                  AND json_extract(metadata_json, '$.has_schedule_heading') = 1
            """).fetchall()
            for (meta_json,) in rows:
                try:
                    meta = _json.loads(meta_json or "{}")
                except (_json.JSONDecodeError, TypeError):
                    continue
                for c in meta.get("extracted_schedule_codes") or []:
                    c = str(c).strip().upper()
                    if not c or len(c) > 15 or len(c) == 1:
                        continue
                    if c.lower() in self._NON_CODE_WORDS:
                        continue
                    if c in (
                        "CHURCH SERVICE", "GENERAL SERVICE",
                        "RATE SET", "RATE UPDATES",
                        "TERM OF SERVICE", "TYPE OF SERVICE",
                        "USE OF RIDER", "SMALL GENERAL SERVICE",
                        "MEDIUM GENERAL SERVICE", "LARGE GENERAL SERVICE",
                        "RESIDENTIAL SERVICE", "LIGHTING SERVICE",
                        "OUTDOOR LIGHTING SERVICE",
                    ):
                        continue
                    counter[c] += 1
        finally:
            conn.close()
        # Require at least 3 occurrences to filter out OCR artifacts
        # like "BEFORE", "CAR" that happen to appear on verified pages
        return frozenset(c for c, count in counter.items() if count >= 3)

    def _get_known_codes(self) -> frozenset[str]:
        """Lazy-load the known-codes whitelist."""
        if self._known_codes is None:
            self._known_codes = self._build_known_codes()
        return self._known_codes

    def _is_known_or_code_like(self, text: str) -> bool:
        """Check whitelist first, then fall back to blacklist logic."""
        if not text or len(text) > 25:
            return False
        upper = text.strip().upper()
        # Whitelist: known codes from standalone exemplars
        if upper in self._get_known_codes():
            return True
        # Fall back to blacklist-based check
        return self._is_code_like(text)

    @classmethod
    def _is_code_like(cls, text: str) -> bool:
        """Return True if *text* looks like a schedule/rider code, not a phrase."""
        if not text or len(text) > 25:
            return False
        lower = text.lower()
        # Split on whitespace AND hyphens so "Out-of-Service" becomes
        # ["out", "of", "service"], all caught by _NON_CODE_WORDS
        tokens = [t.strip(".,;:()[]{}!?\"'") for t in lower.replace("-", " ").split()]
        tokens = [t for t in tokens if t]  # remove empties
        # Reject if any token is a common English word
        if any(t in cls._NON_CODE_WORDS for t in tokens):
            return False
        # Accept patterns like "SRR", "SRR-5", "RIDER SRR-5", "RES-14"
        word_count = len(tokens)
        if word_count <= 3:
            return True
        return False

    # ------------------------------------------------------------------
    # Text-based document boundary detection
    # ------------------------------------------------------------------

    # Schedule heading patterns from Duke website exemplars:
    #   "SCHEDULE RES-14", "SCHEDULE RT (NC)", "SCHEDULE LGS", "SCHEDULE SGS-72"
    # Avoid matching "this Schedule is", "Schedule is available", etc.
    # by requiring the code portion to contain alphanumeric + optional dash.
    # Use [^\S\r\n] (horizontal whitespace only) to prevent matching across lines.
    _RE_SCHEDULE_HEADING: re.Pattern = re.compile(
        r'\bSCHEDULE[^\S\r\n]+([A-Z]{2,}(?:[^\S\r\n-]*[A-Z0-9]+)*)'
        r'(?=[^\S\r\n]|$|\.|,|\))',
        re.IGNORECASE,
    )
    # Rider heading patterns: "RIDER SRR-5", "RIDER EDIT4", etc.
    _RE_RIDER_HEADING: re.Pattern = re.compile(
        r'\bRIDER[^\S\r\n]+([A-Z]{2,}(?:[^\S\r\n-]*[A-Z0-9]+)*)'
        r'(?=[^\S\r\n]|$|\.|,|\))',
        re.IGNORECASE,
    )
    # Leaf number headers: "Original Leaf No. 500", "First Revised Leaf No. 15",
    # "Fiftieth Revised Leaf No. 15"
    _RE_LEAF_HEADER: re.Pattern = re.compile(
        r'(?:'
        r'(?:Original|First|Second|Third|Fourth|Fifth|Sixth'
        r'|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth'
        r'|Thirteenth|Fourteenth|Fifteenth|Sixteenth'
        r'|Seventeenth|Eighteenth|Nineteenth|Twentieth'
        r'|Twenty[-\s]First|Twenty[-\s]Second|Twenty[-\s]Third'
        r'|Twenty[-\s]Fourth|Twenty[-\s]Fifth|Twenty[-\s]Sixth'
        r'|Twenty[-\s]Seventh|Twenty[-\s]Eighth|Twenty[-\s]Ninth'
        r'|Thirtieth|Thirty[-\s]First|Thirty[-\s]Second'
        r'|Thirty[-\s]Third|Thirty[-\s]Fourth|Thirty[-\s]Fifth'
        r'|Thirty[-\s]Sixth|Thirty[-\s]Seventh|Thirty[-\s]Eighth'
        r'|Thirty[-\s]Ninth|Fortieth|Forty[-\s]First|Forty[-\s]Second'
        r'|Forty[-\s]Third|Forty[-\s]Fourth|Forty[-\s]Fifth'
        r'|Forty[-\s]Sixth|Forty[-\s]Seventh|Forty[-\s]Eighth'
        r'|Forty[-\s]Ninth|Fiftieth|Fifty[-\s]First|Fifty[-\s]Second'
        r'|Fifty[-\s]Third|Fifty[-\s]Fourth|Fifty[-\s]Fifth'
        r'|Fifty[-\s]Sixth|Fifty[-\s]Seventh|Fifty[-\s]Eighth'
        r'|Fifty[-\s]Ninth|Sixtieth|Sixty[-\s]First|Sixty[-\s]Second'
        r'|Sixty[-\s]Third|Sixty[-\s]Fourth|Sixty[-\s]Fifth'
        r'|Sixty[-\s]Sixth|Sixty[-\s]Seventh|Sixty[-\s]Eighth'
        r'|Sixty[-\s]Ninth|Seventieth|Seventy[-\s]First'
        r'|Seventy[-\s]Second|Seventy[-\s]Third|Seventy[-\s]Fourth'
        r'|Seventy[-\s]Fifth|Seventy[-\s]Sixth|Seventy[-\s]Seventh'
        r'|Seventy[-\s]Eighth|Seventy[-\s]Ninth|Eightieth'
        r'|Eighty[-\s]First|Eighty[-\s]Second|Eighty[-\s]Third'
        r'|Eighty[-\s]Fourth|Eighty[-\s]Fifth|Eighty[-\s]Sixth'
        r'|Eighty[-\s]Seventh|Eighty[-\s]Eighth|Eighty[-\s]Ninth'
        r'|Ninetieth|Ninety[-\s]First|Ninety[-\s]Second'
        r'|Ninety[-\s]Third|Ninety[-\s]Fourth|Ninety[-\s]Fifth'
        r'|Ninety[-\s]Sixth|Ninety[-\s]Seventh|Ninety[-\s]Eighth'
        r'|Ninety[-\s]Ninth|One\s+Hundredth|One\s+Hundred\s+First'
        r'|One\s+Hundred\s+Second|One\s+Hundred\s+Third'
        r'|One\s+Hundred\s+Fourth|One\s+Hundred\s+Fifth'
        r'|One\s+Hundred\s+Sixth|One\s+Hundred\s+Seventh'
        r'|One\s+Hundred\s+Eighth|One\s+Hundred\s+Ninth'
        r'|One\s+Hundred\s+Tenth|One\s+Hundred\s+Eleventh'
        r'|One\s+Hundred\s+Twelfth|One\s+Hundred\s+Thirteenth'
        r'|\d+(?:st|nd|rd|th)'
        r')\s+'
        r'(?:Revised\s+)?Leaf\s*No\.?\s*(\d+)'
        r'|Original\s+Leaf\s*No\.?\s*(\d+)'
        r')',
        re.IGNORECASE,
    )
    # Company header that often marks the top of a new document page
    _RE_COMPANY_HEADER: re.Pattern = re.compile(
        r'Duke\s+Energy\s+(Carolinas|Progress),\s+LLC',
        re.IGNORECASE,
    )
    # PPA/contract "schedule" references (NOT rate schedules):
    #   "Operational Milestone Schedule", "project schedule", "Planned Outage schedule"
    _RE_NON_RATE_SCHEDULE: re.Pattern = re.compile(
        r'\b(Milestone|Outage|project|Planned|construction|procurement'
        r'|development|operations?|closing|delivery|installation'
        r'|payment|work|testing)\s+[Ss]chedule\b',
    )
    # Superseding language: "Superseding NC Schedule RES-79"
    _RE_SUPERSEDING_SCHEDULE: re.Pattern = re.compile(
        r'Superseding\s+(?:NC\s+)?(?:Schedule|Rider)\s+([A-Z]{2,}(?:[\s-]*\w+)*)',
        re.IGNORECASE,
    )
    # Electricity tariff identifier: "Electricity No. 4"
    _RE_ELECTRICITY_NO: re.Pattern = re.compile(
        r'Electricity\s+No\.\s*\d+',
        re.IGNORECASE,
    )

    # ------------------------------------------------------------------
    # Hearing / testimony / workpaper detection
    # ------------------------------------------------------------------
    # These patterns identify pages from hearing exhibits, testimony
    # transcripts, and cost-of-service workpapers — documents that are
    # often misclassified as RATE_SCHEDULE because they contain dollar
    # amounts and the word "Schedule" in running text.

    # Q&A testimony format: "Q. " or "A. " at line start followed by
    # testimony-like content (all-caps questions, "Yes"/"No", witness
    # self-reference).  Tariff enumerations also use "A." / "B." markers
    # so a single match is not enough — _detect_hearing_signals requires
    # multiple Q/A hits on the same page (see _MIN_QA_HITS).
    _RE_TESTIMONY_QA: re.Pattern = re.compile(
        r'^\s*[QA]\s*\.\s+\w', re.MULTILINE | re.IGNORECASE,
    )
    # Minimum number of Q/A matches on a single page to count as testimony.
    # Below this threshold, matches are likely enumeration markers (A., B.,
    # C.) rather than actual testimony Q&A exchanges.
    _MIN_QA_HITS: int = 3
    # Testimony / witness statement headers
    _RE_TESTIMONY_HEADER: re.Pattern = re.compile(
        r'(?:DIRECT|PRE[\s-]?FILED|REBUTTAL|SURREBUTTAL|SUPPLEMENTAL)\s+TESTIMONY'
        r'|TESTIMONY\s+OF\s+[A-Z]',
        re.IGNORECASE,
    )
    # Witness self-identification
    _RE_WITNESS_INTRO: re.Pattern = re.compile(
        r'(?:My\s+name\s+is\s+[A-Z]|PLEASE\s+STATE\s+YOUR\s+NAME'
        r'|YOUR\s+NAME\s+AND\s+BUSINESS\s+ADDRESS)',
        re.IGNORECASE,
    )
    # Workpaper / exhibit labels (not tariff exhibits)
    _RE_WORKPAPER: re.Pattern = re.compile(
        r'(?:WORKPAPER|Workpaper|WORK\s+PAPER)\s*\d',
    )
    # Hearing / procedural headers — strong signals that are rarely
    # found on real tariff rate schedule pages.
    _RE_HEARING_HEADER: re.Pattern = re.compile(
        r'BEFORE\s+THE\s+NORTH\s+CAROLINA\s+UTILITIES\s+COMMISSION'
        r'|APPEARANCE\s+SLIP'
        r'|APPEARANCES\s*(?:\(Cont\'?d?\.?\))?:'
        r'|AFFIDAVIT\s+OF\s+[A-Z]'
        r'|VERIFICATION\s+OF\s+[A-Z]'
        r'|TRANSCRIPT\s+OF\s+(?:PROCEEDINGS|HEARING)',
        re.IGNORECASE,
    )
    # Number-only schedule references (workpaper schedules, not rate schedules)
    # Matches "Schedule 1", "Schedule 10", "Schedule 1 Page 1", etc.
    # Only triggers when at least 30% of section pages match AND combined
    # with other hearing signals.
    _RE_GENERIC_SCHEDULE_NUMBER: re.Pattern = re.compile(
        r'\bSchedule\s+\d{1,2}\b',
        re.IGNORECASE,
    )

    @classmethod
    def _detect_hearing_signals(cls, page_text: str) -> dict[str, bool]:
        """Detect hearing / testimony / workpaper signals in page text.

        Returns a dict with boolean flags.  An empty dict means no hearing
        signals were detected on this page.
        """
        if not page_text:
            return {}

        signals: dict[str, bool] = {}

        qa_hits = len(cls._RE_TESTIMONY_QA.findall(page_text))
        if qa_hits >= cls._MIN_QA_HITS:
            signals["testimony_qa"] = True
        if cls._RE_TESTIMONY_HEADER.search(page_text):
            signals["testimony_header"] = True
        if cls._RE_WITNESS_INTRO.search(page_text):
            signals["witness_intro"] = True
        if cls._RE_WORKPAPER.search(page_text):
            signals["workpaper"] = True
        if cls._RE_HEARING_HEADER.search(page_text):
            signals["hearing_header"] = True
        if cls._RE_GENERIC_SCHEDULE_NUMBER.search(page_text):
            signals["generic_schedule_number"] = True

        return signals

    @classmethod
    def _score_hearing_likelihood(
        cls, pages: list[_PageSignals],
    ) -> float:
        """Return 0.0–1.0 likelihood that a section is a hearing exhibit.

        High scores indicate testimony transcripts, workpapers, or
        procedural filings — not rate schedules.
        """
        if not pages:
            return 0.0

        n = len(pages)
        qa_pages = 0
        testimony_pages = 0
        witness_pages = 0
        workpaper_pages = 0
        hearing_header_pages = 0
        generic_sched_pages = 0

        for pg in pages:
            sigs = cls._detect_hearing_signals(pg.text_content)
            if not sigs:
                continue
            if sigs.get("testimony_qa"):
                qa_pages += 1
            if sigs.get("testimony_header"):
                testimony_pages += 1
            if sigs.get("witness_intro"):
                witness_pages += 1
            if sigs.get("workpaper"):
                workpaper_pages += 1
            if sigs.get("hearing_header"):
                hearing_header_pages += 1
            if sigs.get("generic_schedule_number"):
                generic_sched_pages += 1

        # Testimony Q&A is the strongest signal — a single page with Q&A
        # format is almost certainly a hearing transcript.
        if qa_pages > 0:
            return min(1.0, 0.5 + 0.3 * (qa_pages / n))

        # Workpaper labels are very strong signals
        if workpaper_pages > 0:
            return min(1.0, 0.4 + 0.3 * (workpaper_pages / n))

        # Combined weaker signals — require at least 2 different signal
        # types to avoid false positives from single-pattern matches
        signal_types = 0
        score = 0.0
        if testimony_pages > 0:
            signal_types += 1
            score += 0.3
        if witness_pages > 0:
            signal_types += 1
            score += 0.3
        if hearing_header_pages > 0:
            signal_types += 1
            score += 0.2
        if generic_sched_pages > n * 0.3:
            signal_types += 1
            score += 0.2

        # Require at least 2 signal types for non-QA/non-workpaper detection
        if signal_types < 2:
            return 0.0

        return min(1.0, score)

    @classmethod
    def _detect_text_boundary_signals(
        cls, page_text: str,
    ) -> dict[str, Any]:
        """Scan page text for document boundary patterns.

        Returns a dict with detected signals, or an empty dict if no
        boundary-relevant patterns are found.
        """
        if not page_text:
            return {}

        signals: dict[str, Any] = {}

        # Check for non-rate "schedule" references (PPA milestones, etc.)
        # — these should NOT trigger a boundary split
        if cls._RE_NON_RATE_SCHEDULE.search(page_text):
            signals["has_non_rate_schedule_ref"] = True
            # Don't short-circuit — still check for real schedule headings
            # in case this page has both PPA milestones and rate schedules

        # SCHEDULE heading with a code
        sched_match = cls._RE_SCHEDULE_HEADING.search(page_text)
        if sched_match:
            code = sched_match.group(1).strip()
            # Filter out non-rate "schedule" references
            if cls._is_code_like(code) or len(code) <= 15:
                signals["schedule_heading_text"] = code
                signals["is_document_boundary"] = True

        # Superseding language
        supersede_match = cls._RE_SUPERSEDING_SCHEDULE.search(page_text)
        if supersede_match:
            code = supersede_match.group(1).strip()
            if cls._is_code_like(code) or len(code) <= 15:
                signals["superseding_schedule_code"] = code
                signals["is_document_boundary"] = True

        # RIDER heading
        rider_match = cls._RE_RIDER_HEADING.search(page_text)
        if rider_match:
            code = rider_match.group(1).strip()
            if cls._is_code_like(code) or len(code) <= 15:
                signals["rider_heading_text"] = code
                signals["is_document_boundary"] = True

        # Leaf number header
        leaf_match = cls._RE_LEAF_HEADER.search(page_text)
        if leaf_match:
            signals["leaf_header_text"] = leaf_match.group(1)

        # Company header
        if cls._RE_COMPANY_HEADER.search(page_text):
            signals["has_company_header"] = True

        # Electricity No. identifier (strong tariff signal)
        if cls._RE_ELECTRICITY_NO.search(page_text):
            signals["has_electricity_no"] = True
            signals["is_document_boundary"] = True

        return signals

    @classmethod
    def _collect_text_boundaries(
        cls,
        section_pages: list[_PageSignals],
        section_start_page: int,
    ) -> set[int]:
        """Return set of page numbers within section_pages that signal a new
        document boundary based on text patterns.

        Used as additional split points in _refine_boundaries for sections
        that are too large (> 8 pages).
        """
        boundary_pages: list[int] = []
        seen_codes: set[str] = set()

        for i, pg in enumerate(section_pages):
            text_signals = cls._detect_text_boundary_signals(pg.text_content)
            if not text_signals.get("is_document_boundary"):
                continue

            # Collect the code from any source
            code = (
                text_signals.get("schedule_heading_text")
                or text_signals.get("superseding_schedule_code")
                or text_signals.get("rider_heading_text")
                or ""
            ).upper().strip()

            # If we've seen this code before in the same section, it's not
            # a new boundary (the same document may have the code on every page)
            if code and code in seen_codes:
                continue
            if code:
                seen_codes.add(code)

            # Only use text boundaries when metadata also weakly supports it
            # (reduces false splits on "schedule" in running text)
            has_meta_support = (
                pg.has_schedule_heading
                or pg.has_leaf_header
                or pg.has_effective_date_phrase
                or bool(pg.extracted_schedule_codes)
                or bool(pg.extracted_leaf_nos)
            )

            if len(section_pages) > 15:
                # Aggressive: any strong text signal is enough
                boundary_pages.append(i)
            elif code and has_meta_support:
                boundary_pages.append(i)
            elif len(section_pages) > 8 and (
                text_signals.get("has_electricity_no")
                or text_signals.get("has_company_header")
            ):
                boundary_pages.append(i)

        return set(boundary_pages)

    def _make_sub_section(
        self,
        parent: SectionBundle,
        pages: list[_PageSignals],
    ) -> SectionBundle:
        """Create a sub-section from a parent section and a page subset."""
        leaf_nos: list[str] = []
        sched_codes: list[str] = []
        titles: list[str] = []
        for pg in pages:
            for ln in (pg.extracted_leaf_nos or []):
                if ln and ln not in leaf_nos:
                    leaf_nos.append(ln)
            for sc in (pg.extracted_schedule_codes or []):
                if sc and sc not in sched_codes and self._is_known_or_code_like(sc):
                    sched_codes.append(sc)

        return SectionBundle(
            source_pdf=parent.source_pdf,
            section_index=0,  # will be re-indexed by caller
            start_page=pages[0].page_number,
            end_page=pages[-1].page_number,
            leaf_numbers=leaf_nos,
            schedule_codes=sched_codes,
            detected_titles=titles,
            evidence_log=list(parent.evidence_log) + [{
                "source": "boundary_refinement",
                "original_start": parent.start_page,
                "original_end": parent.end_page,
                "split_pages": f"{pages[0].page_number}-{pages[-1].page_number}",
                "page_count": len(pages),
            }],
        )

    def _merge_adjacent_similar(
        self,
        sections: list[SectionBundle],
    ) -> list[SectionBundle]:
        """Merge adjacent sections that share leaf numbers or schedule codes.

        Within the same parent span, any overlap triggers a merge.  Across
        different spans, a higher bar is required (≥3 shared leaves or ≥50%
        overlap) to avoid collapsing genuinely distinct documents.
        """
        if len(sections) <= 1:
            return sections

        def _parent_span_index(bundle: SectionBundle) -> int | None:
            for entry in bundle.evidence_log:
                if entry.get("source") == "ncuc_span_artifacts":
                    return entry.get("span_index")
            return None

        merged: list[SectionBundle] = []
        current = sections[0]

        for next_sec in sections[1:]:
            current_leaves = set(current.leaf_numbers)
            next_leaves = set(next_sec.leaf_numbers)
            leaves_overlap = bool(current_leaves & next_leaves)
            codes_overlap = bool(
                set(current.schedule_codes) & set(next_sec.schedule_codes)
            )
            adjacent = current.end_page + 1 >= next_sec.start_page
            same_span = _parent_span_index(current) == _parent_span_index(next_sec)
            same_type = current.section_type == next_sec.section_type

            # Compute overlap significance for cross-span merges
            shared_leaf_count = len(current_leaves & next_leaves) if leaves_overlap else 0
            min_leaf_set = min(len(current_leaves), len(next_leaves)) if leaves_overlap else 0
            significant_overlap = (
                shared_leaf_count >= 3
                or (min_leaf_set > 0 and shared_leaf_count / max(min_leaf_set, 1) >= 0.5)
            )

            should_merge = False
            if adjacent and same_span and (leaves_overlap or codes_overlap):
                # Within same span: any overlap is enough
                should_merge = True
            elif adjacent and not same_span and same_type and codes_overlap:
                # Cross-span with shared schedule codes: merge.
                # Leaf-only overlap across spans is a catalog artifact —
                # in a tariff catalog, different schedules legitimately
                # share leaf references (e.g., all DEP schedules reference
                # leaf 500). Code overlap means they're the same schedule.
                should_merge = True

            if should_merge:
                current.end_page = max(current.end_page, next_sec.end_page)
                if leaves_overlap:
                    current.leaf_numbers = list(current_leaves | next_leaves)
                if codes_overlap:
                    current.schedule_codes = list(
                        set(current.schedule_codes) | set(next_sec.schedule_codes)
                    )
                merge_reason = (
                    "shared_leaf_numbers" if leaves_overlap else "shared_schedule_codes"
                )
                if not same_span:
                    merge_reason = f"cross_span_{merge_reason}"
                current.evidence_log.append({
                    "source": "section_merge",
                    "merged_section_start": next_sec.start_page,
                    "merged_section_end": next_sec.end_page,
                    "reason": merge_reason,
                    "shared_leaf_count": shared_leaf_count,
                })
            else:
                merged.append(current)
                current = next_sec

        merged.append(current)

        for i, s in enumerate(merged):
            s.section_index = i
        return merged

    # ------------------------------------------------------------------
    # Internal — classification and scoring
    # ------------------------------------------------------------------

    def _classify_and_score(
        self,
        sections: list[SectionBundle],
        pages: list[_PageSignals],
        spans: list[dict[str, Any]],
    ) -> list[SectionBundle]:
        """Classify section types and compute confidence scores."""
        pages_by_num = {p.page_number: p for p in pages}

        for section in sections:
            section_pages = [
                pages_by_num[pn]
                for pn in range(section.start_page, section.end_page + 1)
                if pn in pages_by_num
            ]
            if not section_pages:
                continue

            # Classify type
            section.section_type = self._classify_section_type(section, section_pages)

            # Log hearing-exhibit detection when it caused reclassification
            hearing_score = self._score_hearing_likelihood(section_pages)
            if hearing_score >= 0.5:
                leaf_count = sum(1 for p in section_pages if p.has_leaf_header)
                if leaf_count == 0:
                    section.evidence_log.append({
                        "source": "hearing_exhibit_detector",
                        "hearing_score": round(hearing_score, 2),
                        "action": "reclassified_to_procedural",
                        "rationale": (
                            "testimony/workpaper/hearing markers detected "
                            "without leaf headers — not a rate schedule"
                        ),
                    })

            # Clear rate-related metadata from non-rate sections — span-level
            # leaf numbers often bleed into cover letters and TOC pages
            if section.section_type not in RATE_SECTION_TYPES:
                if section.section_type in (SectionType.COVER_LETTER, SectionType.TABLE_OF_CONTENTS):
                    section.leaf_numbers = []
                    section.schedule_codes = []
                    section.rider_codes = []

            # Leaf-header gate: real tariff/rate/rider documents always have
            # at least one page with a leaf header. Sections classified as
            # RATE_SCHEDULE or RIDER that have schedule headings but ZERO
            # leaf-header pages are procedural exhibits, testimony, or
            # cost-of-service studies misclassified because they contain "$"
            # and the word "Schedule" in running text.
            #
            # We check three leaf-header signals because page_miner's metadata
            # regex requires "Leaf No." (with space) but OCR often produces
            # "LeafNo." (no space) on real tariff pages.
            if section.section_type in RATE_SECTION_TYPES:
                sched_count = sum(1 for p in section_pages if p.has_schedule_heading)
                leaf_meta_count = sum(1 for p in section_pages if p.has_leaf_header)
                revised_meta_count = sum(1 for p in section_pages if p.has_revised_header)
                text_leaf_count = sum(
                    1 for p in section_pages
                    if self._RE_LEAF_HEADER.search(p.text_content or "")
                )
                any_leaf_signal = (
                    leaf_meta_count > 0
                    or revised_meta_count > 0
                    or text_leaf_count > 0
                )
                # Schedule codes are strong evidence this IS a real rate schedule
                # or rider — don't reclassify just because the leaf header is
                # OCR-broken or in a nonstandard format.
                has_known_codes = bool(section.schedule_codes or section.rider_codes)
                if not any_leaf_signal and sched_count > 0 and not has_known_codes:
                    # Check whether pages contain actual dollar/cent values.
                    # If a section has both schedule headings AND rate values,
                    # the leaf header is likely OCR-broken or in a nonstandard
                    # format — don't reclassify just because the header is missing.
                    has_rate_values = any(
                        "$" in (p.text_content or "") or "¢" in (p.text_content or "")
                        for p in section_pages
                    )
                    if has_rate_values:
                        # Keep as rate_schedule/rider — dollar values are
                        # strong evidence this is a real rate section.
                        section.evidence_log.append({
                            "source": "leaf_header_gate",
                            "action": "retain_dollar_value",
                            "leaf_header_pages": 0,
                            "schedule_heading_pages": sched_count,
                            "has_rate_values": True,
                            "rationale": (
                                "no leaf header or known codes, but schedule "
                                "headings + dollar values — keeping rate_schedule"
                            ),
                        })
                    else:
                        section.evidence_log.append({
                            "source": "leaf_header_gate",
                            "action": "reclassify",
                            "leaf_header_pages": 0,
                            "schedule_heading_pages": sched_count,
                            "has_rate_values": False,
                            "rationale": (
                                "schedule headings but zero leaf-header pages, "
                                "no known codes, and no dollar values"
                            ),
                        })
                        # Reclassify based on remaining signals
                        n = len(section_pages)
                        proc_density = (
                            sum(p.procedural_vocab_density for p in section_pages) / n
                            if n else 0.0
                        )
                        if section.start_page <= 3:
                            section.section_type = SectionType.COVER_LETTER
                        elif proc_density > 0.03:
                            section.section_type = SectionType.PROCEDURAL
                        else:
                            section.section_type = SectionType.TERMS_CONDITIONS
                        section.evidence_log[-1]["new_type"] = (
                            section.section_type.value
                        )
                elif not any_leaf_signal and sched_count > 0 and has_known_codes:
                    # Has schedule codes but no leaf header — unusual but valid.
                    # Record evidence without reclassifying.
                    section.evidence_log.append({
                        "source": "leaf_header_gate",
                        "action": "retain",
                        "leaf_header_pages": 0,
                        "schedule_heading_pages": sched_count,
                        "known_codes": list(section.schedule_codes),
                        "rationale": "no leaf header but has known schedule codes — keeping rate_schedule",
                    })

            # Compute confidence
            section.overall_confidence = self._score_section(
                section, section_pages, spans
            )

            # Add signal agreement evidence
            section.evidence_log.append(self._build_signal_evidence(section_pages))

        return sections

    # ------------------------------------------------------------------
    # Document-level classification (derived from section composition)
    # ------------------------------------------------------------------

    def _derive_document_type(
        self,
        source_pdf: str,
        sections: list[SectionBundle],
        pages: list[_PageSignals],
    ) -> tuple[str, float, dict[str, Any]]:
        """Derive a document-level classification from the section mix.

        Returns (doc_type, confidence, evidence).
        """
        n = len(sections)
        if n == 0:
            return DocumentType.UNKNOWN, 0.0, {
                "source": "section_composition",
                "reason": "no sections",
            }

        # Count section types
        type_counts: dict[str, int] = {}
        for s in sections:
            key = s.section_type.value if hasattr(s.section_type, 'value') else str(s.section_type)
            type_counts[key] = type_counts.get(key, 0) + 1

        n_rate = type_counts.get("rate_schedule", 0) + type_counts.get("rider", 0)
        n_procedural = type_counts.get("procedural", 0)
        n_cover = type_counts.get("cover_letter", 0)
        n_terms = type_counts.get("terms_conditions", 0)
        n_unknown = type_counts.get("unknown", 0)
        n_toc = type_counts.get("table_of_contents", 0)

        # Leaf header presence across the document
        leaf_pages = sum(1 for p in pages if p.has_leaf_header)
        has_leaf_headers = leaf_pages > 0

        # Code quality: distinct real schedule codes across sections
        all_codes: set[str] = set()
        for s in sections:
            all_codes.update(s.schedule_codes)
            all_codes.update(s.rider_codes)
        distinct_codes = len(all_codes)

        # Compute proportions
        p_rate = n_rate / n
        p_procedural = n_procedural / n
        p_cover = n_cover / n
        p_terms = n_terms / n

        # Build evidence
        evidence: dict[str, Any] = {
            "source": "section_composition",
            "total_sections": n,
            "section_type_counts": type_counts,
            "distinct_schedule_codes": distinct_codes,
            "leaf_header_pages": leaf_pages,
            "has_leaf_headers": has_leaf_headers,
        }

        # --- Classification logic ---

        # Hearing exhibit: procedural-dominant, no leaf headers
        if p_procedural >= _DOC_TYPE_PROCEDURAL_DOMINANCE and not has_leaf_headers:
            conf = min(0.95, 0.5 + p_procedural)
            evidence["reason"] = "procedural-dominant, no leaf headers"
            return DocumentType.HEARING_EXHIBIT, round(conf, 2), evidence

        # DSM/EE evaluation: rate_schedule sections but no leaf headers and no real codes
        if p_rate >= 0.3 and not has_leaf_headers and distinct_codes <= 1:
            conf = 0.65 if n_rate >= 3 else 0.45
            evidence["reason"] = "rate-like sections without leaf headers or distinct codes"
            return DocumentType.DSM_EE_EVALUATION, conf, evidence

        # Compliance tariff bundle: many rate sections with distinct codes
        if n_rate >= _DOC_TYPE_MIN_RATE_FOR_BUNDLE and distinct_codes >= 2 and has_leaf_headers:
            # Confidence scales with code diversity and leaf header density
            leaf_ratio = leaf_pages / max(1, len(pages))
            code_bonus = min(0.2, (distinct_codes - 2) * 0.05)
            conf = min(0.95, 0.55 + leaf_ratio * 0.2 + code_bonus)
            evidence["reason"] = (
                f"{n_rate} rate/rider sections with {distinct_codes} distinct codes, "
                f"leaf headers present"
            )
            return DocumentType.COMPLIANCE_TARIFF_BUNDLE, round(conf, 2), evidence

        # Single rate schedule: 1-2 rate sections, leaf headers
        if 1 <= n_rate <= 2 and has_leaf_headers and n_rate >= 0.5 * n:
            conf = 0.70 if distinct_codes >= 1 else 0.50
            evidence["reason"] = f"{n_rate} rate/rider sections with leaf headers"
            return DocumentType.SINGLE_RATE_SCHEDULE, conf, evidence

        # Cover letter package
        if p_cover >= _DOC_TYPE_COVER_LETTER_DOMINANCE:
            conf = min(0.90, 0.40 + p_cover)
            evidence["reason"] = "cover-letter-dominant"
            return DocumentType.COVER_LETTER_PACKAGE, round(conf, 2), evidence

        # Terms and conditions
        if p_terms >= _DOC_TYPE_TERMS_DOMINANCE:
            conf = min(0.85, 0.40 + p_terms)
            evidence["reason"] = "terms-and-conditions-dominant"
            return DocumentType.TERMS_AND_CONDITIONS, round(conf, 2), evidence

        # Application filing: procedural + cover + some rate
        if p_procedural >= 0.25 and p_cover >= 0.15 and n_rate >= 1:
            conf = 0.55
            evidence["reason"] = "procedural + cover_letter + rate mix"
            return DocumentType.APPLICATION_FILING, conf, evidence

        # Mixed compliance: 2+ types in proportion
        n_types = sum(1 for c in (n_rate, n_procedural, n_cover, n_terms) if c > 0)
        if n_types >= 2:
            conf = 0.35
            evidence["reason"] = f"{n_types} distinct section types present"
            return DocumentType.MIXED_COMPLIANCE, conf, evidence

        # Fallback
        evidence["reason"] = "unable to classify from section composition"
        return DocumentType.UNKNOWN, 0.1, evidence

    def _upsert_document_classification(
        self,
        source_pdf: str,
        doc_type: str,
        confidence: float,
        evidence: dict[str, Any],
    ) -> None:
        """Write document-level classification to ``document_identity``.

        Only touches ``inferred_doc_type``, ``overall_confidence``,
        and ``evidence_log_json`` — other columns are left unchanged.
        Creates the document_identity row if it doesn't exist yet.
        """
        conn = sqlite3.connect(str(self._db_path))
        try:
            # Ensure document_identity table exists
            from .document_identity import ensure_schema as ensure_id_schema
            ensure_id_schema(self._db_path)

            # Read existing evidence log
            existing = conn.execute(
                "SELECT evidence_log_json FROM document_identity WHERE source_pdf = ?",
                (source_pdf,),
            ).fetchone()

            if existing:
                try:
                    ev_log = json.loads(existing[0] or "[]")
                except (json.JSONDecodeError, TypeError):
                    ev_log = []
                ev_log.append(evidence)
                conn.execute(
                    """UPDATE document_identity
                       SET inferred_doc_type = ?,
                           overall_confidence = MAX(overall_confidence, ?),
                           evidence_log_json = ?,
                           last_updated = datetime('now')
                       WHERE source_pdf = ?""",
                    (doc_type, confidence, json.dumps(ev_log), source_pdf),
                )
            else:
                # Row doesn't exist — insert a minimal one
                conn.execute(
                    """INSERT INTO document_identity
                       (source_pdf, inferred_doc_type, overall_confidence,
                        evidence_log_json, last_updated, created_at)
                       VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))""",
                    (source_pdf, doc_type, confidence, json.dumps([evidence])),
                )

            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Section type classification
    # ------------------------------------------------------------------

    @classmethod
    def _classify_section_type(
        cls,
        bundle: SectionBundle,
        pages: list[_PageSignals],
    ) -> SectionType:
        """Classify a section based on page signal prevalence."""
        n = len(pages)
        leaf_count = sum(1 for p in pages if p.has_leaf_header)
        schedule_count = sum(1 for p in pages if p.has_schedule_heading)
        dollar_count = sum(1 for p in pages if "$" in (p.text_content or ""))
        cent_count = sum(1 for p in pages if "¢" in (p.text_content or ""))
        toc_count = sum(1 for p in pages if p.has_toc_page)
        proc_density = (
            sum(p.procedural_vocab_density for p in pages) / n if n else 0.0
        )
        tariff_density = (
            sum(p.tariff_vocab_density for p in pages) / n if n else 0.0
        )
        has_rate_values = dollar_count > 0 or cent_count > 0

        if toc_count > n * 0.3:
            return SectionType.TABLE_OF_CONTENTS
        if proc_density > 0.05:
            return SectionType.PROCEDURAL

        # Hearing-exhibit detection: testimony transcripts, workpapers,
        # and procedural filings often contain dollar amounts and the
        # word "Schedule" in running text, causing false RATE_SCHEDULE
        # classifications.  Check for hearing signals and reclassify
        # when no leaf headers are present.
        hearing_score = cls._score_hearing_likelihood(pages)
        if hearing_score >= 0.5 and leaf_count == 0:
            return SectionType.PROCEDURAL

        # Cover letter: at document start, no rate values, no leaf headers,
        # low tariff vocabulary — even if one page has a schedule_heading hit
        # (which may be a false positive like "CERTIFICATE OF SERVICE").
        if (
            bundle.start_page <= 2
            and not has_rate_values
            and leaf_count == 0
            and tariff_density < 0.01
        ):
            return SectionType.COVER_LETTER
        if schedule_count > 0 and has_rate_values:
            return SectionType.RATE_SCHEDULE
        if leaf_count > 0 and schedule_count > 0 and not has_rate_values:
            return SectionType.TERMS_CONDITIONS
        if leaf_count > 0 and has_rate_values:
            return SectionType.RIDER
        if leaf_count == 0 and schedule_count == 0 and not has_rate_values:
            return SectionType.COVER_LETTER
        # Has rate values but no leaf/schedule headers — likely contract terms
        if has_rate_values and leaf_count == 0 and schedule_count == 0:
            return SectionType.TERMS_CONDITIONS
        return SectionType.UNKNOWN

    def _score_section(
        self,
        bundle: SectionBundle,
        pages: list[_PageSignals],
        spans: list[dict[str, Any]],
    ) -> float:
        """Compute additive confidence score for a section."""
        score = 0.0

        # Leaf numbers consistent across section
        if bundle.leaf_numbers:
            score += WEIGHT_SECTION_LEAF_MATCH

        # Schedule or rider codes present
        if bundle.schedule_codes or bundle.rider_codes:
            score += WEIGHT_SECTION_CODE_MATCH

        # Section type is unambiguous
        if bundle.section_type != SectionType.UNKNOWN:
            score += WEIGHT_SECTION_TYPE_CLEAR

        # Span doc_type agrees with section type
        section_is_rate = bundle.section_type in RATE_SECTION_TYPES
        for span in spans:
            if span["start_page"] <= bundle.start_page and span["end_page"] >= bundle.end_page:
                span_is_tariff = span["doc_type"] == "tariff"
                if section_is_rate == span_is_tariff:
                    score += WEIGHT_SECTION_SPAN_AGREE
                break

        # Rate values ($ or ¢) present on pages
        dollar_count = sum(1 for p in pages if "$" in (p.text_content or ""))
        cent_count = sum(1 for p in pages if "¢" in (p.text_content or ""))
        if dollar_count >= 2 or cent_count >= 2:
            score += WEIGHT_SECTION_RATE_VALUES

        return min(1.0, round(score, 3))

    @staticmethod
    def _build_signal_evidence(pages: list[_PageSignals]) -> dict[str, Any]:
        """Build a page-signal-agreement evidence entry."""
        n = len(pages)
        return {
            "source": "page_signal_agreement",
            "total_pages": n,
            "leaf_header_pages": sum(1 for p in pages if p.has_leaf_header),
            "schedule_heading_pages": sum(1 for p in pages if p.has_schedule_heading),
            "revised_header_pages": sum(1 for p in pages if p.has_revised_header),
            "dollar_sign_pages": sum(1 for p in pages if "$" in (p.text_content or "")),
            "cent_sign_pages": sum(1 for p in pages if "¢" in (p.text_content or "")),
            "mean_tariff_vocab_density": round(
                sum(p.tariff_vocab_density for p in pages) / n, 4
            ) if n else 0,
            "mean_procedural_vocab_density": round(
                sum(p.procedural_vocab_density for p in pages) / n, 4
            ) if n else 0,
            "mean_numeric_density": round(
                sum(p.numeric_density for p in pages) / n, 4
            ) if n else 0,
        }
