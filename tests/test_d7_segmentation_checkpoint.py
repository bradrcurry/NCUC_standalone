"""D7A source typing, ownership, cache preservation and routing regressions."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from duke_rates.config import Settings
from duke_rates.db.artifact_cache import (
    load_page_artifacts,
    load_span_artifacts,
    save_page_artifacts,
    save_span_artifacts,
)
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc import importer as importer_module
from duke_rates.historical.ncuc.importer import NcucPipelineImporter
from duke_rates.historical.ncuc.pipeline.page_miner import _extract_page_features_from_text
from duke_rates.historical.ncuc.pipeline.segmentation import segment_document
from duke_rates.historical.ncuc.pipeline.triage import triage_pdf
from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFetchStatus
from duke_rates.models.pipeline import PageEvidence, PipelineRoute, TariffSpan
from duke_rates.models.tariff import TariffFamilyRecord


def page(text: str, number: int = 1):
    return _extract_page_features_from_text(text, number)


def test_body_leaf_reference_cannot_merge_two_distinct_own_leaves():
    first = page(
        "Duke Energy Progress, LLC\nNC Original Leaf No. 600\nRIDER BA\n"
        "See Leaf No. 601 and Leaf No. 611 for other charges.",
        1,
    )
    second = page(
        "Duke Energy Progress, LLC\nNC Original Leaf No. 601\n"
        "RIDER BA\nEnergy charge $0.01 per kWh",
        2,
    )
    spans = segment_document([first, second])
    assert [(s.start_page, s.end_page, s.extracted_leaf_nos) for s in spans] == [
        (1, 1, {"600"}),
        (2, 2, {"601"}),
    ]


def test_rate_vocabulary_in_legal_or_calculation_pages_is_not_a_tariff():
    inputs = [
        page(
            "SUPREME COURT OF NORTH CAROLINA\nBRIEF FOR APPELLEE\n"
            "The rate, rider, energy charge and service schedule were challenged.",
            1,
        ),
        page(
            "DUKE ENERGY PROGRESS\nNCEMPA GENERATION ASSET PURCHASE\n"
            "EXHIBIT A-TU\nCALCULATION OF JOINT AGENCY ASSET RIDER\n$0.00390 per kWh",
            2,
        ),
    ]
    assert all(s.doc_type == "procedural" for s in segment_document(inputs))
    assert segment_document(inputs)[0].segmentation_evidence["authority_established"] is False


def test_formal_sheet_ends_before_a_following_order():
    spans = segment_document(
        [
            page("NC Original Leaf No. 62\nRIDER EE\n$0.01 per kWh", 1),
            page(
                "ORDER APPROVING STIPULATION\nDOCKET NO. E-7 SUB 1285\n"
                "The Commission considers the rider and rate schedule.",
                2,
            ),
        ]
    )
    assert [(s.start_page, s.end_page, s.doc_type) for s in spans] == [
        (1, 1, "tariff"),
        (2, 2, "procedural"),
    ]
    assert spans[1].segmentation_evidence["kind"] == "commission_order"


def test_headerless_credit_table_keeps_single_leaf_ownership_with_nbsp():
    start = page("NC Original Leaf No. 592\nSCHEDULE PPBE\nAPPLICABILITY\nEnergy credit", 1)
    continuation = page(
        "Energy\u00a0Credit\nCapacity\u00a0Credit\n"
        + "\n".join(f"{year} 3.95 2.70" for year in range(2023, 2036)),
        2,
    )
    end = page("NC Original Leaf No. 600\nSUMMARY OF RIDER ADJUSTMENTS", 3)
    spans = segment_document([start, continuation, end])
    assert [(s.start_page, s.end_page, s.extracted_leaf_nos) for s in spans] == [
        (1, 2, {"592"}),
        (3, 3, {"600"}),
    ]
    assert spans[0].segmentation_evidence["page_kinds"]["2"] == "tariff_appendix"


@pytest.mark.parametrize("numbers", [[1, 1], [2, 1], [0, 1]])
def test_ambiguous_original_page_ownership_is_rejected(numbers):
    with pytest.raises(ValueError, match="unique, increasing original page"):
        segment_document([PageEvidence(page_number=n, text_length=1) for n in numbers])


def test_missing_page_gap_is_not_bridged_even_when_the_leaf_matches():
    spans = segment_document(
        [page("NC Original Leaf No. 600", 1), page("NC Original Leaf No. 600", 3)]
    )
    assert [(s.start_page, s.end_page) for s in spans] == [(1, 1), (3, 3)]


def test_cache_roundtrip_preserves_flags_and_separate_segmentation_evidence(tmp_path):
    conn = connect(tmp_path / "cache.db")
    evidence = page("TABLE OF CONTENTS\nPROPOSED\n0.0464/0.0512", 1)
    spans = [
        TariffSpan(
            start_page=1,
            end_page=1,
            doc_type="procedural",
            evidence_score_breakdown={"family": {"explicit_leaf_hit": 1}},
            segmentation_evidence={
                "kind": "index",
                "boundary_page": 1,
                "authority_established": False,
            },
        )
    ]
    save_page_artifacts(
        conn, discovery_record_id=None, source_pdf="source.pdf", file_hash="test", pages=[evidence]
    )
    save_span_artifacts(
        conn, discovery_record_id=None, source_pdf="source.pdf", file_hash="test", spans=spans
    )
    actual = load_page_artifacts(conn, source_pdf="source.pdf", file_hash="test")[0]
    for field in (
        "has_redline_markers",
        "redline_marker_count",
        "has_dual_rate_pair",
        "has_toc_page",
    ):
        assert getattr(actual, field) == getattr(evidence, field)
    restored = load_span_artifacts(conn, source_pdf="source.pdf", file_hash="test")[0]
    assert restored.segmentation_evidence == spans[0].segmentation_evidence
    assert restored.evidence_score_breakdown == spans[0].evidence_score_breakdown
    # Older metadata lacks those fields: recover only text signals without OCR.
    metadata = json.loads(
        conn.execute("SELECT metadata_json FROM ncuc_page_artifacts").fetchone()[0]
    )
    for field in (
        "has_redline_markers",
        "redline_marker_count",
        "has_dual_rate_pair",
        "has_toc_page",
    ):
        metadata.pop(field)
    conn.execute("UPDATE ncuc_page_artifacts SET metadata_json=?", (json.dumps(metadata),))
    legacy = load_page_artifacts(conn, source_pdf="source.pdf", file_hash="test")[0]
    assert legacy.has_redline_markers and legacy.has_dual_rate_pair and legacy.has_toc_page
    conn.close()


@pytest.mark.parametrize("count,expected_samples", [(6, 3), (1000, 5)])
def test_triage_uses_sampled_pages_instead_of_total_page_count(
    tmp_path, monkeypatch, count, expected_samples
):
    import fitz

    path = tmp_path / "source.pdf"
    path.write_bytes(b"fixture bytes; PDF access supplied by this triage unit test")
    text = "Duke Energy Progress\nNC Original Leaf No. 600\n" + (
        "Readable rate schedule and energy service terms. " * 30
    )

    class FakeDocument:
        page_count = count

        def __getitem__(self, index):
            assert 0 <= index < count
            return SimpleNamespace(get_text=lambda mode: text)

        def close(self):
            pass

    monkeypatch.setattr(fitz, "open", lambda _: FakeDocument())
    result = triage_pdf(str(path))
    assert result.route_recommendation != PipelineRoute.OCR_REQUIRED
    assert not result.is_likely_scanned and result.keyword_hits["sampled_pages"] == expected_samples


@pytest.mark.parametrize("entrypoint", ["native", "premined"])
def test_importer_never_binds_non_tariff_span_even_with_a_strong_family_match(
    tmp_path, monkeypatch, entrypoint
):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    db = tmp_path / "research.db"
    repo = Repository(db)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-500",
            state="NC",
            company="progress",
            tariff_identifier="leaf-500",
            schedule_code="RES",
            family_type="rate_schedule",
            title="Residential Service",
        )
    )
    conn = connect(db)
    save_page_artifacts(
        conn,
        discovery_record_id=9,
        source_pdf=str(path),
        file_hash="fixture",
        pages=[page("NC Original Leaf No. 500\nSCHEDULE RES", 1)],
    )
    save_span_artifacts(
        conn,
        discovery_record_id=9,
        source_pdf=str(path),
        file_hash="fixture",
        spans=[
            TariffSpan(
                start_page=1,
                end_page=1,
                doc_type="procedural",
                extracted_leaf_nos={"500"},
                extracted_schedule_titles={"SCHEDULE RES"},
            )
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: SimpleNamespace(
            route_recommendation=PipelineRoute.TEXT_PARSE, file_hash="fixture"
        ),
    )
    record = NcucDiscoveryRecord(
        id=9,
        utility="Duke Energy Progress",
        filing_title="Progress compliance filing",
        docket_number="E-2 Sub 1300",
        sub_number="1300",
        fetch_status=NcucFetchStatus.SUCCESS,
        local_path=str(path),
        discovered_url="https://example.test/source.pdf",
        content_hash="fixture",
    )
    importer = NcucPipelineImporter(Settings(database_path=db, data_dir=tmp_path / "data"), repo)
    if entrypoint == "native":
        assert importer.mine_discovery_record_spans(record) == []
    else:
        assert (
            importer.mine_discovery_record_spans_with_pages(
                record, [page("NC Original Leaf No. 500\nSCHEDULE RES", 1)]
            )
            == []
        )
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM historical_documents").fetchone()[0] == 0
    conn.close()
