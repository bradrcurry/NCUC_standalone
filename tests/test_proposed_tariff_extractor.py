from __future__ import annotations

from duke_rates.document_intelligence.proposed_tariff_extractor import (
    extract_charge_candidates,
)


def test_extract_charge_candidates_reads_basic_and_energy_lines() -> None:
    charges = extract_charge_candidates(
        """
        Basic Customer Charge $15.50 per month
        Kilowatt-Hour Charge 12.3456 cents per kWh
        On-Peak Demand Charge $4.20 per kW
        """
    )

    by_type = {c.charge_type: c for c in charges}
    assert by_type["fixed"].rate_value == 15.50
    assert by_type["fixed"].rate_unit == "$/month"
    assert by_type["energy"].rate_value == 0.123456
    assert by_type["energy"].rate_unit == "$/kWh"
    assert by_type["demand"].rate_value == 4.20
    assert by_type["demand"].rate_unit == "$/kw"


def test_extract_charge_candidates_keeps_rider_adjustment_separate() -> None:
    charges = extract_charge_candidates(
        "Rider Adjustment Residential 0.123 cents per kWh"
    )

    assert len(charges) == 1
    assert charges[0].charge_type == "adjustment"
    assert charges[0].rate_value == 0.00123
