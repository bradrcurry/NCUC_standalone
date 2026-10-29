"""Regression labels are attributed assistant source reviews, not human approvals."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import fitz
import pytest

from duke_rates.parse.redline_detector import evaluate_redline_page

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests/fixtures/d5_redline/manifest.json").read_text())


@pytest.mark.parametrize("case", MANIFEST["pages"], ids=lambda c: c["id"])
def test_original_archived_page_classification(case):
    fixture = ROOT / case["fixture"]
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == case["fixture_sha256"]
    with fitz.open(fixture) as doc:
        evidence = evaluate_redline_page(doc[0], case["original_page"])
    assert evidence.page == case["original_page"]
    assert (evidence.verdict == "redline") == case["observed_edit_markup"]
    assert (evidence.verdict == "no_text_layer") == case["native_assessment_insufficient"]
    assert evidence.signals()["human_verified"] is False
