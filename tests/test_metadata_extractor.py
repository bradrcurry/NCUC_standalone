from duke_rates.historical.ncuc.pipeline.metadata_extractor import extract_dates_from_span
from duke_rates.models.pipeline import TariffSpan


def test_extract_dates_from_span_scans_later_pages_for_effective_date():
    span = TariffSpan(
        start_page=1,
        end_page=3,
        doc_type="tariff",
        extracted_leaf_nos={"500"},
        extracted_schedule_titles={"RES"},
        header_footer_snippets=[],
    )
    pages = {
        1: "Residential Service Schedule RES",
        2: "Availability and monthly rates.",
        3: "Supersedes Schedule RES-43 Effective for service rendered on and after December 1, 2017",
    }

    dates = extract_dates_from_span(span, pages)

    assert dates
    assert dates[0].date_value == "2017-12-01"
    assert dates[0].page_number == 3


def test_extract_dates_from_span_accepts_period_between_day_and_year():
    span = TariffSpan(
        start_page=1,
        end_page=1,
        doc_type="tariff",
        extracted_leaf_nos={"106"},
        extracted_schedule_titles={"BPM TRUE-UP RIDER"},
        header_footer_snippets=[],
    )
    pages = {
        1: "Effective September 25.2013\nBPM True-Up Rider",
    }

    dates = extract_dates_from_span(span, pages)

    assert dates
    assert dates[0].date_value == "2013-09-25"
