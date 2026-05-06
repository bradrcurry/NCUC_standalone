from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


class FakeDoc:
    def __init__(
        self,
        doc_id: int,
        family_key: str,
        effective_start: str | None = "2024-01-01",
        local_path: str = "C:/tmp/doc.pdf",
        evidence_json: str | None = '{"explicit_leaf_hit": 40.0, "tariff_vocab_density": 8.0}',
        start_page: int | None = 1,
        end_page: int | None = 2,
        metadata_json: str | None = "{}",
    ):
        self.id = doc_id
        self.family_key = family_key
        self.effective_start = effective_start
        self.local_path = local_path
        self.evidence_json = evidence_json
        self.start_page = start_page
        self.end_page = end_page
        self.metadata_json = metadata_json


class FakeVersion:
    def __init__(self, version_id: int | None, historical_document_id: int | None):
        self.id = version_id
        self.historical_document_id = historical_document_id


class FakeRepository:
    def __init__(self):
        self.discovery_by_id = {}
        self.docs = []
        self.version_upserts = []

    def get_ncuc_discovery_record(self, record_id: int):
        return self.discovery_by_id.get(record_id)

    def upsert_ncuc_discovery_record(self, record):
        self.discovery_by_id[int(record.id)] = record
        return int(record.id)

    def list_ncuc_discovery_records(self, *, fetch_status=None, family_key=None):
        rows = list(self.discovery_by_id.values())
        if fetch_status:
            rows = [row for row in rows if row.fetch_status == fetch_status]
        if family_key:
            rows = [row for row in rows if family_key in row.family_keys]
        return rows

    def list_historical_documents(self, *, state=None, company=None):
        return list(self.docs)

    def get_historical_document(self, historical_id: int):
        for doc in self.docs:
            if int(doc.id) == int(historical_id):
                return doc
        return None

    def list_tariff_versions(self, family_key: str):
        return [
            FakeVersion(idx + 1, record.historical_document_id)
            for idx, record in enumerate(self.version_upserts)
            if record.family_key == family_key
        ]

    def upsert_tariff_version(self, record):
        self.version_upserts.append(record)
        return len(self.version_upserts)

    def _connect(self):
        class _Conn:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, exc_type, exc, tb):
                return False

            def commit(self_inner):
                return None

        return _Conn()


def test_run_nc_missing_doc_workflow_full_core_path(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_workflow as mod
    from duke_rates.models.ncuc import NcucFetchStatus

    search_rows = [{
        "persisted_discovery_ids": [11, 12],
    }]
    monkeypatch.setattr(
        mod,
        "search_nc_missing_clean_documents",
        lambda *args, **kwargs: {"rows": search_rows},
    )

    repo = FakeRepository()
    repo.discovery_by_id = {
        11: SimpleNamespace(
            id=11,
            fetch_status=NcucFetchStatus.PENDING.value,
            family_keys=["fk1"],
            search_ideality="ideal",
            search_confidence_score=80.0,
            download_url="https://example/doc11.pdf",
            viewer_url="https://example/doc11.pdf",
            attachment_url="https://example/doc11.pdf",
            metadata_json="{}",
        ),
        12: SimpleNamespace(
            id=12,
            fetch_status=NcucFetchStatus.PENDING.value,
            family_keys=["fk1"],
            search_ideality="probable",
            search_confidence_score=72.0,
            download_url="https://example/doc12.pdf",
            viewer_url="https://example/doc12.pdf",
            attachment_url="https://example/doc12.pdf",
            metadata_json="{}",
        ),
    }
    repo.docs = [FakeDoc(101, "fk1"), FakeDoc(102, "fk1")]

    class FakeDownloader:
        def __init__(self, settings, repository):
            pass

        def fetch(self, record):
            record.fetch_status = NcucFetchStatus.SUCCESS
            return record

        def close(self):
            return None

    monkeypatch.setattr(mod, "NcucDownloader", FakeDownloader)

    class FakeImporter:
        def __init__(self, settings, repository):
            pass

        def import_discovery_record(self, record):
            return {
                "historical_document_ids": [101] if int(record.id) == 11 else [102],
                "family_keys_matched": ["fk1"],
            }

    monkeypatch.setattr(mod, "NcucPipelineImporter", FakeImporter)

    queued = {}

    def fake_enqueue(conn, *, historical_document_ids, priority, requested_by, queue_reason, metadata_by_id=None):
        queued["ids"] = list(historical_document_ids)
        queued["priority"] = priority
        queued["requested_by"] = requested_by
        queued["queue_reason"] = queue_reason
        queued["metadata_by_id"] = dict(metadata_by_id or {})
        return {
            "inserted": len(historical_document_ids),
            "skipped": 0,
            "queue_ids": [1, 2],
            "missing_ids": [],
        }

    monkeypatch.setattr(mod, "enqueue_specific_historical_documents", fake_enqueue)

    report = mod.run_nc_missing_doc_workflow(
        settings=SimpleNamespace(),
        repository=repo,
        family_key="fk1",
        limit=10,
        requested_by="test-workflow",
    )

    assert report["discovery_record_ids"] == [11, 12]
    assert report["historical_document_ids"] == [101, 102]
    assert report["stages"]["fetch"]["success_count"] == 2
    assert report["stages"]["fetch"]["promoted_record_ids"] == [11, 12]
    assert report["stages"]["import"]["historical_document_ids"] == [101, 102]
    assert report["stages"]["bootstrap_versions"]["created_count"] == 2
    assert report["stages"]["queue_reprocess"]["promoted_historical_document_ids"] == [101, 102]
    assert queued["ids"] == [101, 102]
    assert queued["queue_reason"] == "missing_doc_workflow"
    assert queued["metadata_by_id"][101]["family_match_score"] == 48.0


def test_run_nc_missing_doc_workflow_can_resume_from_bootstrap(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_workflow as mod

    repo = FakeRepository()
    repo.docs = [FakeDoc(201, "fk2")]

    monkeypatch.setattr(
        mod,
        "enqueue_specific_historical_documents",
        lambda conn, **kwargs: {
            "inserted": 1,
            "skipped": 0,
            "queue_ids": [7],
            "missing_ids": [],
        },
    )

    report = mod.run_nc_missing_doc_workflow(
        settings=SimpleNamespace(),
        repository=repo,
        from_stage="bootstrap_versions",
        to_stage="queue_reprocess",
        family_key="fk2",
        historical_document_ids=[201],
        limit=5,
    )

    assert "search" not in report["stages"]
    assert "fetch" not in report["stages"]
    assert report["stages"]["bootstrap_versions"]["historical_document_ids"] == [201]
    assert report["stages"]["queue_reprocess"]["historical_document_ids"] == [201]


def test_run_nc_missing_doc_workflow_import_stage_mines_historical_documents(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_workflow as mod
    from duke_rates.models.ncuc import NcucFetchStatus

    repo = FakeRepository()
    repo.discovery_by_id = {
        61: SimpleNamespace(
            id=61,
            fetch_status=NcucFetchStatus.SUCCESS.value,
            family_keys=["fk-import"],
            metadata_json="{}",
        ),
    }

    class FakeImporter:
        def __init__(self, settings, repository):
            pass

        def import_discovery_record(self, record):
            return {"family_keys_matched": ["fk-import"]}

        def mine_discovery_record_spans(self, record):
            return [611]

    monkeypatch.setattr(mod, "NcucPipelineImporter", FakeImporter)

    report = mod.run_nc_missing_doc_workflow(
        settings=SimpleNamespace(),
        repository=repo,
        from_stage="import",
        to_stage="import",
        discovery_record_ids=[61],
        limit=5,
    )

    assert report["historical_document_ids"] == [611]
    assert report["stages"]["import"]["historical_document_ids"] == [611]
    assert report["stages"]["import"]["mined_historical_document_ids"] == [611]


def test_run_nc_missing_doc_workflow_defers_weak_search_hits(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_workflow as mod
    from duke_rates.models.ncuc import NcucFetchStatus

    repo = FakeRepository()
    repo.discovery_by_id = {
        31: SimpleNamespace(
            id=31,
            fetch_status=NcucFetchStatus.PENDING.value,
            family_keys=["fk3"],
            search_ideality="possible",
            search_confidence_score=12.0,
            download_url=None,
            viewer_url=None,
            attachment_url=None,
            metadata_json="{}",
        ),
    }

    class FakeDownloader:
        def __init__(self, settings, repository):
            raise AssertionError("Downloader should not run for deferred weak hits")

    monkeypatch.setattr(mod, "NcucDownloader", FakeDownloader)

    report = mod.run_nc_missing_doc_workflow(
        settings=SimpleNamespace(),
        repository=repo,
        from_stage="fetch",
        to_stage="fetch",
        discovery_record_ids=[31],
        limit=10,
        auto_promote_search_hits=True,
        promotion_min_ideality="probable",
        promotion_min_confidence=45.0,
    )

    assert report["stages"]["fetch"]["fetched_count"] == 0
    assert report["stages"]["fetch"]["deferred_record_ids"] == [31]
    metadata = json.loads(repo.discovery_by_id[31].metadata_json)
    assert metadata["missing_doc_workflow"]["search_promotion"]["promotable"] is False
    assert "no_downloadable_url" in metadata["missing_doc_workflow"]["search_promotion"]["reasons"]


def test_run_nc_missing_doc_workflow_defers_weak_imported_docs(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_workflow as mod

    repo = FakeRepository()
    repo.docs = [
        FakeDoc(
            401,
            "fk4",
            effective_start=None,
            evidence_json="{}",
            start_page=7,
            end_page=8,
        ),
    ]

    class FakeEnqueue:
        def __call__(self, conn, **kwargs):
            raise AssertionError("Queue insert should not run for deferred weak imported docs")

    monkeypatch.setattr(mod, "enqueue_specific_historical_documents", FakeEnqueue())

    report = mod.run_nc_missing_doc_workflow(
        settings=SimpleNamespace(),
        repository=repo,
        from_stage="queue_reprocess",
        to_stage="queue_reprocess",
        historical_document_ids=[401],
        auto_promote_imported_docs=True,
        import_promotion_min_family_score=24.0,
    )

    assert report["stages"]["queue_reprocess"]["inserted"] == 0
    assert report["stages"]["queue_reprocess"]["promoted_historical_document_ids"] == []
    assert report["stages"]["queue_reprocess"]["deferred_historical_document_ids"] == [401]
    assert "family_match_below_threshold:0.00" in report["stages"]["queue_reprocess"]["deferred_reasons"][401]
    assert "missing_effective_start_for_weak_match" in report["stages"]["queue_reprocess"]["deferred_reasons"][401]
    metadata = json.loads(repo.docs[0].metadata_json)
    assert metadata["missing_doc_workflow"]["import_promotion"]["promotable"] is False
    assert metadata["missing_doc_workflow"]["import_promotion"]["thresholds"]["min_family_score"] == 24.0


def test_run_nc_missing_doc_workflow_runs_process_and_validate_stages(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_workflow as mod

    monkeypatch.setattr(
        mod,
        "_process_reprocess_stage",
        lambda settings, repository, **kwargs: {
            "historical_document_ids": kwargs["historical_document_ids"],
            "processed": 1,
            "completed": 1,
            "failed": 0,
            "completed_historical_document_ids": kwargs["historical_document_ids"],
            "failed_historical_document_ids": [],
            "queue_ids": [17],
            "latest_run_ids": [71],
        },
    )
    monkeypatch.setattr(
        mod,
        "_validate_missing_doc_targets",
        lambda settings, repository, **kwargs: {
            "family_key": kwargs["family_key"],
            "historical_document_ids": kwargs["historical_document_ids"],
            "discovery_record_ids": kwargs["discovery_record_ids"],
            "target_count": 1,
            "targets": [{"summary": {"historical_document_count": 1}}],
            "triage_summary": {
                "next_action_counts": {"ready_for_acceptance": 1},
                "blocked_reason_counts": {},
                "needs_review_count": 0,
                "queued_reprocess_count": 0,
                "strong_processed_count": 1,
                "weak_or_empty_processed_count": 0,
            },
        },
    )

    report = mod.run_nc_missing_doc_workflow(
        settings=SimpleNamespace(database_path="test.db"),
        repository=FakeRepository(),
        from_stage="process_reprocess",
        to_stage="validate",
        family_key="fk-validate",
        discovery_record_ids=[81],
        historical_document_ids=[901],
        limit=5,
    )

    assert report["stages"]["process_reprocess"]["completed"] == 1
    assert report["stages"]["process_reprocess"]["completed_historical_document_ids"] == [901]
    assert report["stages"]["validate"]["family_key"] == "fk-validate"
    assert report["stages"]["validate"]["historical_document_ids"] == [901]
    assert report["stages"]["validate"]["triage_summary"]["strong_processed_count"] == 1


def test_validate_stage_persists_triage_metadata(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_workflow as mod
    from duke_rates.models.ncuc import NcucFetchStatus

    repo = FakeRepository()
    repo.discovery_by_id = {
        91: SimpleNamespace(
            id=91,
            fetch_status=NcucFetchStatus.SUCCESS.value,
            family_keys=["fk-triage"],
            metadata_json="{}",
        ),
    }
    repo.docs = [FakeDoc(901, "fk-triage", metadata_json="{}")]

    monkeypatch.setattr(
        mod,
        "build_nc_missing_doc_status_report",
        lambda repository, **kwargs: {
            "target": {
                "family_key": kwargs.get("family_key"),
                "discovery_record_id": kwargs.get("discovery_record_id"),
                "historical_document_id": kwargs.get("historical_document_id"),
            },
            "summary": {
                "needs_review_count": 1,
                "queued_reprocess_count": 0,
            },
            "discovery_records": [
                {
                    "id": 91,
                    "next_action": "import_and_mine_document",
                    "blocked_reason": None,
                    "fetch_status": "success",
                    "linked_historical_document_ids": [],
                    "search_promotion_assessment": {"promotable": True, "reasons": []},
                }
            ],
            "historical_documents": [
                {
                    "id": 901,
                    "family_key": "fk-triage",
                    "next_action": "review_parse_output",
                    "blocked_reason": "needs_review",
                    "current_stage": "needs_review",
                    "latest_processing_run": {"status": "completed", "outcome_quality": "weak"},
                    "latest_review": {"outcome": "needs_review"},
                    "latest_reprocess_queue": None,
                    "import_promotion_assessment": {"promotable": True, "reasons": []},
                }
            ],
        },
    )

    report = mod._validate_missing_doc_targets(
        settings=SimpleNamespace(database_path="test.db"),
        repository=repo,
        family_key="fk-triage",
        discovery_record_ids=[91],
        historical_document_ids=[901],
    )

    discovery_meta = json.loads(repo.discovery_by_id[91].metadata_json)
    historical_meta = json.loads(repo.docs[0].metadata_json)

    assert report["triage_summary"]["next_action_counts"]["import_and_mine_document"] >= 1
    assert discovery_meta["missing_doc_workflow"]["triage"]["next_action"] == "import_and_mine_document"
    assert discovery_meta["missing_doc_workflow"]["triage"]["scope"] == "discovery_record"
    assert historical_meta["missing_doc_workflow"]["triage"]["next_action"] == "review_parse_output"
    assert historical_meta["missing_doc_workflow"]["triage"]["blocked_reason"] == "needs_review"
    assert historical_meta["missing_doc_workflow"]["triage"]["scope"] == "historical_document"


def test_promote_nc_missing_doc_targets_search_scope(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_workflow as mod

    captured = {}

    def fake_run(settings, repository, **kwargs):
        captured.update(kwargs)
        return {
            "from_stage": kwargs["from_stage"],
            "to_stage": kwargs["to_stage"],
            "discovery_record_ids": kwargs.get("discovery_record_ids", []),
            "historical_document_ids": kwargs.get("historical_document_ids", []),
            "stages": {},
        }

    monkeypatch.setattr(mod, "run_nc_missing_doc_workflow", fake_run)

    report = mod.promote_nc_missing_doc_targets(
        settings=SimpleNamespace(),
        repository=FakeRepository(),
        scope="search_hits",
        family_key="fk5",
        discovery_record_ids=[51],
        limit=7,
        promotion_min_confidence=55.0,
    )

    assert report["from_stage"] == "fetch"
    assert report["to_stage"] == "queue_reprocess"
    assert captured["family_key"] == "fk5"
    assert captured["discovery_record_ids"] == [51]
    assert captured["persist_search"] is False
    assert captured["save_manifest"] is False
    assert captured["promotion_min_confidence"] == 55.0


def test_promote_nc_missing_doc_targets_import_scope(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_workflow as mod

    captured = {}

    def fake_run(settings, repository, **kwargs):
        captured.update(kwargs)
        return {
            "from_stage": kwargs["from_stage"],
            "to_stage": kwargs["to_stage"],
            "discovery_record_ids": kwargs.get("discovery_record_ids", []),
            "historical_document_ids": kwargs.get("historical_document_ids", []),
            "stages": {},
        }

    monkeypatch.setattr(mod, "run_nc_missing_doc_workflow", fake_run)

    report = mod.promote_nc_missing_doc_targets(
        settings=SimpleNamespace(),
        repository=FakeRepository(),
        scope="imported_docs",
        historical_document_ids=[901],
        import_promotion_min_family_score=31.0,
    )

    assert report["from_stage"] == "queue_reprocess"
    assert report["to_stage"] == "queue_reprocess"
    assert captured["historical_document_ids"] == [901]
    assert captured["import_promotion_min_family_score"] == 31.0
