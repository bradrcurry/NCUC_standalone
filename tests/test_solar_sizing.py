"""Tests for the solar PV sizing and ROI estimator."""
from __future__ import annotations

import datetime
import sqlite3

import pytest

from duke_rates.billing.espi_parser import MonthlyUsageSummary, UsageProfile
from duke_rates.billing.solar_sizing import (
    SolarMonth,
    SolarSizingResult,
    _get_cf_table,
    _monthly_generation,
    _proportional_net_usage,
    size_solar_system,
    sweep_system_sizes,
    _NC_MONTHLY_CF,
    _DEFAULT_DERATE,
    _DEFAULT_AVOIDED_COST,
)
from duke_rates.billing.tariff_engine import BillInput, TariffBillingEngine
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.tariff import (
    TariffChargeRecord,
    TariffFamilyRecord,
    TariffVersionRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()
    return path


@pytest.fixture
def flat_repo(db_path):
    """In-memory repo seeded with a simple flat-rate RES schedule. No riders.

    Fixed: $14/month
    Energy: $0.12/kWh flat (all seasons, no tiers)
    """
    repo = Repository(str(db_path))
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="solar-test-flat",
        state="NC", company="progress",
        tariff_identifier="flat-test",
        schedule_code="RES",
        family_type="rate_schedule",
        title="Test Flat Rate",
    ))
    vid = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="solar-test-flat",
        effective_start="2024-01-01",
        revision_label="Test v1",
        source_type="utility_current",
        confidence_score=1.0,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=vid, family_key="solar-test-flat",
        charge_type="fixed", charge_label="Customer Charge",
        rate_value=14.00, rate_unit="$/month",
        season="all_year", confidence_score=1.0,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=vid, family_key="solar-test-flat",
        charge_type="energy_block", charge_label="Energy Charge",
        rate_value=12.0, rate_unit="cents/kWh",
        tier_min=0.0, tier_max=None, season="all_year", confidence_score=1.0,
    ))
    return repo


@pytest.fixture
def tou_repo(db_path):
    """In-memory repo seeded with a TOU schedule.

    Fixed: $14/month
    on_peak: $0.30/kWh, off_peak: $0.11/kWh, discount: $0.07/kWh
    """
    repo = Repository(str(db_path))
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="solar-test-tou",
        state="NC", company="progress",
        tariff_identifier="tou-test",
        schedule_code="R-TOU",
        family_type="rate_schedule",
        title="Test TOU Rate",
    ))
    vid = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="solar-test-tou",
        effective_start="2024-01-01",
        revision_label="Test TOU v1",
        source_type="utility_current",
        confidence_score=1.0,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=vid, family_key="solar-test-tou",
        charge_type="fixed", charge_label="Customer Charge",
        rate_value=14.00, rate_unit="$/month",
        season="all_year", confidence_score=1.0,
    ))
    for period, rate in [("on_peak", 30.0), ("off_peak", 11.0), ("discount", 7.0)]:
        repo.upsert_tariff_charge(TariffChargeRecord(
            version_id=vid, family_key="solar-test-tou",
            charge_type="tou_energy", charge_label=f"Energy {period}",
            rate_value=rate, rate_unit="cents/kWh",
            tou_period=period, season="all_year", confidence_score=1.0,
        ))
    return repo


def _make_profile(months=12, kwh_per_month=1000.0, on_pct=0.30,
                  off_pct=0.50, disc_pct=0.20) -> UsageProfile:
    """Build a UsageProfile with uniform monthly usage starting 2025-01."""
    month_list = []
    for i in range(months):
        y, mo = divmod(i, 12)
        year = 2025 + y
        month = mo + 1
        on_kwh = round(kwh_per_month * on_pct, 2)
        off_kwh = round(kwh_per_month * off_pct, 2)
        disc_kwh = round(kwh_per_month * disc_pct, 2)
        total = on_kwh + off_kwh + disc_kwh
        month_list.append(MonthlyUsageSummary(
            year=year, month=month,
            total_kwh=total,
            on_peak_kwh=on_kwh,
            off_peak_kwh=off_kwh,
            discount_kwh=disc_kwh,
            peak_kw=5.0,
        ))
    return UsageProfile(months=month_list, total_kwh=sum(m.total_kwh for m in month_list))


# ---------------------------------------------------------------------------
# Group 1: Generation math
# ---------------------------------------------------------------------------

class TestGenerationMath:
    def test_nc_cf_table_has_all_12_months(self):
        cf = _get_cf_table("NC")
        assert set(cf.keys()) == set(range(1, 13))

    def test_generation_scales_with_system_kw(self):
        cf = _get_cf_table("NC")
        gen_2kw = _monthly_generation(2.0, 7, _DEFAULT_DERATE, cf)
        gen_4kw = _monthly_generation(4.0, 7, _DEFAULT_DERATE, cf)
        assert pytest.approx(gen_4kw, rel=1e-6) == gen_2kw * 2

    def test_july_higher_than_january(self):
        cf = _get_cf_table("NC")
        gen_jan = _monthly_generation(5.0, 1, _DEFAULT_DERATE, cf)
        gen_jul = _monthly_generation(5.0, 7, _DEFAULT_DERATE, cf)
        assert gen_jul > gen_jan

    def test_derate_applied(self):
        cf = _get_cf_table("NC")
        gen_half = _monthly_generation(5.0, 6, 0.5, cf)
        gen_full = _monthly_generation(5.0, 6, 1.0, cf)
        assert pytest.approx(gen_half, rel=1e-6) == gen_full * 0.5

    def test_invalid_location_raises(self):
        with pytest.raises(ValueError, match="Unsupported location"):
            _get_cf_table("TX")

    def test_location_case_insensitive(self):
        cf_upper = _get_cf_table("NC")
        cf_lower = _get_cf_table("nc")
        assert cf_upper == cf_lower


# ---------------------------------------------------------------------------
# Group 2: Net usage and export
# ---------------------------------------------------------------------------

class TestNetUsageAndExport:
    def test_no_export_small_system(self):
        """A 1 kW system never exports against a 1000 kWh/month profile."""
        m = MonthlyUsageSummary(year=2025, month=7, total_kwh=1000.0,
                                on_peak_kwh=300.0, off_peak_kwh=500.0,
                                discount_kwh=200.0, peak_kw=5.0)
        # Max July generation for 1 kW: 130 * 0.8 = 104 kWh << 1000 kWh
        gen = _monthly_generation(1.0, 7, _DEFAULT_DERATE, _NC_MONTHLY_CF)
        net_total, net_on, net_off, net_disc = _proportional_net_usage(m, gen)
        export = max(0.0, gen - m.total_kwh)
        assert export == 0.0
        assert net_total > 0

    def test_export_when_oversized(self):
        """A 30 kW system against a 500 kWh/month profile exports in July."""
        m = MonthlyUsageSummary(year=2025, month=7, total_kwh=500.0,
                                on_peak_kwh=150.0, off_peak_kwh=250.0,
                                discount_kwh=100.0, peak_kw=3.0)
        gen = _monthly_generation(30.0, 7, _DEFAULT_DERATE, _NC_MONTHLY_CF)
        export = max(0.0, gen - m.total_kwh)
        assert export > 0

    def test_net_usage_never_negative(self):
        """_proportional_net_usage clamps all period fields at zero."""
        m = MonthlyUsageSummary(year=2025, month=6, total_kwh=100.0,
                                on_peak_kwh=30.0, off_peak_kwh=50.0,
                                discount_kwh=20.0, peak_kw=1.0)
        net_total, net_on, net_off, net_disc = _proportional_net_usage(m, 999.0)
        assert net_on >= 0.0
        assert net_off >= 0.0
        assert net_disc >= 0.0
        assert net_total >= 0.0

    def test_tou_splits_sum_to_net_total(self):
        """After offset, on+off+disc sums to net_total within floating-point tolerance."""
        m = MonthlyUsageSummary(year=2025, month=8, total_kwh=1000.0,
                                on_peak_kwh=300.0, off_peak_kwh=500.0,
                                discount_kwh=200.0, peak_kw=5.0)
        offset = 200.0
        net_total, net_on, net_off, net_disc = _proportional_net_usage(m, offset)
        assert abs((net_on + net_off + net_disc) - net_total) < 1e-9

    def test_proportional_offset_fractions(self):
        """Manual verification: 50% on-peak usage → 50% of offset from on-peak."""
        m = MonthlyUsageSummary(year=2025, month=5, total_kwh=100.0,
                                on_peak_kwh=50.0, off_peak_kwh=30.0,
                                discount_kwh=20.0, peak_kw=2.0)
        offset = 10.0
        _, net_on, net_off, net_disc = _proportional_net_usage(m, offset)
        assert pytest.approx(net_on, abs=1e-9) == 50.0 - 10.0 * 0.50
        assert pytest.approx(net_off, abs=1e-9) == 30.0 - 10.0 * 0.30
        assert pytest.approx(net_disc, abs=1e-9) == 20.0 - 10.0 * 0.20

    def test_zero_usage_month_returns_zeros(self):
        m = MonthlyUsageSummary(year=2025, month=1, total_kwh=0.0,
                                on_peak_kwh=0.0, off_peak_kwh=0.0,
                                discount_kwh=0.0, peak_kw=0.0)
        net_total, net_on, net_off, net_disc = _proportional_net_usage(m, 50.0)
        assert net_total == 0.0 and net_on == 0.0 and net_off == 0.0 and net_disc == 0.0


# ---------------------------------------------------------------------------
# Group 3: Bill calculation via size_solar_system
# ---------------------------------------------------------------------------

class TestBillCalculation:
    def test_bill_with_lower_than_or_equal_to_without(self, flat_repo):
        """Solar always reduces or maintains the bill (generation > 0)."""
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1000.0)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=5.0)
        for sm in result.months:
            assert sm.bill_with <= sm.bill_without + 1e-6, (
                f"{sm.year}-{sm.month:02d}: bill_with {sm.bill_with:.4f} "
                f"> bill_without {sm.bill_without:.4f}"
            )

    def test_savings_equals_difference(self, flat_repo):
        """sm.savings == sm.bill_without - sm.bill_with for every month."""
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=800.0)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=4.0)
        for sm in result.months:
            assert pytest.approx(sm.savings, abs=1e-4) == sm.bill_without - sm.bill_with

    def test_flat_rate_bill_without_math(self, flat_repo):
        """Verify bill_without matches direct tariff engine output."""
        engine = TariffBillingEngine(flat_repo)
        kwh = 1000.0
        profile = _make_profile(months=1, kwh_per_month=kwh)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=3.0)
        sm = result.months[0]
        # Direct calculation: $14 fixed + 1000 * $0.12 = $134
        assert pytest.approx(sm.bill_without, abs=0.02) == 14.0 + kwh * 0.12

    def test_tou_proportional_savings(self, tou_repo):
        """On-peak savings are higher per kWh because on-peak rate is highest."""
        engine = TariffBillingEngine(tou_repo)
        # High on-peak profile (60%) vs low on-peak (10%) same total kWh
        profile_high = _make_profile(months=1, kwh_per_month=1000.0,
                                     on_pct=0.60, off_pct=0.30, disc_pct=0.10)
        profile_low  = _make_profile(months=1, kwh_per_month=1000.0,
                                     on_pct=0.10, off_pct=0.70, disc_pct=0.20)
        res_high = size_solar_system(profile_high, "solar-test-tou", engine, system_kw=4.0)
        res_low  = size_solar_system(profile_low,  "solar-test-tou", engine, system_kw=4.0)
        # High on-peak profile should save more per kWh offset
        assert res_high.annual_savings > res_low.annual_savings


# ---------------------------------------------------------------------------
# Group 4: Annual aggregation
# ---------------------------------------------------------------------------

class TestAnnualAggregation:
    def test_annual_generation_equals_sum_of_months(self, flat_repo):
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1000.0)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=6.0)
        assert pytest.approx(result.annual_generation_kwh, abs=0.01) == \
               sum(sm.generation_kwh for sm in result.months)

    def test_annual_savings_equals_sum_of_months(self, flat_repo):
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1000.0)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=6.0)
        assert pytest.approx(result.annual_savings, abs=0.02) == \
               sum(sm.savings for sm in result.months)

    def test_payback_none_without_cost(self, flat_repo):
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1000.0)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=5.0,
                                   cost_per_watt=None)
        assert result.cost_dollars is None
        assert result.payback_years is None

    def test_payback_calculation(self, flat_repo):
        """6 kW at $3.50/W = $21,000. Payback = 21000 / annual_savings."""
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1000.0)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=6.0,
                                   cost_per_watt=3.50)
        expected_cost = 6.0 * 1000 * 3.50  # $21,000
        assert pytest.approx(result.cost_dollars, abs=0.01) == expected_cost
        if result.annual_savings > 0:
            assert pytest.approx(result.payback_years, abs=0.1) == \
                   expected_cost / result.annual_savings

    def test_months_count_matches_profile(self, flat_repo):
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=7, kwh_per_month=500.0)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=4.0)
        assert len(result.months) == 7

    def test_larger_system_generates_more(self, flat_repo):
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1000.0)
        r4 = size_solar_system(profile, "solar-test-flat", engine, system_kw=4.0)
        r8 = size_solar_system(profile, "solar-test-flat", engine, system_kw=8.0)
        assert r8.annual_generation_kwh > r4.annual_generation_kwh


# ---------------------------------------------------------------------------
# Group 5: Sweep
# ---------------------------------------------------------------------------

class TestSweep:
    def test_sweep_returns_ascending_sizes(self, flat_repo):
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1000.0)
        results = sweep_system_sizes(profile, "solar-test-flat", engine,
                                     sizes=[8, 4, 2, 6])
        assert [r.system_kw for r in results] == [2.0, 4.0, 6.0, 8.0]

    def test_sweep_savings_non_decreasing(self, flat_repo):
        """Larger systems always save at least as much as smaller ones."""
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1200.0)
        results = sweep_system_sizes(profile, "solar-test-flat", engine,
                                     sizes=list(range(2, 13)))
        for i in range(1, len(results)):
            assert results[i].annual_savings >= results[i - 1].annual_savings - 0.01

    def test_sweep_marginal_savings_diminish(self, flat_repo):
        """Marginal savings per additional kW shrinks as system grows past load."""
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=800.0)
        results = sweep_system_sizes(profile, "solar-test-flat", engine,
                                     sizes=[3, 6, 9, 12])
        marginals = [
            results[i].annual_savings - results[i - 1].annual_savings
            for i in range(1, len(results))
        ]
        # Marginal savings from 3→6 should exceed 9→12 (diminishing returns as oversizing increases)
        assert marginals[0] >= marginals[-1] - 0.01

    def test_sweep_custom_sizes(self, flat_repo):
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1000.0)
        results = sweep_system_sizes(profile, "solar-test-flat", engine,
                                     sizes=[4.0, 8.0])
        assert len(results) == 2
        assert results[0].system_kw == 4.0
        assert results[1].system_kw == 8.0

    def test_sweep_default_sizes_2_to_16(self, flat_repo):
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1000.0)
        results = sweep_system_sizes(profile, "solar-test-flat", engine)
        assert len(results) == 15  # 2 through 16 inclusive
        assert results[0].system_kw == 2.0
        assert results[-1].system_kw == 16.0


# ---------------------------------------------------------------------------
# Group 6: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_zero_usage_month_no_crash(self, flat_repo):
        """A month with zero usage produces savings=0 without divide-by-zero."""
        engine = TariffBillingEngine(flat_repo)
        m_zero = MonthlyUsageSummary(year=2025, month=3, total_kwh=0.0,
                                     on_peak_kwh=0.0, off_peak_kwh=0.0,
                                     discount_kwh=0.0, peak_kw=0.0)
        profile = UsageProfile(months=[m_zero], total_kwh=0.0)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=4.0)
        assert len(result.months) == 1
        # Can't save more than the fixed charge (solar can't eliminate fixed charge)
        sm = result.months[0]
        assert sm.savings >= 0.0

    def test_partial_profile_3_months(self, flat_repo):
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=3, kwh_per_month=900.0)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=5.0)
        assert len(result.months) == 3

    def test_derate_zero_means_no_generation(self, flat_repo):
        """derate=0.0 produces zero generation and zero savings."""
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=12, kwh_per_month=1000.0)
        result = size_solar_system(profile, "solar-test-flat", engine,
                                   system_kw=8.0, derate=0.0)
        assert result.annual_generation_kwh == 0.0
        assert result.annual_savings == 0.0

    def test_no_tou_breakdown_uses_total(self, flat_repo):
        """Profile with no TOU breakdown (all zeros) — flat rate should still work."""
        engine = TariffBillingEngine(flat_repo)
        # on/off/discount all zero but total_kwh is non-zero
        m = MonthlyUsageSummary(year=2025, month=6, total_kwh=500.0,
                                on_peak_kwh=0.0, off_peak_kwh=0.0,
                                discount_kwh=0.0, peak_kw=3.0)
        profile = UsageProfile(months=[m], total_kwh=500.0)
        result = size_solar_system(profile, "solar-test-flat", engine, system_kw=4.0)
        sm = result.months[0]
        # Flat rate: bill_without = 14 + 500*0.12 = $74
        assert pytest.approx(sm.bill_without, abs=0.05) == 14.0 + 500.0 * 0.12
        assert sm.savings >= 0.0

    def test_invalid_location_raises_in_size_function(self, flat_repo):
        engine = TariffBillingEngine(flat_repo)
        profile = _make_profile(months=1, kwh_per_month=1000.0)
        with pytest.raises(ValueError, match="Unsupported location"):
            size_solar_system(profile, "solar-test-flat", engine,
                              system_kw=5.0, location="TX")
