from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from duke_rates.billing.calculators import IntervalUsagePoint, UsageInput


def read_usage_file(path: Path) -> UsageInput:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _read_json_usage(path)
    if suffix == ".csv":
        return _read_csv_usage(path)
    if suffix == ".xml":
        return _read_xml_usage(path)
    raise ValueError("Usage file must be JSON, CSV, or XML.")


def _read_json_usage(path: Path) -> UsageInput:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        interval_data = [IntervalUsagePoint.model_validate(row) for row in payload]
        return _usage_from_interval_data(interval_data)
    return UsageInput.model_validate(payload)


def _read_csv_usage(path: Path) -> UsageInput:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("Usage CSV is empty.")
    if "timestamp" in rows[0] and "kwh" in rows[0]:
        interval_data = [
            IntervalUsagePoint(
                timestamp=row["timestamp"],
                kwh=float(row["kwh"]),
                kw=float(row["kw"]) if row.get("kw") else None,
            )
            for row in rows
        ]
        return _usage_from_interval_data(interval_data)

    row = rows[0]
    monthly_kwh = float(row["monthly_kwh"])
    peak_kw = float(row["peak_kw"]) if row.get("peak_kw") else None
    return UsageInput(monthly_kwh=monthly_kwh, peak_kw=peak_kw)


def _read_xml_usage(path: Path) -> UsageInput:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    interval_data = _parse_green_button_interval_data(root)
    if not interval_data:
        interval_data = _parse_simple_interval_xml(root)
    if not interval_data:
        raise ValueError("No interval readings found in XML usage file.")
    return _usage_from_interval_data(interval_data)


def _parse_green_button_interval_data(root: ET.Element) -> list[IntervalUsagePoint]:
    multiplier = _first_int(root, "powerOfTenMultiplier", default=0)
    uom = _first_int(root, "uom", default=None)
    seconds_per_interval = _first_int(root, "secondsPerInterval", default=None)
    interval_points: list[IntervalUsagePoint] = []

    for reading in root.findall(".//{*}IntervalReading"):
        start = _find_text(reading, "start")
        value_text = _find_text(reading, "value")
        if not start or not value_text:
            continue
        raw_value = float(value_text)
        kwh = _convert_interval_value_to_kwh(raw_value, multiplier=multiplier, uom=uom)
        interval_points.append(
            IntervalUsagePoint(
                timestamp=_coerce_timestamp(start),
                kwh=kwh,
                kw=_derive_kw(kwh, seconds_per_interval),
            )
        )
    return interval_points


def _parse_simple_interval_xml(root: ET.Element) -> list[IntervalUsagePoint]:
    interval_points: list[IntervalUsagePoint] = []
    for node in root.findall(".//*[@timestamp]"):
        timestamp = node.attrib.get("timestamp")
        kwh = node.attrib.get("kwh") or node.attrib.get("value")
        if timestamp and kwh:
            interval_points.append(
                IntervalUsagePoint(
                    timestamp=_coerce_timestamp(timestamp),
                    kwh=float(kwh),
                    kw=float(node.attrib["kw"]) if "kw" in node.attrib else None,
                )
            )

    if interval_points:
        return interval_points

    simple_nodes = (
        root.findall(".//interval")
        + root.findall(".//reading")
        + root.findall(".//Interval")
    )
    for node in simple_nodes:
        timestamp = _child_text(node, ("timestamp", "start", "datetime"))
        kwh = _child_text(node, ("kwh", "value", "usage"))
        if timestamp and kwh:
            interval_points.append(
                IntervalUsagePoint(
                    timestamp=_coerce_timestamp(timestamp),
                    kwh=float(kwh),
                    kw=float(_child_text(node, ("kw", "demand")) or 0.0) or None,
                )
            )
    return interval_points


def _usage_from_interval_data(interval_data: list[IntervalUsagePoint]) -> UsageInput:
    return UsageInput(
        monthly_kwh=sum(point.kwh for point in interval_data),
        peak_kw=max((point.kw for point in interval_data if point.kw is not None), default=None),
        interval_data=interval_data,
    )


def _convert_interval_value_to_kwh(
    raw_value: float,
    *,
    multiplier: int,
    uom: int | None,
) -> float:
    scaled = raw_value * (10**multiplier)
    if uom == 72:
        return scaled / 1000.0
    if uom == 73:
        return scaled
    if scaled > 100:
        return scaled / 1000.0
    return scaled


def _derive_kw(kwh: float, seconds_per_interval: int | None) -> float | None:
    if not seconds_per_interval:
        return None
    hours = seconds_per_interval / 3600.0
    if hours <= 0:
        return None
    return round(kwh / hours, 6)


def _coerce_timestamp(value: str):
    stripped = value.strip()
    if stripped.isdigit():
        from datetime import UTC, datetime

        return datetime.fromtimestamp(int(stripped), tz=UTC)
    return IntervalUsagePoint(timestamp=stripped, kwh=0.0).timestamp


def _find_text(node: ET.Element, local_name: str) -> str | None:
    found = node.find(f".//{{*}}{local_name}")
    return found.text.strip() if found is not None and found.text else None


def _first_int(node: ET.Element, local_name: str, default: int | None) -> int | None:
    value = _find_text(node, local_name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _child_text(node: ET.Element, names: tuple[str, ...]) -> str | None:
    for child in list(node):
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in names and child.text:
            return child.text.strip()
    return None
