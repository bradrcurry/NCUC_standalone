from duke_rates.historical.ncuc.document_param_search import (
    DocParamSearchResult,
    _find_next_page_target,
)


def test_find_next_page_target_prefers_next_numeric_page() -> None:
    html = """
    <html><body>
      <a href="javascript:__doPostBack('pager$ctl01','')">2</a>
      <a href="javascript:__doPostBack('pager$ctl02','')">3</a>
      <a href="javascript:__doPostBack('pager$ctl10','')">...</a>
    </body></html>
    """

    assert _find_next_page_target(html, current_page=1) == "pager$ctl01"


def test_find_next_page_target_falls_back_to_ellipsis() -> None:
    html = """
    <html><body>
      <a href="javascript:__doPostBack('pager$ctl00','')">1</a>
      <a href="javascript:__doPostBack('pager$ctl10','')">...</a>
    </body></html>
    """

    assert _find_next_page_target(html, current_page=10) == "pager$ctl10"


def test_is_tariff_related_keeps_unresolved_placeholder_rows() -> None:
    row = DocParamSearchResult(
        description="Click the to view the document.",
        doc_type="",
        date_filed="10/17/2022",
        docket_number="E-2 Sub 1295",
        docket_id="abc",
        company_name="Duke Energy Progress",
        document_detail_url="https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId=abc",
    )

    assert row.is_tariff_related() is True


def test_is_tariff_related_rejects_explicit_non_tariff_rows() -> None:
    row = DocParamSearchResult(
        description="Order Approving Fuel Adjustment",
        doc_type="ORDER",
        date_filed="10/17/2022",
        docket_number="E-2 Sub 1295",
        docket_id="abc",
        company_name="Duke Energy Progress",
        document_detail_url="https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId=abc",
    )

    assert row.is_tariff_related() is False
