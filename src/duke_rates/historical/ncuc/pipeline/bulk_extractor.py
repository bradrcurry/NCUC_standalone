"""
Bulk rate charge extraction from historical NC documents.

Phase 2 of data preparation: Extract rates from confirmed tariff documents
using improved metadata (effective_start dates from Step 1).
"""

import hashlib
import os
import sqlite3
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import copy
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
    DOCLING_BACKEND_VERSION,
    HISTORICAL_BULK_PARSER_VERSION,
)
from duke_rates.db.reprocess import (
    record_historical_processing_run,
)
from duke_rates.db.artifact_cache import docling_artifact_is_degraded, load_page_artifacts
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


#: Environment switch for the destructive legacy persistence (Phase 2C).
LEGACY_REPLACE_ENV = "DUKE_RATES_LEGACY_REPLACE"


def legacy_replace_requested(value: bool | None = None) -> bool:
    """Explicit argument first, else ``DUKE_RATES_LEGACY_REPLACE`` (1/true/yes)."""
    if value is not None:
        return bool(value)
    return os.environ.get(LEGACY_REPLACE_ENV, "").strip().lower() in ("1", "true", "yes")


@dataclass
class DocumentExtraction:
    """Output of :meth:`BulkExtractor.extract_document`. Nothing is persisted yet."""

    doc: dict
    version_id: int | None
    structured_mode: str
    charges: List[ExtractedCharge]
    parser_profile: str | None
    ranked_candidates: list
    status: str
    signals: Any
    text_metrics: dict
    selection_details: dict
    recorders_disabled: bool = False


@dataclass
class ProcessResult:
    """What :meth:`BulkExtractor.process_document_result` did with one document."""

    doc_id: int
    family_key: str | None
    status: str
    parser_profile: str | None
    structured_mode: str | None = None
    extracted: int = 0
    run_id: int | None = None
    validation_status: str | None = None
    needs_review: bool = False
    persistence: str = "candidate"
    #: Rows written to tariff_charges (only with an explicit legacy replace).
    inserted: int = 0
    promotion: dict | None = None
    error: str | None = None
    issues: list = field(default_factory=list)

    def as_tuple(self) -> Tuple[int, str, int, str, str | None]:
        """The pre-Phase 2C return shape: (doc_id, family_key, inserted, status, profile)."""
        return self.doc_id, self.family_key, self.inserted, self.status, self.parser_profile


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

    #: Structured-input modes (Phase 2B). "legacy" keeps the text-only pipeline.
    STRUCTURED_MODES = ("legacy", "docling", "native", "hybrid")

    def __init__(self, db_path: str, structured_mode: str | None = None):
        self.db_path = db_path
        explicit = structured_mode or os.environ.get("DUKE_RATES_STRUCTURED_MODE")
        mode = (explicit or "legacy").lower()
        if mode not in self.STRUCTURED_MODES:
            raise ValueError(f"structured_mode must be one of {self.STRUCTURED_MODES}, not {mode!r}")
        # Direct extract_charges_from_document callers keep "legacy" unless a
        # mode is chosen; candidate persistence uses hybrid by default
        # (see candidate_mode).
        self.structured_mode = mode
        self._explicit_mode = mode if explicit else None
        # Optional override: callable(doc, mode) -> DocumentRepresentation | None
        # (the benchmark uses it to read its own Docling cache).
        self.representation_provider = None
        # Phase 2C: when a docling/hybrid document has no cached Docling
        # artifact, convert only its page span, chunk by chunk
        # (document_intelligence.bounded). Off by default: extraction normally
        # reads cached representations only. Env DUKE_RATES_BOUNDED_CONVERT=1.
        self.bounded_conversion = os.environ.get("DUKE_RATES_BOUNDED_CONVERT", "") in ("1", "true")
        self.bounded_chunk_pages = 8
        self.bounded_converter = None  # callable(pdf_path) -> docling result; default backend
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
        """Return the best usable Docling artifact row for ``pdf_path``, or None.

        Only artifacts whose ``file_hash`` matches the PDF currently on disk
        are eligible: a newer revision downloaded to the same path must not be
        read through a superseded revision's conversion.  Among those, skip
        conversions that lost content (see ``docling_artifact_is_degraded``)
        and prefer the current ``DOCLING_BACKEND_VERSION``, then the newest.

        ``want_doc_json=True`` selects the (large) ``doc_json_content`` column,
        used by page-range slicing. The default omits it to keep memory low
        for the full-document text path.
        """
        file_hash = self._current_file_hash(pdf_path)
        if not file_hash:
            return None
        cols = "id, file_hash, plain_text_content, status, page_count, metadata_json"
        if want_doc_json:
            cols += ", doc_json_content"
        conn = self._get_connection()
        try:
            rows = conn.execute(
                f"""
                SELECT {cols}
                FROM docling_artifacts
                WHERE source_pdf = ?
                  AND plain_text_content IS NOT NULL
                ORDER BY (file_hash = ?) DESC, (backend_version = ?) DESC, id DESC
                """,
                (pdf_path, file_hash, DOCLING_BACKEND_VERSION),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()
        for row in rows:
            if not self._docling_status_is_usable(row["status"]):
                continue
            if docling_artifact_is_degraded(row["metadata_json"]):
                continue
            if row["file_hash"] == file_hash:
                return dict(row)
            # Bytes changed since conversion.  Re-downloads often differ only
            # in byte-level details, so accept the artifact only when its
            # content is verified against the PDF now on disk.
            if self._docling_text_matches_pdf(pdf_path, row):
                logger.info(
                    "Docling artifact %s for %s has a different file_hash but "
                    "matching content; using it", row["id"], pdf_path,
                )
                return dict(row)
            logger.warning(
                "Docling artifact %s for %s was converted from different bytes "
                "and its content does not match the current PDF; ignoring it",
                row["id"], pdf_path,
            )
        return None

    #: Thresholds for accepting a hash-mismatched artifact (see
    #: ``_docling_text_matches_pdf``).  Docling/OCR reads differ slightly from
    #: the PDF text layer, so large docs allow a small numeric residue; small
    #: docs must match every number because one changed rate is a new revision.
    _DOCLING_MATCH_NUMBER_SHARE = 0.95
    _DOCLING_MATCH_NUMBER_EXACT_BELOW = 5
    _DOCLING_MATCH_WORD_SHARE = 0.90
    _DOCLING_MATCH_MIN_WORDS = 20

    def _docling_text_matches_pdf(self, pdf_path: str, row: sqlite3.Row) -> bool:
        """Verify a hash-mismatched artifact describes the PDF currently on disk.

        Requires the same page count, the artifact's words to appear in the
        PDF's text layer, and its decimal tokens (where rates live) to appear
        there too: all of them when there are few, nearly all otherwise.
        Returns False when this cannot be verified (no text layer, unreadable
        PDF), so an unverifiable artifact is never attributed to a newer document.
        """
        try:
            import fitz
            with fitz.open(pdf_path) as pdf:
                if pdf.page_count != (row["page_count"] or 0):
                    return False
                pdf_text = "\n".join(page.get_text() for page in pdf)
        except Exception:
            return False
        artifact_text = row["plain_text_content"] or ""
        artifact_words = set(re.findall(r"[A-Za-z]{4,}", artifact_text.lower()))
        if len(artifact_words) < self._DOCLING_MATCH_MIN_WORDS:
            return False
        pdf_words = set(re.findall(r"[A-Za-z]{4,}", pdf_text.lower()))
        if len(artifact_words & pdf_words) / len(artifact_words) < self._DOCLING_MATCH_WORD_SHARE:
            return False
        artifact_numbers = set(re.findall(r"\d+\.\d+", artifact_text))
        if not artifact_numbers:
            return True
        found = len(artifact_numbers & set(re.findall(r"\d+\.\d+", pdf_text)))
        if len(artifact_numbers) < self._DOCLING_MATCH_NUMBER_EXACT_BELOW:
            return found == len(artifact_numbers)
        return found / len(artifact_numbers) >= self._DOCLING_MATCH_NUMBER_SHARE

    def _current_file_hash(self, pdf_path: str) -> str | None:
        """SHA-256 of ``pdf_path`` as it is on disk now (memoized by size/mtime)."""
        try:
            stat = Path(pdf_path).stat()
        except OSError:
            return None
        cache = self.__dict__.setdefault("_file_hash_cache", {})
        key = (pdf_path, stat.st_size, stat.st_mtime_ns)
        if key not in cache:
            hasher = hashlib.sha256()
            try:
                with open(pdf_path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        hasher.update(chunk)
            except OSError:
                return None
            cache[key] = hasher.hexdigest()
        return cache[key]

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

    def _record_document_type_v2_classification(self, doc: dict, text: str) -> None:
        """Run ``rule_document_type_v2`` on ``doc`` and persist the result.

        Sister of ``_record_document_type_classification`` — same persistence
        path, different classifier. Looks up layout signals from
        ``document_fingerprints_v2`` when available so v2's strong_header
        + has_tables + page_count features all fire. Side-effect only —
        never raises.
        """
        doc_id = doc.get('id')
        if not doc_id:
            return
        try:
            from duke_rates.classification.rule_document_type_v2 import (
                DocumentSignals, classify_v2,
            )
        except Exception:
            return

        first_text = text[:2000]
        last_text = text[-1000:] if len(text) > 2000 else ""

        # Best-effort layout signal lookup. Failures fall through to None
        # so v2 still produces a result — just without layout discriminators.
        page_count: int | None = None
        text_chars: int | None = None
        has_tables: int | None = None
        try:
            cls_conn = self._get_connection()
            try:
                fp = cls_conn.execute(
                    "SELECT page_count, text_chars, has_tables "
                    "FROM document_fingerprints_v2 WHERE source_pdf = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (doc.get('local_path') or '',),
                ).fetchone()
                if fp:
                    page_count = fp[0]
                    text_chars = fp[1]
                    has_tables = fp[2]
            finally:
                cls_conn.close()
        except Exception:
            pass

        signals = DocumentSignals(
            title=doc.get('title') or '',
            first_text=first_text,
            last_text=last_text,
            page_count=page_count,
            text_chars=text_chars if text_chars is not None else len(text),
            has_tables=has_tables,
        )
        try:
            result = classify_v2(signals)
        except Exception as exc:
            logger.debug("v2 classify_v2 failed for doc %s: %s", doc_id, exc)
            return

        self._record_document_type_classification(doc, result)

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
        # v2 rule classifier runs alongside v1 so live ingest populates
        # both rule_document_type_v1 and rule_document_type_v2 rows. The
        # routing tier still uses v1's legacy `doc_type` short-circuit
        # below; v2 contributes via the multi-classifier agreement signal
        # used by gold-set seeding and triage. See
        # docs/research/document_identification.md Stream B.
        self._record_document_type_v2_classification(doc, text)
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
        profile_doc = self._with_structure(doc)
        charges = self._safe_profile_extract(profile, profile_doc, text, parse_warnings)
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
            candidate_charges = self._safe_profile_extract(
                fallback_profile, profile_doc, text, parse_warnings
            )
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
        if self.structured_mode != "legacy":
            charges = self._attach_located_evidence(charges, profile_doc)
            integrity_details = self._structured_integrity_details(charges, profile_doc)
            rep = profile_doc.get("_representation")
            selection_details["structured_input"] = {
                "mode": self.structured_mode,
                "backend": rep.normalizer_backend.value if rep is not None else None,
                "backend_version": rep.backend_version if rep is not None else None,
                "charges_with_evidence": sum(1 for c in charges if c.evidence),
            } | integrity_details
            bounded = (rep.document_metadata or {}).get("bounded") if rep is not None else None
            if bounded:
                # Partial structured input is recorded as such, never as complete.
                selection_details["bounded_extraction"] = bounded
                selection_details["structured_input"]["degraded"] = bounded["degraded"]
                if bounded["degraded"]:
                    selection_details["structured_input"]["degraded_reason"] = "; ".join(
                        f"pages {c['pages'][0]}-{c['pages'][1]}: {c['reason']} (fallback {c.get('fallback')})"
                        for c in bounded["degraded_chunks"]
                    )
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

    # ------------------------------------------------------------------
    # Structured document input (Phase 2B)
    # ------------------------------------------------------------------

    def _with_structure(self, doc: dict) -> dict:
        """A copy of ``doc`` that lets profiles load its structured representation."""
        if self.structured_mode == "legacy":
            return doc
        cache: dict = {}
        diagnostics: dict = {}

        def loader():
            if "rep" not in cache:
                cache["rep"] = self._structured_representation(doc, diagnostics)
            return cache["rep"]

        return {
            **doc,
            "_structured_mode": self.structured_mode,
            "_representation_loader": loader,
            "_structured_diagnostics": diagnostics,
        }

    def _structured_representation(self, doc: dict, diagnostics: dict | None = None):
        """DocumentRepresentation for ``doc`` under the current mode, or None.

        docling: the cached Docling artifact for the bytes on disk (never a new
        conversion). native: pdfplumber text layer with geometry tables.
        hybrid: Docling when cached and it covers the span with original page
        numbers, otherwise native.

        A Docling artifact whose page numbers cannot be verified against the
        PDF is never used (see ``page_integrity``). ``diagnostics``, when
        given, receives the integrity verdict and the reason for any fallback.
        """
        if diagnostics is None:
            diagnostics = {}
        if self.representation_provider is not None:
            return self.representation_provider(doc, self.structured_mode)
        pdf_path = doc.get("local_path") or ""
        if not pdf_path or not Path(pdf_path).exists():
            return None
        start, end = doc.get("start_page"), doc.get("end_page")
        page_range = (int(start), int(end or start)) if start else None
        if self.structured_mode in ("docling", "hybrid"):
            rep, integrity = self._docling_selection(pdf_path, page_range)
            diagnostics["docling_page_integrity"] = integrity
            reason = None
            if rep is None:
                reason = integrity.get("reason") or "no_artifact"
            elif integrity.get("status") == "partial":
                reason = "span_partially_covered"
            elif not rep.pages:
                reason = "empty_slice"
            if reason:
                diagnostics["docling_fallback_reason"] = reason
                if self.structured_mode == "hybrid":
                    # Partial or empty Docling structure must not stand in for
                    # the complete native read of the span.
                    rep = None
                elif reason == "span_partially_covered":
                    diagnostics["degraded"] = True
                    diagnostics["degraded_reason"] = (
                        "docling artifact lacks pages "
                        + ", ".join(f"{a}-{b}" for a, b in integrity.get("missing_pages") or [])
                    )
            if rep is None and self.bounded_conversion:
                rep = self._bounded_representation(doc, pdf_path, page_range)
            if rep is not None or self.structured_mode == "docling":
                return rep
        from duke_rates.document_intelligence.adapters import representation_from_native_pdf

        try:
            return representation_from_native_pdf(pdf_path, page_range=page_range)
        except Exception as exc:  # unreadable PDF: structured input is optional
            logger.warning("native representation failed for %s: %s", pdf_path, exc)
            return None

    def _bounded_representation(self, doc: dict, pdf_path: str, page_range):
        """Convert only the document's span with Docling, a few pages at a time."""
        from duke_rates.document_intelligence.bounded import bounded_representation

        try:
            result = bounded_representation(
                pdf_path,
                page_range,
                chunk_pages=self.bounded_chunk_pages,
                converter=self.bounded_converter,
                native_fallback=True,
                file_sha256=doc.get("content_hash"),
            )
        except Exception as exc:  # structured input is optional; text parse still runs
            logger.warning("bounded conversion failed for %s: %s", pdf_path, exc)
            return None
        return result.representation

    def _docling_representation(self, pdf_path: str, page_range):
        return self._docling_selection(pdf_path, page_range)[0]

    def _docling_selection(self, pdf_path: str, page_range):
        """(representation or None, page-integrity verdict) from the cached Docling artifact.

        The representation is built only when the artifact's page numbers are
        verified original-PDF numbers; it is None for chunk-relative or
        otherwise unverifiable artifacts and for spans the artifact lacks.
        """
        from duke_rates.document_intelligence.adapters import representation_from_docling
        from duke_rates.document_intelligence.page_integrity import (
            PARTIAL,
            assess_docling_page_integrity,
        )

        row = self._load_docling_artifact(pdf_path, want_doc_json=True)
        if not row or not row.get("doc_json_content"):
            return None, {"status": "missing", "reason": "no_artifact"}
        conn = self._get_connection()
        try:
            version = conn.execute(
                "SELECT backend_version FROM docling_artifacts WHERE id = ?", (row["id"],)
            ).fetchone()
        finally:
            conn.close()
        try:
            document = json.loads(row["doc_json_content"])
        except (TypeError, ValueError):
            document = None
        integrity = assess_docling_page_integrity(
            document,
            pdf_page_count=self._pdf_page_count(pdf_path),
            requested=page_range,
            metadata=row.get("metadata_json"),
            artifact_page_count=row.get("page_count"),
        )
        verdict = integrity.as_dict() | {
            "artifact_id": row["id"],
            "file_hash_match": row.get("file_hash") == self._current_file_hash(pdf_path),
        }
        if not (integrity.usable or integrity.status == PARTIAL):
            logger.info(
                "Docling artifact %s for %s not used for pages %s: %s",
                row["id"], pdf_path, page_range, integrity.reason,
            )
            return None, verdict
        rep = representation_from_docling(
            document,
            source_pdf=pdf_path,
            file_sha256=row.get("file_hash"),
            backend_version=version[0] if version else None,
            page_range=page_range,
            page_offset=integrity.page_offset,
        )
        rep.document_metadata["page_integrity"] = verdict
        return rep, verdict

    def _pdf_page_count(self, pdf_path: str) -> int | None:
        """Page count of ``pdf_path`` as it is on disk now (memoized by size/mtime)."""
        try:
            stat = Path(pdf_path).stat()
        except OSError:
            return None
        cache = self.__dict__.setdefault("_pdf_page_count_cache", {})
        key = (pdf_path, stat.st_size, stat.st_mtime_ns)
        if key not in cache:
            try:
                import fitz
                with fitz.open(pdf_path) as pdf:
                    cache[key] = pdf.page_count
            except Exception:
                cache[key] = None
        return cache[key]

    def _structured_integrity_details(self, charges: List[ExtractedCharge], profile_doc: dict) -> dict:
        """Page-integrity diagnostics for ``structured_input``; drops unverifiable evidence.

        Records the Docling artifact verdict and fallback reason gathered while
        the representation was chosen, and removes evidence citing pages the
        representation in use does not hold.
        """
        loader = profile_doc.get("_representation_loader")
        rep = profile_doc.get("_representation")
        if rep is None and callable(loader) and any(c.evidence for c in charges):
            rep = loader()  # cached; evidence was derived from it
        details: dict[str, Any] = {}
        rejected = self._reject_unverified_evidence(charges, rep)
        diagnostics = profile_doc.get("_structured_diagnostics") or {}
        integrity = diagnostics.get("docling_page_integrity")
        if integrity:
            details["docling_page_integrity"] = {
                k: integrity[k]
                for k in (
                    "status", "reason", "numbering", "chunked", "requested",
                    "pdf_page_count", "missing_pages", "artifact_id", "file_hash_match",
                )
                if k in integrity
            }
            if rep is not None and rep.normalizer_backend.value == "docling":
                details["docling_page_integrity"]["tables"] = sum(len(p.tables) for p in rep.pages)
        for key in ("docling_fallback_reason", "degraded", "degraded_reason"):
            if key in diagnostics:
                details[key] = diagnostics[key]
        if rejected:
            details["rejected_evidence"] = rejected
        return details

    @staticmethod
    def _evidence_pages(evidence: Any) -> set[int]:
        """Every page number an evidence record cites, at any nesting level."""
        pages: set[int] = set()
        stack = [evidence]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                page = item.get("page")
                if isinstance(page, int) and not isinstance(page, bool):
                    pages.add(page)
                stack.extend(item.values())
            elif isinstance(item, (list, tuple)):
                stack.extend(item)
        return pages

    @classmethod
    def _reject_unverified_evidence(cls, charges: List[ExtractedCharge], rep) -> list[dict]:
        """Drop evidence that cites a page the verified representation does not hold.

        Evidence is only ever derived from ``rep``, so for a verified
        representation this removes nothing. It guards against page references
        reaching a candidate from any other source.
        """
        allowed = {p.page_number for p in rep.pages} if rep is not None else set()
        rejected = []
        for charge in charges:
            if not charge.evidence:
                continue
            pages = cls._evidence_pages(charge.evidence)
            stray = sorted(pages - allowed)
            if stray:
                rejected.append(
                    {"charge_label": charge.charge_label, "rate_value": charge.rate_value, "pages": stray}
                )
                charge.evidence = None
        return rejected

    @staticmethod
    def _attach_located_evidence(charges: List[ExtractedCharge], profile_doc: dict) -> List[ExtractedCharge]:
        """Give charges without evidence the location of their printed value, when unambiguous."""
        missing = [c for c in charges if c.evidence is None]
        if not missing:
            return charges
        loader = profile_doc.get("_representation_loader")
        rep = profile_doc.get("_representation") or (loader() if callable(loader) else None)
        if rep is None:
            return charges
        from duke_rates.document_intelligence.evidence import locate_value_evidence

        for charge in missing:
            charge.evidence = locate_value_evidence(
                rep, value=charge.rate_value, unit=charge.rate_unit, label=charge.charge_label
            )
        return charges

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
                      charges: List[ExtractedCharge],
                      force_clear: bool = False) -> int:
        """Insert extracted charges into tariff_charges table (low-level legacy writer).

        Phase 2C: no pipeline path calls this; ``process_document`` stores
        candidate runs and replaces charges only through ``provenance.promote``.
        It is kept for callers that manage their own scratch tables.

        Clears existing charges for this (version_id, family_key) before inserting
        so that reprocessing replaces rather than appends.

        When ``force_clear`` is True and ``version_id`` is set, the existing-
        charges DELETE runs even when ``charges`` is empty. This is how
        callers ask the extractor to *clear stale charges* on a re-extraction
        that produced 0 results — e.g. when a cross-attribution-guard now
        refuses the prior polluting fallback. Without this flag the old
        rows would silently survive.
        """
        if not version_id:
            return 0
        if not charges and not force_clear:
            return 0

        conn = self._get_connection()
        try:
            inserted = 0
            now = datetime.now(UTC).isoformat()

            # Delete existing charges first so reprocessing replaces, not appends.
            # Only rows this extractor wrote are replaced: manual corrections,
            # sibling copies and LLM promotions carry notes (and promotions an
            # audit row) and must survive re-extraction.
            has_audit = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_promoted_charge_audit'"
            ).fetchone()
            has_notes = any(
                row[1] == "notes" for row in conn.execute("PRAGMA table_info(tariff_charges)")
            )
            audit_clause = (
                " AND id NOT IN (SELECT tariff_charge_id FROM llm_promoted_charge_audit"
                " WHERE tariff_charge_id IS NOT NULL)"
                if has_audit else ""
            )
            replaceable = ("COALESCE(notes, '') = ''" if has_notes else "1 = 1") + audit_clause
            preserved = conn.execute(
                "SELECT COUNT(*) FROM tariff_charges WHERE version_id = ? AND family_key = ?"
                " AND NOT (" + replaceable + ")",
                (version_id, family_key),
            ).fetchone()[0]
            if preserved:
                logger.info(
                    "insert_charges: keeping %d curated charge(s) for version %s (%s)",
                    preserved, version_id, family_key,
                )
            conn.execute(
                "DELETE FROM tariff_charges WHERE version_id = ? AND family_key = ?"
                " AND " + replaceable,
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

    # ------------------------------------------------------------------
    # Extraction and persistence (Phase 2C: separate steps)
    # ------------------------------------------------------------------

    def candidate_mode(self, *, legacy_replace: bool = False) -> str:
        """Structured mode for persisted extraction.

        An explicitly chosen mode always wins. Otherwise candidates use
        ``hybrid``; a legacy replace reproduces ``legacy`` so the active
        billing interpretation does not silently change.
        """
        if self._explicit_mode:
            return self._explicit_mode
        return "legacy" if legacy_replace else "hybrid"

    def _in_mode(self, mode: str) -> "BulkExtractor":
        if mode == self.structured_mode:
            return self
        clone = copy.copy(self)  # shares classifiers and registries; safe per call
        clone.structured_mode = mode
        return clone

    def extract_document(
        self, doc: dict, *, structured_mode: str | None = None, version_id: int | None = None
    ) -> DocumentExtraction:
        """Run extraction only. ``tariff_charges`` and the run tables are not written.

        Classification recorders inside ``extract_charges_from_document``
        still run unless disabled (``candidate_runs.disable_recorders``).
        """
        mode = structured_mode or self.structured_mode
        version_id = (
            version_id or doc.get("version_id") or self.get_tariff_version_for_document(doc["id"])
        )
        (charges, profile, ranked, status, signals, text_metrics, selection) = self._in_mode(
            mode
        ).extract_charges_from_document(doc)
        return DocumentExtraction(
            doc=doc,
            version_id=version_id,
            structured_mode=mode,
            charges=charges,
            parser_profile=profile,
            ranked_candidates=ranked,
            status=status,
            signals=signals,
            text_metrics=text_metrics or {},
            selection_details=selection or {},
        )

    def persist_candidate(
        self, extraction: DocumentExtraction, *, created_by: str, persistence: str = "candidate"
    ) -> dict:
        """Store an extraction as an immutable, sealed candidate run (never promoted here)."""
        from duke_rates.historical.ncuc.pipeline.candidate_runs import record_candidate_run

        return record_candidate_run(
            self.db_path, extraction, created_by=created_by, persistence=persistence
        )

    def require_provenance_schema(self) -> None:
        from duke_rates.db.provenance import ProvenanceError
        from duke_rates.db.provenance_schema import provenance_schema_installed

        conn = self._get_connection()
        try:
            installed = provenance_schema_installed(conn)
        finally:
            conn.close()
        if not installed:
            raise ProvenanceError(
                f"{self.db_path} has no provenance schema. Bulk extraction stores candidate "
                "runs; install it first with `duke-rates provenance migrate --db <path>`."
            )

    def _record_bookkeeping(self, extraction: DocumentExtraction) -> None:
        """Fingerprint, parse attempt, review outcome and processing run (append-only logs)."""
        doc = extraction.doc
        charge_count = len(extraction.charges)
        common = dict(
            parser_profile=extraction.parser_profile,
            ranked_candidates=extraction.ranked_candidates,
            signals=extraction.signals,
            charge_count=charge_count,
            status=extraction.status,
            selection_details=extraction.selection_details,
        )
        document_intelligence = self._analyze_document_intelligence(
            doc,
            raw_text=str(extraction.text_metrics.get("full_text") or ""),
            parser_profile=extraction.parser_profile,
            charge_count=charge_count,
            status=extraction.status,
        )
        self.record_document_fingerprint(doc, text_metrics=extraction.text_metrics, **common)
        parse_attempt_id = self.record_parse_attempt(
            doc,
            text_metrics=extraction.text_metrics,
            document_intelligence=document_intelligence,
            **common,
        )
        self.record_review_outcome(doc, parse_attempt_id=parse_attempt_id, **common)
        self.record_processing_run(
            doc,
            text_metrics=extraction.text_metrics,
            document_intelligence=document_intelligence,
            **common,
        )

    def process_document_result(
        self,
        doc: dict,
        force_clear: bool = False,
        *,
        legacy_replace: bool | None = None,
        created_by: str = "bulk_extractor",
    ) -> ProcessResult:
        """Extract one document and store it as a candidate run.

        Default: an immutable, sealed candidate; ``tariff_charges`` is not
        touched, so reprocessing never deletes accepted charges. With
        ``legacy_replace`` (or ``DUKE_RATES_LEGACY_REPLACE=1``) the candidate
        is then promoted through ``provenance.promote``: the prior rows are
        logged and ``provenance rollback`` restores them, curated rows
        (notes, LLM promotions, overrides) are kept, and a run that failed
        validation is refused. ``force_clear`` allows promoting an empty run
        (clearing stale charges), as it did for the in-place replace.
        """
        replace = legacy_replace_requested(legacy_replace)
        version_id = doc.get("version_id") or self.get_tariff_version_for_document(doc["id"])
        if not version_id:
            logger.warning(f"No tariff_version found for doc {doc['id']}")
            return ProcessResult(doc["id"], doc.get("family_key"), "missing_version", None)
        self.require_provenance_schema()
        persistence = "legacy_replace" if replace else "candidate"
        extraction = self.extract_document(
            doc, structured_mode=self.candidate_mode(legacy_replace=replace), version_id=version_id
        )
        self._record_bookkeeping(extraction)
        run = self.persist_candidate(extraction, created_by=created_by, persistence=persistence)
        result = ProcessResult(
            doc_id=doc["id"],
            family_key=doc.get("family_key"),
            status=extraction.status,
            parser_profile=extraction.parser_profile,
            structured_mode=extraction.structured_mode,
            extracted=len(extraction.charges),
            run_id=run["run_id"],
            validation_status=run["validation_status"],
            needs_review=run["needs_review"],
            persistence=persistence,
            issues=run["run_issues"],
        )
        if replace:
            self._legacy_promote(result, force_clear=force_clear, created_by=created_by)
        return result

    def _legacy_promote(self, result: ProcessResult, *, force_clear: bool, created_by: str) -> None:
        from duke_rates.db.provenance import ProvenanceError, promote

        if result.extracted == 0 and not force_clear:
            return  # the in-place replace also left rows alone on an empty extraction
        logger.warning(
            "LEGACY REPLACE: promoting run %s over tariff_charges of doc %s (%s) without "
            "review%s. Undo with `duke-rates provenance rollback`.",
            result.run_id,
            result.doc_id,
            result.family_key,
            " (review flags overridden)" if result.needs_review else "",
        )
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        try:
            result.promotion = promote(
                conn,
                result.run_id,
                promoted_by=created_by,
                reason=f"legacy_replace (bulk_extractor, doc {result.doc_id})",
                allow_empty=force_clear,
                acknowledge_review=True,
                pathway="automated:bulk_extractor.legacy_replace",
            )
            result.inserted = int(result.promotion.get("inserted", 0))
        except ProvenanceError as exc:
            result.error = f"legacy replace refused: {exc}"
            logger.error("doc %s: %s", result.doc_id, result.error)
        finally:
            conn.close()

    def process_document(
        self,
        doc: dict,
        force_clear: bool = False,
        *,
        legacy_replace: bool | None = None,
        created_by: str = "bulk_extractor",
    ) -> Tuple[int, str, int, str, str | None]:
        """Compatibility wrapper: (doc_id, family_key, num_inserted, status, parser_profile).

        Phase 2C: stores a candidate run and leaves ``tariff_charges`` alone,
        so ``num_inserted`` is 0 unless ``legacy_replace`` is requested. Use
        :meth:`process_document_result` for the run id and validation.
        """
        return self.process_document_result(
            doc, force_clear, legacy_replace=legacy_replace, created_by=created_by
        ).as_tuple()

    def run_extraction(
        self,
        max_workers: int = 4,
        limit: int | None = None,
        family_key: str | None = None,
        progress: bool = False,
        progress_interval_seconds: int = 30,
        *,
        legacy_replace: bool | None = None,
        created_by: str = "bulk_extractor",
    ) -> dict:
        """Run bulk extraction across all documents.

        Phase 2C: each document becomes a sealed candidate run
        (``candidate_runs``); ``tariff_charges`` changes only with
        ``legacy_replace``. ``total_charges_inserted`` counts rows written to
        ``tariff_charges`` (0 for candidate runs); ``total_charges_extracted``
        counts candidate charges.

        When `progress` is True, emits a periodic stderr line every
        `progress_interval_seconds` showing N/total processed and elapsed time.
        Useful for long runs where stdout is buffered or piped.
        """
        self.require_provenance_schema()
        replace = legacy_replace_requested(legacy_replace)
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
            'total_charges_extracted': 0,
            'persistence': 'legacy_replace' if replace else 'candidate',
            'structured_mode': self.candidate_mode(legacy_replace=replace),
            'candidate_runs': [],
            'runs_needing_review': 0,
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
                executor.submit(
                    self.process_document_result,
                    doc,
                    legacy_replace=replace,
                    created_by=created_by,
                ): doc['id']
                for doc in documents
            }

            for future in as_completed(futures):
                source_doc = docs_by_id.get(int(futures[future]))
                try:
                    outcome = future.result()
                    doc_id, family_key, num_inserted, status, parser_profile = outcome.as_tuple()
                    results['documents_processed'] += 1
                    results['total_charges_inserted'] += num_inserted
                    results['total_charges_extracted'] += outcome.extracted
                    results['runs_needing_review'] += int(outcome.needs_review)
                    if outcome.run_id is not None:
                        results['candidate_runs'].append({
                            "doc_id": doc_id,
                            "run_id": outcome.run_id,
                            "extracted": outcome.extracted,
                            "validation_status": outcome.validation_status,
                            "needs_review": outcome.needs_review,
                            **({"error": outcome.error} if outcome.error else {}),
                        })
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
                    results['by_family'][family_key] += outcome.extracted

                    if outcome.extracted == 0:
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
        results["candidate_runs"].sort(key=lambda row: int(row["doc_id"]))
        return results



def bulk_extract_rates(
    db_path: str,
    limit: int | None = None,
    family_key: str | None = None,
    progress: bool = False,
    progress_interval_seconds: int = 30,
    *,
    structured_mode: str | None = None,
    legacy_replace: bool | None = None,
    created_by: str = "bulk_extractor",
) -> dict:
    """Main entry point for bulk rate extraction (candidate runs by default).

    Pass progress=True for periodic stderr status during long runs.
    """
    extractor = BulkExtractor(db_path, structured_mode=structured_mode)
    return extractor.run_extraction(
        max_workers=4,
        limit=limit,
        family_key=family_key,
        progress=progress,
        progress_interval_seconds=progress_interval_seconds,
        legacy_replace=legacy_replace,
        created_by=created_by,
    )
