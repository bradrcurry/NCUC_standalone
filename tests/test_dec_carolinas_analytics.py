from __future__ import annotations

from pathlib import Path

from duke_rates.analytics.dec_carolinas import (
    load_dec_rs_all_in_history,
    load_dec_rs_base_history,
    load_dec_rs_rider_history,
)


DB_PATH = Path("data/db/duke_rates.db")


def test_dec_rs_base_history_has_expected_window() -> None:
    df = load_dec_rs_base_history(database_path=DB_PATH)

    assert not df.empty
    assert str(df["effective_date"].min().date()) == "2018-09-01"
    assert str(df["effective_date"].max().date()) == "2026-01-01"


def test_dec_rs_all_in_history_coverage_statuses() -> None:
    df = load_dec_rs_all_in_history(database_path=DB_PATH)

    assert not df.empty
    assert set(df["rider_coverage_status"]) <= {"carried_forward", "same_day", "base_only"}
    assert {"carried_forward", "same_day"} <= set(df["rider_coverage_status"])


def test_dec_rs_rider_history_has_leaf_99_snapshot() -> None:
    totals_df, components_df = load_dec_rs_rider_history(database_path=DB_PATH)

    assert not totals_df.empty
    assert str(totals_df["effective_date"].min().date()) == "2018-08-01"
    assert float(totals_df.iloc[0]["total_rider_cents_per_kwh"]) == 0.3335
    assert not components_df.empty
    assert {"FCA", "EE", "DSM", "CPRE", "EDIT-4", "RAL", "CAR", "RDM", "ESM", "PIM"} <= set(
        components_df["rider_code"]
    )
