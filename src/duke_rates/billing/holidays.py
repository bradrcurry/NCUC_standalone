"""Duke Energy NC/SC recognized holidays for TOU billing.

On recognized holidays Duke Energy treats the full day as off-peak (same as
Sunday/weekend), so on-peak and shoulder periods do not apply.

Holiday list
------------
Duke Energy Progress and Duke Energy Carolinas tariff schedules (e.g. RS-14,
EV-A, GS-TOU) recognize the following holidays:

    New Year's Day          January 1
    Memorial Day            Last Monday in May
    Independence Day        July 4
    Labor Day               First Monday in September
    Thanksgiving Day        Fourth Thursday in November
    Christmas Day           December 25

When a fixed-date holiday (New Year's, Independence Day, Christmas) falls on a
Saturday, the prior Friday is observed.  When it falls on a Sunday, the
following Monday is observed.  Floating holidays (Memorial Day, Labor Day,
Thanksgiving) already land on weekdays by definition.

Results are cached per year so repeated calls within a billing run are free.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from functools import lru_cache


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth occurrence (1-based) of weekday in month/year.

    weekday follows the same convention as date.weekday(): 0=Monday … 6=Sunday.
    """
    first = date(year, month, 1)
    # days until the first occurrence of weekday
    delta = (weekday - first.weekday()) % 7
    return first + timedelta(days=delta + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of weekday in month/year."""
    last_day = calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)
    delta = (last.weekday() - weekday) % 7
    return last - timedelta(days=delta)


def _observed(fixed: date) -> date:
    """Return the observed date for a fixed-date holiday.

    Saturday → prior Friday; Sunday → following Monday; otherwise unchanged.
    """
    wd = fixed.weekday()
    if wd == 5:   # Saturday → Friday
        return fixed - timedelta(days=1)
    if wd == 6:   # Sunday → Monday
        return fixed + timedelta(days=1)
    return fixed


@lru_cache(maxsize=20)
def duke_nc_holidays(year: int) -> frozenset[date]:
    """Return the set of Duke Energy NC observed holiday dates for *year*.

    The returned frozenset contains the *observed* dates (i.e. the date on
    which Duke applies the holiday off-peak treatment), not necessarily the
    calendar date of the holiday itself.

    Parameters
    ----------
    year:
        The calendar year (e.g. 2024).

    Returns
    -------
    frozenset[date]
        Observed holiday dates for that year.
    """
    return frozenset([
        _observed(date(year, 1, 1)),                          # New Year's Day
        _last_weekday(year, 5, 0),                            # Memorial Day (last Monday in May)
        _observed(date(year, 7, 4)),                          # Independence Day
        _nth_weekday(year, 9, 0, 1),                          # Labor Day (1st Monday in September)
        _nth_weekday(year, 11, 3, 4),                         # Thanksgiving (4th Thursday in November)
        _observed(date(year, 12, 25)),                        # Christmas Day
    ])


def is_duke_holiday(d: date) -> bool:
    """Return True if *d* is a Duke Energy NC observed holiday."""
    return d in duke_nc_holidays(d.year)
