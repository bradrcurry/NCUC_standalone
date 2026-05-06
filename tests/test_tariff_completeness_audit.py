"""Tests for TariffCompletenessAuditService."""
from __future__ import annotations

import datetime
import sqlite3

import pytest

from duke_rates.analytics.tariff_completeness_audit import (
    TariffCompletenessAuditService,
    _build_supersession_chain,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.audit import VersionTimelineEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "audit_test.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()
    return path


@pytest.fixture
def repo(db_path):
    return Repository(str(db_path))


@pytest.fixture
def svc(repo):
    return TariffCompletenessAuditService(repo)


def _seed_family(repo, family_key, title="Test Family", family_type="rider"):
    from duke_rates.models.tariff import TariffFamilyRecord
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key=family_key,
            state="NC",
            company="progress",
            family_type=family_type,
            title=title,
        )
    )


def _seed_version(repo, family_key, version_id_hint, start, end, revision, supersedes=None):
    from duke_rates.models.tariff import TariffVersionRecord
    repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key=family_key,
            effective_start=start,
            effective_end=end,
            revision_label=revision,
            supersedes_label=supersedes,
            source_type="utility_current",
            confidence_score=0.9,
        )
    )


def _seed_charge(repo, version_id, charge_type="adjustment", rate_value=0.001,
                 rate_unit="$/kWh", customer_class=None):
    from duke_rates.models.tariff import TariffChargeRecord
    repo.upsert_tariff_charge(
        TariffChargeRecord(
            version_id=version_id,
            family_key="dummy",
            charge_type=charge_type,
            rate_value=rate_value,
            rate_unit=rate_unit,
            customer_class=customer_class,
            confidence_score=0.9,
        )
    )


def _seed_rider_link(repo, rider_key, schedule_key, in_rider_summary=True):
    from duke_rates.models.tariff import RiderApplicabilityRecord
    repo.upsert_rider_applicability(
        RiderApplicabilityRecord(
            rider_family_key=rider_key,
            applies_to_family_key=schedule_key,
            mandatory=True,
            enrollment_type="mandatory",
            in_rider_summary=in_rider_summary,
            source_type="tariff_text",
        )
    )


# ---------------------------------------------------------------------------
# Tests: build_temporal_map
# ---------------------------------------------------------------------------


class TestBuildTemporalMap:
    def test_empty_family_returns_empty_status(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-test-empty")
        tm = svc.build_temporal_map("nc-progress-leaf-test-empty")
        assert tm.timeline_status == "empty"
        assert tm.versions == []
        assert tm.gaps == []

    def test_unknown_family_returns_empty(self, svc):
        tm = svc.build_temporal_map("nc-progress-leaf-does-not-exist")
        assert tm.family_key == "nc-progress-leaf-does-not-exist"
        assert tm.timeline_status == "empty"

    def test_single_dated_version_no_gaps(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-x1")
        _seed_version(repo, "nc-progress-leaf-x1", 1, "2025-01-01", None, "NC First Rev")
        tm = svc.build_temporal_map("nc-progress-leaf-x1")
        assert len(tm.versions) == 1
        assert tm.timeline_status == "complete"
        assert tm.gaps == []

    def test_two_contiguous_versions_no_gap(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-x2")
        _seed_version(repo, "nc-progress-leaf-x2", 1, "2025-01-01", "2025-06-30", "NC First Rev")
        _seed_version(repo, "nc-progress-leaf-x2", 2, "2025-07-01", None, "NC Second Rev",
                      supersedes="NC First Rev")
        tm = svc.build_temporal_map("nc-progress-leaf-x2")
        assert len(tm.versions) == 2
        assert tm.timeline_status == "complete"
        assert tm.gaps == []

    def test_gap_detected_between_versions(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-x3")
        _seed_version(repo, "nc-progress-leaf-x3", 1, "2025-01-01", "2025-03-31", "NC First Rev")
        # Gap: Apr 1–Jun 30
        _seed_version(repo, "nc-progress-leaf-x3", 2, "2025-07-01", None, "NC Second Rev")
        tm = svc.build_temporal_map("nc-progress-leaf-x3")
        assert len(tm.gaps) == 1
        assert tm.gaps[0].gap_type == "between_versions"
        assert tm.gaps[0].gap_start == "2025-03-31"
        assert tm.gaps[0].gap_end == "2025-07-01"
        assert tm.gaps[0].gap_days == 91  # Apr(30) + May(31) + Jun(30) = 91 days
        assert tm.timeline_status == "gaps_exist"

    def test_undated_version_flagged(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-x4")
        _seed_version(repo, "nc-progress-leaf-x4", 1, None, None, "NC Undated Rev")
        tm = svc.build_temporal_map("nc-progress-leaf-x4")
        assert len(tm.gaps) == 1
        assert tm.gaps[0].gap_type == "undated_version"
        assert tm.timeline_status == "undated"

    def test_supersession_chain_reconstructed(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-x5")
        _seed_version(repo, "nc-progress-leaf-x5", 1, "2024-01-01", "2024-12-31", "NC First Rev")
        _seed_version(repo, "nc-progress-leaf-x5", 2, "2025-01-01", "2025-12-31", "NC Second Rev",
                      supersedes="NC First Rev")
        _seed_version(repo, "nc-progress-leaf-x5", 3, "2026-01-01", None, "NC Third Rev",
                      supersedes="NC Second Rev")
        tm = svc.build_temporal_map("nc-progress-leaf-x5")
        assert tm.supersession_chain == ["NC First Rev", "NC Second Rev", "NC Third Rev"]
        assert tm.orphaned_revisions == []

    def test_orphaned_revision_detected(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-x6")
        # Chain: First -> Second but Third has no link to them
        _seed_version(repo, "nc-progress-leaf-x6", 1, "2024-01-01", "2024-12-31", "NC First Rev")
        _seed_version(repo, "nc-progress-leaf-x6", 2, "2025-01-01", None, "NC Second Rev",
                      supersedes="NC First Rev")
        _seed_version(repo, "nc-progress-leaf-x6", 3, "2025-06-01", None, "NC Third Rev (orphan)")
        tm = svc.build_temporal_map("nc-progress-leaf-x6")
        assert "NC Third Rev (orphan)" in tm.orphaned_revisions

    def test_charge_counts_populated(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-x7")
        _seed_version(repo, "nc-progress-leaf-x7", 1, "2025-01-01", None, "NC First Rev")
        # Get the version id
        versions = repo.list_tariff_versions("nc-progress-leaf-x7")
        assert len(versions) == 1
        _seed_charge(repo, versions[0].id, rate_value=0.005)
        _seed_charge(repo, versions[0].id, rate_value=None)  # null rate

        tm = svc.build_temporal_map("nc-progress-leaf-x7")
        assert len(tm.versions) == 1
        assert tm.versions[0].charge_count == 2
        assert tm.versions[0].null_rate_count == 1
        assert tm.versions[0].charge_status == "null_rates"


# ---------------------------------------------------------------------------
# Tests: build_coverage_map
# ---------------------------------------------------------------------------


class TestBuildCoverageMap:
    def _setup_schedule_and_rider(self, repo):
        """Seed a minimal rate schedule + one summary rider + one direct-bill rider."""
        _seed_family(repo, "nc-progress-leaf-s1", family_type="rate_schedule")
        _seed_version(repo, "nc-progress-leaf-s1", 10, "2025-01-01", None, "NC First Rev S1")
        s_versions = repo.list_tariff_versions("nc-progress-leaf-s1")
        _seed_charge(repo, s_versions[0].id, charge_type="energy_block", rate_value=0.12,
                     rate_unit="$/kWh")

        _seed_family(repo, "nc-progress-leaf-r1", title="Rider One")
        _seed_version(repo, "nc-progress-leaf-r1", 11, "2025-01-01", None, "NC First Rev R1")
        r1_versions = repo.list_tariff_versions("nc-progress-leaf-r1")
        _seed_charge(repo, r1_versions[0].id, rate_value=0.001, rate_unit="$/kWh")
        _seed_rider_link(repo, "nc-progress-leaf-r1", "nc-progress-leaf-s1", in_rider_summary=True)

        _seed_family(repo, "nc-progress-leaf-r2", title="Rider Direct Bill")
        _seed_version(repo, "nc-progress-leaf-r2", 12, "2025-01-01", None, "NC First Rev R2")
        r2_versions = repo.list_tariff_versions("nc-progress-leaf-r2")
        _seed_charge(repo, r2_versions[0].id, rate_value=0.002, rate_unit="$/kWh")
        _seed_rider_link(repo, "nc-progress-leaf-r2", "nc-progress-leaf-s1", in_rider_summary=False)

        return s_versions[0].id

    def test_complete_coverage_both_riders_ok(self, svc, repo):
        self._setup_schedule_and_rider(repo)
        cm = svc.build_coverage_map("nc-progress-leaf-s1", datetime.date(2025, 6, 1))
        assert len(cm.riders) == 2
        assert cm.riders_ok == 2
        assert cm.riders_missing == 0
        # No leaf-600 reference → no delta check
        assert cm.delta_cents_per_kwh is None

    def test_engine_summary_total_excludes_direct_bill(self, svc, repo):
        self._setup_schedule_and_rider(repo)
        cm = svc.build_coverage_map("nc-progress-leaf-s1", datetime.date(2025, 6, 1))
        # Only rider r1 (in_rider_summary=True, 0.001 $/kWh = 0.1 c/kWh) should count
        assert cm.engine_summary_total_cents_per_kwh == pytest.approx(0.1, abs=0.001)

    def test_no_version_for_schedule(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-sX", family_type="rate_schedule")
        # No versions seeded
        cm = svc.build_coverage_map("nc-progress-leaf-sX", datetime.date(2025, 6, 1))
        assert cm.audit_verdict == "no_data"
        assert cm.schedule_charge_status == "no_version"

    def test_rider_no_version_flagged(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-s2", family_type="rate_schedule")
        _seed_version(repo, "nc-progress-leaf-s2", 20, "2025-01-01", None, "NC First Rev S2")
        s_versions = repo.list_tariff_versions("nc-progress-leaf-s2")
        _seed_charge(repo, s_versions[0].id, charge_type="energy_block", rate_value=0.12,
                     rate_unit="$/kWh")
        # Rider family exists but has NO version
        _seed_family(repo, "nc-progress-leaf-rx", title="Missing Version Rider")
        _seed_rider_link(repo, "nc-progress-leaf-rx", "nc-progress-leaf-s2")

        cm = svc.build_coverage_map("nc-progress-leaf-s2", datetime.date(2025, 6, 1))
        rx = next(r for r in cm.riders if r.rider_family_key == "nc-progress-leaf-rx")
        assert rx.coverage_status == "no_version"
        assert cm.audit_verdict == "missing_riders"

    def test_rider_no_charges_flagged(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-s3", family_type="rate_schedule")
        _seed_version(repo, "nc-progress-leaf-s3", 30, "2025-01-01", None, "NC First Rev S3")
        s_versions = repo.list_tariff_versions("nc-progress-leaf-s3")
        _seed_charge(repo, s_versions[0].id, charge_type="energy_block", rate_value=0.12,
                     rate_unit="$/kWh")
        _seed_family(repo, "nc-progress-leaf-ry", title="Empty Rider")
        _seed_version(repo, "nc-progress-leaf-ry", 31, "2025-01-01", None, "NC First Rev RY")
        # Version exists but NO charges seeded
        _seed_rider_link(repo, "nc-progress-leaf-ry", "nc-progress-leaf-s3")

        cm = svc.build_coverage_map("nc-progress-leaf-s3", datetime.date(2025, 6, 1))
        ry = next(r for r in cm.riders if r.rider_family_key == "nc-progress-leaf-ry")
        assert ry.coverage_status == "no_charges"

    def test_customer_class_all_matches(self, svc, repo):
        """Charges with customer_class='all' should match any customer_class request."""
        _seed_family(repo, "nc-progress-leaf-s4", family_type="rate_schedule")
        _seed_version(repo, "nc-progress-leaf-s4", 40, "2025-01-01", None, "NC Rev S4")
        s_versions = repo.list_tariff_versions("nc-progress-leaf-s4")
        _seed_charge(repo, s_versions[0].id, charge_type="energy_block", rate_value=0.12,
                     rate_unit="$/kWh")
        _seed_family(repo, "nc-progress-leaf-rz", title="All-class Rider")
        _seed_version(repo, "nc-progress-leaf-rz", 41, "2025-01-01", None, "NC Rev RZ")
        rz_versions = repo.list_tariff_versions("nc-progress-leaf-rz")
        _seed_charge(repo, rz_versions[0].id, rate_value=0.003, rate_unit="$/kWh",
                     customer_class="all")
        _seed_rider_link(repo, "nc-progress-leaf-rz", "nc-progress-leaf-s4")

        cm = svc.build_coverage_map("nc-progress-leaf-s4", datetime.date(2025, 6, 1),
                                    customer_class="residential")
        rz = next(r for r in cm.riders if r.rider_family_key == "nc-progress-leaf-rz")
        assert rz.rate_cents_per_kwh == pytest.approx(0.3, abs=0.001)


# ---------------------------------------------------------------------------
# Tests: build_null_audit
# ---------------------------------------------------------------------------


class TestBuildNullAudit:
    def test_batch_returns_one_per_schedule(self, svc, repo):
        for i in range(3):
            key = f"nc-progress-leaf-batch-{i}"
            _seed_family(repo, key, family_type="rate_schedule")
            _seed_version(repo, key, 100 + i, "2025-01-01", None, f"Rev {i}")
            vs = repo.list_tariff_versions(key)
            _seed_charge(repo, vs[0].id, rate_value=0.01, rate_unit="$/kWh",
                         charge_type="energy_block")

        results = svc.build_null_audit("NC", "progress", datetime.date(2025, 6, 1))
        schedule_keys = [r.schedule_family_key for r in results]
        assert "nc-progress-leaf-batch-0" in schedule_keys
        assert "nc-progress-leaf-batch-1" in schedule_keys
        assert "nc-progress-leaf-batch-2" in schedule_keys

    def test_no_schedules_returns_empty(self, svc, repo):
        # No rate_schedule families seeded
        results = svc.build_null_audit("ZZ", "nobody", datetime.date(2025, 6, 1))
        assert results == []


# ---------------------------------------------------------------------------
# Tests: build_rider_map
# ---------------------------------------------------------------------------


class TestBuildRiderMap:
    def test_static_map_groups_by_schedule(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-sm1", family_type="rate_schedule")
        _seed_family(repo, "nc-progress-leaf-rm1", title="Rider Map One")
        _seed_family(repo, "nc-progress-leaf-rm2", title="Rider Map Two")
        _seed_rider_link(repo, "nc-progress-leaf-rm1", "nc-progress-leaf-sm1")
        _seed_rider_link(repo, "nc-progress-leaf-rm2", "nc-progress-leaf-sm1",
                         in_rider_summary=False)

        rmap = svc.build_rider_map("NC", "progress")
        assert "nc-progress-leaf-sm1" in rmap
        keys = [r["rider_family_key"] for r in rmap["nc-progress-leaf-sm1"]]
        assert "nc-progress-leaf-rm1" in keys
        assert "nc-progress-leaf-rm2" in keys

    def test_in_rider_summary_flag_preserved(self, svc, repo):
        _seed_family(repo, "nc-progress-leaf-sm2", family_type="rate_schedule")
        _seed_family(repo, "nc-progress-leaf-rm3", title="Direct Bill Rider")
        _seed_rider_link(repo, "nc-progress-leaf-rm3", "nc-progress-leaf-sm2",
                         in_rider_summary=False)

        rmap = svc.build_rider_map("NC", "progress")
        rm3 = next(r for r in rmap["nc-progress-leaf-sm2"]
                   if r["rider_family_key"] == "nc-progress-leaf-rm3")
        assert rm3["in_rider_summary"] is False


# ---------------------------------------------------------------------------
# Tests: _build_supersession_chain helper
# ---------------------------------------------------------------------------


class TestBuildSupersessionChain:
    def _entry(self, revision, supersedes=None, start="2025-01-01"):
        return VersionTimelineEntry(
            version_id=1,
            family_key="test",
            effective_start=start,
            effective_end=None,
            revision_label=revision,
            supersedes_label=supersedes,
            source_type="utility_current",
            confidence_score=0.9,
            charge_count=1,
            null_rate_count=0,
        )

    def test_linear_chain(self):
        entries = [
            self._entry("Rev 1", start="2024-01-01"),
            self._entry("Rev 2", supersedes="Rev 1", start="2025-01-01"),
            self._entry("Rev 3", supersedes="Rev 2", start="2026-01-01"),
        ]
        chain, orphans = _build_supersession_chain(entries)
        assert chain == ["Rev 1", "Rev 2", "Rev 3"]
        assert orphans == []

    def test_orphan_detected(self):
        entries = [
            self._entry("Rev 1", start="2024-01-01"),
            self._entry("Rev 2", supersedes="Rev 1", start="2025-01-01"),
            self._entry("Rev X", start="2025-06-01"),  # orphan
        ]
        chain, orphans = _build_supersession_chain(entries)
        assert "Rev 1" in chain
        assert "Rev 2" in chain
        assert "Rev X" in orphans

    def test_empty_entries(self):
        chain, orphans = _build_supersession_chain([])
        assert chain == []
        assert orphans == []

    def test_single_entry(self):
        chain, orphans = _build_supersession_chain([self._entry("Rev 1")])
        assert chain == ["Rev 1"]
        assert orphans == []
