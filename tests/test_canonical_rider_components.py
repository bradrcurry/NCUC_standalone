"""Tests for the canonical rider-component analytics module."""
from __future__ import annotations

from pathlib import Path

import pytest

from duke_rates.analytics.canonical_rider_components import (
    load_dec_rs_canonical_rider_components,
    load_dep_res_canonical_rider_components,
    load_dep_sgs_canonical_rider_components,
    load_dep_sgs_clr_canonical_rider_components,
)

DB_PATH = Path("data/db/duke_rates.db")
_EXPECTED_COLUMNS = {
    "effective_date",
    "rider_code",
    "rider_effective_date",
    "cents_per_kwh",
    "source_kind",
    "source_pdf",
    "docket_dir",
}
_VALID_SOURCE_KINDS = {"clean_leaf600", "provisional_ingest"}


class TestDepResCanonicalRiderComponents:
    def setup_method(self):
        self.df = load_dep_res_canonical_rider_components(database_path=DB_PATH)

    def test_not_empty(self):
        assert not self.df.empty

    def test_has_expected_columns(self):
        assert _EXPECTED_COLUMNS <= set(self.df.columns)

    def test_source_kinds_are_valid(self):
        assert set(self.df["source_kind"].unique()) <= _VALID_SOURCE_KINDS

    def test_both_source_kinds_present(self):
        assert "clean_leaf600" in set(self.df["source_kind"].unique())
        assert "provisional_ingest" in set(self.df["source_kind"].unique())

    def test_no_duplicate_date_rider_pairs(self):
        dupes = self.df.duplicated(subset=["effective_date", "rider_code"]).sum()
        assert dupes == 0

    def test_provisional_covers_pre_2023(self):
        provisional = self.df[self.df["source_kind"] == "provisional_ingest"]
        assert not provisional.empty
        assert provisional["effective_date"].min().year <= 2022

    def test_clean_covers_post_2023(self):
        clean = self.df[self.df["source_kind"] == "clean_leaf600"]
        assert not clean.empty
        assert clean["effective_date"].max().year >= 2023

    def test_date_range_spans_2016_to_2026(self):
        assert str(self.df["effective_date"].min().date()) >= "2016-01-01"
        assert str(self.df["effective_date"].max().date()) <= "2026-12-31"

    def test_cents_per_kwh_within_plausible_range(self):
        assert (self.df["cents_per_kwh"].abs() <= 5.0).all()

    def test_known_rider_codes_present(self):
        codes = set(self.df["rider_code"].unique())
        assert "BA-DSM" in codes or "BA" in codes  # provisional uses 'BA', clean uses 'BA-DSM'
        assert "CPRE" in codes

    def test_residential_does_not_contain_ba_ee(self):
        # BA-EE is a commercial-only rider and should not appear in residential
        codes = set(self.df["rider_code"].unique())
        assert "BA-EE" not in codes


class TestDecRsCanonicalRiderComponents:
    def setup_method(self):
        self.df = load_dec_rs_canonical_rider_components(database_path=DB_PATH)

    def test_not_empty(self):
        assert not self.df.empty

    def test_has_expected_columns(self):
        assert _EXPECTED_COLUMNS <= set(self.df.columns)

    def test_source_kind_is_clean_leaf600_only(self):
        assert set(self.df["source_kind"].unique()) == {"clean_leaf600"}

    def test_no_duplicate_date_rider_pairs(self):
        dupes = self.df.duplicated(subset=["effective_date", "rider_code"]).sum()
        assert dupes == 0

    def test_date_range_starts_2018(self):
        assert str(self.df["effective_date"].min().date()) >= "2018-01-01"

    def test_known_rider_codes_present(self):
        codes = set(self.df["rider_code"].unique())
        assert "FCA" in codes
        assert "DSM" in codes

    def test_cents_per_kwh_within_plausible_range(self):
        assert (self.df["cents_per_kwh"].abs() <= 5.0).all()


class TestDepSgsCanonicalRiderComponents:
    """DEP Small General Service Schedules (SGS + SGS-TOUE share one summary page)."""

    def setup_method(self):
        self.df = load_dep_sgs_canonical_rider_components(database_path=DB_PATH)

    def test_not_empty(self):
        assert not self.df.empty

    def test_has_expected_columns(self):
        assert _EXPECTED_COLUMNS <= set(self.df.columns)

    def test_source_kind_is_clean_leaf600_only(self):
        # No provisional path exists for SGS
        assert set(self.df["source_kind"].unique()) == {"clean_leaf600"}

    def test_no_duplicate_date_rider_pairs(self):
        dupes = self.df.duplicated(subset=["effective_date", "rider_code"]).sum()
        assert dupes == 0

    def test_date_range_starts_2023(self):
        assert str(self.df["effective_date"].min().date()) >= "2023-01-01"

    def test_known_rider_codes_present(self):
        codes = set(self.df["rider_code"].unique())
        assert "BA-DSM" in codes
        assert "CPRE" in codes
        assert "EDIT-4" in codes

    def test_sgs_has_ba_ee_not_in_residential(self):
        # BA-EE is a commercial-only rider that appears in SGS but not residential
        codes = set(self.df["rider_code"].unique())
        assert "BA-EE" in codes

    def test_cents_per_kwh_within_plausible_range(self):
        assert (self.df["cents_per_kwh"].abs() <= 5.0).all()


class TestDepSgsClrCanonicalRiderComponents:
    """DEP Small General Service - Constant Load Schedule (SGS-TOU-CLR)."""

    def setup_method(self):
        self.df = load_dep_sgs_clr_canonical_rider_components(database_path=DB_PATH)

    def test_not_empty(self):
        assert not self.df.empty

    def test_has_expected_columns(self):
        assert _EXPECTED_COLUMNS <= set(self.df.columns)

    def test_source_kind_is_clean_leaf600_only(self):
        assert set(self.df["source_kind"].unique()) == {"clean_leaf600"}

    def test_no_duplicate_date_rider_pairs(self):
        dupes = self.df.duplicated(subset=["effective_date", "rider_code"]).sum()
        assert dupes == 0

    def test_date_range_starts_2023(self):
        assert str(self.df["effective_date"].min().date()) >= "2023-01-01"

    def test_known_rider_codes_present(self):
        codes = set(self.df["rider_code"].unique())
        assert "BA-DSM" in codes
        assert "CPRE" in codes

    def test_cents_per_kwh_within_plausible_range(self):
        assert (self.df["cents_per_kwh"].abs() <= 5.0).all()
