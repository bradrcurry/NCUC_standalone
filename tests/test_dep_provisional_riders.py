from __future__ import annotations

from pathlib import Path

from duke_rates.analytics.dep_provisional_riders import load_dep_res_provisional_rider_history


DB_PATH = Path("data/db/duke_rates.db")


def test_dep_res_provisional_rider_history_has_pre_2023_rows() -> None:
    totals_df, components_df = load_dep_res_provisional_rider_history(database_path=DB_PATH)

    assert not totals_df.empty
    assert str(totals_df["effective_date"].min().date()) == "2016-12-01"
    assert not components_df.empty
    assert {"BA", "JAA"} <= set(components_df["rider_code"])
