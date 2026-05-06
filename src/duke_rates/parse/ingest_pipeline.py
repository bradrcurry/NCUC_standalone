"""
Tiered PDF ingest pipeline for NCUC tariff documents.

Tier 1 — Leaf/schedule splitting (leaf_splitter.py)
    Compliance tariff PDFs bundle many leaves.  Split first so each parser
    sees a focused, single-schedule segment.

Tier 2 — Heuristic parsing (schedule_parser / rider_parser)
    Fast regex-based extraction.  Works well on post-2010 native PDFs.

Tier 3 — pdfplumber table extraction
    For rate tables whose column alignment is lost in plain text extraction.
    Runs when Tier 2 produces no energy/fixed charges.

Tier 4 — LLM extraction (ai/extraction.py)
    Fallback for pre-2000 filings, unusual formats, or low-confidence heuristic
    results.  Only invoked when an API key is configured AND heuristic confidence
    is below the threshold.

Usage:
    from duke_rates.parse.ingest_pipeline import IngestPipeline, IngestResult
    from duke_rates.config import get_settings

    pipeline = IngestPipeline(get_settings())
    results = pipeline.ingest_docket(Path("data/historical/ncuc/e-2-sub-1044"))
    for r in results:
        print(r.segment.title, r.status, r.energy_charges)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duke_rates.config import Settings
from duke_rates.parse.leaf_splitter import LeafSegment, split_pdf_into_leaves
from duke_rates.parse.schedule_parser import parse_schedule_text
from duke_rates.parse.rider_parser import parse_rider_text
from duke_rates.models.parse_result import ParseStatus

logger = logging.getLogger(__name__)

# Confidence threshold below which we try the LLM
_LLM_FALLBACK_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class IngestResult:
    """Parsed output for one leaf/schedule segment."""
    segment: LeafSegment
    source_pdf: Path

    # Tier that produced results
    tier: int                          # 1=heuristic, 2=table, 3=llm, 0=no data

    # Extracted fields (from heuristic or LLM)
    schedule_code: str | None = None
    schedule_title: str | None = None
    effective_date: str | None = None
    customer_class: str | None = None
    energy_charges: list[dict] = field(default_factory=list)   # {label, rate, unit, season, period}
    fixed_charges: list[dict] = field(default_factory=list)    # {label, amount, unit}
    demand_charges: list[dict] = field(default_factory=list)   # {label, rate, unit}
    riders: list[str] = field(default_factory=list)
    table_rows: list[list[str]] = field(default_factory=list)  # raw table cells from Tier 2

    # Provenance / version chain
    supersedes: str | None = None     # what leaf/schedule this supersedes, e.g. "RES-77"
    docket_number: str | None = None  # authorizing docket, e.g. "E-2, Sub 1300"
    order_date: str | None = None     # order date, e.g. "August 18, 2023"

    # Rider summary (only set for Leaf 600 "Summary of Rider Adjustments" segments)
    rider_summary: Any | None = None  # RiderSummaryResult from rider_summary.py

    # Metadata
    status: str = "empty"             # parsed | partial | table_only | llm | empty | rider_summary
    confidence: float = 0.0
    review_flags: list[str] = field(default_factory=list)
    llm_raw: str | None = None        # raw LLM response if used
    page_range: tuple[int, int] = (0, 0)

    def has_rate_data(self) -> bool:
        return bool(self.energy_charges or self.fixed_charges or self.demand_charges or self.table_rows)

    def summary_line(self) -> str:
        parts = []
        if self.schedule_code:
            parts.append(self.schedule_code)
        if self.effective_date:
            parts.append(self.effective_date)
        if self.energy_charges:
            parts.append(f"{len(self.energy_charges)}E")
        if self.fixed_charges:
            parts.append(f"{len(self.fixed_charges)}F")
        if self.demand_charges:
            parts.append(f"{len(self.demand_charges)}D")
        if self.table_rows:
            parts.append(f"{len(self.table_rows)}rows")
        return f"[tier{self.tier}] {self.segment.title}: " + " | ".join(parts) if parts else f"[empty] {self.segment.title}"


# ---------------------------------------------------------------------------
# Main pipeline class
# ---------------------------------------------------------------------------

class IngestPipeline:
    """
    Tiered PDF ingest pipeline.

    Args:
        settings: App settings (used for LLM API key / model config).
        use_llm: Whether to allow LLM fallback (default: auto-detect from settings).
        llm_threshold: Confidence below which LLM is attempted (default 0.5).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        use_llm: bool | None = None,
        llm_threshold: float = _LLM_FALLBACK_THRESHOLD,
    ):
        self.settings = settings
        self.llm_threshold = llm_threshold
        self._llm_client = None

        if use_llm is None:
            self._use_llm = bool(settings.openai_api_key)
        else:
            self._use_llm = use_llm

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_pdf(self, path: Path) -> list[IngestResult]:
        """Ingest a single PDF, splitting into leaves if needed."""
        logger.debug("Ingest: %s", path.name)
        segments = split_pdf_into_leaves(path)
        if not segments:
            logger.debug("No segments extracted from %s", path.name)
            return []
        return [self._process_segment(seg, path) for seg in segments]

    def ingest_docket(
        self,
        docket_dir: Path,
        *,
        glob: str = "*.pdf",
    ) -> list[IngestResult]:
        """Ingest all PDFs in a docket directory."""
        results = []
        pdfs = sorted(docket_dir.glob(glob))
        logger.info("Ingesting %d PDFs from %s", len(pdfs), docket_dir.name)
        for pdf in pdfs:
            results.extend(self.ingest_pdf(pdf))
        return results

    def ingest_all_ncuc(
        self,
        ncuc_dir: Path,
        *,
        docket_filter: list[str] | None = None,
    ) -> dict[str, list[IngestResult]]:
        """
        Ingest all docket subdirectories under ncuc_dir.

        Args:
            ncuc_dir: Base directory (e.g. data/historical/ncuc/)
            docket_filter: Optional list of docket directory names to process.

        Returns:
            Dict of {docket_dir_name: [IngestResult, ...]}
        """
        all_results: dict[str, list[IngestResult]] = {}
        for ddir in sorted(ncuc_dir.iterdir()):
            if not ddir.is_dir():
                continue
            if docket_filter and ddir.name not in docket_filter:
                continue
            results = self.ingest_docket(ddir)
            if results:
                all_results[ddir.name] = results
                has_data = sum(1 for r in results if r.has_rate_data())
                logger.info(
                    "%s: %d segments, %d with rate data",
                    ddir.name, len(results), has_data,
                )
        return all_results

    # ------------------------------------------------------------------
    # Segment processing
    # ------------------------------------------------------------------

    def _process_segment(self, seg: LeafSegment, source_pdf: Path) -> IngestResult:
        text = seg.full_text()

        # --- Rider Summary (Leaf 600 "Summary of Rider Adjustments") ---
        if re.search(r"SUMMARY OF RIDER ADJUSTMENTS", text, re.I):
            return self._process_rider_summary(seg, source_pdf, text)

        result = IngestResult(
            segment=seg,
            source_pdf=source_pdf,
            tier=1,
            page_range=seg.page_range(),
        )

        # --- Tier 1: Heuristic parsing ---
        heuristic_conf = self._run_heuristic(seg, text, result)

        # --- Tier 2: Table extraction (when heuristic found no charges) ---
        if not result.energy_charges and not result.fixed_charges:
            self._run_table_extraction(seg, result)

        # --- Tier 3: LLM fallback ---
        if self._use_llm and heuristic_conf < self.llm_threshold and not result.has_rate_data():
            self._run_llm(text, result)

        # Set final status
        if result.energy_charges or result.fixed_charges or result.demand_charges:
            result.status = "parsed" if result.tier <= 1 else ("table_only" if result.tier == 2 else "llm")
        elif result.table_rows:
            result.status = "table_only"
            result.tier = 2
        elif result.llm_raw:
            result.status = "llm"
        elif result.schedule_code or result.effective_date:
            result.status = "partial"
        else:
            result.status = "empty"
            result.tier = 0

        return result

    def _run_heuristic(self, seg: LeafSegment, text: str, result: IngestResult) -> float:
        """Run Tier 1 heuristic parser.  Returns confidence score."""
        # Prefer rider parser for rider segments
        is_rider = bool(
            seg.schedule_code and re.search(r'\bRider\b|\bBA\b|\bDRA\b|\bJRR\b', seg.title, re.I)
            or re.search(r'\brider\b', text[:500], re.I)
        )

        if is_rider:
            pr = parse_rider_text(document_id=0, title=seg.title, state="NC", company="DEP", text=text)
        else:
            pr = parse_schedule_text(document_id=0, title=seg.title, state="NC", company="DEP", text=text)

        result.tier = 1
        result.review_flags = pr.review_flags

        if pr.schedule:
            sched = pr.schedule
            result.schedule_code = sched.schedule_code or seg.schedule_code
            result.schedule_title = sched.schedule_title
            result.effective_date = str(sched.effective_start) if sched.effective_start else None
            result.customer_class = sched.customer_class
            result.energy_charges = [
                {"label": c.label, "rate": c.rate, "unit": "$/kWh",
                 "season": c.season, "period": c.period,
                 "block_from": c.block_from, "block_to": c.block_to}
                for c in sched.energy_charges
            ]
            result.fixed_charges = [
                {"label": c.label, "amount": c.amount, "unit": "$/month"}
                for c in sched.fixed_charges
            ]
            result.demand_charges = [
                {"label": c.label, "rate": c.rate, "unit": "$/kW"}
                for c in sched.demand_charges
            ]
            result.riders = [r.code for r in sched.riders if r.code]

        elif pr.rider:
            rider = pr.rider
            result.schedule_code = rider.code or seg.schedule_code
            result.effective_date = rider.effective_date

        # Effective date from extracted_fields if not already set
        if not result.effective_date:
            for ef in pr.extracted_fields:
                if ef.name == "effective_date":
                    result.effective_date = str(ef.value)
                    break

        # Schedule code from extracted_fields / segment
        if not result.schedule_code:
            for ef in pr.extracted_fields:
                if ef.name == "schedule_code":
                    result.schedule_code = str(ef.value)
                    break
            if not result.schedule_code:
                result.schedule_code = seg.schedule_code

        # Confidence: how many charge types were found?
        charge_count = len(result.energy_charges) + len(result.fixed_charges) + len(result.demand_charges)
        if charge_count >= 3:
            conf = 0.9
        elif charge_count >= 1:
            conf = 0.7
        elif result.schedule_code and result.effective_date:
            conf = 0.4
        else:
            conf = 0.1

        result.confidence = conf

        # Provenance fields — always extracted regardless of charge confidence
        from duke_rates.parse.heuristics import extract_supersedes, extract_docket_footer
        result.supersedes = extract_supersedes(text)
        result.docket_number, result.order_date = extract_docket_footer(text)

        return conf

    def _process_rider_summary(
        self, seg: LeafSegment, source_pdf: Path, text: str
    ) -> IngestResult:
        """Parse a Leaf 600 'Summary of Rider Adjustments' segment."""
        from duke_rates.parse.rider_summary import parse_rider_summary

        result = IngestResult(
            segment=seg,
            source_pdf=source_pdf,
            tier=1,
            page_range=seg.page_range(),
        )
        summary = parse_rider_summary(text, source_pdf=str(source_pdf), leaf_no=seg.leaf_no)
        result.rider_summary = summary
        result.status = "rider_summary"
        result.confidence = 0.9 if summary.rate_classes else 0.4
        result.docket_number = summary.docket_number
        result.order_date = summary.order_date
        result.supersedes = summary.supersedes
        result.effective_date = summary.effective_date
        if not summary.rate_classes:
            result.review_flags.append("Rider summary detected but no rate class blocks parsed")
        return result

    def _run_table_extraction(self, seg: LeafSegment, result: IngestResult) -> None:
        """Tier 2: Extract rate tables using pdfplumber's table reader."""
        if seg.source_pdf is None:
            return
        try:
            import pdfplumber  # type: ignore
        except ImportError:
            return

        pages_to_check = {p.page_num for p in seg.pages}
        try:
            with pdfplumber.open(seg.source_pdf) as pdf:
                for page_obj in pdf.pages:
                    if (page_obj.page_number) not in pages_to_check:
                        continue
                    tables = page_obj.extract_tables()
                    for table in tables:
                        # Filter to rows that look like rate data
                        for row in table:
                            if row and any(
                                cell and re.search(r'\d[\d,.]*\s*[¢$]|per\s+kWh|\/kW|\/month', str(cell), re.I)
                                for cell in row
                            ):
                                result.table_rows.append([str(c or "").strip() for c in row])
        except Exception as exc:
            logger.debug("Table extraction failed for %s: %s", seg.source_pdf.name, exc)

        if result.table_rows:
            result.tier = 2
            logger.debug(
                "Table extraction: %d rate rows from %s [%s]",
                len(result.table_rows), seg.source_pdf.name, seg.title,
            )

    def _run_llm(self, text: str, result: IngestResult) -> None:
        """Tier 3: LLM extraction for low-confidence / unusual format docs."""
        if not self._use_llm:
            return
        if not self._llm_client:
            try:
                from duke_rates.ai.llm_client import LLMClient
                self._llm_client = LLMClient(self.settings)
            except Exception as exc:
                logger.warning("LLM client init failed: %s", exc)
                return

        prompt = _build_extraction_prompt(text)
        try:
            raw = self._llm_client.summarize_tariff(prompt)
            result.llm_raw = raw
            result.tier = 3
            # Parse the JSON response
            parsed = _parse_llm_response(raw)
            if parsed:
                if not result.schedule_code and parsed.get("schedule_code"):
                    result.schedule_code = parsed["schedule_code"]
                if not result.effective_date and parsed.get("effective_date"):
                    result.effective_date = parsed["effective_date"]
                if parsed.get("energy_charges"):
                    result.energy_charges = parsed["energy_charges"]
                if parsed.get("fixed_charges"):
                    result.fixed_charges = parsed["fixed_charges"]
                if parsed.get("demand_charges"):
                    result.demand_charges = parsed["demand_charges"]
                if parsed.get("riders"):
                    result.riders = parsed["riders"]
                result.confidence = float(parsed.get("confidence", 0.6))
            logger.debug("LLM extraction: %s -> %s", result.segment.title, result.schedule_code)
        except Exception as exc:
            logger.warning("LLM extraction failed for %s: %s", result.segment.title, exc)


# ---------------------------------------------------------------------------
# LLM prompt + response parsing
# ---------------------------------------------------------------------------

def _build_extraction_prompt(text: str) -> str:
    return (
        "You are a utility tariff analyst. Extract rate data from the following "
        "rate schedule or tariff leaf text. Return ONLY a JSON object with these fields:\n"
        "  schedule_code: string (e.g. 'RES', 'SGS-86', 'Rider BA')\n"
        "  schedule_title: string (full title if present)\n"
        "  effective_date: string (ISO date or 'Month DD, YYYY')\n"
        "  customer_class: string ('residential', 'commercial', 'industrial', etc.)\n"
        "  energy_charges: list of {label, rate, unit, season, period}\n"
        "  fixed_charges: list of {label, amount, unit}\n"
        "  demand_charges: list of {label, rate, unit}\n"
        "  riders: list of strings (rider codes/names referenced)\n"
        "  confidence: float 0.0-1.0 (how confident you are)\n"
        "  notes: string (anything unusual about the format or content)\n\n"
        "If a field is absent from the text, omit it from the JSON.\n\n"
        "TEXT:\n"
        f"{text[:10000]}"
    )


def _parse_llm_response(raw: str) -> dict[str, Any] | None:
    """Extract JSON from LLM response (handles markdown code blocks)."""
    # Strip markdown fences
    clean = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    # Find first { ... } block
    m = re.search(r"\{[\s\S]*\}", clean)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def print_ingest_summary(results: list[IngestResult], top_n: int = 60) -> None:
    """Print a compact summary table of ingest results."""
    has_data = [r for r in results if r.has_rate_data()]
    empty = [r for r in results if not r.has_rate_data()]

    print(f"\n{'='*80}")
    print(f"Ingest Pipeline Summary  ({len(results)} segments)")
    print(f"  With rate data: {len(has_data)}  |  Empty/partial: {len(empty)}")
    tier_counts = {}
    for r in results:
        tier_counts[r.tier] = tier_counts.get(r.tier, 0) + 1
    print(f"  Tiers: " + "  ".join(f"T{t}={c}" for t, c in sorted(tier_counts.items())))
    print(f"{'='*80}")
    print(f"{'Title':<35} {'Code':<14} {'Date':<12} E  F  D  Rows  Conf  Status")
    print("-" * 80)

    for r in results[:top_n]:
        title = (r.segment.title or "")[:34]
        code = (r.schedule_code or "")[:13]
        date = (r.effective_date or "")[:11]
        e = len(r.energy_charges)
        f = len(r.fixed_charges)
        d = len(r.demand_charges)
        rows = len(r.table_rows)
        conf = r.confidence
        status = r.status[:8]
        print(f"{title:<35} {code:<14} {date:<12} {e:<3}{f:<3}{d:<3}{rows:<6}{conf:5.2f}  {status}")

    if len(results) > top_n:
        print(f"  ... ({len(results) - top_n} more)")
    print()


def serialize_ingest_results(results: list[IngestResult]) -> list[dict]:
    """Convert ingest results into a stable JSON-serializable structure."""
    import dataclasses

    def _seg_dict(seg: LeafSegment) -> dict:
        return {
            "leaf_no": seg.leaf_no,
            "schedule_code": seg.schedule_code,
            "revision": seg.revision,
            "title": seg.title,
            "page_range": seg.page_range(),
            "source_pdf": str(seg.source_pdf) if seg.source_pdf else None,
        }

    data: list[dict] = []
    for r in results:
        text = r.segment.full_text()
        nonblank_lines = [line for line in text.splitlines() if line.strip()]
        entry: dict = {
            "segment": _seg_dict(r.segment),
            "source_pdf": str(r.source_pdf),
            "tier": r.tier,
            "status": r.status,
            "confidence": r.confidence,
            "schedule_code": r.schedule_code,
            "schedule_title": r.schedule_title,
            "effective_date": r.effective_date,
            "customer_class": r.customer_class,
            "energy_charges": r.energy_charges,
            "fixed_charges": r.fixed_charges,
            "demand_charges": r.demand_charges,
            "riders": r.riders,
            "table_rows": r.table_rows[:20],  # cap to avoid huge files
            "review_flags": r.review_flags,
            "page_range": r.page_range,
            "text_length": len(text),
            "line_count": len(nonblank_lines),
            "numeric_line_count": sum(1 for line in nonblank_lines if re.search(r"\d", line)),
            "has_rider_summary": r.rider_summary is not None,
            # Provenance / version chain
            "supersedes": r.supersedes,
            "docket_number": r.docket_number,
            "order_date": r.order_date,
        }
        if r.rider_summary is not None:
            entry["rider_summary"] = dataclasses.asdict(r.rider_summary)
        data.append(entry)

    return data


def export_ingest_results_json(results: list[IngestResult], output_path: Path) -> None:
    """Export all ingest results to a JSON file."""
    data = serialize_ingest_results(results)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logger.info("Exported %d ingest results -> %s", len(data), output_path)


def serialize_rider_summaries(results: list[IngestResult]) -> list[dict]:
    """Convert rider summary results into a stable JSON-serializable structure."""
    import dataclasses

    summaries: list[dict] = []
    for r in results:
        if r.rider_summary is None:
            continue
        summaries.append({
            "source_pdf": str(r.source_pdf),
            "leaf_no": r.segment.leaf_no,
            "effective_date": r.effective_date,
            "docket_number": r.docket_number,
            "order_date": r.order_date,
            "supersedes": r.supersedes,
            "rate_classes": dataclasses.asdict(r.rider_summary)["rate_classes"],
        })

    return summaries


def export_rider_summaries_json(
    results: list[IngestResult],
    output_path: Path,
) -> int:
    """Write rider summary records (Leaf 600) to a separate JSON file.

    Returns the number of rider summary records written.
    """
    summaries = serialize_rider_summaries(results)
    if summaries:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summaries, indent=2, default=str), encoding="utf-8")
        logger.info("Exported %d rider summaries -> %s", len(summaries), output_path)

    return len(summaries)
