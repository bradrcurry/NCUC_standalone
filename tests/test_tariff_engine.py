"""Tests for TariffBillingEngine (Phase 4a billing from tariff_charges tables)."""
from __future__ import annotations

import datetime
import sqlite3

import pytest

from duke_rates.billing.tariff_engine import (
    BillInput,
    BillLineItem,
    TariffBillingEngine,
    _season_for_date,
    _select_version,
    validate_rider_total,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.tariff import (
    RiderApplicabilityRecord,
    TariffChargeRecord,
    TariffFamilyRecord,
    TariffVersionRecord,
)

# ---------------------------------------------------------------------------
# Fixture: in-memory database seeded with test data
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
def repo(db_path):
    return Repository(str(db_path))


@pytest.fixture
def seeded_repo(repo):
    """Repository pre-seeded with a RES-like rate schedule and a simple rider."""
    # Tariff family: leaf-500 (RES)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="test-leaf-500",
            state="NC",
            company="progress",
            tariff_identifier="leaf-500",
            schedule_code="RES",
            family_type="rate_schedule",
            title="Residential Service Schedule RES",
        )
    )
    # Tariff version
    version_id = repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key="test-leaf-500",
            effective_start="2025-10-01",
            revision_label="NC Second Revised Leaf No. 500",
            source_type="utility_current",
            confidence_score=0.95,
        )
    )
    # Charges: fixed + summer flat + winter tiered
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=version_id, family_key="test-leaf-500",
        charge_type="fixed", charge_label="Basic Customer Charge",
        rate_value=14.00, rate_unit="$/month",
        season="all_year", confidence_score=0.95,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=version_id, family_key="test-leaf-500",
        charge_type="energy_block", charge_label="Energy Charge - Summer",
        rate_value=12.623, rate_unit="cents/kWh",
        tier_min=0.0, tier_max=None, season="summer", confidence_score=0.90,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=version_id, family_key="test-leaf-500",
        charge_type="energy_block", charge_label="Energy Charge - Winter (first block)",
        rate_value=12.623, rate_unit="cents/kWh",
        tier_min=0.0, tier_max=800.0, season="winter", confidence_score=0.90,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=version_id, family_key="test-leaf-500",
        charge_type="energy_block", charge_label="Energy Charge - Winter (additional)",
        rate_value=11.623, rate_unit="cents/kWh",
        tier_min=800.0, tier_max=None, season="winter", confidence_score=0.90,
    ))

    # Simple rider: leaf-601 with residential net adjustment
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="test-leaf-601",
            state="NC", company="progress",
            tariff_identifier="leaf-601",
            schedule_code="BA",
            family_type="rider",
            title="Rider BA",
        )
    )
    rider_version_id = repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key="test-leaf-601",
            effective_start="2026-01-01",
            revision_label="NC Sixth Revised Leaf No. 601",
            source_type="utility_current",
            confidence_score=0.80,
        )
    )
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=rider_version_id, family_key="test-leaf-601",
        charge_type="adjustment", charge_label="Billing Adjustment - Residential",
        rate_value=1.549, rate_unit="cents/kWh",
        customer_class="residential", confidence_score=0.80,
    ))
    # Rider applicability link
    repo.upsert_rider_applicability(RiderApplicabilityRecord(
        rider_family_key="test-leaf-601",
        applies_to_family_key="test-leaf-500",
        mandatory=True,
        source_type="tariff_text",
        confidence_score=0.90,
    ))

    return repo


# ---------------------------------------------------------------------------
# Season detection tests
# ---------------------------------------------------------------------------

class TestSeasonDetection:
    def test_august_is_summer(self):
        assert _season_for_date(datetime.date(2025, 8, 1)) == "summer"

    def test_january_is_winter(self):
        assert _season_for_date(datetime.date(2025, 1, 15)) == "winter"

    def test_may_is_summer(self):
        assert _season_for_date(datetime.date(2025, 5, 1)) == "summer"

    def test_october_is_winter(self):
        assert _season_for_date(datetime.date(2025, 10, 1)) == "winter"

    def test_none_returns_none(self):
        assert _season_for_date(None) is None


# ---------------------------------------------------------------------------
# Version selection tests
# ---------------------------------------------------------------------------

class TestVersionSelection:
    def _v(self, eff):
        v = TariffVersionRecord(family_key="x", source_type="utility_current")
        v.effective_start = eff
        return v

    def test_selects_most_recent_before_date(self):
        versions = [self._v("2024-01-01"), self._v("2025-10-01"), self._v("2026-01-01")]
        v = _select_version(versions, datetime.date(2025, 12, 1))
        assert v.effective_start == "2025-10-01"

    def test_selects_only_eligible(self):
        versions = [self._v("2025-10-01"), self._v("2026-01-01")]
        v = _select_version(versions, datetime.date(2025, 11, 1))
        assert v.effective_start == "2025-10-01"

    def test_future_only_returns_earliest(self):
        versions = [self._v("2026-01-01"), self._v("2027-01-01")]
        v = _select_version(versions, datetime.date(2025, 1, 1))
        assert v.effective_start == "2026-01-01"

    def test_empty_returns_none(self):
        assert _select_version([], datetime.date(2025, 1, 1)) is None

    def test_undated_returns_first(self):
        v1 = TariffVersionRecord(family_key="x", source_type="utility_current")
        v2 = TariffVersionRecord(family_key="x", source_type="utility_current")
        result = _select_version([v1, v2], datetime.date(2025, 1, 1))
        assert result is v1


# ---------------------------------------------------------------------------
# Core billing calculation tests
# ---------------------------------------------------------------------------

class TestBillingEngine:
    def test_summer_flat_rate(self, seeded_repo):
        engine = TariffBillingEngine(seeded_repo)
        result = engine.calculate(
            "test-leaf-500",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
        )
        # Fixed: $14.00
        # Energy: 1000 * 0.12623 = $126.23
        # Rider BA: 1000 * 0.01549 = $15.49
        assert result.total == pytest.approx(14.00 + 126.23 + 15.49, abs=0.02)

    def test_winter_tiered_rate_first_block(self, seeded_repo):
        engine = TariffBillingEngine(seeded_repo)
        result = engine.calculate(
            "test-leaf-500",
            BillInput(monthly_kwh=500, service_date=datetime.date(2025, 1, 15)),
        )
        # Fixed: $14
        # Energy: 500 * 0.12623 = $63.12 (all in first block ≤800)
        # Rider BA: 500 * 0.01549 = $7.75
        assert result.total == pytest.approx(14.00 + 63.115 + 7.745, abs=0.02)

    def test_winter_tiered_rate_spans_blocks(self, seeded_repo):
        engine = TariffBillingEngine(seeded_repo)
        result = engine.calculate(
            "test-leaf-500",
            BillInput(monthly_kwh=1200, service_date=datetime.date(2025, 1, 15)),
        )
        # Fixed: $14.00
        # Block 1: 800 * 0.12623 = $100.98
        # Block 2: 400 * 0.11623 = $46.49
        # Rider BA: 1200 * 0.01549 = $18.59
        expected = 14.00 + 100.984 + 46.492 + 18.588
        assert result.total == pytest.approx(expected, abs=0.02)

    def test_base_subtotal_excludes_riders(self, seeded_repo):
        engine = TariffBillingEngine(seeded_repo)
        result = engine.calculate(
            "test-leaf-500",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
        )
        assert result.base_subtotal == pytest.approx(14.00 + 126.23, abs=0.02)
        assert result.rider_subtotal == pytest.approx(15.49, abs=0.02)

    def test_no_riders_flag(self, seeded_repo):
        engine = TariffBillingEngine(seeded_repo)
        result = engine.calculate(
            "test-leaf-500",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
            include_riders=False,
        )
        assert result.rider_subtotal == 0.0
        assert result.total == result.base_subtotal

    def test_unknown_family_returns_warning(self, repo):
        engine = TariffBillingEngine(repo)
        result = engine.calculate(
            "nonexistent-leaf-999",
            BillInput(monthly_kwh=1000),
        )
        assert result.total == 0.0
        assert any("not found" in w for w in result.warnings)

    def test_no_service_date_warning(self, seeded_repo):
        engine = TariffBillingEngine(seeded_repo)
        result = engine.calculate(
            "test-leaf-500",
            BillInput(monthly_kwh=1000),  # no service_date
        )
        assert any("seasonal" in w.lower() or "service_date" in w.lower() for w in result.warnings)

    def test_result_includes_family_title(self, seeded_repo):
        engine = TariffBillingEngine(seeded_repo)
        result = engine.calculate(
            "test-leaf-500",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
        )
        assert result.schedule_title == "Residential Service Schedule RES"

    def test_result_includes_revision_label(self, seeded_repo):
        engine = TariffBillingEngine(seeded_repo)
        result = engine.calculate(
            "test-leaf-500",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2026, 2, 1)),
        )
        assert result.revision_label == "NC Second Revised Leaf No. 500"

    def test_line_items_have_correct_sources(self, seeded_repo):
        engine = TariffBillingEngine(seeded_repo)
        result = engine.calculate(
            "test-leaf-500",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
        )
        sources = {item.source for item in result.line_items}
        assert "test-leaf-500" in sources
        assert "test-leaf-601" in sources  # rider


# ---------------------------------------------------------------------------
# TOU billing tests
# ---------------------------------------------------------------------------

@pytest.fixture
def tou_repo(db_path):
    """Repository seeded with a TOU schedule."""
    repo = Repository(str(db_path))
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="test-leaf-502",
        state="NC", company="progress",
        tariff_identifier="leaf-502",
        schedule_code="R_TOU",
        family_type="rate_schedule",
        title="Residential Service Time-of-Use Schedule R-TOU",
    ))
    version_id = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="test-leaf-502",
        effective_start="2025-10-01",
        source_type="utility_current",
        confidence_score=0.90,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=version_id, family_key="test-leaf-502",
        charge_type="fixed", charge_label="Basic Customer Charge",
        rate_value=14.00, rate_unit="$/month", season="all_year", confidence_score=0.95,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=version_id, family_key="test-leaf-502",
        charge_type="tou_energy", charge_label="Energy Charge - On-Peak",
        rate_value=29.905, rate_unit="cents/kWh",
        tou_period="on_peak", season="all_year", confidence_score=0.90,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=version_id, family_key="test-leaf-502",
        charge_type="tou_energy", charge_label="Energy Charge - Off-Peak",
        rate_value=11.321, rate_unit="cents/kWh",
        tou_period="off_peak", season="all_year", confidence_score=0.90,
    ))
    return repo


class TestTouBilling:
    def test_tou_with_period_breakdown(self, tou_repo):
        engine = TariffBillingEngine(tou_repo)
        result = engine.calculate(
            "test-leaf-502",
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 8, 1),
                on_peak_kwh=300,
                off_peak_kwh=700,
            ),
            include_riders=False,
        )
        # Fixed: $14
        # On-peak: 300 * 0.29905 = $89.72
        # Off-peak: 700 * 0.11321 = $79.25
        assert result.total == pytest.approx(14.00 + 89.715 + 79.247, abs=0.02)

    def test_tou_without_breakdown_uses_average(self, tou_repo):
        engine = TariffBillingEngine(tou_repo)
        result = engine.calculate(
            "test-leaf-502",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
            include_riders=False,
        )
        assert result.total > 0
        assert any("TOU" in w or "average" in w for w in result.warnings)

    def test_tou_line_items_have_correct_type(self, tou_repo):
        engine = TariffBillingEngine(tou_repo)
        result = engine.calculate(
            "test-leaf-502",
            BillInput(
                monthly_kwh=1000,
                service_date=datetime.date(2025, 8, 1),
                on_peak_kwh=300,
                off_peak_kwh=700,
            ),
            include_riders=False,
        )
        types = {item.charge_type for item in result.line_items}
        assert "tou_energy" in types
        assert "fixed" in types


# ---------------------------------------------------------------------------
# Demand billing tests
# ---------------------------------------------------------------------------

@pytest.fixture
def demand_repo(db_path):
    """Repository seeded with a demand-metered schedule."""
    repo = Repository(str(db_path))
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="test-leaf-520",
        state="NC", company="progress",
        tariff_identifier="leaf-520",
        schedule_code="SGS",
        family_type="rate_schedule",
        title="Small General Service Schedule SGS",
    ))
    version_id = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="test-leaf-520",
        effective_start="2025-10-01",
        source_type="utility_current",
        confidence_score=0.90,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=version_id, family_key="test-leaf-520",
        charge_type="fixed", charge_label="Basic Customer Charge",
        rate_value=22.00, rate_unit="$/month", season="all_year", confidence_score=0.95,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=version_id, family_key="test-leaf-520",
        charge_type="demand", charge_label="Demand Charge",
        rate_value=10.50, rate_unit="$/kW", season="all_year", confidence_score=0.85,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=version_id, family_key="test-leaf-520",
        charge_type="energy_block", charge_label="Energy Charge",
        rate_value=6.543, rate_unit="cents/kWh",
        tier_min=0.0, season="all_year", confidence_score=0.85,
    ))
    return repo


class TestDemandBilling:
    def test_demand_charge_with_peak_kw(self, demand_repo):
        engine = TariffBillingEngine(demand_repo)
        result = engine.calculate(
            "test-leaf-520",
            BillInput(monthly_kwh=5000, peak_kw=20.0, service_date=datetime.date(2025, 8, 1)),
            include_riders=False,
        )
        # Fixed: $22
        # Demand: 20 * $10.50 = $210
        # Energy: 5000 * 0.06543 = $327.15
        assert result.total == pytest.approx(22.00 + 210.00 + 327.15, abs=0.02)

    def test_demand_omitted_without_peak_kw(self, demand_repo):
        engine = TariffBillingEngine(demand_repo)
        result = engine.calculate(
            "test-leaf-520",
            BillInput(monthly_kwh=5000, service_date=datetime.date(2025, 8, 1)),
            include_riders=False,
        )
        demand_items = [i for i in result.line_items if i.charge_type == "demand"]
        assert demand_items == []
        assert any("peak_kw" in w or "demand" in w.lower() for w in result.warnings)


@pytest.fixture
def multi_demand_repo(db_path):
    repo = Repository(str(db_path))
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="test-gsdt-1",
        state="FL",
        company="florida",
        tariff_identifier="pe-GSDT-1",
        schedule_code="GSDT-1",
        family_type="rate_schedule",
        title="General Service - Demand Optional Time of Use",
    ))
    version_id = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="test-gsdt-1",
        effective_start="2026-01-01",
        source_type="utility_current",
        confidence_score=0.9,
    ))
    for label, value in [
        ("Customer Charge (Secondary Metering Voltage)", 18.47),
        ("Customer Charge (Primary Metering Voltage)", 233.39),
    ]:
        repo.upsert_tariff_charge(TariffChargeRecord(
            version_id=version_id,
            family_key="test-gsdt-1",
            charge_type="fixed",
            charge_label=label,
            rate_value=value,
            rate_unit="$/month",
            season="all_year",
            customer_class="secondary" if "Secondary" in label else "primary",
            confidence_score=0.9,
        ))
    for label, value in [
        ("Base Demand Charge", 2.81),
        ("Mid-Peak Demand Charge", 3.98),
        ("On-Peak Demand Charge", 2.20),
    ]:
        repo.upsert_tariff_charge(TariffChargeRecord(
            version_id=version_id,
            family_key="test-gsdt-1",
            charge_type="demand",
            charge_label=label,
            rate_value=value,
            rate_unit="$/kW",
            season="all_year",
            confidence_score=0.9,
        ))
    return repo


class TestMultiDemandBilling:
    def test_selects_matching_fixed_charge_class(self, multi_demand_repo):
        engine = TariffBillingEngine(multi_demand_repo)
        result = engine.calculate(
            "test-gsdt-1",
            BillInput(monthly_kwh=0, peak_kw=10.0, service_date=datetime.date(2026, 1, 1)),
            customer_class="primary",
            include_riders=False,
        )
        fixed = [item for item in result.line_items if item.charge_type == "fixed"]
        assert len(fixed) == 1
        assert fixed[0].amount == pytest.approx(233.39, abs=0.01)

    def test_uses_differentiated_demand_quantities(self, multi_demand_repo):
        engine = TariffBillingEngine(multi_demand_repo)
        result = engine.calculate(
            "test-gsdt-1",
            BillInput(
                monthly_kwh=0,
                peak_kw=5.0,
                base_kw=20.0,
                mid_peak_kw=10.0,
                on_peak_kw=8.0,
                service_date=datetime.date(2026, 1, 1),
            ),
            customer_class="secondary",
            include_riders=False,
        )
        demand_items = {
            item.label: item.amount
            for item in result.line_items
            if item.charge_type == "demand"
        }
        assert demand_items["Base Demand Charge"] == pytest.approx(56.20, abs=0.01)
        assert demand_items["Mid-Peak Demand Charge"] == pytest.approx(39.80, abs=0.01)
        assert demand_items["On-Peak Demand Charge"] == pytest.approx(17.60, abs=0.01)


@pytest.fixture
def rider_unit_repo(db_path):
    repo = Repository(str(db_path))
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="test-base",
        state="NC",
        company="progress",
        tariff_identifier="leaf-520",
        schedule_code="SGS",
        family_type="rate_schedule",
        title="Base Schedule",
    ))
    base_version = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="test-base",
        effective_start="2026-01-01",
        source_type="utility_current",
        confidence_score=0.9,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=base_version,
        family_key="test-base",
        charge_type="fixed",
        charge_label="Basic Customer Charge",
        rate_value=22.0,
        rate_unit="$/month",
        season="all_year",
        confidence_score=0.9,
    ))
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="test-rider",
        state="NC",
        company="progress",
        tariff_identifier="leaf-611",
        schedule_code="CAR",
        family_type="rider",
        title="Customer Assistance Recovery Rider",
    ))
    rider_version = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="test-rider",
        effective_start="2026-01-01",
        source_type="utility_current",
        confidence_score=0.9,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=rider_version,
        family_key="test-rider",
        charge_type="adjustment",
        charge_label="Customer Assistance Recovery Rider - Small General Service",
        rate_value=1.12,
        rate_unit="$/bill",
        customer_class="commercial_small",
        season="all_year",
        confidence_score=0.9,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=rider_version,
        family_key="test-rider",
        charge_type="adjustment",
        charge_label="Joint Agency Asset Rider - Medium General Service",
        rate_value=0.92,
        rate_unit="$/kW",
        customer_class="commercial_medium",
        season="all_year",
        confidence_score=0.9,
    ))
    repo.upsert_rider_applicability(RiderApplicabilityRecord(
        rider_family_key="test-rider",
        applies_to_family_key="test-base",
        mandatory=True,
        source_type="tariff_text",
        confidence_score=0.9,
    ))
    return repo


class TestRiderUnitHandling:
    def test_applies_bill_and_kw_rider_units(self, rider_unit_repo):
        engine = TariffBillingEngine(rider_unit_repo)
        result = engine.calculate(
            "test-base",
            BillInput(monthly_kwh=1000, peak_kw=7.0, service_date=datetime.date(2026, 1, 1)),
            customer_class="commercial_small",
        )
        labels = {item.label: item.amount for item in result.line_items}
        car_label = "Customer Assistance Recovery Rider - Small General Service"
        assert labels[car_label] == pytest.approx(
            1.12,
            abs=0.01,
        )

        result_kw = engine.calculate(
            "test-base",
            BillInput(monthly_kwh=1000, peak_kw=7.0, service_date=datetime.date(2026, 1, 1)),
            customer_class="commercial_medium",
        )
        labels_kw = {item.label: item.amount for item in result_kw.line_items}
        jaa_label = "Joint Agency Asset Rider - Medium General Service"
        assert labels_kw[jaa_label] == pytest.approx(
            6.44,
            abs=0.01,
        )


class TestScheduleGroupFor:
    """Unit tests for schedule_group_for() helper."""

    def test_residential_codes(self):
        from duke_rates.billing.tariff_engine import schedule_group_for
        assert schedule_group_for("RES") == "residential"
        assert schedule_group_for("R_TOU") == "residential"
        assert schedule_group_for("R_TOUD") == "residential"
        assert schedule_group_for("RS") == "residential"

    def test_sgs_codes(self):
        from duke_rates.billing.tariff_engine import schedule_group_for
        assert schedule_group_for("SGS") == "sgs"
        assert schedule_group_for("SGS_TOUE") == "sgs"
        assert schedule_group_for("SGS_TOU_CLR") == "sgs"
        assert schedule_group_for("SGS_TOU_CPP") == "sgs"

    def test_mgs_codes(self):
        from duke_rates.billing.tariff_engine import schedule_group_for
        assert schedule_group_for("MGS") == "mgs"
        assert schedule_group_for("MGS_TOU") == "mgs"

    def test_lgs_codes(self):
        from duke_rates.billing.tariff_engine import schedule_group_for
        assert schedule_group_for("LGS") == "lgs"
        assert schedule_group_for("LGS_TOU") == "lgs"
        assert schedule_group_for("LGS_HLF") == "lgs"

    def test_specialty_codes(self):
        from duke_rates.billing.tariff_engine import schedule_group_for
        assert schedule_group_for("HP") == "specialty"
        assert schedule_group_for("CH_TOUE") == "specialty"
        assert schedule_group_for("FUEL") == "specialty"

    def test_none_returns_unknown(self):
        from duke_rates.billing.tariff_engine import schedule_group_for
        assert schedule_group_for(None) == "unknown"
        assert schedule_group_for("") == "unknown"

    def test_case_insensitive(self):
        from duke_rates.billing.tariff_engine import schedule_group_for
        assert schedule_group_for("res") == "residential"
        assert schedule_group_for("Sgs") == "sgs"
        assert schedule_group_for("r_tou") == "residential"

    def test_hyphenated_variants(self):
        from duke_rates.billing.tariff_engine import schedule_group_for
        # Duke sometimes uses hyphens instead of underscores in identifiers
        assert schedule_group_for("R-TOU") == "residential"
        assert schedule_group_for("SGS-TOUE") == "sgs"


# ---------------------------------------------------------------------------
# Optional riders: enrollment_type, extra_riders, %_energy unit
# ---------------------------------------------------------------------------


@pytest.fixture
def optional_rider_repo(repo):
    """Repo with a base schedule, a mandatory rider, and an optional %_energy rider."""
    # Base rate schedule
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="opt-base", state="NC", company="progress",
        tariff_identifier="leaf-500", schedule_code="RES",
        family_type="rate_schedule", title="RES",
    ))
    base_vid = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="opt-base", effective_start="2025-01-01",
        source_type="utility_current", confidence_score=0.95,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=base_vid, family_key="opt-base",
        charge_type="fixed", charge_label="Basic Customer Charge",
        rate_value=14.00, rate_unit="$/month",
        season="all_year", confidence_score=0.95,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=base_vid, family_key="opt-base",
        charge_type="energy_block", charge_label="Energy Charge",
        rate_value=0.12, rate_unit="$/kWh",
        season="summer", customer_class="residential", confidence_score=0.95,
    ))

    # Mandatory rider
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="mand-rider", state="NC", company="progress",
        tariff_identifier="leaf-601", schedule_code="BA",
        family_type="rider", title="Rider BA",
    ))
    mand_vid = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="mand-rider", effective_start="2025-01-01",
        source_type="utility_current", confidence_score=0.90,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=mand_vid, family_key="mand-rider",
        charge_type="adjustment", charge_label="Rider BA",
        rate_value=0.01387, rate_unit="$/kWh",
        customer_class="residential", season="all_year", confidence_score=0.90,
    ))
    repo.upsert_rider_applicability(RiderApplicabilityRecord(
        rider_family_key="mand-rider", applies_to_family_key="opt-base",
        mandatory=True, enrollment_type="mandatory",
        source_type="tariff_text", confidence_score=0.90,
    ))

    # Optional rider: RECD-style %_energy discount
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="opt-recd", state="NC", company="progress",
        tariff_identifier="leaf-640", schedule_code="RECD",
        family_type="rider", title="Energy Conservation Discount RECD",
    ))
    opt_vid = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="opt-recd", effective_start="2025-01-01",
        source_type="utility_current", confidence_score=0.85,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=opt_vid, family_key="opt-recd",
        charge_type="adjustment", charge_label="Energy Conservation Discount",
        rate_value=-0.05, rate_unit="%_energy",
        customer_class="residential", season="all_year", confidence_score=0.85,
    ))
    repo.upsert_rider_applicability(RiderApplicabilityRecord(
        rider_family_key="opt-recd", applies_to_family_key="opt-base",
        mandatory=False, enrollment_type="opt_in",
        applicability_notes="5% discount for Energy Star homes.",
        source_type="tariff_text", confidence_score=0.85,
    ))

    return repo


class TestOptionalRiders:
    def test_mandatory_rider_applied_without_extra_riders(self, optional_rider_repo):
        engine = TariffBillingEngine(optional_rider_repo)
        result = engine.calculate(
            "opt-base",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
        )
        labels = {item.label for item in result.line_items}
        assert "Rider BA" in labels
        assert "Energy Conservation Discount" not in labels
        assert result.optional_riders_applied == []

    def test_optional_rider_excluded_by_default(self, optional_rider_repo):
        engine = TariffBillingEngine(optional_rider_repo)
        result = engine.calculate(
            "opt-base",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
        )
        assert "opt-recd" not in result.optional_riders_applied
        labels = {item.label for item in result.line_items}
        assert "Energy Conservation Discount" not in labels

    def test_extra_riders_includes_optional_rider(self, optional_rider_repo):
        engine = TariffBillingEngine(optional_rider_repo)
        result = engine.calculate(
            "opt-base",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
            extra_riders=["opt-recd"],
        )
        assert "opt-recd" in result.optional_riders_applied
        labels = {item.label for item in result.line_items}
        assert "Energy Conservation Discount" in labels

    def test_pct_energy_rider_reduces_total(self, optional_rider_repo):
        engine = TariffBillingEngine(optional_rider_repo)
        r_base = engine.calculate(
            "opt-base",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
        )
        r_recd = engine.calculate(
            "opt-base",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
            extra_riders=["opt-recd"],
        )
        # RECD = -5% of energy subtotal (1000 kWh × $0.12 = $120 energy; 5% = $6.00)
        assert r_recd.total < r_base.total
        assert r_recd.total == pytest.approx(r_base.total - 6.00, abs=0.01)

    def test_pct_energy_rider_applies_to_energy_subtotal_not_fixed(self, optional_rider_repo):
        engine = TariffBillingEngine(optional_rider_repo)
        r_recd = engine.calculate(
            "opt-base",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
            extra_riders=["opt-recd"],
        )
        discount_item = next(
            i for i in r_recd.line_items if i.label == "Energy Conservation Discount"
        )
        # quantity should be energy_subtotal (1000 × $0.12 = $120), NOT base_subtotal
        assert discount_item.quantity == pytest.approx(120.0, abs=0.01)
        assert discount_item.amount == pytest.approx(-6.00, abs=0.01)

    def test_optional_rider_tagged_with_enrollment_type(self, optional_rider_repo):
        engine = TariffBillingEngine(optional_rider_repo)
        result = engine.calculate(
            "opt-base",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
            extra_riders=["opt-recd"],
        )
        discount_item = next(
            i for i in result.line_items if i.label == "Energy Conservation Discount"
        )
        assert discount_item.notes is not None
        assert "optional:opt_in" in discount_item.notes

    def test_enrollment_type_stored_and_retrieved(self, optional_rider_repo):
        links = optional_rider_repo.list_rider_applicability(
            applies_to_family_key="opt-base"
        )
        by_key = {lnk.rider_family_key: lnk for lnk in links}
        assert by_key["mand-rider"].enrollment_type == "mandatory"
        assert by_key["opt-recd"].enrollment_type == "opt_in"

    def test_unknown_extra_rider_key_ignored_gracefully(self, optional_rider_repo):
        engine = TariffBillingEngine(optional_rider_repo)
        # Passing a key that has no rider_applicability link — should not crash
        result = engine.calculate(
            "opt-base",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 8, 1)),
            extra_riders=["nonexistent-rider-key"],
        )
        assert result.total > 0  # still calculates normally
        assert result.optional_riders_applied == []


# ---------------------------------------------------------------------------
# TD-V4-001: Rider total cross-check against leaf-600 summary
# ---------------------------------------------------------------------------


@pytest.fixture
def rider_check_repo(repo):
    """Repo with nc-progress-leaf-500, a rider, and a leaf-600 summary family."""
    # Base schedule (must use nc-progress- prefix for state/company parsing)
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="nc-progress-leaf-500-test",
        state="nc", company="progress",
        tariff_identifier="leaf-500-test", schedule_code="RES",
        family_type="rate_schedule", title="RES Test",
    ))
    base_vid = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="nc-progress-leaf-500-test", effective_start="2026-01-01",
        source_type="utility_current", confidence_score=0.95,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=base_vid, family_key="nc-progress-leaf-500-test",
        charge_type="fixed", charge_label="Customer Charge",
        rate_value=14.00, rate_unit="$/month",
        season="all_year", customer_class="residential", confidence_score=0.95,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=base_vid, family_key="nc-progress-leaf-500-test",
        charge_type="energy_block", charge_label="Energy Charge",
        rate_value=0.11, rate_unit="$/kWh",
        season="all_year", customer_class="residential", confidence_score=0.95,
    ))

    # Rider: 0.02097 $/kWh (matches leaf-600 Jan 2026 summary)
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="nc-progress-leaf-601-test",
        state="nc", company="progress",
        tariff_identifier="leaf-601-test", schedule_code="BA",
        family_type="rider", title="Rider BA Test",
    ))
    rider_vid = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="nc-progress-leaf-601-test", effective_start="2026-01-01",
        source_type="utility_current", confidence_score=0.90,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=rider_vid, family_key="nc-progress-leaf-601-test",
        charge_type="adjustment", charge_label="Billing Adjustment",
        rate_value=0.02097, rate_unit="$/kWh",
        customer_class="residential", confidence_score=0.90,
    ))
    repo.upsert_rider_applicability(RiderApplicabilityRecord(
        rider_family_key="nc-progress-leaf-601-test",
        applies_to_family_key="nc-progress-leaf-500-test",
        mandatory=True,
        source_type="tariff_text", confidence_score=0.90,
    ))

    # Summary family: nc-progress-leaf-600-test (used as override in test)
    # We store the adjustment_total under a real family key recognised by the module.
    # Since _RIDER_SUMMARY_FAMILY keys on "nc-progress", we use a real leaf-600 family.
    repo.upsert_tariff_family(TariffFamilyRecord(
        family_key="nc-progress-leaf-600",
        state="nc", company="progress",
        tariff_identifier="leaf-600", schedule_code="SUMMARY_OF_RIDERS",
        family_type="rider", title="Summary of Rider Adjustments",
    ))
    summary_vid = repo.upsert_tariff_version(TariffVersionRecord(
        family_key="nc-progress-leaf-600", effective_start="2026-01-01",
        source_type="manual", confidence_score=0.90,
    ))
    repo.upsert_tariff_charge(TariffChargeRecord(
        version_id=summary_vid, family_key="nc-progress-leaf-600",
        charge_type="adjustment_total",
        charge_label="Summary of Rider Adjustments - Residential Total",
        rate_value=0.02097, rate_unit="$/kWh",
        season="all_year", customer_class="residential", confidence_score=0.90,
        notes="Authoritative sum per leaf-600 PDF (Jan 2026). Used for cross-check only.",
    ))

    return repo


class TestRiderTotalValidation:
    """Tests for validate_rider_total() and its integration in _apply_riders()."""

    def test_validate_rider_total_passes_when_matching(self, rider_check_repo):
        """No warning when engine total equals leaf-600 summary."""
        items = [
            BillLineItem(
                label="Billing Adjustment", charge_type="adjustment",
                source="nc-progress-leaf-601-test",
                rate_value=0.02097, rate_unit="$/kWh",
                quantity=1000.0, amount=20.97,
            )
        ]
        result = validate_rider_total(
            rider_check_repo, "nc-progress-leaf-500-test",
            items, datetime.date(2026, 1, 15),
        )
        assert result is None

    def test_validate_rider_total_warns_on_mismatch(self, rider_check_repo):
        """Warning returned when engine total differs from leaf-600 summary."""
        items = [
            BillLineItem(
                label="Billing Adjustment", charge_type="adjustment",
                source="nc-progress-leaf-601-test",
                rate_value=0.01500, rate_unit="$/kWh",  # wrong — too low
                quantity=1000.0, amount=15.00,
            )
        ]
        warning = validate_rider_total(
            rider_check_repo, "nc-progress-leaf-500-test",
            items, datetime.date(2026, 1, 15),
        )
        assert warning is not None
        assert "mismatch" in warning.lower()
        assert "2.0970" in warning  # expected value in warning
        assert "1.5000" in warning  # engine value in warning

    def test_validate_rider_total_returns_none_for_unknown_utility(self, rider_check_repo):
        """No check emitted for utilities with no leaf-600 equivalent."""
        items = [
            BillLineItem(
                label="Rider", charge_type="adjustment",
                source="de-carolinas-leaf-100",
                rate_value=0.01000, rate_unit="$/kWh",
                quantity=1000.0, amount=10.00,
            )
        ]
        result = validate_rider_total(
            rider_check_repo, "de-carolinas-leaf-500",
            items, datetime.date(2026, 1, 15),
        )
        assert result is None

    def test_validate_rider_total_skips_bill_and_pct_items(self, rider_check_repo):
        """$/bill and %_energy items are not counted toward the per-kWh total."""
        items = [
            BillLineItem(
                label="Rider $/kWh", charge_type="adjustment",
                source="nc-progress-leaf-601-test",
                rate_value=0.02097, rate_unit="$/kWh",
                quantity=1000.0, amount=20.97,
            ),
            BillLineItem(
                label="Fixed Rider", charge_type="adjustment",
                source="nc-progress-leaf-605-test",
                rate_value=1.81, rate_unit="$/bill",
                quantity=1.0, amount=1.81,
            ),
            BillLineItem(
                label="RECD Discount", charge_type="adjustment",
                source="nc-progress-leaf-640",
                rate_value=-0.05, rate_unit="%_energy",
                quantity=100.0, amount=-5.00,
            ),
        ]
        result = validate_rider_total(
            rider_check_repo, "nc-progress-leaf-500-test",
            items, datetime.date(2026, 1, 15),
        )
        # Only the $/kWh item should be counted; total matches → no warning
        assert result is None

    def test_calculate_emits_warning_on_rider_total_mismatch(self, rider_check_repo):
        """Engine.calculate() includes rider mismatch in BillResult.warnings."""
        # Add a second rider that pushes the total away from the leaf-600 sum
        repo = rider_check_repo
        repo.upsert_tariff_family(TariffFamilyRecord(
            family_key="nc-progress-leaf-999-test",
            state="nc", company="progress",
            tariff_identifier="leaf-999-test", schedule_code="XX",
            family_type="rider", title="Extra Rider",
        ))
        extra_vid = repo.upsert_tariff_version(TariffVersionRecord(
            family_key="nc-progress-leaf-999-test", effective_start="2026-01-01",
            source_type="manual", confidence_score=0.80,
        ))
        repo.upsert_tariff_charge(TariffChargeRecord(
            version_id=extra_vid, family_key="nc-progress-leaf-999-test",
            charge_type="adjustment", charge_label="Extra Adjustment",
            rate_value=0.00500, rate_unit="$/kWh",
            customer_class="residential", confidence_score=0.80,
        ))
        repo.upsert_rider_applicability(RiderApplicabilityRecord(
            rider_family_key="nc-progress-leaf-999-test",
            applies_to_family_key="nc-progress-leaf-500-test",
            mandatory=True,
            source_type="tariff_text", confidence_score=0.80,
        ))

        engine = TariffBillingEngine(repo)
        result = engine.calculate(
            "nc-progress-leaf-500-test",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2026, 1, 15)),
        )
        mismatch_warnings = [w for w in result.warnings if "mismatch" in w.lower()]
        assert mismatch_warnings, f"Expected rider mismatch warning; got: {result.warnings}"

    def test_calculate_no_warning_when_riders_match(self, rider_check_repo):
        """No mismatch warning when engine total equals the leaf-600 summary."""
        engine = TariffBillingEngine(rider_check_repo)
        result = engine.calculate(
            "nc-progress-leaf-500-test",
            BillInput(monthly_kwh=1000, service_date=datetime.date(2026, 1, 15)),
        )
        mismatch_warnings = [w for w in result.warnings if "mismatch" in w.lower()]
        assert not mismatch_warnings, f"Unexpected mismatch warning: {mismatch_warnings}"
