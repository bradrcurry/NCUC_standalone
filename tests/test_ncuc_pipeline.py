import json
from datetime import UTC, datetime
import pytest
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.ncuc import importer as importer_module
from duke_rates.historical.ncuc.importer import NcucPipelineImporter
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.pipeline import PageEvidence, PipelineRoute, TariffSpan
from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFetchStatus
from duke_rates.models.tariff import TariffFamilyRecord
from duke_rates.historical.ncuc.pipeline.segmentation import segment_document
from duke_rates.historical.ncuc.pipeline.metadata_extractor import extract_dates_from_span
from duke_rates.historical.ncuc.pipeline.family_matcher import score_span_against_family
from duke_rates.historical.ncuc.pipeline.page_miner import _extract_page_features_from_text
from duke_rates.historical.ncuc.pipeline.triage import triage_pdf

def test_page_classification_and_segmentation():
    # Simulate a 4-page PDF:
    # Page 1: Procedural motion
    # Page 2: Tariff Sheet 601 (Rider BA)
    # Page 3: Tariff Sheet 601 continued
    # Page 4: Tariff Sheet 602 (Rider EE)
    pages = [
        PageEvidence(
            page_number=1, text_length=500,
            procedural_vocab_density=0.08, tariff_vocab_density=0.0
        ),
        PageEvidence(
            page_number=2, text_length=800,
            has_leaf_header=True, has_schedule_heading=True,
            extracted_leaf_nos=["601"], extracted_schedule_codes=["RIDER BA"],
            tariff_vocab_density=0.06
        ),
        PageEvidence(
            page_number=3, text_length=400,
            extracted_leaf_nos=["601"], # Cont
            tariff_vocab_density=0.05
        ),
        PageEvidence(
            page_number=4, text_length=700,
            has_leaf_header=True, has_schedule_heading=True,
            extracted_leaf_nos=["602"], extracted_schedule_codes=["RIDER EE"],
            tariff_vocab_density=0.07
        )
    ]
    
    spans = segment_document(pages, parent_discovery_id=99)
    assert len(spans) == 3
    
    assert spans[0].doc_type == "procedural"
    assert spans[0].start_page == 1
    
    # Span 2 should be Rider BA, pages 2-3
    assert spans[1].doc_type == "tariff"
    assert spans[1].start_page == 2
    assert spans[1].end_page == 3
    assert "601" in spans[1].extracted_leaf_nos
    
    # Span 3 should be Rider EE, page 4
    assert spans[2].doc_type == "tariff"
    assert spans[2].start_page == 4
    assert spans[2].end_page == 4


def test_segmentation_promotes_revised_leaf_page_to_tariff_even_with_weak_density():
    pages = [
        PageEvidence(
            page_number=1,
            text_length=500,
            procedural_vocab_density=0.08,
            tariff_vocab_density=0.0,
        ),
        PageEvidence(
            page_number=2,
            text_length=700,
            has_leaf_header=True,
            has_revised_header=True,
            extracted_leaf_nos=["172"],
            tariff_vocab_density=0.003,
            procedural_vocab_density=0.003,
        ),
    ]

    spans = segment_document(pages, parent_discovery_id=55)

    assert len(spans) == 2
    assert spans[0].doc_type == "procedural"
    assert spans[1].doc_type == "tariff"
    assert spans[1].start_page == 2
    assert "172" in spans[1].extracted_leaf_nos


def test_segmentation_keeps_cover_letter_with_inline_rider_phrase_as_procedural():
    pages = [
        PageEvidence(
            page_number=1,
            text_length=600,
            extracted_schedule_codes=["Revised BPM Rider"],
            tariff_vocab_density=0.01,
            procedural_vocab_density=0.02,
        ),
        PageEvidence(
            page_number=2,
            text_length=700,
            has_leaf_header=True,
            has_revised_header=True,
            extracted_leaf_nos=["63"],
            tariff_vocab_density=0.03,
            procedural_vocab_density=0.01,
        ),
    ]

    spans = segment_document(pages, parent_discovery_id=56)

    assert len(spans) == 2
    assert spans[0].doc_type == "procedural"
    assert spans[1].doc_type == "tariff"
    assert spans[1].start_page == 2


def test_segmentation_splits_on_distinct_schedule_heading_transition_without_leaf_headers():
    pages = [
        PageEvidence(
            page_number=1,
            text_length=600,
            has_schedule_heading=True,
            extracted_schedule_codes=["SCHEDULE RES-56", "Duke Energy Progress, LLC R-1 RESIDENTIAL SERVICE"],
            tariff_vocab_density=0.02,
            procedural_vocab_density=0.001,
        ),
        PageEvidence(
            page_number=2,
            text_length=550,
            tariff_vocab_density=0.03,
            procedural_vocab_density=0.0,
        ),
        PageEvidence(
            page_number=3,
            text_length=620,
            has_schedule_heading=True,
            extracted_schedule_codes=["SCHEDULE R-TOUD-56", "Duke Energy Progress, LLC R-2 RESIDENTIAL SERVICE"],
            tariff_vocab_density=0.021,
            procedural_vocab_density=0.001,
        ),
    ]

    spans = segment_document(pages, parent_discovery_id=57)

    assert len(spans) == 2
    assert spans[0].start_page == 1
    assert spans[0].end_page == 2
    assert spans[1].start_page == 3
    assert "SCHEDULE R-TOUD-56" in spans[1].extracted_schedule_titles


def test_segmentation_does_not_split_on_generic_schedule_heading_transition():
    pages = [
        PageEvidence(
            page_number=1,
            text_length=600,
            has_schedule_heading=True,
            extracted_schedule_codes=["SCHEDULE LGS-56", "LARGE GENERAL SERVICE"],
            tariff_vocab_density=0.02,
            procedural_vocab_density=0.001,
        ),
        PageEvidence(
            page_number=2,
            text_length=550,
            has_schedule_heading=True,
            extracted_schedule_codes=["RIDER APPLICATIONS", "TYPE OF SERVICE"],
            tariff_vocab_density=0.03,
            procedural_vocab_density=0.0,
        ),
        PageEvidence(
            page_number=3,
            text_length=580,
            tariff_vocab_density=0.03,
            procedural_vocab_density=0.0,
        ),
    ]

    spans = segment_document(pages, parent_discovery_id=58)

    assert len(spans) == 1
    assert spans[0].start_page == 1
    assert spans[0].end_page == 3

def test_metadata_extraction_dates():
    span = TariffSpan(
        start_page=1, end_page=1,
        header_footer_snippets=["Effective Dec 1, 2021", "ISSUED on November 10, 2021"]
    )
    
    pages_text = {1: "Applicable beginning Jan 1, 2022. This is the body text."}
    
    dates = extract_dates_from_span(span, pages_text)
    
    assert len(dates) == 2  # Dec 1 and Nov 10
    
    eff_date = next(c for c in dates if c.date_type == "effective")
    assert eff_date.date_value == "2021-12-01"
    
    issued_date = next(c for c in dates if c.date_type == "issued")
    assert issued_date.date_value == "2021-11-10"

def test_family_matcher_ambiguous_short_code():
    # Test ambiguous code logic: we expect a penalty if it's a procedural document
    span = TariffSpan(
        start_page=1, end_page=1,
        doc_type="procedural",
        extracted_schedule_titles=set(["BA"])
    )
    
    # Provide no leaf number hit, only the short alias
    score = score_span_against_family(
        span, family_id="nc-leaf-601",
        family_aliases=["Billing Adjustment"],
        target_code="BA"
    )
    
    # Should get: procedural (-20) + ambiguous penalty (-10) = -30
    assert span.evidence_score_breakdown["nc-leaf-601"]["procedural_doc_penalty"] == -20.0
    assert span.evidence_score_breakdown["nc-leaf-601"]["ambiguous_code_penalty"] == -10.0
    assert score < 0


def test_page_miner_extracts_descriptive_rider_heading() -> None:
    evidence = _extract_page_features_from_text(
        "North Carolina Eighteenth Revised Leaf No. 60\nFUEL COST ADJUSTMENT RIDER\nAVAILABILITY\n",
        1,
    )

    assert evidence.has_schedule_heading is True
    assert "60" in evidence.extracted_leaf_nos
    assert "FUEL COST ADJUSTMENT RIDER" in evidence.extracted_schedule_codes


def test_page_miner_extracts_descriptive_rider_heading_with_parenthetical() -> None:
    evidence = _extract_page_features_from_text(
        "North Carolina Thirty-First Revised Leaf No. 60\nFUEL COST ADJUSTMENT RIDER (NC)\nAPPLICABILITY\n",
        1,
    )

    assert evidence.has_schedule_heading is True
    assert "60" in evidence.extracted_leaf_nos
    assert "FUEL COST ADJUSTMENT RIDER (NC)" in evidence.extracted_schedule_codes


def test_page_miner_extracts_inline_descriptive_heading_from_merged_pdf_text() -> None:
    evidence = _extract_page_features_from_text(
        "North Carolina Thirty-First Revised Leaf No. 60\n"
        "FUEL COST ADJUSTMENT RIDER (NC) APPLICABILITY (North Carolina Only)\n",
        1,
    )

    assert "60" in evidence.extracted_leaf_nos
    assert any(code.startswith("FUEL COST ADJUSTMENT RIDER") for code in evidence.extracted_schedule_codes)


def test_page_miner_extracts_descriptive_program_heading() -> None:
    evidence = _extract_page_features_from_text(
        "North Carolina First Revised Leaf No. 172\nSMART ENERGY NOW PROGRAM (NC)\n(Pilot)\n",
        1,
    )

    assert "172" in evidence.extracted_leaf_nos
    assert "SMART ENERGY NOW PROGRAM (NC)" in evidence.extracted_schedule_codes


def test_page_miner_filters_generic_type_of_service_but_keeps_schedule_code() -> None:
    evidence = _extract_page_features_from_text(
        "Duke Energy Carolinas, LLC\n"
        "North Carolina Thirty-Seventh Revised Leaf No. 55\n"
        "SCHEDULE PG (NC)\n"
        "PARALLEL GENERATION\n"
        "TYPE OF SERVICE\n",
        1,
    )

    assert "55" in evidence.extracted_leaf_nos
    assert "SCHEDULE PG (NC)" in evidence.extracted_schedule_codes
    assert "PG" in evidence.extracted_schedule_codes
    assert "TYPE OF SERVICE" not in evidence.extracted_schedule_codes


def test_page_miner_filters_effective_for_service_footer_but_keeps_schedule_code() -> None:
    evidence = _extract_page_features_from_text(
        "Duke Energy Carolinas, LLC Amended\n"
        "North Carolina Original Leaf No. 19\n"
        "SCHEDULE RET (NC)\n"
        "RESIDENTIAL SERVICE, ALL-ELECTRIC, TIME OF USE\n"
        "Effective November 1, 2013 for service on and after September 25, 2013\n",
        1,
    )

    assert "19" in evidence.extracted_leaf_nos
    assert "SCHEDULE RET (NC)" in evidence.extracted_schedule_codes
    assert "RET" in evidence.extracted_schedule_codes
    assert all("EFFECTIVE" not in code.upper() for code in evidence.extracted_schedule_codes)


def test_family_matcher_matches_descriptive_heading_to_abbreviated_family_code() -> None:
    span = TariffSpan(
        start_page=1,
        end_page=1,
        doc_type="tariff",
        extracted_schedule_titles={"FUEL COST ADJUSTMENT RIDER"},
    )

    score = score_span_against_family(
        span,
        family_id="nc-carolinas-doc-FUELCOSTADJRDR",
        family_aliases=[],
        target_code="FUELCOSTADJRDR",
    )

    assert score >= 20.0
    assert span.evidence_score_breakdown["nc-carolinas-doc-FUELCOSTADJRDR"]["schedule_code_hit"] == 20.0


def test_family_matcher_does_not_match_short_alias_as_substring() -> None:
    span = TariffSpan(
        start_page=1,
        end_page=1,
        doc_type="tariff",
        header_footer_snippets=["Duke Energy Carolinas, LLC's Revised BPM Rider Tariff Sheet"],
    )

    score = score_span_against_family(
        span,
        family_id="nc-carolinas-rider-PM",
        family_aliases=["PM", "PM RIDER"],
        target_code="PM",
    )

    assert "heading_alias_similarity" not in span.evidence_score_breakdown["nc-carolinas-rider-PM"]
    assert score == 8.0


def test_importer_infers_carolinas_from_filing_title_and_uses_carolinas_family(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-500",
            state="NC",
            company="progress",
            tariff_identifier="leaf-500",
            schedule_code="RES",
            family_type="rate_schedule",
            title="Progress Residential Service",
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-leaf-500",
            state="NC",
            company="carolinas",
            tariff_identifier="leaf-500",
            schedule_code="RES",
            family_type="rate_schedule",
            title="Carolinas Residential Service",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "duke-energy-carolinas.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": "page_segment"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            PageEvidence(
                page_number=1,
                text_length=120,
                text_content="Leaf No. 500\nResidential Service",
                has_leaf_header=True,
                extracted_leaf_nos=["500"],
            )
        ],
    )
    monkeypatch.setattr(
        importer_module,
        "segment_document",
        lambda _pages, parent_discovery_id=None: [
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=1,
                end_page=2,
                doc_type="tariff",
                extracted_leaf_nos={"500"},
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=1,
        utility="Duke Energy Progress",
        filing_title="Duke Energy Carolinas, LLC's Revisions to Rate Compliance Filing of Approved Tariffs",
        filing_date="2013-11-01",
        docket_number="E-7",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-duke-energy-carolinas.pdf",
        content_hash="hash-1",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.company == "carolinas"
    assert stored.family_key == "nc-carolinas-leaf-500"


def test_importer_prefers_schedule_pg_over_generic_type_of_service_heading(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-schedule-PG",
            state="NC",
            company="carolinas",
            tariff_identifier="schedule-PG",
            schedule_code="PG",
            family_type="rate_schedule",
            title="PG",
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-doc-TYPEOFSERVICE",
            state="NC",
            company="carolinas",
            tariff_identifier="doc-TYPEOFSERVICE",
            schedule_code="TYPEOFSERVICE",
            family_type="rate_schedule",
            title="TYPE OF SERVICE",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "schedule-pg.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-pg"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            _extract_page_features_from_text(
                "Duke Energy Carolinas, LLC\n"
                "North Carolina Thirty-Seventh Revised Leaf No. 55\n"
                "SCHEDULE PG (NC)\n"
                "PARALLEL GENERATION\n"
                "TYPE OF SERVICE\n",
                1,
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=2163,
        utility="Duke Energy Carolinas",
        filing_title="Duke's Revised NC Rate Schedule and Riders",
        docket_number="E-7",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-pg.pdf",
        content_hash="hash-pg",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.family_key == "nc-carolinas-schedule-PG"


def test_importer_matches_existing_historical_opt_i_family_from_explicit_title_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-doc-SCHEDULEOPTIOPTIONALPOWERSERVICETIMEOFUSEINDUSTR",
            state="NC",
            company="carolinas",
            tariff_identifier="doc-SCHEDULEOPTIOPTIONALPOWERSERVICETIMEOFUSEINDUSTR",
            schedule_code="SCHEDULEOPTIOPTIONALPOWERSERVICETIMEOFUSEINDUSTR",
            family_type="rate_schedule",
            title="SCHEDULE OPT-I (NC)\nOPTIONAL POWER SERVICE, TIME OF USE\nINDUSTRIAL SERVICE",
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-doc-INDUSTRIALSERVICE",
            state="NC",
            company="carolinas",
            tariff_identifier="doc-INDUSTRIALSERVICE",
            schedule_code="INDUSTRIALSERVICE",
            family_type="rate_schedule",
            title="INDUSTRIAL SERVICE",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "schedule-opti.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-opti"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            _extract_page_features_from_text(
                "Duke Energy Carolinas, LLC\n"
                "North Carolina Seventh Revised Leaf No. 47\n"
                "SCHEDULE OPT-I (NC)\n"
                "OPTIONAL POWER SERVICE, TIME OF USE\n"
                "INDUSTRIAL SERVICE\n"
                "TYPE OF SERVICE\n",
                1,
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=2156,
        utility="Duke Energy Carolinas",
        filing_title="Duke's Rate Schedule",
        docket_number="E-7",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-opti.pdf",
        content_hash="hash-opti",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.family_key == "nc-carolinas-doc-SCHEDULEOPTIOPTIONALPOWERSERVICETIMEOFUSEINDUSTR"


def test_importer_prefers_explicit_schedule_title_for_provisional_family_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-doc-BILLSUNDERTHISSCHEDULE",
            state="NC",
            company="carolinas",
            tariff_identifier="doc-BILLSUNDERTHISSCHEDULE",
            schedule_code="BILLSUNDERTHISSCHEDULE",
            family_type="rate_schedule",
            title="Bills under this Schedule",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "schedule-ret.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-ret"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            _extract_page_features_from_text(
                "Duke Energy Carolinas, LLC Amended\n"
                "North Carolina Original Leaf No. 19\n"
                "SCHEDULE RET (NC)\n"
                "RESIDENTIAL SERVICE, ALL-ELECTRIC, TIME OF USE\n"
                "Bills under this Schedule are due and payable on the date of the bill.\n"
                "Effective November 1, 2013 for service on and after September 25, 2013\n",
                1,
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=2190,
        utility="Duke Energy Carolinas",
        filing_title="Duke Energy Carolinas, LLC's Revisions to Rate Compliance Filing of Approved Tariffs",
        docket_number="E-7",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-ret.pdf",
        content_hash="hash-ret",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.family_key.startswith("nc-carolinas-doc-SCHEDULERET")


def test_importer_selects_best_legacy_attachment_hint_per_span() -> None:
    span = TariffSpan(
        start_page=5,
        end_page=7,
        doc_type="tariff",
        extracted_schedule_titles={
            "SCHEDULE R-TOU-56",
            "Residential Time-of-Use Energy",
        },
        header_footer_snippets=["TIME-OF-USE"],
    )

    selected = NcucPipelineImporter._select_legacy_attachment_hint(
        span,
        [
            {
                "family_key": "nc-progress-leaf-503",
                "title_candidates": [
                    "Residential Service Time-of-Use with Critical Peak Pricing"
                ],
                "matched_terms": ["R-TOUD"],
                "leaf_no": "503",
            },
            {
                "family_key": "nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY",
                "title_candidates": ["Residential Time-of-Use Energy"],
                "matched_terms": ["R-TOU", "TIME-OF-USE"],
                "leaf_no": "504",
            },
        ],
    )

    assert selected is not None
    assert selected["family_key"] == "nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY"


def test_importer_infers_carolinas_from_mined_page_text_when_utility_is_mislabeled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-500",
            state="NC",
            company="progress",
            tariff_identifier="leaf-500",
            schedule_code="RES",
            family_type="rate_schedule",
            title="Progress Residential Service",
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-leaf-500",
            state="NC",
            company="carolinas",
            tariff_identifier="leaf-500",
            schedule_code="RES",
            family_type="rate_schedule",
            title="Carolinas Residential Service",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "generic-compliance.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-2"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            PageEvidence(
                page_number=1,
                text_length=220,
                text_content=(
                    "Duke Energy Carolinas, LLC\n"
                    "NC Eighteenth Revised Leaf No. 500\n"
                    "Residential Service"
                ),
                has_leaf_header=True,
                has_schedule_heading=True,
                extracted_leaf_nos=["500"],
                header_candidates=[
                    "Duke Energy Carolinas, LLC",
                    "NC Eighteenth Revised Leaf No. 500",
                    "Residential Service",
                ],
            )
        ],
    )
    monkeypatch.setattr(
        importer_module,
        "segment_document",
        lambda _pages, parent_discovery_id=None: [
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=1,
                end_page=1,
                doc_type="tariff",
                extracted_leaf_nos={"500"},
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=11,
        utility="Duke Energy Progress",
        filing_title="Compliance tariff filing",
        filing_date="2013-11-01",
        docket_number="E-7",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-generic-compliance.pdf",
        content_hash="hash-2",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.company == "carolinas"
    assert stored.family_key == "nc-carolinas-leaf-500"


def test_importer_infers_duke_power_as_carolinas() -> None:
    record = NcucDiscoveryRecord(
        utility="Duke Energy Progress",
        filing_title="Duke Power's Rate Schedule/Riders",
        local_path=r"data\raw\historical\ncuc\e-7\e-7-nodate-duke-power-s-rate-schedule-riders.pdf",
    )

    assert NcucPipelineImporter._infer_company_from_record(record) == "carolinas"


def test_importer_keeps_mixed_company_filing_on_progress_when_progress_is_explicit() -> None:
    record = NcucDiscoveryRecord(
        utility="Duke Energy Progress",
        filing_title="Duke Energy Carolinas and Duke Energy Progress Rider GSA-4 NC Tariffs",
        local_path=(
            r"data\raw\historical\ncuc\e-7\duke-energy-carolinas-and-"
            r"duke-energy-progress-rider-gsa-4-nc-tariffs.pdf"
        ),
    )

    assert NcucPipelineImporter._infer_company_from_record(record) == "progress"


def test_importer_infers_carolinas_from_e7_docket_when_text_is_ambiguous() -> None:
    record = NcucDiscoveryRecord(
        utility="Duke Energy Progress",
        filing_title="Compliance filing",
        docket_number="E-7, Sub 1214",
        local_path=r"data\historical\ncuc\e-7-sub-1214\study.pdf",
    )

    assert NcucPipelineImporter._infer_company_from_record(record) == "carolinas"


def test_triage_marks_complex_scanned_docs_for_gpu_candidate(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "scanned.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    class FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def get_text(self, _mode: str) -> str:
            return self._text

    class FakeDoc:
        def __init__(self) -> None:
            self.page_count = 30
            self._pages = [FakePage("  ") for _ in range(self.page_count)]

        def __getitem__(self, index: int) -> FakePage:
            return self._pages[index]

        def close(self) -> None:
            return None

    monkeypatch.setitem(__import__("sys").modules, "fitz", type("FitzModule", (), {"open": lambda _path: FakeDoc()}))

    triage = triage_pdf(str(pdf_path))

    assert triage.is_likely_scanned is True
    assert triage.route_recommendation == "ocr_required"
    assert len(triage.file_hash or "") == 64
    assert triage.ocr_confidence_score >= 0.8
    assert triage.gpu_ocr_candidate is True
    assert triage.document_archetype_candidate == "scanned_bundle"
    assert triage.table_mode_candidate == "scanned_text"
    assert "ocr_required_high_confidence" in triage.triage_flags
    assert "gpu_ocr_candidate" in triage.triage_flags


def test_importer_reintegrates_ocr_required_documents_via_cpu_ocr(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-500",
            state="NC",
            company="progress",
            tariff_identifier="leaf-500",
            schedule_code="RES",
            family_type="rate_schedule",
            title="Progress Residential Service",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "ocr-required.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.OCR_REQUIRED})(),
    )
    monkeypatch.setattr(
        importer_module,
        "extract_ocr_document_pages",
        lambda _: [
            PageEvidence(
                page_number=1,
                text_length=180,
                text_content="Leaf No. 500\nResidential Service\nEffective January 1, 2024",
                has_leaf_header=True,
                has_schedule_heading=True,
                extracted_leaf_nos=["500"],
            )
        ],
    )
    monkeypatch.setattr(
        importer_module,
        "segment_document",
        lambda _pages, parent_discovery_id=None: [
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=1,
                end_page=1,
                doc_type="tariff",
                extracted_leaf_nos={"500"},
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=2,
        utility="Duke Energy Progress",
        filing_title="Progress Energy Carolinas compliance filing",
        filing_date="2024-01-15",
        docket_number="E-2",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-2-progress.pdf",
        content_hash="hash-ocr",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.company == "progress"
    assert stored.family_key == "nc-progress-leaf-500"


def test_importer_deduplicates_created_ids_when_same_document_is_upserted(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-leaf-672",
            state="NC",
            company="carolinas",
            tariff_identifier="leaf-672",
            schedule_code="CEI",
            family_type="rate_schedule",
            title="Clean Energy Impact Rider",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "cei.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-cei"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            PageEvidence(
                page_number=10,
                text_length=180,
                text_content="Leaf No. 672\nClean Energy Impact Rider",
                has_leaf_header=True,
                extracted_leaf_nos=["672"],
            )
        ],
    )
    monkeypatch.setattr(
        importer_module,
        "segment_document",
        lambda _pages, parent_discovery_id=None: [
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=10,
                end_page=15,
                doc_type="tariff",
                extracted_leaf_nos={"672"},
            ),
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=10,
                end_page=15,
                doc_type="tariff",
                extracted_leaf_nos={"672"},
            ),
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=77,
        utility="Duke Energy Carolinas",
        filing_title="DEC DEP Compliance Tariffs Rider Clean Energy Impact",
        filing_date="2025-01-23",
        docket_number="E-7",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-cei.pdf",
        content_hash="hash-cei",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert created_ids == [1]


def test_importer_uses_filing_title_rider_code_when_span_heading_is_noisy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-rider-EE",
            state="NC",
            company="carolinas",
            tariff_identifier="rider-ee",
            schedule_code="EE",
            family_type="rider",
            title="EE",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "rider-ee.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-ee"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            PageEvidence(
                page_number=2,
                text_length=200,
                text_content="Duke Energy Carolinas, LLC\nNorth Carolina Revised Leaf No. 62",
                has_leaf_header=True,
                extracted_leaf_nos=["62"],
                tariff_vocab_density=0.03,
                header_candidates=["Duke Energy Carolinas, LLC"],
            )
        ],
    )
    monkeypatch.setattr(
        importer_module,
        "segment_document",
        lambda _pages, parent_discovery_id=None: [
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=2,
                end_page=3,
                doc_type="tariff",
                extracted_leaf_nos={"62"},
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=88,
        utility="Duke Energy Progress",
        filing_title="DEC Compliance Tariff for DSM/EE Rider EE",
        filing_date="2017-12-07",
        docket_number="E-7",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-ee.pdf",
        content_hash="hash-ee",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.company == "carolinas"
    assert stored.family_key == "nc-carolinas-rider-EE"


def test_importer_uses_single_family_legacy_attachment_hint_when_record_title_is_noise(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY",
            state="NC",
            company="progress",
            tariff_identifier="doc-residentialtimeofuseenergy",
            schedule_code=None,
            family_type="rider",
            title="Residential Time-of-Use Energy",
        )
    )

    pdf_path = tmp_path / "e-2-sub-1142.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY",
            title="Residential Time-of-Use Energy",
            state="NC",
            company="progress",
            category="rider",
            kind="pdf",
            canonical_url="https://example.test/legacy-r-tou.pdf",
            archived_url="https://example.test/legacy-r-tou.pdf#family=nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=tmp_path / "historical" / "raw" / "legacy-r-tou.pdf",
            content_hash="legacy-r-tou-hash",
            content_type="application/pdf",
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            metadata_json=json.dumps(
                {
                    "metadata_json": json.dumps(
                        {
                                "local_file": str(pdf_path),
                                "family_key_override": "nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY",
                                "parse_text_metadata": {
                                    "family_code": "504",
                                    "matched_terms": ["R-TOU", "TIME-OF-USE"],
                                },
                            }
                    )
                }
            ),
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-r-tou"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            PageEvidence(
                page_number=1,
                text_length=300,
                text_content="TYPE OF SERVICE\nNorth Carolina Revised Leaf No. 504",
                has_leaf_header=True,
                has_schedule_heading=True,
                extracted_leaf_nos=["504"],
                extracted_schedule_codes=["TYPE OF SERVICE"],
                tariff_vocab_density=0.03,
                header_candidates=["TYPE OF SERVICE"],
            )
        ],
    )
    monkeypatch.setattr(
        importer_module,
        "segment_document",
        lambda _pages, parent_discovery_id=None: [
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=1,
                end_page=2,
                doc_type="tariff",
                extracted_leaf_nos={"504"},
                extracted_schedule_titles={"TYPE OF SERVICE"},
                header_footer_snippets=["TYPE OF SERVICE"],
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=306,
        utility="Duke Energy Progress",
        filing_title="removed from rates effective for service on and after September 1, 2019",
        filing_date="2019-09-01",
        docket_number="E-2, Sub 1142",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-2-sub-1142.pdf",
        content_hash="hash-r-tou",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.family_key == "nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY"


def test_importer_maps_rider_sg_title_to_carolinas_scg_family(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-rider-SCG",
            state="NC",
            company="carolinas",
            tariff_identifier="rider-SCG",
            schedule_code="SCG",
            family_type="rider",
            title="SCG",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "rider-sg.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-sg"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            PageEvidence(
                page_number=2,
                text_length=220,
                text_content="Duke Energy Carolinas, LLC\nNorth Carolina Revised Leaf No. 82",
                has_leaf_header=True,
                extracted_leaf_nos=["82"],
                tariff_vocab_density=0.02,
                header_candidates=["Duke Energy Carolinas, LLC"],
            )
        ],
    )
    monkeypatch.setattr(
        importer_module,
        "segment_document",
        lambda _pages, parent_discovery_id=None: [
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=2,
                end_page=3,
                doc_type="tariff",
                extracted_leaf_nos={"82"},
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=89,
        utility="Duke Energy Progress",
        filing_title="DEC's Compliance Tariff for Rider SG",
        filing_date="2017-06-07",
        docket_number="E-7",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-sg.pdf",
        content_hash="hash-sg",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.company == "carolinas"
    assert stored.family_key == "nc-carolinas-rider-SCG"


def test_importer_maps_long_form_dsm_rider_title_to_edpr_family(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-rider-EDPR",
            state="NC",
            company="carolinas",
            tariff_identifier="rider-EDPR",
            schedule_code="EDPR",
            family_type="rider",
            title="EDPR",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "edpr.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-edpr"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            PageEvidence(
                page_number=2,
                text_length=260,
                text_content=(
                    "Duke Energy Carolinas, LLC\n"
                    "North Carolina Twelfth Revised Leaf No. 64\n"
                    "EXISTING DSM PROGRAM COSTS ADJUSTMENT RIDER (NC)"
                ),
                has_leaf_header=True,
                has_schedule_heading=True,
                extracted_leaf_nos=["64"],
                extracted_schedule_codes=["EXISTING DSM PROGRAM COSTS ADJUSTMENT RIDER (NC)"],
                tariff_vocab_density=0.04,
                header_candidates=[
                    "Duke Energy Carolinas, LLC",
                    "EXISTING DSM PROGRAM COSTS ADJUSTMENT RIDER (NC)",
                ],
            )
        ],
    )
    monkeypatch.setattr(
        importer_module,
        "segment_document",
        lambda _pages, parent_discovery_id=None: [
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=2,
                end_page=3,
                doc_type="tariff",
                extracted_leaf_nos={"64"},
                extracted_schedule_titles={"EXISTING DSM PROGRAM COSTS ADJUSTMENT RIDER (NC)"},
                header_footer_snippets=["EXISTING DSM PROGRAM COSTS ADJUSTMENT RIDER (NC)"],
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=90,
        utility="Duke Energy Progress",
        filing_title="Duke Energy Carolinas, LLC's Existing DSM Program Rider",
        filing_date="2017-07-01",
        docket_number="E-7",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-edpr.pdf",
        content_hash="hash-edpr",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.company == "carolinas"
    assert stored.family_key == "nc-carolinas-rider-EDPR"


def test_importer_creates_provisional_program_family_for_strong_unmatched_tariff_span(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "smart-energy-now.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-sen"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            PageEvidence(
                page_number=2,
                text_length=220,
                text_content=(
                    "Duke Energy Carolinas, LLC\n"
                    "North Carolina First Revised Leaf No. 172\n"
                    "SMART ENERGY NOW PROGRAM (NC)\n"
                    "(Pilot)"
                ),
                has_leaf_header=True,
                has_revised_header=True,
                has_schedule_heading=True,
                extracted_leaf_nos=["172"],
                extracted_schedule_codes=["SMART ENERGY NOW PROGRAM (NC)"],
                tariff_vocab_density=0.03,
                header_candidates=[
                    "Duke Energy Carolinas, LLC",
                    "SMART ENERGY NOW PROGRAM (NC)",
                ],
            )
        ],
    )
    monkeypatch.setattr(
        importer_module,
        "segment_document",
        lambda _pages, parent_discovery_id=None: [
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=2,
                end_page=2,
                doc_type="tariff",
                extracted_leaf_nos={"172"},
                extracted_schedule_titles={"SMART ENERGY NOW PROGRAM (NC)"},
                header_footer_snippets=["SMART ENERGY NOW PROGRAM (NC)"],
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=91,
        utility="Duke Energy Progress",
        filing_title="Duke Energy Carolinas, LLC Revised Tariff",
        filing_date="2014-01-08",
        docket_number="E-7",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-smart-energy-now.pdf",
        content_hash="hash-sen",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    family = repo.get_tariff_family("nc-carolinas-program-SMARTENERGYNOWPROGRAM")
    assert family is not None
    assert family.family_type == "program"
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.family_key == "nc-carolinas-program-SMARTENERGYNOWPROGRAM"
    assert stored.category == "program"


def test_importer_skips_long_weak_family_match_without_leaf_or_schedule_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-535",
            state="NC",
            company="progress",
            tariff_identifier="leaf-535",
            schedule_code="HP",
            family_type="rate_schedule",
            title="Large General Service Hourly Pricing HP",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "rate-design-study.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type("T", (), {"route_recommendation": PipelineRoute.TEXT_PARSE, "file_hash": "hash-study"})(),
    )
    monkeypatch.setattr(
        importer_module,
        "mine_document_pages",
        lambda _: [
            PageEvidence(
                page_number=1,
                text_length=1200,
                text_content="Quarterly rate design study with hourly pricing discussion.",
                tariff_vocab_density=0.02,
                procedural_vocab_density=0.02,
                header_candidates=["Quarterly rate design study"],
            )
        ],
    )
    monkeypatch.setattr(
        importer_module,
        "segment_document",
        lambda _pages, parent_discovery_id=None: [
            TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=1,
                end_page=20,
                doc_type="tariff",
                extracted_schedule_titles={"the way that hourly pricing is included in cost-of-service"},
                header_footer_snippets=["Quarterly rate design study"],
            )
        ],
    )
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, pages: span)

    record = NcucDiscoveryRecord(
        id=92,
        utility="Duke Energy Progress",
        filing_title="Rate design study quarterly report",
        filing_date="2022-01-21",
        docket_number="E-7, Sub 1214",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-7-rate-design-study.pdf",
        content_hash="hash-study",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert created_ids == []


def test_triage_marks_table_heavy_documents_as_structurally_complex(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "table-heavy.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    table_text = "\n".join(
        [
            "Duke Energy Progress Rate Schedule",
            "Leaf No. 502",
            "Customer Charge      $14.00 per month",
            "On-Peak Energy       0.12567 per kWh",
            "Off-Peak Energy      0.05432 per kWh",
            "Demand Charge        $8.22 per kW",
        ] * 25
    )

    class FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def get_text(self, _mode: str) -> str:
            return self._text

    class FakeDoc:
        def __init__(self) -> None:
            self.page_count = 12
            self._pages = [FakePage(table_text) for _ in range(self.page_count)]

        def __getitem__(self, index: int) -> FakePage:
            return self._pages[index]

        def close(self) -> None:
            return None

    monkeypatch.setitem(__import__("sys").modules, "fitz", type("FitzModule", (), {"open": lambda _path: FakeDoc()}))

    triage = triage_pdf(str(pdf_path))

    assert triage.is_likely_scanned is False
    assert triage.is_likely_tariff_related is True
    assert triage.structure_complexity_score >= 0.45
    assert triage.table_mode_candidate == "native_table"
    assert triage.document_archetype_candidate == "compliance_bundle"
    assert "table_heavy_layout" in triage.triage_flags


def test_triage_marks_reading_order_risk_for_merged_heading_lines(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "merged-lines.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    merged_text = (
        "North Carolina Thirty-First Revised Leaf No. 60\n"
        "FUEL COST ADJUSTMENT RIDER (NC) APPLICABILITY (North Carolina Only) MONTHLY CUSTOMER CHARGE\n"
        "AVAILABILITY TYPE OF SERVICE SERVICE RENDERED RATE SCHEDULE\n"
    )

    class FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def get_text(self, _mode: str) -> str:
            return self._text

    class FakeDoc:
        def __init__(self) -> None:
            self.page_count = 3
            self._pages = [FakePage(merged_text) for _ in range(self.page_count)]

        def __getitem__(self, index: int) -> FakePage:
            return self._pages[index]

        def close(self) -> None:
            return None

    monkeypatch.setitem(__import__("sys").modules, "fitz", type("FitzModule", (), {"open": lambda _path: FakeDoc()}))

    triage = triage_pdf(str(pdf_path))

    assert triage.reading_order_risk_score >= 0.45
    assert "reading_order_risk" in triage.triage_flags
    assert triage.document_archetype_candidate == "tariff_sheet"
