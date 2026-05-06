from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FakeDiscovery:
    id: int
    family_keys: list[str] = field(default_factory=list)
    docket_number: str | None = None
    filing_title: str | None = None
    fetch_status: str | None = None
    metadata_json: str | None = None


@dataclass
class FakeHistoricalDoc:
    id: int
    family_key: str
    title: str
    effective_start: str | None
    local_path: Path
    metadata_json: str | None = None


class FakeRepository:
    def __init__(self):
        self.discovery_rows: list[FakeDiscovery] = []
        self.historical_rows: list[FakeHistoricalDoc] = []

    def list_ncuc_discovery_records(self, *, family_key=None, fetch_status=None):
        rows = list(self.discovery_rows)
        if family_key:
            rows = [row for row in rows if family_key in row.family_keys]
        return rows

    def list_historical_documents(self, *, state=None, company=None):
        return list(self.historical_rows)


def test_build_nc_missing_doc_triage_report_groups_actions(tmp_path: Path):
    from duke_rates.historical.ncuc.missing_doc_triage_report import (
        build_nc_missing_doc_triage_report,
    )

    repo = FakeRepository()
    repo.discovery_rows = [
        FakeDiscovery(
            id=7,
            family_keys=["fk1"],
            docket_number="E-2 Sub 976",
            filing_title="Revised Rate Tariffs",
            fetch_status="success",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "discovery_record",
                            "family_key": "fk1",
                            "next_action": "import_and_mine_document",
                            "blocked_reason": None,
                            "updated_at": "2026-04-21T12:00:00+00:00",
                            "linked_historical_document_ids": [],
                        }
                    }
                }
            ),
        ),
        FakeDiscovery(
            id=8,
            family_keys=["fk1"],
            docket_number="E-2 Sub 977",
            filing_title="Weak clue",
            fetch_status="failed",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "discovery_record",
                            "family_key": "fk1",
                            "next_action": "retry_fetch_or_manual_portal_review",
                            "blocked_reason": "fetch_failed",
                            "updated_at": "2026-04-21T11:00:00+00:00",
                        }
                    }
                }
            ),
        ),
    ]
    repo.historical_rows = [
        FakeHistoricalDoc(
            id=21,
            family_key="fk1",
            title="Schedule RES",
            effective_start="2010-12-01",
            local_path=tmp_path / "doc.pdf",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "historical_document",
                            "family_key": "fk1",
                            "next_action": "review_parse_output",
                            "blocked_reason": "needs_review",
                            "current_stage": "needs_review",
                            "updated_at": "2026-04-21T10:00:00+00:00",
                        }
                    }
                }
            ),
        )
    ]

    report = build_nc_missing_doc_triage_report(repo, family_key="fk1")

    assert report["summary"]["discovery_triage_count"] == 2
    assert report["summary"]["historical_triage_count"] == 1
    assert report["summary"]["combined_triage_count"] == 3
    assert report["summary"]["next_action_summary"][0]["count"] == 1
    reasons = {row["blocked_reason"] for row in report["summary"]["blocked_reason_summary"]}
    assert "fetch_failed" in reasons
    assert "needs_review" in reasons
    assert report["combined_targets"][0]["target_type"] in {"discovery_record", "historical_document"}


def test_build_nc_missing_doc_triage_report_filters_family_key(tmp_path: Path):
    from duke_rates.historical.ncuc.missing_doc_triage_report import (
        build_nc_missing_doc_triage_report,
    )

    repo = FakeRepository()
    repo.discovery_rows = [
        FakeDiscovery(
            id=7,
            family_keys=["fk1"],
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "discovery_record",
                            "family_key": "fk1",
                            "next_action": "import_and_mine_document",
                            "updated_at": "2026-04-21T12:00:00+00:00",
                        }
                    }
                }
            ),
        ),
        FakeDiscovery(
            id=9,
            family_keys=["fk2"],
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "discovery_record",
                            "family_key": "fk2",
                            "next_action": "fetch_document",
                            "updated_at": "2026-04-21T12:00:00+00:00",
                        }
                    }
                }
            ),
        ),
    ]
    repo.historical_rows = [
        FakeHistoricalDoc(
            id=21,
            family_key="fk2",
            title="Other Schedule",
            effective_start="2011-01-01",
            local_path=tmp_path / "other.pdf",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "historical_document",
                            "family_key": "fk2",
                            "next_action": "ready_for_acceptance",
                            "updated_at": "2026-04-21T12:00:00+00:00",
                        }
                    }
                }
            ),
        )
    ]

    report = build_nc_missing_doc_triage_report(repo, family_key="fk1")

    assert report["summary"]["combined_triage_count"] == 1
    assert report["discovery_records"][0]["id"] == 7
    assert report["historical_documents"] == []


def test_build_nc_missing_doc_triage_report_ranks_actionable_targets(tmp_path: Path):
    from duke_rates.historical.ncuc.missing_doc_triage_report import (
        build_nc_missing_doc_triage_report,
    )

    repo = FakeRepository()
    repo.discovery_rows = [
        FakeDiscovery(
            id=7,
            family_keys=["fk1"],
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "discovery_record",
                            "family_key": "fk1",
                            "next_action": "fetch_document",
                            "blocked_reason": None,
                            "updated_at": "2026-04-21T12:00:00+00:00",
                        }
                    }
                }
            ),
        )
    ]
    repo.historical_rows = [
        FakeHistoricalDoc(
            id=21,
            family_key="fk1",
            title="Schedule RES",
            effective_start="2010-12-01",
            local_path=tmp_path / "doc.pdf",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "historical_document",
                            "family_key": "fk1",
                            "next_action": "review_parse_output",
                            "blocked_reason": "needs_review",
                            "current_stage": "needs_review",
                            "latest_outcome_quality": "weak",
                            "updated_at": "2026-04-21T11:00:00+00:00",
                        }
                    }
                }
            ),
        ),
        FakeHistoricalDoc(
            id=22,
            family_key="fk1",
            title="Schedule RES Accepted",
            effective_start="2011-01-01",
            local_path=tmp_path / "doc2.pdf",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "historical_document",
                            "family_key": "fk1",
                            "next_action": "ready_for_acceptance",
                            "blocked_reason": None,
                            "updated_at": "2026-04-21T10:00:00+00:00",
                        }
                    }
                }
            ),
        ),
    ]

    report = build_nc_missing_doc_triage_report(
        repo,
        family_key="fk1",
        actionable_only=True,
        top=2,
    )

    assert report["summary"]["ranked_target_count"] == 2
    assert [row["id"] for row in report["ranked_targets"]] == [21, 7]
    assert all(row["next_action"] != "ready_for_acceptance" for row in report["ranked_targets"])
    assert report["ranked_targets"][0]["priority_score"] > report["ranked_targets"][1]["priority_score"]
    assert report["ranked_targets"][0]["suggested_command"] == (
        "python -m duke_rates show-nc-missing-doc-status --historical-document-id 21 --family-key fk1"
    )
    assert report["ranked_targets"][1]["suggested_command"] == (
        "python -m duke_rates run-nc-missing-doc-workflow --from-stage fetch --to-stage fetch --record-id 7 --family-key fk1"
    )


def test_build_nc_missing_doc_triage_report_suggests_reprocess_retry_command(tmp_path: Path):
    from duke_rates.historical.ncuc.missing_doc_triage_report import (
        build_nc_missing_doc_triage_report,
    )

    repo = FakeRepository()
    repo.historical_rows = [
        FakeHistoricalDoc(
            id=31,
            family_key="fk2",
            title="Schedule Retry",
            effective_start="2012-01-01",
            local_path=tmp_path / "retry.pdf",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "historical_document",
                            "family_key": "fk2",
                            "next_action": "retry_with_better_parser_context",
                            "blocked_reason": "processed_empty",
                            "current_stage": "processed:empty",
                            "latest_outcome_quality": "empty",
                            "updated_at": "2026-04-21T12:00:00+00:00",
                        }
                    }
                }
            ),
        )
    ]

    report = build_nc_missing_doc_triage_report(repo, family_key="fk2", actionable_only=True)

    assert report["ranked_targets"][0]["suggested_command"] == (
        "python -m duke_rates run-nc-missing-doc-workflow --from-stage queue_reprocess --to-stage process_reprocess --historical-document-id 31 --family-key fk2"
    )


def test_execute_top_nc_missing_doc_triage_action_runs_workflow(monkeypatch, tmp_path: Path):
    from duke_rates.historical.ncuc import missing_doc_triage_report as mod

    repo = FakeRepository()
    repo.discovery_rows = [
        FakeDiscovery(
            id=44,
            family_keys=["fk3"],
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "discovery_record",
                            "family_key": "fk3",
                            "next_action": "import_and_mine_document",
                            "updated_at": "2026-04-21T12:00:00+00:00",
                        }
                    }
                }
            ),
        )
    ]

    captured: dict[str, object] = {}

    def fake_run(settings, repository, **kwargs):
        captured.update(kwargs)
        return {"stages": {"import": {"imported_count": 1}}}

    monkeypatch.setattr(
        "duke_rates.historical.ncuc.missing_doc_workflow.run_nc_missing_doc_workflow",
        fake_run,
    )

    report = mod.execute_top_nc_missing_doc_triage_action(
        settings=object(),
        repository=repo,
        family_key="fk3",
        requested_by="triage-test",
    )

    assert report["executed"] is True
    assert report["selected_target"]["id"] == 44
    assert captured["from_stage"] == "import"
    assert captured["to_stage"] == "import"
    assert captured["discovery_record_ids"] == [44]
    assert captured["requested_by"] == "triage-test"
    assert report["execution_report"]["workflow_report"]["stages"]["import"]["imported_count"] == 1


def test_execute_top_nc_missing_doc_triage_action_runs_status_path(monkeypatch, tmp_path: Path):
    from duke_rates.historical.ncuc import missing_doc_triage_report as mod

    repo = FakeRepository()
    repo.historical_rows = [
        FakeHistoricalDoc(
            id=55,
            family_key="fk4",
            title="Schedule Review",
            effective_start="2013-01-01",
            local_path=tmp_path / "review.pdf",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "triage": {
                            "scope": "historical_document",
                            "family_key": "fk4",
                            "next_action": "review_parse_output",
                            "blocked_reason": "needs_review",
                            "updated_at": "2026-04-21T12:00:00+00:00",
                        }
                    }
                }
            ),
        )
    ]

    monkeypatch.setattr(
        "duke_rates.historical.ncuc.missing_doc_status.build_nc_missing_doc_status_report",
        lambda repository, **kwargs: {"target": kwargs, "summary": {"needs_review_count": 1}},
    )

    report = mod.execute_top_nc_missing_doc_triage_action(
        settings=object(),
        repository=repo,
        family_key="fk4",
    )

    assert report["executed"] is True
    assert report["selected_target"]["id"] == 55
    assert report["execution_report"]["action"] == "review_parse_output"
    assert report["execution_report"]["status_report"]["target"]["historical_document_id"] == 55


def test_execute_batch_nc_missing_doc_triage_actions_progresses_and_stops(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_triage_report as mod

    reports = iter([
        {"summary": {"ranked_target_count": 2}, "ranked_targets": [{"target_type": "discovery_record", "id": 1}]},
        {"summary": {"ranked_target_count": 2}, "ranked_targets": [{"target_type": "discovery_record", "id": 1}]},
        {"summary": {"ranked_target_count": 1}, "ranked_targets": [{"target_type": "historical_document", "id": 2}]},
        {"summary": {"ranked_target_count": 0}, "ranked_targets": []},
        {"summary": {"ranked_target_count": 0}, "ranked_targets": []},
        {"summary": {"ranked_target_count": 0}, "ranked_targets": []},
    ])
    executed = iter([
        {
            "executed": True,
            "selected_target": {"target_type": "discovery_record", "id": 1},
            "before_report": {"summary": {"ranked_target_count": 2}},
            "after_report": {"summary": {"ranked_target_count": 1}},
            "execution_report": {},
        },
        {
            "executed": True,
            "selected_target": {"target_type": "historical_document", "id": 2},
            "before_report": {"summary": {"ranked_target_count": 1}},
            "after_report": {"summary": {"ranked_target_count": 0}},
            "execution_report": {},
        },
    ])

    monkeypatch.setattr(
        mod,
        "build_nc_missing_doc_triage_report",
        lambda repository, **kwargs: next(reports),
    )
    monkeypatch.setattr(
        mod,
        "execute_top_nc_missing_doc_triage_action",
        lambda settings, repository, **kwargs: next(executed),
    )

    report = mod.execute_batch_nc_missing_doc_triage_actions(
        settings=object(),
        repository=object(),
        max_actions=3,
    )

    assert report["executed_count"] == 2
    assert report["stop_reason"] == "no_actionable_targets"
    assert len(report["steps"]) == 2


def test_execute_batch_nc_missing_doc_triage_actions_stops_on_repeat(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_triage_report as mod

    reports = [
        {"summary": {"ranked_target_count": 1}, "ranked_targets": [{"target_type": "discovery_record", "id": 9}]},
        {"summary": {"ranked_target_count": 1}, "ranked_targets": [{"target_type": "discovery_record", "id": 9}]},
        {"summary": {"ranked_target_count": 1}, "ranked_targets": [{"target_type": "discovery_record", "id": 9}]},
        {"summary": {"ranked_target_count": 1}, "ranked_targets": [{"target_type": "discovery_record", "id": 9}]},
    ]

    monkeypatch.setattr(
        mod,
        "build_nc_missing_doc_triage_report",
        lambda repository, **kwargs: reports.pop(0),
    )
    monkeypatch.setattr(
        mod,
        "execute_top_nc_missing_doc_triage_action",
        lambda settings, repository, **kwargs: {
            "executed": True,
            "selected_target": {"target_type": "discovery_record", "id": 9},
            "before_report": {"summary": {"ranked_target_count": 1}},
            "after_report": {"summary": {"ranked_target_count": 1}},
            "execution_report": {},
        },
    )

    report = mod.execute_batch_nc_missing_doc_triage_actions(
        settings=object(),
        repository=object(),
        max_actions=3,
    )

    assert report["executed_count"] == 1
    assert report["stop_reason"] == "no_progress_after_step"
