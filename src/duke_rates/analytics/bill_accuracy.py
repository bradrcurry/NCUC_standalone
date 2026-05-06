from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from duke_rates.billing.reconciliation import ProgressNCBillReconciliationService
from duke_rates.billing.usage_io import read_usage_file
from duke_rates.db.repository import Repository
from duke_rates.historical.tariff_selector import ProgressNCHistoricalTariffSelector
from duke_rates.models.bill import BillStatementData

LOCAL_TZ = ZoneInfo("America/New_York")


@dataclass
class BillAccuracyReport:
    rows: list[dict[str, object]]
    summary: dict[str, object]


def build_progress_nc_bill_accuracy_report(
    *,
    repository: Repository,
    usage_xml_path: Path,
) -> BillAccuracyReport:
    usage = read_usage_file(usage_xml_path)
    usage_by_date = _build_usage_by_local_date(usage.interval_data)
    service = ProgressNCBillReconciliationService(ProgressNCHistoricalTariffSelector(repository))

    rows: list[dict[str, object]] = []
    status_counts: dict[str, int] = defaultdict(int)

    for stored in repository.list_bill_statements():
        if "Actual Duke Bills" not in stored.source_path:
            continue
        statement = BillStatementData.model_validate_json(stored.statement_json)
        electric = statement.electric_section
        if electric is None:
            continue

        xml_kwh = _sum_usage_for_period(
            usage_by_date,
            statement.service_start,
            statement.service_end,
        )
        actual_total_fallback = electric.total_current_charges
        if actual_total_fallback is None:
            actual_total_fallback = round(
                sum(item.amount or 0.0 for item in electric.line_items),
                2,
            )

        row: dict[str, object] = {
            "bill_id": stored.id,
            "bill_date": stored.bill_date.isoformat() if stored.bill_date else None,
            "service_start": statement.service_start.isoformat() if statement.service_start else None,
            "service_end": statement.service_end.isoformat() if statement.service_end else None,
            "rate_code": electric.rate_code,
            "bill_kwh": _derive_billed_kwh(statement),
            "xml_kwh": xml_kwh,
            "xml_minus_bill_kwh": (
                round(xml_kwh - _derive_billed_kwh(statement), 3) if xml_kwh is not None else None
            ),
            "actual_electric_total": electric.total_current_charges,
            "actual_electric_total_fallback": actual_total_fallback,
            "source_path": stored.source_path,
        }

        try:
            result = service.reconcile(bill_id=stored.id, statement=statement)
            line_items = {item.key: item for item in result.line_items}
            total_delta = result.total_delta
            if total_delta is None and actual_total_fallback is not None:
                total_delta = round(actual_total_fallback - result.estimated_electric_total, 2)

            row.update(
                {
                    "estimated_electric_total": result.estimated_electric_total,
                    "total_delta": total_delta,
                    "summary_rider_actual": _actual_amount(line_items, "summary_rider_adjustments"),
                    "summary_rider_estimated": _estimated_amount(
                        line_items, "summary_rider_adjustments"
                    ),
                    "summary_rider_delta": _delta_amount(line_items, "summary_rider_adjustments"),
                    "storm_actual": _actual_amount(line_items, "storm_recovery_charge"),
                    "storm_estimated": _estimated_amount(line_items, "storm_recovery_charge"),
                    "storm_delta": _delta_amount(line_items, "storm_recovery_charge"),
                    "clean_energy_actual": _actual_amount(line_items, "clean_energy_rider"),
                    "clean_energy_estimated": _estimated_amount(line_items, "clean_energy_rider"),
                    "clean_energy_delta": _delta_amount(line_items, "clean_energy_rider"),
                    "energy_actual": _actual_amount(line_items, "energy_charge"),
                    "energy_estimated": _estimated_amount(line_items, "energy_charge"),
                    "energy_delta": _delta_amount(line_items, "energy_charge"),
                    "bill_coverage_note": " | ".join(result.notes),
                    "comparison_status": _comparison_status(
                        rate_code=electric.rate_code or "",
                        total_delta=total_delta,
                        actual_total=electric.total_current_charges,
                    ),
                }
            )
        except Exception as exc:
            row.update(
                {
                    "estimated_electric_total": None,
                    "total_delta": None,
                    "summary_rider_actual": None,
                    "summary_rider_estimated": None,
                    "summary_rider_delta": None,
                    "storm_actual": None,
                    "storm_estimated": None,
                    "storm_delta": None,
                    "clean_energy_actual": None,
                    "clean_energy_estimated": None,
                    "clean_energy_delta": None,
                    "energy_actual": None,
                    "energy_estimated": None,
                    "energy_delta": None,
                    "bill_coverage_note": str(exc),
                    "comparison_status": "error",
                }
            )

        status_counts[str(row["comparison_status"])] += 1
        rows.append(row)

    rows.sort(key=lambda item: (item["bill_date"] or "", item["bill_id"]))
    summary = {
        "bill_count": len(rows),
        "usage_xml_path": str(usage_xml_path),
        "status_counts": dict(sorted(status_counts.items())),
        "good_match_count": sum(1 for row in rows if row["comparison_status"] == "good_match"),
        "close_match_count": sum(1 for row in rows if row["comparison_status"] == "close_match"),
        "tou_needs_work_count": sum(
            1 for row in rows if row["comparison_status"] == "tou_needs_work"
        ),
        "max_abs_total_delta": max(
            (
                abs(float(row["total_delta"]))
                for row in rows
                if row.get("total_delta") is not None
            ),
            default=0.0,
        ),
    }
    return BillAccuracyReport(rows=rows, summary=summary)


def export_progress_nc_bill_accuracy_report(
    *,
    repository: Repository,
    usage_xml_path: Path,
    output_dir: Path,
) -> BillAccuracyReport:
    report = build_progress_nc_bill_accuracy_report(
        repository=repository,
        usage_xml_path=usage_xml_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "progress_nc_actual_bill_accuracy.json"
    csv_path = output_dir / "progress_nc_actual_bill_accuracy.csv"
    summary_path = output_dir / "progress_nc_actual_bill_accuracy_summary.json"

    json_path.write_text(
        json.dumps(report.rows, indent=2, default=str),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(report.summary, indent=2, default=str),
        encoding="utf-8",
    )
    if report.rows:
        fieldnames = list(report.rows[0].keys())
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(report.rows)

    return report


def _build_usage_by_local_date(interval_points) -> dict[str, float]:
    usage_by_date: dict[str, float] = defaultdict(float)
    for point in interval_points:
        timestamp = point.timestamp.astimezone(LOCAL_TZ) if point.timestamp.tzinfo else point.timestamp
        usage_by_date[timestamp.date().isoformat()] += point.kwh
    return dict(usage_by_date)


def _sum_usage_for_period(
    usage_by_date: dict[str, float],
    start_date,
    end_date,
) -> float | None:
    if start_date is None or end_date is None:
        return None
    total = 0.0
    current = start_date
    while current <= end_date:
        total += usage_by_date.get(current.isoformat(), 0.0)
        current = current.fromordinal(current.toordinal() + 1)
    return round(total, 3)


def _derive_billed_kwh(statement: BillStatementData) -> float:
    electric = statement.electric_section
    if electric is None:
        return 0.0
    return round(
        sum(
            item.quantity or 0.0
            for item in electric.line_items
            if item.unit == "kWh" and "energy charge" in item.label.lower()
        ),
        3,
    )


def _actual_amount(line_items: dict[str, object], key: str) -> float | None:
    item = line_items.get(key)
    return item.actual_amount if item else None


def _estimated_amount(line_items: dict[str, object], key: str) -> float | None:
    item = line_items.get(key)
    return item.estimated_amount if item else None


def _delta_amount(line_items: dict[str, object], key: str) -> float | None:
    item = line_items.get(key)
    return item.delta if item else None


def _comparison_status(*, rate_code: str, total_delta: float | None, actual_total: float | None) -> str:
    normalized_code = (rate_code or "").replace(" ", "").upper()
    if total_delta is None:
        if normalized_code.startswith("R-TOU"):
            return "tou_needs_work"
        return "needs_review"
    if abs(total_delta) <= 1.0:
        return "good_match"
    if abs(total_delta) <= 2.5:
        return "close_match"
    if actual_total is None:
        return "needs_review"
    return "poor_match"
