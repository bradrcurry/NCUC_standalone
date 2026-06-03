"""Forecast-accuracy 'track record' from Duke's own true-up riders.

Under PBR/MYRP, several riders are explicit *reconciliations of actual vs
forecast* that Duke files and the Commission approves. Their historical values
are therefore a forecast-error time series straight from the utility's own
verified numbers — no external data required:

* **Fuel** (DEC ``FCA``; DEP ``BA-EMF`` Experience Modification Factor) — trues
  up actual fuel cost against the projection embedded in base rates. Positive =
  actual fuel ran *above* forecast (under-collected); customers repay the gap.
* **Decoupling** (``RDM``) — trues up actual residential revenue-per-customer
  against the target set in the last rate case. Positive = actual sales/revenue
  came in *below* forecast (over-forecast); the rider collects the shortfall.
* **Earnings** (``ESM``) — trues up actual earnings against the authorized ROE
  band. Positive = the utility over-earned and is refunding the excess.

This reads the accepted canonical rider-component timelines (not the proposed
lane) and reshapes the true-up riders into a tidy, interpreted series.
"""

from __future__ import annotations

from pathlib import Path

from duke_rates.analytics.canonical_rider_components import (
    load_dec_rs_canonical_rider_components,
    load_dep_res_canonical_rider_components,
)
from duke_rates.analytics.dep_progress import _require_pandas

# Which rider code carries each true-up signal, per utility.
TRUEUP_RIDERS: dict[str, dict[str, str]] = {
    "Fuel cost": {"DEC": "FCA", "DEP": "BA-EMF"},
    "Residential decoupling": {"DEC": "RDM", "DEP": "RDM"},
    "Earnings sharing": {"DEC": "ESM", "DEP": "ESM"},
}

# Plain-language reading of a positive value.
POSITIVE_MEANS: dict[str, str] = {
    "Fuel cost": (
        "actual fuel cost ran above the projection in base rates "
        "(fuel under-forecast); customers repay the gap"
    ),
    "Residential decoupling": (
        "actual residential revenue per customer came in below the target set "
        "in the last case (sales over-forecast); the rider collects the shortfall"
    ),
    "Earnings sharing": (
        "the utility earned above its authorized ROE band and is refunding the "
        "excess"
    ),
}


def load_forecast_trueup_series(*, database_path: Path | None = None):
    """Return a tidy true-up series: one row per (utility, category, date).

    Columns: ``utility, category, rider_code, effective_date, cents_per_kwh,
    positive_means``.
    """
    pd = _require_pandas()
    sources = {
        "DEP": load_dep_res_canonical_rider_components(database_path=database_path),
        "DEC": load_dec_rs_canonical_rider_components(database_path=database_path),
    }
    records: list[dict[str, object]] = []
    for utility, df in sources.items():
        if df is None or df.empty:
            continue
        for category, mapping in TRUEUP_RIDERS.items():
            code = mapping.get(utility)
            if code is None:
                continue
            sub = df[df["rider_code"] == code]
            for row in sub.itertuples():
                records.append(
                    {
                        "utility": utility,
                        "category": category,
                        "rider_code": code,
                        "effective_date": getattr(row, "effective_date"),
                        "cents_per_kwh": float(getattr(row, "cents_per_kwh")),
                        "positive_means": POSITIVE_MEANS[category],
                    }
                )
    if not records:
        return pd.DataFrame(
            columns=[
                "utility",
                "category",
                "rider_code",
                "effective_date",
                "cents_per_kwh",
                "positive_means",
            ]
        )
    out = pd.DataFrame(records)
    out["effective_date"] = pd.to_datetime(out["effective_date"], errors="coerce")
    return out.sort_values(["category", "utility", "effective_date"]).reset_index(
        drop=True
    )


def summarize_trueup(database_path: Path | None = None):
    """Return the latest and peak absolute true-up per (utility, category).

    Columns: ``utility, category, latest_date, latest_cents, peak_cents,
    peak_date, positive_means``.
    """
    pd = _require_pandas()
    series = load_forecast_trueup_series(database_path=database_path)
    if series.empty:
        return series
    rows = []
    for (utility, category), grp in series.groupby(["utility", "category"]):
        grp = grp.sort_values("effective_date")
        latest = grp.iloc[-1]
        peak = grp.iloc[grp["cents_per_kwh"].abs().argmax()]
        rows.append(
            {
                "utility": utility,
                "category": category,
                "latest_date": latest["effective_date"],
                "latest_cents": float(latest["cents_per_kwh"]),
                "peak_cents": float(peak["cents_per_kwh"]),
                "peak_date": peak["effective_date"],
                "positive_means": POSITIVE_MEANS[category],
            }
        )
    return pd.DataFrame(rows).sort_values(["category", "utility"]).reset_index(drop=True)
