"""Phase 4a billing engine: calculates bills from tariff_charges + rider_applicability DB tables.

This engine works directly with the structured TariffChargeRecord objects extracted by
the Phase 3 parsers, rather than the legacy RateScheduleData parse results.

Usage::

    engine = TariffBillingEngine(repository)
    result = engine.calculate(
        family_key="nc-progress-leaf-500",
        usage=BillInput(monthly_kwh=1200, service_date=date(2025, 8, 1)),
    )
    print(result)
"""
from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from duke_rates.db.repository import Repository


# ---------------------------------------------------------------------------
# Input / Output models
# ---------------------------------------------------------------------------


class BillInput(BaseModel):
    """Usage inputs for bill calculation."""

    monthly_kwh: float
    peak_kw: float | None = None
    base_kw: float | None = None
    on_peak_kw: float | None = None
    mid_peak_kw: float | None = None
    off_peak_kw: float | None = None
    kvar_kvar: float | None = None  # reactive power demand (kVAr) for IN HLF tariffs
    service_date: datetime.date | None = None
    # For TOU schedules: on_peak_kwh / off_peak_kwh / discount_kwh / super_off_peak_kwh may be provided
    on_peak_kwh: float | None = None
    off_peak_kwh: float | None = None
    discount_kwh: float | None = None
    super_off_peak_kwh: float | None = None
    shoulder_kwh: float | None = None
    mid_peak_kwh: float | None = None


class BillLineItem(BaseModel):
    """One line on the calculated bill."""

    label: str
    charge_type: str  # fixed | energy_block | tou_energy | demand | adjustment | minimum | credit
    source: str  # family_key of the tariff that produced this line
    rate_value: float
    rate_unit: str
    quantity: float | None = None
    amount: float
    notes: str | None = None


class BillResult(BaseModel):
    """Full calculated bill with line items."""

    family_key: str
    schedule_title: str | None = None
    effective_start: str | None = None
    revision_label: str | None = None
    service_date: datetime.date | None = None
    monthly_kwh: float

    line_items: list[BillLineItem] = Field(default_factory=list)

    base_subtotal: float = 0.0  # before riders
    rider_subtotal: float = 0.0  # net rider adjustments
    total: float = 0.0

    warnings: list[str] = Field(default_factory=list)
    source_confidence: float = 0.0  # min confidence across all charges used
    optional_riders_applied: list[str] = Field(default_factory=list)  # family_keys of non-mandatory riders included


# ---------------------------------------------------------------------------
# Season detection
# ---------------------------------------------------------------------------

_SUMMER_MONTHS = {5, 6, 7, 8, 9}  # May–September
_WINTER_MONTHS = {10, 11, 12, 1, 2, 3, 4}  # October–April


def _season_for_date(d: datetime.date | None) -> str | None:
    """Return 'summer' or 'winter' for a given date, or None if unknown."""
    if d is None:
        return None
    return "summer" if d.month in _SUMMER_MONTHS else "winter"


def _charge_applies(charge, season: str | None, customer_class: str | None = None) -> bool:
    """Return True if a TariffChargeRecord applies given season and customer_class."""
    if charge.season and charge.season != "all_year":
        if season is None:
            return True  # unknown season — include with warning
        if charge.season != season:
            return False
    if customer_class and charge.customer_class:
        if not _class_matches(charge.customer_class, customer_class):
            return False
    return True


# Maps requested_class → set of charge_class values that are acceptable matches.
# When a customer requests "residential", charges tagged "secondary" (low-voltage
# metering) or "three_phase" (service config, not customer type) also apply.
# "primary"/"transmission" are commercial/industrial voltage levels; they match
# general_service requests but not residential.
_CLASS_FALLBACKS: dict[str, set[str]] = {
    "residential": {"secondary", "three_phase"},
    "general_service": {"primary", "secondary", "transmission", "unmetered", "three_phase"},
    "commercial": {"commercial_small", "commercial_medium", "commercial_large", "seasonal"},
    "commercial_small": {"commercial"},
    "commercial_medium": {"commercial"},
    "commercial_large": {"commercial"},
}


def _class_matches(charge_class: str | None, requested_class: str) -> bool:
    if not charge_class or charge_class == "all":
        return True
    if charge_class == requested_class:
        return True
    return charge_class in _CLASS_FALLBACKS.get(requested_class, set())


def _fixed_charges_for_class(charges, requested_class: str):
    exact = [c for c in charges if c.customer_class == requested_class]
    if exact:
        return exact
    generic = [c for c in charges if c.customer_class in (None, "all")]
    return generic if generic else charges


def _demand_quantity_for_charge(charge, usage: BillInput) -> float | None:
    """Return the appropriate demand quantity (kW or kVAr) for a demand charge."""
    unit = (charge.rate_unit or "").lower()
    if "kvar" in unit:
        return usage.kvar_kvar  # may be None → charge will be warned and skipped
    label = (charge.charge_label or "").lower()
    if "mid-peak" in label:
        return usage.mid_peak_kw if usage.mid_peak_kw is not None else usage.peak_kw
    if "on-peak" in label:
        return usage.on_peak_kw if usage.on_peak_kw is not None else usage.peak_kw
    if "off-peak" in label:
        return usage.off_peak_kw if usage.off_peak_kw is not None else usage.peak_kw
    if "base demand" in label:
        return usage.base_kw if usage.base_kw is not None else usage.peak_kw
    return usage.peak_kw


# ---------------------------------------------------------------------------
# Unit normalisation
# ---------------------------------------------------------------------------


def _rate_in_dollars(rate_value: float, rate_unit: str | None) -> float:
    """Return rate_value converted to dollars.

    Rates stored as cents/kWh or cents/kW are divided by 100.
    All other units ($/kWh, $/kW, $/month, etc.) are returned unchanged.
    """
    if rate_unit and "cent" in rate_unit.lower():
        return rate_value / 100.0
    return rate_value


# ---------------------------------------------------------------------------
# Core calculation helpers
# ---------------------------------------------------------------------------


def _calc_fixed(charge, source_key: str) -> BillLineItem:
    return BillLineItem(
        label=charge.charge_label or "Fixed Charge",
        charge_type="fixed",
        source=source_key,
        rate_value=charge.rate_value or 0.0,
        rate_unit=charge.rate_unit or "$/month",
        quantity=1.0,
        amount=round(charge.rate_value or 0.0, 2),
        notes=charge.source_snippet,
    )


def _calc_energy_blocks(charges, monthly_kwh: float, source_key: str) -> list[BillLineItem]:
    """Calculate tiered block energy charges."""
    # Sort by tier_min ascending
    ordered = sorted(charges, key=lambda c: (c.tier_min or 0.0, c.tier_max or float("inf")))
    # has_tiers: at least one charge has a non-None tier_max (open-ended single tier
    # with tier_min=0 and tier_max=None is treated as flat, not tiered)
    has_tiers = any(c.tier_max is not None for c in ordered)
    items: list[BillLineItem] = []

    if not has_tiers:
        # Flat rate (including tier_min=0.0, tier_max=None case)
        c = ordered[0]
        rate = _rate_in_dollars(c.rate_value or 0.0, c.rate_unit)
        amount = round(monthly_kwh * rate, 2)
        items.append(
            BillLineItem(
                label=c.charge_label or "Energy Charge",
                charge_type="energy_block",
                source=source_key,
                rate_value=c.rate_value or 0.0,
                rate_unit=c.rate_unit or "$/kWh",
                quantity=monthly_kwh,
                amount=amount,
                notes=c.source_snippet,
            )
        )
        return items

    remaining = monthly_kwh
    for c in ordered:
        if remaining <= 0:
            break
        tier_start = c.tier_min or 0.0
        tier_end = c.tier_max  # None = unlimited
        # How many kWh fall in this tier
        if tier_end is None:
            block_kwh = max(monthly_kwh - tier_start, 0.0)
        else:
            block_kwh = max(tier_end - tier_start, 0.0)
        block_kwh = min(block_kwh, remaining)
        if block_kwh <= 0:
            continue
        remaining = round(remaining - block_kwh, 6)
        rate = _rate_in_dollars(c.rate_value or 0.0, c.rate_unit)
        amount = round(block_kwh * rate, 2)
        items.append(
            BillLineItem(
                label=c.charge_label or "Energy Charge",
                charge_type="energy_block",
                source=source_key,
                rate_value=c.rate_value or 0.0,
                rate_unit=c.rate_unit or "$/kWh",
                quantity=block_kwh,
                amount=amount,
                notes=c.source_snippet,
            )
        )
    return items


def _calc_tou_energy(charges, usage: BillInput, source_key: str) -> list[BillLineItem]:
    """Calculate TOU energy charges from on_peak_kwh / off_peak_kwh / discount_kwh inputs."""
    period_kwh = {
        "on_peak": usage.on_peak_kwh,
        "off_peak": usage.off_peak_kwh,
        "discount": usage.discount_kwh,
        "super_off_peak": usage.super_off_peak_kwh,
        "shoulder": usage.shoulder_kwh,
        "mid_peak": usage.mid_peak_kwh,
    }
    items: list[BillLineItem] = []
    for c in charges:
        period = c.tou_period
        kwh = period_kwh.get(period or "")
        if kwh is None:
            continue
        rate = _rate_in_dollars(c.rate_value or 0.0, c.rate_unit)
        amount = round(kwh * rate, 2)
        items.append(
            BillLineItem(
                label=c.charge_label or f"Energy Charge - {period}",
                charge_type="tou_energy",
                source=source_key,
                rate_value=c.rate_value or 0.0,
                rate_unit=c.rate_unit or "$/kWh",
                quantity=kwh,
                amount=amount,
                notes=c.source_snippet,
            )
        )
    return items


def _calc_demand(
    charges,
    usage: BillInput,
    source_key: str,
) -> tuple[list[BillLineItem], list[str]]:
    items = []
    warnings: list[str] = []
    # Dedup key includes season so that summer/winter variants at different rates are both applied
    seen_vals: set[tuple[str, float, str | None]] = set()
    for c in charges:
        val = c.rate_value or 0.0
        dedup_key = (c.charge_label or "", val, c.season)
        if dedup_key in seen_vals:
            continue
        seen_vals.add(dedup_key)
        unit = (c.rate_unit or "").lower()
        if "kvar" in unit:
            # Reactive power charge — requires kvar_kvar input
            if usage.kvar_kvar is None:
                warnings.append(
                    f"Reactive power (kVAr) charge '{c.charge_label or 'kVAr Charge'}' on "
                    f"{source_key} requires kvar_kvar input; charge omitted."
                )
                continue
            amount = round(usage.kvar_kvar * val, 2)
            items.append(
                BillLineItem(
                    label=c.charge_label or "Reactive Power Charge",
                    charge_type="demand",
                    source=source_key,
                    rate_value=val,
                    rate_unit=c.rate_unit or "$/kVAr",
                    quantity=usage.kvar_kvar,
                    amount=amount,
                    notes=c.source_snippet,
                )
            )
            continue
        quantity = _demand_quantity_for_charge(c, usage)
        if quantity is None:
            warnings.append(
                f"Demand quantity missing for {c.charge_label or 'Demand Charge'} on {source_key}."
            )
            continue
        amount = round(quantity * val, 2)
        items.append(
            BillLineItem(
                label=c.charge_label or "Demand Charge",
                charge_type="demand",
                source=source_key,
                rate_value=val,
                rate_unit=c.rate_unit or "$/kW",
                quantity=quantity,
                amount=amount,
                notes=c.source_snippet,
            )
        )
    return items, warnings


def _calc_adjustment(
    charges,
    usage: BillInput,
    customer_class: str,
    source_key: str,
) -> tuple[list[BillLineItem], list[str]]:
    """Calculate rider adjustment for the applicable customer class.

    Handles kWh, kW, bill, and monthly rider units.
    """
    items = []
    warnings: list[str] = []
    for c in charges:
        if not _class_matches(c.customer_class, customer_class):
            continue
        rate_val = _rate_in_dollars(c.rate_value or 0.0, c.rate_unit)
        unit = (c.rate_unit or "$/kWh").lower()
        if "bill" in unit or "month" in unit:
            quantity = 1.0
            amount = round(rate_val, 2)
        elif "kw" in unit and "kwh" not in unit:
            quantity = _demand_quantity_for_charge(c, usage)
            if quantity is None:
                warnings.append(
                    "Demand quantity missing for rider "
                    f"{c.charge_label or source_key} on {source_key}."
                )
                continue
            amount = round(quantity * rate_val, 2)
        else:
            quantity = usage.monthly_kwh
            amount = round(quantity * rate_val, 2)
        items.append(
            BillLineItem(
                label=c.charge_label or "Rider Adjustment",
                charge_type="adjustment",
                source=source_key,
                rate_value=c.rate_value or 0.0,
                rate_unit=c.rate_unit or "$/kWh",
                quantity=quantity,
                amount=amount,
                notes=c.source_snippet,
            )
        )
    return items, warnings


def _calc_minimum(
    charges,
    current_subtotal: float,
    source_key: str,
) -> tuple[list[BillLineItem], list[str]]:
    """Apply minimum monthly bill charges.

    If the current subtotal is below the minimum, adds a line item for the shortfall.
    If multiple minimum charges exist (e.g. per-kW minimums), applies all that produce
    a positive shortfall and takes the largest result.
    """
    items: list[BillLineItem] = []
    warnings: list[str] = []
    best_shortfall = 0.0
    best_item: BillLineItem | None = None

    for c in charges:
        min_val = c.rate_value or 0.0
        unit = (c.rate_unit or "").lower()
        # Minimum may be expressed as $/month (flat) or $/kW (demand-based)
        if "kw" in unit and "kwh" not in unit:
            # Per-kW minimum: rate × demand → minimum bill floor
            # We don't have quantity context here, so emit as a note-only warning
            warnings.append(
                f"Per-kW minimum charge '{c.charge_label or 'Minimum Charge'}' on {source_key} "
                "requires peak_kw to evaluate; skipped."
            )
            continue
        shortfall = round(min_val - current_subtotal, 2)
        if shortfall > best_shortfall:
            best_shortfall = shortfall
            best_item = BillLineItem(
                label=c.charge_label or "Minimum Monthly Bill",
                charge_type="minimum",
                source=source_key,
                rate_value=min_val,
                rate_unit=c.rate_unit or "$/month",
                quantity=1.0,
                amount=shortfall,
                notes=c.source_snippet,
            )

    if best_item is not None:
        items.append(best_item)

    return items, warnings


def _calc_credit(charges, usage: BillInput, source_key: str) -> list[BillLineItem]:
    """Calculate credit line items (e.g. FL load management credits).

    Credits are negative amounts; rate_value is stored as a positive number
    and negated here so the total reduces accordingly.
    """
    items: list[BillLineItem] = []
    for c in charges:
        rate_val = c.rate_value or 0.0
        unit = (c.rate_unit or "").lower()
        if "kw" in unit and "kwh" not in unit:
            quantity = _demand_quantity_for_charge(c, usage)
            if quantity is None:
                continue
            amount = round(-abs(quantity * rate_val), 2)
            items.append(
                BillLineItem(
                    label=c.charge_label or "Credit",
                    charge_type="credit",
                    source=source_key,
                    rate_value=rate_val,
                    rate_unit=c.rate_unit or "$/kW",
                    quantity=quantity,
                    amount=amount,
                    notes=c.source_snippet,
                )
            )
        elif "kwh" in unit:
            amount = round(-abs(usage.monthly_kwh * rate_val), 2)
            items.append(
                BillLineItem(
                    label=c.charge_label or "Credit",
                    charge_type="credit",
                    source=source_key,
                    rate_value=rate_val,
                    rate_unit=c.rate_unit or "$/kWh",
                    quantity=usage.monthly_kwh,
                    amount=amount,
                    notes=c.source_snippet,
                )
            )
        else:
            # Flat credit $/month or $/bill
            amount = round(-abs(rate_val), 2)
            items.append(
                BillLineItem(
                    label=c.charge_label or "Credit",
                    charge_type="credit",
                    source=source_key,
                    rate_value=rate_val,
                    rate_unit=c.rate_unit or "$/month",
                    quantity=1.0,
                    amount=amount,
                    notes=c.source_snippet,
                )
            )
    return items


# ---------------------------------------------------------------------------
# Rider total cross-check (TD-V4-001)
# ---------------------------------------------------------------------------

# Family key whose adjustment_total charge rows store authoritative per-kWh sums,
# keyed by {state}-{company}.  Extend this dict when other utilities publish summaries.
_RIDER_SUMMARY_FAMILY: dict[str, str] = {
    "nc-progress": "nc-progress-leaf-600",
}

# Tolerance ($/kWh) — differences smaller than this are ignored.
# 0.0001 $/kWh = 0.01 ¢/kWh which is well below any real discrepancy.
_RIDER_TOTAL_TOLERANCE: float = 0.0001


def _get_rider_summary_total(
    repo: "Repository",
    state: str,
    company: str,
    ref_date: datetime.date,
    customer_class: str = "residential",
) -> float | None:
    """Look up the leaf-600 (or equivalent) authoritative rider total for ref_date.

    Returns the rate in $/kWh, or None if no summary data exists for this
    state/company/date combination.
    """
    key = f"{state}-{company}"
    summary_family_key = _RIDER_SUMMARY_FAMILY.get(key)
    if summary_family_key is None:
        return None

    versions = repo.list_tariff_versions(summary_family_key)
    version = _select_version(versions, ref_date)
    if version is None:
        return None

    charges = repo.list_tariff_charges(version.id)
    totals = [
        c for c in charges
        if c.charge_type == "adjustment_total"
        and _class_matches(c.customer_class, customer_class)
    ]
    if not totals:
        return None

    # If multiple totals exist (shouldn't happen), take the first one
    return _rate_in_dollars(totals[0].rate_value or 0.0, totals[0].rate_unit)


def validate_rider_total(
    repo: "Repository",
    base_family_key: str,
    rider_items: list["BillLineItem"],
    ref_date: datetime.date,
    customer_class: str = "residential",
    summary_rider_keys: set[str] | None = None,
) -> str | None:
    """Compare the engine's summed per-kWh rider rate against the authoritative leaf-600 total.

    Returns a warning string if the rates differ by more than _RIDER_TOTAL_TOLERANCE,
    or None if the check passes or no summary data is available.

    Args:
        repo: Repository instance for DB lookups.
        base_family_key: Family key of the base rate schedule (e.g. 'nc-progress-leaf-500').
        rider_items: Line items produced by _apply_riders() — only $/kWh adjustment items
            are considered for the per-kWh total.
        ref_date: Billing date used to select the correct summary version.
        customer_class: Customer class for filtering.
        summary_rider_keys: Set of rider family_keys whose in_rider_summary=True.
            Only items whose source is in this set are counted in the cross-check sum.
            When None, all $/kWh adjustment items are counted (pre-TD-V4-005 behaviour).
    """
    parts = base_family_key.split("-")
    if len(parts) < 3:
        return None

    expected = _get_rider_summary_total(repo, parts[0], parts[1], ref_date, customer_class)
    if expected is None:
        return None  # No cross-check data available — skip silently

    # Sum only flat $/kWh adjustment items; exclude $/bill, $/kW, %_energy, and
    # direct-bill riders (STS, SSR) whose in_rider_summary=False.
    per_kwh_items = [
        item for item in rider_items
        if item.charge_type == "adjustment"
        and "kwh" in (item.rate_unit or "").lower()
        and (summary_rider_keys is None or item.source in summary_rider_keys)
    ]

    if not per_kwh_items:
        return None

    # Sum rate_values (not amounts) so the check is independent of usage quantity
    engine_total_per_kwh = sum(
        _rate_in_dollars(item.rate_value, item.rate_unit) for item in per_kwh_items
    )

    delta = abs(engine_total_per_kwh - expected)
    if delta > _RIDER_TOTAL_TOLERANCE:
        delta_c = delta * 100
        engine_c = engine_total_per_kwh * 100
        expected_c = expected * 100
        msg = (
            f"Rider total mismatch for {base_family_key} as of {ref_date}: "
            f"engine sums {engine_c:.4f} ¢/kWh but leaf-600 says {expected_c:.4f} ¢/kWh "
            f"(delta {delta_c:.4f} ¢/kWh). "
            "Check rider_applicability links and tariff_charges for this date."
        )
        log.warning(msg)
        return msg

    return None


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------


class TariffBillingEngine:
    """Calculates bills from the tariff_charges and rider_applicability DB tables.

    This is the Phase 4a billing engine. It reads structured charge data
    populated by the Phase 3 parsers.
    """

    def __init__(self, repository: Repository) -> None:
        self._repo = repository

    def calculate(
        self,
        family_key: str,
        usage: BillInput,
        *,
        customer_class: str = "residential",
        include_riders: bool = True,
        extra_riders: list[str] | None = None,
        effective_date: datetime.date | None = None,
    ) -> BillResult:
        """Calculate a bill for the given tariff family and usage inputs.

        Args:
            family_key: Tariff family key (e.g. "nc-progress-leaf-500").
            usage: Usage inputs (kWh, kW, service date, TOU breakdown).
            customer_class: Customer class for rider adjustment matching.
            include_riders: Whether to include mandatory rider adjustments.
            extra_riders: Family keys of optional (non-mandatory) riders to include
                in addition to mandatory riders. E.g. ["nc-progress-leaf-640"] adds
                the RECD energy conservation discount. Included riders are listed in
                BillResult.optional_riders_applied.
            effective_date: Override effective date for version selection.
                Defaults to today if not provided.
        """
        ref_date = effective_date or (usage.service_date or datetime.date.today())
        season = _season_for_date(usage.service_date)
        warnings: list[str] = []
        min_confidence = 1.0

        # --- Get the tariff family ---
        family = self._repo.get_tariff_family(family_key)
        if family is None:
            return BillResult(
                family_key=family_key,
                monthly_kwh=usage.monthly_kwh,
                warnings=[f"Tariff family not found: {family_key}"],
            )

        # --- Get the best tariff version ---
        versions = self._repo.list_tariff_versions(family_key)
        version = _select_version(versions, ref_date)
        if version is None:
            return BillResult(
                family_key=family_key,
                schedule_title=family.title,
                monthly_kwh=usage.monthly_kwh,
                warnings=[f"No tariff version found for {family_key} as of {ref_date}"],
            )

        # --- Get charges for this version ---
        charges = self._repo.list_tariff_charges(version.id)
        if not charges:
            return BillResult(
                family_key=family_key,
                schedule_title=family.title,
                effective_start=version.effective_start,
                revision_label=version.revision_label,
                monthly_kwh=usage.monthly_kwh,
                warnings=[f"No charges found for version {version.id} of {family_key}"],
            )

        line_items: list[BillLineItem] = []

        # Filter charges by season; defer class filtering to type-specific helpers
        # so that fixed charges use _fixed_charges_for_class (exact-then-fallback)
        # while energy/demand/adjustment charges use _class_matches (with fallbacks).
        applicable = [c for c in charges if _charge_applies(c, season, customer_class)]

        if season is None:
            seasonal = [c for c in charges if c.season and c.season != "all_year"]
            if seasonal:
                warnings.append(
                    "Seasonal rate schedule but no service_date provided; "
                    "using all-year charges only. Provide service_date for accurate seasonal rates."
                )

        # Group by charge type
        fixed_charges = [c for c in applicable if c.charge_type == "fixed"]
        energy_charges = [c for c in applicable if c.charge_type == "energy_block"]
        tou_charges = [c for c in applicable if c.charge_type == "tou_energy"]
        demand_charges = [c for c in applicable if c.charge_type == "demand"]
        adjustment_charges = [c for c in applicable if c.charge_type == "adjustment"]
        minimum_charges = [c for c in applicable if c.charge_type == "minimum"]
        credit_charges = [c for c in applicable if c.charge_type == "credit"]

        # --- Fixed charges (take first; skip three-phase surcharges) ---
        primary_fixed = [
            c for c in _fixed_charges_for_class(fixed_charges, customer_class)
            if "Three-Phase" not in (c.charge_label or "")
        ]
        if primary_fixed:
            item = _calc_fixed(primary_fixed[0], family_key)
            line_items.append(item)
            min_confidence = min(min_confidence, primary_fixed[0].confidence_score)

        # --- Energy charges ---
        if tou_charges:
            has_tou_input = any(
                v is not None for v in [
                    usage.on_peak_kwh, usage.off_peak_kwh, usage.discount_kwh,
                    usage.super_off_peak_kwh, usage.shoulder_kwh, usage.mid_peak_kwh,
                ]
            )
            if has_tou_input:
                tou_items = _calc_tou_energy(tou_charges, usage, family_key)
                line_items.extend(tou_items)
                for c in tou_charges:
                    min_confidence = min(min_confidence, c.confidence_score)
                # Warn when supplied TOU kWh don't all match parsed periods
                parsed_periods = {c.tou_period for c in tou_charges if c.tou_period}
                unmatched_kwh = sum(
                    kwh for period, kwh in [
                        ("on_peak", usage.on_peak_kwh),
                        ("off_peak", usage.off_peak_kwh),
                        ("discount", usage.discount_kwh),
                        ("shoulder", usage.shoulder_kwh),
                        ("mid_peak", usage.mid_peak_kwh),
                        ("super_off_peak", usage.super_off_peak_kwh),
                    ]
                    if kwh is not None and period not in parsed_periods
                )
                if unmatched_kwh > 0:
                    warnings.append(
                        f"Partial TOU coverage: {unmatched_kwh:.0f} kWh have no matching "
                        f"period in parsed charges (periods found: "
                        f"{', '.join(sorted(parsed_periods)) or 'none'}). "
                        "Bill total is understated; charge data may be incomplete."
                    )
                    min_confidence = min(min_confidence, 0.5)
            else:
                # No TOU breakdown: fall back to flat-rate estimate using total kWh
                # Use average of on-peak and off-peak rates as an approximation
                avg_rate = sum(c.rate_value or 0.0 for c in tou_charges) / len(tou_charges)
                amount = round(usage.monthly_kwh * avg_rate, 2)
                line_items.append(
                    BillLineItem(
                        label="Energy Charge (TOU avg estimate)",
                        charge_type="tou_energy",
                        source=family_key,
                        rate_value=avg_rate,
                        rate_unit="$/kWh",
                        quantity=usage.monthly_kwh,
                        amount=amount,
                        notes=None,
                    )
                )
                warnings.append(
                    "TOU rate schedule: provide on_peak_kwh/off_peak_kwh for accurate calculation. "
                    "Using average rate as estimate."
                )
        elif energy_charges:
            items = _calc_energy_blocks(energy_charges, usage.monthly_kwh, family_key)
            line_items.extend(items)
            for c in energy_charges:
                min_confidence = min(min_confidence, c.confidence_score)

        # --- Demand charges ---
        if demand_charges:
            items, demand_warnings = _calc_demand(demand_charges, usage, family_key)
            line_items.extend(items)
            demand_values = (
                usage.base_kw,
                usage.on_peak_kw,
                usage.mid_peak_kw,
                usage.off_peak_kw,
            )
            if usage.peak_kw is None and not any(v is not None for v in demand_values):
                warnings.append(
                    "Demand-metered schedule but no peak_kw provided; "
                    "demand charges omitted."
                )
            warnings.extend(demand_warnings)
            for c in demand_charges:
                min_confidence = min(min_confidence, c.confidence_score)

        # --- Embedded adjustment charges (from riders stored on this family) ---
        if adjustment_charges:
            items, adjustment_warnings = _calc_adjustment(
                adjustment_charges, usage, customer_class, family_key
            )
            line_items.extend(items)
            warnings.extend(adjustment_warnings)
            for c in adjustment_charges:
                min_confidence = min(min_confidence, c.confidence_score)

        # --- Credit charges (applied before minimum check) ---
        if credit_charges:
            credit_items = _calc_credit(credit_charges, usage, family_key)
            line_items.extend(credit_items)
            for c in credit_charges:
                min_confidence = min(min_confidence, c.confidence_score)

        base_subtotal = round(sum(item.amount for item in line_items), 2)
        # Energy-only subtotal for percentage-based riders like RECD (5% of kWh charges only)
        energy_subtotal = round(sum(
            item.amount for item in line_items
            if item.charge_type in ("energy_block", "tou_energy")
        ), 2)

        # --- Rider adjustments (from rider_applicability table) ---
        rider_subtotal = 0.0
        optional_riders_applied: list[str] = []
        if include_riders:
            rider_items, rider_warnings, optional_riders_applied = self._apply_riders(
                family_key, usage, customer_class, ref_date,
                extra_riders=extra_riders,
                base_subtotal=base_subtotal,
                energy_subtotal=energy_subtotal,
            )
            line_items.extend(rider_items)
            rider_subtotal = round(sum(item.amount for item in rider_items), 2)
            warnings.extend(rider_warnings)

        subtotal_before_minimum = round(base_subtotal + rider_subtotal, 2)

        # --- Minimum monthly bill (applied after all other charges + riders) ---
        if minimum_charges:
            min_items, min_warnings = _calc_minimum(
                minimum_charges, subtotal_before_minimum, family_key
            )
            line_items.extend(min_items)
            warnings.extend(min_warnings)
            for c in minimum_charges:
                min_confidence = min(min_confidence, c.confidence_score)
            minimum_adjustment = round(sum(item.amount for item in min_items), 2)
        else:
            minimum_adjustment = 0.0

        total = round(subtotal_before_minimum + minimum_adjustment, 2)

        return BillResult(
            family_key=family_key,
            schedule_title=family.title,
            effective_start=version.effective_start,
            revision_label=version.revision_label,
            service_date=usage.service_date,
            monthly_kwh=usage.monthly_kwh,
            line_items=line_items,
            base_subtotal=base_subtotal,
            rider_subtotal=rider_subtotal,
            total=total,
            warnings=warnings,
            source_confidence=round(min_confidence, 3),
            optional_riders_applied=optional_riders_applied,
        )

    def _apply_riders(
        self,
        base_family_key: str,
        usage: BillInput,
        customer_class: str,
        ref_date: datetime.date,
        *,
        extra_riders: list[str] | None = None,
        base_subtotal: float = 0.0,
        energy_subtotal: float = 0.0,
    ) -> tuple[list[BillLineItem], list[str], list[str]]:
        """Look up and apply riders applicable to this base rate schedule.

        Mandatory riders (link.mandatory=True) are always included when present.
        Optional riders (link.mandatory=False) are included only when their
        family_key appears in extra_riders.

        Returns:
            (line_items, warnings, optional_riders_applied)
        """
        items: list[BillLineItem] = []
        warnings: list[str] = []
        optional_applied: list[str] = []
        extra_set: set[str] = set(extra_riders) if extra_riders else set()

        rider_links = self._repo.list_rider_applicability(applies_to_family_key=base_family_key)
        if not rider_links:
            warnings.append(
                f"No rider applicability records for {base_family_key}. "
                "Run parse-tariff-versions to populate rider links."
            )
            return items, warnings, optional_applied

        # Build set of rider family_keys that are part of the leaf-600 Summary of Rider
        # Adjustments (in_rider_summary=True). Direct-bill riders (STS, SSR) are excluded.
        summary_rider_keys: set[str] = {
            link.rider_family_key for link in rider_links if link.in_rider_summary
        }

        not_found: list[str] = []

        for link in rider_links:
            is_optional = not link.mandatory
            if is_optional and link.rider_family_key not in extra_set:
                continue
            # Skip riders not yet in effect or already expired as of ref_date
            if link.effective_start and str(ref_date) < link.effective_start:
                continue
            if link.effective_end and str(ref_date) > link.effective_end:
                continue

            rider_key = link.rider_family_key
            versions = self._repo.list_tariff_versions(rider_key)
            rider_version = _select_version(versions, ref_date)
            if rider_version is None:
                not_found.append(rider_key)
                continue

            rider_charges = self._repo.list_tariff_charges(rider_version.id)
            if not rider_charges:
                not_found.append(rider_key)
                continue

            rider_adj = [c for c in rider_charges if c.charge_type == "adjustment"]
            if rider_adj:
                # Separate percentage-of-energy charges from flat/kWh adjustments
                pct_charges = [c for c in rider_adj if (c.rate_unit or "").lower() == "%_energy"]
                flat_charges = [c for c in rider_adj if c not in pct_charges]
                adj_items: list[BillLineItem] = []
                rider_warnings: list[str] = []
                if flat_charges:
                    flat_items, flat_warnings = _calc_adjustment(
                        flat_charges, usage, customer_class, rider_key
                    )
                    adj_items.extend(flat_items)
                    rider_warnings.extend(flat_warnings)
                for c in pct_charges:
                    if not _class_matches(c.customer_class, customer_class):
                        continue
                    fraction = c.rate_value or 0.0
                    amount = round(energy_subtotal * fraction, 2)
                    adj_items.append(BillLineItem(
                        label=c.charge_label or "Energy Discount",
                        charge_type="adjustment",
                        source=rider_key,
                        rate_value=fraction,
                        rate_unit="%_energy",
                        quantity=energy_subtotal,
                        amount=amount,
                        notes=c.source_snippet,
                    ))
                # Tag optional rider line items so callers can distinguish them
                if is_optional:
                    for item in adj_items:
                        item.notes = (
                            f"[optional:{link.enrollment_type}]"
                            + (f" {item.notes}" if item.notes else "")
                        ).strip()
                    optional_applied.append(rider_key)
                items.extend(adj_items)
                warnings.extend(rider_warnings)
            else:
                not_found.append(rider_key)

        if not_found:
            warnings.append(
                f"Rider charges not yet parsed for: {', '.join(not_found)}. "
                "Rider adjustments for these riders are not included in this estimate."
            )

        # Cross-check total per-kWh rider rate against leaf-600 authoritative sum (TD-V4-001)
        check_warning = validate_rider_total(
            self._repo, base_family_key, items, ref_date, customer_class,
            summary_rider_keys=summary_rider_keys,
        )
        if check_warning:
            warnings.append(check_warning)

        return items, warnings, optional_applied


# ---------------------------------------------------------------------------
# Version selection
# ---------------------------------------------------------------------------


def _select_version(versions, ref_date: datetime.date):
    """Pick the version in effect as of ref_date.

    Prefers the most recent version with effective_start <= ref_date.
    Falls back to the latest version if none have a known effective_start.
    """
    if not versions:
        return None
    dated = [v for v in versions if v.effective_start]
    if dated:
        eligible = [v for v in dated if v.effective_start <= str(ref_date)]
        if eligible:
            return max(eligible, key=lambda v: v.effective_start)
        # All versions are future-dated; return the earliest
        return min(dated, key=lambda v: v.effective_start)
    # No dated versions — return the first (usually only one)
    return versions[0]


# ---------------------------------------------------------------------------
# Schedule group classification
# ---------------------------------------------------------------------------

#: Maps schedule_code prefixes (case-insensitive) to logical customer groups.
#: Used to filter compare-tariff-rates output to eligible schedules.
#: Keys are exact schedule_code values or prefix patterns (longest match wins).
SCHEDULE_GROUPS: dict[str, str] = {
    # Residential
    "RES": "residential",
    "R_TOU": "residential",
    "R_TOUD": "residential",
    "RS": "residential",        # DEC residential
    "RSTOU": "residential",     # SC carolinas R-STOU
    "R_STOU": "residential",    # SC progress
    "R_TOU_CPP": "residential",
    "RS_HERP": "residential",
    "RS_NES": "residential",
    # Small General Service
    "SGS": "sgs",
    "SGS_TOUE": "sgs",
    "SGS_TOU_CLR": "sgs",
    "SGS_TOU_CPP": "sgs",
    # Medium General Service
    "MGS": "mgs",
    "MGS_TOU": "mgs",
    # Large General Service
    "LGS": "lgs",
    "LGS_TOU": "lgs",
    "LGS_HLF": "lgs",
    "LGS_RTP": "lgs",
    "LGS_CUR_TOU": "lgs",
    "LGS_RTP_TOU": "lgs",
    # General Service (other)
    "GS": "gs",
    "GSD": "gs",
    "GST": "gs",
    "GSLM": "gs",
    "GSDT": "gs",
    "GS_TES": "gs",
    "GSA": "gs",
    # Specialty / other — not typically compared in rate plan selection
    "HP": "specialty",
    "APH_TES": "specialty",
    "CH_TOUE": "specialty",
    "SI": "specialty",
    "SFLS": "specialty",
    "TFS": "specialty",
    "FUEL": "specialty",
    "SLR": "specialty",
    "SLS": "specialty",
    "TSS": "specialty",
    "TFS": "specialty",
}

#: Groups shown by default when --group is not specified
RESIDENTIAL_GROUPS = frozenset({"residential"})
SGS_GROUPS = frozenset({"sgs"})
RESIDENTIAL_AND_SGS_GROUPS = frozenset({"residential", "sgs"})


def schedule_group_for(schedule_code: str | None) -> str:
    """Return the logical customer group for a schedule_code.

    Returns one of: residential, sgs, mgs, lgs, gs, specialty, unknown.
    Matching is case-insensitive; longest exact match wins.
    """
    if not schedule_code:
        return "unknown"
    upper = schedule_code.upper().replace("-", "_")
    if upper in SCHEDULE_GROUPS:
        return SCHEDULE_GROUPS[upper]
    # Prefix match (e.g. "R_TOU_CPP" matches "R_TOU" prefix)
    for key in sorted(SCHEDULE_GROUPS, key=len, reverse=True):
        if upper.startswith(key):
            return SCHEDULE_GROUPS[key]
    return "unknown"
