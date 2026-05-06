"""Tests for the URDB/OpenEI export module."""
from __future__ import annotations

import json
import sqlite3

import pytest

from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.external.urdb_export import (
    URDBRecord,
    export_bulk_to_urdb,
    export_family_to_urdb,
    records_to_json,
    _normalize_rate,
    _build_fixed_charges,
    _build_energy_structure,
    _uniform_schedule,
    _flat_seasonal_schedule,
    _build_tou_weekday_schedule,
)


# ---------------------------------------------------------------------------
# Fixtures: in-memory SQLite with minimal tariff data
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    """In-memory SQLite DB with schema + test data."""
    db = tmp_path / "urdb_test.db"
    c = sqlite3.connect(db)
    c.executescript(SCHEMA_SQL)
    migrate(c)

    # Family: flat residential RES
    c.execute("""
        INSERT INTO tariff_families
          (family_key, state, company, tariff_identifier, schedule_code,
           family_type, title, aliases_json, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
    """, ("test-flat-500", "NC", "progress", "leaf-500", "RES",
          "rate_schedule", "Residential Service Schedule RES", "[]"))

    version_id = c.execute("""
        INSERT INTO tariff_versions
          (family_key, effective_start, revision_label, source_type,
           confidence_score, created_at)
        VALUES (?,?,?,?,?,datetime('now'))
    """, ("test-flat-500", "2025-10-01", "NC Second Revised Leaf No. 500",
          "utility_current", 0.9)).lastrowid

    c.executemany("""
        INSERT INTO tariff_charges
          (version_id, family_key, charge_type, charge_label,
           rate_value, rate_unit, tier_min, tier_max, tou_period,
           season, customer_class, confidence_score, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
    """, [
        (version_id, "test-flat-500", "fixed", "Customer Charge",
         14.00, "$/month", None, None, None, "all_year", "residential", 0.95),
        (version_id, "test-flat-500", "energy_block", "Energy - Summer",
         0.12623, "$/kWh", 0.0, None, None, "summer", "residential", 0.90),
        (version_id, "test-flat-500", "energy_block", "Energy - Winter",
         0.11623, "$/kWh", 0.0, None, None, "winter", "residential", 0.90),
    ])

    # Family: TOU schedule R-TOU
    c.execute("""
        INSERT INTO tariff_families
          (family_key, state, company, tariff_identifier, schedule_code,
           family_type, title, aliases_json, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
    """, ("test-tou-502", "NC", "progress", "leaf-502", "R_TOU",
          "rate_schedule", "Residential Time-of-Use R-TOU", "[]"))

    tou_vid = c.execute("""
        INSERT INTO tariff_versions
          (family_key, effective_start, revision_label, source_type,
           confidence_score, created_at)
        VALUES (?,?,?,?,?,datetime('now'))
    """, ("test-tou-502", "2025-10-01", "NC Second Revised Leaf No. 502",
          "utility_current", 0.85)).lastrowid

    c.executemany("""
        INSERT INTO tariff_charges
          (version_id, family_key, charge_type, charge_label,
           rate_value, rate_unit, tier_min, tier_max, tou_period,
           season, customer_class, confidence_score, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
    """, [
        (tou_vid, "test-tou-502", "fixed", "Customer Charge",
         14.00, "$/month", None, None, None, "all_year", None, 0.90),
        (tou_vid, "test-tou-502", "tou_energy", "On-Peak Energy",
         0.29905, "$/kWh", None, None, "on_peak", "all_year", None, 0.90),
        (tou_vid, "test-tou-502", "tou_energy", "Off-Peak Energy",
         0.11321, "$/kWh", None, None, "off_peak", "all_year", None, 0.90),
        (tou_vid, "test-tou-502", "tou_energy", "Discount Energy",
         0.07372, "$/kWh", None, None, "discount", "all_year", None, 0.90),
    ])

    # Rider: BA linked to flat-500
    c.execute("""
        INSERT INTO tariff_families
          (family_key, state, company, tariff_identifier, schedule_code,
           family_type, title, aliases_json, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
    """, ("test-rider-601", "NC", "progress", "leaf-601", "BA",
          "rider", "Rider BA", "[]"))

    rider_vid = c.execute("""
        INSERT INTO tariff_versions
          (family_key, effective_start, revision_label, source_type,
           confidence_score, created_at)
        VALUES (?,?,?,?,?,datetime('now'))
    """, ("test-rider-601", "2025-10-01", "NC Leaf No. 601",
          "utility_current", 0.80)).lastrowid

    c.execute("""
        INSERT INTO tariff_charges
          (version_id, family_key, charge_type, charge_label,
           rate_value, rate_unit, season, customer_class, confidence_score, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))
    """, (rider_vid, "test-rider-601", "adjustment", "Billing Adjustment",
          0.01549, "$/kWh", "all_year", "residential", 0.80))

    c.execute("""
        INSERT INTO rider_applicability
          (rider_family_key, applies_to_family_key, mandatory, source_type,
           confidence_score, created_at)
        VALUES (?,?,?,?,?,datetime('now'))
    """, ("test-rider-601", "test-flat-500", 1, "tariff_text", 0.90))

    c.commit()
    return c


# ---------------------------------------------------------------------------
# Group 1: Rate normalization
# ---------------------------------------------------------------------------

class TestNormalizeRate:
    def test_dollars_per_kwh_unchanged(self):
        c = {"rate_value": 0.12623, "rate_unit": "$/kWh"}
        assert _normalize_rate(c) == pytest.approx(0.12623)

    def test_cents_per_kwh_converted(self):
        c = {"rate_value": 12.623, "rate_unit": "cents/kWh"}
        assert _normalize_rate(c) == pytest.approx(0.12623, rel=1e-5)

    def test_none_rate_returns_none(self):
        c = {"rate_value": None, "rate_unit": "$/kWh"}
        assert _normalize_rate(c) is None

    def test_missing_unit_treated_as_dollars(self):
        c = {"rate_value": 0.10, "rate_unit": None}
        assert _normalize_rate(c) == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Group 2: Schedule array builders
# ---------------------------------------------------------------------------

class TestScheduleArrays:
    def test_uniform_schedule_shape(self):
        s = _uniform_schedule(0)
        assert len(s) == 12
        assert all(len(row) == 24 for row in s)
        assert all(all(v == 0 for v in row) for row in s)

    def test_flat_seasonal_schedule_may_is_summer(self):
        s = _flat_seasonal_schedule(summer_period=0, winter_period=1)
        # May = index 4 (month 5), should be summer (0)
        assert all(v == 0 for v in s[4])

    def test_flat_seasonal_schedule_january_is_winter(self):
        s = _flat_seasonal_schedule(summer_period=0, winter_period=1)
        # January = index 0, should be winter (1)
        assert all(v == 1 for v in s[0])

    def test_tou_weekday_on_peak_hours(self):
        pm = {"on_peak": 0, "off_peak": 1, "discount": 2}
        s = _build_tou_weekday_schedule(pm, "NC")
        # Hour 14 (2 PM) should be on_peak (idx 0)
        assert s[0][14] == 0
        assert s[6][16] == 0  # July, 4 PM

    def test_tou_weekday_off_peak_hours(self):
        pm = {"on_peak": 0, "off_peak": 1, "discount": 2}
        s = _build_tou_weekday_schedule(pm, "NC")
        # Hour 10 (10 AM) should be off_peak (idx 1)
        assert s[0][10] == 1

    def test_tou_weekday_discount_hours(self):
        pm = {"on_peak": 0, "off_peak": 1, "discount": 2}
        s = _build_tou_weekday_schedule(pm, "NC")
        # Hour 2 (2 AM) should be discount (idx 2)
        assert s[0][2] == 2
        # Hour 22 (10 PM) should be discount
        assert s[0][22] == 2

    def test_tou_schedule_has_12_months_24_hours(self):
        pm = {"on_peak": 0, "off_peak": 1, "discount": 2}
        s = _build_tou_weekday_schedule(pm, "NC")
        assert len(s) == 12
        assert all(len(row) == 24 for row in s)


# ---------------------------------------------------------------------------
# Group 3: Fixed charge builder
# ---------------------------------------------------------------------------

class TestBuildFixedCharges:
    def test_single_fixed_charge(self):
        charges = [{"charge_type": "fixed", "charge_label": "Customer Charge",
                    "rate_value": 14.00, "rate_unit": "$/month"}]
        result = _build_fixed_charges(charges)
        assert len(result) == 1
        assert result[0]["charge"] == 14.00
        assert result[0]["chargetype"] == "$/month"

    def test_empty_returns_empty(self):
        assert _build_fixed_charges([]) == []


# ---------------------------------------------------------------------------
# Group 4: export_family_to_urdb
# ---------------------------------------------------------------------------

class TestExportFamily:
    def test_flat_rate_exports_successfully(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        assert record is not None
        assert isinstance(record, URDBRecord)

    def test_flat_rate_fields(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        assert record.family_key == "test-flat-500"
        assert record.schedule_code == "RES"
        assert record.utility == "Duke Energy Progress, LLC"
        assert record.state == "NC"
        assert record.sector == "Residential"
        assert record.effective_start == "2025-10-01"
        assert record.tou is False

    def test_flat_rate_has_fixed_charges(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        assert len(record.fixedcharges) == 1
        assert record.fixedcharges[0]["charge"] == 14.00

    def test_flat_rate_has_energy_structure(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        # Summer and winter are different rates, so should produce 2 periods
        assert len(record.energyratestructure) >= 1
        assert len(record.energyweekdayschedule) == 12
        assert len(record.energyweekendschedule) == 12

    def test_flat_rate_energy_schedule_shape(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        for row in record.energyweekdayschedule:
            assert len(row) == 24

    def test_tou_rate_exports_successfully(self, conn):
        record = export_family_to_urdb(conn, "test-tou-502")
        assert record is not None
        assert record.tou is True

    def test_tou_rate_has_three_energy_periods(self, conn):
        record = export_family_to_urdb(conn, "test-tou-502")
        # on_peak, off_peak, discount → 3 periods
        assert len(record.energyratestructure) == 3

    def test_tou_rates_in_structure(self, conn):
        record = export_family_to_urdb(conn, "test-tou-502")
        # First period = on_peak → highest rate ~0.299
        rates = [period[0]["rate"] for period in record.energyratestructure]
        assert max(rates) == pytest.approx(0.29905, rel=1e-4)

    def test_rider_keys_populated(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        assert "test-rider-601" in record.rider_family_keys

    def test_missing_family_returns_none(self, conn):
        result = export_family_to_urdb(conn, "nonexistent-key")
        assert result is None

    def test_curation_notes_present(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        assert len(record.curation_notes) > 0
        assert any("Curation aid" in n for n in record.curation_notes)

    def test_rider_note_included(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        assert any("rider" in n.lower() for n in record.curation_notes)

    def test_tou_note_included(self, conn):
        record = export_family_to_urdb(conn, "test-tou-502")
        assert any("TOU" in n for n in record.curation_notes)


# ---------------------------------------------------------------------------
# Group 5: export_bulk_to_urdb
# ---------------------------------------------------------------------------

class TestExportBulk:
    def test_bulk_returns_both_nc_progress_schedules(self, conn):
        records = export_bulk_to_urdb(conn, state="NC", company="progress",
                                      min_confidence=0.0)
        keys = [r.family_key for r in records]
        assert "test-flat-500" in keys
        assert "test-tou-502" in keys

    def test_bulk_sorted_by_family_key(self, conn):
        records = export_bulk_to_urdb(conn, state="NC", company="progress",
                                      min_confidence=0.0)
        keys = [r.family_key for r in records]
        assert keys == sorted(keys)

    def test_bulk_excludes_low_confidence(self, conn):
        # min_confidence=1.0 should exclude everything (no charge has 1.0)
        records = export_bulk_to_urdb(conn, state="NC", company="progress",
                                      min_confidence=1.0)
        assert len(records) == 0

    def test_bulk_includes_above_threshold(self, conn):
        # min_confidence=0.85 should include both test families (best conf ≥ 0.90)
        records = export_bulk_to_urdb(conn, state="NC", company="progress",
                                      min_confidence=0.85)
        assert len(records) == 2

    def test_bulk_state_filter(self, conn):
        records = export_bulk_to_urdb(conn, state="FL", min_confidence=0.0)
        assert len(records) == 0  # no FL families in test DB

    def test_bulk_family_type_filter_rider(self, conn):
        records = export_bulk_to_urdb(conn, state="NC", company="progress",
                                      family_type="rider", min_confidence=0.0)
        # Rider 601 has a charge, should appear
        keys = [r.family_key for r in records]
        assert "test-rider-601" in keys
        # Rate schedules should not appear
        assert "test-flat-500" not in keys


# ---------------------------------------------------------------------------
# Group 6: records_to_json
# ---------------------------------------------------------------------------

class TestRecordsToJson:
    def test_json_output_is_valid(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        result = records_to_json([record])
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 1

    def test_json_contains_family_key(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        result = records_to_json([record])
        parsed = json.loads(result)
        assert parsed[0]["_duke_rates_family_key"] == "test-flat-500"

    def test_json_contains_urdb_fields(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        result = records_to_json([record])
        parsed = json.loads(result)[0]
        assert "name" in parsed
        assert "utility" in parsed
        assert "state" in parsed
        assert "sector" in parsed
        assert "tou" in parsed

    def test_json_state_is_full_name(self, conn):
        record = export_family_to_urdb(conn, "test-flat-500")
        result = records_to_json([record])
        parsed = json.loads(result)[0]
        assert parsed["state"] == "North Carolina"

    def test_json_tou_is_int(self, conn):
        record = export_family_to_urdb(conn, "test-tou-502")
        result = records_to_json([record])
        parsed = json.loads(result)[0]
        assert parsed["tou"] in (0, 1)
        assert parsed["tou"] == 1  # TOU schedule

    def test_bulk_json_two_records(self, conn):
        records = export_bulk_to_urdb(conn, state="NC", company="progress",
                                      min_confidence=0.0)
        result = records_to_json(records)
        parsed = json.loads(result)
        rate_schedule_records = [r for r in parsed
                                 if r["_duke_rates_family_key"] in
                                 ("test-flat-500", "test-tou-502")]
        assert len(rate_schedule_records) == 2
