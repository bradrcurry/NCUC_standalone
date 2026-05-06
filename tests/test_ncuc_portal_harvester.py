from duke_rates.config import Settings
from duke_rates.historical.ncuc.document_param_search import DocParamSearchResult
from duke_rates.historical.ncuc.portal_harvester import PortalSearchHarvester
from duke_rates.historical.ncuc.query_builder import QuerySpec
from duke_rates.historical.ncuc.result_harvester import SearchResult


def test_portal_harvester_maps_doc_types_to_filing_types() -> None:
    harvester = PortalSearchHarvester(Settings())
    specs = harvester._build_portal_specs(
        utility="Duke Energy Progress",
        query_specs=[],
        doc_types=["tariff", "order"],
        max_results=100,
    )

    assert len(specs) == 1
    assert specs[0].company_name == "Duke Energy Progress"
    assert specs[0].filing_types == ("TARIFF", "ORDER")


def test_portal_harvester_adds_order_to_tariff_like_searches() -> None:
    harvester = PortalSearchHarvester(Settings())
    specs = harvester._build_portal_specs(
        utility="Duke Energy Progress",
        query_specs=[],
        doc_types=["tariff", "schedule"],
        max_results=100,
    )

    assert len(specs) == 1
    assert specs[0].filing_types == ("TARIFF", "RATESCED", "ORDER")


def test_portal_harvester_expands_neighbor_dockets_from_query_hints() -> None:
    harvester = PortalSearchHarvester(Settings())
    specs = harvester._build_portal_specs(
        utility="Duke Energy Progress",
        query_specs=[
            QuerySpec(
                query_text="portal docket:E-2 Sub 974 fuel adjustment",
                template_name="test",
                utility_hint="Duke Energy Progress",
                doc_type_hint="tariff",
            )
        ],
        doc_types=["tariff"],
        max_results=25,
    )

    assert [spec.docket_number for spec in specs] == [
        "E-2 Sub 972",
        "E-2 Sub 973",
        "E-2 Sub 974",
        "E-2 Sub 975",
        "E-2 Sub 976",
    ]


def test_portal_harvester_extracts_date_bounds_from_query_notes() -> None:
    harvester = PortalSearchHarvester(Settings())
    specs = harvester._build_portal_specs(
        utility="Duke Energy Progress",
        query_specs=[
            QuerySpec(
                query_text="portal fuel adjustment",
                template_name="test",
                utility_hint="Duke Energy Progress",
                doc_type_hint="tariff",
                notes=["date_after=2012-11-01", "date_before=2012-11-30"],
            )
        ],
        doc_types=["tariff"],
        max_results=25,
    )

    assert len(specs) == 1
    assert specs[0].date_after == "11/01/2012"
    assert specs[0].date_before == "11/30/2012"


def test_portal_harvester_converts_doc_param_result_to_search_result() -> None:
    harvester = PortalSearchHarvester(Settings())
    query_spec = QuerySpec(
        query_text="portal:company=Duke Energy Progress types=TARIFF,RATESCED",
        template_name="portal_structured_search",
        utility_hint="Duke Energy Progress",
        doc_type_hint="TARIFF,RATESCED",
    )
    row = DocParamSearchResult(
        description="Residential Service Rate Schedule 602",
        doc_type="RATESCED",
        date_filed="03/19/2026",
        docket_number="E-2 Sub 1300",
        docket_id="123",
        company_name="Duke Energy Progress",
        document_detail_url="https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId=abc",
        extracted_schedule_codes=["602"],
        extracted_rider_codes=[],
        filing_classification="tariff_sheets",
    )

    result = harvester._convert_result(row, query_spec)

    assert result is not None
    assert result.title == "Residential Service Rate Schedule 602"
    assert result.filing_date == "2026-03-19"
    assert result.docket_number == "E-2 Sub 1300"
    assert result.extracted_schedule_codes == ["602"]
    assert result.source_template == "portal_structured_search"


def test_portal_harvester_keeps_structural_rate_case_pair_when_titles_are_generic() -> None:
    harvester = PortalSearchHarvester(Settings())
    results = [
        SearchResult(
            url="https://example.com/order",
            title="Order Approving Fuel Adjustment",
            snippet="Filed In: E-2 Sub 974",
            filing_date="2012-10-25",
            docket_number="E-2 Sub 974",
            sub_number=None,
            source_query="portal",
            source_template="portal_structured_search",
            utility_hint="Duke Energy Progress",
            doc_type_hint="TARIFF,RATESCED,ORDER",
            schedule_code_hint="RES",
            rider_code_hint=None,
            filing_classification="order",
        ),
        SearchResult(
            url="https://example.com/compliance",
            title="PEC's Compliance Filing",
            snippet="Revised Rate Tariffs to Reflect the Approved Fuel Charge",
            filing_date="2012-11-01",
            docket_number="E-2 Sub 974",
            sub_number=None,
            source_query="portal",
            source_template="portal_structured_search",
            utility_hint="Duke Energy Progress",
            doc_type_hint="TARIFF,RATESCED,ORDER",
            schedule_code_hint="RES",
            rider_code_hint=None,
            filing_classification="compliance_tariff",
        ),
    ]

    filtered = harvester._filter_targeted_results(
        results,
        query_specs=[
            QuerySpec(
                query_text="portal RES fuel",
                template_name="test",
                utility_hint="Duke Energy Progress",
                doc_type_hint="tariff",
                schedule_code_hint="RES",
            )
        ],
        broad_run=False,
    )

    assert [result.title for result in filtered] == [
        "Order Approving Fuel Adjustment",
        "PEC's Compliance Filing",
    ]
