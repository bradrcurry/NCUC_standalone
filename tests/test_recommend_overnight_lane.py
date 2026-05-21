"""Tests for the `recommend-overnight-lane-nc` CLI command's decision logic.

Patches `_build_workflow_status_nc_report` to feed synthetic state through the
command, so the decision rules are exercised directly without setting up a
full historical-document fixture.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from duke_rates.cli import app


runner = CliRunner()


def _status(**overrides) -> dict:
    """Base workflow-status snapshot. Override individual keys per test."""
    base = {
        "state": "NC",
        "historical_document_count": 928,
        "linked_version_count": 1390,
        "versions_with_charges_count": 738,
        "extraction_coverage_pct": 53.1,
        "parse_review_needs_review_count": 0,
        "parse_review_active_needs_review_count": 0,
        "parse_review_legacy_needs_review_count": 0,
        "reprocess_pending_count": 0,
        "reprocess_running_count": 0,
        "stale_historical_count": 0,
        "never_processed_historical_count": 0,
        "ocr_pending_count": 0,
        "ocr_running_count": 0,
        "provisional_family_count": 0,
        "null_effective_start_count": 0,
        "last_historical_run_at": "2026-05-16T19:00:00+00:00",
        "top_needs_review_profiles": [],
    }
    base.update(overrides)
    return base


def _invoke_json(**state_overrides) -> dict:
    with patch(
        "duke_rates.cli._build_workflow_status_nc_report",
        return_value=_status(**state_overrides),
    ):
        # We also need to short-circuit the DB connect since we're not touching it.
        with patch("duke_rates.cli.connect_sqlite"):
            result = runner.invoke(app, ["recommend-overnight-lane-nc", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_ocr_drain_wins_when_ocr_pending_is_large():
    report = _invoke_json(ocr_pending_count=50)
    assert report["chosen_lane"] == "ocr_drain"
    assert "scripts\\overnight\\tonight_9am.ps1" in report["recommended_command"]


def test_ocr_drain_wins_on_stale_docs():
    report = _invoke_json(stale_historical_count=12)
    assert report["chosen_lane"] == "ocr_drain"
    assert "stale" in report["chosen_reason"].lower()


def test_ocr_drain_wins_on_never_processed_docs():
    report = _invoke_json(never_processed_historical_count=8)
    assert report["chosen_lane"] == "ocr_drain"
    assert "never-processed" in report["chosen_reason"].lower()


def test_routing_first_wins_when_needs_review_large_and_queues_empty():
    report = _invoke_json(parse_review_active_needs_review_count=6022)
    assert report["chosen_lane"] == "routing_first"
    assert "routing_first_until_9am.ps1" in report["recommended_command"]


def test_extract_loop_wins_when_reprocess_queue_has_real_backlog():
    report = _invoke_json(
        reprocess_pending_count=200,
        parse_review_active_needs_review_count=6022,
    )
    # OCR is clean and reprocess has work — drain it before more routing.
    # Routing rule requires reprocess_pending==0, so it should NOT match here.
    assert report["chosen_lane"] == "extract_loop"
    assert any("reprocess" in r["reason"].lower() for r in report["all_matched_rules"])


def test_idle_when_everything_is_zero():
    report = _invoke_json()
    assert report["chosen_lane"] == "idle"
    assert report["recommended_command"] is None


def test_ocr_drain_takes_priority_over_routing_when_both_apply():
    report = _invoke_json(
        ocr_pending_count=50,
        parse_review_active_needs_review_count=6022,
    )
    assert report["chosen_lane"] == "ocr_drain"
    # Routing rule should NOT fire because it requires queues_empty (reprocess
    # 0 doesn't help when OCR is queued).
    lanes_matched = [r["lane"] for r in report["all_matched_rules"]]
    assert lanes_matched[0] == "ocr_drain"


def test_inputs_are_echoed_in_report():
    report = _invoke_json(
        ocr_pending_count=10,
        parse_review_active_needs_review_count=1500,
    )
    assert report["inputs"]["ocr_pending"] == 10
    assert report["inputs"]["active_needs_review"] == 1500
    assert report["inputs"]["coverage_pct"] == 53.1
