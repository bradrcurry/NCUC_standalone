from __future__ import annotations

import sqlite3
from pathlib import Path

from duke_rates.analytics.nc_document_intelligence_audit import (
    build_nc_document_intelligence_audit,
)
from duke_rates.db.artifact_cache import save_page_artifacts
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.pipeline import PageEvidence


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "nc-document-intelligence-audit.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()
    return db_path


def test_document_intelligence_audit_flags_doc_family_for_canonicalization(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO historical_documents (
            id, current_document_id, family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path, content_hash,
            direct_downloadable, effective_start, retrieved_at, start_page, end_page
        ) VALUES (
            1, NULL, 'nc-carolinas-doc-SCHEDULEWC', 'Schedule WC - Residential Water Heating Service',
            'NC', 'carolinas', 'schedule', 'pdf',
            'https://example.test/sample.pdf', 'https://archive.test/sample.pdf', '2026-04-08T00:00:00Z',
            ?, 'hash-1', 1, '2013-11-01', '2026-04-08T00:00:00Z', 1, 1
        )
        """,
        (str(pdf_path),),
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (
            id, family_key, historical_document_id, effective_start, source_type, confidence_score, created_at
        ) VALUES (
            10, 'nc-carolinas-doc-SCHEDULEWC', 1, '2013-11-01', 'historical_document',
            0.9, '2026-04-08T00:00:00Z'
        )
        """
    )
    save_page_artifacts(
        conn,
        discovery_record_id=None,
        source_pdf=str(pdf_path),
        file_hash="hash-1",
        pages=[
            PageEvidence(
                page_number=1,
                text_length=80,
                text_content="Schedule WC\nMonthly Rate\nCustomer Charge 0.1234 $/kWh",
                has_leaf_header=True,
                has_schedule_heading=True,
            )
        ],
    )
    conn.commit()
    conn.close()

    report = build_nc_document_intelligence_audit(db_path)
    assert report["candidate_count"] == 1
    row = report["rows"][0]
    assert row["family_key"] == "nc-carolinas-doc-SCHEDULEWC"
    assert row["doc_type"] == "tariff_sheet"
    assert row["recommended_action"] == "canonicalize_family_key"


def test_document_intelligence_audit_flags_zero_charge_tariff_for_reparse(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    pdf_path = tmp_path / "leaf-501.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO historical_documents (
            id, current_document_id, family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path, content_hash,
            direct_downloadable, effective_start, retrieved_at, start_page, end_page
        ) VALUES (
            2, NULL, 'nc-progress-leaf-501', 'Residential Time-of-Use Demand',
            'NC', 'progress', 'schedule', 'pdf',
            'https://example.test/leaf-501.pdf', 'https://archive.test/leaf-501.pdf', '2026-04-08T00:00:00Z',
            ?, 'hash-2', 1, '2025-10-01', '2026-04-08T00:00:00Z', 1, 1
        )
        """,
        (str(pdf_path),),
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (
            id, family_key, historical_document_id, effective_start, source_type, confidence_score, created_at
        ) VALUES (
            20, 'nc-progress-leaf-501', 2, '2025-10-01', 'historical_document',
            0.9, '2026-04-08T00:00:00Z'
        )
        """
    )
    save_page_artifacts(
        conn,
        discovery_record_id=None,
        source_pdf=str(pdf_path),
        file_hash="hash-2",
        pages=[
            PageEvidence(
                page_number=1,
                text_length=120,
                text_content="Leaf No. 501\nSchedule R-TOUD\nCustomer Charge\nEnergy Charge\nDemand Charge",
                has_leaf_header=True,
                has_schedule_heading=True,
            )
        ],
    )
    conn.execute(
        """
        INSERT INTO historical_processing_runs (
            historical_document_id, source_pdf, family_key, content_hash, parser_stage,
            parser_profile, parser_version, processing_mode, status, outcome_quality, charge_count,
            started_at, completed_at
        ) VALUES (
            2, ?, 'nc-progress-leaf-501', 'hash-2', 'historical_bulk',
            'generic_residential', 'test', 'historical_bulk', 'empty', 'empty', 0,
            '2026-04-08T00:00:00Z', '2026-04-08T00:00:00Z'
        )
        """,
        (str(pdf_path),),
    )
    conn.commit()
    conn.close()

    report = build_nc_document_intelligence_audit(db_path)
    assert report["candidate_count"] == 1
    row = report["rows"][0]
    assert row["family_key"] == "nc-progress-leaf-501"
    assert row["doc_type"] == "tariff_sheet"
    assert row["recommended_action"] == "inspect_and_reparse"
