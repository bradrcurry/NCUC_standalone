from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from duke_rates.billing.calculators import BillLineItem, prorate_fixed_monthly_amount
from duke_rates.models.parse_result import DocumentParseResult
from duke_rates.parse.normalization import parse_effective_date

log = logging.getLogger(__name__)

# Known storm leaf numbers and their canonical bucket names.
_STORM_LEAF_BUCKETS: dict[str, str] = {
    "607": "leaf_607",
    "613": "leaf_613",
}

# Pattern to extract a leaf number from a free-text path or title string.
# Matches "leaf no. 607", "leaf-no-607", "leaf607", "leaf 607", etc.
_LEAF_NO_RE = re.compile(r"leaf[\s\-\.]*no[\s\-\.]*(\d+)|leaf[\s\-]+(\d+)", re.I)


def apply_riders(
    subtotal: float,
    rider_names: list[str],
    *,
    monthly_kwh: float = 0.0,
    schedule_code: str | None = None,
    energy_charge_amount: float = 0.0,
    billing_period_start: date | None = None,
    billing_period_end: date | None = None,
    rider_parse_results: list[DocumentParseResult] | None = None,
) -> dict:
    line_items: list[BillLineItem] = []
    adjustment = 0.0
    applied_names: list[str] = []
    has_direct_summary = any(
        parse_result.rider
        and "summary of rider adjustments" in (parse_result.rider.title or "").lower()
        for parse_result in rider_parse_results or []
    )

    component_items, used_storm_proration = _build_component_line_items(
        rider_parse_results=rider_parse_results or [],
        monthly_kwh=monthly_kwh,
        energy_charge_amount=energy_charge_amount,
        billing_period_start=billing_period_start,
        billing_period_end=billing_period_end,
        has_direct_summary=has_direct_summary,
    )
    for item in component_items:
        adjustment += item.amount
        applied_names.append(item.details or item.label)
        line_items.append(item)

    for parse_result in rider_parse_results or []:
        rider = parse_result.rider
        if not rider or rider.code != "BA" or not rider.adjustment_rows:
            continue
        if has_direct_summary:
            continue
        if any(
            component.bill_label == "Summary of Rider Adjustments"
            for component in rider.charge_components
        ):
            continue
        normalized_schedule_code = (schedule_code or "").upper()
        schedule_family_codes = {
            normalized_schedule_code,
            normalized_schedule_code.split("-", maxsplit=1)[0],
        }
        matched_row = next(
            (
                row
                for row in rider.adjustment_rows
                if normalized_schedule_code
                and schedule_family_codes
                & {code.upper() for code in row.applicable_schedules}
            ),
            None,
        )
        if not matched_row or matched_row.net_adjustment_cents_per_kwh is None:
            continue
        amount = round(monthly_kwh * matched_row.net_adjustment_cents_per_kwh / 100.0, 2)
        adjustment += amount
        applied_names.append(rider.title)
        line_items.append(
            BillLineItem(
                label=f"{rider.title} ({matched_row.rate_class})",
                amount=amount,
                details=(
                    f"{monthly_kwh} kWh @ "
                    f"{matched_row.net_adjustment_cents_per_kwh} cents/kWh"
                ),
            )
        )

    note = "Formula-based rider calculations are not yet implemented."
    if line_items:
        note = "Applied rider adjustments from parsed historical rider tables."
    elif rider_parse_results:
        note = "Only simple parsed rider table adjustments are currently supported."

    return {
        "subtotal_before_riders": subtotal,
        "riders_applied": applied_names or rider_names,
        "adjustment": round(adjustment, 2),
        "line_items": line_items,
        "note": note,
        "used_storm_proration": used_storm_proration,
    }


def _calculate_component_amount(
    unit: str,
    value: float,
    *,
    monthly_kwh: float,
    energy_charge_amount: float,
    billing_period_start: date | None = None,
    billing_period_end: date | None = None,
) -> float | None:
    if unit == "cents_per_kwh":
        return round(monthly_kwh * value / 100.0, 2)
    if unit == "fixed_monthly":
        return prorate_fixed_monthly_amount(
            value,
            billing_period_start=billing_period_start,
            billing_period_end=billing_period_end,
        )
    if unit == "percent_of_energy_charges":
        return round(-(energy_charge_amount * value / 100.0), 2)
    return None


def _component_details(
    unit: str,
    value: float,
    *,
    monthly_kwh: float,
    energy_charge_amount: float,
) -> str:
    if unit == "cents_per_kwh":
        return f"{monthly_kwh} kWh @ {value} cents/kWh"
    if unit == "fixed_monthly":
        return f"fixed monthly charge {value}"
    if unit == "percent_of_energy_charges":
        return f"{value}% of energy charges {energy_charge_amount:.2f}"
    return str(value)


def _build_component_line_items(
    *,
    rider_parse_results: list[DocumentParseResult],
    monthly_kwh: float,
    energy_charge_amount: float,
    billing_period_start: date | None,
    billing_period_end: date | None,
    has_direct_summary: bool,
) -> list[BillLineItem]:
    grouped: dict[tuple[str, str, str], list[tuple[date | None, str, float, str]]] = {}
    for parse_result in rider_parse_results:
        rider = parse_result.rider
        if not rider:
            continue
        effective_date = parse_effective_date(rider.effective_date)
        source_bucket = _component_source_bucket(parse_result)
        for component in rider.charge_components:
            if (
                has_direct_summary
                and component.bill_label == "Summary of Rider Adjustments"
                and "summary of rider adjustments" not in (rider.title or "").lower()
            ):
                continue
            key = (source_bucket, component.bill_label, component.unit)
            grouped.setdefault(key, []).append(
                (
                    effective_date,
                    rider.title,
                    component.value,
                    component.unit,
                )
            )

    line_items: list[BillLineItem] = []
    used_storm_proration = False
    for (_, bill_label, unit), entries in grouped.items():
        amount, detail, prorated = _prorated_component_amount(
            entries=entries,
            unit=unit,
            bill_label=bill_label,
            monthly_kwh=monthly_kwh,
            energy_charge_amount=energy_charge_amount,
            billing_period_start=billing_period_start,
            billing_period_end=billing_period_end,
        )
        if amount is None:
            continue
        if prorated:
            used_storm_proration = True
        line_items.append(
            BillLineItem(
                label=bill_label,
                amount=amount,
                details=detail,
            )
        )
    return line_items, used_storm_proration


def _prorated_component_amount(
    *,
    entries: list[tuple[date | None, str, float, str]],
    unit: str,
    bill_label: str,
    monthly_kwh: float,
    energy_charge_amount: float,
    billing_period_start: date | None,
    billing_period_end: date | None,
) -> tuple[float | None, str, bool]:
    """Compute the dollar amount for one rider component across a billing period.

    Returns a 3-tuple ``(amount, detail_str, used_proration)``.

    Proration note (TD-006)
    -----------------------
    When a Storm Recovery Charge has multiple effective-date segments within a
    single billing period, the kWh allocated to each segment is computed as a
    *linear day-fraction* of ``monthly_kwh``:

        segment_kwh = monthly_kwh * segment_days / total_days

    This is an approximation.  Duke's actual billing uses meter reads at the
    rate-change date; the linear allocation will diverge whenever usage is
    non-uniform across the billing period (e.g., a hot stretch in the first
    half vs. a mild second half).  Empirically the error is small (≤$0.45 on
    validated residential bills), but it is not an identity.

    ``used_proration`` is ``True`` only when the multi-segment path executes
    and at least one segment was applied.  The caller should surface this to
    the user via ``BillEstimate.notes``.
    """
    entries = sorted(entries, key=lambda item: item[0] or date.min)
    if bill_label != "Storm Recovery Charge":
        latest = entries[-1]
        amount = _calculate_component_amount(
            unit,
            latest[2],
            monthly_kwh=monthly_kwh,
            energy_charge_amount=energy_charge_amount,
            billing_period_start=billing_period_start,
            billing_period_end=billing_period_end,
        )
        return amount, f"{latest[1]}:{bill_label}", False
    if not billing_period_start or not billing_period_end:
        latest = entries[-1]
        amount = _calculate_component_amount(
            unit,
            latest[2],
            monthly_kwh=monthly_kwh,
            energy_charge_amount=energy_charge_amount,
            billing_period_start=billing_period_start,
            billing_period_end=billing_period_end,
        )
        return amount, f"{latest[1]}:{bill_label}", False

    total_days = (billing_period_end - billing_period_start).days + 1
    if total_days <= 0:
        return None, bill_label, False

    total = 0.0
    segment_notes: list[str] = []
    for index, (effective_date, rider_title, value, _) in enumerate(entries):
        start = billing_period_start
        if effective_date and effective_date > start:
            start = effective_date
        next_effective = entries[index + 1][0] if index + 1 < len(entries) else None
        end = billing_period_end
        if next_effective:
            end = min(end, next_effective - timedelta(days=1))
        if start > billing_period_end or end < billing_period_start or start > end:
            continue
        segment_days = (end - start).days + 1
        if unit == "cents_per_kwh":
            # Approximation: kWh allocated by linear day-fraction (see docstring)
            segment_kwh = monthly_kwh * segment_days / total_days
            total += segment_kwh * value / 100.0
        elif unit == "fixed_monthly":
            total += value * segment_days / total_days
        elif unit == "percent_of_energy_charges":
            total += -(energy_charge_amount * value / 100.0) * segment_days / total_days
        segment_notes.append(f"{rider_title}:{start.isoformat()}-{end.isoformat()}")

    if not segment_notes:
        latest = entries[-1]
        amount = _calculate_component_amount(
            unit,
            latest[2],
            monthly_kwh=monthly_kwh,
            energy_charge_amount=energy_charge_amount,
            billing_period_start=billing_period_start,
            billing_period_end=billing_period_end,
        )
        return amount, f"{latest[1]}:{bill_label}", False
    return round(total, 2), "; ".join(segment_notes), True


def _component_source_bucket(parse_result: DocumentParseResult) -> str:
    """Return a stable grouping key for this parsed document's rider components.

    Priority
    --------
    1. ``parse_result.leaf_no`` — structured leaf number set by the parser
       (most reliable; not affected by file-naming variations).
    2. Path/title heuristic — regex search of ``raw_text_path`` and rider
       title for a leaf number pattern (fallback; a WARNING is logged when
       this path is taken so operators know the structured field is missing).
    3. Rider code + title — used for non-storm riders with no leaf number.
    """
    rider = parse_result.rider

    # 1. Structured leaf_no field
    if parse_result.leaf_no:
        normalized = parse_result.leaf_no.strip().lstrip("0") or parse_result.leaf_no
        bucket = _STORM_LEAF_BUCKETS.get(normalized)
        if bucket:
            return bucket

    # 2. Path/title heuristic (fallback)
    probe = " ".join(
        part for part in [parse_result.raw_text_path, rider.title if rider else None] if part
    )
    match = _LEAF_NO_RE.search(probe)
    if match:
        leaf_number = (match.group(1) or match.group(2) or "").lstrip("0")
        bucket = _STORM_LEAF_BUCKETS.get(leaf_number)
        if bucket:
            if not parse_result.leaf_no:
                log.warning(
                    "Storm leaf %r identified via path/title heuristic (document_id=%s). "
                    "Set DocumentParseResult.leaf_no to suppress this warning.",
                    leaf_number,
                    parse_result.document_id,
                )
            return bucket

    # 3. Generic bucket by rider code + title
    code = (rider.code if rider else None) or "unknown"
    title = (rider.title if rider else None) or "unknown"
    return f"{code}:{title}"
