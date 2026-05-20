"""
Bulk rate charge extraction from historical NC documents.

Phase 2 of data preparation: Extract rates from confirmed tariff documents
using improved metadata (effective_start dates from Step 1).
"""

import sqlite3
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from datetime import UTC, datetime

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from duke_rates.historical.ncuc.pipeline.rate_extractor import (
    ResidentialRateExtractor, ExtractedCharge
)
from duke_rates.historical.ncuc.pipeline.document_prep import (
    DocumentClassifier, DateExtractor
)
from duke_rates.historical.ncuc.pipeline.parser_profiles import (
    HistoricalRateParserProfile,
    HistoricalRateParserRegistry,
    ParserProfileCandidate,
    ParserProfileSignals,
)
from duke_rates.historical.ncuc.pipeline.ocr_normalization import (
    normalize_docling_markdown,
    normalize_ocr_text,
)
from duke_rates.historical.ncuc.pipeline.stage_versions import (
    HISTORICAL_BULK_PARSER_VERSION,
)
from duke_rates.db.reprocess import (
    record_historical_processing_run,
)
from duke_rates.db.artifact_cache import load_page_artifacts
from duke_rates.document_intelligence.normalization import DocumentNormalizationConfig
from duke_rates.document_intelligence.service import (
    DocumentIntelligenceOrchestrator,
    HistoricalDocumentIntelligenceContext,
)
from duke_rates.models.pipeline import ParseReviewOutcome
from duke_rates.historical.ncuc.pipeline.page_miner import (
    mine_document_pages,
    classify_compliance_book,
    REDLINE_MARKER_REGEX,
    DUAL_RATE_REGEX,
)
from duke_rates.historical.ncuc.pipeline.doc_tier import infer_doc_quality_tier

logger = logging.getLogger(__name__)

# Progress NC family mappings for residential (Leaf 500-504)
RESIDENTIAL_FAMILIES = {
    'nc-progress-leaf-500': 'Residential Service',
    'nc-progress-leaf-501': 'Residential Service - Time of Use Demand',
    'nc-progress-leaf-502': 'Residential Service - Time of Use',
    'nc-progress-leaf-503': 'Residential Service - Time of Use with Critical Peak Pricing',
    'nc-progress-leaf-504': 'Residential Service - Time of Use - EV',
    'nc-progress-leaf-505': 'Residential Service - Solar',
}

_HISTORICAL_DOC_DISCOVERY_LOOKUP = """
    COALESCE(
        (
            SELECT ndr.id
            FROM ncuc_discovery_records ndr
            WHERE ndr.local_path = hd.local_path
            ORDER BY ndr.id DESC
            LIMIT 1
        ),
        (
            SELECT ndr.id
            FROM ncuc_discovery_records ndr
            WHERE ndr.content_hash IS NOT NULL
              AND ndr.content_hash = hd.content_hash
            ORDER BY ndr.id DESC
            LIMIT 1
        )
    )
"""

_HISTORICAL_DOC_DISCOVERY_DOCKET = """
    COALESCE(
        (
            SELECT ndr.docket_number
            FROM ncuc_discovery_records ndr
            WHERE ndr.local_path = hd.local_path
            ORDER BY ndr.id DESC
            LIMIT 1
        ),
        (
            SELECT ndr.docket_number
            FROM ncuc_discovery_records ndr
            WHERE ndr.content_hash IS NOT NULL
              AND ndr.content_hash = hd.content_hash
            ORDER BY ndr.id DESC
            LIMIT 1
        )
    )
"""

_HISTORICAL_DOC_DISCOVERY_METHOD = """
    COALESCE(
        (
            SELECT ndr.acquisition_method
            FROM ncuc_discovery_records ndr
            WHERE ndr.local_path = hd.local_path
            ORDER BY ndr.id DESC
            LIMIT 1
        ),
        (
            SELECT ndr.acquisition_method
            FROM ncuc_discovery_records ndr
            WHERE ndr.content_hash IS NOT NULL
              AND ndr.content_hash = hd.content_hash
            ORDER BY ndr.id DESC
            LIMIT 1
        )
    )
"""

_HISTORICAL_DOC_DISCOVERY_TIER = """
    COALESCE(
        (
            SELECT ndr.doc_quality_tier
            FROM ncuc_discovery_records ndr
            WHERE ndr.local_path = hd.local_path
            ORDER BY ndr.id DESC
            LIMIT 1
        ),
        (
            SELECT ndr.doc_quality_tier
            FROM ncuc_discovery_records ndr
            WHERE ndr.content_hash IS NOT NULL
              AND ndr.content_hash = hd.content_hash
            ORDER BY ndr.id DESC
            LIMIT 1
        )
    )
"""


class BulkExtractor:
    """Extract charges from all historical NC documents with proper versioning."""

    _REFERENCE_ONLY_FAMILIES = {
        "nc-progress-leaf-740",
        "nc-progress-leaf-741",
        "nc-progress-leaf-742",
        "nc-progress-leaf-743",
        "nc-progress-leaf-744",
        "nc-progress-leaf-800",
        "nc-progress-leaf-801",
        "nc-progress-leaf-802",
        # All 7 docs in this family are misclassified slices from LGS/PG schedules
        # whose Riders list mentions "BPM Prospective Rider". None contain actual
        # Prospective Rider rate content. The page-bounded text is reference-only.
        "nc-carolinas-rider-prospectiverider",
    }
    _REFERENCE_TITLE_TOKENS = (
        "service regulations",
        "line extension plan",
        "charging station program",
        "infrastructure program",
        "pilot",
        "approval of",
        "application of",
        "petition for",
        "order approving",
        "rule r8-",
    )
    _REFERENCE_RATE_MARKERS = (
        "basic customer charge",
        "customer charge",
        "energy charge",
        "demand charge",
        "summary of rider adjustments",
        "rider adjustments",
        "per kwh",
        "per kw",
    )
    _BOUNDED_TARIFF_OVERRIDE_MARKERS = (
        "monthly rate",
        "applicability",
        "customer charge",
        "energy charge",
        "per kilowatt-hour",
        "per kwh",
        "per kw",
    )
    _FORMULA_ONLY_TITLE_HINTS = {
        "nc-progress-leaf-672": ("clean energy impact rider", "rider cei", "clean energy impact"),
        "nc-progress-leaf-701": ("business energy saver program", "sbes"),
        "nc-progress-leaf-702": ("smart $aver performance incentive program", "ssp"),
        "nc-progress-leaf-708": ("residential new construction program", "rnc"),
        "nc-progress-leaf-719": ("weatherization program", "iwz"),
        "nc-progress-leaf-720": ("prepaid advantage program", "ppa"),
        "nc-progress-leaf-723": ("smart $aver", "tobr"),
    }

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.classifier = DocumentClassifier()
        self.extractor = ResidentialRateExtractor()
        self.date_extractor = DateExtractor()
        self.parser_registry = HistoricalRateParserRegistry()
        # Extraction operates on cached representations — OCR escalation
        # belongs in the ingestion path (`ocr process-queue-nc`, process-docling-batch),
        # not here. Disabling GLM/Paddle prevents per-doc Ollama/GPU calls during
        # rate extraction.
        self.document_intelligence = DocumentIntelligenceOrchestrator(
            project_root=self._infer_project_root(db_path),
            normalization_config=DocumentNormalizationConfig(
                enable_glm_ocr=False,
                enable_paddle_structure=False,
                enable_symbol_noise_escalation=False,
            ),
        )
        self._embedding_classifier: object | None = None  # Phase 4 — lazy init
        self._llm_adjudicator: object | None = None  # Phase 5 — lazy init

    @staticmethod
    def _infer_project_root(db_path: str) -> Path:
        path = Path(db_path).resolve()
        try:
            return path.parents[2]
        except IndexError:
            return Path.cwd()

    def _get_connection(self):
        """Create a new database connection (thread-safe)."""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=60000;")
        return conn

    def get_documents_needing_extraction(self, family_key: str | None = None) -> List[dict]:
        """Get all version-linked NC historical documents ready for extraction."""
        conn = self._get_connection()
        try:
            query = """
            WITH latest_fingerprints AS (
                SELECT df.*
                FROM document_fingerprints df
                JOIN (
                    SELECT source_pdf, MAX(id) AS max_id
                    FROM document_fingerprints
                    GROUP BY source_pdf
                ) latest ON latest.max_id = df.id
            )
            SELECT
                hd.id,
                hd.family_key,
                hd.title,
                hd.company,
                hd.state,
                hd.local_path,
                hd.content_hash,
                hd.effective_start,
                hd.revision_label,
                hd.supersedes_label,
                hd.leaf_no,
                hd.start_page,
                hd.end_page,
                ({discovery_record_id}) AS discovery_record_id,
                ({docket_number}) AS docket_number,
                ({acquisition_method}) AS acquisition_method,
                ({doc_quality_tier}) AS discovery_doc_quality_tier,
                COALESCE(lf.is_redline_candidate, 0) AS is_redline_candidate,
                COALESCE(lf.redline_confidence, 0.0) AS redline_confidence,
                dc_redline.label AS classification_is_redline,
                tv.id AS version_id
            FROM historical_documents hd
            JOIN tariff_versions tv ON tv.historical_document_id = hd.id
            LEFT JOIN latest_fingerprints lf ON lf.source_pdf = hd.local_path
            LEFT JOIN (
                SELECT subject_id, label
                FROM document_classifications
                WHERE stage = 'flag_is_redline'
                  AND superseded_by IS NULL
                  AND subject_kind = 'historical_document'
                  AND label = 'true'
            ) dc_redline ON dc_redline.subject_id = CAST(hd.id AS TEXT)
            WHERE hd.state = 'NC'
                AND hd.company IN ('progress', 'carolinas')
                AND hd.effective_start IS NOT NULL
                AND hd.local_path IS NOT NULL
            ORDER BY hd.family_key, hd.effective_start
            """.format(
                discovery_record_id=_HISTORICAL_DOC_DISCOVERY_LOOKUP,
                docket_number=_HISTORICAL_DOC_DISCOVERY_DOCKET,
                acquisition_method=_HISTORICAL_DOC_DISCOVERY_METHOD,
                doc_quality_tier=_HISTORICAL_DOC_DISCOVERY_TIER,
            )
            params: tuple[object, ...] = ()
            if family_key:
                query = query.replace(
                    "ORDER BY hd.family_key, hd.effective_start",
                    "AND hd.family_key = ? ORDER BY hd.family_key, hd.effective_start",
                )
                params = (family_key,)
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def count_documents_missing_versions(self, family_key: str | None = None) -> int:
        """Count extraction-eligible historical docs that still lack tariff_versions."""
        conn = self._get_connection()
        try:
            try:
                query = """
                    SELECT COUNT(*) AS count
                    FROM historical_documents hd
                    LEFT JOIN tariff_versions tv ON tv.historical_document_id = hd.id
                    WHERE hd.state = 'NC'
                      AND hd.company IN ('progress', 'carolinas')
                      AND hd.effective_start IS NOT NULL
                      AND hd.local_path IS NOT NULL
                      AND tv.id IS NULL
                    """
                params: tuple[object, ...] = ()
                if family_key:
                    query += " AND hd.family_key = ?"
                    params = (family_key,)
                row = conn.execute(query, params).fetchone()
            except sqlite3.OperationalError:
                return 0
            return int(row["count"]) if row else 0
        finally:
            conn.close()

    def get_document_for_extraction(self, historical_document_id: int) -> dict | None:
        """Fetch one historical document row in extraction-ready shape."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                """
                WITH latest_fingerprints AS (
                    SELECT df.*
                    FROM document_fingerprints df
                    JOIN (
                        SELECT source_pdf, MAX(id) AS max_id
                        FROM document_fingerprints
                        GROUP BY source_pdf
                    ) latest ON latest.max_id = df.id
                )
                SELECT
                    hd.id,
                    hd.family_key,
                    hd.title,
                    hd.company,
                    hd.state,
                    hd.local_path,
                    hd.content_hash,
                    hd.effective_start,
                    hd.revision_label,
                    hd.supersedes_label,
                    hd.leaf_no,
                    hd.start_page,
                    hd.end_page,
                    ({discovery_record_id}) AS discovery_record_id,
                    ({docket_number}) AS docket_number,
                    ({acquisition_method}) AS acquisition_method,
                    ({doc_quality_tier}) AS discovery_doc_quality_tier,
                    COALESCE(lf.is_redline_candidate, 0) AS is_redline_candidate,
                    COALESCE(lf.redline_confidence, 0.0) AS redline_confidence
                FROM historical_documents hd
                LEFT JOIN latest_fingerprints lf ON lf.source_pdf = hd.local_path
                WHERE hd.id = ?
                LIMIT 1
                """.format(
                    discovery_record_id=_HISTORICAL_DOC_DISCOVERY_LOOKUP,
                    docket_number=_HISTORICAL_DOC_DISCOVERY_DOCKET,
                    acquisition_method=_HISTORICAL_DOC_DISCOVERY_METHOD,
                    doc_quality_tier=_HISTORICAL_DOC_DISCOVERY_TIER,
                ),
                (historical_document_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_tariff_version_for_document(self, doc_id: int) -> Optional[int]:
        """Get the tariff_version id for a historical document."""
        conn = self._get_connection()
        try:
            query = """
            SELECT id FROM tariff_versions
            WHERE historical_document_id = ?
            LIMIT 1
            """
            cursor = conn.execute(query, (doc_id,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    # Minimum Docling plain_text length to consider an artifact usable for
    # extraction. Below this we fall back to pdfplumber. 100 chars is enough
    # to reject empty / trivial conversions but small enough that single-leaf
    # tariff sheets still qualify.
    _DOCLING_TEXT_MIN_CHARS = 100

    @staticmethod
    def _docling_status_is_usable(status: str | None) -> bool:
        """Both ``"success"`` and partial conversions are usable.

        Partial bundles still typically contain the majority of converted
        pages and are far better than pdfplumber on scanned docs. The repo
        mixes status values written at different times (``"success"``,
        ``"ConversionStatus.SUCCESS"``, ``"partial_success"``, etc.), so
        match by substring.
        """
        s = (status or "").lower()
        return "success" in s or "partial" in s

    def _load_docling_artifact(
        self, pdf_path: str, want_doc_json: bool = False
    ) -> dict | None:
        """Return the latest usable Docling artifact row for ``pdf_path``, or None.

        ``want_doc_json=True`` selects the (large) ``doc_json_content`` column,
        used by page-range slicing. The default omits it to keep memory low
        for the full-document text path.
        """
        cols = "plain_text_content, status, page_count"
        if want_doc_json:
            cols += ", doc_json_content"
        conn = self._get_connection()
        try:
            row = conn.execute(
                f"""
                SELECT {cols}
                FROM docling_artifacts
                WHERE source_pdf = ?
                  AND plain_text_content IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (pdf_path,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()
        if not row or not self._docling_status_is_usable(row["status"]):
            return None
        return dict(row)

    def _load_docling_text(self, pdf_path: str) -> str | None:
        """Return the full plain_text body of the latest usable Docling artifact.

        Used for unbounded extraction. Page-bounded callers go through
        ``_slice_docling_text`` instead.
        """
        row = self._load_docling_artifact(pdf_path)
        if not row:
            return None
        text = row["plain_text_content"] or ""
        if len(text.strip()) < self._DOCLING_TEXT_MIN_CHARS:
            return None
        return text

    @staticmethod
    def _render_docling_table_as_markdown(table: dict) -> str:
        """Render a Docling table dict as a GitHub-flavored markdown table.

        Matches the format ``normalize_docling_markdown`` flattens, so the
        downstream parser sees the same shape it does in the unbounded path.
        Returns "" when the table has no recoverable cells.
        """
        data = table.get("data") or {}
        grid = data.get("grid") or []
        if not grid:
            return ""
        rows: list[list[str]] = []
        for grid_row in grid:
            cells = []
            for cell in grid_row:
                txt = str(cell.get("text") or "").strip().replace("|", " ")
                cells.append(txt)
            rows.append(cells)
        if not rows:
            return ""
        ncols = max(len(r) for r in rows)
        # Normalize ragged rows
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        lines = ["| " + " | ".join(r) + " |" for r in rows]
        # Insert separator after first row so normalize_docling_markdown
        # recognizes it as a table.
        sep = "|" + "|".join(["---"] * ncols) + "|"
        return "\n".join([lines[0], sep] + lines[1:])

    def _slice_docling_text(
        self, pdf_path: str, start_page: int, end_page: int
    ) -> str | None:
        """Return text for pages in ``[start_page, end_page]`` from the cached Docling artifact.

        Walks ``doc_json.body.children`` in reading order and emits each
        referenced text/table whose ``prov[].page_no`` falls in range.
        Returns None when no artifact, no doc_json, or the slice is empty.

        Markdown output (``## headers`` are absent here, but tables are
        rendered as markdown so they get flattened by
        ``normalize_docling_markdown`` in the same pass as unbounded text).

        IMPORTANT: ``prov[].page_no`` in Docling artifacts is only reliable as
        a PDF page index when the artifact covers the full PDF. Some artifacts
        in this DB were built from a partial chunk of the source PDF and the
        ``page_no`` values are chunk-relative (1..N) rather than PDF-page-absolute.
        Slicing by ``start_page``/``end_page`` against such an artifact returns
        text from the wrong region. When we detect this mismatch, we return None
        so the caller falls back to pdfplumber.
        """
        row = self._load_docling_artifact(pdf_path, want_doc_json=True)
        if not row:
            return None
        raw_json = row.get("doc_json_content")
        if not raw_json:
            return None
        try:
            doc = json.loads(raw_json)
        except (TypeError, ValueError):
            return None

        body = doc.get("body") or {}
        children = body.get("children") or []
        texts = doc.get("texts") or []
        tables = doc.get("tables") or []

        # Reject artifacts whose prov page numbering doesn't span the requested
        # range. If the highest page_no in any text's provenance is below
        # start_page, this artifact cannot contain content for our target pages.
        # Either the artifact is a partial chunk or the requested range is
        # outside what was processed; pdfplumber is the right fallback.
        max_prov_page = 0
        for t in texts:
            for prov in t.get("prov") or []:
                page_no = prov.get("page_no")
                if isinstance(page_no, int) and page_no > max_prov_page:
                    max_prov_page = page_no
        if max_prov_page < start_page:
            return None
        # If max_prov is well below the actual PDF page count, the artifact
        # is a partial chunk — its page numbering is unreliable as a PDF
        # page index. Verify against the source PDF page count.
        try:
            import fitz
            with fitz.open(pdf_path) as pdf:
                actual_page_count = pdf.page_count
        except Exception:
            actual_page_count = 0
        # Allow some slack — Docling sometimes drops a page at end of doc.
        # Threshold: if max_prov is less than 80% of actual pages AND the
        # PDF is non-trivially large (>10 pages), don't trust the numbering.
        if (
            actual_page_count > 10
            and max_prov_page > 0
            and max_prov_page < actual_page_count * 0.8
        ):
            logger.debug(
                "Docling artifact for %s covers only %d of %d pages — "
                "page numbering unreliable, skipping slice",
                pdf_path, max_prov_page, actual_page_count,
            )
            return None

        def _ref_index(ref_obj: dict, key: str) -> int | None:
            ref = (ref_obj or {}).get("$ref", "")
            prefix = f"#/{key}/"
            if not ref.startswith(prefix):
                return None
            try:
                return int(ref[len(prefix):])
            except ValueError:
                return None

        def _in_range(item: dict) -> bool:
            for prov in item.get("prov") or []:
                page_no = prov.get("page_no")
                if isinstance(page_no, int) and start_page <= page_no <= end_page:
                    return True
            return False

        parts: list[str] = []
        used_text_idx: set[int] = set()
        used_table_idx: set[int] = set()
        # Walk children in reading order. Items not in our target page range
        # are skipped (no ordering loss because we keep the rest in order).
        for child in children:
            ti = _ref_index(child, "texts")
            if ti is not None and 0 <= ti < len(texts):
                used_text_idx.add(ti)
                item = texts[ti]
                if _in_range(item):
                    txt = str(item.get("text") or "").strip()
                    if txt:
                        parts.append(txt)
                continue
            tbl_i = _ref_index(child, "tables")
            if tbl_i is not None and 0 <= tbl_i < len(tables):
                used_table_idx.add(tbl_i)
                item = tables[tbl_i]
                if _in_range(item):
                    rendered = self._render_docling_table_as_markdown(item)
                    if rendered:
                        parts.append(rendered)
                continue

        # Append any in-range texts/tables that weren't referenced from
        # body.children. Some Docling artifacts (e.g. hd_id=29 leaf-500 NC
        # Residential) keep rate-content items like "Basic Customer Charge"
        # and "Kilowatt-Hour Charge" out of body.children entirely, causing
        # them to be silently dropped by the children-walk above. Without
        # this pass the sliced text loses the markers parser profiles need
        # to recognize the doc (e.g. progress_residential_flat requires
        # both "basic customer charge" and "per kwh" markers — both miss
        # together when body.children doesn't reference them).
        # Reading order isn't preserved for these items but it doesn't
        # matter for regex-based supports()/extract() that look for
        # markers and rate patterns rather than positional structure.
        for ti, item in enumerate(texts):
            if ti in used_text_idx:
                continue
            if _in_range(item):
                txt = str(item.get("text") or "").strip()
                if txt:
                    parts.append(txt)
        for tbl_i, item in enumerate(tables):
            if tbl_i in used_table_idx:
                continue
            if _in_range(item):
                rendered = self._render_docling_table_as_markdown(item)
                if rendered:
                    parts.append(rendered)

        if not parts:
            return None
        joined = "\n\n".join(parts)
        if len(joined.strip()) < self._DOCLING_TEXT_MIN_CHARS:
            return None
        return joined

    def extract_text_from_pdf(self, pdf_path: str, start_page: Optional[int] = None,
                             end_page: Optional[int] = None) -> tuple[str, str]:
        """Extract text from PDF file, optionally limited to page range.

        Returns a tuple of (text, source) where ``source`` is one of:
          * "docling_artifact"  — full-document text from a cached Docling artifact
          * "pdfplumber"        — direct PDF text via pdfplumber
          * "none"              — extraction failed or produced no text

        Docling artifacts are only consulted for full-document extraction
        (no page bounds). Page-bounded callers go straight to pdfplumber
        because slicing Docling's plain_text by page boundary is not
        implemented in Phase A — see _load_docling_text docstring.
        """
        # Phase A: prefer Docling artifact when no page bounds are requested.
        # This unlocks scanned docs and table-rich filings where pdfplumber
        # text is sparse or where structured tables only Docling captured
        # carry the rate values.
        if start_page is None and end_page is None:
            docling_text = self._load_docling_text(pdf_path)
            if docling_text is not None:
                return docling_text, "docling_artifact"

        # Phase B: page-bounded slicing of Docling's doc_json. This handles
        # multi-schedule compliance bundles where the desired tariff lives
        # on pages [start_page, end_page] within a 100+ page filing.
        elif start_page is not None and end_page is not None:
            sliced = self._slice_docling_text(pdf_path, start_page, end_page)
            if sliced is not None:
                return sliced, "docling_artifact_sliced"

        if not pdfplumber:
            logger.warning(f"pdfplumber not available, cannot extract from {pdf_path}")
            return "", "none"

        try:
            with pdfplumber.open(pdf_path) as pdf:
                pages = pdf.pages

                # Limit to page range if provided
                if start_page is not None and end_page is not None:
                    # Convert to 0-indexed
                    start_idx = max(0, start_page - 1)
                    end_idx = min(len(pages), end_page)
                    pages = pages[start_idx:end_idx]
                elif start_page is not None:
                    start_idx = max(0, start_page - 1)
                    pages = pages[start_idx:start_idx + 10]  # Get first 10 pages from start

                text_parts = []
                for page in pages:
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)

                joined = '\n'.join(text_parts)
                return joined, ("pdfplumber" if joined else "none")
        except Exception as e:
            logger.error(f"Error extracting from {pdf_path}: {e}")
            return "", "none"

    def classify_document(self, filing_title: str, text_sample: str) -> str:
        """Classify document as tariff, procedural, etc."""
        return self.classifier.classify(filing_title, text_sample)

    def _record_document_type_classification(self, doc: dict, result) -> None:
        """Persist a ``document_type`` ClassificationResult for ``doc``.

        Side-effect only — never raises. Failures are logged at debug level
        because losing an observability row must not break extraction.
        """
        doc_id = doc.get('id')
        if not doc_id or result is None:
            return
        try:
            from duke_rates.classification.persistence import record_classification
        except Exception:
            return
        cls_conn = self._get_connection()
        try:
            record_classification(
                cls_conn,
                subject_kind="historical_document",
                subject_id=str(doc_id),
                stage="document_type",
                result=result,
            )
            cls_conn.commit()
        except Exception as exc:
            logger.debug(
                "Failed to record document_type classification for doc %s: %s",
                doc_id, exc,
            )
        finally:
            cls_conn.close()

    def _record_flag_classifications(self, doc: dict, text: str) -> None:
        """Run all Phase 3 flag classifiers against *doc* and persist results.

        Side-effect only — never raises. Each flag is an independent
        classifier producing its own ``ClassificationResult`` row with
        stage ``flag_<name>`` or ``<extraction_stage>``.
        """
        doc_id = doc.get('id')
        if not doc_id or not text:
            return
        try:
            from duke_rates.classification.persistence import record_classification
            from duke_rates.document_intelligence.flag_classifiers import (
                get_flag_classifier,
                all_flag_stages,
            )
        except Exception:
            return

        metadata = {
            "company": doc.get("company", ""),
            "family_key": doc.get("family_key", ""),
            "leaf_no": doc.get("leaf_no", ""),
            "effective_start": doc.get("effective_start", ""),
            "docket_number": doc.get("docket_number", ""),
            "is_redline_candidate": int(doc.get("is_redline_candidate") or 0),
            "redline_confidence": float(doc.get("redline_confidence") or 0.0),
            "title": doc.get("title", ""),
        }

        cls_conn = self._get_connection()
        try:
            for stage in all_flag_stages():
                classifier = get_flag_classifier(stage)
                if classifier is None:
                    continue
                try:
                    result = classifier.classify(text, metadata)
                    result.classifier = f"rule_{stage}_v1"
                    result.classifier_version = "v1"
                    record_classification(
                        cls_conn,
                        subject_kind="historical_document",
                        subject_id=str(doc_id),
                        stage=stage,
                        result=result,
                    )
                except Exception:
                    pass  # individual flag failure doesn't block others
            cls_conn.commit()
        except Exception as exc:
            logger.debug(
                "Failed to record flag classifications for doc %s: %s",
                doc_id, exc,
            )
        finally:
            cls_conn.close()

    def _record_embedding_document_type(self, doc: dict) -> None:
        """Run the embedding KNN classifier and persist as a second document_type row.

        Only runs if the embedding table has reference rows.
        Side-effect only — never raises.
        """
        doc_id = doc.get("id")
        local_path = doc.get("local_path")
        if not doc_id or not local_path or not Path(local_path).exists():
            return

        try:
            from duke_rates.document_intelligence.embedding_classifier import (
                EmbeddingKNNClassifier,
            )
        except Exception:
            return

        if self._embedding_classifier is None:
            try:
                from duke_rates.document_intelligence.ollama_orchestrator import (
                    OllamaOrchestrator,
                )
                orch = OllamaOrchestrator()
                self._embedding_classifier = EmbeddingKNNClassifier(
                    db_path=Path(self.db_path),
                    orchestrator=orch,
                    model_role="embedding_primary",
                    k=11,
                    min_neighbors=3,
                    embedding_kind="full_text",
                    max_chars=2000,
                )
            except Exception:
                return

        try:
            result = self._embedding_classifier.classify(local_path)
        except Exception as exc:
            logger.debug(
                "Embedding classifier failed for doc %s: %s", doc_id, exc,
            )
            return

        cls_conn = self._get_connection()
        try:
            from duke_rates.classification.persistence import record_classification

            record_classification(
                cls_conn,
                subject_kind="historical_document",
                subject_id=str(doc_id),
                stage="document_type",
                result=result,
            )
            cls_conn.commit()
        except Exception as exc:
            logger.debug(
                "Failed to record embedding classification for doc %s: %s",
                doc_id, exc,
            )
        finally:
            cls_conn.close()

    def _record_llm_document_type(self, doc: dict) -> None:
        """Run LLM adjudication when rule and embedding disagree.

        Only fires when:
        - Rule and embedding labels differ, OR
        - Either returned UNKNOWN, OR
        - Max confidence < 0.5

        Side-effect only — never raises.
        """
        doc_id = doc.get("id")
        if not doc_id:
            return

        # Check if llm row already exists (idempotent)
        cls_conn = self._get_connection()
        try:
            existing = cls_conn.execute(
                """
                SELECT id FROM document_classifications
                WHERE subject_kind = 'historical_document'
                  AND subject_id = ?
                  AND stage = 'document_type'
                  AND classifier LIKE 'llm_%'
                  AND superseded_by IS NULL
                """,
                (str(doc_id),),
            ).fetchone()
        finally:
            cls_conn.close()

        if existing is not None:
            return

        # Get rule and embedding results to check if adjudication needed
        cls_conn = self._get_connection()
        try:
            pair = cls_conn.execute(
                """
                SELECT r.label AS rule_label, r.confidence AS rule_confidence,
                       e.label AS emb_label, e.confidence AS emb_confidence
                FROM document_classifications r
                JOIN document_classifications e
                  ON e.subject_kind = r.subject_kind
                 AND e.subject_id = r.subject_id
                 AND e.stage = r.stage
                 AND e.classifier = 'embedding_knn_v1'
                 AND e.superseded_by IS NULL
                WHERE r.subject_kind = 'historical_document'
                  AND r.subject_id = ?
                  AND r.stage = 'document_type'
                  AND r.classifier = 'rule_document_type_v1'
                  AND r.superseded_by IS NULL
                """,
                (str(doc_id),),
            ).fetchone()
        finally:
            cls_conn.close()

        if not pair:
            return

        rule_label = pair["rule_label"]
        emb_label = pair["emb_label"]
        rule_conf = float(pair["rule_confidence"])
        emb_conf = float(pair["emb_confidence"])

        # Only adjudicate when needed
        need_adjudication = (
            rule_label != emb_label
            or rule_label == "UNKNOWN"
            or emb_label == "UNKNOWN"
            or max(rule_conf, emb_conf) < 0.5
        )
        if not need_adjudication:
            return

        # Get text
        local_path = doc.get("local_path")
        if not local_path or not Path(local_path).exists():
            return

        try:
            from duke_rates.document_intelligence.text_slicer import slice_pdf_text
            slices = slice_pdf_text(Path(local_path), max_chars=2500)
            text = slices.full_text or ""
        except Exception:
            return

        if not text:
            return

        # Lazy init adjudicator
        if self._llm_adjudicator is None:
            try:
                from duke_rates.document_intelligence.llm_classifier import LLMAdjudicator
                from duke_rates.document_intelligence.ollama_orchestrator import (
                    OllamaOrchestrator,
                )
                orch = OllamaOrchestrator(db_path=Path(self.db_path))
                ok, err = orch.health_probe("balanced_classifier")
                if not ok:
                    logger.debug("LLM adjudicator health probe failed: %s", err)
                    return
                self._llm_adjudicator = LLMAdjudicator(
                    orch, db_path=Path(self.db_path), role="balanced_classifier"
                )
            except Exception:
                return

        # Build ClassificationResult wrappers
        from duke_rates.classification.result import ClassificationResult

        rule_result = ClassificationResult(
            label=rule_label, confidence=rule_conf, classifier="rule_document_type_v1"
        )
        emb_result = ClassificationResult(
            label=emb_label, confidence=emb_conf, classifier="embedding_knn_v1"
        )

        # Adjudicate
        try:
            llm_result = self._llm_adjudicator.adjudicate(
                text, rule_result=rule_result, embedding_result=emb_result
            )
        except Exception as exc:
            logger.debug("LLM adjudication failed for doc %s: %s", doc_id, exc)
            return

        if not llm_result or llm_result.label == "UNKNOWN":
            return

        # Persist
        cls_conn = self._get_connection()
        try:
            from duke_rates.classification.persistence import record_classification

            record_classification(
                cls_conn,
                subject_kind="historical_document",
                subject_id=str(doc_id),
                stage="document_type",
                result=llm_result,
            )
            cls_conn.commit()
        except Exception as exc:
            logger.debug(
                "Failed to record LLM classification for doc %s: %s", doc_id, exc,
            )
        finally:
            cls_conn.close()

    @staticmethod
    def _serialize_candidate(candidate: ParserProfileCandidate) -> dict[str, object]:
        return {
            "name": candidate.name,
            "score": candidate.score,
            "supported": candidate.supported,
            "reasons": list(candidate.reasons),
        }

    @staticmethod
    def _safe_profile_extract(
        profile: HistoricalRateParserProfile,
        doc: dict,
        text: str,
        parse_warnings: list[dict[str, str]],
    ) -> list[ExtractedCharge]:
        """Run profile.extract() with ValueError tolerance.

        Catches float-conversion failures (typically OCR-malformed numbers like
        '1.631.88' or '0.600.40') and records them as structured parse warnings
        instead of failing the whole document. Returns [] on ValueError so the
        fallback chain still has a chance to find a working profile. Other
        exceptions still propagate.
        """
        try:
            return profile.extract(doc, text)
        except ValueError as exc:
            parse_warnings.append({
                "profile": getattr(profile, "name", "unknown"),
                "error_type": "parse_value_error",
                "error": str(exc),
            })
            logger.warning(
                f"Profile {getattr(profile, 'name', '?')} raised ValueError on doc "
                f"{doc.get('id')} ({doc.get('family_key')}): {exc}"
            )
            return []

    @staticmethod
    def _score_for_profile(
        parser_profile: str | None,
        ranked_candidates: list[ParserProfileCandidate],
    ) -> float:
        if parser_profile:
            for candidate in ranked_candidates:
                if candidate.name == parser_profile:
                    return candidate.score
        return ranked_candidates[0].score if ranked_candidates else 0.0

    @staticmethod
    def _charge_set_metrics(charges: list[ExtractedCharge]) -> dict[str, int]:
        charge_types = {charge.charge_type for charge in charges if charge.charge_type}
        tou_periods = {charge.tou_period for charge in charges if charge.tou_period}
        seasons = {charge.season for charge in charges if charge.season and charge.season != "all_year"}
        labeled = sum(1 for charge in charges if (charge.charge_label or "").strip())
        valued = sum(1 for charge in charges if charge.rate_value is not None)
        unitized = sum(1 for charge in charges if (charge.rate_unit or "").strip())
        snippet_count = sum(1 for charge in charges if (charge.source_snippet or "").strip())
        return {
            "charge_count": len(charges),
            "unique_charge_types": len(charge_types),
            "tou_period_count": len(tou_periods),
            "season_count": len(seasons),
            "completeness_score": labeled + valued + unitized + snippet_count,
        }

    def _should_apply_fallback(
        self,
        *,
        current_profile_name: str,
        current_charge_count: int,
        current_outcome_quality: str,
        current_metrics: dict[str, int],
        candidate_name: str,
        candidate_charge_count: int,
        candidate_outcome_quality: str,
        candidate_metrics: dict[str, int],
        has_page_bounds: bool = True,
    ) -> tuple[bool, str | None]:
        # Guard against cross-schedule rate attribution via the generic_residential
        # fallback. When a family-specific profile (e.g. progress_jaa_rider,
        # progress_traffic_signal_service) is the initial pick and produces 0
        # charges, generic_residential's broad `$N.NN/unit` regex will harvest
        # any rate-shaped text in the doc — including sentence fragments and
        # rates from referenced-but-not-applicable schedules — and attribute
        # them to the wrong family. This happens even on docs with bounded
        # spans because narrative proposed-order text contains rate mentions.
        # See session_embedding_model_benchmarks_2026_05_16.md (hd_id=14,
        # hd_id=1847) for the unbounded cases and the 2026-05-20 hd_id=179
        # JAA proposed-order regression that motivated broadening the guard.
        _ = has_page_bounds  # kwarg retained for caller compatibility; no longer load-bearing
        if (
            candidate_name == "generic_residential"
            and current_profile_name not in ("generic_residential", "unknown", None)
        ):
            return False, None
        if candidate_charge_count <= current_charge_count:
            if current_outcome_quality != "weak" or candidate_outcome_quality != "strong":
                return False, None
            if candidate_metrics["unique_charge_types"] > current_metrics["unique_charge_types"]:
                return True, "charge_type_coverage_gain"
            if candidate_metrics["tou_period_count"] >= current_metrics["tou_period_count"] + 2:
                return True, "tou_coverage_gain"
            if candidate_metrics["season_count"] >= current_metrics["season_count"] + 1:
                return True, "season_coverage_gain"
            if candidate_metrics["completeness_score"] >= current_metrics["completeness_score"] + 2:
                return True, "field_completeness_gain"
            return False, None
        if current_charge_count == 0:
            return True, "empty_initial_parse"
        if current_outcome_quality != "weak":
            return False, None

        improvement = candidate_charge_count - current_charge_count
        if improvement >= 2:
            return True, "material_charge_gain"

        if (
            improvement >= 1
            and current_charge_count <= 1
            and candidate_name != "generic_residential"
            and current_profile_name == "generic_residential"
        ):
            return True, "specific_profile_upgrade"

        if (
            candidate_outcome_quality == "strong"
            and candidate_metrics["unique_charge_types"] > current_metrics["unique_charge_types"]
        ):
            return True, "charge_type_coverage_gain"

        if (
            candidate_outcome_quality == "strong"
            and candidate_metrics["tou_period_count"] > current_metrics["tou_period_count"]
            and candidate_metrics["tou_period_count"] >= 2
        ):
            return True, "tou_coverage_gain"

        if (
            candidate_outcome_quality == "strong"
            and candidate_metrics["completeness_score"] >= current_metrics["completeness_score"] + 2
        ):
            return True, "field_completeness_gain"

        return False, None

    def extract_charges_from_document(self, doc: dict) -> tuple[
        List[ExtractedCharge], str | None, list[ParserProfileCandidate], str, ParserProfileSignals | None, dict, dict[str, Any]
    ]:
        """Extract charges plus selection diagnostics from a single document."""
        if not Path(doc['local_path']).exists():
            logger.warning(f"PDF not found: {doc['local_path']}")
            return [], None, [], "missing_file", None, {}, {}

        # Skip documents classified as redlines. Prefer the Phase 3
        # flag_is_redline classification when available; fall back to the
        # document_fingerprints signal for documents that haven't been
        # through Phase 3 yet.
        redline_classification = doc.get('classification_is_redline')
        if redline_classification == 'true':
            logger.debug(
                f"Skipping doc {doc['id']} ({doc.get('family_key')}): "
                f"flag_is_redline=true (from document_classifications)"
            )
            return (
                [],
                None,
                [],
                "skipped_redline",
                None,
                {},
                {
                    "skip_reason": "redline_classification",
                    "redline_source": "document_classifications",
                },
            )

        if redline_classification is None and int(doc.get('is_redline_candidate') or 0) == 1:
            logger.debug(
                f"Skipping doc {doc['id']} ({doc.get('family_key')}): "
                f"redline_candidate (confidence={doc.get('redline_confidence')})"
            )
            return (
                [],
                None,
                [],
                "skipped_redline",
                None,
                {
                    "redline_confidence": float(doc.get('redline_confidence') or 0.0),
                },
                {
                    "skip_reason": "redline_candidate_fingerprint",
                },
            )

        # Extract text from PDF (or from a cached Docling artifact when
        # no page bounds are set — see extract_text_from_pdf docstring).
        text, text_source = self.extract_text_from_pdf(
            doc['local_path'],
            start_page=doc.get('start_page'),
            end_page=doc.get('end_page')
        )

        if not text:
            logger.warning(f"No text extracted from {doc['local_path']}")
            return [], None, [], "no_text", None, {}, {}

        # Flatten Docling's markdown export to a pdfplumber-shaped layout so
        # parser profiles tuned against the pdfplumber output can recognize
        # tables and headings. No-op on pdfplumber text.
        if text_source in ("docling_artifact", "docling_artifact_sliced"):
            text = normalize_docling_markdown(text)

        # Universal OCR normalization — applied once before any profile sees the text
        text = normalize_ocr_text(text)

        # Classify document (tariff vs procedural). Returns the legacy
        # string label (preserved for the existing short-circuit logic
        # below) plus a ClassificationResult for the Phase 2 document_type
        # stage, persisted as a side effect for observability.
        text_sample = text[:2000]
        doc_type, _document_type_result = self.classifier.classify_with_result(
            doc['title'], text_sample
        )
        self._record_document_type_classification(doc, _document_type_result)
        self._record_flag_classifications(doc, text)
        self._record_embedding_document_type(doc)
        self._record_llm_document_type(doc)

        if doc_type != 'tariff':
            if self._should_force_tariff_for_bounded_leaf(doc, text):
                doc_type = "tariff"
            # Standalone DEP tariff sheets (leaf-no-XXX-rider-YYY.pdf or schedule-XXX.pdf)
            # are always tariff documents even when the classifier returns 'unknown'/'order'.
            if doc_type != "tariff":
                fk = (doc.get("family_key") or "").lower()
                local_path = (doc.get("local_path") or "").lower().replace("\\", "/")
                title_lower = (doc.get("title") or "").lower()
                is_progress_leaf = fk.startswith("nc-progress-leaf-")
                is_carolinas_rider = fk.startswith("nc-carolinas-rider-") or fk.startswith("nc-carolinas-schedule-")
                has_standalone_signal = any(
                    marker in local_path
                    for marker in ("leaf-no-", "leaf_", "rider-", "rider_", "schedule-", "schedule_", "nc-rider-", "ncride", "ncschedule")
                ) or any(
                    marker in title_lower
                    for marker in ("leaf no.", "leaf ", "rider ", "schedule ")
                ) or "leaf no." in text.lower()  # tariff body identifies itself as a Leaf sheet
                has_rate_content = (
                    "monthly rate" in text.lower()
                    or "rider " in text.lower()
                    or "/kwh" in text.lower()
                    or "¢/kwh" in text.lower()
                    or "per kilowatt-hour" in text.lower()
                    or "per kilowatt hour" in text.lower()
                    or "cents per kilowatt" in text.lower()
                    or "per kwh" in text.lower()
                    or "perkwh" in text.lower()
                    # Fixed monthly charges (CEPS, flat-fee riders): "$/month" or "per month"
                    or "$/month" in text
                    or "monthly charge" in text.lower()
                    or "per agreement per month" in text.lower()
                )
                if (
                    (is_progress_leaf or is_carolinas_rider)
                    and has_standalone_signal
                    and (
                        "leaf no." in text.lower()
                        or "rider " in text.lower()
                        or "schedule " in text.lower()
                    )
                    and has_rate_content
                ):
                    doc_type = "tariff"
                # Carolinas rider/schedule current-document PDFs whose filename encodes
                # the rider code without an explicit "rider " token (e.g. ncfuelcostadjrdr.pdf).
                # Accept them if they contain rate content and have a known rider family key.
                # Also accept FCAR annual application filings whose page text uses
                # "fuel and fuel-related costs factors" (the application summary format,
                # not the Leaf 60 tariff format).
                elif (
                    is_carolinas_rider
                    and has_rate_content
                    and (
                        "fuel cost adjustment" in text.lower()
                        or ("fuel and fuel-related" in text.lower() and "residential" in text.lower())
                    )
                ):
                    doc_type = "tariff"
                # Carolinas EDPR and similar adjustment rider filings: the tariff rate is
                # embedded in cover-letter text as "X cents per kWh". Classifier returns
                # 'order' because it sees the letterhead first, but the family_key and
                # DSM program text confirm this is a rider tariff filing.
                elif (
                    is_carolinas_rider
                    and "existing dsm program" in text.lower()
                    and ("cents per kwh" in text.lower() or "/kwh" in text.lower())
                ):
                    doc_type = "tariff"
                # Carolinas rider single-page tariff sheets (e.g. EDIT-4, RIDER EDIT-3)
                # imported from compliance filings. Title is "EDIT-4 (Span N-N)" with no
                # "rider " token; text body contains "RIDER EDIT-4 (NC)" and a rate table.
                elif (
                    is_carolinas_rider
                    and has_rate_content
                    and "rider " in text.lower()
                    and (
                        "applicable schedules" in text.lower()
                        or "billing rate" in text.lower()
                        or "decremental rate" in text.lower()
                        or "incremental rate" in text.lower()
                    )
                ):
                    doc_type = "tariff"
            # For large multi-schedule filings, the cover letter is non-tariff but
            # tariff schedules appear later. Scan pages to find tariff content.
            if doc_type != "tariff" and pdfplumber and doc.get('start_page') is None:
                doc_type = self._find_tariff_type_in_pages(
                    doc['local_path'], doc['title']
                )
            if doc_type != 'tariff':
                logger.debug(f"Skipping non-tariff document {doc['id']}: classified as {doc_type}")
                return [], None, [], f"skipped_{doc_type}", None, {}, {}
            # Re-extract full text now that we know it contains tariff content.
            # Skip if page bounds are set — the page-bounded extraction is already correct
            # and a full-document re-extraction would include unrelated schedules.
            if doc.get('start_page') is None:
                text, text_source = self.extract_text_from_pdf(doc['local_path'])

        if self._is_reference_only_document(doc, text):
            nonblank_lines = [line for line in text.splitlines() if line.strip()]
            return (
                [],
                None,
                [],
                "skipped_reference",
                None,
                {
                    "text_length": len(text),
                    "line_count": len(nonblank_lines),
                    "numeric_line_count": sum(1 for line in nonblank_lines if any(ch.isdigit() for ch in line)),
                },
                {
                    "skip_reason": "reference_only_family",
                },
            )

        if self._is_formula_only_document(doc, text):
            nonblank_lines = [line for line in text.splitlines() if line.strip()]
            return (
                [],
                None,
                [],
                "skipped_formula",
                None,
                {
                    "text_length": len(text),
                    "line_count": len(nonblank_lines),
                    "numeric_line_count": sum(1 for line in nonblank_lines if any(ch.isdigit() for ch in line)),
                },
                {
                    "skip_reason": "formula_based_customer_specific_rider",
                },
            )

        # Extract charges using a parser profile selected by family/company/structure.
        signals = self.parser_registry.build_signals(doc, text)
        ranked_candidates = self.parser_registry.rank_candidates(doc, text)
        profile = self.parser_registry.select(doc, text)
        initial_profile_name = profile.name
        # parse_warnings collects ValueError-style failures (typically OCR-malformed
        # numbers like '1.631.88') so we record actionable context instead of failing
        # the entire document. Any other exception still propagates.
        parse_warnings: list[dict[str, str]] = []
        charges = self._safe_profile_extract(profile, doc, text, parse_warnings)
        initial_status = "parsed" if charges else "empty"
        initial_selected_score = self._score_for_profile(initial_profile_name, ranked_candidates)
        initial_outcome_quality, initial_review_flags = self._assess_extraction_outcome(
            parser_profile=initial_profile_name,
            ranked_candidates=ranked_candidates,
            charge_count=len(charges),
            status=initial_status,
            selected_score=initial_selected_score,
        )
        initial_metrics = self._charge_set_metrics(charges)
        fallback_candidates = self.parser_registry.recommend_fallback_sequence(
            doc,
            text,
            ranked_candidates=ranked_candidates,
            selected_name=initial_profile_name,
            limit=3,
        )
        fallback_attempts: list[dict[str, object]] = []
        final_profile_name = initial_profile_name
        fallback_reason: str | None = None
        fallback_triggered_by = "empty" if not charges else ("weak" if initial_outcome_quality == "weak" else None)

        best_charges = charges
        best_profile_name = initial_profile_name
        best_outcome_quality = initial_outcome_quality
        best_metrics = initial_metrics
        for candidate in fallback_candidates:
            fallback_profile = self.parser_registry.get_profile(candidate.name)
            if fallback_profile is None:
                continue
            candidate_charges = self._safe_profile_extract(fallback_profile, doc, text, parse_warnings)
            candidate_status = "parsed" if candidate_charges else "empty"
            candidate_outcome_quality, _ = self._assess_extraction_outcome(
                parser_profile=candidate.name,
                ranked_candidates=ranked_candidates,
                charge_count=len(candidate_charges),
                status=candidate_status,
                selected_score=candidate.score,
            )
            candidate_metrics = self._charge_set_metrics(candidate_charges)
            should_apply, candidate_reason = self._should_apply_fallback(
                current_profile_name=best_profile_name,
                current_charge_count=len(best_charges),
                current_outcome_quality=best_outcome_quality,
                current_metrics=best_metrics,
                candidate_name=candidate.name,
                candidate_charge_count=len(candidate_charges),
                candidate_outcome_quality=candidate_outcome_quality,
                candidate_metrics=candidate_metrics,
                has_page_bounds=doc.get("start_page") is not None,
            )
            fallback_attempts.append(
                self._serialize_candidate(candidate)
                | {
                    "charge_count": len(candidate_charges),
                    "outcome_quality": candidate_outcome_quality,
                    "metrics": candidate_metrics,
                    "applied": should_apply,
                    "apply_reason": candidate_reason,
                }
            )
            if should_apply:
                best_charges = candidate_charges
                best_profile_name = fallback_profile.name
                best_outcome_quality = candidate_outcome_quality
                best_metrics = candidate_metrics
                fallback_reason = candidate_reason

        charges = best_charges
        final_profile_name = best_profile_name

        status = "parsed" if charges else "empty"
        final_selected_score = self._score_for_profile(final_profile_name, ranked_candidates)
        final_outcome_quality, final_review_flags = self._assess_extraction_outcome(
            parser_profile=final_profile_name,
            ranked_candidates=ranked_candidates,
            charge_count=len(charges),
            status=status,
            selected_score=final_selected_score,
        )
        selection_details = {
            "initial_parser_profile": initial_profile_name,
            "final_parser_profile": final_profile_name,
            "fallback_applied": final_profile_name != initial_profile_name,
            "fallback_triggered_by": fallback_triggered_by,
            "fallback_reason": fallback_reason,
            "fallback_candidates": [self._serialize_candidate(candidate) for candidate in fallback_candidates],
            "fallback_attempts": fallback_attempts,
            "initial_outcome_quality": initial_outcome_quality,
            "initial_review_flags": initial_review_flags,
            "initial_metrics": initial_metrics,
            "final_outcome_quality": final_outcome_quality,
            "final_review_flags": final_review_flags,
            "final_metrics": best_metrics,
            "parse_warnings": parse_warnings,
            # top_candidates is always populated, even when the final outcome is
            # `unknown` — it records the highest-scoring profiles considered so
            # auditors can see what almost matched and why it was rejected.
            "top_candidates": [
                self._serialize_candidate(candidate) for candidate in ranked_candidates[:5]
            ],
        }
        nonblank_lines = [line for line in text.splitlines() if line.strip()]
        text_metrics = {
            "text_length": len(text),
            "line_count": len(nonblank_lines),
            "numeric_line_count": sum(1 for line in nonblank_lines if any(ch.isdigit() for ch in line)),
            "full_text": text,
            # Recorded so parser regressions on Docling-text docs can be
            # distinguished from regressions on pdfplumber-text docs.
            "text_source": text_source,
        }

        logger.info(
            f"Extracted {len(charges)} charges from {doc['family_key']} "
            f"(doc {doc['id']}, effective {doc['effective_start']}, profile={final_profile_name})"
        )
        if ranked_candidates:
            logger.debug(
                "Profile candidates for %s: %s",
                doc["family_key"],
                ", ".join(
                    f"{candidate.name}={candidate.score:.2f}[{';'.join(candidate.reasons) or 'no_reasons'}]"
                    for candidate in ranked_candidates[:3]
                ),
            )

        return charges, final_profile_name, ranked_candidates, status, signals, text_metrics, selection_details

    def _find_tariff_type_in_pages(self, pdf_path: str, title: str,
                                   max_pages: int = 20) -> str:
        """Scan up to max_pages looking for a page that classifies as tariff."""
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages[:max_pages]:
                    page_text = page.extract_text() or ''
                    if self.classify_document(title, page_text) == 'tariff':
                        return 'tariff'
        except Exception as e:
            logger.warning(f"Error scanning pages of {pdf_path}: {e}")
        return 'other'

    def _should_force_tariff_for_bounded_leaf(self, doc: dict, text: str) -> bool:
        if doc.get("start_page") is None or doc.get("end_page") is None:
            return False

        lowered = text.lower()
        family_key = (doc.get("family_key") or "").lower()
        title = (doc.get("title") or "").lower()

        has_tariff_family = (
            family_key.startswith("nc-progress-leaf-")
            or family_key.startswith("nc-carolinas-leaf-")
            or "rider" in title
            or "schedule" in title
        )
        has_tariff_markers = any(marker in lowered for marker in self._BOUNDED_TARIFF_OVERRIDE_MARKERS)
        has_leaf_no = bool(re.search(r"leaf\s+no\.?\s*\d{1,4}", lowered))

        # Case 1 (original): explicit "Leaf No." text + tariff markers + known family
        if has_leaf_no and has_tariff_markers and has_tariff_family:
            return True

        # Case 2: compliance bundles whose page-range header text lacks "Leaf No." but
        # the family_key is already a known nc-progress-leaf-* (so we know it's a tariff)
        # and the pdfplumber text has at least one tariff-content marker.
        if (
            has_tariff_markers
            and (
                family_key.startswith("nc-progress-leaf-")
                or family_key.startswith("nc-carolinas-leaf-")
            )
        ):
            return True

        # Case 2b: incentive/pilot programs (e.g. PowerPair leaf-770) that use
        # $/Watt or $/kWh incentive rates rather than traditional tariff markers.
        if (
            family_key.startswith("nc-progress-leaf-")
            and doc.get("start_page") is not None
            and any(m in lowered for m in ("/watt", "per watt", "powerpair", "incentive for solar", "incentive for battery"))
        ):
            return True

        # Case 3: bounded Carolinas rider/schedule documents with tariff markers.
        # The LLM classifier can misread rider content as "order" or "other" when
        # the text sample doesn't have strong schedule signals; the page-bounded
        # extraction already restricts scope to the correct pages.
        if (
            has_tariff_markers
            and has_tariff_family
            and (
                family_key.startswith("nc-carolinas-rider-")
                or family_key.startswith("nc-carolinas-schedule-")
            )
        ):
            return True

        return False

    def _is_reference_only_document(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        title = (doc.get("title") or "").lower()
        lowered = text.lower()

        # Family-vs-content mismatch: rider docs whose page-bounded slice landed on
        # a different schedule's text (a known span/title classification bug). When
        # a rider family's text mentions a schedule keyword that doesn't match the
        # rider, AND lacks the rider's own keyword, treat as reference_only.
        # Examples surfaced this session: EDPR slices containing FL-N or TS;
        # BPMPROSPECTIVERIDER slices containing SGST.
        # family_key -> (rider-specific markers that ONLY appear in actual rider
        # rate docs, not in schedule riders-list references; mismatch schedule headers)
        rider_mismatch_families: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
            "nc-carolinas-rider-edpr": (
                # "rider edpr" header / "existing dsm program costs adjustment rider (nc)"
                # appears in actual EDPR rate doc but NOT in schedule riders lists
                # (which use the longer phrase only as a leaf reference label).
                ("rider edpr", "rider edpr (nc)", "(rider edpr)"),
                ("schedule fl-n", "schedule ts", "schedule sgst",
                 "schedule lgs", "schedule pg", "schedule sgs", "schedule rs",
                 "floodlighting service"),
            ),
            "nc-carolinas-rider-bpmprospectiverider": (
                # Actual BPM Prospective Rider rate doc has "rider bpmpr" or
                # "bpm prospective rider (nc)" header — schedule-list references
                # use just "BPM Prospective Rider".
                ("rider bpmpr", "bpm prospective rider (nc)"),
                ("schedule sgst", "schedule fl-n", "schedule ts",
                 "schedule lgs", "schedule pg", "schedule sgs", "schedule rs"),
            ),
            "nc-carolinas-rider-bpmppttrueup": (
                ("rider bpmpt", "bpm true-up rider (nc)"),
                ("schedule sgst", "schedule fl-n", "schedule ts",
                 "schedule lgs", "schedule pg", "schedule sgs", "schedule rs"),
            ),
        }
        if family_key in rider_mismatch_families:
            rider_markers, mismatch_markers = rider_mismatch_families[family_key]
            has_rider_keyword = any(m in lowered for m in rider_markers)
            has_mismatch = any(m in lowered for m in mismatch_markers)
            if has_mismatch and not has_rider_keyword:
                return True

        if family_key == "nc-progress-leaf-672" and lowered:
            # Span-matched docs from compliance bundles may be legal briefs or procedural
            # filings that mention CEI incidentally. If the text lacks any CEI content,
            # treat as reference_only rather than surfacing as an unresolved parser gap.
            if "clean energy impact" not in lowered and "rider cei" not in lowered:
                return True

        if family_key == "nc-carolinas-rider-scg":
            if (
                "small customer generator" not in lowered
                and "schedule rt" in lowered
                and "residential service, time of use" in lowered
            ):
                return True
            if (
                "rider scg" in lowered
                and "small customer generator" not in lowered
                and "determination of on-peak and off-peak energy" in lowered
                and "safety" in lowered
                and "supplemental basic" not in lowered
                and "standby charge" not in lowered
            ):
                return True
        if family_key == "nc-carolinas-rider-nm":
            if (
                "rider nm" in lowered
                and "net metering" in lowered
                and "metering requirements" in lowered
                and "safety" in lowered
                and "standby charge of $" not in lowered
                and "minimum bill set at $" not in lowered
            ):
                return True

        family_signal = family_key in self._REFERENCE_ONLY_FAMILIES
        title_signal = any(token in title for token in self._REFERENCE_TITLE_TOKENS)
        if not family_signal and not title_signal:
            return False

        # Explicit family membership is authoritative — skip the rate-marker veto.
        # Multi-schedule Service Regulations bundles contain embedded rate text from
        # the schedules they govern, which would otherwise defeat the veto checks.
        if family_signal:
            return True

        if any(marker in lowered for marker in self._REFERENCE_RATE_MARKERS):
            return False
        if re.search(r"\$\s*\d+\.?\d*\s*(?:per|/)\s*(?:month|bill|kwh|kw)\b", lowered):
            return False
        if re.search(r"\d+\.?\d*\s*(?:¢|c)\s+per\s+kwh", lowered):
            return False
        return True

    def _is_formula_only_document(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        lowered = text.lower()
        title_lowered = (doc.get("title") or "").lower()

        # Some historical program leaves have missing OCR/plaintext in the live DB.
        # Allow known formula-only families to skip based on title cues so they do
        # not linger as unexplained unknown/empty parser outcomes.
        if not lowered:
            title_hints = self._FORMULA_ONLY_TITLE_HINTS.get(family_key)
            if title_hints and any(hint in title_lowered for hint in title_hints):
                return True
        if family_key == "nc-progress-leaf-660":
            return (
                "premier power service" in lowered
                and ("monthly service payment" in lowered or "monthly services payment" in lowered)
                and "capital cost" in lowered
                and "expenses" in lowered
            )
        if family_key == "nc-carolinas-schedule-nl":
            # Carolinas Schedule NL "Nonstandard Lighting Service (Pilot)" computes
            # the rate as Levelized Capital Cost + Expenses + (Energy × cents/kWh).
            # Capital and expense components are per-customer custom, not in the
            # tariff. Treat as formula-only — same pattern as Progress leaf-660.
            return (
                "schedule nl" in lowered
                and ("monthly services payment" in lowered or "monthly service payment" in lowered)
                and ("levelized capital cost" in lowered or "capital cost" in lowered)
                and "expenses" in lowered
            )
        if family_key == "nc-carolinas-schedule-hp":
            # Carolinas Schedule HP "Hourly Pricing for Incremental Load" computes
            # the rate from a per-customer Customer Baseline Load (CBL), hourly
            # Rationing Charge rates, and incremental demand. The handful of fixed
            # values (e.g. 52.99¢/kW incremental demand) are not enough to represent
            # the schedule for bill computation. Treat as formula-only.
            return (
                "schedule hp" in lowered
                and "hourly pricing" in lowered
                and (
                    "baseline charge" in lowered
                    or "rationing charge" in lowered
                    or "calculated from cbl" in lowered
                )
            )
        if family_key == "nc-progress-leaf-672":
            formula_markers = (
                "market price per block",
                "set annually",
                "administrative fee",
                "clean energy environmental attributes",
                "option to purchase",
                "block of ceeas",
                "blocks of clean energy environmental attributes",
            )
            return (
                "rider cei" in lowered
                and "clean energy impact" in lowered
                and sum(marker in lowered for marker in formula_markers) >= 2
            )
        if family_key == "nc-progress-leaf-712":
            return (
                "low-income weatherization pay for performance" in lowered
                and "payment levels" in lowered
                and "posted on the company" in lowered
                and "website" in lowered
            )
        if family_key == "nc-progress-leaf-721":
            return (
                "tariffed on-bill program" in lowered
                and "monthly service charge =" in lowered
                and "total amount paid for measures" in lowered
                and "participant co-payment" in lowered
            )
        if family_key == "nc-progress-leaf-720":
            return (
                "prepaid advantage program" in lowered
                and "minimum initial payment" in lowered
                and "daily cost of electricity" in lowered
                and "balance becomes zero" in lowered
            )
        if family_key == "nc-progress-leaf-719":
            return (
                "weatherization program" in lowered
                and "payments will be made to the administering agency" in lowered
                and "up to an annual average of $" in lowered
            )
        if family_key == "nc-progress-leaf-701":
            return (
                "business energy saver program" in lowered
                and "incentive payments" in lowered
                and "project completion form" in lowered
                and "installed energy efficiency measure cost" in lowered
            )
        if family_key == "nc-progress-leaf-702":
            return (
                "smart $aver performance incentive program" in lowered
                and "estimated total project savings" in lowered
                and "first year kwh reduction multiplied" in lowered
            )
        if family_key == "nc-progress-leaf-708":
            return (
                "residential new construction program" in lowered
                and "program costs by year" in lowered
                and "program costs per participant" in lowered
            )
        if family_key == "nc-progress-leaf-723":
            return (
                "smart $aver" in lowered
                and ("early replacement and retrofit" in lowered or "tobr" in lowered)
                and "current amount of the incentive payment" in lowered
                and "company" in lowered
                and "website" in lowered
            )
        if family_key == "nc-progress-leaf-640":
            return (
                "energy conservation discount" in lowered
                and "recd credit =" in lowered
                and "5% times" in lowered
                and "incremental adjustment rate" in lowered
            )
        if family_key == "nc-progress-leaf-663":
            # If fixed $/watt rebate amounts are present, this is extractable — not formula-only
            import re as _re
            has_fixed_watt_rates = bool(_re.search(r"\$0\.\d+\s+per\s+watt", text, _re.I))
            if has_fixed_watt_rates:
                return False
            return (
                "solar rebate rider srr" in lowered
                and "application period" in lowered
                and "rebate payment amount" in lowered
                and "early termination charge shall equal" in lowered
            )
        if family_key == "nc-carolinas-rider-ee":
            has_adjustment_values = (
                "energy efficiency rider adjustments" in lowered
                or "total residential rate" in lowered
                or "total nonresidential" in lowered
                or "vintage 1 total" in lowered
            )
            return (
                "rider ee" in lowered
                and "energy efficiency rider" in lowered
                and ("eea residential" in lowered or "determination of energy efficiency rider adjustment" in lowered)
                and not has_adjustment_values
            )
        return False

    def insert_charges(self, version_id: int, family_key: str,
                      charges: List[ExtractedCharge]) -> int:
        """Insert extracted charges into tariff_charges table.

        Clears existing charges for this (version_id, family_key) before inserting
        so that reprocessing replaces rather than appends.
        """
        if not charges or not version_id:
            return 0

        conn = self._get_connection()
        try:
            inserted = 0
            now = datetime.now(UTC).isoformat()

            # Delete existing charges first so reprocessing replaces, not appends.
            conn.execute(
                "DELETE FROM tariff_charges WHERE version_id = ? AND family_key = ?",
                (version_id, family_key),
            )

            for charge in charges:
                try:
                    conn.execute("""
                        INSERT INTO tariff_charges (
                            version_id, family_key, charge_type, charge_label,
                            rate_value, rate_unit, tier_min, tier_max,
                            tou_period, season, source_snippet, confidence_score,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        version_id,
                        family_key,
                        charge.charge_type,
                        charge.charge_label,
                        charge.rate_value,
                        charge.rate_unit,
                        charge.tier_min,
                        charge.tier_max,
                        charge.tou_period,
                        charge.season,
                        charge.source_snippet,
                        charge.confidence_score,
                        now
                    ))
                    inserted += 1
                except Exception as e:
                    logger.error(f"Error inserting charge: {e}")
                    continue

            conn.commit()
            return inserted
        finally:
            conn.close()

    @staticmethod
    def _utility_from_company(company: str | None) -> str | None:
        if company == "progress":
            return "DEP"
        if company == "carolinas":
            return "DEC"
        return None

    def _load_page_artifacts_for_document(self, doc: dict) -> list[dict[str, Any]]:
        conn = self._get_connection()
        try:
            pages = load_page_artifacts(
                conn,
                source_pdf=str(doc.get("local_path") or ""),
                file_hash=doc.get("content_hash"),
            )
        finally:
            conn.close()
        return [page.model_dump(mode="json") for page in pages]

    def _analyze_document_intelligence(
        self,
        doc: dict,
        *,
        raw_text: str,
        parser_profile: str | None,
        charge_count: int,
        status: str,
    ) -> dict[str, Any] | None:
        try:
            snapshot = self.document_intelligence.analyze_historical_document(
                doc,
                raw_text=raw_text,
                page_artifacts=self._load_page_artifacts_for_document(doc),
                context=HistoricalDocumentIntelligenceContext(
                    parser_profile=parser_profile,
                    charge_count=charge_count,
                    status=status,
                    errors=[],
                ),
            )
        except Exception as exc:
            logger.warning(
                "Document-intelligence analysis failed for doc %s: %s",
                doc.get("id"),
                exc,
            )
            return None
        return {
            "fingerprint": snapshot.fingerprint.model_dump(mode="json"),
            "extraction": snapshot.extraction.model_dump(mode="json"),
            "validation": snapshot.validation.model_dump(mode="json"),
            "confidence": snapshot.confidence.model_dump(mode="json"),
            "training_record": {
                "source_pdf": snapshot.training_record.source_pdf,
                "historical_document_id": snapshot.training_record.historical_document_id,
                "doc_type": snapshot.training_record.doc_type,
                "parse_lane": snapshot.training_record.parse_lane,
                "parser_used": snapshot.training_record.parser_used,
            },
        }

    def record_parse_attempt(
        self,
        doc: dict,
        *,
        parser_profile: str | None,
        ranked_candidates: list[ParserProfileCandidate],
        signals: ParserProfileSignals | None,
        text_metrics: dict[str, Any] | None,
        charge_count: int,
        status: str,
        selection_details: dict[str, Any] | None = None,
        document_intelligence: dict[str, Any] | None = None,
    ) -> int:
        """Persist parser-selection diagnostics for later analysis."""
        conn = self._get_connection()
        try:
            now = datetime.now(UTC).isoformat()
            selected_score = self._score_for_profile(parser_profile, ranked_candidates)
            outcome_quality, review_flags = self._assess_extraction_outcome(
                parser_profile=parser_profile,
                ranked_candidates=ranked_candidates,
                charge_count=charge_count,
                status=status,
                selected_score=selected_score,
            )

            metadata = {
                "historical_document_id": doc["id"],
                "family_key": doc["family_key"],
                "company": doc.get("company"),
                "title": doc.get("title"),
                "outcome_quality": outcome_quality,
                "selected_profile_score": selected_score,
                "text_metrics": text_metrics or {},
                "signals": signals.to_metadata() if signals else {},
                "candidate_profiles": [
                    self._serialize_candidate(candidate)
                    for candidate in ranked_candidates
                ],
                "selection": selection_details or {},
                "document_intelligence": document_intelligence or {},
            }

            cur = conn.execute(
                """
                INSERT INTO parse_attempt_logs (
                    source_pdf, docket_dir, page_start, page_end, parser_stage,
                    parser_profile, status, confidence, utility, schedule_code,
                    effective_date, charge_count, review_flags_json, metadata_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    doc["local_path"],
                    None,
                    doc.get("start_page"),
                    doc.get("end_page"),
                    "historical_bulk",
                    parser_profile,
                    status,
                    selected_score,
                    self._utility_from_company(doc.get("company")),
                    None,
                    doc.get("effective_start"),
                    charge_count,
                    json.dumps(review_flags),
                    json.dumps(metadata, sort_keys=True),
                    now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def record_review_outcome(
        self,
        doc: dict,
        *,
        parse_attempt_id: int,
        parser_profile: str | None,
        ranked_candidates: list[ParserProfileCandidate],
        signals: ParserProfileSignals | None,
        charge_count: int,
        status: str,
        selection_details: dict[str, Any] | None = None,
    ) -> None:
        """Persist an initial rule-based review outcome for a parse attempt."""
        conn = self._get_connection()
        try:
            selected_score = self._score_for_profile(parser_profile, ranked_candidates)
            outcome_quality, review_flags = self._assess_extraction_outcome(
                parser_profile=parser_profile,
                ranked_candidates=ranked_candidates,
                charge_count=charge_count,
                status=status,
                selected_score=selected_score,
            )
            review_outcome = ParseReviewOutcome(
                parse_attempt_id=parse_attempt_id,
                source_pdf=doc["local_path"],
                docket_dir=None,
                page_start=doc.get("start_page"),
                page_end=doc.get("end_page"),
                parser_stage="historical_bulk",
                parser_profile=parser_profile,
                utility=self._utility_from_company(doc.get("company")),
                review_source="rule",
                outcome="accepted" if outcome_quality in {"strong", "skipped"} else "needs_review",
                notes={
                    "historical_document_id": doc["id"],
                    "family_key": doc.get("family_key"),
                    "company": doc.get("company"),
                    "title": doc.get("title"),
                    "status": status,
                    "outcome_quality": outcome_quality,
                    "review_flags": review_flags,
                    "selected_profile_score": selected_score,
                    "signals": signals.to_metadata() if signals else {},
                    "selection": selection_details or {},
                },
            )
            conn.execute(
                """
                INSERT INTO parse_review_outcomes (
                    parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
                    parser_stage, parser_profile, utility, review_source, outcome,
                    correction_count, notes_json, corrections_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    review_outcome.parse_attempt_id,
                    review_outcome.source_pdf,
                    review_outcome.docket_dir,
                    review_outcome.page_start,
                    review_outcome.page_end,
                    review_outcome.parser_stage,
                    review_outcome.parser_profile,
                    review_outcome.utility,
                    review_outcome.review_source,
                    review_outcome.outcome,
                    review_outcome.correction_count,
                    json.dumps(review_outcome.notes, sort_keys=True),
                    json.dumps(review_outcome.corrections, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def record_document_fingerprint(
        self,
        doc: dict,
        *,
        signals: ParserProfileSignals | None,
        text_metrics: dict,
        parser_profile: str | None,
        ranked_candidates: list[ParserProfileCandidate],
        status: str,
        charge_count: int,
        selection_details: dict[str, Any] | None = None,
    ) -> None:
        """Persist shared document/profile signals into document_fingerprints."""
        conn = self._get_connection()
        try:
            source_pdf = doc["local_path"]
            page_start = doc.get("start_page")
            page_end = doc.get("end_page")
            leaf_no = signals.leaf_no if signals else None
            schedule_code = None
            family_key = (doc.get("family_key") or "").lower()
            if "-schedule-" in family_key:
                schedule_code = family_key.split("-schedule-", 1)[1].upper()
            elif family_key.endswith("-summary"):
                schedule_code = "SUMMARY_OF_RIDERS"

            existing = conn.execute(
                """
                SELECT id FROM document_fingerprints
                WHERE source_pdf = ? AND page_start IS ? AND page_end IS ?
                  AND leaf_no IS ? AND schedule_code IS ?
                """,
                (source_pdf, page_start, page_end, leaf_no, schedule_code),
            ).fetchone()
            now = datetime.now(UTC).isoformat()
            selected_score = self._score_for_profile(parser_profile, ranked_candidates)
            outcome_quality, review_flags = self._assess_extraction_outcome(
                parser_profile=parser_profile,
                ranked_candidates=ranked_candidates,
                charge_count=charge_count,
                status=status,
                selected_score=selected_score,
            )

            # --- Redline detection ---
            full_text = text_metrics.get("full_text") or ""
            redline_lines = [
                line for line in full_text.splitlines()
                if REDLINE_MARKER_REGEX.search(line)
            ]
            has_dual = bool(DUAL_RATE_REGEX.search(full_text))
            total_lines = max(1, int(text_metrics.get("line_count") or 1))
            redline_confidence = round(
                min(1.0, (len(redline_lines) / total_lines) + (0.3 if has_dual else 0.0)),
                4,
            )
            is_redline = redline_confidence > 0.1 or has_dual
            if is_redline and "redline_candidate" not in review_flags:
                review_flags = list(review_flags) + ["redline_candidate"]

            # --- Compliance book detection ---
            book_signals: dict = {
                "is_compliance_book": False,
                "has_toc_page": False,
                "unique_leaf_nos": [],
                "leaf_span_count": 0,
                "confidence": 0.0,
            }
            local_path = doc.get("local_path") or ""
            if local_path and Path(local_path).exists():
                try:
                    pages = mine_document_pages(local_path, max_pages=20)
                    book_signals = classify_compliance_book(pages)
                except Exception:
                    pass
            is_book = int(book_signals["is_compliance_book"])

            # --- Quality tier ---
            # Prefer discovery_record acquisition_method if available via doc dict.
            acq_method = doc.get("acquisition_method")
            docket_number = doc.get("docket_number")
            doc_quality_tier = doc.get("discovery_doc_quality_tier") or infer_doc_quality_tier(
                local_path, acq_method, docket_number
            )

            params = (
                source_pdf,
                None,
                page_start,
                page_end,
                leaf_no,
                schedule_code,
                doc.get("title"),
                int(text_metrics.get("text_length") or 0),
                int(text_metrics.get("line_count") or 0),
                int(text_metrics.get("numeric_line_count") or 0),
                int(bool(signals and (signals.has_summary_text or signals.has_tou_terms))),
                int(bool(signals and signals.has_summary_text)),
                int(is_redline),
                round(redline_confidence, 4),
                doc_quality_tier,
                is_book,
                json.dumps(review_flags),
                json.dumps(
                    {
                        "family_key": doc.get("family_key"),
                        "company": doc.get("company"),
                        "parser_profile": parser_profile,
                        "status": status,
                        "outcome_quality": outcome_quality,
                        "selected_profile_score": selected_score,
                        "charge_count": charge_count,
                        "signals": signals.to_metadata() if signals else {},
                        "selection": selection_details or {},
                        "book_signals": book_signals,
                    },
                    sort_keys=True,
                ),
                now,
            )
            if existing:
                conn.execute(
                    """
                    UPDATE document_fingerprints SET
                        docket_dir=?, page_start=?, page_end=?, leaf_no=?, schedule_code=?,
                        title=?, text_length=?, line_count=?, numeric_line_count=?,
                        has_table_rows=?, has_rider_summary=?,
                        is_redline_candidate=?, redline_confidence=?,
                        doc_quality_tier=?, is_compliance_book=?,
                        review_flags_json=?, metadata_json=?, created_at=?
                    WHERE id=?
                    """,
                    params[1:] + (existing["id"],),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO document_fingerprints (
                        source_pdf, docket_dir, page_start, page_end, leaf_no,
                        schedule_code, title, text_length, line_count, numeric_line_count,
                        has_table_rows, has_rider_summary,
                        is_redline_candidate, redline_confidence,
                        doc_quality_tier, is_compliance_book,
                        review_flags_json, metadata_json, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    params,
                )
            conn.commit()
        finally:
            conn.close()

    def _assess_extraction_outcome(
        self,
        *,
        parser_profile: str | None,
        ranked_candidates: list[ParserProfileCandidate],
        charge_count: int,
        status: str,
        selected_score: float | None = None,
    ) -> tuple[str, list[str]]:
        effective_score = (
            selected_score
            if selected_score is not None
            else self._score_for_profile(parser_profile, ranked_candidates)
        )
        review_flags: list[str] = []

        if status.startswith("skipped_"):
            review_flags.append(status)
            return "skipped", review_flags
        if status in {"no_text", "missing_file"}:
            review_flags.append(status)
            return "failed", review_flags
        if charge_count == 0:
            review_flags.append("no_charges_extracted")
            return "empty", review_flags

        if parser_profile == "generic_residential":
            review_flags.append("generic_fallback_selected")
            # Stricter gate: when the chosen profile IS the generic fallback AND the
            # selector score is below threshold, surface it explicitly so routing
            # treats it as weak even if charges were extracted. Threshold is lower
            # than low_selector_confidence (0.65) because we already know we are
            # in fallback territory; 0.50 keeps the flag rare enough to be useful.
            if effective_score is not None and effective_score < 0.50:
                review_flags.append("fallback_below_threshold")
        if effective_score and effective_score < 0.65:
            review_flags.append("low_selector_confidence")
        if (
            parser_profile in {
                "progress_single_value_rider",
                "progress_specialty_rider",
                "progress_greenpower_program",
                "carolinas_single_value_rider",
                "carolinas_flat_fee_rider",
                "carolinas_nuclear_production_tax_credits",
            }
            and charge_count == 1
            and (effective_score or 0.0) >= 0.8
        ):
            return ("strong" if not review_flags else "weak"), review_flags
        if charge_count <= 1:
            review_flags.append("sparse_charge_set")

        return ("strong" if not review_flags else "weak"), review_flags

    def record_processing_run(
        self,
        doc: dict,
        *,
        parser_profile: str | None,
        ranked_candidates: list[ParserProfileCandidate],
        signals: ParserProfileSignals | None,
        text_metrics: dict[str, Any] | None,
        charge_count: int,
        status: str,
        selection_details: dict[str, Any] | None = None,
        processing_mode: str = "historical_bulk",
        document_intelligence: dict[str, Any] | None = None,
    ) -> int:
        """Persist a versioned processing run for targeted reprocessing decisions."""
        conn = self._get_connection()
        try:
            profile_confidence = self._score_for_profile(parser_profile, ranked_candidates)
            outcome_quality, review_flags = self._assess_extraction_outcome(
                parser_profile=parser_profile,
                ranked_candidates=ranked_candidates,
                charge_count=charge_count,
                status=status,
                selected_score=profile_confidence,
            )
            run_id = record_historical_processing_run(
                conn,
                historical_document_id=int(doc["id"]),
                source_pdf=doc["local_path"],
                family_key=doc.get("family_key"),
                content_hash=doc.get("content_hash"),
                parser_stage="historical_bulk",
                parser_profile=parser_profile,
                parser_version=HISTORICAL_BULK_PARSER_VERSION,
                processing_mode=processing_mode,
                status=status,
                outcome_quality=outcome_quality,
                charge_count=charge_count,
                review_flags=review_flags,
                metadata={
                    "company": doc.get("company"),
                    "effective_start": doc.get("effective_start"),
                    "start_page": doc.get("start_page"),
                    "end_page": doc.get("end_page"),
                    "profile_confidence": profile_confidence,
                    "text_metrics": text_metrics or {},
                    "signals": signals.to_metadata() if signals else {},
                    "candidate_profiles": [
                        self._serialize_candidate(candidate)
                        for candidate in ranked_candidates
                    ],
                    "selection": selection_details or {},
                    "document_intelligence": document_intelligence or {},
                },
            )
            conn.commit()
            return run_id
        finally:
            conn.close()

    def process_document(self, doc: dict) -> Tuple[int, str, int, str, str | None]:
        """Process a single document: extract and insert charges.

        Returns: (doc_id, family_key, num_inserted, status, parser_profile)
        """
        # Get tariff_version for this document
        version_id = doc.get("version_id") or self.get_tariff_version_for_document(doc['id'])
        if not version_id:
            logger.warning(f"No tariff_version found for doc {doc['id']}")
            return doc['id'], doc['family_key'], 0, "missing_version", None

        # Extract charges from document
        charges, parser_profile, ranked_candidates, status, signals, text_metrics, selection_details = self.extract_charges_from_document(doc)
        document_intelligence = self._analyze_document_intelligence(
            doc,
            raw_text=str(text_metrics.get("full_text") or ""),
            parser_profile=parser_profile,
            charge_count=len(charges),
            status=status,
        )
        self.record_document_fingerprint(
            doc,
            signals=signals,
            text_metrics=text_metrics,
            parser_profile=parser_profile,
            ranked_candidates=ranked_candidates,
            status=status,
            charge_count=len(charges),
            selection_details=selection_details,
        )
        parse_attempt_id = self.record_parse_attempt(
            doc,
            parser_profile=parser_profile,
            ranked_candidates=ranked_candidates,
            signals=signals,
            text_metrics=text_metrics,
            charge_count=len(charges),
            status=status,
            selection_details=selection_details,
            document_intelligence=document_intelligence,
        )
        self.record_review_outcome(
            doc,
            parse_attempt_id=parse_attempt_id,
            parser_profile=parser_profile,
            ranked_candidates=ranked_candidates,
            signals=signals,
            charge_count=len(charges),
            status=status,
            selection_details=selection_details,
        )
        self.record_processing_run(
            doc,
            parser_profile=parser_profile,
            ranked_candidates=ranked_candidates,
            signals=signals,
            text_metrics=text_metrics,
            charge_count=len(charges),
            status=status,
            selection_details=selection_details,
            document_intelligence=document_intelligence,
        )

        # Insert into database
        num_inserted = self.insert_charges(version_id, doc['family_key'], charges)

        return doc['id'], doc['family_key'], num_inserted, status, parser_profile

    def run_extraction(
        self,
        max_workers: int = 4,
        limit: int | None = None,
        family_key: str | None = None,
        progress: bool = False,
        progress_interval_seconds: int = 30,
    ) -> dict:
        """Run bulk extraction across all documents.

        When `progress` is True, emits a periodic stderr line every
        `progress_interval_seconds` showing N/total processed and elapsed time.
        Useful for long runs where stdout is buffered or piped.
        """
        if family_key is None:
            documents = self.get_documents_needing_extraction()
            missing_version_count = self.count_documents_missing_versions()
        else:
            documents = self.get_documents_needing_extraction(family_key=family_key)
            missing_version_count = self.count_documents_missing_versions(family_key=family_key)
        if limit is not None:
            documents = documents[:limit]
        logger.info(f"Processing {len(documents)} historical documents")
        docs_by_id = {int(doc["id"]): doc for doc in documents}

        results = {
            'total_documents': len(documents),
            'documents_missing_versions': missing_version_count,
            'documents_processed': 0,
            'total_charges_inserted': 0,
            'by_family': {},
            'status_counts': {},
            'zero_charge_documents': [],
            'failed_documents': [],
        }
        status_counter: Counter[str] = Counter()

        # Progress reporting: emit a status line to stderr every progress_interval_seconds.
        # stderr is used so it doesn't interleave with stdout consumers (logs/json).
        import sys as _sys
        import time as _time
        progress_start = _time.time()
        progress_last_emit = progress_start
        progress_total = len(documents)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.process_document, doc): doc['id']
                for doc in documents
            }

            for future in as_completed(futures):
                source_doc = docs_by_id.get(int(futures[future]))
                try:
                    doc_id, family_key, num_inserted, status, parser_profile = future.result()
                    results['documents_processed'] += 1
                    results['total_charges_inserted'] += num_inserted
                    status_counter[status] += 1

                    if progress and progress_total > 0:
                        now = _time.time()
                        if now - progress_last_emit >= progress_interval_seconds:
                            done = results['documents_processed']
                            elapsed = int(now - progress_start)
                            inserted = results['total_charges_inserted']
                            rate = (done / elapsed) if elapsed > 0 else 0.0
                            remaining_secs = int((progress_total - done) / rate) if rate > 0 else -1
                            eta = f"~{remaining_secs}s" if remaining_secs >= 0 else "?"
                            print(
                                f"[extract-rates-nc] {done}/{progress_total} docs "
                                f"({inserted} charges, {elapsed}s elapsed, ETA {eta})",
                                file=_sys.stderr,
                                flush=True,
                            )
                            progress_last_emit = now

                    if family_key not in results['by_family']:
                        results['by_family'][family_key] = 0
                    results['by_family'][family_key] += num_inserted

                    if num_inserted == 0:
                        zero_row = {
                            "id": doc_id,
                            "family_key": family_key,
                            "status": status,
                            "parser_profile": parser_profile,
                        }
                        if source_doc:
                            zero_row["title"] = source_doc.get("title")
                            zero_row["effective_start"] = source_doc.get("effective_start")
                        results["zero_charge_documents"].append(zero_row)

                except Exception as e:
                    logger.error(f"Error processing document: {e}")
                    status_counter["error"] += 1
                    error_row = {"id": futures[future], "error": str(e)}
                    if source_doc:
                        error_row["family_key"] = source_doc.get("family_key")
                        error_row["title"] = source_doc.get("title")
                        error_row["effective_start"] = source_doc.get("effective_start")
                    results["failed_documents"].append(error_row)
                    continue

        results["status_counts"] = dict(sorted(status_counter.items()))
        results["zero_charge_documents"].sort(
            key=lambda row: (str(row.get("family_key") or ""), str(row.get("effective_start") or ""), int(row["id"]))
        )
        results["failed_documents"].sort(key=lambda row: int(row["id"]))
        return results



def bulk_extract_rates(
    db_path: str,
    limit: int | None = None,
    family_key: str | None = None,
    progress: bool = False,
    progress_interval_seconds: int = 30,
) -> dict:
    """Main entry point for bulk rate extraction.

    Pass progress=True for periodic stderr status during long runs.
    """
    extractor = BulkExtractor(db_path)
    return extractor.run_extraction(
        max_workers=4,
        limit=limit,
        family_key=family_key,
        progress=progress,
        progress_interval_seconds=progress_interval_seconds,
    )
