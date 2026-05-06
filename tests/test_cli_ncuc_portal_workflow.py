from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from duke_rates import cli


def test_ncuc_portal_smoke_test_success(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(ncid_username="tester"), None),
    )

    import duke_rates.historical.ncuc.session as session

    monkeypatch.setattr(session, "create_authenticated_context", lambda settings: ("pw", "ctx", "page"))
    monkeypatch.setattr(session, "close_authenticated_context", lambda pw, ctx: None)
    monkeypatch.setattr(
        session,
        "resolve_docket_ids",
        lambda page, docket_number: [
            {
                "docket_number": "E-2, Sub 1354",
                "docket_id": "9b3614b6-11d6-4703-8d18-5e2e2ef3d705",
                "href": "https://example.test/docket",
                "match_type": "exact",
            }
        ],
    )
    monkeypatch.setattr(
        session,
        "test_authenticated_access",
        lambda page, docket_id: {
            "accessible": True,
            "cf_blocked": False,
            "status_code": 200,
        },
    )
    monkeypatch.setattr(
        session,
        "get_docket_documents",
        lambda page, docket_id: [
            {
                "doc_type": "TARIFF",
                "date_filed": "11/24/2025",
                "description": "Example tariff filing",
                "document_url": "https://example.test/doc",
                "view_file_urls": ["https://example.test/viewfile"],
            }
        ],
    )

    result = runner.invoke(cli.app, ["ncuc-portal-smoke-test"])

    assert result.exit_code == 0
    assert "Smoke test SUCCESS" in result.stdout
    assert "doc_inventory: 1 documents" in result.stdout


def test_ncuc_portal_search_exact_docket_branch(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(ncid_username="tester"), None),
    )

    import duke_rates.historical.ncuc.session as session

    monkeypatch.setattr(session, "create_authenticated_context", lambda settings: ("pw", "ctx", "page"))
    monkeypatch.setattr(session, "close_authenticated_context", lambda pw, ctx: None)
    monkeypatch.setattr(
        session,
        "resolve_docket_ids",
        lambda page, docket_number: [
            {
                "docket_number": "E-2, Sub 1354",
                "docket_id": "9b3614b6-11d6-4703-8d18-5e2e2ef3d705",
                "href": "https://example.test/docket",
                "match_type": "exact",
            }
        ],
    )
    monkeypatch.setattr(
        session,
        "get_docket_documents",
        lambda page, docket_id: [
            {
                "doc_type": "TARIFF",
                "date_filed": "11/24/2025",
                "description": "Example tariff filing",
                "document_url": "https://example.test/doc",
                "view_file_urls": ["https://example.test/viewfile"],
            }
        ],
    )

    result = runner.invoke(cli.app, ["ncuc-portal-search", "--docket-number", "E-2, Sub 1354"])

    assert result.exit_code == 0
    assert "Search mode: authenticated exact-docket" in result.stdout
    assert "Found 1 docket documents." in result.stdout
    assert "ncuc-docket-fetch 9b3614b6-11d6-4703-8d18-5e2e2ef3d705" in result.stdout


def test_ncuc_portal_search_structured_branch(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    export_json = tmp_path / "portal-results.json"

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(ncid_username="tester"), None),
    )

    import duke_rates.historical.ncuc.session as session
    import duke_rates.historical.ncuc.document_param_search as doc_search

    monkeypatch.setattr(session, "create_authenticated_context", lambda settings: ("pw", "ctx", "page"))
    monkeypatch.setattr(session, "close_authenticated_context", lambda pw, ctx: None)

    class FakeSearcher:
        def __init__(self, settings):
            self.settings = settings

        def search(self, page, **kwargs):
            return [
                doc_search.DocParamSearchResult(
                    description="Duke Energy Progress tariff filing",
                    doc_type="TARIFF",
                    date_filed="11/24/2025",
                    docket_number="E-2 Sub 1354",
                    docket_id="9b3614b6-11d6-4703-8d18-5e2e2ef3d705",
                    company_name="Duke Energy Progress",
                    document_detail_url="https://example.test/doc",
                    extracted_schedule_codes=["RES"],
                    extracted_rider_codes=[],
                    filing_classification="tariff",
                    view_file_urls=["https://example.test/viewfile"],
                )
            ]

    monkeypatch.setattr(doc_search, "DocumentParamSearcher", FakeSearcher)
    monkeypatch.setattr(
        doc_search,
        "print_doc_param_results",
        lambda results, top_n, only_tariff_related: None,
    )

    result = runner.invoke(
        cli.app,
        [
            "ncuc-portal-search",
            "--company",
            "Duke Energy Progress",
            "--after",
            "11/01/2025",
            "--before",
            "12/31/2025",
            "--json",
            str(export_json),
        ],
    )

    assert result.exit_code == 0
    assert "Search mode: authenticated structured" in result.stdout
    assert "Classification: authenticated structured search completed." in result.stdout
    assert "Exported 1 rows" in result.stdout
    assert export_json.exists()


def test_search_doc_param_classifies_structured_search_failure(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(ncid_username="tester"), None),
    )

    import duke_rates.historical.ncuc.session as session
    import duke_rates.historical.ncuc.document_param_search as doc_search

    monkeypatch.setattr(session, "create_authenticated_context", lambda settings: ("pw", "ctx", "page"))
    monkeypatch.setattr(session, "close_authenticated_context", lambda pw, ctx: None)

    class FailingSearcher:
        def __init__(self, settings):
            self.settings = settings

        def search(self, page, **kwargs):
            raise RuntimeError("Cloudflare blocked DocumentsParameterSearch")

    monkeypatch.setattr(doc_search, "DocumentParamSearcher", FailingSearcher)

    result = runner.invoke(cli.app, ["search-doc-param"])

    assert result.exit_code == 1
    assert "Classification: cloudflare_or_forbidden" in result.stdout
    assert "Authenticated structured search hit a 403/Cloudflare-style failure" in result.stdout


def test_ncuc_docket_fetch_classifies_inventory_failure(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(ncid_username="tester", historical_dir="C:/tmp"), None),
    )

    import duke_rates.historical.ncuc.session as session

    monkeypatch.setattr(session, "create_authenticated_context", lambda settings: ("pw", "ctx", "page"))
    monkeypatch.setattr(session, "close_authenticated_context", lambda pw, ctx: None)
    monkeypatch.setattr(
        session,
        "get_docket_documents",
        lambda page, docket_id: (_ for _ in ()).throw(RuntimeError("403 Forbidden on docket docs")),
    )

    result = runner.invoke(cli.app, ["ncuc-docket-fetch", "9b3614b6-11d6-4703-8d18-5e2e2ef3d705"])

    assert result.exit_code == 1
    assert "Classification: cloudflare_or_forbidden" in result.stdout
    assert "Authenticated docket inventory hit a 403/Cloudflare-style failure" in result.stdout
