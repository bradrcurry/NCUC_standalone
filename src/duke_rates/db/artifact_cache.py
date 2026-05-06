from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from duke_rates.historical.ncuc.pipeline.stage_versions import (
    PAGE_ARTIFACT_VERSION,
    SPAN_ARTIFACT_VERSION,
)
from duke_rates.models.pipeline import (
    DateCandidate,
    DocumentPageArtifact,
    DocumentSpanArtifact,
    PageEvidence,
    TariffSpan,
)



def save_page_artifacts(
    conn: sqlite3.Connection,
    *,
    discovery_record_id: int | None,
    source_pdf: str,
    file_hash: str | None,
    pages: list[PageEvidence],
    artifact_version: str = PAGE_ARTIFACT_VERSION,
    metadata: dict | None = None,
) -> int:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        DELETE FROM ncuc_page_artifacts
        WHERE source_pdf = ? AND file_hash IS ? AND artifact_version = ?
        """,
        (source_pdf, file_hash, artifact_version),
    )
    inserted = 0
    for page in pages:
        artifact = DocumentPageArtifact(
            discovery_record_id=discovery_record_id,
            source_pdf=source_pdf,
            file_hash=file_hash,
            artifact_version=artifact_version,
            page_number=page.page_number,
            text_length=page.text_length,
            text_content=page.text_content,
            metadata={
                "has_leaf_header": page.has_leaf_header,
                "has_revised_header": page.has_revised_header,
                "has_schedule_heading": page.has_schedule_heading,
                "tariff_vocab_density": page.tariff_vocab_density,
                "procedural_vocab_density": page.procedural_vocab_density,
                "numeric_density": page.numeric_density,
                "table_like_density": page.table_like_density,
                "header_candidates": page.header_candidates,
                "footer_candidates": page.footer_candidates,
                "has_effective_date_phrase": page.has_effective_date_phrase,
                "has_docket_phrase": page.has_docket_phrase,
                "extracted_leaf_nos": page.extracted_leaf_nos,
                "extracted_schedule_codes": page.extracted_schedule_codes,
                **(metadata or {}),
            },
        )
        conn.execute(
            """
            INSERT INTO ncuc_page_artifacts (
                discovery_record_id, source_pdf, file_hash, artifact_version,
                page_number, text_length, text_content, metadata_json, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                artifact.discovery_record_id,
                artifact.source_pdf,
                artifact.file_hash,
                artifact.artifact_version,
                artifact.page_number,
                artifact.text_length,
                artifact.text_content,
                json.dumps(artifact.metadata, sort_keys=True),
                now,
                now,
            ),
        )
        inserted += 1
    return inserted


def load_page_artifacts(
    conn: sqlite3.Connection,
    *,
    source_pdf: str,
    file_hash: str | None,
    artifact_version: str = PAGE_ARTIFACT_VERSION,
) -> list[PageEvidence]:
    rows = conn.execute(
        """
        SELECT page_number, text_length, text_content, metadata_json
        FROM ncuc_page_artifacts
        WHERE source_pdf = ? AND file_hash IS ? AND artifact_version = ?
        ORDER BY page_number
        """,
        (source_pdf, file_hash, artifact_version),
    ).fetchall()
    pages: list[PageEvidence] = []
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        pages.append(
            PageEvidence(
                page_number=row["page_number"],
                text_length=row["text_length"],
                text_content=row["text_content"],
                has_leaf_header=bool(metadata.get("has_leaf_header")),
                has_revised_header=bool(metadata.get("has_revised_header")),
                has_schedule_heading=bool(metadata.get("has_schedule_heading")),
                tariff_vocab_density=float(metadata.get("tariff_vocab_density") or 0.0),
                procedural_vocab_density=float(metadata.get("procedural_vocab_density") or 0.0),
                numeric_density=float(metadata.get("numeric_density") or 0.0),
                table_like_density=float(metadata.get("table_like_density") or 0.0),
                header_candidates=list(metadata.get("header_candidates") or []),
                footer_candidates=list(metadata.get("footer_candidates") or []),
                has_effective_date_phrase=bool(metadata.get("has_effective_date_phrase")),
                has_docket_phrase=bool(metadata.get("has_docket_phrase")),
                extracted_leaf_nos=list(metadata.get("extracted_leaf_nos") or []),
                extracted_schedule_codes=list(metadata.get("extracted_schedule_codes") or []),
            )
        )
    return pages


def save_span_artifacts(
    conn: sqlite3.Connection,
    *,
    discovery_record_id: int | None,
    source_pdf: str,
    file_hash: str | None,
    spans: list[TariffSpan],
    artifact_version: str = SPAN_ARTIFACT_VERSION,
    metadata: dict | None = None,
) -> int:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        DELETE FROM ncuc_span_artifacts
        WHERE source_pdf = ? AND file_hash IS ? AND artifact_version = ?
        """,
        (source_pdf, file_hash, artifact_version),
    )
    inserted = 0
    for span_index, span in enumerate(spans):
        artifact = DocumentSpanArtifact(
            discovery_record_id=discovery_record_id,
            source_pdf=source_pdf,
            file_hash=file_hash,
            artifact_version=artifact_version,
            span_index=span_index,
            start_page=span.start_page,
            end_page=span.end_page,
            doc_type=span.doc_type,
            confidence=span.confidence,
            extracted_leaf_nos=sorted(span.extracted_leaf_nos),
            extracted_schedule_titles=sorted(span.extracted_schedule_titles),
            header_footer_snippets=list(span.header_footer_snippets),
            dates=list(span.dates),
            evidence_score_breakdown=dict(span.evidence_score_breakdown),
            metadata=metadata or {},
        )
        conn.execute(
            """
            INSERT INTO ncuc_span_artifacts (
                discovery_record_id, source_pdf, file_hash, artifact_version, span_index,
                start_page, end_page, doc_type, confidence, extracted_leaf_nos_json,
                extracted_schedule_titles_json, header_footer_snippets_json, dates_json,
                evidence_score_breakdown_json, metadata_json, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                artifact.discovery_record_id,
                artifact.source_pdf,
                artifact.file_hash,
                artifact.artifact_version,
                artifact.span_index,
                artifact.start_page,
                artifact.end_page,
                artifact.doc_type,
                artifact.confidence,
                json.dumps(artifact.extracted_leaf_nos),
                json.dumps(artifact.extracted_schedule_titles),
                json.dumps(artifact.header_footer_snippets),
                json.dumps([date.model_dump(mode="json") for date in artifact.dates]),
                json.dumps(artifact.evidence_score_breakdown, sort_keys=True),
                json.dumps(artifact.metadata, sort_keys=True),
                now,
                now,
            ),
        )
        inserted += 1
    return inserted


def load_span_artifacts(
    conn: sqlite3.Connection,
    *,
    source_pdf: str,
    file_hash: str | None,
    artifact_version: str = SPAN_ARTIFACT_VERSION,
) -> list[TariffSpan]:
    rows = conn.execute(
        """
        SELECT start_page, end_page, doc_type, confidence, extracted_leaf_nos_json,
               extracted_schedule_titles_json, header_footer_snippets_json, dates_json,
               evidence_score_breakdown_json
        FROM ncuc_span_artifacts
        WHERE source_pdf = ? AND file_hash IS ? AND artifact_version = ?
        ORDER BY span_index
        """,
        (source_pdf, file_hash, artifact_version),
    ).fetchall()
    spans: list[TariffSpan] = []
    for row in rows:
        spans.append(
            TariffSpan(
                start_page=row["start_page"],
                end_page=row["end_page"],
                doc_type=row["doc_type"],
                confidence=float(row["confidence"] or 0.0),
                extracted_leaf_nos=set(json.loads(row["extracted_leaf_nos_json"] or "[]")),
                extracted_schedule_titles=set(json.loads(row["extracted_schedule_titles_json"] or "[]")),
                header_footer_snippets=list(json.loads(row["header_footer_snippets_json"] or "[]")),
                dates=[
                    DateCandidate.model_validate(item)
                    for item in json.loads(row["dates_json"] or "[]")
                ],
                evidence_score_breakdown=dict(json.loads(row["evidence_score_breakdown_json"] or "{}")),
            )
        )
    return spans


def save_docling_artifact(
    conn: sqlite3.Connection,
    *,
    discovery_record_id: int | None,
    source_pdf: str,
    file_hash: str | None,
    backend_version: str,
    accelerator: str,
    pipeline: str | None = None,
    status: str,
    json_sidecar_path: str | None,
    text_sidecar_path: str | None,
    tables_sidecar_path: str | None,
    doc_json_content: str | None = None,
    plain_text_content: str | None = None,
    tables_json_content: str | None = None,
    page_count: int,
    conversion_confidence: float | None,
    table_count: int,
    metadata: dict | None = None,
) -> int:
    """Upsert a Docling artifact record. Returns the row id."""
    now = datetime.now(UTC).isoformat()
    existing = conn.execute(
        """
        SELECT id FROM docling_artifacts
        WHERE source_pdf = ? AND file_hash IS ? AND backend_version = ? AND accelerator = ?
        ORDER BY id DESC LIMIT 1
        """,
        (source_pdf, file_hash, backend_version, accelerator),
    ).fetchone()
    meta_json = json.dumps(metadata or {}, sort_keys=True)
    if existing:
        conn.execute(
            """
            UPDATE docling_artifacts
            SET discovery_record_id=?, status=?, pipeline=?,
                json_sidecar_path=?, text_sidecar_path=?, tables_sidecar_path=?,
                doc_json_content=?, plain_text_content=?, tables_json_content=?,
                page_count=?, conversion_confidence=?, table_count=?,
                metadata_json=?, updated_at=?
            WHERE id=?
            """,
            (
                discovery_record_id,
                status,
                pipeline,
                json_sidecar_path,
                text_sidecar_path,
                tables_sidecar_path,
                doc_json_content,
                plain_text_content,
                tables_json_content,
                page_count,
                conversion_confidence,
                table_count,
                meta_json,
                now,
                existing["id"],
            ),
        )
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO docling_artifacts (
            discovery_record_id, source_pdf, file_hash, backend_version, accelerator,
            pipeline, status, json_sidecar_path, text_sidecar_path, tables_sidecar_path,
            doc_json_content, plain_text_content, tables_json_content,
            page_count, conversion_confidence, table_count, metadata_json,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            discovery_record_id,
            source_pdf,
            file_hash,
            backend_version,
            accelerator,
            pipeline,
            status,
            json_sidecar_path,
            text_sidecar_path,
            tables_sidecar_path,
            doc_json_content,
            plain_text_content,
            tables_json_content,
            page_count,
            conversion_confidence,
            table_count,
            meta_json,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def load_docling_artifact(
    conn: sqlite3.Connection,
    *,
    source_pdf: str,
    file_hash: str | None,
    backend_version: str,
    accelerator: str,
) -> dict | None:
    """Load the most recent Docling artifact row for the given key, or None."""
    row = conn.execute(
        """
        SELECT id, discovery_record_id, source_pdf, file_hash, backend_version,
               accelerator, pipeline, status, json_sidecar_path, text_sidecar_path,
               tables_sidecar_path, doc_json_content, plain_text_content,
               tables_json_content, page_count, conversion_confidence,
               table_count, metadata_json, created_at, updated_at
        FROM docling_artifacts
        WHERE source_pdf = ? AND file_hash IS ? AND backend_version = ? AND accelerator = ?
        ORDER BY id DESC LIMIT 1
        """,
        (source_pdf, file_hash, backend_version, accelerator),
    ).fetchone()
    if row is None:
        return None
    return dict(row)
