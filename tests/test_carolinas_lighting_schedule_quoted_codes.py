"""Regression test for `schedule "FL"` (quoted code) routing.

The 2026-05-16 audit found that `nc-carolinas-schedule-FL` docs with
text like `SCHEDULE "FL"` (with quotes around the code) failed the
`carolinas_lighting_schedule.supports()` gate because the gate was
substring-matching `"schedule fl"` against lowered text — the quotes
broke the substring match.

The fix uses a small regex (`schedule\\s*"?<code>"?`) in both
`CarolinasLightingScheduleProfile.supports()` and the registry's
`_score_profile` branch. Both call sites must stay in sync.
"""

from __future__ import annotations

import pytest

from duke_rates.historical.ncuc.pipeline.parser_profiles import (
    CarolinasLightingScheduleProfile,
    HistoricalRateParserRegistry,
)


FL_DOC_TEXT = """\
NANTAHALA POWER AND LIGHT
SCHEDULE "FL"
FLOODLIGHTING SERVICE
APPLICABILITY
This Schedule is applicable to unmetered service supplied for the
floodlighting of areas by luminaires of the type designated below.
"""

OL_DOC_TEXT = """\
SCHEDULE "OL"
OUTDOOR LIGHTING SERVICE
"""

YL_DOC_TEXT = """\
SCHEDULE "YL"
YARD LIGHTING SERVICE
"""

# Unquoted forms (modern DEC tariffs) should still match.
MODERN_FL_TEXT = """\
SCHEDULE FL
FLOODLIGHTING SERVICE
Standard rates apply.
"""


@pytest.fixture
def profile():
    return CarolinasLightingScheduleProfile()


def test_supports_matches_quoted_fl_code(profile):
    assert profile.supports({"family_key": "nc-carolinas-schedule-fl"}, FL_DOC_TEXT) is True


def test_supports_matches_quoted_ol_code(profile):
    assert profile.supports({"family_key": "nc-carolinas-schedule-ol"}, OL_DOC_TEXT) is True


def test_supports_matches_quoted_yl_code(profile):
    assert profile.supports({"family_key": "nc-carolinas-schedule-yl"}, YL_DOC_TEXT) is True


def test_supports_still_matches_unquoted_fl_code(profile):
    """Modern (unquoted) form must keep working — no regression on existing matches."""
    assert profile.supports({"family_key": "nc-carolinas-schedule-fl"}, MODERN_FL_TEXT) is True


def test_registry_score_branch_uses_same_regex():
    """The `_score_profile` branch in the registry has its own duplicated gate
    check. This test catches drift between the two locations."""
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-carolinas-schedule-fl"}
    ranked = registry.rank_candidates(doc, FL_DOC_TEXT)
    top = ranked[0]
    assert top.name == "carolinas_lighting_schedule"
    assert top.score >= 0.93


def test_supports_rejects_unrelated_family(profile):
    # Quoted-code regex must not accidentally match unrelated families
    assert profile.supports({"family_key": "nc-progress-leaf-500"}, FL_DOC_TEXT) is False
