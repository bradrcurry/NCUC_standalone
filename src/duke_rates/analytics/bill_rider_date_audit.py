from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from duke_rates.analytics.dep_progress import load_dep_res_rider_history
from duke_rates.billing.usage_io import read_usage_file
from duke_rates.db.repository import Repository
from duke_rates.models.bill import BillLineItem, BillStatementData
from duke_rates.models.parse_result import DocumentParseResult
from duke_rates.parse.normalization import parse_effective_date
from duke_rates.parse.rider_parser import parse_rider_text
from duke_rates.parse.pdf_text import extract_pdf_text

LOCAL_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class RiderSnapshot:
    component_key: str
    effective_date: date
    value: float
    unit: str
    source_title: str
    source_path: str
    source_kind: str


def export_progress_nc_bill_rider_date_audit(
    *,
    repository: Repository,
    usage_xml_path: Path,
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows, summary = build_progress_nc_bill_rider_date_audit(
        repository=repository,
        usage_xml_path=usage_xml_path,
    )

    csv_path = output_dir / "progress_nc_bill_rider_date_audit.csv"
    json_path = output_dir / "progress_nc_bill_rider_date_audit.json"
    summary_path = output_dir / "progress_nc_bill_rider_date_audit_summary.json"

    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    json_path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    return {
        "csv": csv_path,
        "json": json_path,
        "summary_json": summary_path,
    }


def build_progress_nc_bill_rider_date_audit(
    *,
    repository: Repository,
    usage_xml_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    usage = read_usage_file(usage_xml_path)
    usage_by_date = _build_usage_by_local_date(usage.interval_data)
    snapshots_by_component = _load_progress_rider_snapshots(repository)

    component_specs = [
        ("summary_rider_adjustments", "Summary of Rider Adjustments"),
        ("storm_recovery_charge", "Storm Recovery Charge"),
        ("clean_energy_rider", "Clean Energy Rider"),
    ]

    rows: list[dict[str, Any]] = []
    for stored in repository.list_bill_statements():
        if "Actual Duke Bills" not in stored.source_path:
            continue
        statement = BillStatementData.model_validate_json(stored.statement_json)
        electric = statement.electric_section
        if electric is None:
            continue
        billed_kwh = _derive_billed_kwh(statement)
        bill_days = ((statement.service_end - statement.service_start).days + 1) if statement.service_start and statement.service_end else None

        for component_key, label in component_specs:
            snapshots = snapshots_by_component.get(component_key, [])
            actual_items = [item for item in electric.line_items if item.label == label]
            if not actual_items:
                continue
            actual_total = round(sum(item.amount or 0.0 for item in actual_items), 2)
            latest_snapshot = _latest_snapshot_on_or_before(
                snapshots,
                statement.service_end,
            )
            latest_estimate = _estimate_component_amount(
                snapshot=latest_snapshot,
                billed_kwh=billed_kwh,
                bill_days=bill_days,
                total_bill_days=bill_days,
            ) if latest_snapshot else None
            latest_delta = _round_delta(actual_total, latest_estimate)

            prorated_estimate, segment_dates = _estimate_component_prorated(
                snapshots=snapshots,
                start_date=statement.service_start,
                end_date=statement.service_end,
                billed_kwh=billed_kwh,
                usage_by_date=usage_by_date,
                component_key=component_key,
            )
            prorated_delta = _round_delta(actual_total, prorated_estimate)
            split_dates = sorted(
                {
                    item.period_start.isoformat()
                    for item in actual_items
                    if item.is_subperiod_detail and item.period_start and statement.service_start and item.period_start > statement.service_start
                }
            )
            matching_effective_dates = sorted(
                {
                    snap.effective_date.isoformat()
                    for snap in snapshots
                    if snap.effective_date.isoformat() in split_dates
                }
            )
            best_single_snapshot, best_single_estimate = _best_single_snapshot_fit(
                snapshots=snapshots,
                actual_total=actual_total,
                billed_kwh=billed_kwh,
                bill_days=bill_days,
            )
            best_single_delta = _round_delta(actual_total, best_single_estimate)

            rows.append(
                {
                    "bill_id": stored.id,
                    "bill_date": stored.bill_date.isoformat() if stored.bill_date else None,
                    "service_start": statement.service_start.isoformat() if statement.service_start else None,
                    "service_end": statement.service_end.isoformat() if statement.service_end else None,
                    "rate_code": (electric.rate_code or "").replace(" ", ""),
                    "component_key": component_key,
                    "component_label": label,
                    "actual_amount": actual_total,
                    "billed_kwh": billed_kwh,
                    "bill_split_dates": ",".join(split_dates),
                    "matching_db_effective_dates": ",".join(matching_effective_dates),
                    "latest_effective_date": latest_snapshot.effective_date.isoformat() if latest_snapshot else None,
                    "latest_value": latest_snapshot.value if latest_snapshot else None,
                    "latest_unit": latest_snapshot.unit if latest_snapshot else None,
                    "latest_source_title": latest_snapshot.source_title if latest_snapshot else None,
                    "latest_estimated_amount": latest_estimate,
                    "latest_delta": latest_delta,
                    "prorated_estimated_amount": prorated_estimate,
                    "prorated_delta": prorated_delta,
                    "prorated_effective_dates_used": ",".join(segment_dates),
                    "best_single_fit_effective_date": best_single_snapshot.effective_date.isoformat() if best_single_snapshot else None,
                    "best_single_fit_estimated_amount": best_single_estimate,
                    "best_single_fit_delta": best_single_delta,
                    "assessment": _assessment(
                        split_dates=split_dates,
                        matching_effective_dates=matching_effective_dates,
                        latest_delta=latest_delta,
                        prorated_delta=prorated_delta,
                        best_single_snapshot=best_single_snapshot,
                        latest_snapshot=latest_snapshot,
                        component_key=component_key,
                    ),
                }
            )

    rows.sort(key=lambda row: (row["bill_date"] or "", row["component_key"]))
    summary = {
        "bill_count": len({row["bill_id"] for row in rows}),
        "row_count": len(rows),
        "components_audited": sorted({row["component_key"] for row in rows}),
        "assessment_counts": dict(_count_by(rows, "assessment")),
        "split_validated_rows": sum(1 for row in rows if row["matching_db_effective_dates"]),
    }
    return rows, summary


def _load_progress_rider_snapshots(repository: Repository) -> dict[str, list[RiderSnapshot]]:
    snapshots_by_component: dict[str, list[RiderSnapshot]] = defaultdict(list)

    rider_totals_df, _ = load_dep_res_rider_history(database_path=repository.database_path)
    for row in rider_totals_df.to_dict("records"):
        effective_date = _coerce_date(row["effective_date"])
        if not effective_date:
            continue
        snapshots_by_component["summary_rider_adjustments"].append(
            RiderSnapshot(
                component_key="summary_rider_adjustments",
                effective_date=effective_date,
                value=float(row["total_rider_cents_per_kwh"]),
                unit="cents_per_kwh",
                source_title=str(row["source_pdf"]),
                source_path=str(row["source_pdf"]),
                source_kind=str(row.get("source_kind") or "clean_summary"),
            )
        )

    for result, source_title, source_path, source_kind in _iter_progress_rider_parse_results(repository):
        rider = result.rider
        if rider is None:
            continue
        effective_date = parse_effective_date(rider.effective_date)
        if effective_date is None:
            continue
        for component in rider.charge_components:
            key = _component_key_for_label(component.bill_label)
            if key not in {"storm_recovery_charge", "clean_energy_rider"}:
                continue
            snapshots_by_component[key].append(
                RiderSnapshot(
                    component_key=key,
                    effective_date=effective_date,
                    value=float(component.value),
                    unit=str(component.unit),
                    source_title=source_title,
                    source_path=source_path,
                    source_kind=source_kind,
                )
            )

    for component_key in list(snapshots_by_component):
        deduped: dict[tuple[date, float, str], RiderSnapshot] = {}
        for snap in snapshots_by_component[component_key]:
            deduped[(snap.effective_date, snap.value, snap.unit)] = snap
        snapshots_by_component[component_key] = sorted(
            deduped.values(),
            key=lambda snap: (snap.effective_date, snap.value, snap.source_title),
        )
    return snapshots_by_component


def _iter_progress_rider_parse_results(
    repository: Repository,
) -> list[tuple[DocumentParseResult, str, str, str]]:
    records: list[tuple[DocumentParseResult, str, str, str]] = []
    for historical in repository.list_historical_documents(state="NC", company="progress"):
        if historical.parsed_result_json:
            result = DocumentParseResult.model_validate_json(historical.parsed_result_json)
            if result.rider:
                records.append(
                    (
                        result,
                        historical.title,
                        str(historical.local_path),
                        "historical",
                    )
                )
    for doc in repository.list_documents(state="NC", company="progress"):
        lowered = doc.title.lower()
        if not any(token in lowered for token in ("leaf-no-601", "leaf-no-607", "leaf-no-613", "annual billing", "storm")):
            continue
        local_path = Path(str(doc.local_path))
        if not local_path.exists() or not local_path.is_file():
            continue
        result = repository.latest_parse_result(doc.id)
        if result is None:
            text = extract_pdf_text(local_path)
            result = parse_rider_text(
                document_id=doc.id,
                title=doc.title,
                state=doc.state,
                company=doc.company,
                text=text,
                raw_text_path=None,
            )
        if result and result.rider:
            records.append((result, doc.title, str(local_path), "current"))
    return records


def _component_key_for_label(label: str) -> str:
    lowered = label.lower()
    if "summary of rider adjustments" in lowered:
        return "summary_rider_adjustments"
    if "storm recovery charge" in lowered:
        return "storm_recovery_charge"
    if "clean energy rider" in lowered:
        return "clean_energy_rider"
    return lowered.replace(" ", "_")


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date):
        if isinstance(value, datetime):
            return value.date()
        return value
    return parse_effective_date(str(value)) if value else None


def _build_usage_by_local_date(interval_points) -> dict[date, float]:
    usage_by_date: dict[date, float] = defaultdict(float)
    for point in interval_points:
        timestamp = point.timestamp.astimezone(LOCAL_TZ) if point.timestamp.tzinfo else point.timestamp
        usage_by_date[timestamp.date()] += point.kwh
    return dict(usage_by_date)


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


def _latest_snapshot_on_or_before(
    snapshots: list[RiderSnapshot],
    target_date: date | None,
) -> RiderSnapshot | None:
    if target_date is None:
        return snapshots[-1] if snapshots else None
    eligible = [snap for snap in snapshots if snap.effective_date <= target_date]
    return eligible[-1] if eligible else None


def _estimate_component_amount(
    *,
    snapshot: RiderSnapshot | None,
    billed_kwh: float,
    bill_days: int | None,
    total_bill_days: int | None,
) -> float | None:
    if snapshot is None:
        return None
    if snapshot.unit == "cents_per_kwh":
        return round(billed_kwh * snapshot.value / 100.0, 2)
    if snapshot.unit == "fixed_monthly":
        if bill_days and total_bill_days and total_bill_days > 0 and bill_days != total_bill_days:
            return round(snapshot.value * bill_days / total_bill_days, 2)
        return round(snapshot.value, 2)
    return None


def _estimate_component_prorated(
    *,
    snapshots: list[RiderSnapshot],
    start_date: date | None,
    end_date: date | None,
    billed_kwh: float,
    usage_by_date: dict[date, float],
    component_key: str,
) -> tuple[float | None, list[str]]:
    if not snapshots or start_date is None or end_date is None:
        return None, []
    if start_date > end_date:
        return None, []

    boundaries = sorted(
        {
            snap.effective_date
            for snap in snapshots
            if start_date <= snap.effective_date <= end_date
        }
    )
    segment_starts = [start_date] + [boundary for boundary in boundaries if boundary > start_date]
    segment_dates_used: list[str] = []
    total = 0.0

    for idx, segment_start in enumerate(segment_starts):
        segment_end = (
            segment_starts[idx + 1] - timedelta(days=1)
            if idx + 1 < len(segment_starts)
            else end_date
        )
        snapshot = _latest_snapshot_on_or_before(snapshots, segment_end)
        if snapshot is None:
            continue
        segment_dates_used.append(snapshot.effective_date.isoformat())
        if snapshot.unit == "cents_per_kwh":
            segment_kwh = round(_sum_usage(usage_by_date, segment_start, segment_end), 3)
            total += segment_kwh * snapshot.value / 100.0
        elif snapshot.unit == "fixed_monthly":
            total_days = (end_date - start_date).days + 1
            segment_days = (segment_end - segment_start).days + 1
            total += snapshot.value * segment_days / total_days
    return round(total, 2), segment_dates_used


def _sum_usage(usage_by_date: dict[date, float], start_date: date, end_date: date) -> float:
    total = 0.0
    current = start_date
    while current <= end_date:
        total += usage_by_date.get(current, 0.0)
        current += timedelta(days=1)
    return total


def _best_single_snapshot_fit(
    *,
    snapshots: list[RiderSnapshot],
    actual_total: float,
    billed_kwh: float,
    bill_days: int | None,
) -> tuple[RiderSnapshot | None, float | None]:
    best_snapshot = None
    best_estimate = None
    best_abs_delta = None
    for snapshot in snapshots:
        estimate = _estimate_component_amount(
            snapshot=snapshot,
            billed_kwh=billed_kwh,
            bill_days=bill_days,
            total_bill_days=bill_days,
        )
        if estimate is None:
            continue
        abs_delta = abs(actual_total - estimate)
        if best_abs_delta is None or abs_delta < best_abs_delta:
            best_abs_delta = abs_delta
            best_snapshot = snapshot
            best_estimate = estimate
    return best_snapshot, best_estimate


def _round_delta(actual: float | None, estimated: float | None) -> float | None:
    if actual is None or estimated is None:
        return None
    return round(actual - estimated, 2)


def _assessment(
    *,
    split_dates: list[str],
    matching_effective_dates: list[str],
    latest_delta: float | None,
    prorated_delta: float | None,
    best_single_snapshot: RiderSnapshot | None,
    latest_snapshot: RiderSnapshot | None,
    component_key: str,
) -> str:
    if split_dates:
        if split_dates == matching_effective_dates:
            return "bill_split_matches_db_effective_dates"
        return "bill_split_not_in_db"
    if latest_delta is not None and abs(latest_delta) <= 0.25:
        return "latest_effective_date_matches_bill"
    if (
        latest_delta is not None
        and prorated_delta is not None
        and abs(prorated_delta) + 0.25 < abs(latest_delta)
    ):
        return "mid_bill_proration_improves_match"
    if (
        best_single_snapshot is not None
        and latest_snapshot is not None
        and best_single_snapshot.effective_date != latest_snapshot.effective_date
    ):
        return "different_effective_date_fits_better"
    if component_key == "clean_energy_rider":
        return "fixed_charge_needs_proration_review"
    return "needs_review"


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get(key) or "")] += 1
    return dict(sorted(counts.items()))
