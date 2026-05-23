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
    filing_date: str | None = None
    metadata_json: str | None = None


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


class FakeRepository:
    def __init__(self):
        self.discovery_rows: list[FakeDiscovery] = []
        self.historical_rows: list[FakeHistoricalDoc] = []
        self.remediation_runs: list[dict] = []

    def list_ncuc_discovery_records(self, *, family_key=None, fetch_status=None):
        rows = list(self.discovery_rows)
        if family_key:
            rows = [row for row in rows if family_key in row.family_keys]
        return rows

    def list_historical_documents(self, *, state=None, company=None):
        return list(self.historical_rows)

    def record_missing_doc_remediation_run(self, **kwargs):
        self.remediation_runs.append(dict(kwargs))
        return len(self.remediation_runs)


def test_build_nc_missing_doc_deferred_report_groups_reasons(tmp_path: Path):
    from duke_rates.historical.ncuc.missing_doc_deferred_report import (
        build_nc_missing_doc_deferred_report,
    )

    repo = FakeRepository()
    repo.discovery_rows = [
        FakeDiscovery(
            id=7,
            family_keys=["fk1"],
            docket_number="E-2 Sub 976",
            filing_date="2010-11-15",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "search_promotion": {
                            "promotable": False,
                            "reasons": ["no_downloadable_url", "confidence_below_threshold:12.00"],
                            "search_confidence_score": 12.0,
                            "search_ideality": "possible",
                        }
                    }
                }
            ),
        ),
        FakeDiscovery(
            id=8,
            family_keys=["fk1"],
            docket_number="E-2 Sub 977",
            filing_date="2010-11-16",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "search_promotion": {
                            "promotable": True,
                            "reasons": [],
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
            effective_start=None,
            start_page=2,
            end_page=3,
            local_path=tmp_path / "doc.pdf",
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "import_promotion": {
                            "promotable": False,
                            "reasons": ["missing_effective_start_for_weak_match", "family_match_below_threshold:0.00"],
                            "family_match_score": 0.0,
                        }
                    }
                }
            ),
        )
    ]

    report = build_nc_missing_doc_deferred_report(repo, family_key="fk1")

    assert report["summary"]["deferred_discovery_count"] == 1
    assert report["summary"]["deferred_historical_count"] == 1
    assert report["summary"]["combined_reason_summary"][0]["reason"] in {
        "no_downloadable_url",
        "confidence_below_threshold:12.00",
        "missing_effective_start_for_weak_match",
        "family_match_below_threshold:0.00",
    }
    assert report["deferred_discovery_records"][0]["id"] == 7
    assert report["deferred_historical_documents"][0]["id"] == 21


def test_build_nc_missing_doc_remediation_plan_ranks_actionable_reasons(tmp_path: Path):
    from duke_rates.historical.ncuc.missing_doc_deferred_report import (
        build_nc_missing_doc_remediation_plan,
    )

    repo = FakeRepository()
    repo.discovery_rows = [
        FakeDiscovery(
            id=7,
            family_keys=["fk1"],
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "search_promotion": {
                            "promotable": False,
                            "reasons": ["no_downloadable_url", "confidence_below_threshold:12.00"],
                        }
                    }
                }
            ),
        ),
        FakeDiscovery(
            id=8,
            family_keys=["fk1"],
            metadata_json=json.dumps(
                {
                    "missing_doc_workflow": {
                        "search_promotion": {
                            "promotable": False,
                            "reasons": ["confidence_below_threshold:18.00"],
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
            effective_start=None,
            start_page=2,
            end_page=3,
            local_path=tmp_path / "doc.pdf",
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
    ]

    report = build_nc_missing_doc_remediation_plan(repo, family_key="fk1")

    assert report["ranked_steps"][0]["reason"] == "confidence_below_threshold"
    assert report["ranked_steps"][0]["count"] == 2
    assert report["ranked_steps"][0]["recommended_command"].endswith("--reason confidence_below_threshold --family-key fk1")
    reasons = {step["reason"] for step in report["ranked_steps"]}
    assert "no_downloadable_url" in reasons
    assert "missing_effective_start_for_weak_match" in reasons


def test_execute_top_nc_missing_doc_remediation_step_noop_when_no_ranked_steps(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_deferred_report as mod

    monkeypatch.setattr(
        mod,
        "build_nc_missing_doc_remediation_plan",
        lambda repository, **kwargs: {
            "family_key": kwargs.get("family_key"),
            "summary": {},
            "ranked_steps": [],
        },
    )

    repo = FakeRepository()
    report = mod.execute_top_nc_missing_doc_remediation_step(
        settings=object(),
        repository=repo,
        family_key="fk1",
    )

    assert report["executed"] is False
    assert report["selected_step"] is None
    assert repo.remediation_runs[0]["executed"] is False


def test_execute_top_nc_missing_doc_remediation_step_runs_selected_reason(monkeypatch):
    from duke_rates.historical.ncuc import missing_doc_deferred_report as mod

    plans = [
        {
            "family_key": "fk1",
            "summary": {"deferred_discovery_count": 3},
            "ranked_steps": [
                {
                    "reason": "no_downloadable_url",
                    "scope": "discovery",
                    "count": 3,
                    "weighted_score": 3.0,
                    "family_keys": ["fk1"],
                    "example_ids": [7],
                    "recommended_command": "python -m duke_rates workflow remediate-and-promote-nc-missing-docs --reason no_downloadable_url --family-key fk1",
                }
            ],
        },
        {
            "family_key": "fk1",
            "summary": {"deferred_discovery_count": 1},
            "ranked_steps": [],
        },
    ]
    captured = {}

    def fake_plan(repository, **kwargs):
        return plans.pop(0)

    monkeypatch.setattr(mod, "build_nc_missing_doc_remediation_plan", fake_plan)
    monkeypatch.setattr(
        mod,
        "remediate_and_promote_missing_doc_targets",
        lambda settings, repository, **kwargs: captured.setdefault("kwargs", kwargs) or {
            "family_key": kwargs.get("family_key"),
            "reasons": kwargs.get("reasons", []),
        },
    )

    repo = FakeRepository()
    report = mod.execute_top_nc_missing_doc_remediation_step(
        settings=object(),
        repository=repo,
        family_key="fk1",
        promotion_min_confidence=30.0,
    )

    assert report["executed"] is True
    assert report["selected_step"]["reason"] == "no_downloadable_url"
    assert captured["kwargs"]["reasons"] == ["no_downloadable_url"]
    assert captured["kwargs"]["promotion_min_confidence"] == 30.0
    assert report["after_plan"]["ranked_steps"] == []
    assert repo.remediation_runs[0]["selected_reason"] == "no_downloadable_url"
    assert repo.remediation_runs[0]["before_step_count"] == 1
    assert repo.remediation_runs[0]["after_step_count"] == 0
