from __future__ import annotations

import json
import sqlite3
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

from duke_rates.db.sqlite import connect
from duke_rates.historical.family_anchor_audit import (
    detect_current_family_anchor_mismatch,
    extract_leaf_number,
)
from duke_rates.historical.ncuc.pipeline.page_miner import mine_document_pages
from duke_rates.models.bill import BillStatementData, StoredBillStatement
from duke_rates.models.bill_observation import BillComponentObservation
from duke_rates.models.docket_lead import RegulatoryDocketLeadRecord
from duke_rates.models.document import DiscoveryRecord, StoredDocument
from duke_rates.models.evidence_anchor import EvidenceAnchorRecord
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.historical_lead import HistoricalLeadRecord
from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFetchStatus
from duke_rates.models.parse_result import DocumentParseResult
from duke_rates.models.search_pack import HistoricalSearchPackRecord
from duke_rates.models.url_variant import CandidateUrlVariantRecord


_FAMILY_CANDIDATE_STOPWORDS = {
    "NC",
    "SC",
    "THE",
    "AND",
    "OF",
    "FOR",
    "PROGRAM",
    "RIDER",
    "SCHEDULE",
    "SERVICE",
}

_FAMILY_CURRENT_DOC_CATEGORY_HINTS = {
    "rate_schedule": {"rate", "tariff"},
    "rider": {"rider", "tariff"},
    "program": {"program"},
    "service": {"tariff"},
    "doc": {"tariff", "rate", "rider", "program"},
}

_PLACEHOLDER_HEADING_FAMILY_KEYS = {
    "nc-carolinas-doc-TYPEOFSERVICE",
    "nc-carolinas-doc-EFFECTIVEFORSERVICE",
}

_GENERIC_PROVISIONAL_TITLE_PATTERNS = {
    "TYPE OF SERVICE",
    "BILLS UNDER THIS SCHEDULE",
    "GENERAL SERVICE",
    "INDUSTRIAL SERVICE",
    "RESIDENTIAL SERVICE",
    "CHARACTER OF SERVICE",
    "EFFECTIVE FOR SERVICE",
}

_GENERIC_PROVISIONAL_TITLE_PREFIXES = (
    "EFFECTIVE ",
    "AVAILABLE ",
    "APPLICABLE ",
    "FOR SERVICE ",
)

_GENERIC_PROVISIONAL_KEY_FRAGMENTS = (
    "TYPEOFSERVICE",
    "EFFECTIVEFORSERVICE",
    "BILLSUNDERTHISSCHEDULE",
    "CHARACTEROFSERVICE",
)

_PROVISIONAL_SPAN_SUFFIX_RE = re.compile(r"\s*\(SPAN\s+\d+\s*-\s*\d+\)\s*$", re.IGNORECASE)
_PROVISIONAL_CODE_RE = re.compile(r"\b(?:SCHEDULE|RIDER)\s+([A-Z][A-Z0-9-]{1,31})\b", re.IGNORECASE)


def _normalized_tokens(text: str | None) -> set[str]:
    return {
        token
        for token in re.findall(r"[A-Z0-9]+", (text or "").upper())
        if len(token) >= 3 and token not in _FAMILY_CANDIDATE_STOPWORDS
    }


def _normalized_phrase(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", (text or "").upper())).strip()


def _strip_provisional_span_suffix(text: str | None) -> str:
    candidate = str(text or "").strip()
    if not candidate:
        return ""
    return _PROVISIONAL_SPAN_SUFFIX_RE.sub("", candidate).strip()


def _looks_generic_provisional_title(text: str | None) -> bool:
    normalized = _normalized_phrase(_strip_provisional_span_suffix(text))
    if not normalized:
        return True
    if normalized in _GENERIC_PROVISIONAL_TITLE_PATTERNS:
        return True
    return any(normalized.startswith(prefix) for prefix in _GENERIC_PROVISIONAL_TITLE_PREFIXES)


def _looks_fragmentary_provisional_title(text: str | None) -> bool:
    normalized = _normalized_phrase(text)
    return "( SPAN " in normalized or " SPAN " in normalized


def _infer_family_type_from_title(text: str | None, fallback: str | None = None) -> str | None:
    normalized = _normalized_phrase(text)
    if "RIDER" in normalized:
        return "rider"
    if "PROGRAM" in normalized:
        return "program"
    if "SCHEDULE" in normalized or "SERVICE" in normalized or "RATE" in normalized:
        return "rate_schedule"
    return fallback


def _extract_schedule_code_candidate(text: str | None) -> str | None:
    candidate = _strip_provisional_span_suffix(text)
    if not candidate:
        return None
    match = _PROVISIONAL_CODE_RE.search(candidate)
    if not match:
        return None
    return match.group(1).upper()


def _coerce_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return int(text)
    return None


def _token_overlap_size(left: str | None, right: str | None) -> int:
    return len(_normalized_tokens(left).intersection(_normalized_tokens(right)))


def _score_token_overlap(
    reasons: list[str],
    *,
    token_label: str,
    family_tokens: set[str],
    candidate_tokens: set[str],
    max_points: int,
    multiplier: int = 2,
) -> int:
    shared_tokens = sorted(family_tokens.intersection(candidate_tokens))
    if not shared_tokens:
        return 0
    reasons.append(f"{token_label}:{','.join(shared_tokens[:4])}")
    return min(max_points, len(shared_tokens) * multiplier)


def _load_current_document_page_signals(local_path: str | None) -> dict[str, object]:
    if not local_path:
        return {
            "headings": [],
            "heading_tokens": set(),
            "leaf_nos": [],
        }

    path = Path(local_path)
    if not path.exists():
        return {
            "headings": [],
            "heading_tokens": set(),
            "leaf_nos": [],
        }

    try:
        pages = mine_document_pages(str(path), max_pages=2)
    except Exception:
        return {
            "headings": [],
            "heading_tokens": set(),
            "leaf_nos": [],
        }

    headings: list[str] = []
    leaf_nos: list[str] = []
    for page in pages:
        for heading in page.extracted_schedule_codes:
            cleaned = re.sub(r"\s+", " ", heading).strip()
            if cleaned and cleaned not in headings:
                headings.append(cleaned)
        for leaf_no in page.extracted_leaf_nos:
            if leaf_no and leaf_no not in leaf_nos:
                leaf_nos.append(leaf_no)

    heading_tokens: set[str] = set()
    for heading in headings:
        heading_tokens.update(_normalized_tokens(heading))

    return {
        "headings": headings,
        "heading_tokens": heading_tokens,
        "leaf_nos": leaf_nos,
    }


def _load_family_historical_leaf_nos(conn: sqlite3.Connection, family_key: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT leaf_no
        FROM historical_documents
        WHERE family_key = ? AND COALESCE(leaf_no, '') <> ''
        """,
        (family_key,),
    ).fetchall()
    return {str(row["leaf_no"]).strip() for row in rows if row["leaf_no"]}


class Repository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def _connect(self):
        return connect(self.database_path)

    def upsert_document(self, record: DiscoveryRecord) -> int:
        payload = record.model_dump(mode="json")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM documents WHERE document_url = ? AND content_hash = ?",
                (str(record.document_url), record.content_hash),
            ).fetchone()
            if existing:
                return int(existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO documents (
                    title, source_page_url, document_url, state, company, category, kind,
                    effective_date, local_path, content_hash, content_type, status_code,
                    discovered_at, retrieved_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.title,
                    str(record.source_page_url),
                    str(record.document_url),
                    record.state,
                    record.company,
                    record.category.value,
                    record.kind.value,
                    record.effective_date,
                    record.local_path,
                    record.content_hash,
                    record.content_type,
                    record.status_code,
                    record.retrieval_timestamp.isoformat(),
                    record.retrieval_timestamp.isoformat(),
                    json.dumps(payload, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def list_documents(
        self, *, state: str | None = None, company: str | None = None
    ) -> list[StoredDocument]:
        query = "SELECT * FROM documents"
        clauses: list[str] = []
        params: list[object] = []
        if state:
            clauses.append("state = ?")
            params.append(state.upper())
        if company:
            clauses.append("LOWER(COALESCE(company, '')) LIKE ?")
            params.append(f"%{company.lower()}%")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY retrieved_at DESC, id DESC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_document(row) for row in rows]

    def get_document(self, document_id: int) -> StoredDocument | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
        return self._row_to_document(row) if row else None

    def save_parse_result(self, result: DocumentParseResult) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO parse_results (
                    document_id, parser_name, status, result_json, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    result.document_id,
                    result.parser_name,
                    result.status.value,
                    result.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def latest_parse_result(self, document_id: int) -> DocumentParseResult | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT result_json FROM parse_results
                WHERE document_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (document_id,),
            ).fetchone()
        return DocumentParseResult.model_validate_json(row["result_json"]) if row else None

    def upsert_historical_document(self, record: HistoricalDocumentRecord) -> int:
        payload = record.model_dump(mode="json", exclude={"id"})
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM historical_documents
                WHERE family_key = ? AND archived_url = ?
                """,
                (record.family_key, record.archived_url),
            ).fetchone()
            if not existing:
                existing = conn.execute(
                    """
                    SELECT id FROM historical_documents
                    WHERE archived_url = ?
                    """,
                    (record.archived_url,),
                ).fetchone()
            if not existing:
                existing = conn.execute(
                    """
                    SELECT id FROM historical_documents
                    WHERE family_key = ? AND content_hash = ?
                    """,
                    (record.family_key, record.content_hash),
                ).fetchone()
            if not existing and (
                record.revision_label or record.effective_start or record.effective_end
            ):
                existing = conn.execute(
                    """
                    SELECT id FROM historical_documents
                    WHERE family_key = ?
                      AND COALESCE(revision_label, '') = COALESCE(?, '')
                      AND COALESCE(effective_start, '') = COALESCE(?, '')
                      AND COALESCE(effective_end, '') = COALESCE(?, '')
                    """,
                    (
                        record.family_key,
                        record.revision_label,
                        record.effective_start,
                        record.effective_end,
                    ),
                ).fetchone()
            if existing:
                # Preserve any operator-set status on the existing row unless the
                # caller is supplying something other than the default 'approved'.
                preserve_status = bool(
                    record.status == "approved"
                    and record.requested_effective_date is None
                    and record.approved_document_id is None
                )
                if preserve_status:
                    update_sql = """
                        UPDATE historical_documents
                        SET current_document_id = ?, family_key = ?, title = ?, state = ?, company = ?,
                            category = ?, kind = ?, canonical_url = ?, snapshot_timestamp = ?,
                            archived_url = ?, local_path = ?, raw_text_path = ?, content_hash = ?,
                            content_type = ?,
                            direct_status_code = ?, direct_downloadable = ?, revision_label = ?,
                            supersedes_label = ?, leaf_no = ?,
                            effective_start = ?, effective_end = ?, retrieved_at = ?, metadata_json = ?,
                            start_page = ?, end_page = ?, evidence_json = ?
                        WHERE id = ?
                    """
                    params = (
                        record.current_document_id,
                        record.family_key,
                        record.title,
                        record.state,
                        record.company,
                        record.category,
                        record.kind,
                        record.canonical_url,
                        record.snapshot_timestamp.isoformat(),
                        record.archived_url,
                        str(record.local_path),
                        str(record.raw_text_path) if record.raw_text_path else None,
                        record.content_hash,
                        record.content_type,
                        record.direct_status_code,
                        1 if record.direct_downloadable else 0,
                        record.revision_label,
                        record.supersedes_label,
                        record.leaf_no,
                        record.effective_start,
                        record.effective_end,
                        record.retrieved_at.isoformat(),
                        json.dumps(payload, sort_keys=True),
                        record.start_page,
                        record.end_page,
                        record.evidence_json,
                        int(existing["id"]),
                    )
                else:
                    update_sql = """
                        UPDATE historical_documents
                        SET current_document_id = ?, family_key = ?, title = ?, state = ?, company = ?,
                            category = ?, kind = ?, canonical_url = ?, snapshot_timestamp = ?,
                            archived_url = ?, local_path = ?, raw_text_path = ?, content_hash = ?,
                            content_type = ?,
                            direct_status_code = ?, direct_downloadable = ?, revision_label = ?,
                            supersedes_label = ?, leaf_no = ?,
                            effective_start = ?, effective_end = ?, retrieved_at = ?, metadata_json = ?,
                            start_page = ?, end_page = ?, evidence_json = ?,
                            status = ?, requested_effective_date = ?, approved_document_id = ?
                        WHERE id = ?
                    """
                    params = (
                        record.current_document_id,
                        record.family_key,
                        record.title,
                        record.state,
                        record.company,
                        record.category,
                        record.kind,
                        record.canonical_url,
                        record.snapshot_timestamp.isoformat(),
                        record.archived_url,
                        str(record.local_path),
                        str(record.raw_text_path) if record.raw_text_path else None,
                        record.content_hash,
                        record.content_type,
                        record.direct_status_code,
                        1 if record.direct_downloadable else 0,
                        record.revision_label,
                        record.supersedes_label,
                        record.leaf_no,
                        record.effective_start,
                        record.effective_end,
                        record.retrieved_at.isoformat(),
                        json.dumps(payload, sort_keys=True),
                        record.start_page,
                        record.end_page,
                        record.evidence_json,
                        record.status,
                        record.requested_effective_date,
                        record.approved_document_id,
                        int(existing["id"]),
                    )
                conn.execute(update_sql, params)
                return int(existing["id"])

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO historical_documents (
                        current_document_id, family_key, title, state, company, category, kind,
                        canonical_url, archived_url, snapshot_timestamp, local_path, raw_text_path,
                        content_hash, content_type, direct_status_code, direct_downloadable,
                        revision_label, supersedes_label, leaf_no, effective_start, effective_end,
                        retrieved_at, metadata_json, start_page, end_page, evidence_json,
                        status, requested_effective_date, approved_document_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.current_document_id,
                        record.family_key,
                        record.title,
                        record.state,
                        record.company,
                        record.category,
                        record.kind,
                        record.canonical_url,
                        record.archived_url,
                        record.snapshot_timestamp.isoformat(),
                        str(record.local_path),
                        str(record.raw_text_path) if record.raw_text_path else None,
                        record.content_hash,
                        record.content_type,
                        record.direct_status_code,
                        1 if record.direct_downloadable else 0,
                        record.revision_label,
                        record.supersedes_label,
                        record.leaf_no,
                        record.effective_start,
                        record.effective_end,
                        record.retrieved_at.isoformat(),
                        json.dumps(payload, sort_keys=True),
                        record.start_page,
                        record.end_page,
                        record.evidence_json,
                        record.status,
                        record.requested_effective_date,
                        record.approved_document_id,
                    ),
                )
                return int(cursor.lastrowid)
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    """
                    SELECT id FROM historical_documents
                    WHERE archived_url = ?
                    """,
                    (record.archived_url,),
                ).fetchone()
                if existing:
                    return int(existing["id"])
                raise

    def save_historical_parse_result(self, historical_id: int, result: DocumentParseResult) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE historical_documents SET parsed_result_json = ? WHERE id = ?",
                (result.model_dump_json(), historical_id),
            )

    def list_historical_documents(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
    ) -> list[HistoricalDocumentRecord]:
        query = "SELECT * FROM historical_documents"
        clauses: list[str] = []
        params: list[object] = []
        if state:
            clauses.append("state = ?")
            params.append(state.upper())
        if company:
            clauses.append("LOWER(COALESCE(company, '')) LIKE ?")
            params.append(f"%{company.lower()}%")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY snapshot_timestamp DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_historical_document(row) for row in rows]

    def get_historical_document(self, historical_id: int) -> HistoricalDocumentRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM historical_documents WHERE id = ?",
                (historical_id,),
            ).fetchone()
        return self._row_to_historical_document(row) if row else None

    def update_historical_document_family(
        self,
        historical_id: int,
        *,
        family_key: str,
        current_document_id: int | None,
        title: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE historical_documents
                SET family_key = ?, current_document_id = ?, title = COALESCE(?, title)
                WHERE id = ?
                """,
                (
                    family_key,
                    current_document_id,
                    title,
                    historical_id,
                ),
            )

    def upsert_bill_statement(
        self,
        statement: BillStatementData,
        *,
        content_hash: str,
        raw_text_path: str | None = None,
    ) -> int:
        payload = statement.model_dump(mode="json")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM bill_statements WHERE source_path = ? AND content_hash = ?",
                (statement.source_path, content_hash),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE bill_statements
                    SET account_number = ?, bill_date = ?, due_date = ?, service_start = ?,
                        service_end = ?, total_amount_due = ?, raw_text_path = ?,
                        statement_json = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (
                        statement.account_number,
                        statement.bill_date.isoformat() if statement.bill_date else None,
                        statement.due_date.isoformat() if statement.due_date else None,
                        statement.service_start.isoformat() if statement.service_start else None,
                        statement.service_end.isoformat() if statement.service_end else None,
                        statement.billing_summary.total_amount_due,
                        raw_text_path,
                        json.dumps(payload, sort_keys=True),
                        datetime.now(UTC).isoformat(),
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO bill_statements (
                    source_path, account_number, bill_date, due_date, service_start,
                    service_end, total_amount_due, content_hash, raw_text_path,
                    statement_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    statement.source_path,
                    statement.account_number,
                    statement.bill_date.isoformat() if statement.bill_date else None,
                    statement.due_date.isoformat() if statement.due_date else None,
                    statement.service_start.isoformat() if statement.service_start else None,
                    statement.service_end.isoformat() if statement.service_end else None,
                    statement.billing_summary.total_amount_due,
                    content_hash,
                    raw_text_path,
                    json.dumps(payload, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def list_bill_statements(self) -> list[StoredBillStatement]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bill_statements ORDER BY COALESCE(bill_date, '') DESC, id DESC"
            ).fetchall()
        return [self._row_to_bill_statement(row) for row in rows]

    def get_bill_statement(self, bill_id: int) -> StoredBillStatement | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bill_statements WHERE id = ?",
                (bill_id,),
            ).fetchone()
        return self._row_to_bill_statement(row) if row else None

    def replace_bill_component_observations(
        self,
        *,
        bill_id: int,
        observations: list[BillComponentObservation],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM bill_component_observations WHERE bill_id = ?",
                (bill_id,),
            )
            for observation in observations:
                conn.execute(
                    """
                    INSERT INTO bill_component_observations (
                        bill_id, source_path, section_name, rate_code, component_key,
                        component_label, amount, service_start, service_end, period_start,
                        period_end, days_in_period, quantity_basis_kwh, inferred_unit,
                        inferred_value, confidence, notes_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.bill_id,
                        observation.source_path,
                        observation.section_name,
                        observation.rate_code,
                        observation.component_key,
                        observation.component_label,
                        observation.amount,
                        observation.service_start.isoformat()
                        if observation.service_start
                        else None,
                        observation.service_end.isoformat() if observation.service_end else None,
                        observation.period_start.isoformat()
                        if observation.period_start
                        else None,
                        observation.period_end.isoformat() if observation.period_end else None,
                        observation.days_in_period,
                        observation.quantity_basis_kwh,
                        observation.inferred_unit,
                        observation.inferred_value,
                        observation.confidence,
                        json.dumps(observation.notes),
                        datetime.now(UTC).isoformat(),
                    ),
                )

    def list_bill_component_observations(
        self,
        *,
        bill_id: int | None = None,
        component_key: str | None = None,
    ) -> list[BillComponentObservation]:
        query = "SELECT * FROM bill_component_observations"
        clauses: list[str] = []
        params: list[object] = []
        if bill_id is not None:
            clauses.append("bill_id = ?")
            params.append(bill_id)
        if component_key:
            clauses.append("component_key = ?")
            params.append(component_key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += (
            " ORDER BY COALESCE(service_end, ''), COALESCE(period_start, ''), "
            "section_name, component_key, id"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_bill_component_observation(row) for row in rows]

    def upsert_historical_lead(self, record: HistoricalLeadRecord) -> int:
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM historical_leads
                WHERE family_key = ?
                  AND COALESCE(extracted_url, '') = COALESCE(?, '')
                  AND COALESCE(docket_number, '') = COALESCE(?, '')
                  AND COALESCE(extraction_method, '') = COALESCE(?, '')
                  AND COALESCE(source_location, '') = COALESCE(?, '')
                """,
                (
                    record.family_key,
                    record.extracted_url,
                    record.docket_number,
                    record.extraction_method,
                    record.source_location,
                ),
            ).fetchone()
            payload = record.model_dump(mode="json", exclude={"id", "created_at"})
            created_at = (record.created_at or datetime.now(UTC)).isoformat()
            if existing:
                conn.execute(
                    """
                    UPDATE historical_leads
                    SET target_leaf_no = ?, target_code = ?, target_title = ?, family_type = ?,
                        category = ?, source_class = ?, provenance_class = ?, source_label = ?,
                        source_url = ?, extracted_title = ?, attachment_url = ?, viewer_url = ?,
                        hostname = ?, path_fragment = ?, filename = ?, schedule_code = ?,
                        rider_code = ?, leaf_reference = ?, effective_start = ?, effective_end = ?,
                        confidence_score = ?, disposition = ?, score_notes_json = ?, notes_json = ?,
                        metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        record.target_leaf_no,
                        record.target_code,
                        record.target_title,
                        record.family_type,
                        record.category,
                        record.source_class,
                        record.provenance_class,
                        record.source_label,
                        record.source_url,
                        record.extracted_title,
                        record.attachment_url,
                        record.viewer_url,
                        record.hostname,
                        record.path_fragment,
                        record.filename,
                        record.schedule_code,
                        record.rider_code,
                        record.leaf_reference,
                        record.effective_start,
                        record.effective_end,
                        record.confidence_score,
                        record.disposition,
                        json.dumps(record.score_notes),
                        json.dumps(record.notes),
                        json.dumps(payload, sort_keys=True),
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO historical_leads (
                    family_key, target_leaf_no, target_code, target_title, family_type,
                    category, source_class, provenance_class, source_label, source_location,
                    source_url, extracted_url, extracted_title, attachment_url, viewer_url,
                    hostname, path_fragment, filename, docket_number, schedule_code, rider_code,
                    leaf_reference, effective_start, effective_end, extraction_method,
                    confidence_score, disposition, score_notes_json, notes_json,
                    metadata_json, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    record.family_key,
                    record.target_leaf_no,
                    record.target_code,
                    record.target_title,
                    record.family_type,
                    record.category,
                    record.source_class,
                    record.provenance_class,
                    record.source_label,
                    record.source_location,
                    record.source_url,
                    record.extracted_url,
                    record.extracted_title,
                    record.attachment_url,
                    record.viewer_url,
                    record.hostname,
                    record.path_fragment,
                    record.filename,
                    record.docket_number,
                    record.schedule_code,
                    record.rider_code,
                    record.leaf_reference,
                    record.effective_start,
                    record.effective_end,
                    record.extraction_method,
                    record.confidence_score,
                    record.disposition,
                    json.dumps(record.score_notes),
                    json.dumps(record.notes),
                    json.dumps(payload, sort_keys=True),
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def list_historical_leads(
        self,
        *,
        family_key: str | None = None,
        target_code: str | None = None,
        disposition: str | None = None,
    ) -> list[HistoricalLeadRecord]:
        query = "SELECT * FROM historical_leads"
        clauses: list[str] = []
        params: list[object] = []
        if family_key:
            clauses.append("family_key = ?")
            params.append(family_key)
        if target_code:
            clauses.append("target_code = ?")
            params.append(target_code)
        if disposition:
            clauses.append("disposition = ?")
            params.append(disposition)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY confidence_score DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_historical_lead(row) for row in rows]

    def upsert_candidate_url_variant(self, record: CandidateUrlVariantRecord) -> int:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM candidate_url_variants WHERE variant_url = ?",
                (record.variant_url,),
            ).fetchone()
            payload = record.model_dump(mode="json", exclude={"id", "created_at"})
            created_at = (record.created_at or datetime.now(UTC)).isoformat()
            if existing:
                conn.execute(
                    """
                    UPDATE candidate_url_variants
                    SET family_key = ?, lead_id = ?, hostname = ?, path_family = ?, filename = ?,
                        heuristic = ?, direct_status_code = ?, direct_downloadable = ?,
                        wayback_snapshot_count = ?, wayback_first_timestamp = ?, score = ?,
                        disposition = ?, notes_json = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        record.family_key,
                        record.lead_id,
                        record.hostname,
                        record.path_family,
                        record.filename,
                        record.heuristic,
                        record.direct_status_code,
                        1 if record.direct_downloadable else 0,
                        record.wayback_snapshot_count,
                        record.wayback_first_timestamp,
                        record.score,
                        record.disposition,
                        json.dumps(record.notes),
                        json.dumps(payload, sort_keys=True),
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO candidate_url_variants (
                    family_key, lead_id, variant_url, hostname, path_family, filename,
                    heuristic, direct_status_code, direct_downloadable, wayback_snapshot_count,
                    wayback_first_timestamp, score, disposition, notes_json, metadata_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.family_key,
                    record.lead_id,
                    record.variant_url,
                    record.hostname,
                    record.path_family,
                    record.filename,
                    record.heuristic,
                    record.direct_status_code,
                    1 if record.direct_downloadable else 0,
                    record.wayback_snapshot_count,
                    record.wayback_first_timestamp,
                    record.score,
                    record.disposition,
                    json.dumps(record.notes),
                    json.dumps(payload, sort_keys=True),
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def list_candidate_url_variants(
        self,
        *,
        family_key: str | None = None,
        lead_id: int | None = None,
    ) -> list[CandidateUrlVariantRecord]:
        query = "SELECT * FROM candidate_url_variants"
        clauses: list[str] = []
        params: list[object] = []
        if family_key:
            clauses.append("family_key = ?")
            params.append(family_key)
        if lead_id is not None:
            clauses.append("lead_id = ?")
            params.append(lead_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY score DESC, wayback_snapshot_count DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_candidate_url_variant(row) for row in rows]

    def upsert_search_pack(self, record: HistoricalSearchPackRecord) -> int:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM historical_search_packs WHERE family_key = ?",
                (record.family_key,),
            ).fetchone()
            created_at = record.created_at or datetime.now(UTC)
            updated_at = record.updated_at or datetime.now(UTC)
            if existing:
                conn.execute(
                    """
                    UPDATE historical_search_packs
                    SET target_leaf_no = ?, target_code = ?, target_title = ?, family_type = ?,
                        payload_json = ?, notes_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        record.target_leaf_no,
                        record.target_code,
                        record.target_title,
                        record.family_type,
                        record.payload_json,
                        json.dumps(record.notes),
                        updated_at.isoformat(),
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO historical_search_packs (
                    family_key, target_leaf_no, target_code, target_title, family_type,
                    payload_json, notes_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.family_key,
                    record.target_leaf_no,
                    record.target_code,
                    record.target_title,
                    record.family_type,
                    record.payload_json,
                    json.dumps(record.notes),
                    created_at.isoformat(),
                    updated_at.isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def list_search_packs(
        self,
        *,
        family_key: str | None = None,
    ) -> list[HistoricalSearchPackRecord]:
        query = "SELECT * FROM historical_search_packs"
        params: list[object] = []
        if family_key:
            query += " WHERE family_key = ?"
            params.append(family_key)
        query += " ORDER BY family_key"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_search_pack(row) for row in rows]

    def get_search_pack(self, family_key: str) -> HistoricalSearchPackRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM historical_search_packs WHERE family_key = ?",
                (family_key,),
            ).fetchone()
        return self._row_to_search_pack(row) if row else None

    def upsert_regulatory_docket_lead(self, record: RegulatoryDocketLeadRecord) -> int:
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM regulatory_docket_leads
                WHERE family_key = ? AND docket_number = ?
                  AND COALESCE(evidence_source_location, '') = COALESCE(?, '')
                  AND COALESCE(title, '') = COALESCE(?, '')
                """,
                (
                    record.family_key,
                    record.docket_number,
                    record.evidence_source_location,
                    record.title,
                ),
            ).fetchone()
            payload = record.model_dump(mode="json", exclude={"id", "created_at"})
            created_at = (record.created_at or datetime.now(UTC)).isoformat()
            if existing:
                conn.execute(
                    """
                    UPDATE regulatory_docket_leads
                    SET utility = ?, proceeding_type = ?, date_start = ?, date_end = ?,
                        referenced_codes_json = ?, evidence_source = ?, evidence_source_type = ?,
                        contains_tariff_text = ?, clue_only = ?, confidence_score = ?,
                        notes_json = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        record.utility,
                        record.proceeding_type,
                        record.date_start,
                        record.date_end,
                        json.dumps(record.referenced_codes),
                        record.evidence_source,
                        record.evidence_source_type,
                        1 if record.contains_tariff_text else 0,
                        1 if record.clue_only else 0,
                        record.confidence_score,
                        json.dumps(record.notes),
                        json.dumps(payload, sort_keys=True),
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO regulatory_docket_leads (
                    family_key, docket_number, utility, proceeding_type, date_start, date_end,
                    referenced_codes_json, evidence_source, evidence_source_type,
                    evidence_source_location, title, contains_tariff_text, clue_only,
                    confidence_score, notes_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.family_key,
                    record.docket_number,
                    record.utility,
                    record.proceeding_type,
                    record.date_start,
                    record.date_end,
                    json.dumps(record.referenced_codes),
                    record.evidence_source,
                    record.evidence_source_type,
                    record.evidence_source_location,
                    record.title,
                    1 if record.contains_tariff_text else 0,
                    1 if record.clue_only else 0,
                    record.confidence_score,
                    json.dumps(record.notes),
                    json.dumps(payload, sort_keys=True),
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def list_regulatory_docket_leads(
        self,
        *,
        family_key: str | None = None,
        docket_number: str | None = None,
    ) -> list[RegulatoryDocketLeadRecord]:
        query = "SELECT * FROM regulatory_docket_leads"
        clauses: list[str] = []
        params: list[object] = []
        if family_key:
            clauses.append("family_key = ?")
            params.append(family_key)
        if docket_number:
            clauses.append("docket_number = ?")
            params.append(docket_number)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY confidence_score DESC, docket_number, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_regulatory_docket_lead(row) for row in rows]

    def upsert_evidence_anchor(self, record: EvidenceAnchorRecord) -> int:
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM evidence_anchors
                WHERE family_key = ? AND anchor_type = ? AND anchor_value = ?
                  AND COALESCE(source_location, '') = COALESCE(?, '')
                """,
                (
                    record.family_key,
                    record.anchor_type,
                    record.anchor_value,
                    record.source_location,
                ),
            ).fetchone()
            payload = record.model_dump(mode="json", exclude={"id", "created_at"})
            created_at = (record.created_at or datetime.now(UTC)).isoformat()
            if existing:
                conn.execute(
                    """
                    UPDATE evidence_anchors
                    SET start_date = ?, end_date = ?, source_type = ?, confidence_score = ?,
                        notes_json = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        record.start_date,
                        record.end_date,
                        record.source_type,
                        record.confidence_score,
                        json.dumps(record.notes),
                        json.dumps(payload, sort_keys=True),
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO evidence_anchors (
                    family_key, anchor_type, anchor_value, start_date, end_date, source_type,
                    source_location, confidence_score, notes_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.family_key,
                    record.anchor_type,
                    record.anchor_value,
                    record.start_date,
                    record.end_date,
                    record.source_type,
                    record.source_location,
                    record.confidence_score,
                    json.dumps(record.notes),
                    json.dumps(payload, sort_keys=True),
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def list_evidence_anchors(
        self,
        *,
        family_key: str | None = None,
        anchor_type: str | None = None,
    ) -> list[EvidenceAnchorRecord]:
        query = "SELECT * FROM evidence_anchors"
        clauses: list[str] = []
        params: list[object] = []
        if family_key:
            clauses.append("family_key = ?")
            params.append(family_key)
        if anchor_type:
            clauses.append("anchor_type = ?")
            params.append(anchor_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY family_key, anchor_type, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_evidence_anchor(row) for row in rows]

    @staticmethod
    def _row_to_document(row) -> StoredDocument:
        return StoredDocument(
            id=int(row["id"]),
            title=row["title"],
            source_page_url=row["source_page_url"],
            document_url=row["document_url"],
            state=row["state"],
            company=row["company"],
            category=row["category"],
            kind=row["kind"],
            effective_date=row["effective_date"],
            local_path=Path(row["local_path"]),
            content_hash=row["content_hash"],
            content_type=row["content_type"],
            status_code=row["status_code"],
            retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
            discovered_at=datetime.fromisoformat(row["discovered_at"]),
            metadata_json=row["metadata_json"],
            tariff_identifier=row["tariff_identifier"] if "tariff_identifier" in row.keys() else None,
            schedule_code=row["schedule_code"] if "schedule_code" in row.keys() else None,
            rev_token=row["rev_token"] if "rev_token" in row.keys() else None,
        )

    def classify_documents(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
    ) -> int:
        """Classify PDF documents: populate tariff_identifier, schedule_code, rev_token from URL patterns."""
        from duke_rates.discovery.classifier import classify_document_url

        docs = self.list_documents(state=state, company=company)
        updated = 0
        with self._connect() as conn:
            for doc in docs:
                if doc.kind != "pdf":
                    continue
                result = classify_document_url(
                    doc.document_url, state=doc.state, company=doc.company
                )
                if any(v is not None for v in result.values()):
                    conn.execute(
                        """
                        UPDATE documents
                        SET tariff_identifier = ?,
                            schedule_code = ?,
                            rev_token = ?
                        WHERE id = ?
                        """,
                        (
                            result["tariff_identifier"],
                            result["schedule_code"],
                            result["rev_token"],
                            doc.id,
                        ),
                    )
                    updated += 1
        return updated

    def get_document_by_base_url(self, document_url: str) -> StoredDocument | None:
        """Look up a document by URL, ignoring query parameters (strips ?rev= etc.)."""
        from urllib.parse import urlparse
        parsed = urlparse(document_url)
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE document_url LIKE ? ORDER BY retrieved_at DESC LIMIT 1",
                (f"{base}%",),
            ).fetchone()
            return self._row_to_document(row) if row else None

    @staticmethod
    def _row_to_historical_document(row) -> HistoricalDocumentRecord:
        return HistoricalDocumentRecord(
            id=int(row["id"]),
            current_document_id=_coerce_optional_int(row["current_document_id"]),
            family_key=row["family_key"],
            title=row["title"],
            state=row["state"],
            company=row["company"],
            category=row["category"],
            kind=row["kind"],
            canonical_url=row["canonical_url"],
            archived_url=row["archived_url"],
            snapshot_timestamp=datetime.fromisoformat(row["snapshot_timestamp"]),
            local_path=Path(row["local_path"]),
            raw_text_path=Path(row["raw_text_path"]) if row["raw_text_path"] else None,
            content_hash=row["content_hash"],
            content_type=row["content_type"],
            direct_status_code=row["direct_status_code"],
            direct_downloadable=bool(row["direct_downloadable"]),
            revision_label=row["revision_label"],
            supersedes_label=row["supersedes_label"],
            leaf_no=row["leaf_no"],
            start_page=int(row["start_page"]) if row["start_page"] is not None else None,
            end_page=int(row["end_page"]) if row["end_page"] is not None else None,
            evidence_json=row["evidence_json"],
            effective_start=row["effective_start"],
            effective_end=row["effective_end"],
            retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
            metadata_json=row["metadata_json"],
            parsed_result_json=row["parsed_result_json"],
        )

    @staticmethod
    def _row_to_bill_statement(row) -> StoredBillStatement:
        return StoredBillStatement(
            id=int(row["id"]),
            source_path=row["source_path"],
            account_number=row["account_number"],
            bill_date=date.fromisoformat(row["bill_date"]) if row["bill_date"] else None,
            due_date=date.fromisoformat(row["due_date"]) if row["due_date"] else None,
            service_start=(
                date.fromisoformat(row["service_start"]) if row["service_start"] else None
            ),
            service_end=date.fromisoformat(row["service_end"]) if row["service_end"] else None,
            total_amount_due=row["total_amount_due"],
            content_hash=row["content_hash"],
            raw_text_path=row["raw_text_path"],
            statement_json=row["statement_json"],
        )

    @staticmethod
    def _row_to_bill_component_observation(row) -> BillComponentObservation:
        return BillComponentObservation(
            id=int(row["id"]),
            bill_id=int(row["bill_id"]),
            source_path=row["source_path"],
            section_name=row["section_name"],
            rate_code=row["rate_code"],
            component_key=row["component_key"],
            component_label=row["component_label"],
            amount=float(row["amount"]),
            service_start=(
                date.fromisoformat(row["service_start"]) if row["service_start"] else None
            ),
            service_end=date.fromisoformat(row["service_end"]) if row["service_end"] else None,
            period_start=(
                date.fromisoformat(row["period_start"]) if row["period_start"] else None
            ),
            period_end=date.fromisoformat(row["period_end"]) if row["period_end"] else None,
            days_in_period=row["days_in_period"],
            quantity_basis_kwh=row["quantity_basis_kwh"],
            inferred_unit=row["inferred_unit"],
            inferred_value=row["inferred_value"],
            confidence=float(row["confidence"]),
            notes=json.loads(row["notes_json"]),
        )

    @staticmethod
    def _row_to_historical_lead(row) -> HistoricalLeadRecord:
        return HistoricalLeadRecord(
            id=int(row["id"]),
            family_key=row["family_key"],
            target_leaf_no=row["target_leaf_no"],
            target_code=row["target_code"],
            target_title=row["target_title"],
            family_type=row["family_type"],
            category=row["category"],
            source_class=row["source_class"],
            provenance_class=row["provenance_class"],
            source_label=row["source_label"],
            source_location=row["source_location"],
            source_url=row["source_url"],
            extracted_url=row["extracted_url"],
            extracted_title=row["extracted_title"],
            attachment_url=row["attachment_url"],
            viewer_url=row["viewer_url"],
            hostname=row["hostname"],
            path_fragment=row["path_fragment"],
            filename=row["filename"],
            docket_number=row["docket_number"],
            schedule_code=row["schedule_code"],
            rider_code=row["rider_code"],
            leaf_reference=row["leaf_reference"],
            effective_start=row["effective_start"],
            effective_end=row["effective_end"],
            extraction_method=row["extraction_method"],
            confidence_score=float(row["confidence_score"]),
            disposition=row["disposition"],
            score_notes=json.loads(row["score_notes_json"]),
            notes=json.loads(row["notes_json"]),
            metadata_json=row["metadata_json"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_candidate_url_variant(row) -> CandidateUrlVariantRecord:
        return CandidateUrlVariantRecord(
            id=int(row["id"]),
            family_key=row["family_key"],
            lead_id=row["lead_id"],
            variant_url=row["variant_url"],
            hostname=row["hostname"],
            path_family=row["path_family"],
            filename=row["filename"],
            heuristic=row["heuristic"],
            direct_status_code=row["direct_status_code"],
            direct_downloadable=bool(row["direct_downloadable"]),
            wayback_snapshot_count=int(row["wayback_snapshot_count"]),
            wayback_first_timestamp=row["wayback_first_timestamp"],
            score=float(row["score"]),
            disposition=row["disposition"],
            notes=json.loads(row["notes_json"]),
            metadata_json=row["metadata_json"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_search_pack(row) -> HistoricalSearchPackRecord:
        return HistoricalSearchPackRecord(
            id=int(row["id"]),
            family_key=row["family_key"],
            target_leaf_no=row["target_leaf_no"],
            target_code=row["target_code"],
            target_title=row["target_title"],
            family_type=row["family_type"],
            payload_json=row["payload_json"],
            notes=json.loads(row["notes_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_regulatory_docket_lead(row) -> RegulatoryDocketLeadRecord:
        return RegulatoryDocketLeadRecord(
            id=int(row["id"]),
            family_key=row["family_key"],
            docket_number=row["docket_number"],
            utility=row["utility"],
            proceeding_type=row["proceeding_type"],
            date_start=row["date_start"],
            date_end=row["date_end"],
            referenced_codes=json.loads(row["referenced_codes_json"]),
            evidence_source=row["evidence_source"],
            evidence_source_type=row["evidence_source_type"],
            evidence_source_location=row["evidence_source_location"],
            title=row["title"],
            contains_tariff_text=bool(row["contains_tariff_text"]),
            clue_only=bool(row["clue_only"]),
            confidence_score=float(row["confidence_score"]),
            notes=json.loads(row["notes_json"]),
            metadata_json=row["metadata_json"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_evidence_anchor(row) -> EvidenceAnchorRecord:
        return EvidenceAnchorRecord(
            id=int(row["id"]),
            family_key=row["family_key"],
            anchor_type=row["anchor_type"],
            anchor_value=row["anchor_value"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            source_type=row["source_type"],
            source_location=row["source_location"],
            confidence_score=float(row["confidence_score"]),
            notes=json.loads(row["notes_json"]),
            metadata_json=row["metadata_json"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ------------------------------------------------------------------
    # NCUC discovery records
    # ------------------------------------------------------------------

    def upsert_ncuc_discovery_record(self, record: NcucDiscoveryRecord) -> int:
        created_at = (record.created_at or datetime.now(UTC)).isoformat()
        with self._connect() as conn:
            # Deduplicate on (docket_number, filing_title, discovered_url) when available,
            # otherwise on content_hash alone for downloaded records.
            existing = None
            if record.content_hash:
                existing = conn.execute(
                    "SELECT id FROM ncuc_discovery_records WHERE content_hash = ?",
                    (record.content_hash,),
                ).fetchone()
            if not existing and record.attachment_url:
                existing = conn.execute(
                    "SELECT id FROM ncuc_discovery_records WHERE attachment_url = ?",
                    (record.attachment_url,),
                ).fetchone()
            if not existing and record.viewer_url:
                existing = conn.execute(
                    "SELECT id FROM ncuc_discovery_records WHERE viewer_url = ?",
                    (record.viewer_url,),
                ).fetchone()
            if (
                not existing
                and record.discovered_url
                and not record.attachment_url
                and not record.viewer_url
            ):
                existing = conn.execute(
                    "SELECT id FROM ncuc_discovery_records WHERE discovered_url = ?",
                    (record.discovered_url,),
                ).fetchone()
            if not existing and record.docket_number and record.filing_title:
                existing = conn.execute(
                    """
                    SELECT id FROM ncuc_discovery_records
                    WHERE docket_number = ?
                      AND COALESCE(filing_title, '') = COALESCE(?, '')
                    """,
                    (record.docket_number, record.filing_title),
                ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE ncuc_discovery_records SET
                        docket_number = ?, sub_number = ?, utility = ?,
                        filing_title = ?, filing_date = ?, proceeding_type = ?,
                        filing_classification = ?, exhibit_label = ?,
                        referenced_schedule_codes_json = ?,
                        referenced_rider_codes_json = ?,
                        referenced_leaf_nos_json = ?,
                        family_keys_json = ?,
                        discovered_url = ?, viewer_url = ?, attachment_url = ?,
                        download_url = ?, acquisition_method = ?,
                        fetch_status = ?, local_path = ?, content_hash = ?,
                        content_type = ?, file_size_bytes = ?,
                        provenance_notes_json = ?, search_query = ?,
                        page_title = ?, error_detail = ?, metadata_json = ?,
                        doc_quality_tier = ?, search_confidence_score = ?,
                        search_ideality = ?,
                        fetched_at = ?
                    WHERE id = ?
                    """,
                    (
                        record.docket_number,
                        record.sub_number,
                        record.utility,
                        record.filing_title,
                        record.filing_date,
                        record.proceeding_type,
                        record.filing_classification.value,
                        record.exhibit_label,
                        json.dumps(record.referenced_schedule_codes),
                        json.dumps(record.referenced_rider_codes),
                        json.dumps(record.referenced_leaf_nos),
                        json.dumps(record.family_keys),
                        record.discovered_url,
                        record.viewer_url,
                        record.attachment_url,
                        record.download_url,
                        record.acquisition_method.value,
                        record.fetch_status.value,
                        record.local_path,
                        record.content_hash,
                        record.content_type,
                        record.file_size_bytes,
                        json.dumps(record.provenance_notes),
                        record.search_query,
                        record.page_title,
                        record.error_detail,
                        record.metadata_json,
                        record.doc_quality_tier,
                        record.search_confidence_score,
                        record.search_ideality,
                        record.fetched_at.isoformat() if record.fetched_at else None,
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO ncuc_discovery_records (
                    docket_number, sub_number, utility, filing_title, filing_date,
                    proceeding_type, filing_classification, exhibit_label,
                    referenced_schedule_codes_json, referenced_rider_codes_json,
                    referenced_leaf_nos_json, family_keys_json,
                    discovered_url, viewer_url, attachment_url, download_url,
                    acquisition_method, fetch_status, local_path, content_hash,
                    content_type, file_size_bytes, provenance_notes_json,
                    search_query, page_title, error_detail, metadata_json,
                    doc_quality_tier, search_confidence_score, search_ideality,
                    created_at, fetched_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    record.docket_number,
                    record.sub_number,
                    record.utility,
                    record.filing_title,
                    record.filing_date,
                    record.proceeding_type,
                    record.filing_classification.value,
                    record.exhibit_label,
                    json.dumps(record.referenced_schedule_codes),
                    json.dumps(record.referenced_rider_codes),
                    json.dumps(record.referenced_leaf_nos),
                    json.dumps(record.family_keys),
                    record.discovered_url,
                    record.viewer_url,
                    record.attachment_url,
                    record.download_url,
                    record.acquisition_method.value,
                    record.fetch_status.value,
                    record.local_path,
                    record.content_hash,
                    record.content_type,
                    record.file_size_bytes,
                    json.dumps(record.provenance_notes),
                    record.search_query,
                    record.page_title,
                    record.error_detail,
                    record.metadata_json,
                    record.doc_quality_tier,
                    record.search_confidence_score,
                    record.search_ideality,
                    created_at,
                    record.fetched_at.isoformat() if record.fetched_at else None,
                ),
            )
            return int(cursor.lastrowid)

    def get_ncuc_discovery_record(self, record_id: int) -> NcucDiscoveryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ncuc_discovery_records WHERE id = ?",
                (record_id,),
            ).fetchone()
        return self._row_to_ncuc_discovery_record(row) if row else None

    def list_ncuc_discovery_records(
        self,
        *,
        docket_number: str | None = None,
        fetch_status: str | None = None,
        family_key: str | None = None,
    ) -> list[NcucDiscoveryRecord]:
        query = "SELECT * FROM ncuc_discovery_records"
        clauses: list[str] = []
        params: list[object] = []
        if docket_number:
            clauses.append("docket_number = ?")
            params.append(docket_number)
        if fetch_status:
            clauses.append("fetch_status = ?")
            params.append(fetch_status)
        if family_key:
            clauses.append("family_keys_json LIKE ?")
            params.append(f"%{family_key}%")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY COALESCE(filing_date, '') DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_ncuc_discovery_record(row) for row in rows]

    def list_ncuc_pending_imports(self) -> list[NcucDiscoveryRecord]:
        """Return SUCCESS-fetched discovery records that have NOT yet been imported
        (no rows in ncuc_span_artifacts). Used by the loop drain to avoid
        re-processing the full 4K-record SUCCESS backlog every cycle."""
        query = (
            "SELECT r.* FROM ncuc_discovery_records r "
            "WHERE r.fetch_status = ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM ncuc_span_artifacts a "
            "  WHERE a.discovery_record_id = r.id"
            ") "
            "ORDER BY COALESCE(r.filing_date, '') DESC, r.id DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(query, (NcucFetchStatus.SUCCESS.value,)).fetchall()
        return [self._row_to_ncuc_discovery_record(row) for row in rows]

    def mark_ncuc_fetch_status(
        self,
        record_id: int,
        *,
        status: NcucFetchStatus,
        local_path: str | None = None,
        content_hash: str | None = None,
        content_type: str | None = None,
        file_size_bytes: int | None = None,
        error_detail: str | None = None,
        fetched_at: datetime | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE ncuc_discovery_records
                SET fetch_status = ?, local_path = ?, content_hash = ?,
                    content_type = ?, file_size_bytes = ?, error_detail = ?,
                    fetched_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    local_path,
                    content_hash,
                    content_type,
                    file_size_bytes,
                    error_detail,
                    (fetched_at or datetime.now(UTC)).isoformat(),
                    record_id,
                ),
            )

    def record_missing_doc_remediation_run(
        self,
        *,
        family_key: str | None,
        selected_reason: str | None,
        selected_scope: str | None,
        selected_weighted_score: float | None,
        executed: bool,
        before_step_count: int,
        after_step_count: int,
        before_deferred_discovery_count: int,
        before_deferred_historical_count: int,
        after_deferred_discovery_count: int,
        after_deferred_historical_count: int,
        requested_by: str,
        metadata: dict[str, object] | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO missing_doc_remediation_runs (
                    family_key, selected_reason, selected_scope, selected_weighted_score,
                    executed, before_step_count, after_step_count,
                    before_deferred_discovery_count, before_deferred_historical_count,
                    after_deferred_discovery_count, after_deferred_historical_count,
                    requested_by, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    family_key,
                    selected_reason,
                    selected_scope,
                    selected_weighted_score,
                    1 if executed else 0,
                    before_step_count,
                    after_step_count,
                    before_deferred_discovery_count,
                    before_deferred_historical_count,
                    after_deferred_discovery_count,
                    after_deferred_historical_count,
                    requested_by,
                    json.dumps(metadata or {}, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def list_missing_doc_remediation_runs(
        self,
        *,
        family_key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        query = "SELECT * FROM missing_doc_remediation_runs"
        params: list[object] = []
        if family_key:
            query += " WHERE family_key = ?"
            params.append(family_key)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                **dict(row),
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
            for row in rows
        ]

    @staticmethod
    def _row_to_ncuc_discovery_record(row) -> NcucDiscoveryRecord:
        from duke_rates.models.ncuc import NcucAcquisitionMethod, NcucFilingClassification

        acquisition_method_raw = str(row["acquisition_method"] or "").strip().lower()
        if acquisition_method_raw == "portal_harvest":
            acquisition_method = NcucAcquisitionMethod.PLAYWRIGHT
        else:
            acquisition_method = NcucAcquisitionMethod(acquisition_method_raw or "manual_seed")

        return NcucDiscoveryRecord(
            id=int(row["id"]),
            docket_number=row["docket_number"],
            sub_number=row["sub_number"],
            utility=row["utility"],
            filing_title=row["filing_title"],
            filing_date=row["filing_date"],
            proceeding_type=row["proceeding_type"],
            filing_classification=NcucFilingClassification(row["filing_classification"]),
            exhibit_label=row["exhibit_label"],
            referenced_schedule_codes=json.loads(row["referenced_schedule_codes_json"]),
            referenced_rider_codes=json.loads(row["referenced_rider_codes_json"]),
            referenced_leaf_nos=json.loads(row["referenced_leaf_nos_json"]),
            family_keys=json.loads(row["family_keys_json"]),
            discovered_url=row["discovered_url"],
            viewer_url=row["viewer_url"],
            attachment_url=row["attachment_url"],
            download_url=row["download_url"],
            acquisition_method=acquisition_method,
            fetch_status=NcucFetchStatus(row["fetch_status"]),
            local_path=row["local_path"],
            content_hash=row["content_hash"],
            content_type=row["content_type"],
            file_size_bytes=row["file_size_bytes"],
            provenance_notes=Repository._decode_provenance_notes(row["provenance_notes_json"]),
            search_query=row["search_query"],
            page_title=row["page_title"],
            error_detail=row["error_detail"],
            metadata_json=row["metadata_json"],
            doc_quality_tier=row["doc_quality_tier"] if "doc_quality_tier" in row.keys() else None,
            search_confidence_score=row["search_confidence_score"] if "search_confidence_score" in row.keys() else None,
            search_ideality=row["search_ideality"] if "search_ideality" in row.keys() else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            fetched_at=datetime.fromisoformat(row["fetched_at"]) if row["fetched_at"] else None,
        )

    @staticmethod
    def _decode_provenance_notes(payload: str | None) -> list[str]:
        if not payload:
            return []
        try:
            parsed = json.loads(payload)
        except Exception:
            return [payload]
        if isinstance(parsed, list):
            notes: list[str] = []
            for item in parsed:
                if isinstance(item, str):
                    notes.append(item)
                else:
                    notes.append(json.dumps(item, sort_keys=True))
            return notes
        if isinstance(parsed, dict):
            return [json.dumps(parsed, sort_keys=True)]
        return [str(parsed)]

    # ------------------------------------------------------------------
    # Versioned tariff data model
    # ------------------------------------------------------------------

    def upsert_tariff_family(self, record: "TariffFamilyRecord") -> str:
        """Insert or update a tariff family. Returns family_key."""
        from duke_rates.models.tariff import TariffFamilyRecord  # noqa: F401
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tariff_families (
                    family_key, state, company, tariff_identifier, schedule_code,
                    family_type, title, aliases_json, current_document_id, notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(family_key) DO UPDATE SET
                    tariff_identifier = excluded.tariff_identifier,
                    schedule_code = excluded.schedule_code,
                    family_type = excluded.family_type,
                    title = excluded.title,
                    aliases_json = excluded.aliases_json,
                    current_document_id = excluded.current_document_id,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    record.family_key,
                    record.state,
                    record.company,
                    record.tariff_identifier,
                    record.schedule_code,
                    record.family_type,
                    record.title,
                    json.dumps(record.aliases, sort_keys=True),
                    record.current_document_id,
                    record.notes,
                    now,
                    now,
                ),
            )
        return record.family_key

    def get_tariff_family(self, family_key: str) -> "TariffFamilyRecord | None":
        from duke_rates.models.tariff import TariffFamilyRecord
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tariff_families WHERE family_key = ?", (family_key,)
            ).fetchone()
            return self._row_to_tariff_family(row) if row else None

    def list_tariff_families(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
        family_type: str | None = None,
    ) -> list["TariffFamilyRecord"]:
        from duke_rates.models.tariff import TariffFamilyRecord  # noqa: F401
        query = "SELECT * FROM tariff_families"
        clauses: list[str] = []
        params: list[object] = []
        if state:
            clauses.append("state = ?")
            params.append(state.upper())
        if company:
            clauses.append("LOWER(company) = ?")
            params.append(company.lower())
        if family_type:
            clauses.append("family_type = ?")
            params.append(family_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY state, company, tariff_identifier"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_tariff_family(r) for r in rows]

    def list_provisional_tariff_families(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
    ) -> list["TariffFamilyRecord"]:
        from duke_rates.models.tariff import TariffFamilyRecord  # noqa: F401

        query = "SELECT * FROM tariff_families WHERE notes LIKE ?"
        params: list[object] = ["Provisional historical family%"]
        if state:
            query += " AND state = ?"
            params.append(state.upper())
        if company:
            query += " AND LOWER(company) = ?"
            params.append(company.lower())
        query += " ORDER BY state, company, tariff_identifier"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_tariff_family(r) for r in rows]

    def audit_legacy_ncuc_data_issues(self) -> dict[str, object]:
        with self._connect() as conn:
            legacy_method_rows = conn.execute(
                """
                SELECT id, docket_number, filing_title, acquisition_method
                FROM ncuc_discovery_records
                WHERE LOWER(COALESCE(acquisition_method, '')) = 'portal_harvest'
                ORDER BY id
                """
            ).fetchall()
            malformed_historical_rows = conn.execute(
                """
                SELECT id, family_key, current_document_id, local_path
                FROM historical_documents
                WHERE current_document_id IS NOT NULL
                  AND TRIM(CAST(current_document_id AS TEXT)) <> ''
                  AND CAST(current_document_id AS TEXT) GLOB '*[^0-9]*'
                ORDER BY id
                """
            ).fetchall()

        return {
            "legacy_portal_harvest_count": len(legacy_method_rows),
            "legacy_portal_harvest_rows": [dict(row) for row in legacy_method_rows[:25]],
            "malformed_historical_current_document_id_count": len(malformed_historical_rows),
            "malformed_historical_current_document_id_rows": [
                dict(row) for row in malformed_historical_rows[:25]
            ],
        }

    def repair_legacy_ncuc_data_issues(self, *, dry_run: bool = True) -> dict[str, object]:
        report = self.audit_legacy_ncuc_data_issues()
        if dry_run:
            report["updated_legacy_portal_harvest_count"] = 0
            report["cleared_historical_current_document_id_count"] = 0
            return report

        with self._connect() as conn:
            portal_result = conn.execute(
                """
                UPDATE ncuc_discovery_records
                SET acquisition_method = 'playwright'
                WHERE LOWER(COALESCE(acquisition_method, '')) = 'portal_harvest'
                """
            )
            historical_result = conn.execute(
                """
                UPDATE historical_documents
                SET current_document_id = NULL
                WHERE current_document_id IS NOT NULL
                  AND TRIM(CAST(current_document_id AS TEXT)) <> ''
                  AND CAST(current_document_id AS TEXT) GLOB '*[^0-9]*'
                """
            )
            conn.commit()

        report["updated_legacy_portal_harvest_count"] = int(portal_result.rowcount or 0)
        report["cleared_historical_current_document_id_count"] = int(historical_result.rowcount or 0)
        return report

    def score_provisional_tariff_families(
        self,
        *,
        state: str = "NC",
        company: str | None = None,
        family_key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        query = """
            SELECT
                tf.family_key,
                tf.state,
                tf.company,
                tf.tariff_identifier,
                tf.schedule_code,
                tf.family_type,
                tf.title,
                tf.notes,
                COUNT(DISTINCT hd.id) AS historical_document_count,
                COUNT(DISTINCT tv.id) AS version_count,
                COUNT(tc.id) AS charge_count,
                COUNT(DISTINCT CASE WHEN COALESCE(tc.charge_label, '') <> '' THEN tc.charge_label END) AS distinct_charge_labels,
                SUM(CASE WHEN tc.rate_value IS NULL THEN 1 ELSE 0 END) AS null_rate_count,
                AVG(COALESCE(tc.confidence_score, 0.0)) AS avg_charge_confidence
            FROM tariff_families tf
            LEFT JOIN historical_documents hd ON hd.family_key = tf.family_key
            LEFT JOIN tariff_versions tv ON tv.family_key = tf.family_key
            LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
            WHERE tf.notes LIKE 'Provisional historical family%'
              AND tf.state = ?
        """
        params: list[object] = [state.upper()]
        if company:
            query += " AND LOWER(tf.company) = ?"
            params.append(company.lower())
        if family_key:
            query += " AND tf.family_key = ?"
            params.append(family_key)
        query += """
            GROUP BY
                tf.family_key, tf.state, tf.company, tf.tariff_identifier,
                tf.schedule_code, tf.family_type, tf.title, tf.notes
            ORDER BY tf.family_key
        """

        with self._connect() as conn:
            families = conn.execute(query, params).fetchall()
            scored_rows: list[dict[str, object]] = []
            for row in families:
                key = str(row["family_key"])
                history_rows = conn.execute(
                    """
                    SELECT title, start_page, end_page, effective_start
                    FROM historical_documents
                    WHERE family_key = ?
                    ORDER BY COALESCE(effective_start, ''), id
                    """,
                    (key,),
                ).fetchall()

                score = 0
                reasons: list[str] = []
                family_title = str(row["title"] or "")
                normalized_key = re.sub(r"[^A-Z0-9]+", "", key.upper())
                key_length = len(key)
                charge_count = int(row["charge_count"] or 0)
                distinct_charge_labels = int(row["distinct_charge_labels"] or 0)
                null_rate_count = int(row["null_rate_count"] or 0)
                avg_charge_confidence = float(row["avg_charge_confidence"] or 0.0)
                charge_quality = 1.0
                if charge_count > 0:
                    charge_quality = max(
                        0.0,
                        min(
                            1.0,
                            (
                                avg_charge_confidence * 0.55
                                + min(1.0, distinct_charge_labels / max(charge_count, 1)) * 0.20
                                + min(1.0, charge_count / 6.0) * 0.15
                                + (1.0 - (null_rate_count / charge_count)) * 0.10
                            ),
                        ),
                    )

                if key_length >= 36:
                    score += min(20, key_length - 35)
                    reasons.append(f"long_key:{key_length}")
                if any(fragment in normalized_key for fragment in _GENERIC_PROVISIONAL_KEY_FRAGMENTS):
                    score += 35
                    reasons.append("generic_family_key")
                if _looks_generic_provisional_title(family_title):
                    score += 30
                    reasons.append("generic_title")
                if _looks_fragmentary_provisional_title(family_title):
                    score += 10
                    reasons.append("span_fragment_title")
                if charge_count <= 2:
                    score += 18
                    reasons.append(f"low_charge_count:{charge_count}")
                elif charge_count <= 5:
                    score += 8
                    reasons.append(f"thin_charge_count:{charge_count}")
                if distinct_charge_labels <= 1 and charge_count > 0:
                    score += 10
                    reasons.append(f"single_charge_label:{distinct_charge_labels}")
                if charge_count > 0 and (null_rate_count / charge_count) >= 0.5:
                    score += 10
                    reasons.append("many_null_rates")
                if charge_count > 0 and avg_charge_confidence < 0.45:
                    score += 15
                    reasons.append(f"low_charge_confidence:{avg_charge_confidence:.2f}")
                elif charge_count > 0 and avg_charge_confidence < 0.70:
                    score += 6
                    reasons.append(f"mid_charge_confidence:{avg_charge_confidence:.2f}")

                history_titles = [str(item["title"] or "") for item in history_rows]
                generic_history_titles = [title for title in history_titles if _looks_generic_provisional_title(title)]
                fragment_history_titles = [title for title in history_titles if _looks_fragmentary_provisional_title(title)]
                if generic_history_titles:
                    score += min(20, len(generic_history_titles) * 6)
                    reasons.append(f"generic_history_titles:{len(generic_history_titles)}")
                if fragment_history_titles:
                    score += min(12, len(fragment_history_titles) * 4)
                    reasons.append(f"fragment_history_titles:{len(fragment_history_titles)}")

                suggested_title = family_title if family_title and not _looks_generic_provisional_title(family_title) else None
                if suggested_title is None:
                    for title_candidate in history_titles:
                        cleaned_title = _strip_provisional_span_suffix(title_candidate)
                        if cleaned_title and not _looks_generic_provisional_title(cleaned_title):
                            suggested_title = cleaned_title
                            break
                suggested_schedule_code = row["schedule_code"]
                if suggested_schedule_code:
                    suggested_schedule_code = str(suggested_schedule_code).upper()
                if not suggested_schedule_code or _looks_generic_provisional_title(suggested_schedule_code):
                    suggested_schedule_code = _extract_schedule_code_candidate(suggested_title or family_title)
                suggested_family_type = str(row["family_type"] or "") or None
                if suggested_family_type == "doc":
                    suggested_family_type = None
                suggested_family_type = _infer_family_type_from_title(
                    suggested_title or family_title,
                    fallback=suggested_family_type or str(row["family_type"] or "") or None,
                )
                if not suggested_family_type:
                    suggested_family_type = str(row["family_type"] or "rate_schedule")

                review_band = "high" if score >= 60 else "medium" if score >= 30 else "low"
                recommended_action = "review_cleanup" if score >= 60 else "review_then_promote" if score >= 30 else "promote_if_lineage_checks_pass"
                promotion_command = None
                if suggested_title and suggested_schedule_code and suggested_family_type:
                    promotion_command = (
                        "python -m duke_rates lineage promote-provisional-family "
                        f"{key} --title \"{suggested_title}\" "
                        f"--schedule-code {suggested_schedule_code} "
                        f"--family-type {suggested_family_type}"
                    )

                scored_rows.append(
                    {
                        "family_key": key,
                        "state": row["state"],
                        "company": row["company"],
                        "tariff_identifier": row["tariff_identifier"],
                        "schedule_code": row["schedule_code"],
                        "family_type": row["family_type"],
                        "title": row["title"],
                        "historical_document_count": int(row["historical_document_count"] or 0),
                        "version_count": int(row["version_count"] or 0),
                        "charge_count": charge_count,
                        "distinct_charge_labels": distinct_charge_labels,
                        "null_rate_count": null_rate_count,
                        "avg_charge_confidence": round(avg_charge_confidence, 3),
                        "charge_quality_score": round(charge_quality, 3),
                        "review_score": score,
                        "review_band": review_band,
                        "recommended_action": recommended_action,
                        "review_reasons": reasons,
                        "suggested_title": suggested_title,
                        "suggested_schedule_code": suggested_schedule_code,
                        "suggested_family_type": suggested_family_type,
                        "promotion_command": promotion_command,
                    }
                )

        scored_rows.sort(
            key=lambda item: (
                -int(item["review_score"]),
                -int(item["charge_count"]),
                str(item["family_key"]),
            )
        )
        return scored_rows[:limit]

    def migrate_historical_family_lineage(
        self,
        source_family_key: str,
        target_family_key: str,
        *,
        historical_document_ids: list[int],
        title: str,
        schedule_code: str | None = None,
        family_type: str | None = None,
        tariff_identifier: str | None = None,
        aliases: list[str] | None = None,
        notes: str | None = None,
    ) -> "TariffFamilyRecord | None":
        from duke_rates.models.tariff import TariffFamilyRecord

        source_family = self.get_tariff_family(source_family_key)
        if source_family is None:
            return None
        if not historical_document_ids:
            raise ValueError("At least one historical_document_id is required")

        target_family = self.get_tariff_family(target_family_key)
        merged_aliases = list(
            dict.fromkeys(
                [
                    *(target_family.aliases if target_family else []),
                    *(aliases or []),
                    source_family.title or "",
                    title,
                ]
            )
        )
        merged_aliases = [alias for alias in merged_aliases if alias]
        target_record = TariffFamilyRecord(
            family_key=target_family_key,
            state=source_family.state,
            company=source_family.company,
            tariff_identifier=tariff_identifier,
            schedule_code=schedule_code,
            family_type=family_type or source_family.family_type,
            title=title,
            aliases=merged_aliases,
            current_document_id=None,
            notes=notes,
        )
        self.upsert_tariff_family(target_record)

        placeholders = ",".join("?" for _ in historical_document_ids)
        params: list[object] = [target_family_key, *historical_document_ids, source_family_key]
        with self._connect() as conn:
            moved_rows = conn.execute(
                f"""
                SELECT id, local_path
                FROM historical_documents
                WHERE id IN ({placeholders}) AND family_key = ?
                """,
                [*historical_document_ids, source_family_key],
            ).fetchall()
            if len(moved_rows) != len(historical_document_ids):
                raise ValueError("One or more historical documents do not belong to the source family")

            moved_paths = [str(row["local_path"]) for row in moved_rows if row["local_path"]]

            conn.execute(
                f"""
                UPDATE historical_documents
                SET family_key = ?, current_document_id = NULL
                WHERE id IN ({placeholders}) AND family_key = ?
                """,
                params,
            )
            conn.execute(
                f"""
                UPDATE tariff_versions
                SET family_key = ?
                WHERE historical_document_id IN ({placeholders}) AND family_key = ?
                """,
                params,
            )
            version_rows = conn.execute(
                f"""
                SELECT id
                FROM tariff_versions
                WHERE historical_document_id IN ({placeholders}) AND family_key = ?
                """,
                [*historical_document_ids, target_family_key],
            ).fetchall()
            version_ids = [int(row["id"]) for row in version_rows]
            if version_ids:
                version_placeholders = ",".join("?" for _ in version_ids)
                conn.execute(
                    f"""
                    UPDATE tariff_charges
                    SET family_key = ?
                    WHERE version_id IN ({version_placeholders})
                    """,
                    [target_family_key, *version_ids],
                )
            conn.execute(
                f"""
                UPDATE historical_processing_runs
                SET family_key = ?
                WHERE historical_document_id IN ({placeholders}) AND family_key = ?
                """,
                params,
            )
            conn.execute(
                f"""
                UPDATE historical_reprocess_queue
                SET family_key = ?
                WHERE historical_document_id IN ({placeholders}) AND family_key = ?
                """,
                params,
            )

            if moved_paths:
                path_placeholders = ",".join("?" for _ in moved_paths)
                conn.execute(
                    f"""
                    UPDATE parse_attempt_logs
                    SET metadata_json = json_set(metadata_json, '$.family_key', ?)
                    WHERE source_pdf IN ({path_placeholders})
                      AND json_extract(metadata_json, '$.family_key') = ?
                    """,
                    [target_family_key, *moved_paths, source_family_key],
                )
                conn.execute(
                    f"""
                    UPDATE document_fingerprints
                    SET metadata_json = json_set(metadata_json, '$.family_key', ?)
                    WHERE source_pdf IN ({path_placeholders})
                      AND json_extract(metadata_json, '$.family_key') = ?
                    """,
                    [target_family_key, *moved_paths, source_family_key],
                )
                conn.execute(
                    f"""
                    UPDATE parse_review_outcomes
                    SET notes_json = json_set(notes_json, '$.family_key', ?)
                    WHERE source_pdf IN ({path_placeholders})
                      AND json_extract(notes_json, '$.family_key') = ?
                    """,
                    [target_family_key, *moved_paths, source_family_key],
                )

        return self.get_tariff_family(target_family_key)

    def canonicalize_historical_family_key(
        self,
        source_family_key: str,
        target_family_key: str,
        *,
        historical_document_ids: list[int] | None = None,
        title: str | None = None,
        schedule_code: str | None = None,
        family_type: str | None = None,
        tariff_identifier: str | None = None,
        aliases: list[str] | None = None,
        notes: str | None = None,
        prune_source_family: bool = True,
        move_discovery_metadata: bool = True,
    ) -> dict[str, object] | None:
        """Move malformed historical family content into a canonical family key.

        Unlike ``migrate_historical_family_lineage``, this helper is intended for
        operator-driven cleanup where the target family may already exist as the
        canonical current/historical lineage. It can move all source historical
        documents in one step, update ancillary lineage tables, and optionally
        prune the now-empty source family row.
        """
        source_family = self.get_tariff_family(source_family_key)
        if source_family is None:
            return None

        with self._connect() as conn:
            if historical_document_ids:
                moved_ids = sorted({int(item) for item in historical_document_ids})
            else:
                moved_ids = [
                    int(row["id"])
                    for row in conn.execute(
                        """
                        SELECT id
                        FROM historical_documents
                        WHERE family_key = ?
                        ORDER BY COALESCE(effective_start, ''), id
                        """,
                        (source_family_key,),
                    ).fetchall()
                ]

        target_family = self.get_tariff_family(target_family_key)
        resolved_title = title or (target_family.title if target_family else None) or source_family.title
        if not resolved_title:
            raise ValueError("A title is required when the target family does not already exist")

        resolved_schedule_code = schedule_code or (target_family.schedule_code if target_family else None) or source_family.schedule_code
        resolved_family_type = family_type or (target_family.family_type if target_family else None) or source_family.family_type
        resolved_tariff_identifier = tariff_identifier or (target_family.tariff_identifier if target_family else None) or source_family.tariff_identifier
        merged_notes = notes or (target_family.notes if target_family else None) or source_family.notes
        merged_aliases = list(
            dict.fromkeys(
                [
                    *(target_family.aliases if target_family else []),
                    *(aliases or []),
                    source_family.title or "",
                    resolved_title,
                ]
            )
        )
        merged_aliases = [alias for alias in merged_aliases if alias]

        if moved_ids:
            family = self.migrate_historical_family_lineage(
                source_family_key,
                target_family_key,
                historical_document_ids=moved_ids,
                title=resolved_title,
                schedule_code=resolved_schedule_code,
                family_type=resolved_family_type,
                tariff_identifier=resolved_tariff_identifier,
                aliases=merged_aliases,
                notes=merged_notes,
            )
            if family is None:
                return None
        else:
            from duke_rates.models.tariff import TariffFamilyRecord

            self.upsert_tariff_family(
                TariffFamilyRecord(
                    family_key=target_family_key,
                    state=source_family.state,
                    company=source_family.company,
                    tariff_identifier=resolved_tariff_identifier,
                    schedule_code=resolved_schedule_code,
                    family_type=resolved_family_type,
                    title=resolved_title,
                    aliases=merged_aliases,
                    current_document_id=target_family.current_document_id if target_family else None,
                    notes=merged_notes,
                )
            )
            family = self.get_tariff_family(target_family_key)

        with self._connect() as conn:
            orphaned_versions = conn.execute(
                """
                SELECT tv.id, tv.historical_document_id, hd.family_key AS historical_family_key
                FROM tariff_versions tv
                JOIN historical_documents hd
                  ON hd.id = tv.historical_document_id
                WHERE tv.family_key = ?
                  AND hd.family_key <> ?
                """,
                (source_family_key, source_family_key),
            ).fetchall()
            for row in orphaned_versions:
                orphan_target = str(row["historical_family_key"])
                version_id = int(row["id"])
                conn.execute(
                    "UPDATE tariff_versions SET family_key = ? WHERE id = ?",
                    (orphan_target, version_id),
                )
                conn.execute(
                    "UPDATE tariff_charges SET family_key = ? WHERE version_id = ?",
                    (orphan_target, version_id),
                )

            if source_family.current_document_id and not (target_family and target_family.current_document_id):
                conn.execute(
                    """
                    UPDATE tariff_families
                    SET current_document_id = ?, updated_at = ?
                    WHERE family_key = ?
                    """,
                    (
                        source_family.current_document_id,
                        datetime.now(UTC).isoformat(),
                        target_family_key,
                    ),
                )
                conn.execute(
                    """
                    UPDATE tariff_families
                    SET current_document_id = NULL, updated_at = ?
                    WHERE family_key = ?
                    """,
                    (
                        datetime.now(UTC).isoformat(),
                        source_family_key,
                    ),
                )

            conn.execute(
                """
                UPDATE tariff_versions
                SET family_key = ?
                WHERE family_key = ?
                  AND historical_document_id IS NULL
                """,
                (target_family_key, source_family_key),
            )
            conn.execute(
                """
                UPDATE tariff_charges
                SET family_key = ?
                WHERE family_key = ?
                  AND version_id IN (
                    SELECT id
                    FROM tariff_versions
                    WHERE family_key = ?
                      AND historical_document_id IS NULL
                  )
                """,
                (target_family_key, source_family_key, target_family_key),
            )

            conn.execute(
                "UPDATE historical_leads SET family_key = ? WHERE family_key = ?",
                (target_family_key, source_family_key),
            )
            conn.execute(
                "UPDATE candidate_url_variants SET family_key = ? WHERE family_key = ?",
                (target_family_key, source_family_key),
            )
            conn.execute(
                "UPDATE regulatory_docket_leads SET family_key = ? WHERE family_key = ?",
                (target_family_key, source_family_key),
            )
            conn.execute(
                "UPDATE evidence_anchors SET family_key = ? WHERE family_key = ?",
                (target_family_key, source_family_key),
            )
            conn.execute(
                "UPDATE rider_applicability SET rider_family_key = ? WHERE rider_family_key = ?",
                (target_family_key, source_family_key),
            )
            conn.execute(
                "UPDATE rider_applicability SET applies_to_family_key = ? WHERE applies_to_family_key = ?",
                (target_family_key, source_family_key),
            )

            source_pack = conn.execute(
                "SELECT id FROM historical_search_packs WHERE family_key = ?",
                (source_family_key,),
            ).fetchone()
            target_pack = conn.execute(
                "SELECT id FROM historical_search_packs WHERE family_key = ?",
                (target_family_key,),
            ).fetchone()
            if source_pack:
                if target_pack:
                    conn.execute(
                        "DELETE FROM historical_search_packs WHERE family_key = ?",
                        (source_family_key,),
                    )
                else:
                    conn.execute(
                        "UPDATE historical_search_packs SET family_key = ? WHERE family_key = ?",
                        (target_family_key, source_family_key),
                    )

            if move_discovery_metadata:
                discovery_rows = conn.execute(
                    """
                    SELECT id, family_keys_json
                    FROM ncuc_discovery_records
                    WHERE family_keys_json LIKE ?
                    """,
                    (f"%{source_family_key}%",),
                ).fetchall()
                for row in discovery_rows:
                    try:
                        family_keys = json.loads(row["family_keys_json"] or "[]")
                    except json.JSONDecodeError:
                        continue
                    if source_family_key not in family_keys:
                        continue
                    normalized = [
                        target_family_key if item == source_family_key else item
                        for item in family_keys
                    ]
                    normalized = list(dict.fromkeys(normalized))
                    conn.execute(
                        """
                        UPDATE ncuc_discovery_records
                        SET family_keys_json = ?
                        WHERE id = ?
                        """,
                        (json.dumps(normalized), row["id"]),
                    )

            source_usage = {
                "historical_documents": conn.execute(
                    "SELECT COUNT(*) FROM historical_documents WHERE family_key = ?",
                    (source_family_key,),
                ).fetchone()[0],
                "tariff_versions": conn.execute(
                    "SELECT COUNT(*) FROM tariff_versions WHERE family_key = ?",
                    (source_family_key,),
                ).fetchone()[0],
                "current_document_anchor": conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM tariff_families
                    WHERE family_key = ? AND current_document_id IS NOT NULL
                    """,
                    (source_family_key,),
                ).fetchone()[0],
                "rider_applicability": conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM rider_applicability
                    WHERE rider_family_key = ? OR applies_to_family_key = ?
                    """,
                    (source_family_key, source_family_key),
                ).fetchone()[0],
                "historical_leads": conn.execute(
                    "SELECT COUNT(*) FROM historical_leads WHERE family_key = ?",
                    (source_family_key,),
                ).fetchone()[0],
                "candidate_url_variants": conn.execute(
                    "SELECT COUNT(*) FROM candidate_url_variants WHERE family_key = ?",
                    (source_family_key,),
                ).fetchone()[0],
                "historical_search_packs": conn.execute(
                    "SELECT COUNT(*) FROM historical_search_packs WHERE family_key = ?",
                    (source_family_key,),
                ).fetchone()[0],
                "regulatory_docket_leads": conn.execute(
                    "SELECT COUNT(*) FROM regulatory_docket_leads WHERE family_key = ?",
                    (source_family_key,),
                ).fetchone()[0],
                "evidence_anchors": conn.execute(
                    "SELECT COUNT(*) FROM evidence_anchors WHERE family_key = ?",
                    (source_family_key,),
                ).fetchone()[0],
            }

            source_pruned = False
            if prune_source_family and not any(source_usage.values()):
                conn.execute(
                    "DELETE FROM tariff_families WHERE family_key = ?",
                    (source_family_key,),
                )
                source_pruned = True

        return {
            "family": self.get_tariff_family(target_family_key),
            "moved_historical_document_ids": moved_ids,
            "source_family_pruned": source_pruned,
            "source_usage": source_usage,
        }

    def promote_provisional_tariff_family(
        self,
        family_key: str,
        *,
        title: str | None = None,
        schedule_code: str | None = None,
        family_type: str | None = None,
        aliases: list[str] | None = None,
        notes: str | None = None,
        current_document_id: int | None = None,
    ) -> "TariffFamilyRecord | None":
        existing = self.get_tariff_family(family_key)
        if existing is None:
            return None

        merged_aliases = list(existing.aliases)
        if aliases:
            for alias in aliases:
                cleaned = (alias or "").strip()
                if cleaned and cleaned not in merged_aliases:
                    merged_aliases.append(cleaned)

        promoted_notes = notes
        if promoted_notes is None:
            if (existing.notes or "").startswith("Provisional historical family"):
                promoted_notes = "Promoted from provisional historical family."
            else:
                promoted_notes = existing.notes

        record = existing.model_copy(
            update={
                "title": title or existing.title,
                "schedule_code": schedule_code or existing.schedule_code,
                "family_type": family_type or existing.family_type,
                "aliases": merged_aliases,
                "notes": promoted_notes,
                "current_document_id": (
                    current_document_id
                    if current_document_id is not None
                    else existing.current_document_id
                ),
            }
        )
        self.upsert_tariff_family(record)
        return self.get_tariff_family(family_key)

    def list_historical_only_tariff_families(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
        family_type: str | None = None,
    ) -> list[dict]:
        query = """
            SELECT
                tf.family_key,
                tf.title,
                tf.schedule_code,
                tf.family_type,
                tf.company,
                tf.current_document_id,
                tf.notes,
                COUNT(hd.id) AS historical_document_count,
                MIN(hd.snapshot_timestamp) AS first_snapshot_timestamp,
                MAX(hd.snapshot_timestamp) AS last_snapshot_timestamp
            FROM tariff_families tf
            JOIN historical_documents hd ON hd.family_key = tf.family_key
            WHERE tf.current_document_id IS NULL
        """
        params: list[object] = []
        if state:
            query += " AND tf.state = ?"
            params.append(state.upper())
        if company:
            query += " AND LOWER(tf.company) = ?"
            params.append(company.lower())
        if family_type:
            query += " AND tf.family_type = ?"
            params.append(family_type)
        query += """
            GROUP BY
                tf.family_key, tf.title, tf.schedule_code, tf.family_type,
                tf.company, tf.current_document_id, tf.notes
            ORDER BY historical_document_count DESC, tf.family_key
        """
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def review_historical_only_tariff_families(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
        family_type: str | None = None,
        limit_suggestions: int = 3,
    ) -> list[dict]:
        rows = self.list_historical_only_tariff_families(
            state=state,
            company=company,
            family_type=family_type,
        )
        review_rows: list[dict] = []
        for row in rows:
            suggestions = self.suggest_current_documents_for_family(
                row["family_key"],
                limit=limit_suggestions,
            )
            top_score = suggestions[0]["score"] if suggestions else None
            review_status = "review_candidates" if suggestions else "unresolved"
            review_rows.append(
                {
                    **row,
                    "review_status": review_status,
                    "candidate_count": len(suggestions),
                    "top_candidate_score": top_score,
                    "suggestions": suggestions,
                }
            )
        return review_rows

    def list_weak_unbounded_historical_documents(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
        family_key: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        query = """
            WITH latest AS (
                SELECT
                    pal.id,
                    pal.source_pdf,
                    pal.page_start,
                    pal.page_end,
                    json_extract(pal.metadata_json, '$.family_key') AS family_key,
                    pal.parser_profile,
                    pal.status,
                    pal.charge_count,
                    json_extract(pal.metadata_json, '$.outcome_quality') AS outcome_quality,
                    ROW_NUMBER() OVER (
                        PARTITION BY pal.source_pdf, pal.page_start, pal.page_end
                        ORDER BY pal.id DESC
                    ) AS rn
                FROM parse_attempt_logs pal
                WHERE pal.parser_stage = 'historical_bulk'
            )
            SELECT
                hd.id AS historical_document_id,
                hd.family_key,
                hd.state,
                hd.company,
                hd.title,
                hd.local_path,
                hd.effective_start,
                latest.parser_profile,
                latest.status,
                latest.charge_count,
                latest.outcome_quality,
                ndr.id AS discovery_record_id
            FROM latest
            JOIN historical_documents hd
              ON hd.local_path = latest.source_pdf
             AND COALESCE(hd.start_page, 1) = COALESCE(latest.page_start, 1)
             AND COALESCE(hd.end_page, COALESCE(hd.start_page, 1)) = COALESCE(latest.page_end, COALESCE(latest.page_start, 1))
             AND hd.family_key = latest.family_key
            LEFT JOIN ncuc_discovery_records ndr
              ON ndr.local_path = hd.local_path
            WHERE latest.rn = 1
              AND latest.outcome_quality = 'weak'
              AND hd.start_page IS NULL
        """
        params: list[object] = []
        if state:
            query += " AND hd.state = ?"
            params.append(state.upper())
        if company:
            query += " AND LOWER(hd.company) = ?"
            params.append(company.lower())
        if family_key:
            query += " AND hd.family_key = ?"
            params.append(family_key)
        query += """
            ORDER BY COALESCE(hd.effective_start, '') DESC, hd.family_key, hd.id DESC
            LIMIT ?
        """
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        reviewed: list[dict] = []
        for row in rows:
            local_path = str(row["local_path"] or "")
            source_kind = self._classify_unbounded_historical_source(local_path)
            inferred_discovery_record_id = self._infer_discovery_record_id_for_historical_document(
                historical_document_id=int(row["historical_document_id"])
            )
            discovery_record_id = row["discovery_record_id"] or inferred_discovery_record_id
            stale_current_snapshot = self._detect_stale_current_document_snapshot(
                historical_document_id=int(row["historical_document_id"])
            )
            bundle_reference_overlap = self._detect_legacy_bundle_reference_residue(
                historical_document_id=int(row["historical_document_id"]),
                discovery_record_id=discovery_record_id,
            )
            review_action = self._suggest_unbounded_historical_review_action(
                source_kind=source_kind,
                discovery_record_id=discovery_record_id,
                historical_document_id=int(row["historical_document_id"]),
                stale_current_snapshot=stale_current_snapshot,
                bundle_reference_overlap=bundle_reference_overlap,
            )
            reviewed.append(
                {
                    "historical_document_id": int(row["historical_document_id"]),
                    "family_key": row["family_key"],
                    "state": row["state"],
                    "company": row["company"],
                    "title": row["title"],
                    "local_path": local_path,
                    "effective_start": row["effective_start"],
                    "parser_profile": row["parser_profile"],
                    "status": row["status"],
                    "charge_count": int(row["charge_count"] or 0),
                    "outcome_quality": row["outcome_quality"],
                    "discovery_record_id": discovery_record_id,
                    "source_kind": source_kind,
                    "review_action": review_action,
                    "stale_current_snapshot": stale_current_snapshot,
                    "bundle_reference_overlap": bundle_reference_overlap,
                }
            )
        return reviewed

    def list_redundant_legacy_raw_historical_documents(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
        family_key: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        rows = self.list_weak_unbounded_historical_documents(
            state=state,
            company=company,
            family_key=family_key,
            limit=limit,
        )
        redundant: list[dict] = []
        with self._connect() as conn:
            for row in rows:
                if row["source_kind"] != "legacy_raw_attachment":
                    continue
                regulator_local_path = self._infer_regulator_local_file_for_historical_document(
                    historical_document_id=int(row["historical_document_id"])
                )
                if not regulator_local_path:
                    continue
                replacements = conn.execute(
                    """
                    SELECT id, title, start_page, end_page, effective_start
                    FROM historical_documents
                    WHERE family_key = ?
                      AND local_path = ?
                      AND start_page IS NOT NULL
                    ORDER BY id
                    """,
                    (row["family_key"], regulator_local_path),
                ).fetchall()
                if not replacements:
                    continue
                redundant.append(
                    {
                        **row,
                        "regulator_local_path": regulator_local_path,
                        "replacement_count": len(replacements),
                        "replacement_ids": [int(item["id"]) for item in replacements],
                    }
                )
        return redundant

    def list_bundle_reference_legacy_raw_historical_documents(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
        family_key: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        rows = self.list_weak_unbounded_historical_documents(
            state=state,
            company=company,
            family_key=family_key,
            limit=limit,
        )
        return [
            row
            for row in rows
            if row["review_action"] == "retire_bundle_reference_residue"
        ]

    def list_placeholder_heading_residue_historical_documents(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
        family_key: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        placeholders = ",".join("?" for _ in _PLACEHOLDER_HEADING_FAMILY_KEYS)
        query = f"""
            SELECT
                id AS historical_document_id,
                family_key,
                state,
                company,
                title,
                local_path,
                start_page,
                end_page
            FROM historical_documents
            WHERE family_key IN ({placeholders})
              AND start_page IS NOT NULL
        """
        params: list[object] = [*_PLACEHOLDER_HEADING_FAMILY_KEYS]
        if state:
            query += " AND state = ?"
            params.append(state.upper())
        if company:
            query += " AND LOWER(company) = ?"
            params.append(company.lower())
        if family_key:
            query += " AND family_key = ?"
            params.append(family_key)
        query += " ORDER BY local_path, start_page, end_page, id LIMIT ?"
        params.append(limit)

        rows: list[dict] = []
        with self._connect() as conn:
            candidates = conn.execute(query, params).fetchall()
            for candidate in candidates:
                overlap_start = max(1, int(candidate["start_page"]) - 2)
                overlap_end = int(candidate["end_page"]) + 2
                neighbors = conn.execute(
                    f"""
                    SELECT id, family_key, title, start_page, end_page
                    FROM historical_documents
                    WHERE local_path = ?
                      AND start_page IS NOT NULL
                      AND id != ?
                      AND family_key NOT IN ({placeholders})
                      AND NOT (
                        COALESCE(end_page, start_page) < ?
                        OR start_page > ?
                      )
                    ORDER BY start_page, end_page, id
                    """,
                    [
                        str(candidate["local_path"]),
                        int(candidate["historical_document_id"]),
                        *_PLACEHOLDER_HEADING_FAMILY_KEYS,
                        overlap_start,
                        overlap_end,
                    ],
                ).fetchall()
                if not neighbors:
                    continue
                rows.append(
                    {
                        "historical_document_id": int(candidate["historical_document_id"]),
                        "family_key": candidate["family_key"],
                        "state": candidate["state"],
                        "company": candidate["company"],
                        "title": candidate["title"],
                        "local_path": candidate["local_path"],
                        "start_page": int(candidate["start_page"]),
                        "end_page": int(candidate["end_page"]),
                        "review_action": "retire_placeholder_heading_residue",
                        "neighbor_count": len(neighbors),
                        "neighbors": [
                            {
                                "historical_document_id": int(neighbor["id"]),
                                "family_key": neighbor["family_key"],
                                "title": neighbor["title"],
                                "start_page": int(neighbor["start_page"]),
                                "end_page": int(neighbor["end_page"]),
                            }
                            for neighbor in neighbors
                        ],
                    }
                )
        return rows

    def retire_historical_document(self, historical_document_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, local_path, start_page, end_page
                FROM historical_documents
                WHERE id = ?
                """,
                (historical_document_id,),
            ).fetchone()
            if row is None:
                return False

            local_path = str(row["local_path"] or "")
            start_page = int(row["start_page"]) if row["start_page"] is not None else None
            end_page = int(row["end_page"]) if row["end_page"] is not None else None

            conn.execute(
                """
                DELETE FROM tariff_charges
                WHERE version_id IN (
                    SELECT id FROM tariff_versions WHERE historical_document_id = ?
                )
                """,
                (historical_document_id,),
            )
            conn.execute(
                "DELETE FROM tariff_versions WHERE historical_document_id = ?",
                (historical_document_id,),
            )
            conn.execute(
                "DELETE FROM historical_processing_runs WHERE historical_document_id = ?",
                (historical_document_id,),
            )
            conn.execute(
                "DELETE FROM historical_reprocess_queue WHERE historical_document_id = ?",
                (historical_document_id,),
            )

            if local_path:
                if start_page is None:
                    conn.execute(
                        "DELETE FROM parse_review_outcomes WHERE source_pdf = ?",
                        (local_path,),
                    )
                    conn.execute(
                        "DELETE FROM document_fingerprints WHERE source_pdf = ?",
                        (local_path,),
                    )
                    conn.execute(
                        "DELETE FROM parse_attempt_logs WHERE source_pdf = ?",
                        (local_path,),
                    )
                else:
                    page_end = end_page if end_page is not None else start_page
                    conn.execute(
                        """
                        DELETE FROM parse_review_outcomes
                        WHERE source_pdf = ?
                          AND COALESCE(page_start, 1) = ?
                          AND COALESCE(page_end, ?) = ?
                        """,
                        (local_path, start_page, start_page, page_end),
                    )
                    conn.execute(
                        """
                        DELETE FROM document_fingerprints
                        WHERE source_pdf = ?
                          AND COALESCE(page_start, 1) = ?
                          AND COALESCE(page_end, ?) = ?
                        """,
                        (local_path, start_page, start_page, page_end),
                    )
                    conn.execute(
                        """
                        DELETE FROM parse_attempt_logs
                        WHERE source_pdf = ?
                          AND COALESCE(page_start, 1) = ?
                          AND COALESCE(page_end, ?) = ?
                        """,
                        (local_path, start_page, start_page, page_end),
                    )

            conn.execute(
                "DELETE FROM historical_documents WHERE id = ?",
                (historical_document_id,),
            )
            conn.commit()
        return True

    def deduplicate_historical_documents(
        self,
        *,
        dry_run: bool = True,
        file_hash: str | None = None,
        limit: int = 0,
    ) -> dict[str, Any]:
        """Consolidate historical_documents that share the same scoped hash.

        A full-PDF content hash can be shared by distinct page spans from the
        same source PDF.  For each duplicate group with the same hash, family,
        start page, and end page, the best survivor is kept and all foreign-key
        references from the other rows are remapped to it.  Non-survivors are
        then deleted.

        Returns a summary dict suitable for CLI reporting.
        """
        import time as _time

        t0 = _time.monotonic()

        with self._connect() as conn:
            # ── 1. Find duplicate groups ──────────────────────────────────
            where = ""
            params: list[Any] = []
            if file_hash:
                where = "AND hd.content_hash = ?"
                params.append(file_hash)

            dup_rows = conn.execute(
                f"""
                SELECT
                    hd.content_hash,
                    hd.family_key,
                    COALESCE(hd.start_page, -1) AS start_page_scope,
                    COALESCE(hd.end_page, COALESCE(hd.start_page, -1)) AS end_page_scope,
                    COUNT(*)        AS cnt,
                    GROUP_CONCAT(hd.id) AS id_list
                FROM historical_documents hd
                WHERE hd.content_hash IS NOT NULL
                  AND hd.content_hash != ''
                {where}
                GROUP BY
                    hd.content_hash,
                    hd.family_key,
                    COALESCE(hd.start_page, -1),
                    COALESCE(hd.end_page, COALESCE(hd.start_page, -1))
                HAVING COUNT(*) > 1
                ORDER BY cnt DESC
                """,
                params,
            ).fetchall()

            if limit > 0:
                dup_rows = dup_rows[:limit]

            groups_processed = 0
            documents_removed = 0
            total_groups = len(dup_rows)
            per_group: list[dict[str, Any]] = []
            errors: list[str] = []

            for dr in dup_rows:
                content_hash_val = dr["content_hash"]
                family_key_val = dr["family_key"]
                start_page_scope = None if int(dr["start_page_scope"]) == -1 else int(dr["start_page_scope"])
                end_page_scope = None if int(dr["end_page_scope"]) == -1 else int(dr["end_page_scope"])
                id_str = dr["id_list"]
                if not id_str:
                    continue
                dup_ids = [int(x.strip()) for x in id_str.split(",") if x.strip()]
                if len(dup_ids) < 2:
                    continue

                # ── 2. Select survivor ────────────────────────────────────
                # Best = most charges, then has local_path, then newest, then lowest id
                survivor_id: int | None = None
                best_score = (-1, False, "", 999999999)

                for hd_id in dup_ids:
                    charge_count = int(
                        conn.execute(
                            """
                            SELECT COUNT(*)
                            FROM tariff_charges tc
                            JOIN tariff_versions tv ON tc.version_id = tv.id
                            WHERE tv.historical_document_id = ?
                            """,
                            (hd_id,),
                        ).fetchone()[0]
                        or 0
                    )
                    row_info = conn.execute(
                        "SELECT local_path, retrieved_at FROM historical_documents WHERE id = ?",
                        (hd_id,),
                    ).fetchone()
                    has_path = bool(row_info and row_info["local_path"])
                    retrieved = str(row_info["retrieved_at"] or "")

                    score = (charge_count, has_path, retrieved, -hd_id)
                    if score > best_score:
                        best_score = score
                        survivor_id = hd_id

                if survivor_id is None:
                    errors.append(f"Could not select survivor for content_hash={content_hash_val}")
                    continue

                non_survivors = [i for i in dup_ids if i != survivor_id]

                if dry_run:
                    per_group.append({
                        "content_hash": content_hash_val,
                        "family_key": family_key_val,
                        "start_page": start_page_scope,
                        "end_page": end_page_scope,
                        "survivor_id": survivor_id,
                        "survivor_charges": best_score[0],
                        "removed_ids": non_survivors,
                        "group_size": len(dup_ids),
                    })
                    documents_removed += len(non_survivors)
                    groups_processed += 1
                    continue

                # ── 3. Remap FKs & delete non-survivors ──────────────────
                try:
                    for dup_id in non_survivors:
                        # a. tariff_versions → survivor
                        conn.execute(
                            "UPDATE tariff_versions SET historical_document_id = ? WHERE historical_document_id = ?",
                            (survivor_id, dup_id),
                        )
                        # b. processing_runs — delete (don't remap; they're doc-specific runs)
                        conn.execute(
                            "DELETE FROM historical_processing_runs WHERE historical_document_id = ?",
                            (dup_id,),
                        )
                        # c. reprocess_queue — delete
                        conn.execute(
                            "DELETE FROM historical_reprocess_queue WHERE historical_document_id = ?",
                            (dup_id,),
                        )
                        # d. llm_candidate_rate_extractions
                        conn.execute(
                            "UPDATE llm_candidate_rate_extractions SET historical_document_id = ? WHERE historical_document_id = ?",
                            (survivor_id, dup_id),
                        )
                        # e. document_classifications (polymorphic) — delete rows
                        # that would collide with existing survivor classifications,
                        # then remap the rest.
                        conn.execute(
                            """DELETE FROM document_classifications
                               WHERE subject_kind = 'historical_document'
                                 AND subject_id = ?
                                 AND (stage, classifier, classifier_version) IN (
                                     SELECT stage, classifier, classifier_version
                                     FROM document_classifications
                                     WHERE subject_kind = 'historical_document'
                                       AND subject_id = ?
                                 )""",
                            (str(dup_id), str(survivor_id)),
                        )
                        conn.execute(
                            """UPDATE document_classifications
                               SET subject_id = ?
                               WHERE subject_kind = 'historical_document'
                                 AND subject_id = ?""",
                            (str(survivor_id), str(dup_id)),
                        )
                        # f. workflow_action_receipts
                        conn.execute(
                            "UPDATE workflow_action_receipts SET target_historical_document_id = ? WHERE target_historical_document_id = ?",
                            (survivor_id, dup_id),
                        )
                        # g. Delete the duplicate document row
                        conn.execute(
                            "DELETE FROM historical_documents WHERE id = ?",
                            (dup_id,),
                        )

                    conn.commit()
                    per_group.append({
                        "content_hash": content_hash_val,
                        "family_key": family_key_val,
                        "start_page": start_page_scope,
                        "end_page": end_page_scope,
                        "survivor_id": survivor_id,
                        "survivor_charges": best_score[0],
                        "removed_ids": non_survivors,
                        "group_size": len(dup_ids),
                    })
                    documents_removed += len(non_survivors)
                    groups_processed += 1
                except Exception as exc:
                    conn.rollback()
                    errors.append(f"content_hash={content_hash_val}: {exc}")

            duration_ms = int((_time.monotonic() - t0) * 1000)
            return {
                "dry_run": dry_run,
                "total_groups": total_groups,
                "groups_processed": groups_processed,
                "documents_removed": documents_removed,
                "per_group": per_group,
                "errors": errors,
                "duration_ms": duration_ms,
            }

    def populate_evidence_json_for_document(
        self, historical_document_id: int
    ) -> bool:
        """Populate ``evidence_json`` for a single document from its span artifacts.

        Called automatically by the reprocess pipeline after successful
        extraction.  Returns True if evidence_json was written.
        """
        import json as _json

        with self._connect() as conn:
            span_rows = conn.execute(
                """
                SELECT id, evidence_score_breakdown_json
                FROM ncuc_span_artifacts
                WHERE file_hash = (SELECT content_hash FROM historical_documents WHERE id = ?)
                  AND evidence_score_breakdown_json IS NOT NULL
                  AND evidence_score_breakdown_json != ''
                  AND evidence_score_breakdown_json != '{}'
                """,
                (historical_document_id,),
            ).fetchall()

            if not span_rows:
                return False

            best_breakdown: dict[str, Any] | None = None
            best_score: float = -999999.0

            for sr in span_rows:
                try:
                    breakdown_map = _json.loads(sr["evidence_score_breakdown_json"])
                except Exception:
                    continue

                if not isinstance(breakdown_map, dict):
                    continue

                for _fam_key, components in breakdown_map.items():
                    if isinstance(components, dict):
                        total = sum(
                            float(v) for v in components.values()
                            if isinstance(v, (int, float))
                        )
                        if total > best_score:
                            best_score = total
                            best_breakdown = components

            if best_breakdown is None:
                return False

            conn.execute(
                "UPDATE historical_documents SET evidence_json = ? WHERE id = ?",
                (_json.dumps(best_breakdown), historical_document_id),
            )
            conn.commit()
            return True

    def backfill_evidence_json(
        self,
        *,
        dry_run: bool = True,
        limit: int = 0,
        family_key: str | None = None,
    ) -> dict[str, Any]:
        """Backfill ``evidence_json`` for historical documents where it is missing.

        Uses the lighter path: extracts the best-family evidence breakdown from
        existing ``ncuc_span_artifacts`` rows.  Documents without span artifacts
        are skipped (they need full reprocessing via the queue).

        Returns a summary dict.
        """
        import time as _time
        import json as _json

        t0 = _time.monotonic()

        with self._connect() as conn:
            # ── 1. Find candidates ──────────────────────────────────────
            family_filter = ""
            params: list[Any] = []
            if family_key:
                family_filter = "AND hd.family_key = ?"
                params.append(family_key)

            if limit > 0:
                limit_clause = "LIMIT ?"
                params.append(limit)
            else:
                limit_clause = ""

            candidates = conn.execute(
                f"""
                SELECT hd.id, hd.local_path, hd.family_key
                FROM historical_documents hd
                WHERE (hd.evidence_json IS NULL OR hd.evidence_json = '' OR hd.evidence_json = '{{}}')
                  AND hd.local_path IS NOT NULL
                  AND hd.content_hash IS NOT NULL
                  AND hd.content_hash != ''
                  AND EXISTS (
                      SELECT 1 FROM ncuc_span_artifacts nsa
                      WHERE nsa.file_hash = hd.content_hash
                        AND nsa.evidence_score_breakdown_json IS NOT NULL
                        AND nsa.evidence_score_breakdown_json != ''
                        AND nsa.evidence_score_breakdown_json != '{{}}'
                  )
                  {family_filter}
                ORDER BY hd.id
                {limit_clause}
                """,
                params,
            ).fetchall()

            total_candidates = len(candidates)
            backfilled = 0
            skipped_no_spans = 0
            skipped_no_breakdown = 0
            per_doc: list[dict[str, Any]] = []
            errors: list[str] = []

            for c in candidates:
                hd_id = c["id"]
                local_path = c["local_path"]

                # ── 2. Find best span for this document ──────────────────
                # Match via content_hash/file_hash (paths differ between tables)
                span_rows = conn.execute(
                    """
                    SELECT id, evidence_score_breakdown_json
                    FROM ncuc_span_artifacts
                    WHERE file_hash = (SELECT content_hash FROM historical_documents WHERE id = ?)
                      AND evidence_score_breakdown_json IS NOT NULL
                      AND evidence_score_breakdown_json != ''
                      AND evidence_score_breakdown_json != '{}'
                    """,
                    (hd_id,),
                ).fetchall()

                if not span_rows:
                    skipped_no_spans += 1
                    continue

                # Pick the span whose breakdown has the highest total score
                best_breakdown: dict[str, Any] | None = None
                best_score: float = -999999.0
                best_family: str = ""

                for sr in span_rows:
                    try:
                        breakdown_map = _json.loads(sr["evidence_score_breakdown_json"])
                    except Exception:
                        continue

                    if not isinstance(breakdown_map, dict):
                        continue

                    for fam_key, components in breakdown_map.items():
                        if isinstance(components, dict):
                            total = sum(
                                float(v) for v in components.values()
                                if isinstance(v, (int, float))
                            )
                            if total > best_score:
                                best_score = total
                                best_breakdown = components
                                best_family = str(fam_key)

                if best_breakdown is None:
                    skipped_no_breakdown += 1
                    continue

                if dry_run:
                    per_doc.append({
                        "historical_document_id": hd_id,
                        "local_path": local_path,
                        "family_key": best_family,
                        "evidence_score": best_score,
                        "evidence_json": best_breakdown,
                    })
                    backfilled += 1
                    continue

                # ── 3. Update ───────────────────────────────────────────
                try:
                    conn.execute(
                        "UPDATE historical_documents SET evidence_json = ? WHERE id = ?",
                        (_json.dumps(best_breakdown), hd_id),
                    )
                    per_doc.append({
                        "historical_document_id": hd_id,
                        "local_path": local_path,
                        "family_key": best_family,
                        "evidence_score": best_score,
                    })
                    backfilled += 1
                except Exception as exc:
                    errors.append(f"hd:{hd_id}: {exc}")

            if not dry_run and backfilled > 0:
                conn.commit()

            duration_ms = int((_time.monotonic() - t0) * 1000)
            return {
                "dry_run": dry_run,
                "total_candidates": total_candidates,
                "backfilled": backfilled,
                "skipped_no_spans": skipped_no_spans,
                "skipped_no_breakdown": skipped_no_breakdown,
                "per_doc": per_doc,
                "errors": errors,
                "duration_ms": duration_ms,
            }

    def rebind_historical_page_range(
        self,
        historical_document_id: int,
        *,
        start_page: int,
        end_page: int | None = None,
        requeue: bool = False,
        requested_by: str = "operator",
        queue_priority: int = 90,
    ) -> HistoricalDocumentRecord | None:
        if start_page < 1:
            raise ValueError("start_page must be >= 1")
        resolved_end_page = end_page if end_page is not None else start_page
        if resolved_end_page < start_page:
            raise ValueError("end_page must be >= start_page")

        historical = self.get_historical_document(historical_document_id)
        if historical is None:
            return None

        old_start_page = historical.start_page
        old_end_page = historical.end_page
        source_pdf = str(historical.local_path)
        now = datetime.now(UTC).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM tariff_charges
                WHERE version_id IN (
                    SELECT id FROM tariff_versions WHERE historical_document_id = ?
                )
                """,
                (historical_document_id,),
            )
            conn.execute(
                "DELETE FROM historical_processing_runs WHERE historical_document_id = ?",
                (historical_document_id,),
            )
            parse_attempt_ids = [
                int(row["id"])
                for row in conn.execute(
                    """
                    SELECT id
                    FROM parse_attempt_logs
                    WHERE json_extract(metadata_json, '$.historical_document_id') = ?
                    """,
                    (historical_document_id,),
                ).fetchall()
            ]
            if parse_attempt_ids:
                placeholders = ",".join("?" for _ in parse_attempt_ids)
                conn.execute(
                    f"DELETE FROM parse_review_outcomes WHERE parse_attempt_id IN ({placeholders})",
                    parse_attempt_ids,
                )
                conn.execute(
                    f"DELETE FROM parse_attempt_logs WHERE id IN ({placeholders})",
                    parse_attempt_ids,
                )
            conn.execute(
                "DELETE FROM historical_reprocess_queue WHERE historical_document_id = ?",
                (historical_document_id,),
            )
            conn.execute(
                """
                UPDATE historical_documents
                SET start_page = ?, end_page = ?, raw_text_path = NULL,
                    parsed_result_json = NULL, evidence_json = NULL
                WHERE id = ?
                """,
                (start_page, resolved_end_page, historical_document_id),
            )
            if requeue:
                conn.execute(
                    """
                    INSERT INTO historical_reprocess_queue (
                        historical_document_id, source_pdf, family_key, priority,
                        queue_reason, requested_by, status, metadata_json, requested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        historical_document_id,
                        source_pdf,
                        historical.family_key,
                        queue_priority,
                        "page_range_rebind",
                        requested_by,
                        json.dumps(
                            {
                                "old_start_page": old_start_page,
                                "old_end_page": old_end_page,
                                "new_start_page": start_page,
                                "new_end_page": resolved_end_page,
                            },
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
            conn.commit()
        return self.get_historical_document(historical_document_id)

    def clear_redline_fingerprint_for_historical_document(
        self,
        historical_document_id: int,
        *,
        include_path_rollup: bool = False,
    ) -> dict[str, object] | None:
        historical = self.get_historical_document(historical_document_id)
        if historical is None:
            return None

        source_pdf = str(historical.local_path)
        start_page = historical.start_page
        end_page = historical.end_page
        normalized_source_pdf = str(Path(source_pdf).as_posix()).lower()

        with self._connect() as conn:
            exact_rows = [
                row
                for row in conn.execute(
                    """
                    SELECT id, source_pdf, page_start, page_end, review_flags_json
                    FROM document_fingerprints
                    ORDER BY id
                    """,
                ).fetchall()
                if (
                    str(Path(str(row["source_pdf"] or "")).as_posix()).lower() == normalized_source_pdf
                    and
                    (int(row["page_start"]) if row["page_start"] is not None else None) == start_page
                    and (int(row["page_end"]) if row["page_end"] is not None else None) == end_page
                )
            ]
            path_rows = []
            if include_path_rollup:
                path_rows = conn.execute(
                    """
                    SELECT id, review_flags_json
                    FROM document_fingerprints
                    WHERE source_pdf = ?
                      AND page_start IS NULL
                      AND page_end IS NULL
                    ORDER BY id
                    """,
                    (source_pdf,),
                ).fetchall()

            target_rows = list(exact_rows)
            seen_ids = {int(row["id"]) for row in target_rows}
            for row in path_rows:
                if int(row["id"]) in seen_ids:
                    continue
                target_rows.append(row)

            updated_ids: list[int] = []
            for row in target_rows:
                try:
                    review_flags = json.loads(row["review_flags_json"] or "[]")
                except Exception:
                    review_flags = []
                if "manual_redline_clear" not in review_flags:
                    review_flags = list(review_flags) + ["manual_redline_clear"]
                conn.execute(
                    """
                    UPDATE document_fingerprints
                    SET is_redline_candidate = 0,
                        redline_confidence = 0.0,
                        redline_signals_json = '[]',
                        red_text_samples_json = '[]',
                        strikethrough_samples_json = '[]',
                        red_is_index_only = 0,
                        review_flags_json = ?
                    WHERE id = ?
                    """,
                    (json.dumps(review_flags), int(row["id"])),
                )
                updated_ids.append(int(row["id"]))
            conn.commit()

        return {
            "historical_document_id": historical_document_id,
            "source_pdf": source_pdf,
            "page_start": start_page,
            "page_end": end_page,
            "updated_fingerprint_ids": updated_ids,
            "updated_count": len(updated_ids),
        }

    def retire_tariff_version(self, version_id: int) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, family_key, historical_document_id, effective_start
                FROM tariff_versions
                WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                return None
            charge_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?",
                    (version_id,),
                ).fetchone()[0]
            )
            conn.execute("DELETE FROM tariff_charges WHERE version_id = ?", (version_id,))
            conn.execute("DELETE FROM tariff_versions WHERE id = ?", (version_id,))
            conn.commit()
        return {
            "version_id": int(row["id"]),
            "family_key": str(row["family_key"]),
            "historical_document_id": row["historical_document_id"],
            "effective_start": row["effective_start"],
            "deleted_charge_count": charge_count,
        }

    def retire_provisional_garbage_families_nc(
        self,
        *,
        dry_run: bool = False,
        require_zero_charges: bool = True,
        state: str = "NC",
    ) -> dict[str, int]:
        """Retire provisional families that have no charged tariff content.

        Targets provisional families where every version (if any) has zero charges.
        Families with actual charge rows are skipped so real parsed content is preserved.

        Returns a dict of counters:
            families_deleted, historical_docs_deleted, versions_deleted,
            parse_review_rows_deleted, processing_runs_deleted, reprocess_queue_deleted
        """
        with self._connect() as conn:
            # Identify candidates: provisional families with no charged versions
            if require_zero_charges:
                charge_guard = """
                    AND NOT EXISTS (
                        SELECT 1 FROM tariff_versions tv2
                        JOIN tariff_charges tc ON tc.version_id = tv2.id
                        WHERE tv2.family_key = tf.family_key
                    )
                """
            else:
                charge_guard = ""

            candidate_rows = conn.execute(
                f"""
                SELECT tf.family_key
                FROM tariff_families tf
                WHERE tf.state = ?
                  AND tf.notes LIKE 'Provisional%'
                  {charge_guard}
                ORDER BY tf.family_key
                """,
                (state,),
            ).fetchall()
            candidate_keys = [r[0] for r in candidate_rows]

            if not candidate_keys or dry_run:
                return {
                    "families_deleted": 0,
                    "historical_docs_deleted": 0,
                    "versions_deleted": 0,
                    "parse_review_rows_deleted": 0,
                    "processing_runs_deleted": 0,
                    "reprocess_queue_deleted": 0,
                    "candidates_found": len(candidate_keys),
                }

            families_deleted = 0
            historical_docs_deleted = 0
            versions_deleted = 0
            parse_review_rows_deleted = 0
            processing_runs_deleted = 0
            reprocess_queue_deleted = 0

            for family_key in candidate_keys:
                # Get all historical_documents for this family
                hd_rows = conn.execute(
                    """
                    SELECT id, local_path, start_page, end_page
                    FROM historical_documents
                    WHERE family_key = ?
                    """,
                    (family_key,),
                ).fetchall()

                for hd in hd_rows:
                    hd_id = hd[0]
                    local_path = str(hd[1] or "")
                    start_page = int(hd[2]) if hd[2] is not None else None
                    end_page = int(hd[3]) if hd[3] is not None else None

                    # Delete charges and versions linked to this hd
                    version_ids = [
                        r[0] for r in conn.execute(
                            "SELECT id FROM tariff_versions WHERE historical_document_id = ?",
                            (hd_id,),
                        ).fetchall()
                    ]
                    for vid in version_ids:
                        conn.execute("DELETE FROM tariff_charges WHERE version_id = ?", (vid,))
                        versions_deleted += 1
                    conn.execute(
                        "DELETE FROM tariff_versions WHERE historical_document_id = ?",
                        (hd_id,),
                    )

                    # Delete processing metadata
                    r = conn.execute(
                        "DELETE FROM historical_processing_runs WHERE historical_document_id = ?",
                        (hd_id,),
                    )
                    processing_runs_deleted += r.rowcount
                    r = conn.execute(
                        "DELETE FROM historical_reprocess_queue WHERE historical_document_id = ?",
                        (hd_id,),
                    )
                    reprocess_queue_deleted += r.rowcount

                    # Delete parse review outcomes (span-scoped or whole-pdf)
                    if local_path:
                        if start_page is None:
                            r = conn.execute(
                                "DELETE FROM parse_review_outcomes WHERE source_pdf = ?",
                                (local_path,),
                            )
                            parse_review_rows_deleted += r.rowcount
                        else:
                            page_end = end_page if end_page is not None else start_page
                            r = conn.execute(
                                """
                                DELETE FROM parse_review_outcomes
                                WHERE source_pdf = ?
                                  AND COALESCE(page_start, 1) = ?
                                  AND COALESCE(page_end, ?) = ?
                                """,
                                (local_path, start_page, start_page, page_end),
                            )
                            parse_review_rows_deleted += r.rowcount

                    conn.execute(
                        "DELETE FROM historical_documents WHERE id = ?", (hd_id,)
                    )
                    historical_docs_deleted += 1

                # Delete any remaining versions (those without historical_document_id)
                r = conn.execute(
                    "DELETE FROM tariff_versions WHERE family_key = ?", (family_key,)
                )
                versions_deleted += r.rowcount

                # Delete rider_applicability
                conn.execute(
                    "DELETE FROM rider_applicability WHERE applies_to_family_key = ? OR rider_family_key = ?",
                    (family_key, family_key),
                )

                # Delete the family itself
                conn.execute(
                    "DELETE FROM tariff_families WHERE family_key = ?", (family_key,)
                )
                families_deleted += 1

            conn.commit()

        return {
            "families_deleted": families_deleted,
            "historical_docs_deleted": historical_docs_deleted,
            "versions_deleted": versions_deleted,
            "parse_review_rows_deleted": parse_review_rows_deleted,
            "processing_runs_deleted": processing_runs_deleted,
            "reprocess_queue_deleted": reprocess_queue_deleted,
            "candidates_found": len(candidate_keys),
        }

    def _infer_discovery_record_id_for_historical_document(
        self,
        historical_document_id: int,
    ) -> int | None:
        def _normalize_path(value: str | None) -> str:
            return str(Path(str(value or "")).as_posix()).lower()

        def _extract_nested_metadata(metadata: dict[str, object]) -> dict[str, object]:
            current: object = metadata
            for _ in range(3):
                if not isinstance(current, dict):
                    return {}
                nested = current.get("metadata_json")
                if not nested:
                    return current
                if isinstance(nested, dict):
                    current = nested
                    continue
                if not isinstance(nested, str):
                    return current
                try:
                    current = json.loads(nested)
                except Exception:
                    return current
            return current if isinstance(current, dict) else {}

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT metadata_json
                FROM historical_documents
                WHERE id = ?
                """,
                (historical_document_id,),
            ).fetchone()
            if not row:
                return None

            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            nested_metadata = _extract_nested_metadata(metadata)

            local_file = nested_metadata.get("local_file")
            if not local_file:
                return None

            record = conn.execute(
                """
                SELECT id
                FROM ncuc_discovery_records
                WHERE local_path = ?
                LIMIT 1
                """,
                (str(local_file),),
            ).fetchone()
            if record:
                return int(record["id"])

            normalized_local_file = _normalize_path(str(local_file))
            for candidate in conn.execute(
                "SELECT id, local_path FROM ncuc_discovery_records WHERE local_path IS NOT NULL"
            ).fetchall():
                if _normalize_path(candidate["local_path"]) == normalized_local_file:
                    return int(candidate["id"])
            return None

    def _infer_regulator_local_file_for_historical_document(
        self,
        historical_document_id: int,
    ) -> str | None:
        def _extract_nested_metadata(metadata: dict[str, object]) -> dict[str, object]:
            current: object = metadata
            for _ in range(3):
                if not isinstance(current, dict):
                    return {}
                nested = current.get("metadata_json")
                if not nested:
                    return current
                if isinstance(nested, dict):
                    current = nested
                    continue
                if not isinstance(nested, str):
                    return current
                try:
                    current = json.loads(nested)
                except Exception:
                    return current
            return current if isinstance(current, dict) else {}

        with self._connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM historical_documents WHERE id = ?",
                (historical_document_id,),
            ).fetchone()
            if not row:
                return None
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            nested_metadata = _extract_nested_metadata(metadata)
            local_file = nested_metadata.get("local_file")
            return str(local_file) if local_file else None

    @staticmethod
    def _classify_unbounded_historical_source(local_path: str) -> str:
        normalized = local_path.replace("/", "\\").lower()
        if "data\\raw\\nc\\" in normalized:
            return "current_pdf"
        if "data\\historical\\raw\\" in normalized:
            return "legacy_raw_attachment"
        if "data\\raw\\historical\\ncuc\\" in normalized:
            return "legacy_regulator_pdf"
        if "data\\historical\\ncuc\\" in normalized:
            return "discovery_pdf"
        return "other"

    def _suggest_unbounded_historical_review_action(
        self,
        *,
        source_kind: str,
        discovery_record_id: int | None,
        historical_document_id: int | None = None,
        stale_current_snapshot: dict[str, object] | None = None,
        bundle_reference_overlap: dict[str, object] | None = None,
    ) -> str:
        if source_kind == "current_pdf" and stale_current_snapshot:
            return "repair_current_document_snapshot"
        if (
            source_kind == "legacy_raw_attachment"
            and discovery_record_id is not None
            and historical_document_id is not None
        ):
            if self._historical_document_has_bounded_regulator_peer(historical_document_id):
                if bundle_reference_overlap:
                    return "retire_bundle_reference_residue"
                return "manual_lineage_review"
            if self._discovery_record_lacks_tariff_structure(discovery_record_id):
                return "retire_legacy_raw_attachment"
        if discovery_record_id is not None:
            return "remine_from_discovery_record"
        if source_kind == "current_pdf":
            return "add_profile_or_current_parser_bridge"
        if source_kind in {"legacy_raw_attachment", "legacy_regulator_pdf"}:
            return "manual_lineage_review"
        return "manual_review"

    def _detect_stale_current_document_snapshot(
        self,
        *,
        historical_document_id: int,
    ) -> dict[str, object] | None:
        row: sqlite3.Row | None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    hd.id AS historical_document_id,
                    hd.family_key,
                    hd.title AS historical_title,
                    hd.archived_url,
                    hd.local_path AS historical_local_path,
                    tf.title AS family_title,
                    tf.current_document_id AS anchor_document_id,
                    stale.id AS stale_document_id,
                    stale.title AS stale_document_title,
                    stale.category AS stale_document_category,
                    stale.schedule_code AS stale_schedule_code,
                    stale.tariff_identifier AS stale_tariff_identifier,
                    anchor.title AS anchor_document_title,
                    anchor.category AS anchor_document_category,
                    anchor.schedule_code AS anchor_schedule_code,
                    anchor.tariff_identifier AS anchor_tariff_identifier,
                    anchor.local_path AS anchor_local_path
                FROM historical_documents hd
                JOIN tariff_families tf ON tf.family_key = hd.family_key
                LEFT JOIN documents anchor ON anchor.id = tf.current_document_id
                LEFT JOIN documents stale
                  ON stale.id = CAST(SUBSTR(hd.archived_url, 11) AS INTEGER)
                WHERE hd.id = ?
                  AND hd.archived_url LIKE 'documents/%'
                """,
                (historical_document_id,),
            ).fetchone()

        if row is None or row["anchor_document_id"] is None:
            return None
        archived_url = str(row["archived_url"] or "")
        match = re.fullmatch(r"documents/(\d+)", archived_url)
        if not match:
            return None
        stale_document_id = int(match.group(1))
        anchor_document_id = int(row["anchor_document_id"])
        if stale_document_id == anchor_document_id:
            return None

        stale_title = str(row["stale_document_title"] or "")
        anchor_title = str(row["anchor_document_title"] or "")
        family_title = str(row["family_title"] or row["historical_title"] or "")
        stale_category = str(row["stale_document_category"] or "").lower()
        anchor_category = str(row["anchor_document_category"] or "").lower()
        stale_score = _token_overlap_size(family_title, stale_title)
        anchor_score = _token_overlap_size(family_title, anchor_title)

        reasons: list[str] = []
        if stale_category == "other" and anchor_category in {"rate", "rider"}:
            reasons.append("stale_document_category_other")
        if not row["stale_schedule_code"] and row["anchor_schedule_code"]:
            reasons.append("stale_document_missing_schedule_code")
        if not row["stale_tariff_identifier"] and row["anchor_tariff_identifier"]:
            reasons.append("stale_document_missing_tariff_identifier")
        if anchor_score >= stale_score + 2:
            reasons.append("anchor_title_support")

        if not reasons:
            return None

        return {
            "stale_document_id": stale_document_id,
            "stale_document_title": stale_title,
            "stale_document_category": stale_category,
            "anchor_document_id": anchor_document_id,
            "anchor_document_title": anchor_title,
            "anchor_document_category": anchor_category,
            "anchor_local_path": row["anchor_local_path"],
            "reasons": reasons,
        }

    def repair_historical_current_document_snapshot(
        self,
        historical_document_id: int,
        *,
        requested_by: str = "operator",
        queue_priority: int = 95,
    ) -> HistoricalDocumentRecord | None:
        historical = self.get_historical_document(historical_document_id)
        if historical is None:
            return None

        stale_snapshot = self._detect_stale_current_document_snapshot(
            historical_document_id=historical_document_id
        )
        if not stale_snapshot:
            return historical

        family = self.get_tariff_family(historical.family_key)
        if family is None or family.current_document_id is None:
            return historical

        document = self.get_document(family.current_document_id)
        if document is None:
            return historical

        updated = historical.model_copy(
            update={
                "current_document_id": document.id,
                "title": document.title,
                "category": document.category,
                "canonical_url": document.document_url,
                "archived_url": f"documents/{document.id}",
                "snapshot_timestamp": document.retrieved_at,
                "local_path": Path(document.local_path),
                "raw_text_path": None,
                "content_hash": document.content_hash,
                "content_type": document.content_type,
                "direct_status_code": document.status_code,
                "direct_downloadable": document.status_code == 200,
                "effective_start": document.effective_date or historical.effective_start,
                "retrieved_at": document.retrieved_at,
                "parsed_result_json": None,
                "evidence_json": None,
            }
        )
        payload = updated.model_dump(mode="json", exclude={"id"})
        page_start = historical.start_page or 1
        page_end = historical.end_page or page_start
        old_source_pdf = str(historical.local_path)
        now = datetime.now(UTC).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM tariff_charges
                WHERE version_id IN (
                    SELECT id FROM tariff_versions WHERE historical_document_id = ?
                )
                """,
                (historical_document_id,),
            )
            conn.execute(
                "DELETE FROM tariff_versions WHERE historical_document_id = ?",
                (historical_document_id,),
            )
            conn.execute(
                "DELETE FROM historical_processing_runs WHERE historical_document_id = ?",
                (historical_document_id,),
            )
            conn.execute(
                "DELETE FROM historical_reprocess_queue WHERE historical_document_id = ?",
                (historical_document_id,),
            )
            conn.execute(
                """
                DELETE FROM parse_review_outcomes
                WHERE source_pdf = ?
                  AND COALESCE(page_start, 1) = ?
                  AND COALESCE(page_end, ?) = ?
                """,
                (old_source_pdf, page_start, page_start, page_end),
            )
            conn.execute(
                """
                DELETE FROM document_fingerprints
                WHERE source_pdf = ?
                  AND COALESCE(page_start, 1) = ?
                  AND COALESCE(page_end, ?) = ?
                """,
                (old_source_pdf, page_start, page_start, page_end),
            )
            conn.execute(
                """
                DELETE FROM parse_attempt_logs
                WHERE source_pdf = ?
                  AND COALESCE(page_start, 1) = ?
                  AND COALESCE(page_end, ?) = ?
                """,
                (old_source_pdf, page_start, page_start, page_end),
            )
            conn.execute(
                """
                UPDATE historical_documents
                SET current_document_id = ?, title = ?, category = ?, canonical_url = ?,
                    archived_url = ?, snapshot_timestamp = ?, local_path = ?, raw_text_path = ?,
                    content_hash = ?, content_type = ?, direct_status_code = ?,
                    direct_downloadable = ?, effective_start = ?, retrieved_at = ?,
                    metadata_json = ?, parsed_result_json = NULL, evidence_json = NULL
                WHERE id = ?
                """,
                (
                    updated.current_document_id,
                    updated.title,
                    updated.category,
                    updated.canonical_url,
                    updated.archived_url,
                    updated.snapshot_timestamp.isoformat(),
                    str(updated.local_path),
                    None,
                    updated.content_hash,
                    updated.content_type,
                    updated.direct_status_code,
                    1 if updated.direct_downloadable else 0,
                    updated.effective_start,
                    updated.retrieved_at.isoformat(),
                    json.dumps(payload, sort_keys=True),
                    historical_document_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO historical_reprocess_queue (
                    historical_document_id, source_pdf, family_key, priority,
                    queue_reason, requested_by, status, latest_run_id,
                    error_message, metadata_json, requested_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)
                """,
                (
                    historical_document_id,
                    str(updated.local_path),
                    updated.family_key,
                    queue_priority,
                    "repair_current_document_snapshot",
                    requested_by,
                    json.dumps(
                        {
                            "repair_reasons": stale_snapshot["reasons"],
                            "stale_document_id": stale_snapshot["stale_document_id"],
                            "anchor_document_id": stale_snapshot["anchor_document_id"],
                        },
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            conn.commit()

        return self.get_historical_document(historical_document_id)

    def _historical_document_has_bounded_regulator_peer(
        self,
        historical_document_id: int,
    ) -> bool:
        def _normalize_path(value: str | None) -> str:
            return str(Path(str(value or "")).as_posix()).lower()

        regulator_local_path = self._infer_regulator_local_file_for_historical_document(
            historical_document_id
        )
        if not regulator_local_path:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM historical_documents
                WHERE local_path = ?
                  AND start_page IS NOT NULL
                  AND id != ?
                LIMIT 1
                """,
                (regulator_local_path, historical_document_id),
            ).fetchone()
            if row is not None:
                return True
            normalized_regulator_path = _normalize_path(regulator_local_path)
            for candidate in conn.execute(
                """
                SELECT id, local_path
                FROM historical_documents
                WHERE start_page IS NOT NULL
                  AND id != ?
                """,
                (historical_document_id,),
            ).fetchall():
                if _normalize_path(candidate["local_path"]) == normalized_regulator_path:
                    return True
        return False

    def _detect_legacy_bundle_reference_residue(
        self,
        *,
        historical_document_id: int,
        discovery_record_id: int | None,
    ) -> dict[str, object] | None:
        def _extract_nested_metadata(metadata: dict[str, object]) -> dict[str, object]:
            current: object = metadata
            for _ in range(3):
                if not isinstance(current, dict):
                    return {}
                nested = current.get("metadata_json")
                if not nested:
                    return current
                if isinstance(nested, dict):
                    current = nested
                    continue
                if not isinstance(nested, str):
                    return current
                try:
                    current = json.loads(nested)
                except Exception:
                    return current
            return current if isinstance(current, dict) else {}

        if discovery_record_id is None:
            return None

        regulator_local_path = self._infer_regulator_local_file_for_historical_document(
            historical_document_id
        )
        if not regulator_local_path:
            return None

        with self._connect() as conn:
            historical_row = conn.execute(
                """
                SELECT family_key, title, metadata_json
                FROM historical_documents
                WHERE id = ?
                """,
                (historical_document_id,),
            ).fetchone()
            if historical_row is None:
                return None

            target_leaf = extract_leaf_number(historical_row["family_key"])
            if not target_leaf:
                try:
                    metadata = json.loads(historical_row["metadata_json"] or "{}")
                except Exception:
                    metadata = {}
                nested_metadata = _extract_nested_metadata(metadata)
                parse_text_metadata = nested_metadata.get("parse_text_metadata") or {}
                if isinstance(parse_text_metadata, dict):
                    family_code = str(parse_text_metadata.get("family_code") or "").strip()
                    if family_code.isdigit():
                        target_leaf = family_code
            if not target_leaf:
                return None

            same_family_bounded = conn.execute(
                """
                SELECT 1
                FROM historical_documents
                WHERE local_path = ?
                  AND family_key = ?
                  AND start_page IS NOT NULL
                  AND id != ?
                LIMIT 1
                """,
                (regulator_local_path, historical_row["family_key"], historical_document_id),
            ).fetchone()
            if same_family_bounded is not None:
                return None

            bounded_docs = conn.execute(
                """
                SELECT id, family_key, title, start_page, end_page
                FROM historical_documents
                WHERE local_path = ?
                  AND start_page IS NOT NULL
                  AND id != ?
                ORDER BY start_page, end_page, id
                """,
                (regulator_local_path, historical_document_id),
            ).fetchall()
            if not bounded_docs:
                return None

            host_docs_by_range: dict[tuple[int, int], list[sqlite3.Row]] = defaultdict(list)
            for item in bounded_docs:
                host_docs_by_range[(int(item["start_page"]), int(item["end_page"]))].append(item)

            latest_spans: dict[tuple[int, int], sqlite3.Row] = {}
            for row in conn.execute(
                """
                SELECT id, start_page, end_page, extracted_leaf_nos_json, extracted_schedule_titles_json,
                       metadata_json, updated_at
                FROM ncuc_span_artifacts
                WHERE discovery_record_id = ?
                  AND source_pdf = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (discovery_record_id, regulator_local_path),
            ).fetchall():
                key = (int(row["start_page"]), int(row["end_page"]))
                if key not in latest_spans:
                    latest_spans[key] = row

        host_matches: list[dict[str, object]] = []
        for key, span_row in latest_spans.items():
            try:
                extracted_leafs = {
                    str(item).strip()
                    for item in json.loads(span_row["extracted_leaf_nos_json"] or "[]")
                    if str(item).strip()
                }
            except Exception:
                extracted_leafs = set()
            if target_leaf not in extracted_leafs:
                continue
            if len(extracted_leafs) < 4:
                continue
            host_docs = host_docs_by_range.get(key) or []
            for host_doc in host_docs:
                if host_doc["family_key"] == historical_row["family_key"]:
                    continue
                try:
                    titles = [
                        str(item).strip()
                        for item in json.loads(
                            span_row["extracted_schedule_titles_json"] or "[]"
                        )
                        if str(item).strip()
                    ]
                except Exception:
                    titles = []
                host_matches.append(
                    {
                        "host_historical_document_id": int(host_doc["id"]),
                        "host_family_key": str(host_doc["family_key"]),
                        "host_title": str(host_doc["title"] or ""),
                        "host_start_page": int(host_doc["start_page"]),
                        "host_end_page": int(host_doc["end_page"]),
                        "span_leaf_count": len(extracted_leafs),
                        "span_leafs": sorted(extracted_leafs),
                        "span_titles": titles[:6],
                    }
                )

        if not host_matches:
            return None

        host_matches.sort(
            key=lambda item: (
                item["host_start_page"],
                item["host_end_page"],
                item["host_historical_document_id"],
            )
        )
        return {
            "target_leaf": target_leaf,
            "host_count": len(host_matches),
            "hosts": host_matches,
        }

    def _discovery_record_lacks_tariff_structure(
        self,
        discovery_record_id: int,
    ) -> bool:
        def _normalize_title(value: str | None) -> str:
            return re.sub(r"\s+", " ", str(value or "").strip()).upper()

        rows: list[sqlite3.Row]
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT metadata_json
                FROM ncuc_page_artifacts
                WHERE discovery_record_id = ?
                ORDER BY page_number
                """,
                (discovery_record_id,),
            ).fetchall()
        if not rows:
            return False

        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            if metadata.get("has_leaf_header") or metadata.get("has_revised_header"):
                return False
            if metadata.get("extracted_leaf_nos"):
                return False
            for raw_code in metadata.get("extracted_schedule_codes") or []:
                code = _normalize_title(raw_code)
                if not code:
                    continue
                if code.startswith(("SCHEDULE ", "RIDER ", "RATE ")):
                    return False
                if code in {"CERTIFICATE OF SERVICE", "TYPE OF SERVICE", "RIDER APPLICATIONS"}:
                    continue
                if (
                    "DUKE ENERGY" in code
                    and len(code.split()) <= 12
                    and any(
                    token in code for token in ("SERVICE", "RIDER", "PROGRAM", "SCHEDULE")
                    )
                ):
                    return False
        return True

    def list_current_anchor_mismatches(
        self,
        *,
        state: str | None = None,
        company: str | None = None,
        family_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        query = """
            SELECT
                tf.family_key,
                tf.title AS family_title,
                tf.schedule_code AS family_schedule_code,
                tf.tariff_identifier AS family_tariff_identifier,
                tf.family_type,
                tf.company,
                tf.current_document_id,
                d.title AS document_title,
                d.schedule_code AS document_schedule_code,
                d.tariff_identifier AS document_tariff_identifier,
                d.local_path AS document_local_path
            FROM tariff_families tf
            JOIN documents d ON d.id = tf.current_document_id
            WHERE tf.current_document_id IS NOT NULL
        """
        params: list[object] = []
        if state:
            query += " AND tf.state = ?"
            params.append(state.upper())
        if company:
            query += " AND LOWER(tf.company) = ?"
            params.append(company.lower())
        if family_type:
            query += " AND tf.family_type = ?"
            params.append(family_type)
        query += " ORDER BY tf.family_key"

        rows_out: list[dict] = []
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        page_signal_cache: dict[str, dict[str, object]] = {}
        for row in rows:
            page_signals = {"headings": [], "leaf_nos": []}
            local_path = row["document_local_path"]
            if local_path:
                page_signals = page_signal_cache.get(local_path)
                if page_signals is None:
                    page_signals = _load_current_document_page_signals(local_path)
                    page_signal_cache[local_path] = page_signals

            reasons = detect_current_family_anchor_mismatch(
                family_key=row["family_key"],
                family_schedule_code=row["family_schedule_code"],
                document_tariff_identifier=row["document_tariff_identifier"],
                document_schedule_code=row["document_schedule_code"],
                document_title=row["document_title"],
                page_headings=list(page_signals["headings"]),
                page_leaf_nos=list(page_signals["leaf_nos"]),
            )
            if not reasons:
                continue

            with self._connect() as conn:
                historical_title_rows = conn.execute(
                    """
                    SELECT title
                    FROM historical_documents
                    WHERE family_key = ?
                    ORDER BY id
                    """,
                    (row["family_key"],),
                ).fetchall()
            historical_titles = [hist_row["title"] for hist_row in historical_title_rows if hist_row["title"]]
            family_support = sum(
                _token_overlap_size(row["family_title"], title) for title in historical_titles
            )
            document_support = sum(
                _token_overlap_size(row["document_title"], title) for title in historical_titles
            )
            if historical_titles and family_support >= document_support + 3:
                reasons.append("historical_title_conflict")

            rows_out.append(
                {
                    "family_key": row["family_key"],
                    "family_title": row["family_title"],
                    "family_schedule_code": row["family_schedule_code"],
                    "family_tariff_identifier": row["family_tariff_identifier"],
                    "family_leaf_no": extract_leaf_number(row["family_key"]),
                    "family_type": row["family_type"],
                    "company": row["company"],
                    "current_document_id": row["current_document_id"],
                    "document_title": row["document_title"],
                    "document_schedule_code": row["document_schedule_code"],
                    "document_tariff_identifier": row["document_tariff_identifier"],
                    "document_leaf_no": extract_leaf_number(row["document_tariff_identifier"]),
                    "document_local_path": row["document_local_path"],
                    "candidate_headings": list(page_signals["headings"])[:3],
                    "candidate_leaf_nos": list(page_signals["leaf_nos"])[:3],
                    "historical_title_conflict_score": family_support - document_support,
                    "reasons": reasons,
                    "review_action": self._suggest_current_anchor_review_action(
                        family_leaf_no=extract_leaf_number(row["family_key"]),
                        document_leaf_no=extract_leaf_number(row["document_tariff_identifier"]),
                        reasons=reasons,
                    ),
                    "suggested_title": row["document_title"],
                    "suggested_schedule_code": row["document_schedule_code"],
                    "suggested_tariff_identifier": row["document_tariff_identifier"],
                }
            )

        rows_out.sort(key=lambda item: (item["family_key"], item["current_document_id"]))
        if limit and limit > 0:
            return rows_out[:limit]
        return rows_out

    def sync_family_metadata_from_current_document(
        self,
        family_key: str,
    ) -> "TariffFamilyRecord | None":
        family = self.get_tariff_family(family_key)
        if family is None or family.current_document_id is None:
            return None

        document = self.get_document(family.current_document_id)
        if document is None:
            return None

        merged_aliases = list(dict.fromkeys([
            *(family.aliases or []),
            *( [family.title] if family.title and family.title != document.title else [] ),
        ]))
        updated = family.model_copy(
            update={
                "title": document.title or family.title,
                "schedule_code": document.schedule_code or family.schedule_code,
                "tariff_identifier": document.tariff_identifier or family.tariff_identifier,
                "aliases": merged_aliases,
            }
        )
        self.upsert_tariff_family(updated)
        return self.get_tariff_family(family_key)

    def suggest_current_documents_for_family(
        self,
        family_key: str,
        *,
        limit: int = 3,
    ) -> list[dict]:
        family = self.get_tariff_family(family_key)
        if family is None:
            return []

        search_titles = [family.title or "", *family.aliases]
        title_tokens = set()
        for text in search_titles:
            title_tokens.update(_normalized_tokens(text))
        normalized_search_titles = [
            normalized
            for normalized in (_normalized_phrase(text) for text in search_titles)
            if len(normalized) >= 8
        ]
        current_doc_categories = _FAMILY_CURRENT_DOC_CATEGORY_HINTS.get(
            family.family_type,
            set(),
        )

        with self._connect() as conn:
            historical_leaf_nos = _load_family_historical_leaf_nos(conn, family.family_key)
            rows = conn.execute(
                """
                SELECT id, title, category, tariff_identifier, schedule_code, local_path
                FROM documents
                WHERE state = ? AND LOWER(COALESCE(company, '')) = ? AND kind = 'pdf'
                ORDER BY id
                """,
                (family.state.upper(), family.company.lower()),
            ).fetchall()

        suggestions: list[dict] = []
        page_signal_cache: dict[str, dict[str, object]] = {}
        for row in rows:
            score = 0
            reasons: list[str] = []
            doc_title = row["title"] or ""
            doc_path = row["local_path"] or ""
            doc_tokens = _normalized_tokens(doc_title) | _normalized_tokens(doc_path)
            doc_category = str(row["category"] or "").lower()

            if family.schedule_code and row["schedule_code"] and family.schedule_code.upper() == str(row["schedule_code"]).upper():
                score += 12
                reasons.append("schedule_code")
            if family.tariff_identifier and row["tariff_identifier"] and family.tariff_identifier == row["tariff_identifier"]:
                score += 10
                reasons.append("tariff_identifier")
            if current_doc_categories and doc_category in current_doc_categories:
                score += 1
                reasons.append("category")

            score += _score_token_overlap(
                reasons,
                token_label="tokens",
                family_tokens=title_tokens,
                candidate_tokens=doc_tokens,
                max_points=6,
            )

            haystacks = f"{doc_title} {doc_path}".upper()
            for normalized in normalized_search_titles:
                if normalized in _normalized_phrase(haystacks):
                    score += 6
                    reasons.append("title_phrase")
                    break

            should_mine_pages = bool(
                row["local_path"]
                and (
                    score > 0
                    or title_tokens.intersection(doc_tokens)
                    or doc_category in current_doc_categories
                )
            )
            page_signals = {
                "headings": [],
                "heading_tokens": set(),
                "leaf_nos": [],
            }
            if should_mine_pages:
                cache_key = str(row["local_path"])
                page_signals = page_signal_cache.get(cache_key)
                if page_signals is None:
                    page_signals = _load_current_document_page_signals(row["local_path"])
                    page_signal_cache[cache_key] = page_signals

                heading_tokens = set(page_signals["heading_tokens"])
                score += _score_token_overlap(
                    reasons,
                    token_label="page_tokens",
                    family_tokens=title_tokens,
                    candidate_tokens=heading_tokens,
                    max_points=8,
                )

                normalized_headings = [
                    _normalized_phrase(heading)
                    for heading in page_signals["headings"]
                    if heading
                ]
                for normalized_title in normalized_search_titles:
                    if any(
                        normalized_title in heading or heading in normalized_title
                        for heading in normalized_headings
                        if heading
                    ):
                        score += 8
                        reasons.append("page_heading_phrase")
                        break
                candidate_leaf_nos = {str(value).strip() for value in page_signals["leaf_nos"] if value}
                if historical_leaf_nos and candidate_leaf_nos:
                    if historical_leaf_nos.intersection(candidate_leaf_nos):
                        score += 10
                        reasons.append("historical_leaf_match")
                    elif not row["schedule_code"] and not row["tariff_identifier"]:
                        score -= 8
                        reasons.append("historical_leaf_mismatch")

            if score < 4:
                continue
            suggestions.append(
                {
                    "document_id": int(row["id"]),
                    "title": doc_title,
                    "category": row["category"],
                    "tariff_identifier": row["tariff_identifier"],
                    "schedule_code": row["schedule_code"],
                    "local_path": row["local_path"],
                    "score": score,
                    "reasons": reasons,
                    "candidate_headings": list(page_signals["headings"])[:2],
                    "candidate_leaf_nos": list(page_signals["leaf_nos"])[:3],
                }
            )

        suggestions.sort(key=lambda item: (-item["score"], item["document_id"]))
        return suggestions[:limit]

    def attach_current_document_to_family(
        self,
        family_key: str,
        *,
        document_id: int,
    ) -> "TariffFamilyRecord | None":
        family = self.get_tariff_family(family_key)
        if family is None:
            return None

        document = self.get_document(document_id)
        if document is None:
            raise ValueError(f"Document not found: {document_id}")

        family_state = (family.state or "").upper()
        document_state = (document.state or "").upper()
        if family_state and document_state and family_state != document_state:
            raise ValueError(
                f"Document {document_id} state mismatch: expected {family_state}, got {document_state}"
            )

        family_company = (family.company or "").lower()
        document_company = (document.company or "").lower()
        if family_company and document_company and family_company != document_company:
            raise ValueError(
                f"Document {document_id} company mismatch: expected {family_company}, got {document_company}"
            )

        record = family.model_copy(update={"current_document_id": document_id})
        self.upsert_tariff_family(record)
        return self.get_tariff_family(family_key)

    @staticmethod
    def _suggest_current_anchor_review_action(
        *,
        family_leaf_no: str | None,
        document_leaf_no: str | None,
        reasons: list[str],
    ) -> str:
        if "historical_title_conflict" in reasons:
            return "review_historical_family_migration"
        if "tariff_identifier_leaf_mismatch" in reasons:
            return "review_current_document_anchor"
        if family_leaf_no and document_leaf_no and family_leaf_no == document_leaf_no:
            if any(
                reason in reasons
                for reason in (
                    "document_schedule_code_mismatch",
                    "mined_schedule_code_mismatch",
                )
            ):
                return "sync_family_metadata_from_current_document"
        return "manual_review"

    def delete_tariff_data_for_family(self, family_key: str) -> None:
        """Delete all tariff_versions, tariff_charges, and rider_applicability for a family.

        Used before re-parsing to avoid duplicate rows.
        """
        with self._connect() as conn:
            # Get version ids first
            version_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM tariff_versions WHERE family_key = ?", (family_key,)
                ).fetchall()
            ]
            for vid in version_ids:
                conn.execute("DELETE FROM tariff_charges WHERE version_id = ?", (vid,))
            conn.execute("DELETE FROM tariff_versions WHERE family_key = ?", (family_key,))
            conn.execute(
                "DELETE FROM rider_applicability WHERE applies_to_family_key = ? OR rider_family_key = ?",
                (family_key, family_key),
            )

    def replace_parsed_tariff_data(
        self,
        family_key: str,
        version: "TariffVersionRecord",
        charges: list["TariffChargeRecord"],
        riders: list["RiderApplicabilityRecord"],
    ) -> None:
        """Atomically delete old tariff data and insert the new version and its charges/riders.
        
        This prevents empty or incomplete tariff records if parsing or insertion fails.
        """
        import sqlite3
        import logging
        now = datetime.now(UTC).isoformat()
        log = logging.getLogger("duke_rates.repository")
        
        with self._connect() as conn:
            try:
                # 1. Delete existing data for this family
                version_ids = [
                    row[0] for row in conn.execute(
                        "SELECT id FROM tariff_versions WHERE family_key = ?", (family_key,)
                    ).fetchall()
                ]
                for vid in version_ids:
                    conn.execute("DELETE FROM tariff_charges WHERE version_id = ?", (vid,))
                conn.execute("DELETE FROM tariff_versions WHERE family_key = ?", (family_key,))
                conn.execute(
                    "DELETE FROM rider_applicability WHERE applies_to_family_key = ? OR rider_family_key = ?",
                    (family_key, family_key),
                )

                # 2. Insert the new version
                cursor = conn.execute(
                    """
                    INSERT INTO tariff_versions (
                        family_key, document_id, historical_document_id,
                        effective_start, effective_end, revision_label, supersedes_label,
                        source_type, confidence_score, notes, created_at,
                        docket_number, order_date, leaf_no, source_pdf, docket_dir
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version.family_key,
                        version.document_id,
                        version.historical_document_id,
                        version.effective_start,
                        version.effective_end,
                        version.revision_label,
                        version.supersedes_label,
                        version.source_type,
                        version.confidence_score,
                        version.notes,
                        now,
                        version.docket_number,
                        version.order_date,
                        version.leaf_no,
                        version.source_pdf,
                        version.docket_dir,
                    ),
                )
                version_id = int(cursor.lastrowid)

                # 3. Insert the charges
                for charge in charges:
                    conn.execute(
                        """
                        INSERT INTO tariff_charges (
                            version_id, family_key, charge_type, charge_label,
                            rate_value, rate_unit, tier_min, tier_max,
                            tou_period, season, customer_class,
                            source_snippet, confidence_score, notes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            version_id,
                            charge.family_key,
                            charge.charge_type,
                            charge.charge_label,
                            charge.rate_value,
                            charge.rate_unit,
                            charge.tier_min,
                            charge.tier_max,
                            charge.tou_period,
                            charge.season,
                            charge.customer_class,
                            charge.source_snippet,
                            charge.confidence_score,
                            charge.notes,
                            now,
                        ),
                    )

                # 4. Insert the applicable riders
                for rider in riders:
                    try:
                        conn.execute(
                            """
                            INSERT INTO rider_applicability (
                                rider_family_key, applies_to_family_key, applicability_notes,
                                mandatory, enrollment_type, in_rider_summary, source_type, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rider.rider_family_key,
                                rider.applies_to_family_key,
                                rider.applicability_notes,
                                1 if rider.mandatory else 0,
                                rider.enrollment_type,
                                1 if rider.in_rider_summary else 0,
                                rider.source_type,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        log.warning(
                            "Unable to link rider %s -> %s; rider family key is unresolved",
                            rider.rider_family_key,
                            rider.applies_to_family_key,
                        )

                # Commit transaction explicitly
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def upsert_tariff_version(self, record: "TariffVersionRecord") -> int:
        """Insert a tariff version. Returns the new id."""
        from duke_rates.models.tariff import TariffVersionRecord  # noqa: F401
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tariff_versions (
                    family_key, document_id, historical_document_id,
                    effective_start, effective_end, revision_label, supersedes_label,
                    source_type, confidence_score, notes, created_at,
                    docket_number, order_date, leaf_no, source_pdf, docket_dir
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.family_key,
                    record.document_id,
                    record.historical_document_id,
                    record.effective_start,
                    record.effective_end,
                    record.revision_label,
                    record.supersedes_label,
                    record.source_type,
                    record.confidence_score,
                    record.notes,
                    now,
                    record.docket_number,
                    record.order_date,
                    record.leaf_no,
                    record.source_pdf,
                    record.docket_dir,
                ),
            )
            return int(cursor.lastrowid)

    def list_tariff_versions(self, family_key: str) -> list["TariffVersionRecord"]:
        from duke_rates.models.tariff import TariffVersionRecord  # noqa: F401
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tariff_versions WHERE family_key = ? ORDER BY effective_start",
                (family_key,),
            ).fetchall()
            return [self._row_to_tariff_version(r) for r in rows]

    def upsert_tariff_charge(self, record: "TariffChargeRecord") -> int:
        """Insert a tariff charge. Returns the new id."""
        from duke_rates.models.tariff import TariffChargeRecord  # noqa: F401
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tariff_charges (
                    version_id, family_key, charge_type, charge_label,
                    rate_value, rate_unit, tier_min, tier_max,
                    tou_period, season, customer_class,
                    source_snippet, confidence_score, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.version_id,
                    record.family_key,
                    record.charge_type,
                    record.charge_label,
                    record.rate_value,
                    record.rate_unit,
                    record.tier_min,
                    record.tier_max,
                    record.tou_period,
                    record.season,
                    record.customer_class,
                    record.source_snippet,
                    record.confidence_score,
                    record.notes,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_tariff_charges(self, version_id: int) -> list["TariffChargeRecord"]:
        from duke_rates.models.tariff import TariffChargeRecord  # noqa: F401
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tariff_charges WHERE version_id = ? ORDER BY charge_type, tier_min",
                (version_id,),
            ).fetchall()
            return [self._row_to_tariff_charge(r) for r in rows]

    def deduplicate_tariff_charges_for_version(
        self,
        version_id: int,
    ) -> dict[str, int]:
        """Deduplicate tariff_charges rows for one version using the natural charge signature."""
        with self._connect() as conn:
            before = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?",
                    (version_id,),
                ).fetchone()[0]
            )
            if before == 0:
                return {
                    "version_id": version_id,
                    "before_count": 0,
                    "after_count": 0,
                    "duplicates_removed": 0,
                }

            conn.execute(
                """
                DELETE FROM tariff_charges
                WHERE version_id = ?
                  AND id NOT IN (
                    SELECT MIN(id)
                    FROM tariff_charges
                    WHERE version_id = ?
                    GROUP BY
                        charge_type,
                        COALESCE(charge_label, ''),
                        COALESCE(rate_value, -999999999.0),
                        COALESCE(rate_unit, ''),
                        COALESCE(season, ''),
                        COALESCE(tou_period, ''),
                        COALESCE(tier_min, -999999999.0),
                        COALESCE(tier_max, -999999999.0),
                        COALESCE(customer_class, '')
                  )
                """,
                (version_id, version_id),
            )
            after = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?",
                    (version_id,),
                ).fetchone()[0]
            )
        return {
            "version_id": version_id,
            "before_count": before,
            "after_count": after,
            "duplicates_removed": before - after,
        }

    def upsert_rider_applicability(self, record: "RiderApplicabilityRecord") -> int:
        """Insert or replace a rider applicability link. Returns id."""
        from duke_rates.models.tariff import RiderApplicabilityRecord  # noqa: F401
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rider_applicability (
                    rider_family_key, applies_to_family_key, mandatory,
                    enrollment_type, in_rider_summary, applicability_notes,
                    effective_start, effective_end,
                    source_type, confidence_score, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rider_family_key, applies_to_family_key, effective_start)
                DO UPDATE SET
                    mandatory = excluded.mandatory,
                    enrollment_type = excluded.enrollment_type,
                    in_rider_summary = excluded.in_rider_summary,
                    applicability_notes = excluded.applicability_notes,
                    effective_end = excluded.effective_end,
                    source_type = excluded.source_type,
                    confidence_score = excluded.confidence_score
                """,
                (
                    record.rider_family_key,
                    record.applies_to_family_key,
                    int(record.mandatory),
                    record.enrollment_type,
                    1 if record.in_rider_summary else 0,
                    record.applicability_notes,
                    record.effective_start,
                    record.effective_end,
                    record.source_type,
                    record.confidence_score,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_rider_applicability(
        self,
        *,
        rider_family_key: str | None = None,
        applies_to_family_key: str | None = None,
    ) -> list["RiderApplicabilityRecord"]:
        from duke_rates.models.tariff import RiderApplicabilityRecord  # noqa: F401
        query = "SELECT * FROM rider_applicability"
        clauses: list[str] = []
        params: list[object] = []
        if rider_family_key:
            clauses.append("rider_family_key = ?")
            params.append(rider_family_key)
        if applies_to_family_key:
            clauses.append("applies_to_family_key = ?")
            params.append(applies_to_family_key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_rider_applicability(r) for r in rows]

    @staticmethod
    def _row_to_tariff_family(row) -> "TariffFamilyRecord":
        from duke_rates.models.tariff import TariffFamilyRecord
        return TariffFamilyRecord(
            id=int(row["id"]),
            family_key=row["family_key"],
            state=row["state"],
            company=row["company"],
            tariff_identifier=row["tariff_identifier"],
            schedule_code=row["schedule_code"],
            family_type=row["family_type"],
            title=row["title"],
            aliases=json.loads(row["aliases_json"] or "[]"),
            current_document_id=_coerce_optional_int(row["current_document_id"]),
            notes=row["notes"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_tariff_version(row) -> "TariffVersionRecord":
        from duke_rates.models.tariff import TariffVersionRecord
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        status = (row["status"] if "status" in keys and row["status"] else "approved")
        req_eff = row["requested_effective_date"] if "requested_effective_date" in keys else None
        approved_vid = row["approved_version_id"] if "approved_version_id" in keys else None
        return TariffVersionRecord(
            id=int(row["id"]),
            family_key=row["family_key"],
            document_id=row["document_id"],
            historical_document_id=row["historical_document_id"],
            effective_start=row["effective_start"],
            effective_end=row["effective_end"],
            revision_label=row["revision_label"],
            supersedes_label=row["supersedes_label"],
            docket_number=row["docket_number"],
            order_date=row["order_date"],
            leaf_no=row["leaf_no"],
            source_pdf=row["source_pdf"],
            docket_dir=row["docket_dir"],
            source_type=row["source_type"],
            confidence_score=float(row["confidence_score"]),
            notes=row["notes"],
            created_at=datetime.fromisoformat(row["created_at"]),
            status=status,
            requested_effective_date=req_eff,
            approved_version_id=approved_vid,
        )

    @staticmethod
    def _row_to_tariff_charge(row) -> "TariffChargeRecord":
        from duke_rates.models.tariff import TariffChargeRecord
        return TariffChargeRecord(
            id=int(row["id"]),
            version_id=int(row["version_id"]),
            family_key=row["family_key"],
            charge_type=row["charge_type"],
            charge_label=row["charge_label"],
            rate_value=float(row["rate_value"]) if row["rate_value"] is not None else None,
            rate_unit=row["rate_unit"],
            tier_min=float(row["tier_min"]) if row["tier_min"] is not None else None,
            tier_max=float(row["tier_max"]) if row["tier_max"] is not None else None,
            tou_period=row["tou_period"],
            season=row["season"],
            customer_class=row["customer_class"],
            source_snippet=row["source_snippet"],
            confidence_score=float(row["confidence_score"]),
            notes=row["notes"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_rider_applicability(row) -> "RiderApplicabilityRecord":
        from duke_rates.models.tariff import RiderApplicabilityRecord
        return RiderApplicabilityRecord(
            id=int(row["id"]),
            rider_family_key=row["rider_family_key"],
            applies_to_family_key=row["applies_to_family_key"],
            mandatory=bool(row["mandatory"]),
            enrollment_type=row["enrollment_type"] or "mandatory",
            in_rider_summary=bool(row["in_rider_summary"]) if row["in_rider_summary"] is not None else True,
            applicability_notes=row["applicability_notes"],
            effective_start=row["effective_start"],
            effective_end=row["effective_end"],
            source_type=row["source_type"],
            confidence_score=float(row["confidence_score"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
