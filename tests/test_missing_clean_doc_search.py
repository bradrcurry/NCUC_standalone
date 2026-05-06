from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from duke_rates.models.ncuc import (
    NcucAcquisitionMethod,
    NcucDiscoveryRecord,
    NcucFilingClassification,
)


class FakeRepository:
    def __init__(self):
        self.discovery_records = []
        self.historical_leads = []
        self.docket_leads = []

    def upsert_ncuc_discovery_record(self, record):
        self.discovery_records.append(record)
        return len(self.discovery_records)

    def upsert_historical_lead(self, record):
        self.historical_leads.append(record)
        return len(self.historical_leads)

    def upsert_regulatory_docket_lead(self, record):
        self.docket_leads.append(record)
        return len(self.docket_leads)


def test_search_nc_missing_clean_documents_persists_structured_and_keyword_candidates(monkeypatch, tmp_path: Path):
    from duke_rates.historical.ncuc import missing_clean_doc_search as mod
    from duke_rates.historical.ncuc.document_param_search import DocParamSearchResult

    monkeypatch.setattr(
        mod,
        "build_nc_missing_clean_doc_audit",
        lambda database_path=None: {
            "generated_at": "2026-04-20",
            "rows": [
                {
                    "family_key": "nc-progress-leaf-500",
                    "utility": "DEP",
                    "title": "Schedule RES",
                    "family_type": "rate_schedule",
                    "schedule_code": "RES",
                    "missing_kind": "missing_superseded_revision",
                    "priority_band": "high",
                    "priority_score": 88,
                    "evidence_summary": "missing supersedes labels: RES-12",
                    "suggested_dockets": '["E-2, Sub 976"]',
                    "suggested_query_terms": '["RES-12", "Revised Rate Tariffs"]',
                    "suggested_portal_filing_types": '["TARIFF","RATESCED","ORDER"]',
                    "suggested_date_after": "2010-10-01",
                    "suggested_date_before": "2010-12-15",
                    "redline_search_hint": "Supersedes RES-12",
                }
            ],
        },
    )

    monkeypatch.setattr(mod, "create_authenticated_context", lambda settings: ("pw", "ctx", object()))
    monkeypatch.setattr(mod, "close_authenticated_context", lambda pw, ctx: None)

    class FakeSearcher:
        def __init__(self, settings):
            pass

        def search(self, page, **kwargs):
            return [
                DocParamSearchResult(
                    description="Revised Rate Tariffs to Reflect the Approved Fuel Charge",
                    doc_type="TARIFF",
                    date_filed="11/15/2010",
                    docket_number="E-2 Sub 976",
                    docket_id="dock-1",
                    company_name="Duke Energy Progress",
                    document_detail_url="https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId=abc",
                    document_id="abc",
                    extracted_schedule_codes=["RES"],
                    extracted_rider_codes=[],
                    filing_classification="tariff_sheets",
                )
            ]

        def enrich_with_document_details(self, page, results, *, delay_seconds=0.5):
            results[0].view_file_urls = ["https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=abc"]
            results[0].view_file_labels = ["PDF"]
            results[0].synopsis = "Tariff sheets"
            return results

    monkeypatch.setattr(mod, "DocumentParamSearcher", FakeSearcher)

    class FakeDiscoveryService:
        def __init__(self, settings):
            pass

        def search_public_site(self, query, max_results=20):
            record = NcucDiscoveryRecord(
                docket_number="E-2 Sub 976",
                sub_number="976",
                utility="Duke Energy Progress",
                filing_title="Order Approving Fuel Charge",
                filing_date="11/03/2010",
                proceeding_type="order",
                filing_classification=NcucFilingClassification.ORDER,
                referenced_schedule_codes=["RES"],
                family_keys=[],
                discovered_url="https://www.ncuc.gov/orders/order-976.pdf",
                viewer_url="https://www.ncuc.gov/orders/order-976.pdf",
                attachment_url="https://www.ncuc.gov/orders/order-976.pdf",
                download_url="https://www.ncuc.gov/orders/order-976.pdf",
                acquisition_method=NcucAcquisitionMethod.SEARCH_ENGINE,
            )
            yield SimpleNamespace(record=record, relevance_score=0.7, notes=["ncuc_zoom"])

        def close(self):
            return None

    monkeypatch.setattr(mod, "NcucDiscoveryService", FakeDiscoveryService)

    manifest_path = tmp_path / "harvest.jsonl"
    monkeypatch.setattr(
        mod.search_persistence,
        "save_harvest_session",
        lambda session, settings: manifest_path,
    )

    settings = SimpleNamespace(ncid_username="user", ncid_password="pass")
    repo = FakeRepository()
    report = mod.search_nc_missing_clean_documents(
        settings,
        repo,
        persist=True,
        save_manifest=True,
        limit=5,
    )

    assert report["lead_count"] == 1
    assert report["persisted_discovery_count"] == 2
    assert report["persisted_historical_lead_count"] == 2
    assert report["persisted_docket_lead_count"] == 2
    assert report["harvest_path"] == str(manifest_path)
    assert repo.discovery_records[0].family_keys == ["nc-progress-leaf-500"]
    assert repo.discovery_records[0].search_ideality in {"ideal", "probable"}
    assert float(repo.discovery_records[0].search_confidence_score or 0.0) > 0
    assert repo.historical_leads[0].extraction_method == "structured_portal_missing_clean_doc_search"
    assert "missing_clean_doc_search" in (repo.historical_leads[0].metadata_json or "")


def test_search_nc_missing_clean_documents_skips_persistence_in_dry_run(monkeypatch):
    from duke_rates.historical.ncuc import missing_clean_doc_search as mod

    monkeypatch.setattr(
        mod,
        "build_nc_missing_clean_doc_audit",
        lambda database_path=None: {
            "generated_at": "2026-04-20",
            "rows": [],
        },
    )

    repo = FakeRepository()
    settings = SimpleNamespace(ncid_username=None, ncid_password=None)
    report = mod.search_nc_missing_clean_documents(
        settings,
        repo,
        persist=False,
        save_manifest=False,
    )

    assert report["lead_count"] == 0
    assert not repo.discovery_records


def test_build_structured_query_specs_adds_expanded_and_docketless_searches():
    from duke_rates.historical.ncuc import missing_clean_doc_search as mod

    lead_row = {
        "family_key": "nc-progress-leaf-500",
        "utility": "DEP",
        "title": "Schedule RES",
        "family_type": "rate_schedule",
        "schedule_code": "RES",
        "missing_kind": "missing_superseded_revision",
        "priority_band": "high",
        "priority_score": 88,
        "evidence_summary": "missing supersedes labels",
        "suggested_dockets": '["E-2, Sub 976"]',
        "suggested_query_terms": '["RES-12"]',
        "suggested_portal_filing_types": '["TARIFF","RATESCED"]',
        "suggested_date_after": "2010-10-01",
        "suggested_date_before": "2010-12-15",
    }

    specs = mod._build_structured_query_specs(lead_row)
    notes = [tuple(spec.notes) for spec in specs]

    assert any("search_scope=exact_docket" in item for note in notes for item in note)
    assert any("search_scope=expanded_docket" in item for note in notes for item in note)
    assert any("search_scope=docketless_broad" in item for note in notes for item in note)
    assert any("docket=E-2 Sub 975" in item for note in notes for item in note)
    assert any("docket=E-2 Sub 979" in item for note in notes for item in note)


def test_build_keyword_queries_expands_terms_and_dockets():
    from duke_rates.historical.ncuc import missing_clean_doc_search as mod

    lead_row = {
        "family_key": "nc-progress-leaf-500",
        "utility": "DEP",
        "title": "Schedule RES",
        "family_type": "rate_schedule",
        "schedule_code": "RES",
        "suggested_dockets": '["E-2, Sub 976"]',
        "suggested_query_terms": '["RES-12"]',
        "suggested_date_after": "2010-10-01",
        "suggested_date_before": "2010-12-15",
        "redline_search_hint": "Supersedes RES-12",
    }

    queries = mod._build_keyword_queries(lead_row)

    query_texts = {query.query_text for query in queries}
    docket_hints = {query.docket_hint for query in queries}
    assert any("Schedule RES" in text for text in query_texts)
    assert any("Leaf 500" in text for text in query_texts)
    assert None in docket_hints
    assert "E-2 Sub 976" in docket_hints
    assert len([item for item in docket_hints if item and item.startswith("E-2 Sub ")]) >= 4
    assert any(item not in {None, "E-2 Sub 976"} for item in docket_hints)


def test_ncuc_discovery_service_search_public_site_delegates_to_keyword_search():
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucSearchQuery

    service = object.__new__(NcucDiscoveryService)
    expected = [SimpleNamespace(record="sentinel", relevance_score=0.9, notes=["ok"])]

    def fake_search(query, *, max_results=100):
        assert query.query_text == "Schedule RES"
        assert max_results == 7
        yield from expected

    service.search_edocket_keyword = fake_search

    actual = list(
        service.search_public_site(
            NcucSearchQuery(query_text="Schedule RES"),
            max_results=7,
        )
    )

    assert actual == expected
