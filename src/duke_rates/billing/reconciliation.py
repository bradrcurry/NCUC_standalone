from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from duke_rates.billing.calculators import (
    calculate_energy_charge,
    BillLineItem,
    UsageInput,
    prorate_fixed_monthly_amount,
)
from duke_rates.billing.engine import _select_applicable_energy_charges
from duke_rates.billing.engine import BillingEngine
from duke_rates.billing.observations import derive_bill_component_observations
from duke_rates.billing.riders import apply_riders
from duke_rates.historical.observed_components import (
    ProgressNCObservedComponentHistoryService,
)
from duke_rates.historical.tariff_selector import ProgressNCHistoricalTariffSelector
from duke_rates.models.bill import BillLineItem as ActualBillLineItem
from duke_rates.models.bill import BillSection, BillStatementData
from duke_rates.models.bill_observation import BillComponentObservation
from duke_rates.models.bill_reconciliation import BillReconciliation, ReconciledLineItem
from duke_rates.models.document import DocumentKind
from duke_rates.models.parse_result import DocumentParseResult
from duke_rates.parse.html_extract import extract_html_text
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.normalization import parse_effective_date
from duke_rates.parse.rider_parser import parse_rider_text


def derive_billed_kwh(statement: BillStatementData) -> float:
    electric = statement.electric_section
    if not electric:
        return 0.0
    quantities = [
        item.quantity or 0.0
        for item in electric.line_items
        if _canonical_label(item.label) == "energy_charge"
        and item.unit == "kWh"
    ]
    return round(sum(quantities), 3)


class ProgressNCBillReconciliationService:
    def __init__(self, selector: ProgressNCHistoricalTariffSelector):
        self.selector = selector
        self.engine = BillingEngine()

    def reconcile(self, *, bill_id: int, statement: BillStatementData) -> BillReconciliation:
        electric = statement.electric_section
        if electric is None or not electric.rate_code:
            raise ValueError("Parsed bill does not include an electric section with a rate code.")
        service_date = electric.billing_period_end or statement.service_end or statement.bill_date
        if service_date is None:
            raise ValueError("Parsed bill does not include a usable service date.")

        billed_kwh = derive_billed_kwh(statement)
        selection = self.selector.select_schedule(
            schedule_code=electric.rate_code,
            service_date=service_date,
        )
        extra_riders = self._load_additional_rider_parse_results(
            statement=statement,
            service_date=service_date,
        )
        actual_labels = {
            _canonical_label(item.label)
            for item in (electric.line_items or [])
        }
        rider_parse_results = _filter_rider_parse_results_for_actual_labels(
            _dedupe_rider_parse_results(extra_riders),
            actual_labels=actual_labels,
        )
        usage_input = UsageInput(
            monthly_kwh=billed_kwh,
            service_date=service_date,
            billing_period_start=statement.service_start or electric.billing_period_start,
            billing_period_end=statement.service_end or electric.billing_period_end,
        )
        split_energy_items = _build_split_energy_estimated_items_from_bill_quantities(
            selector=self.selector,
            statement=statement,
            schedule=selection.schedule,
            actual_items=electric.line_items,
        )
        tou_energy_items = _build_tou_estimated_items_from_bill_quantities(
            schedule=selection.schedule,
            actual_items=electric.line_items,
        )
        if tou_energy_items or split_energy_items:
            base_estimate = self.engine.estimate(
                selection.schedule,
                usage_input,
                rider_parse_results=[],
            )
            retained_items = [
                item
                for item in base_estimate.line_items
                if _canonical_label(item.label) != "energy_charge"
            ]
            energy_items = tou_energy_items or split_energy_items
            base_line_items = retained_items + energy_items
            subtotal = round(sum(item.amount for item in base_line_items), 2)
            energy_charge_amount = round(
                sum(
                    item.amount
                    for item in base_line_items
                    if "charge" in item.label.lower() and "customer" not in item.label.lower()
                ),
                2,
            )
            rider_result = apply_riders(
                subtotal,
                [r.title for r in selection.schedule.riders],
                monthly_kwh=billed_kwh,
                schedule_code=selection.schedule.schedule_code,
                energy_charge_amount=energy_charge_amount,
                rider_parse_results=rider_parse_results,
            )
            estimate = base_estimate.model_copy(
                update={
                    "line_items": base_line_items + rider_result["line_items"],
                    "subtotal": subtotal,
                    "total": round(subtotal + rider_result["adjustment"], 2),
                    "notes": list(base_estimate.notes)
                    + [rider_result["note"]]
                    + [
                        "Used billed TOU period quantities to validate parsed TOU energy rates."
                        if tou_energy_items
                        else "Used billed split energy rows to validate tariff changes within the billing period."
                    ],
                }
            )
        else:
            estimate = self.engine.estimate(
                selection.schedule,
                usage_input,
                rider_parse_results=rider_parse_results,
            )
        observed_fallback_items, observed_notes = self._build_observed_fallback_line_items(
            bill_id=bill_id,
            statement=statement,
            billed_kwh=billed_kwh,
            estimated_items=estimate.line_items,
        )
        estimated_items = _apply_estimated_overrides(
            estimate.line_items,
            observed_fallback_items,
        )
        estimated_total = round(sum(item.amount for item in estimated_items), 2)
        estimate = estimate.model_copy(
            update={
                "line_items": estimated_items,
                "total": estimated_total,
                "notes": list(estimate.notes) + observed_notes,
            }
        )
        line_items = _reconcile_line_items(electric, estimate.line_items)
        actual_total = electric.total_current_charges
        total_delta = (
            round(actual_total - estimated_total, 2)
            if actual_total is not None
            else None
        )
        supported_keys = {item.key for item in line_items if item.estimated_amount is not None}
        unsupported_actual = sorted(
            {
                item.label
                for item in line_items
                if item.status == "actual_only"
            }
        )
        unsupported_estimated = sorted(
            {
                item.label
                for item in line_items
                if item.status == "estimated_only"
            }
        )
        notes = list(estimate.notes)
        if actual_total is not None:
            notes.append(
                "Electric total delta = "
                f"actual {actual_total:.2f} - estimated {estimated_total:.2f}."
            )
        if billed_kwh <= 0:
            notes.append("No billed kWh could be derived from Energy Charge rows.")
        if unsupported_actual:
            notes.append("Actual bill includes components the engine does not yet model directly.")
        if "summary_rider_adjustments" not in supported_keys and any(
            _canonical_label(item.label) == "summary_rider_adjustments"
            for item in electric.line_items
        ):
            notes.append(
                "Summary of Rider Adjustments is present on the bill, but current estimation "
                "still decomposes only limited rider tables such as historical BA."
            )

        return BillReconciliation(
            bill_id=bill_id,
            source_path=statement.source_path,
            service_date=service_date,
            rate_code=electric.rate_code,
            billed_kwh=billed_kwh,
            actual_electric_total=actual_total,
            estimated_electric_total=estimated_total,
            total_delta=total_delta,
            selected_tariff=selection,
            estimate=estimate,
            line_items=line_items,
            unsupported_actual_labels=unsupported_actual,
            unsupported_estimated_labels=unsupported_estimated,
            notes=notes,
        )

    def _build_observed_fallback_line_items(
        self,
        *,
        bill_id: int,
        statement: BillStatementData,
        billed_kwh: float,
        estimated_items: list[BillLineItem],
    ) -> tuple[list[BillLineItem], list[str]]:
        electric = statement.electric_section
        if (
            electric is None
            or electric.billing_period_start is None
            or electric.billing_period_end is None
        ):
            return [], []

        actual_by_key = _actual_items_by_key(electric.line_items)
        estimated_keys = {_canonical_label(item.label) for item in estimated_items}
        observations = self.selector.repository.list_bill_component_observations()
        history = ProgressNCObservedComponentHistoryService(observations)
        same_bill_observations = {
            (item.component_key, item.period_start, item.period_end): item
            for item in derive_bill_component_observations(bill_id=bill_id, statement=statement)
            if item.section_name == "Electric"
        }

        fallback_items: list[BillLineItem] = []
        notes: list[str] = []
        supported_components = {
            "clean_energy_rider",
            "storm_recovery_charge",
            "summary_rider_adjustments",
        }
        total_days = _days_inclusive(electric.billing_period_start, electric.billing_period_end)

        for component_key in supported_components:
            if component_key not in actual_by_key:
                continue
            if component_key not in estimated_keys or _should_force_observed_component_override(
                component_key=component_key,
                actual_items=actual_by_key[component_key],
            ):
                targets = _observed_targets_for_component(
                    component_key=component_key,
                    actual_items=actual_by_key[component_key],
                    section=electric,
                )
                component_items: list[BillLineItem] = []
                used_same_bill = False
                for target_start, target_end in targets:
                    entry = history.select_entry(
                        component_key=component_key,
                        rate_code=electric.rate_code,
                        target_start=target_start,
                        target_end=target_end,
                        exclude_bill_id=bill_id,
                    )
                    if entry is None:
                        same_bill = same_bill_observations.get(
                            (component_key, target_start, target_end)
                        )
                        if same_bill is None and len(targets) == 1:
                            same_bill = same_bill_observations.get((component_key, None, None))
                        if same_bill is not None:
                            item = _fallback_line_item_from_same_bill_observation(
                                component_key=component_key,
                                observation=same_bill,
                                billed_kwh=billed_kwh,
                                total_days=total_days,
                                target_start=target_start,
                                target_end=target_end,
                            )
                            if item is not None:
                                component_items.append(item)
                                used_same_bill = True
                        continue
                    item = _fallback_line_item_from_history_entry(
                        component_key=component_key,
                        entry=entry,
                        billed_kwh=billed_kwh,
                        total_days=total_days,
                        target_start=target_start,
                        target_end=target_end,
                    )
                    if item is not None:
                        component_items.append(item)
                if component_items:
                    fallback_items.extend(component_items)
                    note = (
                        f"Applied observed-history fallback for {component_key.replace('_', ' ')}."
                    )
                    if used_same_bill:
                        note += (
                            " Some values were taken from the current bill's "
                            "parsed component evidence."
                        )
                    notes.append(note)
        return fallback_items, notes

    def _load_additional_rider_parse_results(
        self,
        *,
        statement: BillStatementData,
        service_date,
    ) -> list[DocumentParseResult]:
        actual_labels = {
            _canonical_label(item.label)
            for item in (
                statement.electric_section.line_items if statement.electric_section else []
            )
        }
        parse_results: list[DocumentParseResult] = []
        for doc in self.selector.repository.list_documents(state="NC", company="progress"):
            lowered_title = doc.title.lower()
            if (
                "summary of rider adjustments" in lowered_title
                and "summary_rider_adjustments" in actual_labels
            ):
                result = self._parse_current_rider_document(doc.id)
                if result and _is_effective_for_service_date(result, service_date):
                    parse_results.append(result)
            elif (
                "annual-billing-adjustments" in lowered_title
                or "annual billing adjustments" in lowered_title
            ) and "clean_energy_rider" in actual_labels:
                result = self._parse_current_rider_document(doc.id)
                if result and _is_effective_for_service_date(result, service_date):
                    parse_results.append(result)
            elif (
                ("storm securitization rider sts" in lowered_title or "storm recovery rider" in lowered_title)
                and "storm_recovery_charge" in actual_labels
            ):
                result = self._parse_current_rider_document(doc.id)
                if result and _is_effective_for_service_date(result, service_date):
                    parse_results.append(result)
            elif (
                "energy conservation discount" in lowered_title
                and "energy_conservation_credit" in actual_labels
            ):
                result = self._parse_current_rider_document(doc.id)
                if result and _is_effective_for_service_date(result, service_date):
                    parse_results.append(result)
        if "summary_rider_adjustments" in actual_labels:
            for historical in self.selector.repository.list_historical_documents(
                state="NC",
                company="progress",
            ):
                lowered_title = (historical.title or "").lower()
                if (
                    "summary of rider adjustments" not in lowered_title
                    or not historical.parsed_result_json
                ):
                    continue
                result = DocumentParseResult.model_validate_json(historical.parsed_result_json)
                if _is_effective_for_service_date(result, service_date):
                    parse_results.append(result)
        if "storm_recovery_charge" in actual_labels:
            for historical in self.selector.repository.list_historical_documents(
                state="NC",
                company="progress",
            ):
                lowered_title = (historical.title or "").lower()
                if not historical.parsed_result_json:
                    continue
                if "storm" not in lowered_title and "sts" not in lowered_title:
                    continue
                result = DocumentParseResult.model_validate_json(historical.parsed_result_json)
                if result.rider and _is_effective_for_service_date(result, service_date):
                    parse_results.append(result)
        deduped_map: dict[tuple[str, str | None, tuple[str, ...]], DocumentParseResult] = {}
        for result in parse_results:
            rider = result.rider
            if not rider:
                continue
            key = (
                rider.title or "",
                rider.effective_date,
                tuple(component.bill_label for component in rider.charge_components),
            )
            existing = deduped_map.get(key)
            if existing is None or _parse_result_richness(result) > _parse_result_richness(existing):
                deduped_map[key] = result
        deduped = list(deduped_map.values())
        summary_results = [
            result
            for result in deduped
            if result.rider
            and "summary of rider adjustments" in (result.rider.title or "").lower()
        ]
        if summary_results:
            latest_summary = max(
                summary_results,
                key=lambda result: (
                    parse_effective_date(result.rider.effective_date) or service_date,
                    result.document_id,
                ),
            )
            deduped = [
                result
                for result in deduped
                if result is latest_summary
                or not (
                    result.rider
                    and "summary of rider adjustments" in (result.rider.title or "").lower()
                )
            ]
        return deduped

    def _parse_current_rider_document(self, document_id: int) -> DocumentParseResult | None:
        document = self.selector.repository.get_document(document_id)
        if not document:
            return None
        existing = self.selector.repository.latest_parse_result(document_id)
        if existing and existing.rider:
            return existing
        local_path = Path(str(document.local_path))
        if not local_path.exists() or not local_path.is_file():
            return None
        text = _extract_document_text(local_path, document.kind)
        return parse_rider_text(
            document_id=document.id,
            title=document.title,
            state=document.state,
            company=document.company,
            text=text,
            raw_text_path=None,
        )


def _reconcile_line_items(
    actual_section: BillSection,
    estimated_items,
) -> list[ReconciledLineItem]:
    actual_totals = _aggregate_actual_line_items(actual_section.line_items)
    estimated_totals: dict[str, dict[str, object]] = defaultdict(
        lambda: {"amount": 0.0, "labels": []}
    )
    for item in estimated_items:
        key = _canonical_label(item.label)
        bucket = estimated_totals[key]
        bucket["amount"] = round(float(bucket["amount"]) + item.amount, 2)
        bucket["labels"].append(item.label)

    line_items: list[ReconciledLineItem] = []
    for key in sorted(set(actual_totals) | set(estimated_totals)):
        actual = actual_totals.get(key)
        estimated = estimated_totals.get(key)
        actual_amount = actual["amount"] if actual else None
        estimated_amount = estimated["amount"] if estimated else None
        if actual and estimated:
            status = "matched"
        elif actual:
            status = "actual_only"
        else:
            status = "estimated_only"
        delta = None
        if actual_amount is not None and estimated_amount is not None:
            delta = round(actual_amount - estimated_amount, 2)
        label = (
            actual["labels"][0]
            if actual
            else estimated["labels"][0]
            if estimated
            else key
        )
        line_items.append(
            ReconciledLineItem(
                key=key,
                label=label,
                actual_amount=actual_amount,
                estimated_amount=estimated_amount,
                delta=delta,
                status=status,
                actual_labels=list(actual["labels"]) if actual else [],
                estimated_labels=list(estimated["labels"]) if estimated else [],
            )
        )
    return line_items


def _aggregate_actual_line_items(
    items: list[ActualBillLineItem],
) -> dict[str, dict[str, object]]:
    grouped: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "amount": 0.0,
            "labels": [],
            "subperiod_amount": 0.0,
            "has_base": False,
        }
    )
    for item in items:
        key = _canonical_label(item.label)
        bucket = grouped[key]
        bucket["labels"].append(item.label)
        if item.is_subperiod_detail:
            bucket["subperiod_amount"] = round(
                float(bucket["subperiod_amount"]) + (item.amount or 0.0),
                2,
            )
        else:
            bucket["amount"] = round(float(bucket["amount"]) + (item.amount or 0.0), 2)
            bucket["has_base"] = True

    result: dict[str, dict[str, object]] = {}
    for key, bucket in grouped.items():
        amount = bucket["amount"] if bucket["has_base"] else bucket["subperiod_amount"]
        if bucket["has_base"] and bucket["subperiod_amount"]:
            amount = round(float(bucket["amount"]) + float(bucket["subperiod_amount"]), 2)
        result[key] = {
            "amount": round(float(amount), 2),
            "labels": _unique_preserve_order(bucket["labels"]),
        }
    return result


def _actual_items_by_key(items: list[ActualBillLineItem]) -> dict[str, list[ActualBillLineItem]]:
    grouped: dict[str, list[ActualBillLineItem]] = defaultdict(list)
    for item in items:
        grouped[_canonical_label(item.label)].append(item)
    return grouped


def _observed_targets_for_component(
    *,
    component_key: str,
    actual_items: list[ActualBillLineItem],
    section: BillSection,
) -> list[tuple[object, object]]:
    if component_key in {"clean_energy_rider", "storm_recovery_charge"}:
        detailed = [
            item
            for item in actual_items
            if item.is_subperiod_detail and item.period_start and item.period_end
        ]
        if detailed:
            return [(item.period_start, item.period_end) for item in detailed]
    return [(section.billing_period_start, section.billing_period_end)]


def _apply_estimated_overrides(
    estimated_items: list[BillLineItem],
    fallback_items: list[BillLineItem],
) -> list[BillLineItem]:
    if not fallback_items:
        return estimated_items
    override_keys = {_canonical_label(item.label) for item in fallback_items}
    retained = [
        item
        for item in estimated_items
        if _canonical_label(item.label) not in override_keys
    ]
    return retained + fallback_items


def _fallback_line_item_from_history_entry(
    *,
    component_key: str,
    entry,
    billed_kwh: float,
    total_days: int | None,
    target_start,
    target_end,
) -> BillLineItem | None:
    if entry.normalized_unit == "fixed_monthly":
        amount = prorate_fixed_monthly_amount(
            entry.normalized_value,
            billing_period_start=target_start,
            billing_period_end=target_end,
        )
        target_days = _days_inclusive(target_start, target_end)
        if total_days and target_days and target_days != total_days:
            amount = round(entry.normalized_value * target_days / total_days, 2)
        return BillLineItem(
            label=_label_for_component(component_key),
            amount=round(amount, 2),
            details=f"observed history {entry.normalized_value:.3f} per month",
        )
    if entry.normalized_unit == "cents_per_kwh":
        target_kwh = billed_kwh
        target_days = _days_inclusive(target_start, target_end)
        if total_days and target_days and target_days != total_days:
            target_kwh = round(billed_kwh * target_days / total_days, 3)
        return BillLineItem(
            label=_label_for_component(component_key),
            amount=round(target_kwh * entry.normalized_value / 100.0, 2),
            details=f"observed history {entry.normalized_value:.3f} cents/kWh",
        )
    return None


def _fallback_line_item_from_same_bill_observation(
    *,
    component_key: str,
    observation: BillComponentObservation,
    billed_kwh: float,
    total_days: int | None,
    target_start,
    target_end,
) -> BillLineItem | None:
    if observation.inferred_unit is None or observation.inferred_value is None:
        return None
    normalized_unit = observation.inferred_unit
    normalized_value = observation.inferred_value
    if normalized_unit == "dollars_per_kwh" and component_key != "energy_charge":
        normalized_unit = "cents_per_kwh"
        normalized_value = round(normalized_value * 100.0, 3)
    if normalized_unit == "fixed_monthly":
        amount = prorate_fixed_monthly_amount(
            normalized_value,
            billing_period_start=target_start,
            billing_period_end=target_end,
        )
        target_days = _days_inclusive(target_start, target_end)
        if total_days and target_days and target_days != total_days:
            amount = round(normalized_value * target_days / total_days, 2)
        return BillLineItem(
            label=_label_for_component(component_key),
            amount=round(amount, 2),
            details="current bill observed fallback",
        )
    if normalized_unit == "cents_per_kwh":
        target_kwh = observation.quantity_basis_kwh or billed_kwh
        target_days = _days_inclusive(target_start, target_end)
        if (
            observation.quantity_basis_kwh is None
            and total_days
            and target_days
            and target_days != total_days
        ):
            target_kwh = round(billed_kwh * target_days / total_days, 3)
        return BillLineItem(
            label=_label_for_component(component_key),
            amount=round(target_kwh * normalized_value / 100.0, 2),
            details="current bill observed fallback",
        )
    return None


def _label_for_component(component_key: str) -> str:
    return {
        "clean_energy_rider": "Clean Energy Rider",
        "storm_recovery_charge": "Storm Recovery Charge",
        "summary_rider_adjustments": "Summary of Rider Adjustments",
    }.get(component_key, component_key.replace("_", " ").title())


def _days_inclusive(start, end) -> int | None:
    if start is None or end is None:
        return None
    return (end - start).days + 1


def _canonical_label(label: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    if "customer charge" in normalized:
        return "customer_charge"
    if "energy charge" in normalized or "kilowatt hour charge" in normalized:
        return "energy_charge"
    if "clean energy rider" in normalized:
        return "clean_energy_rider"
    if "energy conservation credit" in normalized:
        return "energy_conservation_credit"
    if "storm recovery charge" in normalized:
        return "storm_recovery_charge"
    if "summary of rider adjustments" in normalized:
        return "summary_rider_adjustments"
    if "annual billing adjustments" in normalized:
        return "annual_billing_adjustments"
    if "sales tax" in normalized:
        return "sales_tax"
    return normalized.replace(" ", "_")


def _unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _extract_document_text(path: Path, kind: str) -> str:
    if kind == DocumentKind.PDF.value:
        return extract_pdf_text(path)
    return extract_html_text(path)


def _dedupe_rider_parse_results(
    results: list[DocumentParseResult],
) -> list[DocumentParseResult]:
    deduped_by_key: dict[
        tuple[str, str, tuple[tuple[str, float, str], ...]],
        DocumentParseResult,
    ] = {}
    for result in results:
        rider = result.rider
        if not rider:
            continue
        component_key = tuple(
            sorted(
                (
                    component.bill_label,
                    round(component.value, 6),
                    component.unit,
                )
                for component in rider.charge_components
            )
        )
        key = (
            rider.code or "",
            rider.effective_date or "",
            component_key,
        )
        existing = deduped_by_key.get(key)
        if existing is None or _parse_result_richness(result) > _parse_result_richness(existing):
            deduped_by_key[key] = result

    deduped = list(deduped_by_key.values())

    return deduped


def _build_tou_estimated_items_from_bill_quantities(
    *,
    schedule,
    actual_items: list[ActualBillLineItem],
) -> list[BillLineItem]:
    period_quantities: dict[str, float] = {}
    for item in actual_items:
        if item.quantity is None or item.unit != "kWh":
            continue
        label = (item.label or "").lower()
        if "critical peak" in label:
            period_quantities["Critical Peak"] = item.quantity
        elif "on-peak" in label:
            period_quantities["On-Peak"] = item.quantity
        elif "off-peak" in label:
            period_quantities["Off-Peak"] = item.quantity
        elif "discount" in label:
            period_quantities["Discount"] = item.quantity
    if not period_quantities:
        return []

    line_items: list[BillLineItem] = []
    for charge in schedule.energy_charges:
        if not charge.period or charge.rate is None:
            continue
        quantity = period_quantities.get(charge.period)
        if quantity is None:
            continue
        line_items.append(
            BillLineItem(
                label=charge.label,
                amount=round(quantity * charge.rate, 2),
                details=f"{round(quantity, 3)} kWh @ {charge.rate}",
            )
        )
    return line_items


def _build_split_energy_estimated_items_from_bill_quantities(
    *,
    selector: ProgressNCHistoricalTariffSelector,
    statement: BillStatementData,
    schedule,
    actual_items: list[ActualBillLineItem],
) -> list[BillLineItem]:
    if schedule.tou_periods:
        return []
    energy_items = [
        item
        for item in actual_items
        if _canonical_label(item.label) == "energy_charge"
        and item.quantity is not None
        and item.unit == "kWh"
    ]
    if len(energy_items) <= 1 or not any(item.is_subperiod_detail for item in energy_items):
        return []

    ordered = sorted(
        energy_items,
        key=lambda item: (
            item.period_start or statement.service_start or statement.bill_date,
            item.period_end or statement.service_end or statement.bill_date,
            0 if item.is_subperiod_detail else 1,
        ),
    )
    cumulative_kwh = 0.0
    estimated: list[BillLineItem] = []
    for item in ordered:
        quantity = item.quantity or 0.0
        service_date = item.period_end or statement.service_end or statement.bill_date
        if service_date is None:
            return []
        selection = selector.select_schedule(
            schedule_code=statement.electric_section.rate_code or schedule.schedule_code,
            service_date=service_date,
        )
        charges = _select_applicable_energy_charges(
            selection.schedule.energy_charges,
            service_date,
        )
        estimated.extend(
            _calculate_energy_charge_with_offset(
                charges,
                quantity,
                starting_kwh=cumulative_kwh,
            )
        )
        cumulative_kwh = round(cumulative_kwh + quantity, 6)
    return estimated


def _calculate_energy_charge_with_offset(
    charges,
    quantity: float,
    *,
    starting_kwh: float,
) -> list[BillLineItem]:
    if not charges or quantity <= 0:
        return []
    ordered = sorted(
        charges,
        key=lambda charge: (
            charge.block_from if charge.block_from is not None else 0.0,
            charge.block_to if charge.block_to is not None else float("inf"),
        ),
    )
    has_blocks = any(charge.block_from is not None or charge.block_to is not None for charge in ordered)
    if not has_blocks:
        return calculate_energy_charge(ordered, quantity)

    remaining = quantity
    current_kwh = starting_kwh
    line_items: list[BillLineItem] = []
    for charge in ordered:
        rate = charge.rate or 0.0
        block_start = charge.block_from or 0.0
        block_end = charge.block_to if charge.block_to is not None else float("inf")
        if current_kwh >= block_end:
            continue
        alloc_start = max(current_kwh, block_start)
        capacity = block_end - alloc_start if block_end != float("inf") else remaining
        block_kwh = min(remaining, capacity)
        if block_kwh <= 0:
            continue
        line_items.append(
            BillLineItem(
                label=charge.label,
                amount=round(block_kwh * rate, 2),
                details=f"{round(block_kwh, 3)} {charge.unit} @ {rate}",
            )
        )
        remaining = round(remaining - block_kwh, 6)
        current_kwh = round(current_kwh + block_kwh, 6)
        if remaining <= 0:
            break
    return line_items


def _should_force_observed_component_override(
    *,
    component_key: str,
    actual_items: list[ActualBillLineItem],
) -> bool:
    return component_key == "storm_recovery_charge" and any(
        item.is_subperiod_detail and item.period_start and item.period_end
        for item in actual_items
    )


def _filter_rider_parse_results_for_actual_labels(
    results: list[DocumentParseResult],
    *,
    actual_labels: set[str],
) -> list[DocumentParseResult]:
    filtered: list[DocumentParseResult] = []
    for result in results:
        rider = result.rider
        if not rider:
            continue
        kept_components = [
            component
            for component in rider.charge_components
            if _canonical_label(component.bill_label) in actual_labels
        ]
        kept_adjustment_rows = rider.adjustment_rows
        if rider.code == "BA" and "summary_rider_adjustments" not in actual_labels:
            kept_adjustment_rows = []
        if rider.charge_components and not kept_components and not kept_adjustment_rows:
            continue
        filtered.append(
            result.model_copy(
                update={
                    "rider": rider.model_copy(
                        update={
                            "charge_components": kept_components,
                            "adjustment_rows": kept_adjustment_rows,
                        }
                    ),
                }
            )
        )
    return filtered


def _parse_result_richness(result: DocumentParseResult) -> tuple[int, int]:
    raw_path = result.raw_text_path or ""
    rider = result.rider
    return (
        1 if raw_path else 0,
        len(rider.charge_components) if rider else 0,
    )


def _effective_on_or_before(effective_date: str | None, service_date) -> bool:
    if effective_date is None:
        return True
    from duke_rates.parse.normalization import parse_effective_date

    parsed = parse_effective_date(effective_date)
    if parsed is None:
        return True
    return parsed <= service_date


def _is_effective_for_service_date(result: DocumentParseResult, service_date) -> bool:
    effective_date = result.rider.effective_date if result.rider else None
    return _effective_on_or_before(effective_date, service_date)
