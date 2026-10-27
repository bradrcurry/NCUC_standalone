"""Phase 2A billing-engine safety: version coverage and unsupported charges."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from duke_rates.billing.charge_types import (
    classify_charge,
    has_unconverted_cent_unit,
    is_potentially_billable,
)
from duke_rates.billing.tariff_engine import (
    BillInput,
    TariffBillingEngine,
    select_version_with_coverage,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.tariff import (
    RiderApplicabilityRecord,
    TariffChargeRecord,
    TariffFamilyRecord,
    TariffVersionRecord,
)


def _v(start, end=None, status="approved"):
    return TariffVersionRecord(
        family_key="x", source_type="t", effective_start=start, effective_end=end, status=status
    )


# ---------------------------------------------------------------------------
# select_version_with_coverage
# ---------------------------------------------------------------------------


class TestVersionCoverage:
    def test_missing_historical_coverage_is_not_priced(self):
        version, coverage = select_version_with_coverage(
            [_v("2025-10-01"), _v("2026-10-01")], datetime.date(2024, 6, 1)
        )
        assert (version, coverage) == (None, "not_yet_effective")

    def test_effective_date_boundary_is_inclusive(self):
        old, new = _v("2024-10-01"), _v("2025-10-01")
        assert select_version_with_coverage([old, new], datetime.date(2025, 10, 1)) == (
            new,
            "effective",
        )
        assert select_version_with_coverage([old, new], datetime.date(2025, 9, 30)) == (
            old,
            "effective",
        )

    def test_day_before_first_version_is_incomplete(self):
        assert select_version_with_coverage([_v("2024-10-01")], datetime.date(2024, 9, 30)) == (
            None,
            "not_yet_effective",
        )

    def test_overlapping_versions_pick_latest_start_on_or_before_date(self):
        broad = _v("2024-01-01", "2026-12-31")
        narrow = _v("2025-06-01")
        future = _v("2027-01-01")
        assert select_version_with_coverage([broad, narrow, future], datetime.date(2025, 7, 1)) == (
            narrow,
            "effective",
        )
        assert select_version_with_coverage(
            [broad, narrow, future], datetime.date(2025, 5, 31)
        ) == (broad, "effective")

    def test_expired_latest_version_is_flagged(self):
        ended = _v("2024-01-01", "2024-12-31")
        assert select_version_with_coverage([ended], datetime.date(2025, 3, 1)) == (
            ended,
            "expired",
        )

    def test_undated_versions_keep_fallback_but_say_so(self):
        undated = _v(None)
        assert select_version_with_coverage([undated], datetime.date(2025, 3, 1)) == (
            undated,
            "undated_fallback",
        )

    def test_future_proposed_version_does_not_rescue_missing_coverage(self):
        assert select_version_with_coverage(
            [_v("2026-07-01", status="proposed")], datetime.date(2026, 1, 1)
        ) == (None, "not_yet_effective")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    path = tmp_path / "engine.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()
    repo = Repository(path)
    for key, ftype in (("base", "rate_schedule"), ("storm", "rider"), ("late-rider", "rider")):
        repo.upsert_tariff_family(
            TariffFamilyRecord(
                family_key=key, state="NC", company="progress", family_type=ftype, title=key
            )
        )
    base = repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key="base", effective_start="2025-10-01", source_type="t", revision_label="Rev 1"
        )
    )
    charges = [
        ("fixed", "Basic Customer Charge", 14.0, "$/month", None),
        ("energy_block", "Energy Charge", 0.12, "$/kWh", None),
        # Skipped on purpose (TOTAL checksum / customer incentive): no warning.
        ("adjustment_total", "Total Rider Adjustments", 0.02, "$/kWh", None),
        ("incentive", "Load Control Credit", 25.0, "$/event", None),
        # Billable but unpriced: must be reported, never added.
        ("Fixed Monthly Charge", "Fixed Monthly Charge - Residential", 8.0, "$/month", None),
        ("energy", "Energy Charge - Standard", 0.13, "$/kWh", "standard"),
    ]
    for ctype, label, value, unit, tou in charges:
        repo.upsert_tariff_charge(
            TariffChargeRecord(
                version_id=base,
                family_key="base",
                charge_type=ctype,
                charge_label=label,
                rate_value=value,
                rate_unit=unit,
                tou_period=tou,
                customer_class="residential",
                confidence_score=0.9,
            )
        )
    storm = repo.upsert_tariff_version(
        TariffVersionRecord(family_key="storm", effective_start="2025-01-01", source_type="t")
    )
    repo.upsert_tariff_charge(
        TariffChargeRecord(
            version_id=storm,
            family_key="storm",
            charge_type="Rider Adjustment",
            charge_label="Storm Recovery Charge - Residential",
            rate_value=0.321,
            rate_unit="¢/kWh",
            customer_class="residential",
            confidence_score=0.9,
        )
    )
    repo.upsert_tariff_charge(
        TariffChargeRecord(
            version_id=storm,
            family_key="storm",
            charge_type="adjustment",
            charge_label="Storm Securitization - Residential",
            rate_value=0.246,
            rate_unit="¢/kWh",
            customer_class="residential",
            confidence_score=0.9,
        )
    )
    repo.upsert_tariff_version(
        TariffVersionRecord(family_key="late-rider", effective_start="2027-01-01", source_type="t")
    )
    for rider in ("storm", "late-rider"):
        repo.upsert_rider_applicability(
            RiderApplicabilityRecord(
                rider_family_key=rider,
                applies_to_family_key="base",
                mandatory=True,
                source_type="manual",
                in_rider_summary=False,
            )
        )
    return repo


def test_engine_refuses_to_price_service_before_first_version(repo):
    result = TariffBillingEngine(repo).calculate(
        "base", BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 9, 30))
    )
    assert result.coverage_status == "incomplete"
    assert result.total == 0.0 and result.line_items == []
    assert "Incomplete coverage" in result.warnings[0]
    assert "2025-10-01" in result.warnings[0]


def test_engine_prices_service_on_the_effective_date(repo):
    result = TariffBillingEngine(repo).calculate(
        "base",
        BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 10, 1)),
        include_riders=False,
    )
    assert result.coverage_status == "complete"
    assert result.total == pytest.approx(14.0 + 120.0)


def test_unsupported_charges_are_reported_not_billed(repo):
    result = TariffBillingEngine(repo).calculate(
        "base",
        BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 11, 1)),
        include_riders=False,
    )
    # Totals are exactly what they were before the warnings existed.
    assert result.total == pytest.approx(134.0)
    reported = {(c["charge_type"], c["category"]) for c in result.unsupported_charges}
    assert reported == {
        ("Fixed Monthly Charge", "supported_but_mismapped"),
        ("energy", "billable_unsupported"),
    }
    assert all(c["source"] == "base" for c in result.unsupported_charges)
    warning = next(w for w in result.warnings if w.startswith("Unsupported charges"))
    assert "'Fixed Monthly Charge'" in warning and "'energy'" in warning
    assert "adjustment_total" not in warning and "incentive" not in warning


def test_rider_path_reports_unsupported_types_cent_units_and_future_riders(repo):
    result = TariffBillingEngine(repo).calculate(
        "base", BillInput(monthly_kwh=1000, service_date=datetime.date(2025, 11, 1))
    )
    storm_items = [i for i in result.line_items if i.source == "storm"]
    # Only the 'adjustment' row is priced; existing (dollar-as-written) behaviour is unchanged.
    assert [i.label for i in storm_items] == ["Storm Securitization - Residential"]
    assert storm_items[0].amount == pytest.approx(246.0)
    assert any(
        c["charge_type"] == "Rider Adjustment" and c["source"] == "storm"
        for c in result.unsupported_charges
    )
    assert any(w.startswith("Unit warning for storm") and "100x" in w for w in result.warnings)
    assert any("late-rider (earliest 2027-01-01)" in w for w in result.warnings)
    assert not any("late-rider" in i.source for i in result.line_items)


# ---------------------------------------------------------------------------
# Charge-type classification (the 13 non-priced types found in production)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ctype", "kwargs", "category", "suggested"),
    [
        ("adjustment_total", {}, "excluded_informational", None),
        ("incentive", {}, "excluded_informational", None),
        ("rebate", {}, "excluded_informational", None),
        ("Program Incentive", {}, "excluded_informational", None),
        ("Fixed Monthly Charge", {}, "supported_but_mismapped", "fixed"),
        ("Basic Facilities Charge", {}, "supported_but_mismapped", "fixed"),
        ("Demand Charge", {}, "supported_but_mismapped", "demand"),
        ("Energy Charge", {}, "supported_but_mismapped", "energy_block"),
        ("Seasonal Rate", {}, "supported_but_mismapped", "energy_block"),
        ("Rider Adjustment", {"rate_unit": "¢/kWh"}, "supported_but_mismapped", "adjustment"),
        ("rider_adjustment", {}, "supported_but_mismapped", "adjustment"),
        ("rider_adjustment", {"charge_label": "Total Adjustment"}, "ambiguous_or_invalid", None),
        ("Rider Adjustment", {"rate_unit": "%"}, "billable_unsupported", "adjustment"),
        ("energy", {"tou_period": "standard"}, "billable_unsupported", None),
        ("energy", {"charge_label": "RTP Adder"}, "billable_unsupported", None),
        ("energy", {"charge_label": "On-Peak Energy"}, "supported_but_mismapped", "tou_energy"),
        ("energy", {"charge_label": "Energy Charge"}, "supported_but_mismapped", "energy_block"),
        ("Fee", {}, "ambiguous_or_invalid", None),
        ("mystery", {}, "ambiguous_or_invalid", None),
        ("fixed", {}, "priced", None),
    ],
)
def test_classify_charge(ctype, kwargs, category, suggested):
    result = classify_charge(ctype, **kwargs)
    assert (result.category, result.suggested_type) == (category, suggested)
    assert is_potentially_billable(result) == (category not in ("priced", "excluded_informational"))


def test_cent_sign_units_are_detected():
    assert has_unconverted_cent_unit("¢/kWh")
    assert not has_unconverted_cent_unit("cents/kWh")
    assert not has_unconverted_cent_unit("$/kWh")
    assert not has_unconverted_cent_unit(None)
