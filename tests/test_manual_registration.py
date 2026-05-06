from duke_rates.historical.ncuc.manual_registration import (
    RegistrationTargetHints,
    _suggest_from_pages,
)
from duke_rates.models.pipeline import PageEvidence, TariffSpan


def test_suggest_from_pages_picks_matching_tariff_span_and_extracts_footer_metadata() -> None:
    pages = [
        PageEvidence(
            page_number=1,
            text_length=80,
            text_content="TABLE OF CONTENTS\nSchedule RES\nSchedule R-TOUD\n",
            extracted_schedule_codes=["TABLE OF CONTENTS"],
        ),
        PageEvidence(
            page_number=2,
            text_length=220,
            text_content=(
                "Schedule RES\nResidential Service\n"
                "NC Revised Leaf No. 500\nSheet 1 of 2\n"
            ),
            extracted_leaf_nos=["500"],
            extracted_schedule_codes=["Schedule RES", "Residential Service"],
            has_leaf_header=True,
            has_schedule_heading=True,
        ),
        PageEvidence(
            page_number=3,
            text_length=320,
            text_content=(
                "Schedule RES\nSheet 2 of 2\n"
                "Supersedes Schedule RES-14\n"
                "Effective for service rendered on and after December 1, 2012\n"
                "NCUC Docket No. E-2, Sub 976, Order dated November 15, 2012\n"
            ),
            extracted_leaf_nos=["500"],
            extracted_schedule_codes=["Schedule RES"],
            has_leaf_header=True,
            has_schedule_heading=True,
            has_effective_date_phrase=True,
            has_docket_phrase=True,
        ),
        PageEvidence(
            page_number=4,
            text_length=220,
            text_content="Schedule R-TOUD\nResidential Time of Use Demand\nNC Revised Leaf No. 501\n",
            extracted_leaf_nos=["501"],
            extracted_schedule_codes=["Schedule R-TOUD", "Residential Time of Use Demand"],
            has_leaf_header=True,
            has_schedule_heading=True,
        ),
    ]
    spans = [
        TariffSpan(
            start_page=2,
            end_page=3,
            doc_type="tariff",
            extracted_leaf_nos={"500"},
            extracted_schedule_titles={"Schedule RES", "Residential Service"},
            header_footer_snippets=["Sheet 2 of 2", "Supersedes Schedule RES-14"],
        ),
        TariffSpan(
            start_page=4,
            end_page=4,
            doc_type="tariff",
            extracted_leaf_nos={"501"},
            extracted_schedule_titles={"Schedule R-TOUD"},
            header_footer_snippets=[],
        ),
    ]

    suggestion = _suggest_from_pages(
        spans=spans,
        pages=pages,
        hints=RegistrationTargetHints(
            family_key="nc-progress-leaf-500",
            leaf_no="500",
            code="RES",
            title="Residential Service",
            aliases=("Residential Service", "RES", "500"),
        ),
    )

    assert suggestion is not None
    assert suggestion.start_page == 2
    assert suggestion.end_page == 3
    assert suggestion.effective_start == "2012-12-01"
    assert suggestion.supersedes_label == "RES-14"
    assert suggestion.leaf_no == "500"
    assert suggestion.docket_number == "E-2, Sub 976"
    assert suggestion.order_date == "November 15, 2012"
    assert suggestion.title == "Schedule RES"


def test_suggest_from_pages_returns_none_when_no_tariff_span_matches() -> None:
    pages = [
        PageEvidence(
            page_number=1,
            text_length=120,
            text_content="Certificate of Service\nProcedural filing\n",
            extracted_schedule_codes=[],
        )
    ]
    spans = [
        TariffSpan(
            start_page=1,
            end_page=1,
            doc_type="procedural",
            extracted_leaf_nos=set(),
            extracted_schedule_titles=set(),
            header_footer_snippets=[],
        )
    ]

    suggestion = _suggest_from_pages(
        spans=spans,
        pages=pages,
        hints=RegistrationTargetHints(
            family_key="nc-progress-leaf-500",
            leaf_no="500",
            code="RES",
            aliases=("Residential Service",),
        ),
    )

    assert suggestion is None
