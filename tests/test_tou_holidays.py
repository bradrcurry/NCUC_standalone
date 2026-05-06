"""Tests for Duke Energy NC holiday calendar and TOU holiday treatment.

Verifies:
1. ``duke_nc_holidays()`` returns correct observed dates for each holiday,
   including the Saturday→Friday and Sunday→Monday shift rules.
2. ``is_duke_holiday()`` returns True/False correctly.
3. The TOU billing engine classifies interval points that fall on a Duke
   holiday as off-peak (same treatment as weekend/Sunday), even when the
   holiday is a weekday.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from duke_rates.billing.holidays import duke_nc_holidays, is_duke_holiday
from duke_rates.billing.tou import classify_interval_point
from duke_rates.models.rate_schedule import TOUPeriod


# ---------------------------------------------------------------------------
# 1. Holiday calendar — known observed dates
# ---------------------------------------------------------------------------

class TestDukeHolidayCalendar:
    # 2024 calendar spot-checks
    # New Year's 2024: Jan 1 = Monday → no shift
    def test_new_years_2024(self):
        assert date(2024, 1, 1) in duke_nc_holidays(2024)

    # Memorial Day 2024: last Monday in May → May 27
    def test_memorial_day_2024(self):
        assert date(2024, 5, 27) in duke_nc_holidays(2024)

    # Independence Day 2024: Jul 4 = Thursday → no shift
    def test_independence_day_2024(self):
        assert date(2024, 7, 4) in duke_nc_holidays(2024)

    # Labor Day 2024: first Monday in September → Sep 2
    def test_labor_day_2024(self):
        assert date(2024, 9, 2) in duke_nc_holidays(2024)

    # Thanksgiving 2024: 4th Thursday in November → Nov 28
    def test_thanksgiving_2024(self):
        assert date(2024, 11, 28) in duke_nc_holidays(2024)

    # Christmas 2024: Dec 25 = Wednesday → no shift
    def test_christmas_2024(self):
        assert date(2024, 12, 25) in duke_nc_holidays(2024)

    # Exactly 6 holidays per year
    def test_six_holidays_per_year(self):
        assert len(duke_nc_holidays(2024)) == 6
        assert len(duke_nc_holidays(2025)) == 6
        assert len(duke_nc_holidays(2026)) == 6

    # Saturday shift: Independence Day 2020 falls on Saturday → observed Friday Jul 3
    def test_independence_day_2020_saturday_shift(self):
        assert date(2020, 7, 4).weekday() == 5  # Saturday
        assert date(2020, 7, 3) in duke_nc_holidays(2020)
        assert date(2020, 7, 4) not in duke_nc_holidays(2020)

    # Sunday shift: New Year's Day 2023 falls on Sunday → observed Monday Jan 2
    def test_new_years_2023_sunday_shift(self):
        assert date(2023, 1, 1).weekday() == 6  # Sunday
        assert date(2023, 1, 2) in duke_nc_holidays(2023)
        assert date(2023, 1, 1) not in duke_nc_holidays(2023)

    # Christmas 2022 falls on Sunday → observed Monday Dec 26
    def test_christmas_2022_sunday_shift(self):
        assert date(2022, 12, 25).weekday() == 6  # Sunday
        assert date(2022, 12, 26) in duke_nc_holidays(2022)
        assert date(2022, 12, 25) not in duke_nc_holidays(2022)

    # A non-holiday weekday should not appear
    def test_random_tuesday_is_not_holiday(self):
        assert date(2024, 3, 12) not in duke_nc_holidays(2024)  # random Tuesday


class TestIsDukeHoliday:
    def test_known_holiday_returns_true(self):
        assert is_duke_holiday(date(2024, 7, 4)) is True

    def test_non_holiday_returns_false(self):
        assert is_duke_holiday(date(2024, 7, 5)) is False

    def test_regular_weekend_not_counted_as_holiday(self):
        # is_duke_holiday only checks the holiday list, not weekday()
        assert is_duke_holiday(date(2024, 7, 6)) is False  # Saturday, not a holiday


# ---------------------------------------------------------------------------
# 2. TOU period classification — holiday treated as off-peak
#
# Schedule: weekday on-peak 7am–9pm, fallback off-peak
# A Wednesday on-peak hour should be on-peak.
# Labor Day (first Monday in September) at the same hour should be off-peak.
# ---------------------------------------------------------------------------

# Minimal TOU period list mirroring a typical Duke residential TOU schedule
_TOU_PERIODS = [
    TOUPeriod(
        name="On-Peak",
        weekday_hours="7:00 a.m. to 9:00 p.m.",
    ),
    TOUPeriod(name="Off-Peak"),  # fallback
]


@pytest.mark.parametrize("ts_str,expected_period", [
    # Regular Wednesday at 3 PM → on-peak
    ("2024-09-04T15:00:00", "On-Peak"),
    # Labor Day 2024 (Monday Sep 2) at 3 PM → off-peak (holiday)
    ("2024-09-02T15:00:00", "Off-Peak"),
    # Thanksgiving 2024 (Thursday Nov 28) at noon → off-peak (holiday)
    ("2024-11-28T12:00:00", "Off-Peak"),
    # Christmas 2024 (Wednesday Dec 25) at 6 PM → off-peak (holiday)
    ("2024-12-25T18:00:00", "Off-Peak"),
    # New Year's 2024 (Monday Jan 1) at 8 AM → off-peak (holiday)
    ("2024-01-01T08:00:00", "Off-Peak"),
    # Independence Day 2024 (Thursday Jul 4) at 2 PM → off-peak (holiday)
    ("2024-07-04T14:00:00", "Off-Peak"),
    # Memorial Day 2024 (Monday May 27) at 10 AM → off-peak (holiday)
    ("2024-05-27T10:00:00", "Off-Peak"),
    # Day before Thanksgiving (Wednesday) at noon → on-peak (not a holiday)
    ("2024-11-27T12:00:00", "On-Peak"),
    # Saturday → off-peak (weekend, not holiday list, but weekend_hours absent)
    ("2024-09-07T15:00:00", "Off-Peak"),
])
def test_holiday_classified_as_off_peak(ts_str, expected_period):
    from duke_rates.billing.calculators import IntervalUsagePoint
    point = IntervalUsagePoint(
        timestamp=datetime.fromisoformat(ts_str),
        kwh=1.0,
    )
    result = classify_interval_point(point, _TOU_PERIODS)
    assert result == expected_period, (
        f"{ts_str}: expected {expected_period!r}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# 3. Observed-shift holiday also gets off-peak treatment
# ---------------------------------------------------------------------------

def test_observed_saturday_holiday_treated_as_off_peak():
    """Independence Day 2020 falls on Saturday; observed Friday Jul 3 is off-peak."""
    from duke_rates.billing.calculators import IntervalUsagePoint
    # Jul 3 2020 is a Friday (observed holiday) → should be off-peak
    point_observed = IntervalUsagePoint(
        timestamp=datetime.fromisoformat("2020-07-03T14:00:00"),
        kwh=1.0,
    )
    # Jul 6 2020 is a Monday (regular weekday) → should be on-peak
    point_regular = IntervalUsagePoint(
        timestamp=datetime.fromisoformat("2020-07-06T14:00:00"),
        kwh=1.0,
    )
    assert classify_interval_point(point_observed, _TOU_PERIODS) == "Off-Peak"
    assert classify_interval_point(point_regular, _TOU_PERIODS) == "On-Peak"


def test_observed_sunday_holiday_treated_as_off_peak():
    """New Year's 2023 falls on Sunday; observed Monday Jan 2 is off-peak."""
    from duke_rates.billing.calculators import IntervalUsagePoint
    point_observed = IntervalUsagePoint(
        timestamp=datetime.fromisoformat("2023-01-02T10:00:00"),
        kwh=1.0,
    )
    point_regular = IntervalUsagePoint(
        timestamp=datetime.fromisoformat("2023-01-03T10:00:00"),
        kwh=1.0,
    )
    assert classify_interval_point(point_observed, _TOU_PERIODS) == "Off-Peak"
    assert classify_interval_point(point_regular, _TOU_PERIODS) == "On-Peak"
