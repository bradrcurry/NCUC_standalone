from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from duke_rates.models.rate_schedule import DemandCharge, EnergyCharge, FixedCharge


class BillLineItem(BaseModel):
    label: str
    amount: float
    details: str | None = None


class IntervalUsagePoint(BaseModel):
    timestamp: datetime
    kwh: float
    kw: float | None = None


class UsageInput(BaseModel):
    monthly_kwh: float
    peak_kw: float | None = None
    service_date: date | None = None
    billing_period_start: date | None = None
    billing_period_end: date | None = None
    interval_data: list[IntervalUsagePoint] = Field(default_factory=list)


def calculate_fixed_charge(
    charges: list[FixedCharge],
    *,
    billing_period_start: date | None = None,
    billing_period_end: date | None = None,
) -> list[BillLineItem]:
    if not charges:
        return []
    charge = charges[0]
    amount = prorate_fixed_monthly_amount(
        charge.amount or 0.0,
        billing_period_start=billing_period_start,
        billing_period_end=billing_period_end,
    )
    details = f"per {charge.unit}"
    if amount != (charge.amount or 0.0):
        total_days = _billing_period_days(billing_period_start, billing_period_end)
        details = f"{details}; prorated {total_days}/30"
    return [
        BillLineItem(
            label=charge.label,
            amount=amount,
            details=details,
        )
    ]


def prorate_fixed_monthly_amount(
    amount: float,
    *,
    billing_period_start: date | None = None,
    billing_period_end: date | None = None,
) -> float:
    total_days = _billing_period_days(billing_period_start, billing_period_end)
    if total_days is None or not _is_partial_billing_period(total_days):
        return round(amount, 2)
    return round(amount * total_days / 30.0, 2)


def _billing_period_days(
    billing_period_start: date | None,
    billing_period_end: date | None,
) -> int | None:
    if billing_period_start is None or billing_period_end is None:
        return None
    total_days = (billing_period_end - billing_period_start).days + 1
    if total_days <= 0:
        return None
    return total_days


def _is_partial_billing_period(total_days: int) -> bool:
    return total_days < 25


def calculate_energy_charge(charges: list[EnergyCharge], monthly_kwh: float) -> list[BillLineItem]:
    if not charges:
        return []
    return _calculate_block_energy_charge(charges, monthly_kwh)


def calculate_demand_charge(
    charges: list[DemandCharge], peak_kw: float | None
) -> list[BillLineItem]:
    if not charges or peak_kw is None:
        return []
    primary = charges[0]
    rate = primary.rate or 0.0
    return [
        BillLineItem(
            label=primary.label,
            amount=round(peak_kw * rate, 2),
            details=f"{peak_kw} {primary.unit} @ {rate}",
        )
    ]


def apply_block_tiers(charges: list[dict], kwh: float) -> list[dict]:
    """Apply tiered block pricing to *kwh* using a list of charge dicts.

    Each dict must contain at minimum:
        ``rate``      (float, $/kWh already converted)
        ``label``     (str)
        ``unit``      (str, for the output dict)
    Optional tier keys:
        ``block_from`` (float | None) — start of this tier (default 0)
        ``block_to``   (float | None) — end of this tier (None = unbounded)

    Returns a list of dicts with keys:
        ``label``, ``unit``, ``quantity`` (kWh in this tier), ``rate``, ``amount``.

    This is the single shared implementation for block-tier energy calculation.
    Both the ``BillingEngine`` path (via :func:`_calculate_block_energy_charge`) and
    the ``calculate_bill()`` path in ``ncuc_loader`` delegate here.
    """
    if not charges:
        return []

    has_blocks = any(
        c.get("block_from") is not None or c.get("block_to") is not None
        for c in charges
    )
    if not has_blocks:
        primary = charges[0]
        rate = float(primary.get("rate") or 0.0)
        qty = kwh
        return [{
            "label": primary.get("label", "Energy Charge"),
            "unit": primary.get("unit", "$/kWh"),
            "quantity": qty,
            "rate": rate,
            "amount": round(qty * rate, 6),
        }]

    ordered = sorted(
        charges,
        key=lambda c: (
            float(c["block_from"]) if c.get("block_from") is not None else 0.0,
            float(c["block_to"]) if c.get("block_to") is not None else float("inf"),
        ),
    )

    remaining = kwh
    results: list[dict] = []
    for charge in ordered:
        if remaining <= 0:
            break
        rate = float(charge.get("rate") or 0.0)
        block_start = float(charge["block_from"]) if charge.get("block_from") is not None else 0.0
        if charge.get("block_to") is None:
            block_kwh = max(kwh - block_start, 0.0)
            block_kwh = min(block_kwh, remaining)
        else:
            block_kwh = max(float(charge["block_to"]) - block_start, 0.0)
            block_kwh = min(block_kwh, remaining)
        if block_kwh <= 0:
            continue
        remaining = round(remaining - block_kwh, 6)
        results.append({
            "label": charge.get("label", "Energy Charge"),
            "unit": charge.get("unit", "$/kWh"),
            "quantity": block_kwh,
            "rate": rate,
            "amount": round(block_kwh * rate, 6),
        })
    return results


def _calculate_block_energy_charge(
    charges: list[EnergyCharge],
    monthly_kwh: float,
) -> list[BillLineItem]:
    remaining = monthly_kwh
    line_items: list[BillLineItem] = []
    ordered = sorted(
        charges,
        key=lambda charge: (
            charge.block_from if charge.block_from is not None else 0.0,
            charge.block_to if charge.block_to is not None else float("inf"),
        ),
    )
    has_blocks = any(
        charge.block_from is not None or charge.block_to is not None for charge in ordered
    )
    if not has_blocks:
        primary = ordered[0]
        rate = primary.rate or 0.0
        return [
            BillLineItem(
                label=primary.label,
                amount=round(monthly_kwh * rate, 2),
                details=f"{monthly_kwh} {primary.unit} @ {rate}",
            )
        ]

    for charge in ordered:
        rate = charge.rate or 0.0
        block_start = charge.block_from or 0.0
        if remaining <= 0:
            break
        if charge.block_to is None:
            block_kwh = max(monthly_kwh - block_start, 0.0)
            block_kwh = min(block_kwh, remaining)
        else:
            block_kwh = max(charge.block_to - block_start, 0.0)
            block_kwh = min(block_kwh, remaining)
        if block_kwh <= 0:
            continue
        remaining = round(remaining - block_kwh, 6)
        line_items.append(
            BillLineItem(
                label=charge.label,
                amount=round(block_kwh * rate, 2),
                details=f"{block_kwh} {charge.unit} @ {rate}",
            )
        )
    return line_items
