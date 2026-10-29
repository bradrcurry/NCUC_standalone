"""Canonical charge types and units (Decimal arithmetic).

One place decides what a printed unit means and what a stored charge type is,
for extraction, validation and billing alike. The rules:

* The **printed** value and unit are kept as printed; the canonical value and
  unit are derived from them, never the other way round.
* Values are ``Decimal``. Cents become dollars by an exact ``* 0.01``.
* A unit is recognized only from an explicit list. Anything else (including
  the U+FFFD replacement character where a ``¢`` or ``$`` used to be) yields
  no canonical value and a warning. No unit is ever assumed.
* Charge types map through explicit aliases. The billing basis (fixed,
  energy, demand, percent...) comes from the unit, and a type whose unit
  contradicts it is flagged rather than coerced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class CanonicalUnit(StrEnum):
    DOLLARS_PER_MONTH = "$/month"
    DOLLARS_PER_KWH = "$/kWh"
    DOLLARS_PER_KW = "$/kW"
    DOLLARS_PER_KVAR = "$/kVAR"
    DOLLARS_PER_UNIT_MONTH = "$/unit-month"  # per fixture, signal, device, account...
    DOLLARS_PER_YEAR = "$/year"
    DOLLARS_PER_WATT = "$/W"
    DOLLARS_PER_EVENT = "$/event"
    DOLLARS = "$"  # one-time amount
    PERCENT = "%"  # percentage points as printed (5 means 5%)
    FRACTION = "fraction"  # share of a base, as stored for '%_energy' (0.05 means 5%)


class Basis(StrEnum):
    FIXED = "fixed"
    ENERGY = "energy"
    DEMAND = "demand"
    REACTIVE = "reactive"
    PERCENT = "percent"
    ONE_TIME = "one_time"
    OTHER = "other"


_CENT = Decimal("0.01")
_ONE = Decimal(1)


@dataclass(frozen=True)
class UnitParse:
    printed: str | None
    canonical: CanonicalUnit | None
    factor: Decimal | None  # printed value * factor = canonical value
    basis: Basis | None
    warning: str | None = None

    @property
    def recognized(self) -> bool:
        return self.canonical is not None


# (pattern on the normalized unit, canonical unit, factor, basis). Order matters.
_UNIT_RULES: tuple[tuple[re.Pattern[str], CanonicalUnit, Decimal, Basis], ...] = tuple(
    (re.compile(pattern), unit, factor, basis)
    for pattern, unit, factor, basis in (
        (r"^%_energy$", CanonicalUnit.FRACTION, _ONE, Basis.PERCENT),
        (r"^(%|percent)( of (the )?applicable bill)?$", CanonicalUnit.PERCENT, _ONE, Basis.PERCENT),
        (r"^(cents?|¢)/kwh$", CanonicalUnit.DOLLARS_PER_KWH, _CENT, Basis.ENERGY),
        (r"^\$/kwh$", CanonicalUnit.DOLLARS_PER_KWH, _ONE, Basis.ENERGY),
        (r"^(cents?|¢)/kw(-?month|/month)?$", CanonicalUnit.DOLLARS_PER_KW, _CENT, Basis.DEMAND),
        (r"^\$/kw(-?month|/month)?$", CanonicalUnit.DOLLARS_PER_KW, _ONE, Basis.DEMAND),
        (r"^\$/kvar$", CanonicalUnit.DOLLARS_PER_KVAR, _ONE, Basis.REACTIVE),
        (r"^\$/kva$", CanonicalUnit.DOLLARS_PER_KW, _ONE, Basis.DEMAND),
        (
            r"^\$/(month|bill|customer/month|account-month)$",
            CanonicalUnit.DOLLARS_PER_MONTH,
            _ONE,
            Basis.FIXED,
        ),
        (
            r"^\$/(fixture|signal|block|device)(/|-)month$",
            CanonicalUnit.DOLLARS_PER_UNIT_MONTH,
            _ONE,
            Basis.FIXED,
        ),
        (r"^\$/block$", CanonicalUnit.DOLLARS_PER_UNIT_MONTH, _ONE, Basis.FIXED),
        (
            r"^\$/(year|device-year|thermostat-year)$",
            CanonicalUnit.DOLLARS_PER_YEAR,
            _ONE,
            Basis.OTHER,
        ),
        (r"^\$/(w|watt)$", CanonicalUnit.DOLLARS_PER_WATT, _ONE, Basis.ONE_TIME),
        (r"^\$/event$", CanonicalUnit.DOLLARS_PER_EVENT, _ONE, Basis.OTHER),
        (r"^\$(/(enrollment|referral))?$", CanonicalUnit.DOLLARS, _ONE, Basis.ONE_TIME),
    )
)

_WORD_UNITS = (
    (re.compile(r"cents?\s+per\s+kilowatt[- ]?hours?|cents?\s+per\s+kwh"), "¢/kwh"),
    (re.compile(r"dollars?\s+per\s+kilowatt[- ]?hours?|dollars?\s+per\s+kwh"), "$/kwh"),
    (re.compile(r"dollars?\s+per\s+kilowatt\b|dollars?\s+per\s+kw\b"), "$/kw"),
    (re.compile(r"cents?\s+per\s+kilowatt\b|cents?\s+per\s+kw\b"), "¢/kw"),
    (re.compile(r"dollars?\s+per\s+month|per\s+month"), "$/month"),
)


def _normalize_unit_text(text: str) -> str:
    unit = " ".join(text.strip().lower().split())
    unit = unit.replace("dollars/", "$/").replace("dollar/", "$/")
    for pattern, replacement in _WORD_UNITS:
        if pattern.fullmatch(unit):
            return replacement
    unit = re.sub(r"\s*/\s*", "/", unit)
    unit = unit.replace(" per ", "/")
    return unit


def parse_unit(printed: str | None) -> UnitParse:
    """Interpret a printed or stored unit. Unknown units are reported, not guessed."""
    if printed is None or not printed.strip():
        return UnitParse(printed, None, None, None, "unit_missing")
    if "�" in printed:
        return UnitParse(printed, None, None, None, "unit_has_replacement_character")
    normalized = _normalize_unit_text(printed)
    for pattern, unit, factor, basis in _UNIT_RULES:
        if pattern.fullmatch(normalized):
            return UnitParse(printed, unit, factor, basis)
    return UnitParse(printed, None, None, None, "unit_unrecognized")


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

_NUMBER_TEXT_RE = re.compile(
    r"^\s*(?P<neg_open>\()?\s*(?P<sign>[-−–])?\s*(?:\$|¢)?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"\s*(?:¢|cents?)?\s*(?P<neg_close>\))?\s*\**\s*$",
    re.I,
)


def parse_decimal(text: str | float | int | Decimal | None) -> Decimal | None:
    """Exact Decimal from a printed number: ``(0.249)`` -> -0.249, ``1,250`` -> 1250.

    Floats are converted through ``repr`` so 0.1 stays 0.1. Returns None for
    anything that is not a single number (no partial matches).
    """
    if text is None:
        return None
    if isinstance(text, Decimal):
        return text
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        try:
            return Decimal(repr(text)) if isinstance(text, float) else Decimal(text)
        except InvalidOperation:
            return None
    match = _NUMBER_TEXT_RE.match(str(text))
    if not match:
        return None
    if bool(match.group("neg_open")) != bool(match.group("neg_close")):
        return None
    value = Decimal(match.group("num").replace(",", ""))
    if match.group("neg_open") or match.group("sign"):
        value = -value
    return value


# ---------------------------------------------------------------------------
# Charge types
# ---------------------------------------------------------------------------


class CanonicalChargeType(StrEnum):
    FIXED = "fixed"
    FIXED_CREDIT = "fixed_credit"
    ENERGY = "energy"
    TOU_ENERGY = "tou_energy"
    DEMAND = "demand"
    MINIMUM = "minimum"
    ENERGY_ADJUSTMENT = "energy_adjustment"
    DEMAND_ADJUSTMENT = "demand_adjustment"
    FIXED_ADJUSTMENT = "fixed_adjustment"
    PERCENT_ADJUSTMENT = "percent_adjustment"
    ENERGY_CREDIT = "energy_credit"
    ADJUSTMENT_TOTAL = "adjustment_total"  # informational checksum row, never billed
    INCENTIVE = "incentive"  # customer program payment, never billed
    UNKNOWN = "unknown"


#: Stored charge_type spellings -> the family they belong to. Anything not
#: listed is UNKNOWN. Keep in step with billing.charge_types.
CHARGE_TYPE_ALIASES: dict[str, str] = {
    "fixed": "fixed",
    "Fixed Monthly Charge": "fixed",
    "Basic Facilities Charge": "fixed",
    "energy_block": "energy",
    "Energy Charge": "energy",
    "Seasonal Rate": "energy",
    "tou_energy": "tou_energy",
    "demand": "demand",
    "Demand Charge": "demand",
    "minimum": "minimum",
    "credit": "credit",
    "adjustment": "adjustment",
    "Rider Adjustment": "adjustment",
    "rider_adjustment": "adjustment",
    "adjustment_total": "adjustment_total",
    "incentive": "incentive",
    "rebate": "incentive",
    "Program Incentive": "incentive",
}

_INFORMATIONAL = {CanonicalChargeType.ADJUSTMENT_TOTAL, CanonicalChargeType.INCENTIVE}

_EXPECTED_BASIS: dict[str, set[Basis]] = {
    "fixed": {Basis.FIXED},
    "energy": {Basis.ENERGY},
    "tou_energy": {Basis.ENERGY},
    "demand": {Basis.DEMAND, Basis.REACTIVE},
    "minimum": {Basis.FIXED, Basis.DEMAND},
}


@dataclass
class CanonicalCharge:
    stored_type: str
    charge_type: CanonicalChargeType
    label: str | None
    printed_value_text: str | None
    printed_unit: str | None
    value: Decimal | None  # as printed, in the printed unit
    canonical_value: Decimal | None
    canonical_unit: CanonicalUnit | None
    basis: Basis | None
    warnings: list[str] = field(default_factory=list)

    @property
    def billable(self) -> bool:
        return (
            self.charge_type not in _INFORMATIONAL
            and self.charge_type != CanonicalChargeType.UNKNOWN
        )


def _adjustment_type(basis: Basis | None) -> CanonicalChargeType | None:
    return (
        {
            Basis.ENERGY: CanonicalChargeType.ENERGY_ADJUSTMENT,
            Basis.DEMAND: CanonicalChargeType.DEMAND_ADJUSTMENT,
            Basis.REACTIVE: CanonicalChargeType.DEMAND_ADJUSTMENT,
            Basis.FIXED: CanonicalChargeType.FIXED_ADJUSTMENT,
            Basis.PERCENT: CanonicalChargeType.PERCENT_ADJUSTMENT,
        }.get(basis)
        if basis
        else None
    )


def canonicalize_charge(
    stored_type: str | None,
    value: str | float | Decimal | None,
    unit: str | None,
    *,
    label: str | None = None,
    printed_value_text: str | None = None,
    tou_period: str | None = None,
) -> CanonicalCharge:
    """Canonical type, unit and value for one charge, with warnings for anything unclear."""
    warnings: list[str] = []
    raw = printed_value_text if printed_value_text is not None else value
    number = parse_decimal(raw)
    if number is None and raw is not None:
        warnings.append(f"value_unparseable:{raw!r}")
    parsed = parse_unit(unit)
    if parsed.warning:
        warnings.append(f"{parsed.warning}:{unit!r}")
    canonical_value = (
        number * parsed.factor if number is not None and parsed.factor is not None else None
    )

    family = CHARGE_TYPE_ALIASES.get(stored_type or "")
    lowered = (label or "").lower()
    ctype: CanonicalChargeType
    if family is None:
        ctype = CanonicalChargeType.UNKNOWN
        warnings.append(f"charge_type_unrecognized:{stored_type!r}")
    elif family == "adjustment_total":
        ctype = CanonicalChargeType.ADJUSTMENT_TOTAL
    elif family == "incentive":
        ctype = CanonicalChargeType.INCENTIVE
    elif family == "adjustment":
        adj = _adjustment_type(parsed.basis)
        if adj is None:
            ctype = CanonicalChargeType.UNKNOWN
            warnings.append("adjustment_basis_unknown")
        else:
            ctype = adj
        if "total" in lowered:
            warnings.append("adjustment_labelled_total:may_double_count")
    elif family == "credit":
        if parsed.basis == Basis.ENERGY:
            ctype = CanonicalChargeType.ENERGY_CREDIT
        elif parsed.basis == Basis.FIXED:
            ctype = CanonicalChargeType.FIXED_CREDIT
        else:
            ctype = CanonicalChargeType.UNKNOWN
            warnings.append("credit_basis_unknown")
    elif family == "energy" and (tou_period or stored_type == "tou_energy"):
        ctype = CanonicalChargeType.TOU_ENERGY
    else:
        ctype = CanonicalChargeType(
            {
                "fixed": "fixed",
                "energy": "energy",
                "tou_energy": "tou_energy",
                "demand": "demand",
                "minimum": "minimum",
            }[family]
        )
        if family == "fixed" and number is not None and number < 0:
            ctype = CanonicalChargeType.FIXED_CREDIT
    expected = _EXPECTED_BASIS.get(family or "")
    if expected and parsed.basis and parsed.basis not in expected:
        warnings.append(f"type_unit_conflict:{stored_type}:{parsed.basis.value}")
    return CanonicalCharge(
        stored_type=stored_type or "",
        charge_type=ctype,
        label=label,
        printed_value_text=printed_value_text,
        printed_unit=unit,
        value=number,
        canonical_value=canonical_value,
        canonical_unit=parsed.canonical,
        basis=parsed.basis,
        warnings=warnings,
    )


def dollars_factor(unit: str | None) -> Decimal | None:
    """Factor that turns a value in ``unit`` into dollars, or None if unknown."""
    return parse_unit(unit).factor
