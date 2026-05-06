"""Solar PV sizing and ROI estimator for Duke Energy customers.

Given a UsageProfile (from ESPI/Green Button XML) and a system size, estimates
monthly solar generation, bill savings under net metering, and payback period.

Duke NC Net Metering (Rider NM / NMB):
    Monthly net: generation offsets consumption at retail rates.
    Annual surplus: excess credited at avoided-cost rate (~$0.03–0.05/kWh),
    which is far below retail. Oversizing beyond annual load is therefore
    financially inefficient. This module uses $0.04/kWh as the default
    avoided-cost export credit.

NC Monthly Capacity Factors:
    kWh generated per kW_dc nameplate per month for central NC (approx.
    4° tilt, south-facing). Source: NREL PVWatts typical values for Raleigh NC.
    These are AC output after applying the derate factor internally; the CF
    table values represent ideal conditions and the derate is applied separately.

Demand charge caveat:
    Solar PV generates only during daylight hours. Duke TOU on-peak is 2–9 PM,
    so solar does overlap peak hours in summer — but demand charges are based on
    a single 15-minute peak reading which may not coincide with peak solar output
    (e.g., a cloudy afternoon). Without interval data we cannot reliably reduce
    peak_kw, so it is passed through unchanged. Savings estimates for
    demand-metered schedules (R-TOUD, SGS, MGS) are therefore conservative.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duke_rates.billing.tariff_engine import TariffBillingEngine

from duke_rates.billing.espi_parser import MonthlyUsageSummary, UsageProfile
from duke_rates.billing.tariff_engine import BillInput

# ---------------------------------------------------------------------------
# NC monthly capacity factors: kWh per kW_dc per month (pre-derate)
# ---------------------------------------------------------------------------
_NC_MONTHLY_CF: dict[int, float] = {
    1: 85.0, 2: 100.0, 3: 120.0, 4: 130.0,
    5: 135.0, 6: 130.0, 7: 130.0, 8: 125.0,
    9: 115.0, 10: 110.0, 11: 90.0, 12: 80.0,
}

_DEFAULT_DERATE = 0.80          # DC-to-AC inverter + wiring losses (residential string)
_DEFAULT_AVOIDED_COST = 0.04   # $/kWh export credit (Duke NC annual true-up rate)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SolarMonth:
    """Per-month solar production and billing result."""
    year: int
    month: int
    generation_kwh: float       # AC output from the array this month
    offset_kwh: float           # kWh of generation that covered on-site load
    export_kwh: float           # kWh surplus exported to grid (valued at avoided cost)
    net_usage_kwh: float        # metered consumption after solar credit (≥ 0)
    net_on_peak_kwh: float      # on_peak after proportional solar offset
    net_off_peak_kwh: float     # off_peak after proportional solar offset
    net_discount_kwh: float     # discount after proportional solar offset
    net_peak_kw: float | None   # demand — unchanged (see module docstring)
    bill_without: float         # monthly bill without solar
    bill_with: float            # monthly bill with solar (after export credit)
    savings: float              # bill_without - bill_with


@dataclass
class SolarSizingResult:
    """Full solar sizing result for one system size."""
    system_kw: float
    derate: float
    annual_generation_kwh: float
    annual_offset_kwh: float
    annual_export_kwh: float
    annual_savings: float
    export_credit_value: float  # total $ earned from exported kWh at avoided cost
    cost_dollars: float | None  # installed cost (system_kw * 1000 * cost_per_watt)
    payback_years: float | None # cost_dollars / annual_savings
    months: list[SolarMonth] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_cf_table(location: str) -> dict[int, float]:
    """Return the monthly capacity factor table for a location."""
    loc = location.upper().strip()
    if loc in ("NC", "NORTH CAROLINA"):
        return _NC_MONTHLY_CF
    raise ValueError(
        f"Unsupported location: {location!r}. "
        "Currently only 'NC' (North Carolina) capacity factors are defined."
    )


def _monthly_generation(
    system_kw: float,
    month: int,
    derate: float,
    cf_table: dict[int, float],
) -> float:
    """Return AC kWh output for a given system size and month."""
    return system_kw * cf_table[month] * derate


def _proportional_net_usage(
    usage: MonthlyUsageSummary,
    offset_kwh: float,
) -> tuple[float, float, float, float]:
    """Return (net_total, net_on_peak, net_off_peak, net_discount) after solar offset.

    The offset is distributed proportionally across TOU periods based on this
    month's actual usage split. If total_kwh is zero the function returns zeros.
    """
    total = usage.total_kwh
    if total <= 0:
        return 0.0, 0.0, 0.0, 0.0

    on_frac   = usage.on_peak_kwh  / total
    off_frac  = usage.off_peak_kwh / total
    disc_frac = usage.discount_kwh / total

    net_on   = max(0.0, usage.on_peak_kwh  - offset_kwh * on_frac)
    net_off  = max(0.0, usage.off_peak_kwh - offset_kwh * off_frac)
    net_disc = max(0.0, usage.discount_kwh - offset_kwh * disc_frac)

    # Use the sum of reduced period fields as net_total for BillInput consistency
    net_total = net_on + net_off + net_disc
    return net_total, net_on, net_off, net_disc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def size_solar_system(
    profile: UsageProfile,
    family_key: str,
    engine: "TariffBillingEngine",
    system_kw: float,
    *,
    derate: float = _DEFAULT_DERATE,
    avoided_cost_per_kwh: float = _DEFAULT_AVOIDED_COST,
    cost_per_watt: float | None = None,
    location: str = "NC",
    customer_class: str = "residential",
    include_riders: bool = True,
) -> SolarSizingResult:
    """Estimate solar ROI for a given system size against an actual usage profile.

    Args:
        profile: UsageProfile from parse_espi_xml() with monthly TOU breakdowns.
        family_key: Tariff family key to bill under (e.g. "nc-progress-leaf-502").
        engine: TariffBillingEngine instance.
        system_kw: Nameplate DC system capacity in kW.
        derate: DC-to-AC efficiency factor (default 0.80 for residential string inverters).
        avoided_cost_per_kwh: Export credit rate in $/kWh for annual NEM true-up surplus.
        cost_per_watt: Installed cost in $/watt for payback calculation. Pass None to skip.
        location: Region for capacity factor table. Currently only "NC" is supported.
        customer_class: Passed to TariffBillingEngine.calculate().
        include_riders: Whether to include riders in the bill calculation.

    Returns:
        SolarSizingResult with per-month detail and annual summary.

    Raises:
        ValueError: If location is not supported.

    Notes:
        - peak_kw (demand) is NOT reduced by solar — see module docstring.
        - Payback calculation assumes constant rates and ignores ITC, inflation,
          system degradation, and maintenance costs.
    """
    cf_table = _get_cf_table(location)
    solar_months: list[SolarMonth] = []
    warnings: list[str] = []

    for m in profile.months:
        generation_ac = _monthly_generation(system_kw, m.month, derate, cf_table)
        offset_kwh = min(generation_ac, m.total_kwh)
        export_kwh = max(0.0, generation_ac - m.total_kwh)

        net_total, net_on, net_off, net_disc = _proportional_net_usage(m, offset_kwh)

        service_date = datetime.date(m.year, m.month, 1)

        # Baseline bill (without solar)
        bi_without = BillInput(
            monthly_kwh=m.total_kwh,
            service_date=service_date,
            on_peak_kwh=m.on_peak_kwh if m.on_peak_kwh else None,
            off_peak_kwh=m.off_peak_kwh if m.off_peak_kwh else None,
            discount_kwh=m.discount_kwh if m.discount_kwh else None,
            peak_kw=m.peak_kw if m.peak_kw > 0 else None,
        )
        r_without = engine.calculate(
            family_key, bi_without,
            customer_class=customer_class, include_riders=include_riders,
        )
        if r_without.warnings:
            for w in r_without.warnings:
                if w not in warnings:
                    warnings.append(w)

        # Bill with solar (net metering: reduced consumption + export credit)
        if net_total > 0:
            bi_with = BillInput(
                monthly_kwh=round(net_total, 3),
                service_date=service_date,
                on_peak_kwh=round(net_on, 3) if net_on > 0 else None,
                off_peak_kwh=round(net_off, 3) if net_off > 0 else None,
                discount_kwh=round(net_disc, 3) if net_disc > 0 else None,
                peak_kw=m.peak_kw if m.peak_kw > 0 else None,
            )
            r_with = engine.calculate(
                family_key, bi_with,
                customer_class=customer_class, include_riders=include_riders,
            )
            export_credit = round(export_kwh * avoided_cost_per_kwh, 4)
            bill_with = max(0.0, r_with.total - export_credit)
        else:
            # Usage fully offset by solar — only fixed charges remain
            bi_with = BillInput(
                monthly_kwh=0.0,
                service_date=service_date,
                peak_kw=m.peak_kw if m.peak_kw > 0 else None,
            )
            r_with = engine.calculate(
                family_key, bi_with,
                customer_class=customer_class, include_riders=include_riders,
            )
            export_credit = round(export_kwh * avoided_cost_per_kwh, 4)
            bill_with = max(0.0, r_with.total - export_credit)

        bill_without = round(r_without.total, 4)
        bill_with = round(bill_with, 4)
        savings = round(bill_without - bill_with, 4)

        solar_months.append(SolarMonth(
            year=m.year,
            month=m.month,
            generation_kwh=round(generation_ac, 3),
            offset_kwh=round(offset_kwh, 3),
            export_kwh=round(export_kwh, 3),
            net_usage_kwh=round(net_total, 3),
            net_on_peak_kwh=round(net_on, 3),
            net_off_peak_kwh=round(net_off, 3),
            net_discount_kwh=round(net_disc, 3),
            net_peak_kw=m.peak_kw if m.peak_kw > 0 else None,
            bill_without=bill_without,
            bill_with=bill_with,
            savings=savings,
        ))

    annual_gen = round(sum(sm.generation_kwh for sm in solar_months), 2)
    annual_offset = round(sum(sm.offset_kwh for sm in solar_months), 2)
    annual_export = round(sum(sm.export_kwh for sm in solar_months), 2)
    annual_savings = round(sum(sm.savings for sm in solar_months), 2)
    export_credit_value = round(annual_export * avoided_cost_per_kwh, 2)

    cost_dollars: float | None = None
    payback_years: float | None = None
    if cost_per_watt is not None:
        cost_dollars = round(system_kw * 1000 * cost_per_watt, 2)
        if annual_savings > 0:
            payback_years = round(cost_dollars / annual_savings, 1)

    return SolarSizingResult(
        system_kw=system_kw,
        derate=derate,
        annual_generation_kwh=annual_gen,
        annual_offset_kwh=annual_offset,
        annual_export_kwh=annual_export,
        annual_savings=annual_savings,
        export_credit_value=export_credit_value,
        cost_dollars=cost_dollars,
        payback_years=payback_years,
        months=solar_months,
        warnings=warnings,
    )


def sweep_system_sizes(
    profile: UsageProfile,
    family_key: str,
    engine: "TariffBillingEngine",
    sizes: list[float] | None = None,
    *,
    cost_per_watt: float = 3.50,
    derate: float = _DEFAULT_DERATE,
    avoided_cost_per_kwh: float = _DEFAULT_AVOIDED_COST,
    location: str = "NC",
    customer_class: str = "residential",
    include_riders: bool = True,
) -> list[SolarSizingResult]:
    """Sweep a range of system sizes and return sizing results for each.

    Args:
        sizes: List of kW values to evaluate. Defaults to 2–16 kW (1 kW steps).
        cost_per_watt: Installed cost in $/watt (used for payback calculation).

    Returns:
        List of SolarSizingResult, sorted ascending by system_kw.
    """
    if sizes is None:
        sizes = list(range(2, 17))  # 2–16 kW inclusive

    return [
        size_solar_system(
            profile, family_key, engine, float(kw),
            derate=derate,
            avoided_cost_per_kwh=avoided_cost_per_kwh,
            cost_per_watt=cost_per_watt,
            location=location,
            customer_class=customer_class,
            include_riders=include_riders,
        )
        for kw in sorted(sizes)
    ]
