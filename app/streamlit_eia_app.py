"""EIA National Electricity Context — Streamlit App.

Visualizes EIA state-level electricity data alongside Duke tariff context.

Run::

    streamlit run streamlit_eia_app.py

Requires that eia_retail_sales and related tables have been populated:

    duke-rates eia-backfill

or::

    duke-rates eia-backfill --states NC SC VA GA TN FL IN OH KY

"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ is importable when run from project root
sys.path.insert(0, str(Path(__file__).parent / "src"))

import streamlit as st

st.set_page_config(
    page_title="EIA Electricity Context",
    layout="wide",
    initial_sidebar_state="expanded",
)

try:
    import pandas as pd
    import plotly.express as px
    import plotly.graph_objects as go
except ImportError:
    st.error("Missing dependencies. Install with: pip install pandas plotly streamlit")
    st.stop()

from duke_rates.analytics.eia_analytics import (
    load_duke_state_context,
    load_fuel_mix_shares,
    load_market_structure_comparison,
    load_price_history,
    load_price_rankings,
    load_price_vs_fuel_mix,
    load_southeast_comparison,
    load_state_vs_national,
)
from duke_rates.config import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def _price_history(states, sector, freq, start_year):
    return load_price_history(states=states or None, sector=sector, frequency=freq, start_year=start_year)


@st.cache_data(ttl=3600)
def _southeast(sector, start_year):
    return load_southeast_comparison(sector=sector, start_year=start_year)


@st.cache_data(ttl=3600)
def _rankings(year, sector):
    return load_price_rankings(year=year, sector=sector)


@st.cache_data(ttl=3600)
def _fuel_mix(states, start_year):
    return load_fuel_mix_shares(states=states or None, start_year=start_year)


@st.cache_data(ttl=3600)
def _price_vs_mix(sector, year):
    return load_price_vs_fuel_mix(sector=sector, year=year)


@st.cache_data(ttl=3600)
def _market_comparison(year, sector):
    return load_market_structure_comparison(year=year, sector=sector)


def _check_data(df, label="data"):
    if df is None or df.empty:
        st.warning(
            f"No {label} found. Run **`duke-rates eia-backfill`** to populate the EIA tables, "
            "then reload this page."
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("EIA Context Settings")
    sector = st.selectbox("Sector", ["RES", "COM", "IND", "ALL"],
                          format_func=lambda x: {"RES": "Residential", "COM": "Commercial",
                                                  "IND": "Industrial", "ALL": "All Sectors"}[x])
    start_year = st.slider("Start Year", 2001, 2023, 2010)
    current_year = st.slider("Focus Year (rankings)", 2010, 2025, 2023)
    st.markdown("---")
    st.caption("Data: EIA Open Data API v2")
    st.caption("Prices: ¢/kWh average retail")
    st.caption("⚠️ Correlations shown are observational — not causal explanations.")

# ---------------------------------------------------------------------------
# Page tabs
# ---------------------------------------------------------------------------

tab_se, tab_national, tab_fuelmix, tab_scatter, tab_market, tab_duke = st.tabs([
    "Southeast Trends",
    "National Rankings",
    "Fuel Mix",
    "Price vs Fuel Mix",
    "Market Structure",
    "Duke State Context",
])

# ---- Tab 1: Southeast Trends ----
with tab_se:
    st.header(f"Southeast Retail Price Trends — {sector}")
    st.caption("States: NC, SC, VA, GA, TN, FL, AL, MS, KY, WV + US average")

    df_se = _southeast(sector, start_year)
    if _check_data(df_se, "Southeast data"):
        # Add US national average
        df_us = _price_history(["US"], sector, "annual", start_year)
        if not df_us.empty:
            df_us["state"] = "US (avg)"
            df_plot = pd.concat([df_se, df_us[["state", "year", "price_cents_per_kwh"]]])
        else:
            df_plot = df_se

        fig = px.line(
            df_plot[df_plot["price_cents_per_kwh"].notna()],
            x="year", y="price_cents_per_kwh", color="state",
            title=f"Average Retail Electricity Price — Southeast ({sector})",
            labels={"price_cents_per_kwh": "¢/kWh", "year": "Year", "state": "State"},
            template="plotly_white",
        )
        fig.update_layout(hovermode="x unified")
        # Highlight NC
        for trace in fig.data:
            if trace.name == "NC":
                trace.line.width = 3
                trace.line.dash = "solid"
        st.plotly_chart(fig, use_container_width=True)

        # YoY change table
        st.subheader("Year-over-Year Change")
        nc_df = df_se[df_se["state"] == "NC"].copy()
        if not nc_df.empty and "yoy_price_change_pct" in nc_df.columns:
            nc_recent = nc_df[["year", "price_cents_per_kwh", "yoy_price_change", "yoy_price_change_pct"]].dropna(subset=["yoy_price_change"])
            st.dataframe(
                nc_recent.rename(columns={
                    "year": "Year", "price_cents_per_kwh": "¢/kWh",
                    "yoy_price_change": "YoY Δ¢", "yoy_price_change_pct": "YoY Δ%"
                }).set_index("Year").sort_index(ascending=False),
                use_container_width=True,
            )


# ---- Tab 2: National Rankings ----
with tab_national:
    st.header(f"National Price Rankings — {current_year} / {sector}")
    st.caption(f"All 50 states + DC. Ranked cheapest (1) to most expensive. US average shown as reference.")

    df_rank = _rankings(current_year, sector)
    if _check_data(df_rank, "rankings data"):
        df_us_avg = _price_history(["US"], sector, "annual", current_year)
        us_avg = df_us_avg[df_us_avg["year"] == current_year]["price_cents_per_kwh"].iloc[0] if not df_us_avg.empty else None

        col1, col2, col3 = st.columns(3)
        nc_rows = df_rank[df_rank["state"] == "NC"]
        if not nc_rows.empty:
            nc = nc_rows.iloc[0]
            col1.metric("NC Price", f"{nc['price_cents_per_kwh']:.2f} ¢/kWh",
                        f"{nc.get('delta_vs_us', 0):+.2f} vs US avg")
            col2.metric("NC Rank", f"{int(nc['rank'])} of {len(df_rank)}",
                        "1=cheapest")
        if us_avg:
            col3.metric("US Average", f"{us_avg:.2f} ¢/kWh")

        fig = px.bar(
            df_rank.sort_values("rank"),
            x="state", y="price_cents_per_kwh",
            color="market_structure",
            color_discrete_map={"regulated": "#3b82f6", "hybrid": "#f59e0b", "restructured": "#10b981"},
            title=f"Retail Electricity Price by State — {current_year} ({sector})",
            labels={"price_cents_per_kwh": "¢/kWh", "state": "State"},
            template="plotly_white",
        )
        if us_avg:
            fig.add_hline(y=us_avg, line_dash="dot", line_color="red",
                          annotation_text=f"US avg {us_avg:.2f}¢", annotation_position="top right")
        st.plotly_chart(fig, use_container_width=True)

        st.dataframe(
            df_rank[["rank", "state", "state_name", "price_cents_per_kwh",
                      "delta_vs_us", "pct_vs_us", "market_structure", "rto", "census_division"]
            ].rename(columns={
                "rank": "Rank", "state": "State", "state_name": "State Name",
                "price_cents_per_kwh": "¢/kWh", "delta_vs_us": "Δ vs US",
                "pct_vs_us": "% vs US", "market_structure": "Market", "rto": "RTO",
                "census_division": "Division",
            }),
            use_container_width=True,
            height=500,
        )


# ---- Tab 3: Fuel Mix ----
with tab_fuelmix:
    st.header("State Electricity Generation Fuel Mix")
    st.caption("Net generation by fuel type as share of total. Source: EIA-923.")

    sel_states_fm = st.multiselect("States", ["NC", "SC", "VA", "GA", "TN", "TX", "CA", "WA", "IN", "OH"],
                                    default=["NC", "SC", "VA", "GA", "TN"])

    df_mix = _fuel_mix(sel_states_fm, start_year)
    if _check_data(df_mix, "fuel mix data"):
        fuel_cols = [c for c in df_mix.columns if c.startswith("fuel_share_")]
        fuel_labels = {
            "fuel_share_gas": "Natural Gas",
            "fuel_share_coal": "Coal",
            "fuel_share_nuclear": "Nuclear",
            "fuel_share_hydro": "Hydro",
            "fuel_share_wind": "Wind",
            "fuel_share_solar": "Solar",
            "fuel_share_petroleum": "Petroleum",
            "fuel_share_other_renewable": "Other Renewable",
        }

        if sel_states_fm and len(sel_states_fm) == 1:
            state = sel_states_fm[0]
            df_state = df_mix[df_mix["state"] == state].sort_values("year")
            melt = df_state.melt(id_vars=["year"], value_vars=fuel_cols, var_name="fuel", value_name="share")
            melt["fuel"] = melt["fuel"].map(fuel_labels)
            fig = px.area(melt, x="year", y="share", color="fuel",
                           title=f"{state} Generation Fuel Mix Over Time",
                           labels={"share": "Share", "year": "Year"},
                           template="plotly_white")
            st.plotly_chart(fig, use_container_width=True)
        else:
            latest_year = df_mix["year"].max() if not df_mix.empty else current_year
            df_latest = df_mix[df_mix["year"] == latest_year].copy()
            melt = df_latest.melt(id_vars=["state"], value_vars=fuel_cols, var_name="fuel", value_name="share")
            melt["fuel"] = melt["fuel"].map(fuel_labels)
            fig = px.bar(melt, x="state", y="share", color="fuel",
                          barmode="stack",
                          title=f"Fuel Mix by State — {latest_year}",
                          labels={"share": "Share", "state": "State"},
                          template="plotly_white")
            st.plotly_chart(fig, use_container_width=True)


# ---- Tab 4: Price vs Fuel Mix Scatter ----
with tab_scatter:
    st.header("Retail Price vs Fuel Mix — Exploratory Scatter")
    st.warning(
        "**CAUTION — observational only.** These scatter plots show correlations in the data. "
        "They do not establish causation. Electricity prices are driven by capital recovery, "
        "regulation, transmission costs, geography, and policy — not fuel mix alone."
    )

    scatter_fuel = st.selectbox("Fuel axis", list({
        "fuel_share_gas": "Natural Gas Share",
        "fuel_share_coal": "Coal Share",
        "fuel_share_nuclear": "Nuclear Share",
        "fuel_share_wind": "Wind Share",
        "fuel_share_solar": "Solar Share",
    }.keys()), format_func=lambda x: {
        "fuel_share_gas": "Natural Gas Share",
        "fuel_share_coal": "Coal Share",
        "fuel_share_nuclear": "Nuclear Share",
        "fuel_share_wind": "Wind Share",
        "fuel_share_solar": "Solar Share",
    }[x])

    df_scatter = _price_vs_mix(sector, current_year)
    if _check_data(df_scatter, "price vs fuel mix data") and scatter_fuel in df_scatter.columns:
        fig = px.scatter(
            df_scatter.dropna(subset=[scatter_fuel, "price_cents_per_kwh"]),
            x=scatter_fuel, y="price_cents_per_kwh",
            color="market_structure",
            text="state",
            trendline="ols",
            title=f"{scatter_fuel.replace('fuel_share_','').title()} Share vs Retail Price — {current_year} ({sector})",
            labels={scatter_fuel: "Fuel Share", "price_cents_per_kwh": "¢/kWh"},
            template="plotly_white",
            color_discrete_map={"regulated": "#3b82f6", "hybrid": "#f59e0b", "restructured": "#10b981"},
        )
        fig.update_traces(textposition="top center", selector=dict(mode="markers+text"))
        st.plotly_chart(fig, use_container_width=True)


# ---- Tab 5: Market Structure ----
with tab_market:
    st.header(f"Market Structure Price Comparison — {current_year} / {sector}")
    st.caption("regulated = vertically integrated IOU with rate-of-return regulation. "
               "hybrid = partial competition or co-op/muni dominance. "
               "restructured = retail competition enacted.")
    st.warning(
        "Market structure classification is a proxy, not a cause. States with low prices "
        "under regulation often have access to low-cost hydro (WA, OR, ID) or nuclear "
        "(SC, IL), not a structural effect per se."
    )

    df_mkt = _market_comparison(current_year, sector)
    if _check_data(df_mkt, "market structure data"):
        fig = px.bar(df_mkt, x="market_structure", y="median_price_cents",
                      error_y=df_mkt["max_price_cents"] - df_mkt["median_price_cents"],
                      color="market_structure",
                      color_discrete_map={"regulated": "#3b82f6", "hybrid": "#f59e0b", "restructured": "#10b981"},
                      title=f"Median Retail Price by Market Structure — {current_year} ({sector})",
                      labels={"median_price_cents": "Median ¢/kWh", "market_structure": ""},
                      template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(df_mkt.rename(columns={
            "market_structure": "Structure", "state_count": "# States",
            "median_price_cents": "Median ¢/kWh", "mean_price_cents": "Mean ¢/kWh",
            "min_price_cents": "Min ¢/kWh", "max_price_cents": "Max ¢/kWh",
        }), use_container_width=True)


# ---- Tab 6: Duke State Context ----
with tab_duke:
    st.header("Duke-Served State Price Context")
    st.caption("Duke Energy operates in NC, SC, IN, OH, KY, FL. "
               "This view provides EIA state-average context alongside Duke tariff territory.")
    st.info(
        "**Integration note:** These are EIA state averages — the blended average across all utilities "
        "in each state.  Duke tariff bills (from the billing engine) can be overlaid for direct "
        "comparison once the EIA data is loaded."
    )

    df_duke = load_duke_state_context(sector=sector, years=12)
    if _check_data(df_duke, "Duke state context"):
        fig = px.line(
            df_duke[df_duke["price_cents_per_kwh"].notna()],
            x="year", y="price_cents_per_kwh", color="state",
            title=f"EIA Average Retail Price — Duke-Served States ({sector})",
            labels={"price_cents_per_kwh": "¢/kWh", "year": "Year", "state": "State"},
            template="plotly_white",
        )
        # Add US reference
        df_us_d = _price_history(["US"], sector, "annual", df_duke["year"].min() if not df_duke.empty else 2010)
        if not df_us_d.empty:
            fig.add_scatter(x=df_us_d["year"], y=df_us_d["price_cents_per_kwh"],
                            mode="lines", name="US avg", line=dict(dash="dot", color="gray"))
        st.plotly_chart(fig, use_container_width=True)

        # Delta table
        st.subheader("Price vs US Average (most recent year)")
        latest_yr = df_duke["year"].max() if not df_duke.empty else current_year
        df_latest = df_duke[df_duke["year"] == latest_yr][
            ["state", "price_cents_per_kwh", "us_avg_cents_per_kwh", "delta_vs_us", "pct_vs_us", "market_structure"]
        ].sort_values("price_cents_per_kwh")
        st.dataframe(df_latest.rename(columns={
            "state": "State", "price_cents_per_kwh": "¢/kWh",
            "us_avg_cents_per_kwh": "US Avg ¢/kWh", "delta_vs_us": "Δ vs US",
            "pct_vs_us": "% vs US", "market_structure": "Market",
        }), use_container_width=True)
