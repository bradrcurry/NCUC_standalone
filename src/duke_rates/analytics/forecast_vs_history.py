"""Backtest-adjacent check: Duke's load forecast vs realized history.

The classic 'prior-vintage forecast vs actual' backtest needs the 2022 rate-case
application PDFs (E-2 Sub 1300 / E-7 Sub 1276), which are not held locally and
would require an NCUC portal fetch. With on-hand data we can still answer the
sharper question — *is this forecast a break from history?* — by comparing the
harvested Duke forecast (DEC + DEP combined) against realized NC retail sales
from EIA.

Caveat surfaced to the reader: EIA actuals are **NC state-level** (DEC + DEP +
Dominion NC + co-ops/munis) while the forecast is **DEC + DEP only**, so absolute
levels differ. We therefore compare **growth** — CAGR and an index to the 2025
overlap year — which is robust to the territory mismatch.
"""

from __future__ import annotations

from pathlib import Path

from duke_rates.analytics.dep_progress import _require_pandas
from duke_rates.analytics.proposed_forecast import load_proposed_load_forecast
from duke_rates.db.sqlite import connect

OVERLAP_YEAR = 2025


def _cagr(start: float, end: float, years: int) -> float | None:
    if not start or not end or years <= 0 or start <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def _nc_actual_total(pd, database_path: Path | None):
    db = _database_path(database_path)
    conn = connect(db)
    try:
        rows = conn.execute(
            """
            SELECT year, sales_million_kwh AS gwh
            FROM eia_retail_sales
            WHERE state='NC' AND frequency='annual' AND sector='ALL'
              AND sales_million_kwh IS NOT NULL
            ORDER BY year
            """
        ).fetchall()
    except Exception:
        return pd.DataFrame(columns=["year", "gwh"])
    finally:
        conn.close()
    return pd.DataFrame([{"year": int(r["year"]), "gwh": float(r["gwh"])} for r in rows])


def load_load_growth_continuity(*, database_path: Path | None = None):
    """Return one tidy frame: realized NC history then the Duke forecast, with an
    index (overlap year = 100) so growth is comparable despite level mismatch.

    Columns: ``series, year, gwh, indexed`` where ``series`` is
    ``"NC actual (EIA)"`` or ``"Duke DEC+DEP forecast"``.
    """
    pd = _require_pandas()
    actual = _nc_actual_total(pd, database_path)
    fc = load_proposed_load_forecast(database_path=database_path)
    if not fc.empty:
        fc_total = (
            fc[(fc["table_type"] == "retail_sales") & (fc["segment"] == "Total")]
            .groupby("year", as_index=False)["value"]
            .sum()
            .rename(columns={"value": "gwh"})
        )
    else:
        fc_total = pd.DataFrame(columns=["year", "gwh"])

    frames = []
    for label, df in [("NC actual (EIA)", actual), ("Duke DEC+DEP forecast", fc_total)]:
        if df.empty:
            continue
        df = df.copy()
        base_row = df[df["year"] == OVERLAP_YEAR]
        base = float(base_row["gwh"].iloc[0]) if not base_row.empty else float(df["gwh"].iloc[0])
        df["indexed"] = df["gwh"] / base * 100.0
        df["series"] = label
        frames.append(df[["series", "year", "gwh", "indexed"]])
    if not frames:
        return pd.DataFrame(columns=["series", "year", "gwh", "indexed"])
    return pd.concat(frames, ignore_index=True)


def load_load_growth_cagr(*, database_path: Path | None = None):
    """Return CAGR comparison rows: realized history vs forecast, total + class.

    Columns: ``scope, basis, start_year, end_year, cagr_pct``.
    """
    pd = _require_pandas()
    rows: list[dict[str, object]] = []
    actual = _nc_actual_total(pd, database_path)
    if not actual.empty:
        a = actual.set_index("year")["gwh"].to_dict()
        ys = sorted(a)
        rows.append(
            {
                "scope": "Total",
                "basis": "NC actual (EIA)",
                "start_year": ys[0],
                "end_year": ys[-1],
                "cagr_pct": _pct(_cagr(a[ys[0]], a[ys[-1]], ys[-1] - ys[0])),
            }
        )
        if 2019 in a and 2024 in a:
            rows.append(
                {
                    "scope": "Total",
                    "basis": "NC actual (EIA) 2019–24",
                    "start_year": 2019,
                    "end_year": 2024,
                    "cagr_pct": _pct(_cagr(a[2019], a[2024], 5)),
                }
            )

    fc = load_proposed_load_forecast(database_path=database_path)
    if not fc.empty:
        rs = fc[fc["table_type"] == "retail_sales"]
        for scope in ["Total", "Residential", "Commercial", "Industrial"]:
            ser = rs[rs["segment"] == scope].groupby("year")["value"].sum().to_dict()
            if 2025 in ser and 2040 in ser:
                rows.append(
                    {
                        "scope": scope,
                        "basis": "Duke forecast 2025–40",
                        "start_year": 2025,
                        "end_year": 2040,
                        "cagr_pct": _pct(_cagr(ser[2025], ser[2040], 15)),
                    }
                )
            if scope == "Total" and 2025 in ser and 2030 in ser:
                rows.append(
                    {
                        "scope": "Total",
                        "basis": "Duke forecast 2025–30 (near-term)",
                        "start_year": 2025,
                        "end_year": 2030,
                        "cagr_pct": _pct(_cagr(ser[2025], ser[2030], 5)),
                    }
                )
    return pd.DataFrame(rows)


def _pct(value: float | None) -> float | None:
    return round(value * 100.0, 2) if value is not None else None


def _database_path(database_path: Path | None) -> Path:
    if database_path is not None:
        return Path(database_path)
    from duke_rates.config import get_settings

    return get_settings().database_path
