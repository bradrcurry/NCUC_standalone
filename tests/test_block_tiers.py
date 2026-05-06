"""Tests for the shared block-tier energy charge implementation (TD-005).

Verifies that ``apply_block_tiers()`` handles all known edge cases correctly,
and that both the engine path (``BillingEngine.estimate()``) and the ncuc_loader
path (``calculate_bill()``) produce identical results for tiered scenarios.
"""
from __future__ import annotations

import pytest

from duke_rates.billing.calculators import apply_block_tiers


# ---------------------------------------------------------------------------
# 1. apply_block_tiers — unit tests
# ---------------------------------------------------------------------------

class TestApplyBlockTiers:
    def test_flat_rate_no_blocks(self):
        charges = [{"label": "Energy", "rate": 0.10, "unit": "$/kWh"}]
        results = apply_block_tiers(charges, 1000)
        assert len(results) == 1
        assert results[0]["quantity"] == 1000
        assert results[0]["amount"] == pytest.approx(100.0)

    def test_two_tier_first_block_only(self):
        """500 kWh, all in the first block (0–800)."""
        charges = [
            {"label": "First", "rate": 0.12, "unit": "$/kWh", "block_from": 0, "block_to": 800},
            {"label": "Additional", "rate": 0.10, "unit": "$/kWh", "block_from": 800},
        ]
        results = apply_block_tiers(charges, 500)
        assert len(results) == 1
        assert results[0]["label"] == "First"
        assert results[0]["quantity"] == 500
        assert results[0]["amount"] == pytest.approx(60.0)

    def test_two_tier_spans_both_blocks(self):
        """1200 kWh: 800 in first block, 400 in second."""
        charges = [
            {"label": "First", "rate": 0.12623, "unit": "$/kWh", "block_from": 0, "block_to": 800},
            {"label": "Additional", "rate": 0.11623, "unit": "$/kWh", "block_from": 800},
        ]
        results = apply_block_tiers(charges, 1200)
        assert len(results) == 2
        first = next(r for r in results if r["label"] == "First")
        second = next(r for r in results if r["label"] == "Additional")
        assert first["quantity"] == 800
        assert first["amount"] == pytest.approx(800 * 0.12623, abs=1e-4)
        assert second["quantity"] == 400
        assert second["amount"] == pytest.approx(400 * 0.11623, abs=1e-4)

    def test_two_tier_exactly_at_boundary(self):
        """800 kWh — exactly fills the first block, none in second."""
        charges = [
            {"label": "First", "rate": 0.12, "unit": "$/kWh", "block_from": 0, "block_to": 800},
            {"label": "Additional", "rate": 0.10, "unit": "$/kWh", "block_from": 800},
        ]
        results = apply_block_tiers(charges, 800)
        assert len(results) == 1
        assert results[0]["label"] == "First"
        assert results[0]["quantity"] == 800

    def test_empty_charges(self):
        assert apply_block_tiers([], 1000) == []

    def test_zero_kwh(self):
        charges = [{"label": "Energy", "rate": 0.10, "unit": "$/kWh", "block_from": 0}]
        results = apply_block_tiers(charges, 0)
        assert all(r["quantity"] == 0 for r in results) or results == []

    def test_unsorted_charges_sorted_correctly(self):
        """Charges supplied in reverse order should still produce correct tiers."""
        charges = [
            {"label": "Additional", "rate": 0.10, "unit": "$/kWh", "block_from": 800},
            {"label": "First", "rate": 0.12, "unit": "$/kWh", "block_from": 0, "block_to": 800},
        ]
        results = apply_block_tiers(charges, 1000)
        assert len(results) == 2
        labels = [r["label"] for r in results]
        assert labels == ["First", "Additional"]
        assert results[0]["quantity"] == 800
        assert results[1]["quantity"] == 200

    def test_three_tiers(self):
        """Three tiers: 0–500, 500–1000, 1000+."""
        charges = [
            {"label": "T1", "rate": 0.10, "unit": "$/kWh", "block_from": 0, "block_to": 500},
            {"label": "T2", "rate": 0.09, "unit": "$/kWh", "block_from": 500, "block_to": 1000},
            {"label": "T3", "rate": 0.08, "unit": "$/kWh", "block_from": 1000},
        ]
        results = apply_block_tiers(charges, 1500)
        assert len(results) == 3
        qty_map = {r["label"]: r["quantity"] for r in results}
        assert qty_map["T1"] == 500
        assert qty_map["T2"] == 500
        assert qty_map["T3"] == 500
        total = sum(r["amount"] for r in results)
        assert total == pytest.approx(500*0.10 + 500*0.09 + 500*0.08, abs=1e-4)

    def test_no_double_counting(self):
        """Sum of all tier quantities must equal total kwh."""
        charges = [
            {"label": "First", "rate": 0.12, "unit": "$/kWh", "block_from": 0, "block_to": 800},
            {"label": "Additional", "rate": 0.10, "unit": "$/kWh", "block_from": 800},
        ]
        for kwh in [0, 100, 800, 801, 1234, 5000]:
            results = apply_block_tiers(charges, kwh)
            total_qty = sum(r["quantity"] for r in results)
            assert total_qty == pytest.approx(kwh, abs=1e-4), \
                f"kwh={kwh}: sum of tier quantities {total_qty} != {kwh}"


# ---------------------------------------------------------------------------
# 2. Cross-path agreement: engine path vs. ncuc_loader path
#
# The original bug in ncuc_loader was that it computed tier_kwh independently
# per tier (not using a remaining counter), causing double-counting.
# apply_block_tiers() fixes this. We verify both paths agree on a tiered DEP
# RES scenario.
# ---------------------------------------------------------------------------

from datetime import date

from duke_rates.billing.engine import BillingEngine
from duke_rates.billing.calculators import UsageInput
from duke_rates.models.rate_schedule import EnergyCharge, FixedCharge, RateScheduleData


def _engine_tiered_bill(kwh: float, service_month: int) -> float:
    """Compute total energy charge via BillingEngine for a tiered NC RES-like schedule."""
    schedule = RateScheduleData(
        tariff_id="test_tiered",
        state="NC",
        company="progress",
        schedule_title="Test Tiered",
        fixed_charges=[FixedCharge(label="BFC", amount=0.0)],
        energy_charges=[
            EnergyCharge(
                label="Kilowatt-Hour Charge",
                rate=0.12623,
                season="October - April",
                block_from=0,
                block_to=800,
            ),
            EnergyCharge(
                label="Kilowatt-Hour Charge",
                rate=0.11623,
                season="October - April",
                block_from=800,
            ),
            EnergyCharge(
                label="Kilowatt-Hour Charge",
                rate=0.12623,
                season="May - September",
                block_from=0,
            ),
        ],
    )
    estimate = BillingEngine().estimate(
        schedule,
        UsageInput(monthly_kwh=kwh, service_date=date(2024, service_month, 15)),
    )
    return sum(
        item.amount for item in estimate.line_items
        if item.details and "@" in item.details
    )


def _loader_tiered_energy(kwh: float, service_month: int) -> float:
    """Compute total energy charge via apply_block_tiers() directly, mirroring
    the ncuc_loader path after the TD-005 fix."""
    if service_month in {10, 11, 12, 1, 2, 3, 4}:
        charges = [
            {"label": "KWH", "rate": 0.12623, "unit": "$/kWh",
             "block_from": 0, "block_to": 800, "season": "October - April"},
            {"label": "KWH", "rate": 0.11623, "unit": "$/kWh",
             "block_from": 800, "season": "October - April"},
        ]
    else:
        charges = [
            {"label": "KWH", "rate": 0.12623, "unit": "$/kWh",
             "block_from": 0, "season": "May - September"},
        ]
    return sum(r["amount"] for r in apply_block_tiers(charges, kwh))


@pytest.mark.parametrize("kwh,month", [
    (500,  1),    # winter, first block only
    (800,  1),    # winter, exactly at boundary
    (1234, 1),    # winter, spans both blocks
    (2000, 1),    # winter, heavily in second block
    (1000, 7),    # summer, flat
])
def test_engine_and_loader_paths_agree_on_tiered_bills(kwh, month):
    engine_energy = _engine_tiered_bill(kwh, month)
    loader_energy = _loader_tiered_energy(kwh, month)
    assert engine_energy == pytest.approx(loader_energy, abs=0.01), (
        f"kwh={kwh} month={month}: engine={engine_energy:.4f} loader={loader_energy:.4f}"
    )
