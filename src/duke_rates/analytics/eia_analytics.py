"""EIA-derived analytics functions.

All functions return pandas DataFrames.  They assume the EIA tables have
been populated via ``duke-rates eia-backfill`` or the scripts in
``scripts/``.

Functions are intentionally composable: each does one thing and returns a
tidy DataFrame that can be filtered, joined, or plotted directly.

Caution notes (embedded in docstrings):
- EIA data describes prices, sales, and generation at the state level.
  It does NOT explain why prices differ — that requires supplemental context
  (fuel costs, capital recovery, regulatory frameworks, transmission costs, etc.).
- Correlations between fuel mix and price are observable; causality is not
  established by these numbers alone.
- Data-center load growth effects are a hypothesis that requires non-EIA
  sources (e.g., county-level utility interconnection queues, commercial
  property records).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def _require_pandas():
    try:
        import pandas as pd
        return pd
    except ImportError:
        raise ImportError("pandas is required for EIA analytics. Install with: pip install pandas")


def _conn(database_path: Path | None = None) -> sqlite3.Connection:
    from duke_rates.config import get_settings
    path = database_path or get_settings().database_path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 1. Retail price time series
# ---------------------------------------------------------------------------

def load_price_history(
    *,
    states: list[str] | None = None,
    sector: str = "RES",
    frequency: str = "annual",
    start_year: int | None = None,
    end_year: int | None = None,
    database_path: Path | None = None,
):
    """Return a DataFrame of retail price history by state and year.

    Columns: state, state_name, year, month, period, price_cents_per_kwh,
             sales_million_kwh, revenue_million_dollars, customers

    Notes:
        - ``sector`` should be one of RES | COM | IND | ALL
        - Monthly data also available when ``frequency='monthly'``
        - Missing values (NULL in DB) remain as NaN
    """
    pd = _require_pandas()
    conn = _conn(database_path)

    query = """
        SELECT r.state, r.state_name, r.year, r.month, r.period,
               r.price_cents_per_kwh, r.sales_million_kwh,
               r.revenue_million_dollars, r.customers,
               reg.census_division, reg.census_region,
               mkt.market_structure, mkt.rto
        FROM eia_retail_sales r
        LEFT JOIN eia_state_region_lookup reg ON reg.state = r.state
        LEFT JOIN eia_market_structure_lookup mkt ON mkt.state = r.state
        WHERE r.sector = ? AND r.frequency = ?
    """
    params: list[Any] = [sector.upper(), frequency]

    if states:
        placeholders = ",".join("?" * len(states))
        query += f" AND r.state IN ({placeholders})"
        params.extend([s.upper() for s in states])
    if start_year:
        query += " AND r.year >= ?"
        params.append(start_year)
    if end_year:
        query += " AND r.year <= ?"
        params.append(end_year)

    query += " ORDER BY r.state, r.year, r.month"
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


# ---------------------------------------------------------------------------
# 2. State vs national benchmark
# ---------------------------------------------------------------------------

def load_state_vs_national(
    *,
    states: list[str] | None = None,
    sector: str = "RES",
    start_year: int | None = 2010,
    database_path: Path | None = None,
):
    """Return price delta (state - US national average) by state and year.

    Adds derived columns:
        - price_cents_per_kwh          (state price)
        - us_avg_cents_per_kwh         (national average)
        - delta_vs_us                  (state - US, ¢/kWh)
        - pct_vs_us                    (delta / US * 100)
        - yoy_price_change             (year-over-year absolute change)
        - yoy_price_change_pct         (year-over-year % change)
    """
    pd = _require_pandas()
    df = load_price_history(
        states=(states or []) + ["US"],
        sector=sector,
        frequency="annual",
        start_year=start_year,
        database_path=database_path,
    )
    if df.empty:
        return df

    us = df[df["state"] == "US"][["year", "price_cents_per_kwh"]].rename(
        columns={"price_cents_per_kwh": "us_avg_cents_per_kwh"}
    )
    state_df = df[df["state"] != "US"].copy()
    merged = state_df.merge(us, on="year", how="left")
    merged["delta_vs_us"] = merged["price_cents_per_kwh"] - merged["us_avg_cents_per_kwh"]
    merged["pct_vs_us"] = (merged["delta_vs_us"] / merged["us_avg_cents_per_kwh"] * 100).round(2)

    # YoY change per state
    merged.sort_values(["state", "year"], inplace=True)
    merged["yoy_price_change"] = merged.groupby("state")["price_cents_per_kwh"].diff()
    merged["yoy_price_change_pct"] = (
        merged["yoy_price_change"] / merged.groupby("state")["price_cents_per_kwh"].shift(1) * 100
    ).round(2)

    if states:
        merged = merged[merged["state"].isin([s.upper() for s in states])]

    return merged.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3. Fuel mix shares
# ---------------------------------------------------------------------------

def load_fuel_mix_shares(
    *,
    states: list[str] | None = None,
    start_year: int | None = 2001,
    end_year: int | None = None,
    database_path: Path | None = None,
):
    """Return annual generation fuel mix shares by state.

    Returns a wide-format DataFrame with one row per (state, year) and
    columns for each major fuel share:

        fuel_share_gas, fuel_share_coal, fuel_share_nuclear,
        fuel_share_hydro, fuel_share_wind, fuel_share_solar,
        fuel_share_petroleum, fuel_share_other_renewable,
        total_generation_mwh

    Notes:
        - Shares are 0.0–1.0 fractions of total generation
        - ``ALL`` fuel rows are used as the denominator
        - Small fuel types are lumped into fuel_share_other
        - Missing fuel types for a state/year have share = 0
    """
    pd = _require_pandas()
    conn = _conn(database_path)

    query = """
        SELECT g.state, g.year, g.fuel_type, g.generation_mwh,
               reg.census_division, reg.census_region,
               mkt.market_structure
        FROM eia_generation_by_fuel g
        LEFT JOIN eia_state_region_lookup reg ON reg.state = g.state
        LEFT JOIN eia_market_structure_lookup mkt ON mkt.state = g.state
        WHERE g.frequency = 'annual' AND g.sector = '99'
          AND g.generation_mwh IS NOT NULL
    """
    params: list[Any] = []

    if states:
        phs = ",".join("?" * len(states))
        query += f" AND g.state IN ({phs})"
        params.extend([s.upper() for s in states])
    if start_year:
        query += " AND g.year >= ?"
        params.append(start_year)
    if end_year:
        query += " AND g.year <= ?"
        params.append(end_year)

    df = pd.read_sql_query(query, conn, params=params)
    conn.close()

    if df.empty:
        return df

    # Pivot so each fuel is a column
    pivot = df.pivot_table(
        index=["state", "year", "census_division", "census_region", "market_structure"],
        columns="fuel_type",
        values="generation_mwh",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    pivot.columns.name = None

    total_col = pivot.get("ALL", None)
    if total_col is None:
        # Fall back: sum of all fuels
        fuel_cols = [c for c in pivot.columns if c not in ("state", "year", "census_division", "census_region", "market_structure")]
        total_col = pivot[fuel_cols].sum(axis=1)

    out = pivot[["state", "year", "census_division", "census_region", "market_structure"]].copy()
    out["total_generation_mwh"] = total_col

    def _share(col: str) -> "pd.Series":
        vals = pivot.get(col, 0)
        return (vals / total_col.replace(0, float("nan"))).fillna(0).round(4)

    out["fuel_share_gas"]               = _share("NG")
    out["fuel_share_coal"]              = _share("COW")
    out["fuel_share_nuclear"]           = _share("NUC")
    out["fuel_share_hydro"]             = _share("HYC")
    out["fuel_share_wind"]              = _share("WND")
    out["fuel_share_solar"]             = _share("SUN")
    out["fuel_share_petroleum"]         = _share("PET")

    # "Other renewable" = geo + bio — sum what's available
    other_ren = sum(pivot.get(c, 0) for c in ("GEO", "BIO"))
    out["fuel_share_other_renewable"]   = (other_ren / total_col.replace(0, float("nan"))).fillna(0).round(4)

    return out.sort_values(["state", "year"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4. Price vs fuel mix scatter dataset
# ---------------------------------------------------------------------------

def load_price_vs_fuel_mix(
    *,
    sector: str = "RES",
    year: int | None = None,
    database_path: Path | None = None,
):
    """Return a per-state dataset suitable for scatter plots of price vs fuel mix.

    Merges retail price with fuel mix shares.  One row per (state, year).
    Use for: "states with more gas generation vs retail price" scatter charts.

    CAUTION: correlation in this dataset is observational.  Prices are driven
    by many factors (capital costs, transmission, regulation, weather) beyond
    fuel mix alone.  Treat any pattern as a hypothesis, not a conclusion.
    """
    pd = _require_pandas()

    states_all = None  # all states
    prices = load_price_history(states=states_all, sector=sector, frequency="annual",
                                 database_path=database_path)
    mix = load_fuel_mix_shares(database_path=database_path)

    if prices.empty or mix.empty:
        return pd.DataFrame()

    merged = prices.merge(mix[["state", "year"] + [c for c in mix.columns if c.startswith("fuel_share_") or c == "total_generation_mwh"]],
                          on=["state", "year"], how="inner")
    if year:
        merged = merged[merged["year"] == year]

    return merged.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. Annual price rankings by state
# ---------------------------------------------------------------------------

def load_price_rankings(
    *,
    year: int,
    sector: str = "RES",
    database_path: Path | None = None,
):
    """Return all states ranked cheapest to most expensive for a given year.

    Adds columns: rank (1=cheapest), delta_vs_us, market_structure, rto,
    census_division, census_region.
    """
    pd = _require_pandas()
    df = load_state_vs_national(sector=sector, start_year=year, database_path=database_path)
    if df.empty:
        return df

    df = df[df["year"] == year].copy()
    df = df[df["state"] != "US"]
    df = df[df["price_cents_per_kwh"].notna()]
    df.sort_values("price_cents_per_kwh", inplace=True)
    df["rank"] = range(1, len(df) + 1)
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 6. Duke state context table
# ---------------------------------------------------------------------------

def load_duke_state_context(
    *,
    sector: str = "RES",
    years: int = 10,
    database_path: Path | None = None,
):
    """Return recent price history for Duke-served states vs US average.

    Duke Energy operates in NC, SC, IN, OH, KY, FL.
    Use this to annotate Duke-focused dashboards with state-level EIA context.

    Returns a DataFrame with columns:
        state, year, price_cents_per_kwh, us_avg_cents_per_kwh,
        delta_vs_us, pct_vs_us, market_structure, rto
    """
    from duke_rates.eia.endpoints import DUKE_STATES
    from datetime import datetime
    current_year = datetime.now().year
    start_year = current_year - years

    df = load_state_vs_national(
        states=DUKE_STATES,
        sector=sector,
        start_year=start_year,
        database_path=database_path,
    )
    return df


# ---------------------------------------------------------------------------
# 7. Southeast regional comparison
# ---------------------------------------------------------------------------

def load_southeast_comparison(
    *,
    sector: str = "RES",
    start_year: int = 2010,
    database_path: Path | None = None,
):
    """Return price trend for Southeast states with US benchmark.

    Southeast: NC, SC, VA, GA, TN, FL, AL, MS, KY, WV
    """
    from duke_rates.eia.endpoints import SOUTHEAST_STATES
    df = load_state_vs_national(
        states=SOUTHEAST_STATES,
        sector=sector,
        start_year=start_year,
        database_path=database_path,
    )
    return df


# ---------------------------------------------------------------------------
# 8. Market structure comparison
# ---------------------------------------------------------------------------

def load_monthly_fuel_mix_shares(
    *,
    states: list[str] | None = None,
    start_year: int | None = 2010,
    end_year: int | None = None,
    database_path: Path | None = None,
):
    """Return monthly generation fuel mix shares by state for seasonal analysis.

    Requires the monthly generation backfill pass (``duke-rates eia-backfill``
    without ``--skip-monthly-generation``).  Returns an empty DataFrame if
    monthly generation data is not yet loaded.

    Columns: state, year, month, period, census_division, census_region,
             market_structure, total_generation_mwh, fuel_share_gas,
             fuel_share_coal, fuel_share_nuclear, fuel_share_hydro,
             fuel_share_wind, fuel_share_solar

    Notes:
        - Useful for seasonal questions: summer gas peaking, winter nuclear
          baseload share, solar growth by month.
        - Shares are 0.0–1.0 fractions of total (ALL) generation.
        - Small fuel types not individually tracked are excluded.
    """
    pd = _require_pandas()
    conn = _conn(database_path)

    query = """
        SELECT g.state, g.year, g.month, g.period, g.fuel_type, g.generation_mwh,
               reg.census_division, reg.census_region,
               mkt.market_structure
        FROM eia_generation_by_fuel g
        LEFT JOIN eia_state_region_lookup reg ON reg.state = g.state
        LEFT JOIN eia_market_structure_lookup mkt ON mkt.state = g.state
        WHERE g.frequency = 'monthly' AND g.sector = '99'
          AND g.generation_mwh IS NOT NULL
          AND g.month IS NOT NULL
    """
    params: list[Any] = []

    if states:
        phs = ",".join("?" * len(states))
        query += f" AND g.state IN ({phs})"
        params.extend([s.upper() for s in states])
    if start_year:
        query += " AND g.year >= ?"
        params.append(start_year)
    if end_year:
        query += " AND g.year <= ?"
        params.append(end_year)

    df = pd.read_sql_query(query, conn, params=params)
    conn.close()

    if df.empty:
        return df

    pivot = df.pivot_table(
        index=["state", "year", "month", "period", "census_division", "census_region", "market_structure"],
        columns="fuel_type",
        values="generation_mwh",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    pivot.columns.name = None

    total_col = pivot.get("ALL", None)
    if total_col is None:
        fuel_cols = [c for c in pivot.columns if c not in ("state", "year", "month", "period", "census_division", "census_region", "market_structure")]
        total_col = pivot[fuel_cols].sum(axis=1)

    out = pivot[["state", "year", "month", "period", "census_division", "census_region", "market_structure"]].copy()
    out["total_generation_mwh"] = total_col

    def _share(col: str) -> "pd.Series":
        vals = pivot.get(col, 0)
        return (vals / total_col.replace(0, float("nan"))).fillna(0).round(4)

    out["fuel_share_gas"]      = _share("NG")
    out["fuel_share_coal"]     = _share("COW")
    out["fuel_share_nuclear"]  = _share("NUC")
    out["fuel_share_hydro"]    = _share("HYC")
    out["fuel_share_wind"]     = _share("WND")
    out["fuel_share_solar"]    = _share("SUN")

    return out.sort_values(["state", "year", "month"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 9. Duke + EIA revenue reconciliation
# ---------------------------------------------------------------------------

def load_duke_eia_revenue_reconciliation(
    *,
    states: list[str] | None = None,
    sector: str = "RES",
    start_year: int = 2016,
    end_year: int | None = None,
    database_path: Path | None = None,
):
    """Compare EIA implied residential price to Duke tariff-derived rate estimates.

    EIA ``retail-sales`` provides ``revenue_million_dollars`` and
    ``sales_million_kwh`` by state/sector, from which an implied average price
    can be computed:

        implied_price_cents_per_kwh = (revenue * 1e6 / (sales * 1e6)) * 100

    This should be broadly comparable to DEP/DEC all-in tariff estimates.
    Divergence between the two signals:
    - Rate increases captured by tariff engine but not yet reflected in EIA data
    - Non-tariff revenue components (e.g., standby charges, connection fees)
    - Sampling differences (EIA covers all NC utilities, not just Duke)

    Returns a DataFrame with columns:
        state, year, sector, eia_reported_price_cents,
        eia_implied_price_cents, price_delta_reported_vs_implied,
        sales_million_kwh, revenue_million_dollars

    Notes:
        - EIA ``price`` field is the directly reported average price.
        - ``eia_implied_price_cents`` is independently derived from
          revenue / sales — useful to cross-check the reported value.
        - Duke tariff engine estimates are NOT included here (they require
          a separate call to the billing engine with a representative kWh).
          Use this function's output as the EIA side of the comparison.
        - NC EIA data covers all NC utilities, not just Duke — treat as
          state-average context, not a Duke-specific benchmark.

    CAUTION: This is observational context, not a direct audit of Duke billing.
    Many factors outside the tariff schedule affect state-average EIA prices.
    """
    pd = _require_pandas()

    df = load_price_history(
        states=states or ["NC", "SC"],
        sector=sector,
        frequency="annual",
        start_year=start_year,
        end_year=end_year,
        database_path=database_path,
    )
    if df.empty:
        return df

    df = df.copy()
    # Derived implied price: revenue ($M) / sales (MWh * 1000) * 100 to get ¢/kWh
    # sales_million_kwh * 1e6 kWh per million, revenue_million_dollars * 1e6 $ per million
    # ¢/kWh = ($ / kWh) * 100
    df["eia_implied_price_cents"] = (
        (df["revenue_million_dollars"] * 1e6) /
        (df["sales_million_kwh"] * 1e6) * 100
    ).round(4)

    df = df.rename(columns={"price_cents_per_kwh": "eia_reported_price_cents"})
    df["price_delta_reported_vs_implied"] = (
        df["eia_reported_price_cents"] - df["eia_implied_price_cents"]
    ).round(4)

    keep_cols = [
        "state", "state_name", "year", "sector",
        "eia_reported_price_cents", "eia_implied_price_cents",
        "price_delta_reported_vs_implied",
        "sales_million_kwh", "revenue_million_dollars",
        "census_division", "market_structure",
    ]
    available = [c for c in keep_cols if c in df.columns]
    return df[available].sort_values(["state", "year"]).reset_index(drop=True)


def load_market_structure_comparison(
    *,
    year: int,
    sector: str = "RES",
    database_path: Path | None = None,
):
    """Return median/mean price by market structure category for a year.

    market_structure values: regulated | hybrid | restructured

    CAUTION: market structure is one factor among many.  State size, fuel mix,
    geographic access to cheap fuel, and climate all affect prices
    independently of regulatory structure.

    Returns a summary DataFrame with columns:
        market_structure, state_count, median_price, mean_price,
        min_price, max_price, states_list
    """
    pd = _require_pandas()
    df = load_price_rankings(year=year, sector=sector, database_path=database_path)
    if df.empty or "market_structure" not in df.columns:
        return pd.DataFrame()

    grouped = df[df["market_structure"].notna()].groupby("market_structure")
    rows = []
    for struct, grp in grouped:
        rows.append({
            "market_structure": struct,
            "state_count": len(grp),
            "median_price_cents": grp["price_cents_per_kwh"].median().round(2),
            "mean_price_cents": grp["price_cents_per_kwh"].mean().round(2),
            "min_price_cents": grp["price_cents_per_kwh"].min().round(2),
            "max_price_cents": grp["price_cents_per_kwh"].max().round(2),
            "states": ", ".join(sorted(grp["state"].tolist())),
        })
    return pd.DataFrame(rows).sort_values("median_price_cents")
