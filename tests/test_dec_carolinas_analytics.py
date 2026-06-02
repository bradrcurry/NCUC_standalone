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


def test_dec_rs_rider_totals_report_component_reconciliation() -> None:
    totals_df, _components_df = load_dec_rs_rider_history(database_path=DB_PATH)

    assert {
        "component_sum_cents_per_kwh",
        "component_reconciliation_delta",
        "component_reconciliation_status",
    } <= set(totals_df.columns)

    early = totals_df[totals_df["effective_date"].dt.strftime("%Y-%m-%d") == "2022-01-01"].iloc[0]
    assert early["component_reconciliation_status"] == "reconciled"
    assert abs(float(early["component_reconciliation_delta"])) <= 0.005
    assert float(early["total_rider_cents_per_kwh"]) < 0

    latest = totals_df[totals_df["effective_date"].dt.strftime("%Y-%m-%d") == "2025-01-01"].iloc[0]
    assert latest["component_reconciliation_status"] == "component_gap"
    assert abs(float(latest["component_reconciliation_delta"])) > 0.005


def test_dec_all_in_history_carries_component_reconciliation_status() -> None:
    df = load_dec_rs_all_in_history(database_path=DB_PATH)

    assert "rider_component_reconciliation_status" in df.columns
    dec_2022 = df[df["effective_date"].dt.strftime("%Y-%m-%d") == "2022-01-01"].iloc[0]
    assert dec_2022["rider_component_reconciliation_status"] == "reconciled"
    assert float(dec_2022["blended_all_in_cents_per_kwh"]) < float(
        dec_2022["blended_base_cents_per_kwh"]
    )
