"""Tests for CarolinasPurchasedPowerScheduleProfile (DEC SCHEDULE PP).

DEC equivalent of Progress leaf-590. Same two-tier capacity-based structure
but uses "Monthly Administrative Charge" terminology instead of "Monthly
Seller Charge", plus a DEC-specific "$25 minimum monthly Interconnection
Facilities Charge".

Confirmed production candidate (2026-05-16 audit):
  - nc-carolinas-schedule-pp (hd_id=480, eff 2021-10-11)
"""

from __future__ import annotations

import pytest

from duke_rates.historical.ncuc.pipeline.ocr_normalization import (
    normalize_docling_markdown,
)
from duke_rates.historical.ncuc.pipeline.parser_profiles import (
    CarolinasPurchasedPowerScheduleProfile,
    HistoricalRateParserRegistry,
)


# Realistic excerpt from hd_id=480 (DEC PP, eff. 2021-10-11). Includes both
# the Monthly Administrative Charge tiers AND the Interconnection Facilities
# Charge minimum. Matches the post-flatten production text shape.
DEC_PP_TEXT = """\
Duke Energy Carolinas, LLC Electricity No. 4
PURCHASED POWER SCHEDULE PP

AVAILABILITY
Upon Seller's completion and Company's acceptance of a Purchase Power
Agreement, this Schedule is available for electrical energy and capacity
supplied by Eligible Qualifying Facilities to Company.

Monthly Administrative Charge: $19.91 for Eligible Qualifying Facilities
with capacity greater than 15 kilowatts (AC).
$3.00 for Eligible Qualifying Facilities with capacity of 15 kilowatts (AC)
or less.

INTERCONNECTION
If the Seller does not require interconnection facilities for the purchase
of electric power, the $25 minimum monthly Interconnection Facilities Charge
shall not be applicable.
"""


@pytest.fixture
def profile():
    return CarolinasPurchasedPowerScheduleProfile()


def test_supports_only_schedule_pp(profile):
    assert profile.supports({"family_key": "nc-carolinas-schedule-pp"}, DEC_PP_TEXT) is True
    # Wrong family
    assert profile.supports({"family_key": "nc-progress-leaf-590"}, DEC_PP_TEXT) is False
    # Right family but no rate markers
    assert profile.supports({"family_key": "nc-carolinas-schedule-pp"}, "no rates here") is False


def test_extracts_two_admin_charge_tiers_and_interconnection_minimum(profile):
    charges = profile.extract({"family_key": "nc-carolinas-schedule-pp"}, DEC_PP_TEXT)
    values = sorted(ch.rate_value for ch in charges)
    assert values == [3.00, 19.91, 25.00]
    for ch in charges:
        assert ch.rate_unit == "$/month"
        assert ch.charge_type == "fixed"
    labels = " ".join(ch.charge_label or "" for ch in charges)
    assert "Monthly Administrative Charge" in labels
    assert "Interconnection Facilities Charge" in labels


def test_registry_picks_profile_for_schedule_pp_family():
    registry = HistoricalRateParserRegistry()
    ranked = registry.rank_candidates(
        {"family_key": "nc-carolinas-schedule-pp"}, DEC_PP_TEXT,
    )
    top = ranked[0]
    assert top.name == "carolinas_purchased_power_schedule"
    assert top.score >= 0.95


def test_handles_post_flatten_text():
    """Production path runs `normalize_docling_markdown` BEFORE profiles see
    text. Mirrors the regression lesson from `nantahala_fl` work.
    """
    profile = CarolinasPurchasedPowerScheduleProfile()
    flattened = normalize_docling_markdown(DEC_PP_TEXT)
    charges = profile.extract(
        {"family_key": "nc-carolinas-schedule-pp"}, flattened,
    )
    values = sorted(ch.rate_value for ch in charges)
    assert values == [3.00, 19.91, 25.00]


def test_extract_returns_empty_when_supports_false(profile):
    charges = profile.extract({"family_key": "nc-carolinas-schedule-pp"}, "no rates here")
    assert charges == []


def test_extracts_only_admin_when_no_interconnection(profile):
    text = """\
Monthly Administrative Charge: $19.91 for Eligible Qualifying Facilities with capacity greater than 15 kilowatts (AC).
$3.00 for Eligible Qualifying Facilities with capacity of 15 kilowatts (AC) or less.
"""
    charges = profile.extract({"family_key": "nc-carolinas-schedule-pp"}, text)
    values = sorted(ch.rate_value for ch in charges)
    assert values == [3.00, 19.91]


def test_extracts_only_interconnection_when_no_admin(profile):
    text = "$25 minimum monthly Interconnection Facilities Charge applies."
    charges = profile.extract({"family_key": "nc-carolinas-schedule-pp"}, text)
    assert len(charges) == 1
    assert charges[0].rate_value == 25.0
    assert "Interconnection" in (charges[0].charge_label or "")
