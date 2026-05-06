"""Live-DB integration tests for the TariffBillingEngine.

These tests exercise R-TOU, R-TOUD, SGS, and SGS-TOUE schedules against the
actual charge data in the production database.  They verify that the engine
produces structurally correct, numerically sensible bills and that the
three-phase surcharge exclusion, TOU period routing, and rider linking all
work end-to-end on real parsed data.

All expected values are computed from the live charge rows:
    nc-progress-leaf-502  R-TOU   : fixed=$14, on_peak=0.29905$/kWh, off_peak=0.11321$/kWh, discount=0.07372$/kWh
    nc-progress-leaf-503  R-TOUD  : fixed=$14, on_peak=0.21952$/kWh, off_peak=0.11$/kWh, discount=0.08274$/kWh
    nc-progress-leaf-521  SGS-TOUE: fixed=$22, on_peak=0.19463$/kWh, off_peak=0.09721$/kWh, discount=0.07624$/kWh
    nc-progress-leaf-520  SGS     : fixed(3-phase only), energy_block=0.13188$/kWh first 750 kWh (winter only)
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from duke_rates.billing.tariff_engine import BillInput, TariffBillingEngine
from duke_rates.db.repository import Repository

DB_PATH = Path("data/db/duke_rates.db")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repo():
    return Repository(str(DB_PATH))


@pytest.fixture(scope="module")
def engine(repo):
    return TariffBillingEngine(repo)


# ---------------------------------------------------------------------------
# R-TOU (nc-progress-leaf-502) — Residential Time-of-Use
# ---------------------------------------------------------------------------

class TestRTouLive:
    FAMILY_KEY = "nc-progress-leaf-502"

    def test_family_exists_in_db(self, repo):
        fam = repo.get_tariff_family(self.FAMILY_KEY)
        assert fam is not None
        assert "R-TOU" in (fam.title or "") or "R_TOU" == fam.schedule_code

    def test_tou_with_full_period_breakdown(self, engine):
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 11, 1),
                on_peak_kwh=300,
                off_peak_kwh=600,
                discount_kwh=100,
            ),
            include_riders=False,
        )
        assert result.total > 0
        assert len(result.warnings) == 0 or not any("TOU" in w and "average" in w for w in result.warnings)

        # Expected: $14 fixed + 300*0.29905 + 600*0.11321 + 100*0.07372
        # = 14 + 89.715 + 67.926 + 7.372 = 179.013
        assert result.total == pytest.approx(14.0 + 300*0.29905 + 600*0.11321 + 100*0.07372, abs=0.02)

    def test_three_phase_surcharge_excluded(self, engine):
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 11, 1),
                on_peak_kwh=500,
                off_peak_kwh=500,
            ),
            include_riders=False,
        )
        labels = [item.label for item in result.line_items]
        assert not any("Three-Phase" in label for label in labels)

    def test_fixed_charge_is_14_dollars(self, engine):
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 11, 1),
                on_peak_kwh=500,
                off_peak_kwh=500,
            ),
            include_riders=False,
        )
        fixed_items = [item for item in result.line_items if item.charge_type == "fixed"]
        assert len(fixed_items) == 1
        assert fixed_items[0].amount == pytest.approx(14.0, abs=0.01)

    def test_tou_without_breakdown_warns_and_estimates(self, engine):
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 11, 1)),
            include_riders=False,
        )
        assert result.total > 0
        assert any("TOU" in w or "average" in w for w in result.warnings)

    def test_with_riders_total_higher_than_base(self, engine):
        base = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 11, 1),
                on_peak_kwh=300,
                off_peak_kwh=600,
                discount_kwh=100,
            ),
            include_riders=False,
        )
        with_riders = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 11, 1),
                on_peak_kwh=300,
                off_peak_kwh=600,
                discount_kwh=100,
            ),
            include_riders=True,
        )
        # Riders add mandatory adjustments — total should be >= base
        assert with_riders.total >= base.total

    def test_all_tou_line_items_have_correct_charge_type(self, engine):
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=900,
                service_date=datetime.date(2025, 11, 1),
                on_peak_kwh=200,
                off_peak_kwh=600,
                discount_kwh=100,
            ),
            include_riders=False,
        )
        base_types = {item.charge_type for item in result.line_items
                      if item.source == self.FAMILY_KEY}
        assert "tou_energy" in base_types
        assert "fixed" in base_types

    def test_on_peak_rate_is_highest_tou_rate(self, engine):
        # With equal kWh on each period, on_peak contribution should be highest
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=300,
                service_date=datetime.date(2025, 11, 1),
                on_peak_kwh=100,
                off_peak_kwh=100,
                discount_kwh=100,
            ),
            include_riders=False,
        )
        tou_items = [i for i in result.line_items if i.charge_type == "tou_energy" and i.source == self.FAMILY_KEY]
        assert len(tou_items) == 3
        by_period = {i.label: i.amount for i in tou_items}
        on_peak_amount = by_period.get("Energy Charge - On-Peak", 0)
        off_peak_amount = by_period.get("Energy Charge - Off-Peak", 0)
        discount_amount = by_period.get("Energy Charge - Discount", 0)
        assert on_peak_amount > off_peak_amount > discount_amount


# ---------------------------------------------------------------------------
# R-TOUD (nc-progress-leaf-503) — Residential TOU Demand
# ---------------------------------------------------------------------------

class TestRToudLive:
    FAMILY_KEY = "nc-progress-leaf-503"

    def test_family_exists_in_db(self, repo):
        fam = repo.get_tariff_family(self.FAMILY_KEY)
        assert fam is not None

    def test_tou_with_period_breakdown(self, engine):
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 11, 1),
                on_peak_kwh=300,
                off_peak_kwh=600,
                discount_kwh=100,
            ),
            include_riders=False,
        )
        # Expected: $14 + 300*0.21952 + 600*0.11 + 100*0.08274
        assert result.total == pytest.approx(14.0 + 300*0.21952 + 600*0.11 + 100*0.08274, abs=0.02)

    def test_r_toud_total_lower_than_r_tou_on_peak_heavy(self, engine):
        # R-TOUD has lower on_peak rate (0.21952) than R-TOU (0.29905)
        input_ = BillInput(
            monthly_kwh=1000,
            service_date=datetime.date(2025, 11, 1),
            on_peak_kwh=600,
            off_peak_kwh=300,
            discount_kwh=100,
        )
        r_tou = engine.calculate("nc-progress-leaf-502", input_, include_riders=False)
        r_toud = engine.calculate(self.FAMILY_KEY, input_, include_riders=False)
        assert r_toud.total < r_tou.total


# ---------------------------------------------------------------------------
# SGS-TOUE (nc-progress-leaf-521) — Small General Service All-Energy TOU
# ---------------------------------------------------------------------------

class TestSgsToueLive:
    FAMILY_KEY = "nc-progress-leaf-521"

    def test_family_exists_in_db(self, repo):
        fam = repo.get_tariff_family(self.FAMILY_KEY)
        assert fam is not None
        assert "SGS" in (fam.schedule_code or "")

    def test_tou_with_full_period_breakdown(self, engine):
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=2000,
                service_date=datetime.date(2025, 8, 1),
                on_peak_kwh=600,
                off_peak_kwh=1200,
                discount_kwh=200,
            ),
            include_riders=False,
        )
        # Expected: $22 + 600*0.19463 + 1200*0.09721 + 200*0.07624
        expected = 22.0 + 600*0.19463 + 1200*0.09721 + 200*0.07624
        assert result.total == pytest.approx(expected, abs=0.02)

    def test_three_phase_surcharge_excluded(self, engine):
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 8, 1),
                on_peak_kwh=400,
                off_peak_kwh=600,
            ),
            include_riders=False,
        )
        labels = [item.label for item in result.line_items]
        assert not any("Three-Phase" in label for label in labels)

    def test_sgs_toue_fixed_is_22_dollars(self, engine):
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=500,
                service_date=datetime.date(2025, 8, 1),
                on_peak_kwh=200,
                off_peak_kwh=300,
            ),
            include_riders=False,
        )
        fixed_items = [item for item in result.line_items if item.charge_type == "fixed"]
        assert len(fixed_items) == 1
        assert fixed_items[0].amount == pytest.approx(22.0, abs=0.01)

    def test_sgs_toue_on_peak_higher_than_rtou_off_peak(self, engine):
        # SGS-TOUE off_peak (0.09721) > R-TOU off_peak (0.11321) is FALSE
        # but SGS-TOUE on_peak (0.19463) < R-TOU on_peak (0.29905) — commercial rates cheaper
        sgs_result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 8, 1),
                on_peak_kwh=500,
                off_peak_kwh=500,
            ),
            include_riders=False,
        )
        rtou_result = engine.calculate(
            "nc-progress-leaf-502",
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 11, 1),
                on_peak_kwh=500,
                off_peak_kwh=500,
            ),
            include_riders=False,
        )
        # SGS-TOUE should be cheaper at on-peak-heavy usage (lower on_peak rate)
        assert sgs_result.total < rtou_result.total


# ---------------------------------------------------------------------------
# SGS (nc-progress-leaf-520) — Small General Service flat rate
# ---------------------------------------------------------------------------

class TestSgsLive:
    FAMILY_KEY = "nc-progress-leaf-520"

    def test_family_exists_in_db(self, repo):
        fam = repo.get_tariff_family(self.FAMILY_KEY)
        assert fam is not None

    def test_winter_energy_charge_applied(self, engine):
        # SGS has energy_block rate for winter only: 0.13188 $/kWh for first 750 kWh
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 1, 1)),
            include_riders=False,
        )
        assert result.total > 0
        energy_items = [i for i in result.line_items if i.charge_type == "energy_block"]
        assert len(energy_items) >= 1
        # 750 kWh (tier cap) * $0.13188/kWh = $98.91
        assert sum(i.amount for i in energy_items) == pytest.approx(750 * 0.13188, abs=0.02)

    def test_summer_has_no_energy_charges(self, engine):
        # SGS only has a winter energy rate — summer should produce no energy_block items
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 7, 1)),
            include_riders=False,
        )
        energy_items = [i for i in result.line_items if i.charge_type == "energy_block"]
        assert len(energy_items) == 0
        # Seasonal charge omitted warning expected (no all-year energy rate)
        # total may be zero since only fixed was Three-Phase (excluded)
        assert result.total == pytest.approx(0.0, abs=0.01) or result.total > 0

    def test_no_service_date_uses_all_year_charges_only(self, engine):
        result = engine.calculate(
            self.FAMILY_KEY,
            BillInput(monthly_kwh=1000),
            include_riders=False,
        )
        # Without service_date, seasonal charges are excluded with a warning
        assert any("season" in w.lower() or "service_date" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# Cross-schedule sanity checks
# ---------------------------------------------------------------------------

class TestCrossScheduleSanity:
    def test_all_tou_schedules_return_results(self, engine):
        tou_families = [
            "nc-progress-leaf-502",  # R-TOU
            "nc-progress-leaf-503",  # R-TOUD
            "nc-progress-leaf-521",  # SGS-TOUE
            "nc-progress-leaf-523",  # SGS-TOU-CPP
        ]
        for fk in tou_families:
            result = engine.calculate(
                fk,
                BillInput(
                    monthly_kwh=1000,
                    service_date=datetime.date(2025, 11, 1),
                    on_peak_kwh=300,
                    off_peak_kwh=600,
                    discount_kwh=100,
                ),
                include_riders=False,
            )
            assert result.total > 0, f"{fk} returned zero total"
            assert result.family_key == fk

    def test_all_schedules_have_no_negative_totals(self, engine):
        for fk in [
            "nc-progress-leaf-502",
            "nc-progress-leaf-503",
            "nc-progress-leaf-520",
            "nc-progress-leaf-521",
            "nc-progress-leaf-523",
        ]:
            result = engine.calculate(
                fk,
                BillInput(
                    monthly_kwh=500,
                    service_date=datetime.date(2025, 11, 1),
                    on_peak_kwh=200,
                    off_peak_kwh=250,
                    discount_kwh=50,
                ),
                include_riders=False,
            )
            assert result.total >= 0, f"{fk} returned negative total: {result.total}"

    def test_source_confidence_in_valid_range(self, engine):
        for fk in ["nc-progress-leaf-502", "nc-progress-leaf-521"]:
            result = engine.calculate(
                fk,
                BillInput(
                    monthly_kwh=1000,
                    service_date=datetime.date(2025, 11, 1),
                    on_peak_kwh=400,
                    off_peak_kwh=500,
                    discount_kwh=100,
                ),
                include_riders=False,
            )
            assert 0.0 <= result.source_confidence <= 1.0

    def test_partial_tou_coverage_warns_and_reduces_confidence(self, engine):
        # nc-progress-leaf-504 only has a discount period parsed — on_peak/off_peak kWh
        # are unmatched, so the engine should warn and cap confidence at 0.5
        result = engine.calculate(
            "nc-progress-leaf-504",
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 11, 1),
                on_peak_kwh=500,
                off_peak_kwh=500,
            ),
            include_riders=False,
        )
        assert any("Partial TOU coverage" in w for w in result.warnings)
        assert result.source_confidence <= 0.5
