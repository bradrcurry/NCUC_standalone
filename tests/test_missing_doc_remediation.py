from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass
class FakeDiscovery:
    id: int
    family_keys: list[str] | None = None
    discovered_url: str | None = None
    viewer_url: str | None = None
    download_url: str | None = None
    attachment_url: str | None = None
    docket_number: str | None = None
    filing_title: str | None = None
    filing_date: str | None = None
    utility: str | None = "Duke Energy Progress"
    metadata_json: str | None = None

    def model_copy(self, update=None):
        return replace(self, **(update or {}))


class FakeRepository:
    def __init__(self):
        self.discovery_by_id: dict[int, FakeDiscovery] = {}
        self.historical_by_id = {}

    def get_ncuc_discovery_record(self, record_id: int):
        return self.discovery_by_id.get(record_id)

    def list_ncuc_discovery_records(self, *, family_key=None, fetch_status=None):
        return list(self.discovery_by_id.values())

    def upsert_ncuc_discovery_record(self, record):
        self.discovery_by_id[int(record.id)] = record
        return int(record.id)

    def get_historical_document(self, historical_id: int):
        return self.historical_by_id.get(historical_id)

    def list_historical_documents(self, *, state=None, company=None):
        return list(self.historical_by_id.values())

    def upsert_historical_document(self, record):
        self.historical_by_id[int(record.id)] = record
        return int(record.id)


@dataclass
class FakeHistoricalDoc:
    id: int
    family_key: str
    title: str
    effective_start: str | None
    start_page: int | None
    end_page: int | None
    local_path: Path
    metadata_json: str | None = None

    def model_copy(self, update=None):
        return replace(self, **(update or {}))


def test_remediate_no_downloadable_url_discovery_records_recovers_viewfile(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_remediation as mod

    repo = FakeRepository()
    repo.discovery_by_id[11] = FakeDiscovery(
        id=11,
        discovered_url="https://starw1.ncuc.gov/NCUC/PSCDocumentDetailsPageNCUC.aspx?DocumentId=abc",
        metadata_json=json.dumps(
            {
                "missing_doc_workflow": {
                    "search_promotion": {
                        "promotable": False,
                        "reasons": ["no_downloadable_url"],
                    }
                }
            }
        ),
    )

    monkeypatch.setattr(mod, "create_authenticated_context", lambda settings: ("pw", "ctx", object()))
    monkeypatch.setattr(mod, "close_authenticated_context", lambda pw, ctx: None)

    class FakeSearcher:
        def __init__(self, settings):
            pass

        def enrich_with_document_details(self, page, results, *, delay_seconds=0.0):
            results[0].view_file_urls = ["https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=123.pdf"]
            results[0].synopsis = "Recovered"
            return results

    monkeypatch.setattr(mod, "DocumentParamSearcher", FakeSearcher)

    report = mod.remediate_no_downloadable_url_discovery_records(
        settings=object(),
        repository=repo,
    )

    assert report["resolved_count"] == 1
    updated = repo.discovery_by_id[11]
    assert updated.download_url == "https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=123.pdf"
    metadata = json.loads(updated.metadata_json or "{}")
    assert metadata["missing_doc_workflow"]["search_remediation"]["resolved"] is True
    assert "no_downloadable_url" not in metadata["missing_doc_workflow"]["search_promotion"]["reasons"]


def test_remediate_no_downloadable_url_discovery_records_marks_unresolved_without_detail_url(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_remediation as mod

    repo = FakeRepository()
    repo.discovery_by_id[21] = FakeDiscovery(
        id=21,
        discovered_url="https://example.com/no-detail",
        metadata_json=json.dumps(
            {
                "missing_doc_workflow": {
                    "search_promotion": {
                        "promotable": False,
                        "reasons": ["no_downloadable_url"],
                    }
                }
            }
        ),
    )

    monkeypatch.setattr(mod, "create_authenticated_context", lambda settings: ("pw", "ctx", object()))
    monkeypatch.setattr(mod, "close_authenticated_context", lambda pw, ctx: None)
    monkeypatch.setattr(mod, "DocumentParamSearcher", lambda settings: None)

    report = mod.remediate_no_downloadable_url_discovery_records(
        settings=object(),
        repository=repo,
    )

    assert report["resolved_count"] == 0
    assert report["unresolved_record_ids"] == [21]
    metadata = json.loads(repo.discovery_by_id[21].metadata_json or "{}")
    assert metadata["missing_doc_workflow"]["search_remediation"]["reason"] == "no_detail_url"


def test_remediate_missing_effective_start_historical_documents_recovers_date(monkeypatch, tmp_path: Path):
    from duke_rates.historical.ncuc import missing_doc_remediation as mod

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    repo = FakeRepository()
    repo.historical_by_id[31] = FakeHistoricalDoc(
        id=31,
        family_key="fk1",
        title="Schedule RES",
        effective_start=None,
        start_page=2,
        end_page=3,
        local_path=pdf_path,
        metadata_json=json.dumps(
            {
                "missing_doc_workflow": {
                    "import_promotion": {
                        "promotable": False,
                        "reasons": ["missing_effective_start_for_weak_match"],
                    }
                }
            }
        ),
    )

    class FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class FakePdf:
        def __init__(self, pages):
            self.pages = pages

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        mod.pdfplumber,
        "open",
        lambda path: FakePdf(
            [
                FakePage("noise"),
                FakePage("Effective for service rendered on and after December 1, 2010"),
                FakePage("continued"),
            ]
        ),
    )

    report = mod.remediate_missing_effective_start_historical_documents(repository=repo)

    assert report["resolved_count"] == 1
    updated = repo.historical_by_id[31]
    assert updated.effective_start == "2010-12-01"
    metadata = json.loads(updated.metadata_json or "{}")
    assert metadata["missing_doc_workflow"]["import_remediation"]["resolved"] is True
    assert "missing_effective_start_for_weak_match" not in metadata["missing_doc_workflow"]["import_promotion"]["reasons"]


def test_remediate_missing_effective_start_historical_documents_marks_unresolved(monkeypatch, tmp_path: Path):
    from duke_rates.historical.ncuc import missing_doc_remediation as mod

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    repo = FakeRepository()
    repo.historical_by_id[41] = FakeHistoricalDoc(
        id=41,
        family_key="fk1",
        title="Schedule RES",
        effective_start=None,
        start_page=1,
        end_page=1,
        local_path=pdf_path,
        metadata_json=json.dumps(
            {
                "missing_doc_workflow": {
                    "import_promotion": {
                        "promotable": False,
                        "reasons": ["missing_effective_start_for_weak_match"],
                    }
                }
            }
        ),
    )

    class FakePage:
        def extract_text(self):
            return "No effective date here"

    class FakePdf:
        def __init__(self, pages):
            self.pages = pages

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(mod.pdfplumber, "open", lambda path: FakePdf([FakePage()]))

    report = mod.remediate_missing_effective_start_historical_documents(repository=repo)

    assert report["resolved_count"] == 0
    assert report["unresolved_historical_document_ids"] == [41]
    metadata = json.loads(repo.historical_by_id[41].metadata_json or "{}")
    assert metadata["missing_doc_workflow"]["import_remediation"]["reason"] == "effective_date_not_found_in_page_span"


def test_remediate_confidence_below_threshold_discovery_records_reruns_family_search(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_remediation as mod

    repo = FakeRepository()
    repo.discovery_by_id[51] = FakeDiscovery(
        id=51,
        metadata_json=json.dumps(
            {
                "missing_doc_workflow": {
                    "search_promotion": {
                        "promotable": False,
                        "reasons": ["confidence_below_threshold:22.00"],
                    }
                }
            }
        ),
    )
    repo.discovery_by_id[51].family_keys = ["fk1"]

    def fake_search(settings, repository, **kwargs):
        assert kwargs["family_key"] == "fk1"
        assert kwargs["structured_max_results"] == 100
        assert kwargs["keyword_max_results"] == 40
        return {
            "rows": [
                {
                    "persisted_discovery_ids": [88, 89],
                }
            ]
        }

    monkeypatch.setattr(mod, "search_nc_missing_clean_documents", fake_search)

    report = mod.remediate_confidence_below_threshold_discovery_records(
        settings=object(),
        repository=repo,
    )

    assert report["rerun_family_keys"] == ["fk1"]
    assert report["updated_record_ids"] == [88, 89]
    metadata = json.loads(repo.discovery_by_id[51].metadata_json or "{}")
    assert metadata["missing_doc_workflow"]["search_requery_remediation"]["resolved"] is True
    assert metadata["missing_doc_workflow"]["search_requery_remediation"]["new_record_ids"] == [88, 89]


def test_remediate_and_promote_missing_doc_targets_chains_search_reason(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_remediation as mod

    captured = {}

    monkeypatch.setattr(
        mod,
        "remediate_no_downloadable_url_discovery_records",
        lambda *args, **kwargs: {
            "selected_count": 1,
            "resolved_count": 1,
            "updated_record_ids": [77],
            "unresolved_record_ids": [],
        },
    )
    monkeypatch.setattr(
        mod,
        "promote_nc_missing_doc_targets",
        lambda settings, repository, **kwargs: captured.setdefault("calls", []).append(kwargs) or {
            "from_stage": "fetch",
            "to_stage": "queue_reprocess",
            "stages": {},
            "discovery_record_ids": kwargs.get("discovery_record_ids", []),
            "historical_document_ids": kwargs.get("historical_document_ids", []),
        },
    )

    report = mod.remediate_and_promote_missing_doc_targets(
        settings=object(),
        repository=object(),
        family_key="fk1",
        reasons=["no_downloadable_url"],
    )

    assert report["remediation_reports"]["no_downloadable_url"]["resolved_count"] == 1
    assert captured["calls"][0]["scope"] == "search_hits"
    assert captured["calls"][0]["discovery_record_ids"] == [77]


def test_remediate_and_promote_missing_doc_targets_chains_import_reason(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_remediation as mod

    captured = {}

    monkeypatch.setattr(
        mod,
        "remediate_missing_effective_start_historical_documents",
        lambda *args, **kwargs: {
            "selected_count": 1,
            "resolved_count": 1,
            "updated_historical_document_ids": [91],
            "unresolved_historical_document_ids": [],
        },
    )
    monkeypatch.setattr(
        mod,
        "promote_nc_missing_doc_targets",
        lambda settings, repository, **kwargs: captured.setdefault("calls", []).append(kwargs) or {
            "from_stage": "queue_reprocess",
            "to_stage": "queue_reprocess",
            "stages": {},
            "discovery_record_ids": kwargs.get("discovery_record_ids", []),
            "historical_document_ids": kwargs.get("historical_document_ids", []),
        },
    )

    report = mod.remediate_and_promote_missing_doc_targets(
        settings=object(),
        repository=object(),
        reasons=["missing_effective_start_for_weak_match"],
    )

    assert report["remediation_reports"]["missing_effective_start_for_weak_match"]["resolved_count"] == 1
    assert captured["calls"][0]["scope"] == "imported_docs"
    assert captured["calls"][0]["historical_document_ids"] == [91]


def test_remediate_and_promote_missing_doc_targets_chains_confidence_reason(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_remediation as mod

    captured = {}

    monkeypatch.setattr(
        mod,
        "remediate_confidence_below_threshold_discovery_records",
        lambda *args, **kwargs: {
            "selected_count": 1,
            "rerun_family_keys": ["fk1"],
            "updated_record_ids": [303],
            "unresolved_record_ids": [],
        },
    )
    monkeypatch.setattr(
        mod,
        "promote_nc_missing_doc_targets",
        lambda settings, repository, **kwargs: captured.setdefault("calls", []).append(kwargs) or {
            "from_stage": "fetch",
            "to_stage": "queue_reprocess",
            "stages": {},
            "discovery_record_ids": kwargs.get("discovery_record_ids", []),
            "historical_document_ids": kwargs.get("historical_document_ids", []),
        },
    )

    report = mod.remediate_and_promote_missing_doc_targets(
        settings=object(),
        repository=object(),
        reasons=["confidence_below_threshold"],
        promotion_min_confidence=30.0,
    )

    assert report["remediation_reports"]["confidence_below_threshold"]["updated_record_ids"] == [303]
    assert captured["calls"][0]["scope"] == "search_hits"
    assert captured["calls"][0]["discovery_record_ids"] == [303]
    assert captured["calls"][0]["promotion_min_confidence"] == 30.0
