"""Tests for ProgressSellerChargeProfile (leaf-590 / 656 / 662 fixed-fee patterns)."""

from __future__ import annotations

import pytest

from duke_rates.historical.ncuc.pipeline.parser_profiles import (
    HistoricalRateParserRegistry,
    ProgressSellerChargeProfile,
)


LEAF_590_TEXT = """
Duke Energy Progress, LLC NC Second Revised Leaf No. 590
PURCHASED POWER SCHEDULE PP

Seller Charge
An Eligible Qualifying Facility shall pay to Company a Seller Charge outlined below in
accordance with the Contract Capacity specified in the Purchase Power Agreement between
Company and Seller:
Monthly Seller Charge $23.06 for Eligible Qualifying Facilities with capacity greater than 15
kilowatts (AC).
$3.00 for Eligible Qualifying Facilities with capacity of 15 kilowatts (AC) or less.

Energy and Capacity Payments
""".strip()

LEAF_656_TEXT = """
NC Original Leaf No. 656
RIDER 68 DISPATCHED POWER
The Basic Customer Charge in the rate schedule: $$5.00.
B. Demands established during a Class 2 Dispatched Power Period will not be used.
""".strip()

LEAF_662_TEXT = """
NC Revised Leaf No. 662
RIDER EPPWP
The customer's REPS adjustment shall be assessed under their applicable revenue
classification:
Residential Classification - $1.17/month
Upon written request, only one REPS Adjustment shall apply.
""".strip()


@pytest.fixture
def profile():
    return ProgressSellerChargeProfile()


def test_supports_only_listed_families(profile):
    assert profile.supports({"family_key": "nc-progress-leaf-590"}, LEAF_590_TEXT) is True
    assert profile.supports({"family_key": "nc-progress-leaf-656"}, LEAF_656_TEXT) is True
    assert profile.supports({"family_key": "nc-progress-leaf-662"}, LEAF_662_TEXT) is True
    # Unrelated family
    assert profile.supports({"family_key": "nc-progress-leaf-600"}, LEAF_590_TEXT) is False
    # Right family but no matching pattern
    assert profile.supports({"family_key": "nc-progress-leaf-590"}, "no rates here") is False


def test_extracts_two_seller_charge_tiers_from_leaf_590(profile):
    charges = profile.extract({"family_key": "nc-progress-leaf-590"}, LEAF_590_TEXT)
    values = sorted(ch.rate_value for ch in charges)
    assert values == [3.0, 23.06]
    for ch in charges:
        assert ch.rate_unit == "$/month"
        assert ch.charge_type == "fixed"


def test_extracts_basic_customer_charge_from_leaf_656(profile):
    charges = profile.extract({"family_key": "nc-progress-leaf-656"}, LEAF_656_TEXT)
    assert len(charges) == 1
    ch = charges[0]
    assert ch.rate_value == 5.0
    assert ch.rate_unit == "$/month"
    assert "Basic Customer Charge" in (ch.charge_label or "")


def test_extracts_reps_adjustment_from_leaf_662(profile):
    charges = profile.extract({"family_key": "nc-progress-leaf-662"}, LEAF_662_TEXT)
    assert len(charges) == 1
    ch = charges[0]
    assert ch.rate_value == 1.17
    assert ch.rate_unit == "$/month"
    assert "Residential" in (ch.charge_label or "")


def test_registry_picks_seller_charge_over_single_value_rider():
    """The new profile must outrank progress_single_value_rider on its
    supported families so the registry routes leaf-590/656/662 through us."""
    registry = HistoricalRateParserRegistry()
    ranked = registry.rank_candidates(
        {"family_key": "nc-progress-leaf-590"}, LEAF_590_TEXT
    )
    top = ranked[0]
    assert top.name == "progress_seller_charge"
    assert top.score >= 0.95


def test_extract_returns_empty_when_supports_returns_false(profile):
    charges = profile.extract({"family_key": "nc-progress-leaf-590"}, "no rates here")
    assert charges == []
