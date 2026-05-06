"""ESPI/Green Button XML parser for Duke Energy 15-minute interval usage exports.

Parses the XML format exported from Duke's MyAccount portal (standard ESPI
schema, 900-second intervals) into a structured usage profile with per-month
TOU breakdowns suitable for feeding directly into TariffBillingEngine.

Duke NC TOU on-peak hours (R-TOU, R-TOUD, SGS-TOUE):
    Weekdays (non-holiday): 2:00 PM – 9:00 PM  (14:00–20:59)
    Weekends and Duke holidays: always off-peak

Duke NC R-TOU Discount period:
    Weekdays (non-holiday): 9:00 PM – midnight AND midnight – 6:00 AM
    i.e. 9 PM – 6 AM next day (21:00–05:59)  — off-peak is the remainder
    (6:00 AM – 2:00 PM)
"""
from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import IO

from duke_rates.billing.holidays import is_duke_holiday

# Duke Energy operates in the Eastern time zone
import zoneinfo

_EASTERN = zoneinfo.ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# TOU period boundaries (Duke NC R-TOU / R-TOUD / SGS-TOUE)
# ---------------------------------------------------------------------------
_ON_PEAK_START = datetime.time(14, 0)   # 2:00 PM
_ON_PEAK_END   = datetime.time(21, 0)   # 9:00 PM  (exclusive)
_DISCOUNT_AM_END   = datetime.time(6, 0)    # 6:00 AM  (discount ends)
_DISCOUNT_PM_START = datetime.time(21, 0)   # 9:00 PM  (discount starts)


def _classify_interval(local_dt: datetime.datetime) -> str:
    """Return 'on_peak', 'off_peak', or 'discount' for a local Eastern datetime.

    Rules (Duke NC TOU weekday):
        on_peak:   14:00 – 20:59
        discount:  21:00 – 05:59 (next day)
        off_peak:  06:00 – 13:59
    Weekends and Duke holidays: always off_peak.
    """
    d = local_dt.date()
    is_weekend = local_dt.weekday() >= 5
    if is_weekend or is_duke_holiday(d):
        return "off_peak"

    t = local_dt.time()
    if _ON_PEAK_START <= t < _ON_PEAK_END:
        return "on_peak"
    if t >= _DISCOUNT_PM_START or t < _DISCOUNT_AM_END:
        return "discount"
    return "off_peak"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MonthlyUsageSummary:
    """Aggregated TOU usage for one calendar month."""
    year: int
    month: int
    total_kwh: float = 0.0
    on_peak_kwh: float = 0.0
    off_peak_kwh: float = 0.0
    discount_kwh: float = 0.0
    peak_kw: float = 0.0          # highest 15-min demand reading (kWh * 4)
    interval_count: int = 0

    @property
    def service_date(self) -> datetime.date:
        return datetime.date(self.year, self.month, 1)

    @property
    def on_peak_pct(self) -> float:
        return (self.on_peak_kwh / self.total_kwh * 100) if self.total_kwh else 0.0

    @property
    def off_peak_pct(self) -> float:
        return (self.off_peak_kwh / self.total_kwh * 100) if self.total_kwh else 0.0

    @property
    def discount_pct(self) -> float:
        return (self.discount_kwh / self.total_kwh * 100) if self.total_kwh else 0.0

    def to_bill_input_kwargs(self) -> dict:
        """Return kwargs for BillInput construction."""
        return dict(
            monthly_kwh=round(self.total_kwh, 2),
            service_date=self.service_date,
            on_peak_kwh=round(self.on_peak_kwh, 2),
            off_peak_kwh=round(self.off_peak_kwh, 2),
            discount_kwh=round(self.discount_kwh, 2),
            peak_kw=round(self.peak_kw, 3) if self.peak_kw > 0 else None,
        )


@dataclass
class UsageProfile:
    """Full parsed usage profile from an ESPI export."""
    service_point_id: str | None = None
    meter_serial: str | None = None
    seconds_per_interval: int = 900
    total_kwh: float = 0.0
    interval_count: int = 0
    date_range_start: datetime.date | None = None
    date_range_end: datetime.date | None = None
    months: list[MonthlyUsageSummary] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def month(self, year: int, month: int) -> MonthlyUsageSummary | None:
        return next((m for m in self.months if m.year == year and m.month == month), None)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_ESPI_NS = "http://naesb.org/espi"


def parse_espi_xml(source: bytes | str | IO) -> UsageProfile:
    """Parse a Duke Energy ESPI/Green Button XML export.

    Args:
        source: Raw XML bytes, a filename string, or a file-like object.

    Returns:
        UsageProfile with per-month TOU breakdowns.

    Raises:
        ValueError: If the XML cannot be parsed or contains no interval readings.
    """
    if isinstance(source, bytes):
        root = ET.fromstring(source)
    elif isinstance(source, str):
        root = ET.parse(source).getroot()
    else:
        root = ET.parse(source).getroot()

    profile = UsageProfile()

    # --- Metadata ---
    def _text(tag: str) -> str | None:
        el = root.find(f".//{{{_ESPI_NS}}}{tag}")
        return el.text.strip() if el is not None and el.text else None

    profile.service_point_id = _text("servicePointId")
    profile.meter_serial = _text("meterSerialNumber")
    spi_text = _text("secondsPerInterval")
    if spi_text and spi_text.isdigit():
        profile.seconds_per_interval = int(spi_text)

    # --- Interval readings ---
    readings = root.findall(f".//{{{_ESPI_NS}}}IntervalReading")
    if not readings:
        raise ValueError("No IntervalReading elements found in ESPI XML.")

    hours_per_interval = profile.seconds_per_interval / 3600.0
    month_map: dict[tuple[int, int], MonthlyUsageSummary] = {}
    skipped = 0

    for reading in readings:
        start_el = reading.find(f".//{{{_ESPI_NS}}}start")
        value_el = reading.find(f"{{{_ESPI_NS}}}value")
        if start_el is None or value_el is None:
            skipped += 1
            continue

        try:
            ts_unix = int(start_el.text.strip())
            kwh = float(value_el.text.strip())
        except (ValueError, TypeError):
            skipped += 1
            continue

        # Convert Unix timestamp → Eastern local time
        utc_dt = datetime.datetime.fromtimestamp(ts_unix, tz=datetime.timezone.utc)
        local_dt = utc_dt.astimezone(_EASTERN)

        period = _classify_interval(local_dt)
        key = (local_dt.year, local_dt.month)
        if key not in month_map:
            month_map[key] = MonthlyUsageSummary(year=local_dt.year, month=local_dt.month)

        m = month_map[key]
        m.total_kwh += kwh
        m.interval_count += 1
        if period == "on_peak":
            m.on_peak_kwh += kwh
        elif period == "discount":
            m.discount_kwh += kwh
        else:
            m.off_peak_kwh += kwh

        # Peak demand: kWh in a 15-min interval × 4 = kW
        kw = kwh / hours_per_interval
        if kw > m.peak_kw:
            m.peak_kw = kw

    if skipped:
        profile.warnings.append(f"{skipped} interval readings skipped (missing start or value).")

    if not month_map:
        raise ValueError("No valid interval readings could be parsed.")

    profile.months = sorted(month_map.values(), key=lambda m: (m.year, m.month))
    profile.total_kwh = sum(m.total_kwh for m in profile.months)
    profile.interval_count = sum(m.interval_count for m in profile.months)

    first = profile.months[0]
    last = profile.months[-1]
    profile.date_range_start = first.service_date
    profile.date_range_end = datetime.date(last.year, last.month,
        _last_day_of_month(last.year, last.month))

    # Warn about months with suspiciously low interval counts
    # (partial months at start/end are expected; interior gaps are not)
    expected = round(30 * 24 * 60 / (profile.seconds_per_interval / 60))
    interior = profile.months[1:-1]
    for m in interior:
        if m.interval_count < expected * 0.8:
            profile.warnings.append(
                f"{m.year}-{m.month:02d}: only {m.interval_count} intervals "
                f"(expected ~{expected}); possible data gap."
            )

    return profile


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)).day
