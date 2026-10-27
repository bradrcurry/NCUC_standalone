"""Phase 2B: canonical charge types and units (Decimal)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from duke_rates.parse.canonical import (
    Basis,
    CanonicalChargeType,
    CanonicalUnit,
    canonicalize_charge,
    parse_decimal,
    parse_unit,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.246", Decimal("0.246")),
        ("(0.249)", Decimal("-0.249")),
        ("-0.012", Decimal("-0.012")),
        ("$14.00", Decimal("14.00")),
        ("1,250", Decimal("1250")),
        ("12.119¢", Decimal("12.119")),
        ("0.464*", Decimal("0.464")),
        (".5", Decimal(".5")),
        (0.1, Decimal("0.1")),
        ("12/1/25", None),
        ("0.262 12/1/25", None),
        ("(0.249", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_decimal(text, expected):
    assert parse_decimal(text) == expected


@pytest.mark.parametrize(
    ("unit", "canonical", "factor", "basis"),
    [
        ("$/month", CanonicalUnit.DOLLARS_PER_MONTH, Decimal(1), Basis.FIXED),
        ("$/bill", CanonicalUnit.DOLLARS_PER_MONTH, Decimal(1), Basis.FIXED),
        ("$/customer/month", CanonicalUnit.DOLLARS_PER_MONTH, Decimal(1), Basis.FIXED),
        ("$/kWh", CanonicalUnit.DOLLARS_PER_KWH, Decimal(1), Basis.ENERGY),
        ("cents/kWh", CanonicalUnit.DOLLARS_PER_KWH, Decimal("0.01"), Basis.ENERGY),
        ("¢/kWh", CanonicalUnit.DOLLARS_PER_KWH, Decimal("0.01"), Basis.ENERGY),
        ("cents /kWh", CanonicalUnit.DOLLARS_PER_KWH, Decimal("0.01"), Basis.ENERGY),
        ("cents per kilowatt-hour", CanonicalUnit.DOLLARS_PER_KWH, Decimal("0.01"), Basis.ENERGY),
        ("dollars per kilowatt-hour", CanonicalUnit.DOLLARS_PER_KWH, Decimal(1), Basis.ENERGY),
        ("$/kW", CanonicalUnit.DOLLARS_PER_KW, Decimal(1), Basis.DEMAND),
        ("dollars/kW", CanonicalUnit.DOLLARS_PER_KW, Decimal(1), Basis.DEMAND),
        ("dollars per kilowatt", CanonicalUnit.DOLLARS_PER_KW, Decimal(1), Basis.DEMAND),
        ("$/kW-month", CanonicalUnit.DOLLARS_PER_KW, Decimal(1), Basis.DEMAND),
        ("%", CanonicalUnit.PERCENT, Decimal(1), Basis.PERCENT),
        ("% of applicable bill", CanonicalUnit.PERCENT, Decimal(1), Basis.PERCENT),
        ("%_energy", CanonicalUnit.FRACTION, Decimal(1), Basis.PERCENT),
        ("$/fixture/month", CanonicalUnit.DOLLARS_PER_UNIT_MONTH, Decimal(1), Basis.FIXED),
        ("$/kVAR", CanonicalUnit.DOLLARS_PER_KVAR, Decimal(1), Basis.REACTIVE),
    ],
)
def test_supported_units(unit, canonical, factor, basis):
    parsed = parse_unit(unit)
    assert (parsed.canonical, parsed.factor, parsed.basis, parsed.warning) == (
        canonical,
        factor,
        basis,
        None,
    )


@pytest.mark.parametrize(
    ("unit", "warning"),
    [
        (None, "unit_missing"),
        ("", "unit_missing"),
        ("�/kWh", "unit_has_replacement_character"),
        ("kWh", "unit_unrecognized"),
        ("per kWh", "unit_unrecognized"),
        ("cents", "unit_unrecognized"),
        ("$/therm", "unit_unrecognized"),
    ],
)
def test_unknown_or_ambiguous_units_are_never_assumed(unit, warning):
    parsed = parse_unit(unit)
    assert parsed.canonical is None and parsed.factor is None
    assert parsed.warning == warning


def test_storm_recovery_cents_convert_exactly():
    # Phase 2A finding: leaf-607 stores "0.321 cents per kWh" as 0.321 '¢/kWh'.
    charge = canonicalize_charge(
        "adjustment", 0.321, "¢/kWh", label="Storm Recovery Charge - Small General Service"
    )
    assert charge.canonical_value == Decimal("0.00321")
    assert charge.canonical_unit == CanonicalUnit.DOLLARS_PER_KWH
    assert charge.charge_type == CanonicalChargeType.ENERGY_ADJUSTMENT
    assert charge.value == Decimal("0.321") and charge.printed_unit == "¢/kWh"
    assert charge.warnings == []


def test_printed_text_wins_over_float_and_is_kept():
    charge = canonicalize_charge(
        "adjustment", 0.0024899999, "cents/kWh", printed_value_text="(0.249)"
    )
    assert charge.value == Decimal("-0.249")
    assert charge.canonical_value == Decimal("-0.00249")
    assert charge.printed_value_text == "(0.249)"


@pytest.mark.parametrize(
    ("stored", "unit", "label", "expected"),
    [
        ("fixed", "$/month", "Basic Customer Charge", CanonicalChargeType.FIXED),
        ("Fixed Monthly Charge", "$/month", None, CanonicalChargeType.FIXED),
        ("fixed", "$/month", "Credit", None),  # value decides (see below)
        ("energy_block", "$/kWh", None, CanonicalChargeType.ENERGY),
        ("tou_energy", "$/kWh", None, CanonicalChargeType.TOU_ENERGY),
        ("demand", "$/kW", None, CanonicalChargeType.DEMAND),
        ("adjustment", "$/kWh", None, CanonicalChargeType.ENERGY_ADJUSTMENT),
        ("adjustment", "$/kW", None, CanonicalChargeType.DEMAND_ADJUSTMENT),
        ("adjustment", "%", None, CanonicalChargeType.PERCENT_ADJUSTMENT),
        ("adjustment", "$/bill", None, CanonicalChargeType.FIXED_ADJUSTMENT),
        ("Rider Adjustment", "¢/kWh", None, CanonicalChargeType.ENERGY_ADJUSTMENT),
        ("credit", "$/month", None, CanonicalChargeType.FIXED_CREDIT),
        ("credit", "$/kWh", None, CanonicalChargeType.ENERGY_CREDIT),
        ("adjustment_total", "$/kWh", None, CanonicalChargeType.ADJUSTMENT_TOTAL),
        ("rebate", "$/watt", None, CanonicalChargeType.INCENTIVE),
        ("Fee", "$/month", None, CanonicalChargeType.UNKNOWN),
    ],
)
def test_charge_type_aliases(stored, unit, label, expected):
    charge = canonicalize_charge(stored, 1.0, unit, label=label)
    if expected is not None:
        assert charge.charge_type == expected


def test_negative_fixed_is_a_fixed_credit():
    assert (
        canonicalize_charge("fixed", -5.0, "$/month").charge_type
        == CanonicalChargeType.FIXED_CREDIT
    )


def test_energy_and_demand_are_never_confused():
    demand_in_kwh = canonicalize_charge("demand", 0.05, "$/kWh")
    assert "type_unit_conflict:demand:energy" in demand_in_kwh.warnings
    energy_in_kw = canonicalize_charge("energy_block", 3.0, "$/kW")
    assert "type_unit_conflict:energy_block:demand" in energy_in_kw.warnings


def test_rider_summary_totals_are_informational_not_billable():
    total = canonicalize_charge(
        "adjustment_total", 0.02108, "$/kWh", label="Residential Total Rider Adjustments"
    )
    assert total.charge_type == CanonicalChargeType.ADJUSTMENT_TOTAL
    assert not total.billable
    labelled = canonicalize_charge("rider_adjustment", 0.0007, "$/kWh", label="Total Adjustment")
    assert "adjustment_labelled_total:may_double_count" in labelled.warnings


def test_unknown_unit_leaves_no_canonical_value_and_warns():
    charge = canonicalize_charge("adjustment", 0.3, "�/kWh")
    assert charge.canonical_value is None
    assert charge.charge_type == CanonicalChargeType.UNKNOWN
    assert any(w.startswith("unit_has_replacement_character") for w in charge.warnings)
    assert "adjustment_basis_unknown" in charge.warnings
