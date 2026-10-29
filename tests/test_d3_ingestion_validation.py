"""Phase 2E D3: shared source validation on every promotion pathway.

A contaminated candidate (redline source pages, a redline-duplicate assertion,
an effective date its pages do not print, source bytes changed after
extraction) must not reach ``tariff_charges`` / ``rider_line_items`` through
the bulk extractor's legacy replace, the NCUC loader's legacy replace, a direct
``provenance.promote`` call, or the reviewer CLI without a recorded override.
Clean sources promote exactly as before. Scratch databases only.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from duke_rates.db import provenance as pv
from duke_rates.db import rider_summary_provenance as rsp
from duke_rates.db.ncuc_loader import persist_rider_summary_records
from duke_rates.historical.ncuc.ingestion_validation import IngestionValidationError
from test_bulk_candidate_pipeline import _doc, _extractor
from test_extraction_provenance import _charges, db, migrated, write_clean_sheet  # noqa: F401  (fixtures)
from test_rider_summary_versioning import PDF as RIDER_PDF
from test_rider_summary_versioning import _live, _record
from test_rider_summary_versioning import conn as rider_conn  # noqa: F401  (fixture)
from test_rider_summary_versioning import source_pdf  # noqa: F401  (autouse fixture)

REDLINE = Path(__file__).parent / "fixtures" / "d2_redline" / "hd2275_leaf600_redline_p100.pdf"


def _use_source(conn, ids, src: Path, dest: Path) -> None:
    """Bind the fixture's historical document to ``src`` (copied), with its true hash."""
    shutil.copyfile(src, dest)
    conn.execute("UPDATE historical_documents SET local_path = ?, content_hash = ?, start_page = 1, end_page = 1 "
                 "WHERE id = ?", (str(dest), hashlib.sha256(dest.read_bytes()).hexdigest(), ids["hd_id"]))
    conn.commit()


def _validations(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM promotion_validations ORDER BY id")]


def _candidate(path, ids, pdf, **doc) -> int:
    result = _extractor(path).process_document_result(_doc(ids, pdf) | doc)
    assert result.persistence == "candidate" and result.run_id
    return result.run_id


def test_clean_source_promotes_and_records_a_pass(migrated):
    conn, ids, path, pdf = migrated
    result = _extractor(path).process_document_result(_doc(ids, pdf), legacy_replace=True)
    assert result.error is None and result.inserted == 2
    (row,) = _validations(conn)
    assert row["verdict"] == "pass" and row["pathway"] == "automated:bulk_extractor.legacy_replace"
    assert json.loads(row["checks_json"])["bound_pages_not_redline"]["ok"] is True
    assert result.promotion["validation"]["verdict"] == "pass"


def test_bulk_legacy_replace_cannot_promote_a_redline_source(migrated, tmp_path):
    conn, ids, path, pdf = migrated
    _use_source(conn, ids, REDLINE, tmp_path / "redline.pdf")
    before = _charges(conn, ids["version_id"])
    result = _extractor(path).process_document_result(_doc(ids, tmp_path / "redline.pdf"), legacy_replace=True)
    assert result.inserted == 0 and "bound_pages_not_redline" in (result.error or "")
    assert _charges(conn, ids["version_id"]) == before  # nothing struck reached tariff_charges
    (row,) = _validations(conn)
    assert row["verdict"] == "blocked" and json.loads(row["failed_json"]) == ["bound_pages_not_redline"]


def test_direct_promote_fails_closed_and_refuses_overrides(migrated, tmp_path):
    conn, ids, path, pdf = migrated
    _use_source(conn, ids, REDLINE, tmp_path / "redline.pdf")
    run_id = _candidate(path, ids, tmp_path / "redline.pdf")
    before = _charges(conn, ids["version_id"])
    with pytest.raises(IngestionValidationError, match="bound_pages_not_redline"):
        pv.promote(conn, run_id, promoted_by="script", reason="r")
    with pytest.raises(pv.ProvenanceError, match="only on the reviewed pathway"):
        pv.promote(conn, run_id, promoted_by="script", reason="r", validation_overrides=["bound_pages_not_redline"])
    with pytest.raises(pv.ProvenanceError, match="non-overridable"):
        pv.promote(conn, run_id, promoted_by="alice", reason="r", pathway="reviewed",
                   validation_overrides=["bound_pages_in_file"])
    assert _charges(conn, ids["version_id"]) == before
    # A dry run reports the validation and changes nothing.
    dry = pv.promote(conn, run_id, promoted_by="alice", reason="r", dry_run=True)
    assert dry["validation"]["verdict"] == "blocked"


def test_reviewed_override_is_named_and_recorded(migrated, tmp_path):
    conn, ids, path, pdf = migrated
    _use_source(conn, ids, REDLINE, tmp_path / "redline.pdf")
    run_id = _candidate(path, ids, tmp_path / "redline.pdf")
    out = pv.promote(conn, run_id, promoted_by="alice", reason="struck rows removed by hand; see review #4",
                     pathway="reviewed", validation_overrides=["bound_pages_not_redline"])
    assert out["validation"]["verdict"] == "overridden"
    row = _validations(conn)[-1]
    assert (row["pathway"], row["requested_by"], json.loads(row["overrides_json"])) == (
        "reviewed", "alice", ["bound_pages_not_redline"])


def test_redline_duplicate_assertion_blocks_unattended_promotion(migrated):
    conn, ids, path, pdf = migrated
    conn.execute("INSERT INTO document_assertions (historical_document_id, assertion, target_historical_document_id, "
                 "asserted_by, asserted_at) VALUES (?, 'redline_duplicate_of', 99, 'r', 't')", (ids["hd_id"],))
    conn.commit()
    result = _extractor(path).process_document_result(_doc(ids, pdf), legacy_replace=True)
    assert result.inserted == 0 and "no_redline_duplicate_assertion" in result.error


def test_effective_date_not_printed_blocks_unattended_promotion(migrated):
    conn, ids, path, pdf = migrated
    # The run reads 2025-09-01 (as hd 7265's did); the page prints October 1, 2024.
    conn.execute("UPDATE historical_documents SET effective_start = '2025-09-01' WHERE id = ?", (ids["hd_id"],))
    conn.commit()
    result = _extractor(path).process_document_result(_doc(ids, pdf) | {"effective_start": "2025-09-01"},
                                                      legacy_replace=True)
    assert result.inserted == 0 and "effective_date_printed" in result.error
    detail = json.loads(_validations(conn)[-1]["checks_json"])["effective_date_printed"]["detail"]
    assert detail["printed_on_bound_pages"] == ["2024-10-01"]


def test_source_rewritten_after_extraction_blocks(migrated):
    conn, ids, path, pdf = migrated
    run_id = _candidate(path, ids, pdf)
    write_clean_sheet(pdf, "NC Second Revised Leaf No. 500\nEffective October 1, 2024")  # different bytes
    with pytest.raises(IngestionValidationError, match="source_hash_matches"):
        pv.promote(conn, run_id, promoted_by="script", reason="r")


def test_missing_source_blocks_unattended_but_only_warns_a_reviewer(migrated):
    conn, ids, path, pdf = migrated
    run_id = _candidate(path, ids, pdf)
    pdf.unlink()
    with pytest.raises(IngestionValidationError, match="source_present"):
        pv.promote(conn, run_id, promoted_by="script", reason="r")
    out = pv.promote(conn, run_id, promoted_by="alice", reason="reviewed diff", pathway="reviewed")
    assert out["validation"]["verdict"] == "pass"
    assert json.loads(_validations(conn)[-1]["checks_json"])["source_present"]["ok"] is False


def test_web_document_version_is_not_applicable(migrated):
    conn, ids, path, pdf = migrated
    run_id = _candidate(path, ids, pdf)
    conn.execute("UPDATE tariff_versions SET historical_document_id = NULL, document_id = 1 WHERE id = ?",
                 (ids["version_id"],))
    conn.commit()
    out = pv.promote(conn, run_id, promoted_by="script", reason="r")
    assert out["validation"]["verdict"] == "not_applicable"


def test_reviewer_cli_refuses_redline_then_records_the_acknowledgement(migrated, tmp_path):
    from duke_rates.cli import app

    conn, ids, path, pdf = migrated
    _use_source(conn, ids, REDLINE, tmp_path / "redline.pdf")
    run_id = _candidate(path, ids, tmp_path / "redline.pdf")
    base = ["provenance", "promote", "--db", str(path), "--run-id", str(run_id), "--by", "alice",
            "--reason", "reviewed diff"]
    refused = CliRunner().invoke(app, base)
    assert refused.exit_code != 0 and "bound_pages_not_redline" in refused.output
    done = CliRunner().invoke(app, base + ["--acknowledge-redline"])
    assert done.exit_code == 0, done.output
    assert _validations(conn)[-1]["verdict"] == "overridden"


def test_rider_loader_legacy_replace_cannot_promote_redline_evidence_pages(rider_conn, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DUKE_RATES_LEGACY_REPLACE", "1")
    shutil.copyfile(REDLINE, RIDER_PDF)   # the evidence page (p.1) is now a redline
    before = _live(rider_conn)
    with pytest.raises(IngestionValidationError, match="bound_pages_not_redline"):
        persist_rider_summary_records(rider_conn, [_record(storm=0.300, total=0.800)], replace=True)
    assert _live(rider_conn) == before


def test_rider_run_without_evidence_pages_fails_closed_unattended(rider_conn):  # noqa: F811
    record = _record(storm=0.300, total=0.800)
    for rc in record["rate_classes"]:
        rc.pop("evidence", None)
    run_id = rsp.record_rider_summary_runs(rider_conn, [record], created_by="pytest")[0]
    with pytest.raises(IngestionValidationError, match="evidence_pages_known"):
        pv.promote(rider_conn, run_id, promoted_by="script", reason="r")
    out = pv.promote(rider_conn, run_id, promoted_by="alice", reason="reviewed", pathway="reviewed")
    assert out["validation"]["verdict"] == "pass"


def test_box_border_under_black_text_is_suspect_not_redline(tmp_path):
    """D3 audit false positive: a blue box edge under a black workpaper heading read as an insertion."""
    import fitz

    from duke_rates.parse.redline_detector import evaluate_redline_page

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "DEMAND SIDE MANAGEMENT - DEFERRED COSTS", fontsize=10)   # black text
    page.draw_rect(fitz.Rect(70, 101.5, 300, 102.5), color=(0, 0, 1), fill=(0, 0, 1))       # blue bar beneath
    ev = evaluate_redline_page(page, 1)
    assert ev.inserted_spans == 1 and ev.colored_spans == 0 and ev.verdict == "suspect"
    page.insert_text((72, 140), "Second Revised", fontsize=10, color=(1, 0, 0))               # coloured insertion
    page.draw_rect(fitz.Rect(70, 141.5, 160, 142.5), color=(1, 0, 0), fill=(1, 0, 0))
    assert evaluate_redline_page(page, 1).verdict == "redline"
    doc.close()


def test_version_source_redline_for_writers_without_a_run(migrated, tmp_path):
    """The LLM charge promotion path adds rows without a run; it asks the same page checks first."""
    from duke_rates.historical.ncuc.ingestion_validation import version_source_redline

    conn, ids, path, pdf = migrated
    assert version_source_redline(conn, ids["version_id"]) is None             # clean sheet
    _use_source(conn, ids, REDLINE, tmp_path / "redline.pdf")
    found = version_source_redline(conn, ids["version_id"])
    assert found and "bound_pages_not_redline" in found["failed"]
    (tmp_path / "redline.pdf").unlink()
    assert version_source_redline(conn, ids["version_id"]) is None             # unreadable: left to that writer
