from __future__ import annotations

from pathlib import Path

from duke_rates.analytics.dep_progress import (
    load_dep_res_all_in_history,
    load_dep_res_base_history,
    load_dep_res_rider_history,
)


DB_PATH = Path("data/db/duke_rates.db")


def test_dep_res_base_history_has_expected_coverage() -> None:
    df = load_dep_res_base_history(database_path=DB_PATH)

    assert not df.empty
    assert str(df["effective_date"].min().date()) == "2016-12-01"
    assert str(df["effective_date"].max().date()) == "2025-10-01"
    assert df["fixed_monthly_charge"].notna().all()


def test_dep_res_rider_history_starts_in_2023() -> None:
    totals_df, components_df = load_dep_res_rider_history(database_path=DB_PATH)

    assert not totals_df.empty
    assert not components_df.empty
    assert str(totals_df["effective_date"].min().date()) == "2023-10-01"
    assert "BA-DSM" in set(components_df["rider_code"])


def test_dep_res_all_in_history_marks_status_by_rider_coverage() -> None:
    all_in_df, _, _ = load_dep_res_all_in_history(database_path=DB_PATH)

    row_2022 = all_in_df.loc[all_in_df["effective_date"].dt.strftime("%Y-%m-%d") == "2022-12-01"].iloc[0]
    row_2024 = all_in_df.loc[all_in_df["effective_date"].dt.strftime("%Y-%m-%d") == "2024-10-01"].iloc[0]

    assert row_2022["rider_coverage_status"] == "same_day"
    assert row_2024["rider_coverage_status"] == "same_day"
