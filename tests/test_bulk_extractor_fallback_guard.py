"""Tests for the generic_residential cross-attribution guard in
BulkExtractor._should_apply_fallback.

Regression history:
  - 2026-05-16: hd_id=14 (JAA compliance book, no span) and hd_id=1847 (RDM
    compliance book, no span) had family-specific initial profiles return 0
    charges and the fallback unconditionally accepted generic_residential,
    which harvested 55/64 charges from a multi-schedule rate matrix and
    attributed them to the wrong family. The initial guard refused
    generic_residential fallback only when the doc had no page bounds.
  - 2026-05-20: hd_id=179 (leaf-602 JAA proposed order, bounded span 1-6)
    still produced 9 garbage charges via generic_residential fallback —
    narrative proposed-order text like "Duke Energy Progress filed an
    application... on June" parsed as "$0.49/kWh". The guard was broadened
    to refuse generic_residential fallback whenever the initial profile is
    family-specific, regardless of span boundedness.
"""

from __future__ import annotations

import pytest

from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor


METRICS_EMPTY = {
    "unique_charge_types": 0,
    "tou_period_count": 0,
    "season_count": 0,
    "completeness_score": 0,
}
METRICS_GENERIC_RES = {
    "unique_charge_types": 3,
    "tou_period_count": 2,
    "season_count": 1,
    "completeness_score": 5,
}


@pytest.fixture
def extractor(tmp_path):
    db_path = tmp_path / "stub.db"
    return BulkExtractor(db_path=str(db_path))


def test_unbounded_fallback_to_generic_residential_is_blocked(extractor):
    """When start_page is None and initial profile is family-specific,
    fallback to generic_residential must be refused even if it would
    produce charges. Regression: hd_id=14 / hd_id=1847."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="progress_jaa_rider",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="generic_residential",
        candidate_charge_count=55,
        candidate_outcome_quality="weak",
        candidate_metrics=METRICS_GENERIC_RES,
        has_page_bounds=False,
    )
    assert should is False
    assert reason is None


def test_bounded_fallback_to_generic_residential_is_also_blocked(extractor):
    """2026-05-20 broadening: even with bounded spans, generic_residential's
    broad rate-shaped-text regex will harvest narrative mentions and attribute
    them to a non-residential family. Regression: hd_id=179 leaf-602 JAA
    proposed-order doc (span 1-6) produced 9 garbage charges via this path."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="progress_jaa_rider",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="generic_residential",
        candidate_charge_count=9,
        candidate_outcome_quality="weak",
        candidate_metrics=METRICS_GENERIC_RES,
        has_page_bounds=True,
    )
    assert should is False
    assert reason is None


def test_unbounded_fallback_to_specific_profile_still_allowed(extractor):
    """The guard targets only generic_residential. Family-specific fallbacks
    (e.g. progress_single_value_rider) are still allowed even without span
    boundaries — they have their own family-key gates that prevent
    cross-schedule pollution."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="progress_jaa_rider",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="progress_single_value_rider",
        candidate_charge_count=3,
        candidate_outcome_quality="strong",
        candidate_metrics=METRICS_GENERIC_RES,
        has_page_bounds=False,
    )
    assert should is True
    assert reason == "empty_initial_parse"


def test_unbounded_generic_to_generic_not_blocked(extractor):
    """If the initial profile was also generic_residential, the guard does
    not apply — there's no cross-family attribution to worry about."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="generic_residential",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="generic_residential",
        candidate_charge_count=5,
        candidate_outcome_quality="strong",
        candidate_metrics=METRICS_GENERIC_RES,
        has_page_bounds=False,
    )
    assert should is True
    assert reason == "empty_initial_parse"


def test_unknown_initial_profile_still_allows_generic_residential_fallback(extractor):
    """When the initial profile is `unknown` (no family-specific match), the
    guard does not fire — generic_residential is the intended salvage path
    for unclassified docs that happen to contain residential rate patterns."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="unknown",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="generic_residential",
        candidate_charge_count=5,
        candidate_outcome_quality="strong",
        candidate_metrics=METRICS_GENERIC_RES,
    )
    assert should is True
    assert reason == "empty_initial_parse"


def test_insert_charges_force_clear_deletes_stale_when_no_new_charges(tmp_path):
    """2026-05-20 force_clear: when re-extraction now produces 0 charges
    (because the broadened guard refuses a previously-polluting fallback),
    the old polluted charges must be deletable in the same run. Without
    force_clear, insert_charges returns early on empty input and leaves
    stale rows in tariff_charges. With force_clear=True, the DELETE runs
    even when charges is empty."""
    import sqlite3
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    db_path = tmp_path / "force_clear.db"
    # Minimal schema for the test — just the table insert_charges writes to.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tariff_charges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id INTEGER, family_key TEXT, charge_type TEXT, charge_label TEXT,
            rate_value REAL, rate_unit TEXT, tier_min REAL, tier_max REAL,
            tou_period TEXT, season TEXT, source_snippet TEXT,
            confidence_score REAL, created_at TEXT
        );
        INSERT INTO tariff_charges (version_id, family_key, charge_label, rate_value)
        VALUES (1, 'nc-progress-leaf-602', 'Polluted Energy Block', 0.005);
        INSERT INTO tariff_charges (version_id, family_key, charge_label, rate_value)
        VALUES (1, 'nc-progress-leaf-602', 'Polluted Energy Block', 0.007);
        """
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(db_path=str(db_path))

    # Without force_clear: 0 new charges → no DELETE, old rows survive
    inserted = extractor.insert_charges(version_id=1, family_key="nc-progress-leaf-602", charges=[])
    assert inserted == 0
    check = sqlite3.connect(db_path)
    count = check.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id=1").fetchone()[0]
    check.close()
    assert count == 2, f"expected stale charges to survive without force_clear; got {count}"

    # With force_clear=True: 0 new charges → DELETE still runs, old rows cleared
    inserted = extractor.insert_charges(
        version_id=1, family_key="nc-progress-leaf-602", charges=[], force_clear=True
    )
    assert inserted == 0
    check = sqlite3.connect(db_path)
    count = check.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id=1").fetchone()[0]
    check.close()
    assert count == 0, f"expected stale charges cleared with force_clear; got {count}"


def test_insert_charges_force_clear_no_op_when_version_id_unset(tmp_path):
    """force_clear must not bypass the version_id guard. Without a
    version_id we don't know which rows to clear and a wildcard DELETE
    would be catastrophic — must return 0 unchanged."""
    import sqlite3
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    db_path = tmp_path / "force_clear_noversion.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tariff_charges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id INTEGER, family_key TEXT, charge_type TEXT, charge_label TEXT,
            rate_value REAL, rate_unit TEXT, tier_min REAL, tier_max REAL,
            tou_period TEXT, season TEXT, source_snippet TEXT,
            confidence_score REAL, created_at TEXT
        );
        INSERT INTO tariff_charges (version_id, family_key, charge_label, rate_value)
        VALUES (1, 'some-family', 'Existing', 0.05);
        """
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(db_path=str(db_path))
    inserted = extractor.insert_charges(
        version_id=0, family_key="some-family", charges=[], force_clear=True
    )
    assert inserted == 0
    check = sqlite3.connect(db_path)
    count = check.execute("SELECT COUNT(*) FROM tariff_charges").fetchone()[0]
    check.close()
    assert count == 1, "force_clear with version_id=0 must not delete anything"


def test_runtime_trace_flags_slicer_dropped_markers(tmp_path, monkeypatch):
    """diagnose-document-nc --trace-runtime must detect when the page-bounded
    text slice silently loses rate markers that the full-doc path has.
    This is the diagnostic that would have caught the 2026-05-20 Docling
    slicer bug in seconds instead of 30 minutes of replay-vs-runtime
    reconciliation."""
    from duke_rates.cli import _build_runtime_trace
    from duke_rates.historical.ncuc.pipeline import bulk_extractor as be

    pdf_path = tmp_path / "stub.pdf"
    pdf_path.write_text("placeholder")

    # Stub get_document_for_extraction so we don't need the full DB schema.
    fake_doc = {
        "id": 99,
        "family_key": "nc-progress-leaf-500",
        "title": "Residential",
        "company": "progress",
        "state": "NC",
        "local_path": str(pdf_path),
        "effective_start": "2024-10-01",
        "start_page": 1,
        "end_page": 3,
        "leaf_no": "500",
        "content_hash": None,
        "revision_label": None,
        "supersedes_label": None,
        "discovery_record_id": None,
        "docket_number": None,
        "acquisition_method": None,
        "discovery_doc_quality_tier": None,
        "is_redline_candidate": 0,
        "redline_confidence": 0.0,
    }
    monkeypatch.setattr(
        be.BulkExtractor, "get_document_for_extraction",
        lambda self, hd_id: fake_doc,
    )

    # Bounded slice drops "basic customer charge" / "per kwh" / etc.;
    # full doc retains them. Simulates pre-fix slicer behavior.
    def fake_extract(self, path, start_page=None, end_page=None):
        if start_page is not None:
            return (
                "AVAILABILITY: Residential service. Schedule details below.",
                "docling_artifact_sliced",
            )
        return (
            "AVAILABILITY: Residential service. Basic Customer Charge: $14.00 "
            "per month. Kilowatt-Hour Charge: 12.119 cents per kWh.",
            "docling_artifact",
        )
    monkeypatch.setattr(be.BulkExtractor, "extract_text_from_pdf", fake_extract)

    # Stub extract_charges_from_document so the trace doesn't try to run the
    # full classifier/routing/fallback chain (needs more DB fixtures).
    def fake_full_extract(self, doc):
        return ([], None, [], "empty", None, {}, {
            "initial_parser_profile": "unknown",
            "final_parser_profile": "unknown",
            "fallback_applied": False,
            "fallback_attempts": [],
        })
    monkeypatch.setattr(
        be.BulkExtractor, "extract_charges_from_document", fake_full_extract,
    )

    trace = _build_runtime_trace(str(tmp_path / "any.db"), 99)
    dropped = trace.get("slicer_dropped_markers") or []
    assert "basic customer charge" in dropped, (
        f"trace should flag dropped markers; got: {dropped}"
    )
    assert "per kwh" in dropped


def test_record_document_type_v2_persists_v2_classification(tmp_path):
    """Live ingest wiring: when extract_charges_from_document runs,
    rule_document_type_v2 should fire alongside v1 and write a row to
    document_classifications with classifier='rule_document_type_v2'.

    Regression for the 2026-05-21 Stream B production wiring — without
    it, only docs touched by classify-documents-v2-nc had v2 votes,
    and newly-ingested docs were stuck at the v1-only agreement layer."""
    import sqlite3
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    db_path = tmp_path / "v2_wiring.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE document_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
            stage TEXT NOT NULL, label TEXT NOT NULL, confidence REAL NOT NULL,
            classifier TEXT NOT NULL, classifier_version TEXT NOT NULL DEFAULT '',
            evidence_json TEXT, alternatives_json TEXT, metadata_json TEXT,
            superseded_by INTEGER, created_at TEXT NOT NULL,
            UNIQUE(subject_kind, subject_id, stage, classifier, classifier_version)
        );
        CREATE TABLE document_fingerprints_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT NOT NULL, file_hash TEXT, page_count INTEGER,
            text_chars INTEGER, has_tables INTEGER, has_scanned_pages INTEGER,
            avg_chars_per_page REAL, token_signals_json TEXT,
            first_page_signature TEXT, title_candidates_json TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(db_path=str(db_path))
    doc = {
        "id": 42,
        "title": "Residential Service (Leaf No. 500)",
        "local_path": str(tmp_path / "stub.pdf"),
    }
    text = (
        "Duke Energy Progress, LLC NC First Revised Leaf No. 500\n"
        "AVAILABILITY: This Schedule is available for residential service.\n"
        "Basic Customer Charge: $14.00 per month.\n"
        "Kilowatt-Hour Charge: 12.119 cents per kWh."
    )
    extractor._record_document_type_v2_classification(doc, text)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT classifier, label, confidence FROM document_classifications "
        "WHERE subject_id = '42' AND stage = 'document_type'"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    classifier, label, conf = rows[0]
    assert classifier == "rule_document_type_v2"
    assert label == "TARIFF_SHEET"
    assert conf >= 0.9


def test_record_document_type_v2_does_not_raise_on_missing_fingerprint(tmp_path):
    """If document_fingerprints_v2 has no row for the PDF, v2 should still
    fire — just without layout signals. Production must not crash when
    a doc bypassed the fingerprint pipeline."""
    import sqlite3
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    db_path = tmp_path / "v2_no_fp.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE document_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
            stage TEXT NOT NULL, label TEXT NOT NULL, confidence REAL NOT NULL,
            classifier TEXT NOT NULL, classifier_version TEXT NOT NULL DEFAULT '',
            evidence_json TEXT, alternatives_json TEXT, metadata_json TEXT,
            superseded_by INTEGER, created_at TEXT NOT NULL,
            UNIQUE(subject_kind, subject_id, stage, classifier, classifier_version)
        );
        CREATE TABLE document_fingerprints_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT NOT NULL, file_hash TEXT, page_count INTEGER,
            text_chars INTEGER, has_tables INTEGER, has_scanned_pages INTEGER,
            avg_chars_per_page REAL, token_signals_json TEXT,
            first_page_signature TEXT, title_candidates_json TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(db_path=str(db_path))
    doc = {
        "id": 99,
        "title": "Direct Testimony of Jane Smith",
        "local_path": "/no/such/path",
    }
    text = (
        "DIRECT TESTIMONY OF JANE SMITH on behalf of Duke Energy.\n"
        "Q. Please state your name and business address.\n"
        "A. My name is Jane Smith."
    )
    extractor._record_document_type_v2_classification(doc, text)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT classifier, label FROM document_classifications "
        "WHERE subject_id = '99'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "rule_document_type_v2"
    assert rows[0][1] == "TESTIMONY"


def test_default_has_page_bounds_preserves_caller_compatibility(extractor):
    """has_page_bounds still defaults to True so existing call sites that
    don't pass the kwarg keep working; the kwarg is no longer load-bearing
    for the guard logic but is retained for caller compatibility."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="progress_jaa_rider",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="generic_residential",
        candidate_charge_count=5,
        candidate_outcome_quality="strong",
        candidate_metrics=METRICS_GENERIC_RES,
    )
    # Now refused regardless of has_page_bounds — this is the 2026-05-20 fix.
    assert should is False
    assert reason is None
