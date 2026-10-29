"""Preserve metadata selection while avoiding unused work on large filing tails."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from duke_rates.historical.metadata import REVISION_LINE_RE, SUPERSEDES_LINE_RE, _first_unique_match


@pytest.mark.parametrize("pattern", [REVISION_LINE_RE, SUPERSEDES_LINE_RE])
@pytest.mark.parametrize(
    "text",
    [
        "",
        "No revision here",
        "NC Original Leaf No. 500\nNC First Revised Leaf No. 500\n",
        "NC Original Leaf No. 500\nNC Original Leaf No. 500\nNC Second Revised Leaf No. 500",
        "\tSC First Revised Leaf No. 2\nSuperseding SC Original Leaf No. 2\n",
        "nc first revised Leaf No. 600\nSuperseding NC Original Leaf No. 600\n",
        "NC\nFirst Revised Leaf No. 600\nSuperseding\nNC Original Leaf No. 600\n",
    ],
)
def test_first_value_matches_previous_unique_collection(pattern, text):
    # Independent reference to the previous collect/deduplicate/select contract.
    values = list(dict.fromkeys(" ".join(m.group(1).split()) for m in pattern.finditer(text)))
    assert _first_unique_match(pattern, text) == (values[0] if values else None)


def test_metadata_does_not_scan_unneeded_large_whitespace_tail():
    # A child deadline keeps a future regex regression from hanging the suite.
    # Both headers precede a pathological tail; no source clipping is allowed.
    code = """
import json
from duke_rates.historical.metadata import extract_historical_metadata
text = ('NC First Revised Leaf No. 600\\n'
        'Superseding NC Original Leaf No. 600\\n'
        'Effective for service rendered on and after October 1, 2025\\n'
        + '\\n' * 100000)
print(json.dumps(extract_historical_metadata(text)))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10, check=True
    )
    metadata = json.loads(result.stdout)
    assert metadata["revision_label"] == "NC First Revised Leaf No. 600"
    assert metadata["supersedes_label"] == "NC Original Leaf No. 600"
    assert metadata["effective_start"] == "October 1, 2025"
