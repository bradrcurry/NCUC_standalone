"""Tests for the forecast-vs-history growth comparison."""

import pandas as pd

import duke_rates.analytics.forecast_vs_history as fvh


def _patch(monkeypatch, actual_rows, forecast_rows):
    monkeypatch.setattr(
        fvh,
        "_nc_actual_total",
        lambda pd_, database_path: pd.DataFrame(actual_rows, columns=["year", "gwh"]),
    )
    monkeypatch.setattr(
        fvh,
        "load_proposed_load_forecast",
        lambda **_: pd.DataFrame(
            forecast_rows, columns=["utility", "table_type", "segment", "year", "value"]
        ),
    )


def _fc(util, seg, pairs):
    return [(util, "retail_sales", seg, y, v) for y, v in pairs]


def test_continuity_indexes_both_series_to_overlap_year(monkeypatch) -> None:
    _patch(
        monkeypatch,
        actual_rows=[(2019, 100.0), (2025, 110.0)],
        forecast_rows=(
            _fc("DEC", "Total", [(2025, 80.0), (2040, 120.0)])
            + _fc("DEP", "Total", [(2025, 20.0), (2040, 40.0)])
        ),
    )
    cont = fvh.load_load_growth_continuity()
    # Both series indexed to 2025 = 100.
    act_2025 = cont[(cont.series == "NC actual (EIA)") & (cont.year == 2025)]
    fc_2025 = cont[(cont.series == "Duke DEC+DEP forecast") & (cont.year == 2025)]
    assert float(act_2025["indexed"].iloc[0]) == 100.0
    assert float(fc_2025["indexed"].iloc[0]) == 100.0
    # Forecast combines DEC+DEP: 2025 = 100, 2040 = 160 -> indexed 160.
    fc_2040 = cont[(cont.series == "Duke DEC+DEP forecast") & (cont.year == 2040)]
    assert float(fc_2040["gwh"].iloc[0]) == 160.0
    assert round(float(fc_2040["indexed"].iloc[0]), 1) == 160.0


def test_cagr_compares_actual_vs_forecast(monkeypatch) -> None:
    _patch(
        monkeypatch,
        actual_rows=[(2019, 136436.0), (2024, 136905.0), (2025, 140584.0)],
        forecast_rows=(
            _fc("DEC", "Total", [(2025, 82488.0), (2030, 100000.0), (2040, 121121.0)])
            + _fc("DEP", "Total", [(2025, 44003.0), (2030, 50000.0), (2040, 59451.0)])
            + _fc("DEC", "Commercial", [(2025, 30535.0), (2040, 51611.0)])
            + _fc("DEP", "Commercial", [(2025, 13819.0), (2040, 22012.0)])
        ),
    )
    cg = fvh.load_load_growth_cagr()
    by = {(r.scope, r.basis): r.cagr_pct for r in cg.itertuples()}
    # Flat history vs accelerating forecast.
    assert by[("Total", "NC actual (EIA) 2019–24")] == 0.07
    assert by[("Total", "Duke forecast 2025–40")] == 2.40
    assert by[("Total", "Duke forecast 2025–30 (near-term)")] > 2.40  # front-loaded
    assert by[("Commercial", "Duke forecast 2025–40")] > by[("Total", "Duke forecast 2025–40")]


def test_empty_inputs_are_safe(monkeypatch) -> None:
    _patch(monkeypatch, actual_rows=[], forecast_rows=[])
    assert fvh.load_load_growth_continuity().empty
    assert fvh.load_load_growth_cagr().empty
