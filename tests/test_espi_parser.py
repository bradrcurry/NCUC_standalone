"""Tests for the ESPI/Green Button XML parser."""
from __future__ import annotations

import datetime
import textwrap

import pytest

from duke_rates.billing.espi_parser import (
    MonthlyUsageSummary,
    UsageProfile,
    _classify_interval,
    parse_espi_xml,
)

import zoneinfo
_EASTERN = zoneinfo.ZoneInfo("America/New_York")


def _dt(year, month, day, hour, minute=0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=_EASTERN)


# ---------------------------------------------------------------------------
# TOU classification
# ---------------------------------------------------------------------------

class TestClassifyInterval:
    def test_weekday_on_peak(self):
        assert _classify_interval(_dt(2025, 11, 3, 14, 0)) == "on_peak"
        assert _classify_interval(_dt(2025, 11, 3, 16, 30)) == "on_peak"
        assert _classify_interval(_dt(2025, 11, 3, 20, 59)) == "on_peak"

    def test_weekday_off_peak_morning(self):
        assert _classify_interval(_dt(2025, 11, 3, 6, 0)) == "off_peak"
        assert _classify_interval(_dt(2025, 11, 3, 10, 0)) == "off_peak"
        assert _classify_interval(_dt(2025, 11, 3, 13, 59)) == "off_peak"

    def test_weekday_discount_evening(self):
        assert _classify_interval(_dt(2025, 11, 3, 21, 0)) == "discount"
        assert _classify_interval(_dt(2025, 11, 3, 23, 0)) == "discount"

    def test_weekday_discount_overnight(self):
        assert _classify_interval(_dt(2025, 11, 3, 0, 0)) == "discount"
        assert _classify_interval(_dt(2025, 11, 3, 5, 45)) == "discount"

    def test_weekday_on_peak_boundary_exclusive(self):
        # 9:00 PM is NOT on-peak (end is exclusive)
        assert _classify_interval(_dt(2025, 11, 3, 21, 0)) == "discount"

    def test_saturday_always_off_peak(self):
        assert _classify_interval(_dt(2025, 11, 1, 15, 0)) == "off_peak"
        assert _classify_interval(_dt(2025, 11, 1, 22, 0)) == "off_peak"

    def test_sunday_always_off_peak(self):
        assert _classify_interval(_dt(2025, 11, 2, 16, 0)) == "off_peak"

    def test_thanksgiving_holiday_off_peak(self):
        # Thanksgiving 2025: Thursday Nov 27 — Duke holiday
        assert _classify_interval(_dt(2025, 11, 27, 15, 0)) == "off_peak"

    def test_christmas_holiday_off_peak(self):
        # Christmas 2025: Thursday Dec 25
        assert _classify_interval(_dt(2025, 12, 25, 14, 30)) == "off_peak"

    def test_labor_day_holiday_off_peak(self):
        # Labor Day 2025: Monday Sep 1
        assert _classify_interval(_dt(2025, 9, 1, 16, 0)) == "off_peak"


# ---------------------------------------------------------------------------
# ESPI XML parsing
# ---------------------------------------------------------------------------

def _make_espi_xml(readings: list[tuple[int, float]]) -> bytes:
    """Build a minimal ESPI XML with the given (unix_ts, kwh) readings."""
    items = "\n".join(
        f"""<espi:IntervalReading>
    <espi:timePeriod><espi:start>{ts}</espi:start></espi:timePeriod>
    <espi:readingQuality>ACTUAL</espi:readingQuality>
    <espi:value>{kwh}</espi:value>
</espi:IntervalReading>"""
        for ts, kwh in readings
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ns3:entry xmlns:espi="http://naesb.org/espi" xmlns:ns3="http://www.w3.org/2005/Atom">
  <ns3:content>
    <espi:IntervalBlock>
      <espi:interval>
        <espi:servicePointId>TEST-001</espi:servicePointId>
        <espi:secondsPerInterval>900</espi:secondsPerInterval>
        <espi:start>1743397200</espi:start>
      </espi:interval>
      {items}
    </espi:IntervalBlock>
  </ns3:content>
</ns3:entry>"""
    return xml.encode()


class TestParseEspiXml:
    def test_empty_raises(self):
        xml = _make_espi_xml([])
        # strip IntervalReading elements
        xml_str = xml.decode().replace(
            '<espi:IntervalReading>', '<!--'
        ).replace('</espi:IntervalReading>', '-->')
        with pytest.raises(ValueError, match="No.*IntervalReading"):
            parse_espi_xml(xml_str.encode())

    def test_single_reading_parsed(self):
        # 2025-11-03 15:00 Eastern = weekday on-peak
        # Nov 3, 2025 3pm ET = Unix: 1730653200 (UTC 8pm = 2025-11-03T20:00:00Z)
        ts = int(datetime.datetime(2025, 11, 3, 20, 0, 0,
                 tzinfo=datetime.timezone.utc).timestamp())
        profile = parse_espi_xml(_make_espi_xml([(ts, 0.25)]))
        assert profile.interval_count == 1
        assert abs(profile.total_kwh - 0.25) < 0.001
        assert len(profile.months) == 1
        m = profile.months[0]
        assert m.year == 2025 and m.month == 11
        assert abs(m.on_peak_kwh - 0.25) < 0.001
        assert m.off_peak_kwh == 0.0

    def test_weekend_classified_off_peak(self):
        # 2025-11-01 (Saturday) 3pm ET
        ts = int(datetime.datetime(2025, 11, 1, 20, 0, 0,
                 tzinfo=datetime.timezone.utc).timestamp())
        profile = parse_espi_xml(_make_espi_xml([(ts, 0.5)]))
        m = profile.months[0]
        assert m.off_peak_kwh == pytest.approx(0.5)
        assert m.on_peak_kwh == 0.0

    def test_discount_period_classified(self):
        # 2025-11-03 (Monday) 10pm ET = discount
        ts = int(datetime.datetime(2025, 11, 4, 3, 0, 0,
                 tzinfo=datetime.timezone.utc).timestamp())  # 10pm ET = 3am UTC next day
        profile = parse_espi_xml(_make_espi_xml([(ts, 0.3)]))
        m = profile.months[0]
        assert m.discount_kwh == pytest.approx(0.3)
        assert m.on_peak_kwh == 0.0

    def test_multi_month_grouping(self):
        # Two readings in different months
        ts_oct = int(datetime.datetime(2025, 10, 15, 20, 0, 0,
                     tzinfo=datetime.timezone.utc).timestamp())  # Oct weekday 4pm ET
        ts_nov = int(datetime.datetime(2025, 11, 15, 20, 0, 0,
                     tzinfo=datetime.timezone.utc).timestamp())  # Nov weekday 3pm ET (DST ended)
        profile = parse_espi_xml(_make_espi_xml([(ts_oct, 1.0), (ts_nov, 2.0)]))
        assert len(profile.months) == 2
        assert profile.months[0].month == 10
        assert profile.months[1].month == 11
        assert abs(profile.total_kwh - 3.0) < 0.001

    def test_peak_kw_computed(self):
        # 0.5 kWh in 15-min = 2.0 kW
        ts = int(datetime.datetime(2025, 11, 3, 20, 0, 0,
                 tzinfo=datetime.timezone.utc).timestamp())
        profile = parse_espi_xml(_make_espi_xml([(ts, 0.5)]))
        assert profile.months[0].peak_kw == pytest.approx(2.0)

    def test_service_point_id_parsed(self):
        ts = int(datetime.datetime(2025, 11, 3, 20, 0, 0,
                 tzinfo=datetime.timezone.utc).timestamp())
        profile = parse_espi_xml(_make_espi_xml([(ts, 0.1)]))
        assert profile.service_point_id == "TEST-001"

    def test_to_bill_input_kwargs(self):
        m = MonthlyUsageSummary(year=2025, month=11,
                                total_kwh=1000.0, on_peak_kwh=250.0,
                                off_peak_kwh=600.0, discount_kwh=150.0,
                                peak_kw=5.0)
        kwargs = m.to_bill_input_kwargs()
        assert kwargs["monthly_kwh"] == 1000.0
        assert kwargs["on_peak_kwh"] == 250.0
        assert kwargs["off_peak_kwh"] == 600.0
        assert kwargs["discount_kwh"] == 150.0
        assert kwargs["service_date"] == datetime.date(2025, 11, 1)
        assert kwargs["peak_kw"] == 5.0

    def test_pct_properties(self):
        m = MonthlyUsageSummary(year=2025, month=6,
                                total_kwh=1000.0, on_peak_kwh=250.0,
                                off_peak_kwh=600.0, discount_kwh=150.0)
        assert m.on_peak_pct == pytest.approx(25.0)
        assert m.off_peak_pct == pytest.approx(60.0)
        assert m.discount_pct == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Live file smoke test (skipped if file absent)
# ---------------------------------------------------------------------------

class TestLiveEspiFile:
    SAMPLE_PATH = "data/usage/Energy Usage.xml"

    @pytest.fixture(autouse=True)
    def _check_file(self):
        import os
        if not os.path.exists(self.SAMPLE_PATH):
            pytest.skip("Live ESPI sample file not present")

    def test_parses_without_error(self):
        profile = parse_espi_xml(self.SAMPLE_PATH)
        assert profile.interval_count > 30000
        assert profile.total_kwh > 10000
        assert len(profile.months) >= 10

    def test_all_months_sum_to_total(self):
        profile = parse_espi_xml(self.SAMPLE_PATH)
        month_sum = sum(m.total_kwh for m in profile.months)
        assert abs(month_sum - profile.total_kwh) < 0.01

    def test_tou_periods_sum_to_total_per_month(self):
        profile = parse_espi_xml(self.SAMPLE_PATH)
        for m in profile.months:
            period_sum = m.on_peak_kwh + m.off_peak_kwh + m.discount_kwh
            assert abs(period_sum - m.total_kwh) < 0.01, \
                f"{m.year}-{m.month:02d}: period sum {period_sum:.2f} != total {m.total_kwh:.2f}"

    def test_on_peak_pct_plausible(self):
        profile = parse_espi_xml(self.SAMPLE_PATH)
        for m in profile.months[1:-1]:  # skip partial first/last months
            assert 5.0 <= m.on_peak_pct <= 55.0, \
                f"{m.year}-{m.month:02d}: on_peak_pct={m.on_peak_pct:.1f}% out of plausible range"

    def test_peak_kw_plausible(self):
        profile = parse_espi_xml(self.SAMPLE_PATH)
        for m in profile.months:
            assert 0 < m.peak_kw < 100, \
                f"{m.year}-{m.month:02d}: peak_kw={m.peak_kw:.2f} implausible"

    def test_bill_input_kwargs_valid(self):
        from duke_rates.billing.tariff_engine import BillInput
        profile = parse_espi_xml(self.SAMPLE_PATH)
        for m in profile.months[1:-1]:
            kwargs = m.to_bill_input_kwargs()
            bi = BillInput(**kwargs)
            assert bi.monthly_kwh > 0
