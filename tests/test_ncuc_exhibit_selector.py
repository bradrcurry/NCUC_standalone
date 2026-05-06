from pathlib import Path
from types import SimpleNamespace

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.ncuc import exhibit_selector as selector_module
from duke_rates.historical.ncuc.exhibit_selector import (
    NcucExhibitSelector,
    _build_focused_parse_text,
    _choose_import_title,
    _infer_import_category,
    _is_generic_title,
    _record_matches_target,
    _score_candidate,
    _score_text_profile,
)
from duke_rates.models.ncuc import (
    NcucAcquisitionMethod,
    NcucDiscoveryRecord,
    NcucFetchStatus,
    NcucFilingClassification,
)


def test_score_candidate_prefers_tariff_like_attachment(tmp_path: Path) -> None:
    pdf_path = tmp_path / "candidate.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test")
    record = NcucDiscoveryRecord(
        id=1,
        docket_number="E-2, Sub 1109",
        filing_title="RESIDENTIAL SERVICE SCHEDULE RES-42A",
        local_path=str(pdf_path),
        attachment_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=file-1",
        family_keys=["ncuc-dep-605"],
        filing_classification=NcucFilingClassification.TARIFF_SHEETS,
        fetch_status=NcucFetchStatus.SUCCESS,
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
        metadata_json=(
            '{"pdf_content_mining": {"contains_tariff_text": true, '
            '"effective_date": "January 24, 2017", '
            '"derived_title": "RESIDENTIAL SERVICE SCHEDULE RES-42A", '
            '"extracted_schedule_codes": ["605"], '
            '"extracted_rider_codes": [], '
            '"extracted_leaf_nos": ["605"]}}'
        ),
    )

    candidate = _score_candidate(
        record,
        family_key="ncuc-dep-605",
        family_code="605",
        related_dockets={"E-2, Sub 1109"},
    )

    assert candidate.score >= 90
    assert candidate.contains_tariff_text is True
    assert candidate.extracted_schedule_codes == ["605"]


def test_selector_lists_family_candidates(tmp_path: Path, monkeypatch) -> None:
    repo = Repository(tmp_path / "test.db")
    pdf_path = tmp_path / "candidate.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test")
    repo.upsert_ncuc_discovery_record(
        NcucDiscoveryRecord(
            docket_number="E-2, Sub 1109",
            filing_title="RESIDENTIAL SERVICE SCHEDULE RES-42A",
            local_path=str(pdf_path),
            attachment_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=file-1",
            family_keys=["ncuc-dep-605"],
            filing_classification=NcucFilingClassification.TARIFF_SHEETS,
            fetch_status=NcucFetchStatus.SUCCESS,
            acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
            metadata_json=(
                '{"pdf_content_mining": {"contains_tariff_text": true, '
                '"effective_date": "January 24, 2017", '
                '"derived_title": "RESIDENTIAL SERVICE SCHEDULE RES-42A", '
                '"extracted_schedule_codes": ["605"], '
                '"extracted_rider_codes": [], '
                '"extracted_leaf_nos": ["605"]}}'
            ),
        )
    )

    monkeypatch.setattr(
        selector_module,
        "find_target_by_query",
        lambda repository, query, missing_only=False: SimpleNamespace(
            family_key="ncuc-dep-605",
            category="rider",
            title="REPS EMF Rider",
        ),
    )

    selector = NcucExhibitSelector(Settings(database_path=tmp_path / "test.db"), repo)
    candidates = selector.list_candidates(family_query="605", limit=5, min_score=10)

    assert len(candidates) == 1
    assert candidates[0].family_key == "ncuc-dep-605"


def test_record_matches_family_via_docket_and_referenced_code() -> None:
    record = NcucDiscoveryRecord(
        id=9,
        docket_number="E-2, Sub 1254",
        filing_title="CPRE filing",
        referenced_schedule_codes=["640"],
        filing_classification=NcucFilingClassification.TARIFF_SHEETS,
        fetch_status=NcucFetchStatus.SUCCESS,
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
    )

    assert _record_matches_target(
        record,
        family_key="ncuc-dep-640",
        family_code="640",
        related_dockets={"E-2, Sub 1254"},
    )


def test_infer_import_category_prefers_schedule_title() -> None:
    category = _infer_import_category(
        selector_module.NcucExhibitCandidate(
            record_id=1,
            family_key="ncuc-dep-605",
            filing_title="cover letter",
            score=100,
            derived_title="RESIDENTIAL SERVICE SCHEDULE RES-42A",
            extracted_schedule_codes=[],
            extracted_rider_codes=["BA"],
        ),
        fallback_category="rider",
    )

    assert category == "rate"


def test_infer_import_category_marks_testimony_as_other() -> None:
    category = _infer_import_category(
        selector_module.NcucExhibitCandidate(
            record_id=1,
            family_key="ncuc-dep-640",
            filing_title="Supplemental Testimony of Bryan L. Sykes",
            score=80,
            extracted_schedule_codes=[],
            extracted_rider_codes=["CPRE"],
            filing_classification="tariff_sheets",
        ),
        fallback_category="rider",
    )

    assert category == "other"


def test_score_candidate_rewards_alias_title_match(tmp_path: Path) -> None:
    pdf_path = tmp_path / "candidate.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test")
    record = NcucDiscoveryRecord(
        id=2,
        docket_number="E-2, Sub 1254",
        filing_title="Proposed Rider CPRE (NC)",
        local_path=str(pdf_path),
        attachment_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=file-2",
        filing_classification=NcucFilingClassification.TARIFF_SHEETS,
        fetch_status=NcucFetchStatus.SUCCESS,
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
        metadata_json=(
            '{"pdf_content_mining": {"contains_tariff_text": true, '
            '"derived_title": "Proposed Rider CPRE (NC)", '
            '"extracted_rider_codes": ["CPRE"]}}'
        ),
    )

    candidate = _score_candidate(
        record,
        family_key="ncuc-dep-640",
        family_code="640",
        related_dockets={"E-2, Sub 1254"},
    )

    assert any("title matched aliases" in reason for reason in candidate.reasons)


def test_score_text_profile_prefers_exact_family_terms(tmp_path: Path) -> None:
    pdf_path = tmp_path / "candidate.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test")
    text_path = pdf_path.with_suffix(".pdf.txt")
    text_path.write_text(
        "The REPS EMF Rider applies to residential customers.",
        encoding="utf-8",
    )
    record = NcucDiscoveryRecord(
        id=3,
        docket_number="E-2, Sub 1109",
        filing_title="cover letter",
        local_path=str(pdf_path),
        metadata_json=f'{{"pdf_content_mining": {{"text_path": "{text_path.as_posix()}"}}}}',
        filing_classification=NcucFilingClassification.TARIFF_SHEETS,
        fetch_status=NcucFetchStatus.SUCCESS,
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
    )

    profile = _score_text_profile(
        record,
        family_code="605",
        title_text="COVER LETTER",
    )

    assert profile["score_delta"] > 0
    assert profile["positive_hits"] == ["REPS EMF", "REPS EMF RIDER"]


def test_is_generic_title_flags_cover_names() -> None:
    assert _is_generic_title("KENDRICK C. FENTRESS") is True
    assert _is_generic_title("RESIDENTIAL SERVICE SCHEDULE RES-42A") is False


def test_choose_import_title_falls_back_for_generic_titles() -> None:
    title = _choose_import_title(
        selector_module.NcucExhibitCandidate(
            record_id=1,
            family_key="ncuc-dep-610",
            filing_title="Kendrick C. Fentress",
            score=90,
        ),
        fallback_title="Energy Efficiency Rider",
    )

    assert title == "Energy Efficiency Rider"


def test_choose_import_title_falls_back_for_long_docket_sentence() -> None:
    title = _choose_import_title(
        selector_module.NcucExhibitCandidate(
            record_id=1,
            family_key="ncuc-dep-605",
            filing_title=(
                "On January 17, 2017, the Commission issued an Order Approving REPS "
                "and REPS EMF Rider and REPS Compliance Report."
            ),
            score=95,
        ),
        fallback_title="REPS EMF Rider",
    )

    assert title == "REPS EMF Rider"


def test_build_focused_parse_text_extracts_family_specific_excerpt(tmp_path: Path) -> None:
    pdf_path = tmp_path / "candidate.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test")
    text_path = pdf_path.with_suffix(".pdf.txt")
    text_path.write_text(
        "\n\n".join(
            [
                "Cover letter from counsel.",
                (
                    "Notice is hereby given that residential customers would see a DSM rider "
                    "decrease of 0.010 cents per kWh, and an EE rider decrease of "
                    "0.008 cents per kWh."
                ),
                "Unrelated appendix text.",
            ]
        ),
        encoding="utf-8",
    )
    record = NcucDiscoveryRecord(
        id=4,
        docket_number="E-2, Sub 1145",
        filing_title="cover letter",
        local_path=str(pdf_path),
        metadata_json=f'{{"pdf_content_mining": {{"text_path": "{text_path.as_posix()}"}}}}',
        filing_classification=NcucFilingClassification.TARIFF_SHEETS,
        fetch_status=NcucFetchStatus.SUCCESS,
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
    )

    focused_text, metadata = _build_focused_parse_text(record, family_code="611")

    assert focused_text is not None
    assert "DSM rider decrease of 0.010 cents per kWh" in focused_text
    assert metadata is not None
    assert metadata["strategy"] == "ncuc_focused_excerpt"


def test_score_text_profile_suppresses_sts_cost_abbreviation() -> None:
    """STS appearing only in cost-context should not score positively for family 613."""
    from duke_rates.historical.ncuc.exhibit_selector import _score_text_profile

    record = NcucDiscoveryRecord(
        id=10,
        docket_number="E-2, Sub 1206",
        filing_title="Annual DSM/EE Compliance Filing",
        filing_classification=NcucFilingClassification.COMPLIANCE_FILING,
        fetch_status=NcucFetchStatus.SUCCESS,
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
    )
    # Text mentions STS only in cost-deferral context, not as rider name
    title_text = "ANNUAL DSM/EE COMPLIANCE FILING"
    with patch_text_content(
        record,
        "Duke Energy Progress has deferred STS costs incurred under the non-STS cost balance.",
    ):
        profile = _score_text_profile(record, family_code="613", title_text=title_text)

    assert profile["score_delta"] <= 0, "Should not reward STS when only used as cost abbreviation"


def test_score_text_profile_rewards_explicit_rider_sts_reference() -> None:
    """'Rider STS' as an explicit name should score positively for family 613."""
    from duke_rates.historical.ncuc.exhibit_selector import _score_text_profile

    record = NcucDiscoveryRecord(
        id=11,
        docket_number="E-2, Sub 1106",
        filing_title="Storm Securitization Rider",
        filing_classification=NcucFilingClassification.TARIFF_SHEETS,
        fetch_status=NcucFetchStatus.SUCCESS,
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
    )
    title_text = "STORM SECURITIZATION RIDER"
    with patch_text_content(
        record,
        "Rider STS Storm Securitization Rider 0.145 cents per kilowatt-hour Residential RES",
    ):
        profile = _score_text_profile(record, family_code="613", title_text=title_text)

    assert profile["score_delta"] > 0
    assert any("RIDER STS" in hit or "STORM SECURITIZATION RIDER" in hit for hit in profile["positive_hits"])


def test_procedural_filing_penalty_applied() -> None:
    """Testimony filings should receive a score penalty."""
    import tempfile

    pdf_path = Path(tempfile.mktemp(suffix=".pdf"))
    record = NcucDiscoveryRecord(
        id=12,
        docket_number="E-2, Sub 1106",
        filing_title="Testimony of John Smith",
        local_path=str(pdf_path),
        filing_classification=NcucFilingClassification.EXHIBIT,
        fetch_status=NcucFetchStatus.SUCCESS,
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
    )
    candidate = _score_candidate(
        record,
        family_key="ncuc-dep-607",
        family_code="607",
        related_dockets={"E-2, Sub 1106"},
    )
    assert any("procedural" in r for r in candidate.reasons)


def test_cei_extractor_parses_monthly_charge() -> None:
    from duke_rates.parse.heuristics import extract_rider_charge_components

    text = (
        "CLEAN ENERGY IMPACT RIDER (CEI)\n"
        "Residential $1.50 per month\n"
        "Small General Service $3.00 per month\n"
        "APPLICABILITY: Applicable to Schedules: RES\n"
        "Effective January 1, 2024\n"
    )
    components = extract_rider_charge_components(text, rider_code="CEI")
    assert any(c.unit == "fixed_monthly" and c.rate_class == "Residential" for c in components)
    res = next(c for c in components if c.rate_class == "Residential" and c.unit == "fixed_monthly")
    assert res.value == 1.50


def test_dsm_ee_order_table_extractor() -> None:
    from duke_rates.parse.heuristics import extract_rider_charge_components

    text = (
        "The Commission finds that the appropriate forward-looking DSM/EE rates "
        "as filed by Duke Energy Progress are: 0.611 cents per kWh for the "
        "Residential class, 0.622 cents per kWh for the General Service class, "
        "and 0.084 cents per kWh for the Lighting class.\n"
        "RIDER EE\nApplicable to Schedules: RES\nEffective July 1, 2022\n"
    )
    components = extract_rider_charge_components(text, rider_code="EE")
    res = next(
        (c for c in components if c.rate_class == "Residential" and c.unit == "cents_per_kwh"),
        None,
    )
    assert res is not None
    assert res.value == 0.611


import contextlib
from unittest.mock import patch as _mock_patch


@contextlib.contextmanager
def patch_text_content(record: NcucDiscoveryRecord, text: str):
    """Temporarily make _load_candidate_text return the given text for a record."""
    import duke_rates.historical.ncuc.exhibit_selector as _mod

    original = _mod._load_candidate_text

    def _patched(r):
        if r is record:
            return text
        return original(r)

    with _mock_patch.object(_mod, "_load_candidate_text", _patched):
        yield
