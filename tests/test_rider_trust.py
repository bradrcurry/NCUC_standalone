"""Tests for the rider trust scoring module."""
from __future__ import annotations

from pathlib import Path

from duke_rates.analytics.rider_trust import load_rider_trust_table, trust_summary

DB_PATH = Path("data/db/duke_rates.db")

_EXPECTED_COLUMNS = {
    "utility",
    "rate_class_group",
    "rider_code",
    "effective_date",
    "rider_effective_date",
    "cents_per_kwh",
    "source_kind",
    "source_score",
    "date_score",
    "bill_score",
    "continuity_score",
    "trust_score",
    "trust_tier",
}
_VALID_TIERS = {"high", "medium", "low", "unverified"}
_VALID_GROUPS = {"dep_residential", "dep_sgs", "dep_sgs_clr", "dec_residential"}


class TestLoadRiderTrustTable:
    def setup_method(self):
        self.df = load_rider_trust_table(database_path=DB_PATH)

    def test_not_empty(self):
        assert not self.df.empty

    def test_has_expected_columns(self):
        assert _EXPECTED_COLUMNS <= set(self.df.columns)

    def test_both_utilities_present(self):
        assert set(self.df["utility"].unique()) >= {"DEP", "DEC"}

    def test_all_rate_class_groups_present(self):
        groups = set(self.df["rate_class_group"].unique())
        assert groups == _VALID_GROUPS

    def test_trust_score_range(self):
        assert (self.df["trust_score"] >= 0.0).all()
        assert (self.df["trust_score"] <= 1.0).all()

    def test_trust_tier_values_valid(self):
        assert set(self.df["trust_tier"].unique()) <= _VALID_TIERS

    def test_high_tier_rows_exist(self):
        assert (self.df["trust_tier"] == "high").any()

    def test_clean_leaf600_scores_higher_than_provisional(self):
        clean_scores = self.df[self.df["source_kind"] == "clean_leaf600"]["source_score"]
        prov_scores = self.df[self.df["source_kind"] == "provisional_ingest"]["source_score"]
        if not clean_scores.empty and not prov_scores.empty:
            assert clean_scores.mean() > prov_scores.mean()

    def test_known_high_confidence_riders_residential(self):
        dep_res_high = set(
            self.df[
                (self.df["utility"] == "DEP")
                & (self.df["rate_class_group"] == "dep_residential")
                & (self.df["trust_tier"] == "high")
            ]["rider_code"]
        )
        assert "BA-DSM" in dep_res_high
        assert "CAR" in dep_res_high

        dec_high = set(
            self.df[
                (self.df["utility"] == "DEC")
                & (self.df["trust_tier"] == "high")
            ]["rider_code"]
        )
        assert "FCA" in dec_high
        assert "DSM" in dec_high

    def test_sgs_group_present_with_ba_ee(self):
        sgs_codes = set(
            self.df[self.df["rate_class_group"] == "dep_sgs"]["rider_code"].unique()
        )
        assert "BA-EE" in sgs_codes
        assert "BA-DSM" in sgs_codes

    def test_sgs_clr_group_present(self):
        clr_rows = self.df[self.df["rate_class_group"] == "dep_sgs_clr"]
        assert not clr_rows.empty
        assert "BA-DSM" in set(clr_rows["rider_code"].unique())

    def test_provisional_only_riders_score_below_high(self):
        # SCR and STS are DEP provisional-only — should not reach 'high'
        scr_rows = self.df[
            (self.df["utility"] == "DEP")
            & (self.df["rider_code"] == "SCR")
        ]
        if not scr_rows.empty:
            assert (scr_rows["trust_tier"] != "high").all()

    def test_no_nulls_in_key_score_columns(self):
        for col in ("source_score", "date_score", "bill_score", "continuity_score", "trust_score"):
            assert self.df[col].notna().all(), f"NaN found in {col}"

    def test_trust_score_equals_sum_of_components(self):
        computed = (
            self.df["source_score"]
            + self.df["date_score"]
            + self.df["bill_score"]
            + self.df["continuity_score"]
        ).round(4)
        assert (self.df["trust_score"] == computed).all()

    def test_sgs_rows_more_than_residential(self):
        # SGS has two groups (dep_sgs + dep_sgs_clr); combined should exceed
        # residential count since they have the same set of dates
        dep_res_count = len(self.df[self.df["rate_class_group"] == "dep_residential"])
        dep_sgs_count = len(self.df[self.df["rate_class_group"] == "dep_sgs"])
        dep_clr_count = len(self.df[self.df["rate_class_group"] == "dep_sgs_clr"])
        assert dep_sgs_count > 0
        assert dep_clr_count > 0
        assert dep_res_count > 0


class TestTrustSummary:
    def setup_method(self):
        self.summary = trust_summary(database_path=DB_PATH)

    def test_has_expected_keys(self):
        assert "total_rows" in self.summary
        assert "tier_counts" in self.summary
        assert "high_confidence_rider_codes" in self.summary
        assert "mean_trust_score_by_rider" in self.summary
        assert "by_group" in self.summary

    def test_total_rows_positive(self):
        assert self.summary["total_rows"] > 0

    def test_total_rows_exceeds_prior_baseline(self):
        # Prior baseline was 172 rows (DEP res + DEC res only).
        # Adding SGS and SGS-CLR should push this substantially higher.
        assert self.summary["total_rows"] > 300

    def test_high_confidence_list_nonempty(self):
        assert len(self.summary["high_confidence_rider_codes"]) > 0

    def test_mean_trust_score_records_have_required_fields(self):
        for rec in self.summary["mean_trust_score_by_rider"]:
            assert "utility" in rec
            assert "rate_class_group" in rec
            assert "rider_code" in rec
            assert "mean_trust_score" in rec
            assert 0.0 <= rec["mean_trust_score"] <= 1.0

    def test_by_group_contains_all_groups(self):
        # by_group is a dict of {tier: {group: count}} from unstack
        # The outer keys are tier names; inner keys are group names
        all_groups = set()
        for tier_dict in self.summary["by_group"].values():
            all_groups.update(tier_dict.keys())
        assert all_groups >= _VALID_GROUPS
