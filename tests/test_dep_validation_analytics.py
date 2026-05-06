from __future__ import annotations

from pathlib import Path

from duke_rates.analytics.dep_validation import load_dep_res_validation_report


DB_PATH = Path("data/db/duke_rates.db")


def test_dep_validation_report_identifies_expected_clean_rider_gaps() -> None:
    report = load_dep_res_validation_report(database_path=DB_PATH)

    summary = report["summary"]
    assert summary["partial_clean_rider_dates"] == []
    assert "BA-DSM" in summary["expected_clean_rider_codes"]
    assert summary["base_distinct_effective_dates"] >= 17
    assert summary["clean_rider_distinct_effective_dates"] >= 6


def test_dep_validation_report_has_schedule_mapping_for_res() -> None:
    report = load_dep_res_validation_report(database_path=DB_PATH)
    mapping = report["summary"]["applicable_riders_by_schedule"]

    assert "RES" in mapping
    assert "BA-DSM" in mapping["RES"]
