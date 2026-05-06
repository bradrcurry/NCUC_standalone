from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from duke_rates.analytics import nc_confidence_audit, nc_redline_fingerprint_refresh, nc_redline_parse_audit
from duke_rates.db.sqlite import connect


class _FakeSignals:
    def __init__(
        self,
        *,
        is_redline: bool,
        confidence: float,
        signals: list[str] | None = None,
        red_text_samples: list[str] | None = None,
        strikethrough_samples: list[str] | None = None,
        red_is_index_only: bool = False,
    ) -> None:
        self.is_redline = is_redline
        self.confidence = confidence
        self.signals = signals or []
        self.red_text_samples = red_text_samples or []
        self.strikethrough_samples = strikethrough_samples or []
        self.annotation_types: list[str] = []
        self.red_is_index_only = red_is_index_only


def _seed_historical_document(
    conn,
    *,
    doc_id: int,
    family_key: str,
    local_path: str,
    start_page: int | None,
    end_page: int | None,
) -> None:
    now = datetime(2026, 4, 15, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO historical_documents (
            id, current_document_id, family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path, raw_text_path,
            content_hash, content_type, direct_status_code, direct_downloadable,
            revision_label, supersedes_label, leaf_no, effective_start, effective_end,
            retrieved_at, metadata_json, parsed_result_json, start_page, end_page, evidence_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            doc_id,
            None,
            family_key,
            family_key,
            "NC",
            "progress" if "progress" in family_key else "carolinas",
            "tariff",
            "pdf",
            f"https://example.com/{Path(local_path).name}",
            f"https://archive.example.com/{Path(local_path).name}",
            now,
            local_path,
            None,
            f"hash-{doc_id}",
            "application/pdf",
            200,
            1,
            None,
            None,
            "611",
            "2025-01-01",
            None,
            now,
            "{}",
            None,
            start_page,
            end_page,
            "{}",
        ),
    )


def _seed_tariff_version(conn, *, version_id: int, family_key: str, historical_document_id: int) -> None:
    now = datetime(2026, 4, 15, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code, family_type,
            title, aliases_json, current_document_id, notes, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            family_key,
            "NC",
            "progress" if "progress" in family_key else "carolinas",
            None,
            None,
            "rider",
            family_key,
            "[]",
            None,
            None,
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (
            id, family_key, document_id, historical_document_id, effective_start, effective_end,
            revision_label, supersedes_label, source_type, confidence_score, notes, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            version_id,
            family_key,
            None,
            historical_document_id,
            "2025-01-01",
            None,
            None,
            None,
            "historical_document",
            0.9,
            None,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO tariff_charges (
            version_id, family_key, charge_type, charge_label, rate_value, rate_unit,
            tier_min, tier_max, tou_period, season, customer_class, source_snippet,
            confidence_score, notes, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            version_id,
            family_key,
            "energy",
            "sample",
            1.23,
            "c/kWh",
            None,
            None,
            None,
            None,
            None,
            None,
            0.9,
            None,
            now,
        ),
    )


def test_refresh_creates_slice_fingerprint_and_persists_detector_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "test.db"
    pdf_path = tmp_path / "slice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    conn = connect(db_path)
    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-progress-leaf-611",
        local_path=str(pdf_path),
        start_page=3,
        end_page=4,
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        nc_redline_fingerprint_refresh,
        "detect_redline",
        lambda *args, **kwargs: _FakeSignals(
            is_redline=True,
            confidence=0.85,
            signals=["red_text_in_body=4", "p3:horizontal_lines=8"],
            red_text_samples=["First Second"],
            strikethrough_samples=["January 1, 2025"],
        ),
    )

    report = nc_redline_fingerprint_refresh.refresh_nc_redline_fingerprints(db_path)

    assert report["changed_fingerprint_rows"] == 1
    conn = connect(db_path)
    row = conn.execute(
        """
        SELECT page_start, page_end, is_redline_candidate, redline_confidence,
               redline_detector_version, redline_signals_json, red_text_samples_json,
               strikethrough_samples_json, red_is_index_only, review_flags_json
        FROM document_fingerprints
        WHERE source_pdf = ?
        """,
        (str(pdf_path),),
    ).fetchone()
    conn.close()

    assert row["page_start"] == 3
    assert row["page_end"] == 4
    assert row["is_redline_candidate"] == 1
    assert row["redline_confidence"] == 0.85
    assert row["redline_detector_version"] == "native_redline_v2"
    assert json.loads(row["redline_signals_json"]) == ["red_text_in_body=4", "p3:horizontal_lines=8"]
    assert json.loads(row["red_text_samples_json"]) == ["First Second"]
    assert json.loads(row["strikethrough_samples_json"]) == ["January 1, 2025"]
    assert row["red_is_index_only"] == 0
    assert "redline_candidate" in json.loads(row["review_flags_json"])


def test_parse_audit_prefers_exact_slice_fingerprint_over_path_rollup(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "test.db"
    pdf_path = tmp_path / "shared.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    conn = connect(db_path)
    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-progress-leaf-611",
        local_path=str(pdf_path),
        start_page=3,
        end_page=3,
    )
    _seed_tariff_version(conn, version_id=1, family_key="nc-progress-leaf-611", historical_document_id=1)
    now = datetime(2026, 4, 15, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO document_fingerprints (
            source_pdf, page_start, page_end, review_flags_json, metadata_json, created_at,
            is_redline_candidate, redline_confidence
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (str(pdf_path), None, None, "[]", "{}", now, 1, 0.85),
    )
    conn.execute(
        """
        INSERT INTO document_fingerprints (
            source_pdf, page_start, page_end, review_flags_json, metadata_json, created_at,
            is_redline_candidate, redline_confidence
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (str(pdf_path), 3, 3, "[]", "{}", now, 0, 0.0),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        nc_redline_parse_audit,
        "detect_redline",
        lambda *args, **kwargs: _FakeSignals(is_redline=False, confidence=0.0),
    )

    report = nc_redline_parse_audit.build_nc_redline_parse_audit(db_path)
    row = next(item for item in report["rows"] if item["version_id"] == 1)

    assert row["stored_is_redline"] == 0
    assert row["stored_redline_confidence"] == 0.0
    assert row["recommended_action"] == "likely_ok"


def test_confidence_audit_uses_exact_slice_fingerprint_without_double_counting(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    pdf_path = tmp_path / "shared.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    conn = connect(db_path)
    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-progress-leaf-611",
        local_path=str(pdf_path),
        start_page=3,
        end_page=3,
    )
    now = datetime(2026, 4, 15, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO document_fingerprints (
            source_pdf, page_start, page_end, review_flags_json, metadata_json, created_at,
            is_redline_candidate, redline_confidence
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (str(pdf_path), None, None, "[]", "{}", now, 1, 0.85),
    )
    conn.execute(
        """
        INSERT INTO document_fingerprints (
            source_pdf, page_start, page_end, review_flags_json, metadata_json, created_at,
            is_redline_candidate, redline_confidence
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (str(pdf_path), 3, 3, "[]", "{}", now, 0, 0.0),
    )
    conn.commit()
    conn.close()

    signals = nc_confidence_audit._load_document_signals(db_path)
    row = signals["nc-progress-leaf-611"]

    assert row["historical_doc_count"] == 1
    assert row["redline_doc_count"] == 0
    assert row["max_redline_confidence"] == 0.0
