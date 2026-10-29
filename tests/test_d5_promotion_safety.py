"""Source-integrity enforcement uses real temporary PDFs and scratch databases."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import fitz
import pytest
from test_d3_ingestion_validation import _candidate, _charges, _use_source
from test_extraction_provenance import db, migrated  # noqa: F401
from test_llm_charge_promotion import _init_db, _insert_validated_row
from typer.testing import CliRunner

from duke_rates.db import provenance as pv
from duke_rates.document_intelligence.llm_charge_promotion import (
    promote_llm_charge_proposals,
    propose_llm_charge_promotions,
)
from duke_rates.historical.ncuc.ingestion_validation import IngestionValidationError

FIXTURES = Path(__file__).parent / "fixtures"


def _llm_db(tmp_path, case):
    path = tmp_path / "llm.db"
    pdf = tmp_path / "source.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), "Energy Charge 6.0370 per on-peak kWh")
        if case == "redline":
            page.add_strikeout_annot(page.search_for("6.0370"))
        doc.save(pdf)
    conn = _init_db(path)
    _insert_validated_row(conn)
    conn.execute("ALTER TABLE tariff_versions ADD COLUMN document_id INTEGER")
    conn.execute(
        "CREATE TABLE historical_documents (id INTEGER PRIMARY KEY, local_path TEXT, "
        "content_hash TEXT, start_page INTEGER, end_page INTEGER)"
    )
    conn.execute(
        "CREATE TABLE document_fingerprints (source_pdf TEXT, redline_confidence REAL, "
        "redline_detector_version TEXT, is_redline_candidate INTEGER, red_is_index_only INTEGER, "
        "page_start INTEGER, page_end INTEGER)"
    )
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    conn.execute("INSERT INTO historical_documents VALUES (100, ?, ?, 1, 1)", (str(pdf), sha))
    conn.execute("UPDATE llm_candidate_rate_extractions SET source_pdf = ?", (str(pdf),))
    conn.commit()
    conn.close()
    propose_llm_charge_promotions(path, execute=True)
    conn = sqlite3.connect(path)
    if case == "missing":
        pdf.unlink()
    elif case == "changed_hash":
        conn.execute("UPDATE historical_documents SET content_hash = ?", ("0" * 64,))
    elif case == "unverifiable_hash":
        conn.execute("UPDATE historical_documents SET content_hash = 'portal:legacy-id'")
    elif case == "wrong_span":
        conn.execute("UPDATE llm_candidate_rate_extractions SET historical_document_id = 101")
    elif case == "wrong_bounds":
        conn.execute("UPDATE historical_documents SET end_page = 2")
    elif case == "missing_binding":
        conn.execute("DELETE FROM historical_documents")
    elif case == "wrong_evidence":
        other = tmp_path / "other.pdf"
        with fitz.open() as doc:
            doc.new_page().insert_text((72, 100), "Another source")
            doc.save(other)
        conn.execute("UPDATE llm_candidate_rate_extractions SET source_pdf = ?", (str(other),))
    elif case == "image_only":
        with fitz.open(FIXTURES / "d5_redline/17.pdf") as doc:
            doc.save(pdf)
        conn.execute(
            "UPDATE historical_documents SET content_hash = ?",
            (hashlib.sha256(pdf.read_bytes()).hexdigest(),),
        )
    conn.commit()
    conn.close()
    return path


@pytest.mark.parametrize(
    "case, failed",
    [
        ("missing", "source_present"),
        ("changed_hash", "source_hash_matches"),
        ("unverifiable_hash", "archive_hash_verifiable"),
        ("wrong_span", "evidence_document_matches"),
        ("wrong_bounds", "bound_pages_in_file"),
        ("missing_binding", "source_present"),
        ("wrong_evidence", "evidence_source_matches"),
        ("redline", "bound_pages_not_redline"),
        ("image_only", "bound_pages_readable"),
    ],
)
def test_llm_unattended_write_refuses_incomplete_or_unsafe_source(tmp_path, case, failed):
    path = _llm_db(tmp_path, case)
    dry = promote_llm_charge_proposals(path, execute=False)
    assert dry["summary"]["promoted"] == 0
    assert failed in dry["skipped"][0]["failed"]
    result = promote_llm_charge_proposals(path, execute=True)
    assert result["summary"]["promoted"] == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tariff_charges").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM llm_promoted_charge_audit").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT promotion_status FROM llm_rate_charge_promotion_proposals"
            ).fetchone()[0]
            == "skipped_source_integrity"
        )


def test_llm_valid_readable_source_preserves_promotion_audit(tmp_path):
    path = _llm_db(tmp_path, "clean")
    result = promote_llm_charge_proposals(path, execute=True)
    assert result["summary"]["promoted"] == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT rate_value FROM tariff_charges").fetchone()[0] == 6.037
        assert conn.execute("SELECT COUNT(*) FROM llm_promoted_charge_audit").fetchone()[0] == 1


def test_image_source_needs_explicit_reviewed_override(migrated, tmp_path):  # noqa: F811
    conn, ids, path, _ = migrated
    pdf = tmp_path / "image.pdf"
    _use_source(conn, ids, FIXTURES / "d5_redline/17.pdf", pdf)
    run_id = _candidate(path, ids, pdf)
    before = _charges(conn, ids["version_id"])
    with pytest.raises(IngestionValidationError, match="bound_pages_readable"):
        pv.promote(conn, run_id, promoted_by="automated:test", reason="test")
    assert _charges(conn, ids["version_id"]) == before
    # Do not execute an override: a rendered page review remains required.


def test_retired_proposal_confirmation_refuses_before_settings_or_database(monkeypatch):
    from duke_rates.cli_commands import doc_intel

    def forbidden():
        raise AssertionError("confirmation must be rejected before database settings are loaded")

    monkeypatch.setattr(doc_intel, "get_settings", forbidden)
    result = CliRunner().invoke(
        doc_intel.doc_intel_app,
        ["promote-proposed-tariffs", "--docket-number", "E-2 Sub 1380", "--confirm"],
    )
    assert result.exit_code == 2
    assert "disabled" in result.output and "--confirm" in result.output
