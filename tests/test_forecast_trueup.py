"""Tests for the true-up forecast-accuracy series.

The canonical rider-component loaders are monkeypatched with small frames so the
reshape/interpretation logic is exercised without the full tariff lineage.
"""

import pandas as pd

import duke_rates.analytics.forecast_trueup as tu


def _frame(rows):
    return pd.DataFrame(rows, columns=["effective_date", "rider_code", "cents_per_kwh"])


def _patch(monkeypatch, dep_rows, dec_rows):
    monkeypatch.setattr(
        tu, "load_dep_res_canonical_rider_components", lambda **_: _frame(dep_rows)
    )
    monkeypatch.setattr(
        tu, "load_dec_rs_canonical_rider_components", lambda **_: _frame(dec_rows)
    )


def test_series_maps_riders_to_categories_and_interpretation(monkeypatch) -> None:
    _patch(
        monkeypatch,
        dep_rows=[
            ("2024-12-01", "BA-EMF", 0.58),
            ("2025-01-01", "RDM", 0.232),
            ("2025-01-01", "ESM", 0.0),
            ("2025-01-01", "BA-DSM", 0.767),  # not a true-up rider -> ignored
        ],
        dec_rows=[
            ("2024-01-01", "FCA", 2.296),
            ("2025-01-01", "FCA", 0.178),
        ],
    )
    series = tu.load_forecast_trueup_series()
    cats = set(series["category"])
    assert cats == {"Fuel cost", "Residential decoupling", "Earnings sharing"}
    # BA-DSM is not a true-up rider and must not appear.
    assert "BA-DSM" not in set(series["rider_code"])
    dep_rdm = series[(series.utility == "DEP") & (series.category == "Residential decoupling")]
    assert float(dep_rdm["cents_per_kwh"].iloc[0]) == 0.232
    assert "over-forecast" in dep_rdm["positive_means"].iloc[0]
    fuel = series[series.category == "Fuel cost"]
    assert "under-forecast" in fuel["positive_means"].iloc[0]


def test_summary_picks_latest_and_absolute_peak(monkeypatch) -> None:
    _patch(
        monkeypatch,
        dep_rows=[("2026-01-01", "BA-EMF", 0.518)],
        dec_rows=[
            ("2023-06-01", "FCA", 0.885),
            ("2024-01-01", "FCA", 2.296),  # peak
            ("2025-01-01", "FCA", 0.178),  # latest
        ],
    )
    summ = tu.summarize_trueup()
    dec_fuel = summ[(summ.utility == "DEC") & (summ.category == "Fuel cost")].iloc[0]
    assert dec_fuel["latest_cents"] == 0.178
    assert dec_fuel["peak_cents"] == 2.296
    assert str(dec_fuel["peak_date"])[:10] == "2024-01-01"


def test_empty_sources_return_empty_frame(monkeypatch) -> None:
    _patch(monkeypatch, dep_rows=[], dec_rows=[])
    series = tu.load_forecast_trueup_series()
    assert series.empty
    assert list(series.columns) == [
        "utility",
        "category",
        "rider_code",
        "effective_date",
        "cents_per_kwh",
        "positive_means",
    ]
