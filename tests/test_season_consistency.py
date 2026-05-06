"""Tests for the consolidated season-matching utilities (TD-003 / TD-008).

Verifies that:
1. The shared ``season_matches()`` function handles all known Duke NC season
   label variants — with and without spaces, with en-dash, upper/lower case.
2. Both ``BillingEngine.estimate()`` (engine path) and ``calculate_bill()``
   (ncuc_loader path) agree on every season label exercised here.
3. An unknown season label produces a WARNING log entry.
4. ``month=0`` sentinel always returns True (no billing date available).
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from duke_rates.billing.season_utils import SEASON_MONTHS, _normalize_season_label, season_matches


# ---------------------------------------------------------------------------
# 1. Normalization
# ---------------------------------------------------------------------------

class TestNormalizeSeason:
    def test_strips_spaces_around_dash(self):
        assert _normalize_season_label("May - September") == "may-september"

    def test_strips_no_spaces(self):
        assert _normalize_season_label("May-September") == "may-september"

    def test_strips_en_dash(self):
        assert _normalize_season_label("May\u2013September") == "may-september"

    def test_strips_en_dash_with_spaces(self):
        assert _normalize_season_label("May \u2013 September") == "may-september"

    def test_uppercase(self):
        assert _normalize_season_label("OCTOBER - APRIL") == "october-april"

    def test_strips_outer_whitespace(self):
        assert _normalize_season_label("  july - october  ") == "july-october"


# ---------------------------------------------------------------------------
# 2. season_matches — known Duke NC labels in all surface forms
# ---------------------------------------------------------------------------

# (raw_label, month_should_match, month_should_not_match)
DUKE_NC_CASES = [
    # Summer billing period: May–September
    ("May - September",  5,  4),
    ("May-September",    9, 10),
    ("May\u2013September", 7, 1),
    # June–September (some schedules)
    ("June - September", 6,  5),
    ("june-september",   9, 10),
    # October–April (off-peak/winter)
    ("October - April",  10, 5),
    ("October-April",     1, 6),
    ("October \u2013 April", 4, 5),
    # October–May
    ("October - May",     5,  6),
    # Pre-2023 DEP RES two-column "Bills Rendered During"
    ("July - October",    7,  6),
    ("November - June",  11, 10),
    # Generic labels
    ("summer",            7,  1),
    ("winter",           12,  6),
]

@pytest.mark.parametrize("label,match_month,no_match_month", DUKE_NC_CASES)
def test_known_labels_match(label, match_month, no_match_month):
    assert season_matches(label, match_month) is True, \
        f"Expected {label!r} to match month {match_month}"
    assert season_matches(label, no_match_month) is False, \
        f"Expected {label!r} NOT to match month {no_match_month}"


# ---------------------------------------------------------------------------
# 3. None / empty label → always True
# ---------------------------------------------------------------------------

def test_none_label_is_year_round():
    for m in range(1, 13):
        assert season_matches(None, m) is True

def test_empty_label_is_year_round():
    assert season_matches("", 6) is True

def test_month_zero_sentinel_is_year_round():
    assert season_matches("May - September", 0) is True
    assert season_matches("October - April", 0) is True


# ---------------------------------------------------------------------------
# 4. Unknown season label → True (year-round fallback) + WARNING log
# ---------------------------------------------------------------------------

def test_unknown_label_returns_true_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="duke_rates.billing.season_utils"):
        result = season_matches("summer / fall", 3)

    assert result is True
    assert any("Unknown season label" in r.message for r in caplog.records), \
        "Expected a WARNING log for unknown season label"
    # The actual label should appear in the warning
    assert any("summer / fall" in r.message for r in caplog.records)


def test_unknown_label_includes_normalized_form_in_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="duke_rates.billing.season_utils"):
        season_matches("Spring \u2013 Fall", 4)
    assert any("spring-fall" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 5. Both billing paths agree on the same label
#
# This is the key TD-003 acceptance criterion: BillingEngine.estimate()
# and calculate_bill() must produce the same season-filtering result.
#
# We test this by:
#   a) running the engine path (BillingEngine.estimate with a service_date)
#   b) running the ncuc_loader path (_filter_seasonal_charges directly)
#   c) verifying both pick the same charge for each month
# ---------------------------------------------------------------------------

from datetime import date

from duke_rates.billing.engine import BillingEngine
from duke_rates.billing.calculators import UsageInput
from duke_rates.db.ncuc_loader import _filter_seasonal_charges
from duke_rates.models.rate_schedule import EnergyCharge, FixedCharge, RateScheduleData


def _make_seasonal_schedule(summer_rate: float, winter_rate: float) -> RateScheduleData:
    """Build a minimal two-season schedule for engine path tests."""
    return RateScheduleData(
        tariff_id="test_seasonal",
        state="NC",
        company="progress",
        schedule_title="Test Seasonal",
        fixed_charges=[FixedCharge(label="BFC", amount=0.0)],
        energy_charges=[
            EnergyCharge(label="Summer", rate=summer_rate, season="May - September"),
            EnergyCharge(label="Winter", rate=winter_rate, season="October - April"),
        ],
    )


def _ncuc_charges(summer_rate: float, winter_rate: float) -> list[dict]:
    """Equivalent charge list for ncuc_loader path."""
    return [
        {"label": "Summer", "rate": summer_rate, "unit": "$/kWh", "season": "May - September"},
        {"label": "Winter", "rate": winter_rate, "unit": "$/kWh", "season": "October - April"},
    ]


@pytest.mark.parametrize("month,expected_season", [
    (1,  "Winter"),   # January
    (4,  "Winter"),   # April
    (5,  "Summer"),   # May
    (9,  "Summer"),   # September
    (10, "Winter"),   # October
    (12, "Winter"),   # December
])
def test_season_consistency_both_paths(month, expected_season):
    """Both billing paths pick the same season for each month."""
    summer_rate, winter_rate = 0.12, 0.10

    # Engine path
    schedule = _make_seasonal_schedule(summer_rate, winter_rate)
    svc_date = date(2024, month, 15)
    estimate = BillingEngine().estimate(schedule, UsageInput(monthly_kwh=1000, service_date=svc_date))
    # Find the energy line item (skip fixed charge BFC which has amount=0.0 and no "@" in details)
    energy_items = [item for item in estimate.line_items if item.details and "@" in item.details]
    assert len(energy_items) == 1, f"Month {month}: expected 1 energy line item, got {len(energy_items)}"
    # Extract rate from details string like "1000.0 kWh @ 0.12"
    engine_rate = float(energy_items[0].details.split("@")[1].strip())

    # ncuc_loader path
    charges = _ncuc_charges(summer_rate, winter_rate)
    filtered = _filter_seasonal_charges(charges, month)
    assert len(filtered) == 1, f"Month {month}: expected 1 charge after filter, got {len(filtered)}"
    ncuc_rate = filtered[0]["rate"]

    assert engine_rate == ncuc_rate, (
        f"Month {month}: engine used rate {engine_rate} but ncuc_loader used {ncuc_rate}. "
        f"Both should agree on the {expected_season} charge."
    )


def test_season_consistency_en_dash_variant():
    """Both paths agree when the season label contains an en-dash."""
    summer_rate, winter_rate = 0.13, 0.11

    # Engine path with en-dash label
    schedule = RateScheduleData(
        tariff_id="test_endash",
        state="NC",
        company="progress",
        schedule_title="Test",
        fixed_charges=[],
        energy_charges=[
            EnergyCharge(label="Summer", rate=summer_rate, season="May\u2013September"),
            EnergyCharge(label="Winter", rate=winter_rate, season="October\u2013April"),
        ],
    )
    estimate = BillingEngine().estimate(
        schedule, UsageInput(monthly_kwh=500, service_date=date(2024, 7, 15))
    )
    energy_items = [item for item in estimate.line_items if item.details and "@" in item.details]
    assert len(energy_items) == 1
    engine_rate = float(energy_items[0].details.split("@")[1].strip())

    # ncuc_loader path with en-dash label
    charges = [
        {"label": "Summer", "rate": summer_rate, "unit": "$/kWh", "season": "May\u2013September"},
        {"label": "Winter", "rate": winter_rate, "unit": "$/kWh", "season": "October\u2013April"},
    ]
    filtered = _filter_seasonal_charges(charges, 7)
    assert len(filtered) == 1
    ncuc_rate = filtered[0]["rate"]

    assert engine_rate == ncuc_rate == summer_rate
