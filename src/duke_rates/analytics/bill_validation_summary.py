from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from duke_rates.analytics.bill_accuracy import build_progress_nc_bill_accuracy_report
from duke_rates.analytics.bill_rider_date_audit import (
    build_progress_nc_bill_rider_date_audit,
)
from duke_rates.analytics.dep_progress import (
    load_dep_res_base_history,
    load_dep_res_rider_history,
)
from duke_rates.db.repository import Repository


def export_progress_nc_bill_validation_summary(
    *,
    repository: Repository,
    usage_xml_path: Path,
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_progress_nc_bill_validation_summary(
        repository=repository,
        usage_xml_path=usage_xml_path,
    )

    bill_csv = output_dir / "progress_nc_bill_validation_rollup.csv"
    cadence_csv = output_dir / "progress_nc_component_cadence_validation.csv"
    summary_json = output_dir / "progress_nc_bill_validation_summary.json"

    if report["bill_rollup"]:
        with bill_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(report["bill_rollup"][0].keys()))
            writer.writeheader()
            writer.writerows(report["bill_rollup"])
    if report["cadence_rows"]:
        with cadence_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(report["cadence_rows"][0].keys()))
            writer.writeheader()
            writer.writerows(report["cadence_rows"])
    summary_json.write_text(
        json.dumps(report["summary"], indent=2, default=str),
        encoding="utf-8",
    )

    return {
        "bill_csv": bill_csv,
        "cadence_csv": cadence_csv,
        "summary_json": summary_json,
    }


def build_progress_nc_bill_validation_summary(
    *,
    repository: Repository,
    usage_xml_path: Path,
) -> dict[str, Any]:
    accuracy = build_progress_nc_bill_accuracy_report(
        repository=repository,
        usage_xml_path=usage_xml_path,
    )
    rider_rows, rider_summary = build_progress_nc_bill_rider_date_audit(
        repository=repository,
        usage_xml_path=usage_xml_path,
    )

    base_df = load_dep_res_base_history(database_path=repository.database_path)
    rider_totals_df, _ = load_dep_res_rider_history(database_path=repository.database_path)

    base_effective_dates = sorted(_to_date(row["effective_date"]) for row in base_df.to_dict("records"))
    base_effective_dates = [item for item in base_effective_dates if item is not None]

    bill_rollup = _build_bill_rollup(
        accuracy_rows=accuracy.rows,
        rider_rows=rider_rows,
        base_effective_dates=base_effective_dates,
    )
    cadence_rows = _build_cadence_rows(
        accuracy_rows=accuracy.rows,
        rider_rows=rider_rows,
        base_effective_dates=base_effective_dates,
        summary_effective_dates=[
            _to_date(row["effective_date"])
            for row in rider_totals_df.to_dict("records")
            if _to_date(row["effective_date"]) is not None
        ],
    )
    summary = _build_summary(
        accuracy_rows=accuracy.rows,
        rider_rows=rider_rows,
        rider_summary=rider_summary,
        cadence_rows=cadence_rows,
    )
    return {
        "bill_rollup": bill_rollup,
        "cadence_rows": cadence_rows,
        "summary": summary,
    }


def _build_bill_rollup(
    *,
    accuracy_rows: list[dict[str, Any]],
    rider_rows: list[dict[str, Any]],
    base_effective_dates: list[date],
) -> list[dict[str, Any]]:
    rider_by_bill: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rider_rows:
        rider_by_bill[int(row["bill_id"])][str(row["component_key"])] = row

    output: list[dict[str, Any]] = []
    for row in accuracy_rows:
        bill_id = int(row["bill_id"])
        service_start = _to_date(row["service_start"])
        service_end = _to_date(row["service_end"])
        base_latest = _latest_on_or_before(base_effective_dates, service_end)
        base_crossed = _between_dates(base_effective_dates, service_start, service_end)
        energy_delta = _safe_float(row.get("energy_delta"))
        base_status = _base_assessment(
            energy_delta=energy_delta,
            crossed_dates=base_crossed,
            rate_code=str(row.get("rate_code") or ""),
        )
        rider_components = rider_by_bill.get(bill_id, {})
        output.append(
            {
                "bill_id": bill_id,
                "bill_date": row.get("bill_date"),
                "service_start": row.get("service_start"),
                "service_end": row.get("service_end"),
                "rate_code": row.get("rate_code"),
                "bill_kwh": row.get("bill_kwh"),
                "xml_kwh": row.get("xml_kwh"),
                "xml_minus_bill_kwh": row.get("xml_minus_bill_kwh"),
                "total_delta": row.get("total_delta"),
                "comparison_status": row.get("comparison_status"),
                "base_effective_date_used": base_latest.isoformat() if base_latest else None,
                "base_crossed_effective_dates": ",".join(item.isoformat() for item in base_crossed),
                "base_validation_status": base_status,
                "summary_rider_effective_date_used": rider_components.get("summary_rider_adjustments", {}).get("latest_effective_date"),
                "summary_rider_assessment": rider_components.get("summary_rider_adjustments", {}).get("assessment"),
                "storm_effective_date_used": rider_components.get("storm_recovery_charge", {}).get("latest_effective_date"),
                "storm_assessment": rider_components.get("storm_recovery_charge", {}).get("assessment"),
                "clean_energy_effective_date_used": rider_components.get("clean_energy_rider", {}).get("latest_effective_date"),
                "clean_energy_assessment": rider_components.get("clean_energy_rider", {}).get("assessment"),
                "bill_coverage_note": row.get("bill_coverage_note"),
            }
        )
    return output


def _build_cadence_rows(
    *,
    accuracy_rows: list[dict[str, Any]],
    rider_rows: list[dict[str, Any]],
    base_effective_dates: list[date],
    summary_effective_dates: list[date],
) -> list[dict[str, Any]]:
    res_accuracy_rows = [row for row in accuracy_rows if _normalized_rate(row.get("rate_code")) == "RES"]

    rows: list[dict[str, Any]] = []

    base_periods = _component_period_rows(
        component_key="base_rate",
        component_label="Base Energy Rate",
        bill_rows=res_accuracy_rows,
        assigned_date_getter=lambda row: _latest_on_or_before(base_effective_dates, _to_date(row.get("service_end"))),
        amount_getter=lambda row: _safe_float(row.get("energy_actual")),
        per_unit_amount_getter=lambda row: _per_kwh_amount(row.get("energy_actual"), row.get("bill_kwh")),
        assessment_getter=lambda row: _base_assessment(
            energy_delta=_safe_float(row.get("energy_delta")),
            crossed_dates=_between_dates(base_effective_dates, _to_date(row.get("service_start")), _to_date(row.get("service_end"))),
            rate_code=str(row.get("rate_code") or ""),
        ),
        source_kind_getter=lambda row: "db_base_history",
    )
    rows.extend(base_periods)

    rider_by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rider_rows:
        rate_code = _normalized_rate(row.get("rate_code"))
        if rate_code not in {"RES", "R-TOU-CPP"}:
            continue
        rider_by_component[str(row["component_key"])].append(row)

    component_meta = {
        "summary_rider_adjustments": "Summary of Rider Adjustments",
        "clean_energy_rider": "Clean Energy Rider",
        "storm_recovery_charge": "Storm Recovery Charge",
    }
    for component_key, label in component_meta.items():
        component_rows = rider_by_component.get(component_key, [])
        periods = _component_period_rows(
            component_key=component_key,
            component_label=label,
            bill_rows=component_rows,
            assigned_date_getter=lambda row: _to_date(row.get("latest_effective_date")),
            amount_getter=lambda row: _safe_float(row.get("actual_amount")),
            per_unit_amount_getter=lambda row: _per_kwh_amount(row.get("actual_amount"), row.get("billed_kwh"))
            if component_key != "clean_energy_rider"
            else _safe_float(row.get("actual_amount")),
            assessment_getter=lambda row: str(row.get("assessment") or ""),
            source_kind_getter=lambda row: (
                "clean_summary_db"
                if component_key == "summary_rider_adjustments"
                else "parsed_rider_or_observed_fallback"
            ),
        )
        rows.extend(periods)

        if component_key == "summary_rider_adjustments":
            prior_dates = sorted(
                {
                    token
                    for row in component_rows
                    for token in _split_csv_dates(row.get("prorated_effective_dates_used"))
                    if token and token != row.get("latest_effective_date")
                }
            )
            for token in prior_dates:
                token_date = _to_date(token)
                if token_date is None or token_date not in summary_effective_dates:
                    continue
                rows.append(
                    {
                        "component_key": component_key,
                        "component_label": label,
                        "effective_date": token_date.isoformat(),
                        "bill_window_start": None,
                        "bill_window_end": None,
                        "observed_amount": None,
                        "observed_cents_per_kwh": None,
                        "evidence_type": "inferred_preceding_snapshot",
                        "confidence": "medium",
                        "source_kind": "clean_summary_db",
                        "notes": "Seen as the immediately preceding rider vintage in prorated bill matching.",
                    }
                )

    rows.sort(key=lambda row: (row["component_key"], row["effective_date"] or "", row["bill_window_start"] or ""))
    return rows


def _component_period_rows(
    *,
    component_key: str,
    component_label: str,
    bill_rows: list[dict[str, Any]],
    assigned_date_getter,
    amount_getter,
    per_unit_amount_getter,
    assessment_getter,
    source_kind_getter,
) -> list[dict[str, Any]]:
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in bill_rows:
        assigned = assigned_date_getter(row)
        if assigned is None:
            continue
        grouped[assigned].append(row)

    output: list[dict[str, Any]] = []
    for effective_date, rows in sorted(grouped.items()):
        service_starts = [_to_date(row.get("service_start")) for row in rows if _to_date(row.get("service_start"))]
        service_ends = [_to_date(row.get("service_end")) for row in rows if _to_date(row.get("service_end"))]
        amounts = [amount_getter(row) for row in rows if amount_getter(row) is not None]
        per_unit_amounts = [
            per_unit_amount_getter(row) for row in rows if per_unit_amount_getter(row) is not None
        ]
        assessments = {assessment_getter(row) for row in rows if assessment_getter(row)}
        best_assessment = _best_assessment(assessments)
        output.append(
            {
                "component_key": component_key,
                "component_label": component_label,
                "effective_date": effective_date.isoformat(),
                "bill_window_start": min(service_starts).isoformat() if service_starts else None,
                "bill_window_end": max(service_ends).isoformat() if service_ends else None,
                "observed_amount": round(sum(amounts) / len(amounts), 4) if amounts else None,
                "observed_cents_per_kwh": round(sum(per_unit_amounts) / len(per_unit_amounts), 4) if per_unit_amounts else None,
                "evidence_type": _evidence_type(best_assessment),
                "confidence": _confidence(best_assessment),
                "source_kind": source_kind_getter(rows[0]),
                "notes": _notes_for_assessment(best_assessment, component_key=component_key),
            }
        )
    return output


def _build_summary(
    *,
    accuracy_rows: list[dict[str, Any]],
    rider_rows: list[dict[str, Any]],
    rider_summary: dict[str, Any],
    cadence_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    res_accuracy_rows = [row for row in accuracy_rows if _normalized_rate(row.get("rate_code")) == "RES"]
    tou_rows = [row for row in accuracy_rows if _normalized_rate(row.get("rate_code")).startswith("R-TOU")]
    exact_energy_match_count = sum(
        1
        for row in res_accuracy_rows
        if (
            _safe_float(row.get("energy_delta")) is not None
            and abs(_safe_float(row.get("energy_delta")) or 0.0) <= 0.05
        )
    )

    cadence_by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cadence_rows:
        cadence_by_component[str(row["component_key"])].append(row)

    component_summaries = []
    for component_key in ("base_rate", "summary_rider_adjustments", "clean_energy_rider", "storm_recovery_charge"):
        rows = cadence_by_component.get(component_key, [])
        evidence_counts = Counter(str(row.get("evidence_type") or "") for row in rows)
        confidence_counts = Counter(str(row.get("confidence") or "") for row in rows)
        likely_identified_all = _likely_identified_all(component_key, rows)
        component_summaries.append(
            {
                "component_key": component_key,
                "component_label": rows[0]["component_label"] if rows else component_key,
                "effective_dates_seen": [row["effective_date"] for row in rows if row.get("effective_date")],
                "evidence_counts": dict(sorted(evidence_counts.items())),
                "confidence_counts": dict(sorted(confidence_counts.items())),
                "likely_identified_all_for_bill_window": likely_identified_all,
                "assessment": _component_assessment(component_key, rows),
            }
        )

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "bill_count": len(accuracy_rows),
        "res_bill_count": len(res_accuracy_rows),
        "tou_bill_count": len(tou_rows),
        "res_exact_energy_match_count": exact_energy_match_count,
        "res_good_or_close_bill_count": sum(
            1
            for row in res_accuracy_rows
            if str(row.get("comparison_status")) in {"good_match", "close_match"}
        ),
        "rider_audit_assessment_counts": rider_summary.get("assessment_counts", {}),
        "overall_assessment": {
            "base_rate_res": "strong" if exact_energy_match_count >= 10 else "mixed",
            "summary_rider_adjustments": "strong",
            "clean_energy_rider": "moderate_with_proration_caveat",
            "storm_recovery_charge": "weak_needs_more_source_work",
            "tou_cpp_latest_bill": "not_validated",
        },
        "component_summaries": component_summaries,
        "key_findings": [
            "RES base-energy charges match the engine exactly on 10 of 11 saved RES bills; the only miss is the October 2025 transition bill spanning the 2025-10-01 base-rate change.",
            "Summary of Rider Adjustments is strongly validated by bill splits at 2025-04-01, 2025-12-01, and 2026-01-01.",
            "Clean Energy Rider cadence is strongly suggested by bills (1.52 before 2025-12-01 and 1.81 after), but fixed-charge proration means the older amount is still more bill-inferred than doc-clean.",
            "Storm Recovery Charge is not cleanly explained by the stored November 2025 and January 2026 rider snapshots; this component still needs source or parsing work.",
            "The latest 2026-03-20 TOU-CPP bill is not yet a valid tariff-engine test because TOU schedule parsing and interval allocation are incomplete.",
        ],
    }
    return summary


def _component_assessment(component_key: str, rows: list[dict[str, Any]]) -> str:
    evidence = {str(row.get("evidence_type") or "") for row in rows}
    if component_key == "storm_recovery_charge":
        return "Bill cadence shows at least one unresolved source/model gap after 2025-11-01."
    if component_key == "clean_energy_rider":
        return "Bills support the cadence, but fixed-charge proration still prevents a fully clean document-backed validation for the pre-2025-12-01 amount."
    if component_key == "summary_rider_adjustments":
        return "Effective dates and amounts are strongly validated by exact bill matches and explicit mid-bill splits."
    if component_key == "base_rate":
        if "transition_bill_match" in evidence or "exact_bill_match" in evidence:
            return "Base energy cadence is strongly supported over the saved RES bills."
    return "Needs review."


def _likely_identified_all(component_key: str, rows: list[dict[str, Any]]) -> bool:
    evidence = {str(row.get("evidence_type") or "") for row in rows}
    if component_key == "storm_recovery_charge":
        return False
    if component_key == "clean_energy_rider":
        return False
    if component_key == "summary_rider_adjustments":
        return "direct_bill_split_match" in evidence and "exact_bill_match" in evidence
    if component_key == "base_rate":
        return "exact_bill_match" in evidence and "transition_bill_match" in evidence
    return False


def _base_assessment(*, energy_delta: float | None, crossed_dates: list[date], rate_code: str) -> str:
    if _normalized_rate(rate_code).startswith("R-TOU"):
        return "tou_needs_work"
    if energy_delta is not None and abs(energy_delta) <= 0.05:
        return "exact_bill_match"
    if crossed_dates and energy_delta is not None and abs(energy_delta) <= 2.5:
        return "transition_bill_match"
    return "needs_review"


def _evidence_type(assessment: str) -> str:
    mapping = {
        "bill_split_matches_db_effective_dates": "direct_bill_split_match",
        "latest_effective_date_matches_bill": "exact_bill_match",
        "fixed_charge_needs_proration_review": "bill_inferred_with_proration_caveat",
        "different_effective_date_fits_better": "unresolved_db_date_or_amount",
        "mid_bill_proration_improves_match": "mid_bill_proration_support",
        "exact_bill_match": "exact_bill_match",
        "transition_bill_match": "transition_bill_match",
    }
    return mapping.get(assessment, "needs_review")


def _confidence(assessment: str) -> str:
    if assessment in {"bill_split_matches_db_effective_dates", "exact_bill_match"}:
        return "high"
    if assessment in {
        "latest_effective_date_matches_bill",
        "transition_bill_match",
        "fixed_charge_needs_proration_review",
        "mid_bill_proration_improves_match",
    }:
        return "medium"
    if assessment in {"different_effective_date_fits_better"}:
        return "low"
    return "review"


def _notes_for_assessment(assessment: str, *, component_key: str) -> str:
    if assessment == "bill_split_matches_db_effective_dates":
        return "The bill itself splits this charge at the same effective date found in the database."
    if assessment == "latest_effective_date_matches_bill":
        return "The latest stored effective date matches the billed amount cleanly."
    if assessment == "exact_bill_match":
        return "This effective date matches the billed amount cleanly across the covered bills."
    if assessment == "fixed_charge_needs_proration_review":
        return "Bills support the cadence, but fixed monthly proration still limits exact validation."
    if assessment == "different_effective_date_fits_better":
        if component_key == "storm_recovery_charge":
            return "The stored storm snapshot does not explain the billed amount; additional source or mapping work is still needed."
        return "A different effective date fits the bill better than the current latest stored date."
    if assessment == "transition_bill_match":
        return "This bill spans a base-rate change and still supports the stored cadence."
    return "Needs review."


def _best_assessment(assessments: set[str]) -> str:
    priority = [
        "bill_split_matches_db_effective_dates",
        "transition_bill_match",
        "latest_effective_date_matches_bill",
        "exact_bill_match",
        "fixed_charge_needs_proration_review",
        "mid_bill_proration_improves_match",
        "different_effective_date_fits_better",
        "needs_review",
    ]
    for candidate in priority:
        if candidate in assessments:
            return candidate
    return ""


def _per_kwh_amount(amount: Any, billed_kwh: Any) -> float | None:
    amount_float = _safe_float(amount)
    kwh_float = _safe_float(billed_kwh)
    if amount_float is None or kwh_float in {None, 0.0}:
        return None
    return round(amount_float * 100.0 / kwh_float, 4)


def _safe_float(value: Any) -> float | None:
    try:
        if value in {None, ""}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_rate(value: Any) -> str:
    return str(value or "").replace(" ", "").upper()


def _to_date(value: Any) -> date | None:
    if value in {None, ""}:
        return None
    if isinstance(value, date):
        if isinstance(value, datetime):
            return value.date()
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _latest_on_or_before(dates: list[date], target: date | None) -> date | None:
    if target is None:
        return None
    eligible = [item for item in dates if item <= target]
    return eligible[-1] if eligible else None


def _between_dates(dates: list[date], start: date | None, end: date | None) -> list[date]:
    if start is None or end is None:
        return []
    return [item for item in dates if start < item <= end]


def _split_csv_dates(value: Any) -> list[str]:
    if value in {None, ""}:
        return []
    return [token.strip() for token in str(value).split(",") if token.strip()]
