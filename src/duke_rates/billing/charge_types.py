"""Classification of stored ``tariff_charges.charge_type`` values for billing.

``TariffBillingEngine.calculate`` prices only the charge types in
:data:`ENGINE_PRICED_TYPES`. Anything else used to be dropped without a word.
This module names every other value so the engine can say what it left out,
and so reports can separate harmless exclusions from billable gaps.

Nothing here changes what the engine adds to a bill. Suggested canonical
types are reported, never applied: normalising stored charge types is a data
change that needs review (Phase 2A report, unsupported-charge triage).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ChargeTypeCategory = Literal[
    "priced",
    "excluded_informational",
    "supported_but_mismapped",
    "billable_unsupported",
    "ambiguous_or_invalid",
]

# Types the engine prices (see TariffBillingEngine.calculate / _apply_riders).
ENGINE_PRICED_TYPES: frozenset[str] = frozenset(
    {"fixed", "energy_block", "tou_energy", "demand", "adjustment", "minimum", "credit"}
)

# Never added to a bill on purpose.
# * adjustment_total: the printed TOTAL row of a rider summary (leaf-600,
#   DEC SUMMARY). Adding it on top of the component riders would double-count;
#   validate_rider_total() uses it only as a checksum.
# * incentive / rebate / Program Incentive: program payments to the customer
#   (load-control credits, solar rebates, PowerPair). They are not monthly
#   tariff charges.
EXCLUDED_INFORMATIONAL_TYPES: dict[str, str] = {
    "adjustment_total": "rider-summary TOTAL checksum row; pricing it would double-count the component riders",
    "incentive": "customer program incentive payment, not a monthly tariff charge",
    "rebate": "one-time customer rebate payment, not a monthly tariff charge",
    "Program Incentive": "customer program incentive payment, not a monthly tariff charge",
}

# Free-text or alternate spellings of a type the engine supports. The stored
# rows are billable, but the engine never sees them because of the spelling.
MISMAPPED_TYPE_ALIASES: dict[str, str] = {
    "Fixed Monthly Charge": "fixed",
    "Basic Facilities Charge": "fixed",
    "Demand Charge": "demand",
    "Energy Charge": "energy_block",
    "Seasonal Rate": "energy_block",
    "Rider Adjustment": "adjustment",
    "rider_adjustment": "adjustment",
}

# Generic 'energy' rows. Flat ones would be energy_block; rows with an
# on/off-peak/discount period would be tou_energy; rows with a 'standard'
# period (R-TOU-EV) have no engine equivalent at all.
_ENERGY_TOU_PERIODS = {"on_peak", "off_peak", "discount", "shoulder", "mid_peak", "super_off_peak"}

@dataclass(frozen=True)
class ChargeTypeClassification:
    category: ChargeTypeCategory
    charge_type: str
    suggested_type: str | None
    reason: str


def classify_charge(
    charge_type: str | None,
    *,
    tou_period: str | None = None,
    rate_unit: str | None = None,
    charge_label: str | None = None,
) -> ChargeTypeClassification:
    """Classify one stored charge by how the billing engine treats it."""
    ctype = charge_type or ""
    label = (charge_label or "").lower()
    unit = rate_unit or ""

    if ctype in ENGINE_PRICED_TYPES:
        return ChargeTypeClassification("priced", ctype, None, "priced by TariffBillingEngine")

    if ctype in EXCLUDED_INFORMATIONAL_TYPES:
        return ChargeTypeClassification(
            "excluded_informational", ctype, None, EXCLUDED_INFORMATIONAL_TYPES[ctype]
        )

    if ctype == "Rider Adjustment" and unit.strip().startswith("%"):
        return ChargeTypeClassification(
            "billable_unsupported",
            ctype,
            "adjustment",
            "percentage rider adjustment; the engine prices only '%_energy' percentages",
        )

    if ctype in MISMAPPED_TYPE_ALIASES and "total" in label:
        return ChargeTypeClassification(
            "ambiguous_or_invalid",
            ctype,
            None,
            "a TOTAL row stored beside its components; mapping it would double-count",
        )

    if ctype in MISMAPPED_TYPE_ALIASES:
        return ChargeTypeClassification(
            "supported_but_mismapped",
            ctype,
            MISMAPPED_TYPE_ALIASES[ctype],
            f"free-text charge_type; the engine supports this as '{MISMAPPED_TYPE_ALIASES[ctype]}'",
        )

    if ctype == "energy":
        period = (tou_period or "").lower()
        if period == "standard":
            return ChargeTypeClassification(
                "billable_unsupported",
                ctype,
                None,
                "'standard' TOU period (R-TOU-EV style) has no engine equivalent",
            )
        if "adder" in label:
            return ChargeTypeClassification(
                "billable_unsupported",
                ctype,
                None,
                "real-time-pricing adder depends on hourly prices the engine does not model",
            )
        if period in _ENERGY_TOU_PERIODS or any(
            token in label for token in ("on-peak", "off-peak", "discount")
        ):
            return ChargeTypeClassification(
                "supported_but_mismapped",
                ctype,
                "tou_energy",
                "generic 'energy' row with a TOU period; the engine prices it as 'tou_energy'",
            )
        return ChargeTypeClassification(
            "supported_but_mismapped",
            ctype,
            "energy_block",
            "generic flat 'energy' row; the engine prices it as 'energy_block'",
        )

    if ctype == "Fee":
        return ChargeTypeClassification(
            "ambiguous_or_invalid",
            ctype,
            None,
            "unlabelled 'Fee'; cannot tell a recurring charge from a one-time service fee",
        )

    if not ctype:
        return ChargeTypeClassification("ambiguous_or_invalid", ctype, None, "empty charge_type")

    return ChargeTypeClassification(
        "ambiguous_or_invalid", ctype, None, "charge_type not known to the billing engine"
    )


def is_potentially_billable(classification: ChargeTypeClassification) -> bool:
    """True for anything the engine skips that might belong on a bill."""
    return classification.category in (
        "supported_but_mismapped",
        "billable_unsupported",
        "ambiguous_or_invalid",
    )
