"""Duke NC Residential Dashboard — consolidated single-page app.

Three sequential sections, residential-only (DEP RES + DEC RS):

  1. Where your dollar actually goes  — the hidden rider stack
  2. How we got here                  — annotated rate history
  3. What should you do?              — plan optimizer + TOU + solar

Run with::

    streamlit run app/streamlit_duke_residential.py
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from duke_rates.analytics.canonical_residential import (
    load_canonical_residential_timeline,
)
from duke_rates.analytics.canonical_rider_components import (
    load_dec_rs_canonical_rider_components,
    load_dep_res_canonical_rider_components,
)
from duke_rates.analytics.eia_analytics import load_price_history
from duke_rates.analytics.residential_bill_breakdown import (
    load_latest_residential_breakdown,
    load_residential_event_annotations,
    load_rider_glossary,
)
from duke_rates.analytics.proposed_residential import (
    load_proposed_residential_comparison,
    load_proposed_residential_rider_stack,
    load_proposed_rider_summary,
)
from duke_rates.analytics.proposed_revenue import (
    load_proposed_capex_anchors,
    load_proposed_revenue_adjustments,
)
from duke_rates.analytics.proposed_forecast import load_proposed_load_forecast
from duke_rates.analytics.forecast_trueup import (
    load_forecast_trueup_series,
    summarize_trueup,
)
from duke_rates.analytics.forecast_vs_history import (
    load_load_growth_cagr,
    load_load_growth_continuity,
)
from duke_rates.analytics.proposed_financial_context import load_proposed_class_revenue_impacts
from duke_rates.charts.residential_dashboard import (
    CATEGORY_COLORS,
    all_in_rate_history_stack,
    annotated_history_chart,
    rider_breakdown_donut,
)

DB_PATH = ROOT / "data" / "db" / "duke_rates.db"
BREAKDOWN_CACHE_VERSION = "canonical_breakdown_v2"

# Residential schedules only — DEP RES (and RES variants R-TOU, R-TOUD) + DEC RS
_RESIDENTIAL_GROUP = "residential"

_STATE_COMPANY_OPTIONS = {
    "DEP — Duke Energy Progress (NC)": ("NC", "progress"),
    "DEC — Duke Energy Carolinas (NC)": ("NC", "carolinas"),
}


# ---------------------------------------------------------------------------
# Cached loaders
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _timeline(db_path: str) -> pd.DataFrame:
    df = load_canonical_residential_timeline(database_path=Path(db_path))
    if df.empty:
        return df
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    if "rider_effective_date" in df.columns:
        df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce")
    return df


@st.cache_data(show_spinner=False)
def _events(db_path: str) -> pd.DataFrame:
    return load_residential_event_annotations(database_path=Path(db_path))


@st.cache_data(show_spinner=False)
def _glossary(db_path: str) -> pd.DataFrame:
    return load_rider_glossary(database_path=Path(db_path))


@st.cache_data(show_spinner=False)
def _components(db_path: str, utility: str) -> pd.DataFrame:
    if utility == "DEP":
        return load_dep_res_canonical_rider_components(database_path=Path(db_path))
    return load_dec_rs_canonical_rider_components(database_path=Path(db_path))


@st.cache_data(show_spinner=False)
def _proposed_comparison(
    db_path: str, monthly_kwh: float, rider_basis: str = "summary_total"
) -> pd.DataFrame:
    return load_proposed_residential_comparison(
        database_path=Path(db_path),
        representative_kwh=float(monthly_kwh),
        rider_basis=rider_basis,
    )


@st.cache_data(show_spinner=False)
def _proposed_riders(db_path: str, monthly_kwh: float) -> pd.DataFrame:
    return load_proposed_rider_summary(
        database_path=Path(db_path),
        representative_kwh=float(monthly_kwh),
    )


@st.cache_data(show_spinner=False)
def _proposed_rider_stack(db_path: str, monthly_kwh: float) -> pd.DataFrame:
    return load_proposed_residential_rider_stack(
        database_path=Path(db_path),
        representative_kwh=float(monthly_kwh),
    )


@st.cache_data(show_spinner=False)
def _proposed_revenue(db_path: str) -> pd.DataFrame:
    return load_proposed_revenue_adjustments(database_path=Path(db_path))


@st.cache_data(show_spinner=False)
def _proposed_class_impacts() -> pd.DataFrame:
    return load_proposed_class_revenue_impacts(root=ROOT)


@st.cache_data(show_spinner=False)
def _proposed_forecast(db_path: str) -> pd.DataFrame:
    return load_proposed_load_forecast(database_path=Path(db_path))


@st.cache_data(show_spinner=False)
def _proposed_capex_anchors(db_path: str) -> pd.DataFrame:
    return load_proposed_capex_anchors(database_path=Path(db_path))


@st.cache_data(show_spinner=False)
def _trueup_series(db_path: str) -> pd.DataFrame:
    return load_forecast_trueup_series(database_path=Path(db_path))


@st.cache_data(show_spinner=False)
def _trueup_summary(db_path: str) -> pd.DataFrame:
    return summarize_trueup(database_path=Path(db_path))


@st.cache_data(show_spinner=False)
def _growth_continuity(db_path: str) -> pd.DataFrame:
    return load_load_growth_continuity(database_path=Path(db_path))


@st.cache_data(show_spinner=False)
def _growth_cagr(db_path: str) -> pd.DataFrame:
    return load_load_growth_cagr(database_path=Path(db_path))


def _breakdown(db_path: str, utility: str, monthly_kwh: float, cache_version: str) -> pd.DataFrame:
    _ = cache_version
    return load_latest_residential_breakdown(
        utility=utility,
        monthly_kwh=monthly_kwh,
        database_path=Path(db_path),
    )


@st.cache_data(show_spinner=False, ttl=3600)
def _eia(start_year: int = 2016) -> pd.DataFrame:
    try:
        nc = load_price_history(states=["NC"], sector="RES", frequency="annual", start_year=start_year)
        us = load_price_history(states=["US"], sector="RES", frequency="annual", start_year=start_year)
        if nc.empty and us.empty:
            return pd.DataFrame()
        return pd.concat([nc, us], ignore_index=True)
    except Exception:
        return pd.DataFrame()


@st.cache_resource(show_spinner=False)
def _engine(db_path: str):
    from duke_rates.billing.tariff_engine import TariffBillingEngine
    from duke_rates.db.repository import Repository
    repo = Repository(db_path)
    return repo, TariffBillingEngine(repo)


@st.cache_data(show_spinner=False)
def _residential_families(db_path: str, state: str, company: str):
    from duke_rates.billing.tariff_engine import schedule_group_for
    from duke_rates.db.repository import Repository
    repo = Repository(db_path)
    all_fams = repo.list_tariff_families(state=state, company=company, family_type="rate_schedule")
    return [
        (f.family_key, f.title or f.family_key, f.schedule_code)
        for f in all_fams
        if schedule_group_for(f.schedule_code) == _RESIDENTIAL_GROUP
    ]


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Duke NC Residential",
    layout="wide",
    initial_sidebar_state="expanded",
)


# Custom visual design injection
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

    /* Global Page overrides */
    .stApp {
        background: radial-gradient(circle at 50% 0%, #0c111e 0%, #05070d 100%) !important;
        color: #e2e8f0 !important;
        font-family: 'Inter', sans-serif;
    }

    header[data-testid="stHeader"] {
        background: rgba(5, 7, 13, 0.96) !important;
        border-bottom: 1px solid rgba(255, 255, 255, 0.06) !important;
    }

    header[data-testid="stHeader"] * {
        color: #e2e8f0 !important;
    }

    div[data-testid="stToolbar"] {
        background: transparent !important;
    }

    h1, h2, h3, h4, h5, h6 {
        font-family: 'Plus Jakarta Sans', sans-serif !important;
        font-weight: 700 !important;
        color: #f8fafc !important;
    }

    /* Sidebar Styling */
    section[data-testid="stSidebar"] {
        background-color: rgba(6, 8, 14, 0.9) !important;
        border-right: 1px solid rgba(255, 255, 255, 0.05) !important;
        backdrop-filter: blur(20px) !important;
    }

    section[data-testid="stSidebar"] hr {
        border-color: rgba(255, 255, 255, 0.08) !important;
    }

    /* Selectbox, Number Input, Sliders styling */
    div[data-baseweb="select"] > div, 
    input, 
    div[role="slider"] {
        background-color: rgba(15, 23, 42, 0.6) !important;
        color: #cbd5e1 !important;
        border: 1px solid rgba(255, 255, 255, 0.1) !important;
        border-radius: 8px !important;
    }

    /* Glass Cards for Spotlights */
    .spotlight-card {
        background: linear-gradient(135deg, rgba(79, 172, 254, 0.08) 0%, rgba(243, 85, 218, 0.04) 100%) !important;
        border: 1px solid rgba(79, 172, 254, 0.2) !important;
        border-radius: 16px !important;
        padding: 24px !important;
        backdrop-filter: blur(16px) !important;
        -webkit-backdrop-filter: blur(16px) !important;
        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3) !important;
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1) !important;
        margin-top: 15px;
        margin-bottom: 25px;
    }

    .spotlight-card:hover {
        transform: translateY(-2px) !important;
        border-color: rgba(79, 172, 254, 0.4) !important;
        box-shadow: 0 12px 40px 0 rgba(79, 172, 254, 0.2) !important;
    }

    .spotlight-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
        margin-bottom: 16px;
    }

    .spotlight-title {
        font-size: 1.4rem;
        font-weight: 800;
        font-family: 'Plus Jakarta Sans', sans-serif;
        margin: 0;
        background: linear-gradient(135deg, #00f2fe 0%, #4facfe 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }

    .spotlight-desc {
        font-size: 1rem;
        line-height: 1.6;
        color: #cbd5e1 !important;
    }

    .spotlight-stat {
        display: flex;
        flex-direction: column;
        background: rgba(15, 23, 42, 0.6) !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        border-radius: 12px;
        padding: 12px 18px;
        min-width: 140px;
    }

    .spotlight-stat-label {
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: #94a3b8 !important;
        margin-bottom: 4px;
    }

    .spotlight-stat-val {
        font-size: 1.25rem;
        font-weight: 700;
        font-family: 'Plus Jakarta Sans', sans-serif;
    }

    /* Custom premium card style for KPIs */
    .metric-card {
        padding: 20px;
        border-radius: 16px;
        background: linear-gradient(135deg, rgba(22, 28, 45, 0.45) 0%, rgba(15, 23, 42, 0.35) 100%) !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        backdrop-filter: blur(12px) !important;
        -webkit-backdrop-filter: blur(12px) !important;
        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3) !important;
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        margin-bottom: 15px;
    }

    .metric-card:hover {
        transform: translateY(-4px);
        border-color: rgba(0, 242, 254, 0.4) !important;
        box-shadow: 0 16px 36px -5px rgba(0, 242, 254, 0.2) !important;
    }

    .metric-title {
        font-size: 0.8rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 8px;
        color: #94a3b8;
    }

    .metric-value {
        font-size: 2.2rem;
        font-weight: 800;
        line-height: 1.1;
        margin-bottom: 8px;
        font-family: 'Plus Jakarta Sans', sans-serif;
        color: #f8fafc;
    }

    .metric-delta {
        font-size: 0.85rem;
        font-weight: 600;
        display: inline-flex;
        align-items: center;
        border-radius: 6px;
        padding: 2px 8px;
    }

    .delta-positive {
        background-color: rgba(255, 90, 95, 0.12) !important;
        color: #ff5a5f !important;
        border: 1px solid rgba(255, 90, 95, 0.2) !important;
    }

    .delta-negative {
        background-color: rgba(0, 255, 208, 0.12) !important;
        color: #00ffd0 !important;
        border: 1px solid rgba(0, 255, 208, 0.2) !important;
    }

    .delta-neutral {
        background-color: rgba(148, 163, 184, 0.12) !important;
        color: #cbd5e1 !important;
        border: 1px solid rgba(148, 163, 184, 0.2) !important;
    }

    /* Tabs styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px !important;
        background-color: rgba(15, 23, 42, 0.4) !important;
        padding: 6px !important;
        border-radius: 12px !important;
        border: 1px solid rgba(255, 255, 255, 0.05) !important;
    }

    .stTabs [data-baseweb="tab"] {
        padding: 8px 16px !important;
        border-radius: 8px !important;
        font-family: 'Plus Jakarta Sans', sans-serif !important;
        font-weight: 600 !important;
        color: #94a3b8 !important;
        border: none !important;
        background-color: transparent !important;
        transition: all 0.2s ease !important;
    }

    .stTabs [data-baseweb="tab"]:hover {
        color: #f8fafc !important;
        background-color: rgba(255, 255, 255, 0.05) !important;
    }

    .stTabs [aria-selected="true"] {
        color: #00f2fe !important;
        background-color: rgba(0, 242, 254, 0.1) !important;
        border: 1px solid rgba(0, 242, 254, 0.2) !important;
    }

    /* Step Infographic */
    .infographic-container {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 25px;
        flex-wrap: wrap;
    }

    .infographic-step {
        flex: 1;
        min-width: 220px;
        background: rgba(22, 28, 45, 0.45);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 12px;
        padding: 18px;
        position: relative;
        backdrop-filter: blur(12px);
    }

    .infographic-step::before {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        height: 4px;
        border-radius: 12px 12px 0 0;
    }

    .step-base::before { background: #4facfe; }
    .step-riders::before { background: #ff5a5f; }
    .step-total::before { background: #00ffd0; }

    .step-num {
        font-family: 'Plus Jakarta Sans', sans-serif;
        font-size: 0.75rem;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        color: #64748b;
        margin-bottom: 6px;
    }

    .step-title {
        font-family: 'Plus Jakarta Sans', sans-serif;
        font-size: 1.1rem;
        font-weight: 700;
        color: #f8fafc;
        margin-bottom: 8px;
    }

    .step-desc {
        font-size: 0.85rem;
        line-height: 1.5;
        color: #94a3b8;
    }

    /* Category tag badges */
    .cat-badge {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }

    .cat-fuel { background-color: rgba(255, 90, 95, 0.12); color: #ff5a5f; border: 1px solid rgba(255, 90, 95, 0.2); }
    .cat-renewable { background-color: rgba(0, 255, 208, 0.12); color: #00ffd0; border: 1px solid rgba(0, 255, 208, 0.2); }
    .cat-efficiency { background-color: rgba(168, 255, 53, 0.12); color: #a8ff35; border: 1px solid rgba(168, 255, 53, 0.2); }
    .cat-tax { background-color: rgba(243, 85, 218, 0.12); color: #f355da; border: 1px solid rgba(243, 85, 218, 0.2); }
    .cat-base { background-color: rgba(79, 172, 254, 0.12); color: #4facfe; border: 1px solid rgba(79, 172, 254, 0.2); }
    .cat-other { background-color: rgba(148, 163, 184, 0.12); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.2); }
    </style>
    """,
    unsafe_allow_html=True
)


# Sidebar — shared inputs that drive all three sections
with st.sidebar:
    st.header("Inputs")
    st.caption(
        "Drives the bill amounts shown in every section below. Defaults to "
        "1,000 kWh — the rough average for a NC home."
    )
    monthly_kwh = st.number_input(
        "Monthly usage (kWh)",
        min_value=100,
        max_value=5000,
        value=1000,
        step=50,
    )
    service_date = st.date_input(
        "Service date (for the optimizer)",
        value=datetime.date.today().replace(day=1),
        help="Month/year of the bill — used to pick the right tariff version in Section 3.",
    )
    st.markdown("---")
    utility_label = st.selectbox(
        "Featured utility",
        list(_STATE_COMPANY_OPTIONS.keys()),
        index=0,
        help="Sections 1 and 4 focus on this utility. Section 2 can compare either utility.",
    )
    state, company = _STATE_COMPANY_OPTIONS[utility_label]
    primary_utility = "DEP" if company == "progress" else "DEC"

    st.markdown("---")
    graph_style = st.selectbox(
        "Graph line style",
        ["Fluid (Curved)", "Stepped (Technical)"],
        index=0,
        help="Fluid uses smooth curves for an elegant view. Stepped shows the actual flat-rate periods between rate filings."
    )
    interpolation = "spline" if graph_style == "Fluid (Curved)" else "hv"


# ---------------------------------------------------------------------------
# Hero metrics
# ---------------------------------------------------------------------------

st.title("Duke Energy NC — what you actually pay")
st.caption(
    "A residential-customer view of the DEP and DEC rate stack: not just the base rate, "
    "but every named rider that lands on your bill, where it came from, and what your "
    "options are."
)

# Step-by-step visual rate composition infographic
st.markdown(
    """
    <div class="infographic-container">
        <div class="infographic-step step-base">
            <div class="step-num">Step 1</div>
            <div class="step-title">Base Tariff Rate</div>
            <div class="step-desc">The baseline cost for generation, transmission, distribution, and core support operations. Approved during major regulatory rate cases.</div>
        </div>
        <div class="infographic-step step-riders">
            <div class="step-num">Step 2</div>
            <div class="step-title">Rider Adjustments</div>
            <div class="step-desc">Dynamic monthly additions or credits for fuel cost volatility, renewable energy integration, energy efficiency fees, and corporate tax refunds.</div>
        </div>
        <div class="infographic-step step-total">
            <div class="step-num">Step 3</div>
            <div class="step-title">All-In Energy Rate</div>
            <div class="step-desc">The sum of Base Rate + Riders. This represents the total price per kilowatt-hour (¢/kWh) used to compute your monthly electric charge.</div>
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

timeline_df = _timeline(str(DB_PATH))
if timeline_df.empty:
    st.error(
        "No canonical residential timeline data found. "
        "Run `duke-rates recover-history-progress-nc` to populate it."
    )
    st.stop()

events_df = _events(str(DB_PATH))
glossary_df = _glossary(str(DB_PATH))

# Latest per utility
latest_per_utility = (
    timeline_df.sort_values("effective_date")
    .groupby("utility", as_index=False)
    .tail(1)
    .set_index("utility")
)
first_per_utility = (
    timeline_df.sort_values("effective_date")
    .groupby("utility", as_index=False)
    .head(1)
    .set_index("utility")
)

def _safe(value, fmt):
    try:
        return fmt.format(value)
    except Exception:
        return "—"

kpi_cols = st.columns(4)

dep_latest = latest_per_utility.loc["DEP"] if "DEP" in latest_per_utility.index else None
dec_latest = latest_per_utility.loc["DEC"] if "DEC" in latest_per_utility.index else None
dep_first = first_per_utility.loc["DEP"] if "DEP" in first_per_utility.index else None
dec_first = first_per_utility.loc["DEC"] if "DEC" in first_per_utility.index else None

if dep_latest is not None and dep_first is not None:
    delta_dep = float(dep_latest["all_in_cents_per_kwh"]) - float(dep_first["all_in_cents_per_kwh"])
    kpi_cols[0].markdown(
        f"""
        <div class="metric-card">
            <div class="metric-title">DEP all-in ¢/kWh</div>
            <div class="metric-value">{float(dep_latest['all_in_cents_per_kwh']):.2f}</div>
            <div class="metric-delta {'delta-positive' if delta_dep >= 0 else 'delta-negative'}">
                {delta_dep:+.2f} since {pd.to_datetime(dep_first['effective_date']).year}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )
else:
    kpi_cols[0].markdown('<div class="metric-card"><div class="metric-title">DEP all-in ¢/kWh</div><div class="metric-value">—</div></div>', unsafe_allow_html=True)

if dec_latest is not None and dec_first is not None:
    delta_dec = float(dec_latest["all_in_cents_per_kwh"]) - float(dec_first["all_in_cents_per_kwh"])
    kpi_cols[1].markdown(
        f"""
        <div class="metric-card">
            <div class="metric-title">DEC all-in ¢/kWh</div>
            <div class="metric-value">{float(dec_latest['all_in_cents_per_kwh']):.2f}</div>
            <div class="metric-delta {'delta-positive' if delta_dec >= 0 else 'delta-negative'}">
                {delta_dec:+.2f} since {pd.to_datetime(dec_first['effective_date']).year}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )
else:
    kpi_cols[1].markdown('<div class="metric-card"><div class="metric-title">DEC all-in ¢/kWh</div><div class="metric-value">—</div></div>', unsafe_allow_html=True)

# Rider share for featured utility
if primary_utility in latest_per_utility.index:
    row = latest_per_utility.loc[primary_utility]
    base = float(row["base_cents_per_kwh"] or 0.0)
    all_in = float(row["all_in_cents_per_kwh"] or 0.0)
    rider_share = (all_in - base) / all_in * 100.0 if all_in else 0.0
    kpi_cols[2].markdown(
        f"""
        <div class="metric-card">
            <div class="metric-title">{primary_utility} rider share</div>
            <div class="metric-value">{rider_share:.1f}%</div>
            <div class="metric-delta delta-neutral">
                Riders vs Base Rate
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )
else:
    kpi_cols[2].markdown('<div class="metric-card"><div class="metric-title">Rider share</div><div class="metric-value">—</div></div>', unsafe_allow_html=True)

# Last data refresh: max effective_date in timeline
last_eff = pd.to_datetime(timeline_df["effective_date"]).max()
date_str = last_eff.strftime("%b %Y") if pd.notna(last_eff) else "—"
kpi_cols[3].markdown(
    f"""
    <div class="metric-card">
        <div class="metric-title">Latest rate-filing</div>
        <div class="metric-value" style="font-size: 1.8rem; margin-top: 6px; margin-bottom: 6px;">{date_str}</div>
        <div class="metric-delta delta-neutral">
            Database updated
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

# Tabs setup to tell a sequential data story
tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Today's Bill Composition", 
    "📜 Historical Cost Story", 
    "🧭 Proposed Rate Case",
    "💡 Optimize Your Plan"
])

# ---------------------------------------------------------------------------
# Section 1 — Where your dollar actually goes
# ---------------------------------------------------------------------------
with tab1:
    st.header("1 · Where your dollar actually goes")
    st.caption(
        f"Breakdown of the most recent {primary_utility} residential bill at "
        f"{monthly_kwh:,.0f} kWh/month. Base rate is the part you'd see in a rate-case "
        "headline; the surrounding wedges are the named riders that show up on the "
        "second page of your bill — fuel adjustments, solar program costs, energy-efficiency "
        "fees, EDIT tax credits, and more."
    )

    breakdown_df = _breakdown(
        str(DB_PATH),
        primary_utility,
        float(monthly_kwh),
        BREAKDOWN_CACHE_VERSION,
    )
    if breakdown_df.empty:
        st.warning(f"No breakdown available for {primary_utility} at this time.")
    else:
        left, right = st.columns([3, 2])
        with left:
            st.plotly_chart(
                rider_breakdown_donut(
                    breakdown_df,
                    utility=primary_utility,
                    monthly_kwh=float(monthly_kwh),
                ),
                use_container_width=True,
            )
        with right:
            rider_only = breakdown_df[breakdown_df["component_kind"] == "rider"].copy()
            n_riders = len(rider_only)
            rider_dollars = rider_only["dollars"].sum()
            credit_rows = rider_only[rider_only["dollars"] < 0]
            credit_dollars = credit_rows["dollars"].sum()
            st.markdown("### What this view shows")
            st.markdown(
                f"- **{n_riders} named riders** were active in the most recent filing.\n"
                f"- They add **${rider_dollars:,.2f}/mo** to your bill at this usage.\n"
                + (
                    f"- That includes **${abs(credit_dollars):,.2f}/mo in credits** "
                    f"(EDIT refund of over-collected federal tax)."
                    if not credit_rows.empty
                    else ""
                )
            )
            st.markdown(
                "Energy-only total shown — fixed monthly customer charges and taxes "
                "aren't included here. See Section 3 for a fully-itemized bill."
            )

        table = breakdown_df[
            ["component", "short_name", "category", "cents_per_kwh", "dollars"]
        ].copy()
        table.columns = ["Code", "Name", "Category", "¢/kWh", "$ / month"]
        st.dataframe(
            table,
            use_container_width=True,
            hide_index=True,
            column_config={
                "¢/kWh": st.column_config.NumberColumn(format="%.4f"),
                "$ / month": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

        # --- Interactive Rider Spotlight Explorer ---
        rider_rows = breakdown_df[breakdown_df["component_kind"] == "rider"].copy()
        if not rider_rows.empty:
            st.markdown("---")
            st.subheader("🔍 Interactive Rider Explorer")
            st.caption(
                "Select any active rider on your current bill below to highlight its "
                "purpose, category, monthly impact, and see its historical rate trajectory."
            )
            rider_rows = rider_rows.reindex(
                rider_rows["dollars"].abs().sort_values(ascending=False).index
            )
            
            spotlight_options = []
            code_to_row = {}
            for _, r_row in rider_rows.iterrows():
                lbl = f"{r_row['component']} — {r_row['short_name']}" if r_row['short_name'] != r_row['component'] else r_row['component']
                spotlight_options.append(lbl)
                code_to_row[lbl] = r_row
                
            selected_lbl = st.selectbox("Choose a rider to spotlight:", spotlight_options)
            
            if selected_lbl:
                sel_row = code_to_row[selected_lbl]
                code = sel_row["component"]
                dollars = float(sel_row["dollars"])
                cents = float(sel_row["cents_per_kwh"])
                category = sel_row["category"] or "rider"
                description = (sel_row["description"] or "").strip()
                if not description:
                    description = "No plain-English description on file for this rider yet."
                    
                badge_class = f"cat-{category.lower()}" if f"cat-{category.lower()}" in ["cat-fuel", "cat-renewable", "cat-efficiency", "cat-tax", "cat-base"] else "cat-other"
                
                col_desc, col_spark = st.columns([5, 4])
                
                with col_desc:
                    st.markdown(
                        f"""
                        <div class="spotlight-card">
                            <div class="spotlight-header">
                                <span class="spotlight-title">{code}</span>
                                <span class="cat-badge {badge_class}">{category}</span>
                            </div>
                            <p class="spotlight-desc" style="font-size: 1.15rem; font-weight: 600;">{sel_row['short_name']}</p>
                            <p class="spotlight-desc">{description}</p>
                            <div style="display: flex; gap: 16px; margin-top: 15px; flex-wrap: wrap;">
                                <div class="spotlight-stat">
                                    <span class="spotlight-stat-label">Monthly Impact</span>
                                    <span class="spotlight-stat-val" style="color: {'#10b981' if dollars < 0 else '#ef4444'}">${dollars:+,.2f}/mo</span>
                                </div>
                                <div class="spotlight-stat">
                                    <span class="spotlight-stat-label">Unit Rate</span>
                                    <span class="spotlight-stat-val">{cents:+.4f} ¢/kWh</span>
                                </div>
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
                    
                with col_spark:
                    st.markdown("<p style='font-size: 0.9rem; font-weight: 600; margin-top: 15px; margin-bottom: 5px; font-family: Plus Jakarta Sans;'>Historical Trajectory (¢/kWh)</p>", unsafe_allow_html=True)
                    components_full = _components(str(DB_PATH), primary_utility)
                    if not components_full.empty:
                        rider_hist = components_full[components_full["rider_code"] == code].sort_values("effective_date").copy()
                        if rider_hist.empty:
                            st.info("No historical component details for this rider.")
                        else:
                            fig_spark = go.Figure()
                            fig_spark.add_trace(
                                go.Scatter(
                                    x=rider_hist["effective_date"],
                                    y=rider_hist["cents_per_kwh"],
                                    mode="lines+markers",
                                    line=dict(color="#00ffd0" if dollars >= 0 else "#f355da", width=2.5, shape=interpolation),
                                    marker=dict(size=4),
                                    hovertemplate="<b>%{x|%b %Y}</b><br>Rate: %{y:.4f} ¢/kWh<extra></extra>"
                                )
                            )
                            fig_spark.update_layout(
                                height=180,
                                margin=dict(t=10, b=10, l=10, r=10),
                                template="plotly_dark",
                                xaxis=dict(showgrid=False, zeroline=False),
                                yaxis=dict(showgrid=True, gridcolor="rgba(255, 255, 255, 0.05)"),
                                paper_bgcolor="rgba(0,0,0,0)",
                                plot_bgcolor="rgba(0,0,0,0)",
                                font=dict(color="#cbd5e1", family="Inter, sans-serif"),
                            )
                            st.plotly_chart(fig_spark, use_container_width=True)

            # --- Educational Note on Variable/Storm Riders ---
            st.markdown(
                """
                <div class="spotlight-card" style="background: linear-gradient(135deg, rgba(251, 146, 60, 0.08) 0%, rgba(243, 85, 218, 0.03) 100%) !important; border-color: rgba(251, 146, 60, 0.25) !important;">
                    <div class="spotlight-header">
                        <span class="spotlight-title" style="background: linear-gradient(135deg, #ffaf40 0%, #fb923c 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">💡 Understanding Variable & Storm Riders</span>
                    </div>
                    <p class="spotlight-desc">
                        <b>Why are these riders added on top of your base rate?</b><br>
                        The core <b>Base Rate</b> is set during major regulatory rate cases (which occur only once every few years) to cover expected baseline operations like running power plants and maintaining transmission lines. 
                    </p>
                    <p class="spotlight-desc">
                        However, the utility faces unpredictable, volatile expenses that cannot be forecast in advance. Instead of initiating complex rate cases for every unexpected cost, the North Carolina Utilities Commission (NCUC) allows <b>variable riders</b> to adjust customer bills dynamically:
                    </p>
                    <ul class="spotlight-desc" style="margin-left: 20px; padding-left: 10px; margin-top: -10px;">
                        <li><b>Storm Recovery (Riders STS & STS-2):</b> Major weather events (like Hurricanes Florence, Dorian, and Isaias) cause hundreds of millions of dollars in unexpected grid damage. Rather than funding these with high-interest utility debt, the NCUC authorizes "Securitization"—issuing low-interest, AAA-rated bonds. These storm riders service that bond debt at a much lower cost to customers, spread out over 10-15 years.</li>
                        <li><b>Fuel Cost Volatility (Rider BA-Fuel):</b> Market fuel prices fluctuate constantly. The fuel rider acts as a dynamic pass-through mechanism: it increases when fuel costs rise and credits customers back when fuel prices decrease, without any utility profit markup.</li>
                    </ul>
                </div>
                """,
                unsafe_allow_html=True
            )

            with st.expander("Show every rider Duke tracks (not just yours)", expanded=False):
                if glossary_df.empty:
                    st.info("The `rider_descriptions` table is empty.")
                else:
                    full = glossary_df[
                        ["rider_code", "short_name", "category", "description"]
                    ].copy()
                    full.columns = ["Code", "Name", "Category", "What it does"]
                    st.dataframe(full, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Section 2 — How we got here
# ---------------------------------------------------------------------------
with tab2:
    st.header("2 · How we got here")
    st.caption(
        "Residential rates over time, annotated with the laws and market events that drove "
        "major changes. The timeline starts when the canonical dataset has both base rates "
        "and enough rider detail to calculate all-in rates with confidence."
    )

    control_cols = st.columns([1.2, 1.1, 1.1])
    utility_options = ["DEP", "DEC", "Compare both"]
    selected_utility_view = control_cols[0].radio(
        "Utility view",
        utility_options,
        index=utility_options.index(primary_utility),
        horizontal=True,
    )
    rate_view_label = control_cols[1].radio(
        "Rate lines",
        ["All-in + base", "All-in only", "Base only"],
        horizontal=True,
    )
    eia_benchmark = control_cols[2].radio(
        "EIA benchmark",
        ["Off", "NC", "US", "NC + US"],
        horizontal=True,
    )

    chart_utilities = ["DEP", "DEC"] if selected_utility_view == "Compare both" else [selected_utility_view]
    rate_view = {
        "All-in + base": "all_in_base",
        "All-in only": "all_in",
        "Base only": "base",
    }[rate_view_label]
    eia_df = _eia(start_year=2016) if eia_benchmark != "Off" else pd.DataFrame()
    eia_states = {
        "NC": ["NC"],
        "US": ["US"],
        "NC + US": ["NC", "US"],
    }.get(eia_benchmark, [])

    # Dynamic EIA comparisons
    if eia_benchmark != "Off" and not eia_df.empty and primary_utility in latest_per_utility.index:
        latest_year = int(eia_df["year"].max())
        nc_latest = eia_df[(eia_df["state"] == "NC") & (eia_df["year"] == latest_year)]
        us_latest = eia_df[(eia_df["state"] == "US") & (eia_df["year"] == latest_year)]
        
        if not nc_latest.empty and not us_latest.empty:
            nc_val = float(nc_latest["price_cents_per_kwh"].iloc[0])
            us_val = float(us_latest["price_cents_per_kwh"].iloc[0])
            duke_val = float(latest_per_utility.loc[primary_utility]["all_in_cents_per_kwh"])
            
            diff_nc = ((duke_val - nc_val) / nc_val) * 100.0
            diff_us = ((duke_val - us_val) / us_val) * 100.0
            
            st.markdown(f"#### Comparative Cost Snapshots (vs. {latest_year} EIA averages)")
            
            comp_cols = st.columns(2)
            comp_cols[0].markdown(
                f"""
                <div class="metric-card">
                    <div class="metric-title">Duke {primary_utility} vs. NC Average</div>
                    <div class="metric-value">{abs(diff_nc):.1f}% {'Higher' if diff_nc >= 0 else 'Lower'}</div>
                    <div class="metric-delta {'delta-positive' if diff_nc >= 0 else 'delta-negative'}">
                        NC EIA Avg: {nc_val:.2f} ¢/kWh ({latest_year})
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
            comp_cols[1].markdown(
                f"""
                <div class="metric-card">
                    <div class="metric-title">Duke {primary_utility} vs. US Average</div>
                    <div class="metric-value">{abs(diff_us):.1f}% {'Higher' if diff_us >= 0 else 'Lower'}</div>
                    <div class="metric-delta {'delta-positive' if diff_us >= 0 else 'delta-negative'}">
                        US EIA Avg: {us_val:.2f} ¢/kWh ({latest_year})
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

    st.plotly_chart(
        annotated_history_chart(
            timeline_df,
            events_df=events_df,
            utilities=chart_utilities,
            monthly_kwh=float(monthly_kwh),
            show_eia=False,
            eia_df=pd.DataFrame(),
            interpolation=interpolation,
            rate_view=rate_view,
        ),
        use_container_width=True,
    )
    coverage = (
        timeline_df[timeline_df["utility"].isin(chart_utilities)]
        .groupby("utility")["effective_date"]
        .agg(["min", "max", "count"])
        .reset_index()
    )
    coverage_bits = [
        f"{row.utility}: {pd.to_datetime(row['min']).date()} to {pd.to_datetime(row['max']).date()} ({int(row['count'])} points)"
        for _, row in coverage.iterrows()
    ]
    if coverage_bits:
        st.caption(
            "Canonical all-in coverage shown here: "
            + "; ".join(coverage_bits)
            + ". Earlier filings may exist in the corpus but are not included until their base and rider data are validated."
        )

    if "DEC" in chart_utilities:
        dec_history = timeline_df[timeline_df["utility"] == "DEC"].sort_values("effective_date").copy()
        if not dec_history.empty:
            credit_rows = dec_history[dec_history["rider_cents_per_kwh"] < 0]
            if not credit_rows.empty:
                first_credit = pd.to_datetime(credit_rows["effective_date"].min()).date()
                last_credit = pd.to_datetime(credit_rows["effective_date"].max()).date()
                st.caption(
                    f"DEC all-in falls below base from {first_credit} through {last_credit} "
                    "because explicit rider-summary totals are net credits in those periods."
                )
            if "rider_component_reconciliation_status" in dec_history.columns:
                recon = (
                    dec_history.groupby("rider_component_reconciliation_status", dropna=False)
                    .size()
                    .reset_index(name="Periods")
                    .rename(columns={"rider_component_reconciliation_status": "Component reconciliation"})
                )
                with st.expander("DEC rider-total audit", expanded=False):
                    st.caption(
                        "The DEC all-in line uses explicit total rider rows when available. "
                        "`reconciled` means parsed component rows sum back to that explicit total; "
                        "`component_gap` means the total is still used, but one or more component rows "
                        "are not fully attributed."
                    )
                    st.dataframe(recon, use_container_width=True, hide_index=True)
                    audit_cols = [
                        "effective_date",
                        "rider_cents_per_kwh",
                        "rider_component_sum_cents_per_kwh",
                        "rider_component_reconciliation_delta",
                        "rider_component_reconciliation_status",
                    ]
                    audit_view = dec_history[[c for c in audit_cols if c in dec_history.columns]].copy()
                    audit_view["effective_date"] = pd.to_datetime(audit_view["effective_date"]).dt.date
                    audit_view = audit_view.rename(
                        columns={
                            "effective_date": "Date",
                            "rider_cents_per_kwh": "Explicit rider total c/kWh",
                            "rider_component_sum_cents_per_kwh": "Parsed component sum c/kWh",
                            "rider_component_reconciliation_delta": "Total minus components",
                            "rider_component_reconciliation_status": "Status",
                        }
                    )
                    st.dataframe(
                        audit_view,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Explicit rider total c/kWh": st.column_config.NumberColumn(format="%.4f"),
                            "Parsed component sum c/kWh": st.column_config.NumberColumn(format="%.4f"),
                            "Total minus components": st.column_config.NumberColumn(format="%.4f"),
                        },
                    )

    if eia_benchmark != "Off" and not eia_df.empty:
        st.markdown("#### Benchmark against EIA averages")
        fig_eia = go.Figure()
        utility_colors = {"DEP": "#00f2fe", "DEC": "#f355da"}
        for utility in chart_utilities:
            sub = timeline_df[timeline_df["utility"] == utility].sort_values("effective_date")
            if sub.empty:
                continue
            fig_eia.add_trace(
                go.Scatter(
                    x=sub["effective_date"],
                    y=sub["all_in_cents_per_kwh"],
                    mode="lines+markers",
                    name=f"{utility} all-in",
                    line=dict(color=utility_colors.get(utility, "#cbd5e1"), width=3, shape=interpolation),
                    marker=dict(size=5),
                    hovertemplate="<b>%{x|%b %Y}</b><br>%{y:.3f} ¢/kWh<extra></extra>",
                )
            )
        for state_code, dash, color, label in [
            ("NC", "dash", "#00ffd0", "NC State Avg (EIA)"),
            ("US", "dot", "#ffd000", "US Nat'l Avg (EIA)"),
        ]:
            if state_code not in eia_states:
                continue
            state_eia = eia_df[eia_df["state"] == state_code].sort_values("year")
            if state_eia.empty:
                continue
            xs, ys = [], []
            for _, row in state_eia.iterrows():
                year = int(row["year"])
                xs.extend([pd.Timestamp(year=year, month=1, day=1), pd.Timestamp(year=year, month=12, day=31)])
                ys.extend([float(row["price_cents_per_kwh"])] * 2)
            fig_eia.add_trace(
                go.Scatter(
                    x=xs,
                    y=ys,
                    mode="lines",
                    name=label,
                    line=dict(color=color, width=2, dash=dash),
                    opacity=0.7,
                    hovertemplate=f"<b>{label}</b>: %{{y:.2f}} ¢/kWh<extra></extra>",
                )
            )
        fig_eia.update_layout(
            template="plotly_dark",
            height=360,
            title="Residential all-in rate vs EIA average price",
            xaxis_title="Year",
            yaxis_title="¢/kWh",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, bgcolor="rgba(0,0,0,0)"),
            margin=dict(t=70, b=45),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
            xaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
            font=dict(color="#cbd5e1", family="Inter, sans-serif"),
        )
        st.plotly_chart(fig_eia, use_container_width=True)

    if not events_df.empty:
        with st.expander("Event details (regulatory & market timeline annotations)", expanded=False):
            ev_view = events_df[
                ["effective_date", "bill_number", "short_title", "impact_category", "summary", "source_url"]
            ].copy()
            ev_view["effective_date"] = ev_view["effective_date"].dt.date
            ev_view.columns = ["Date", "Event", "Title", "Category", "What happened", "Source"]
            st.dataframe(
                ev_view,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Source": st.column_config.LinkColumn("Source", display_text="link"),
                },
            )
            st.caption(
                "Events are stored in the `legislative_actions` table. "
                "Add more rows there and they'll appear automatically."
            )

    st.markdown("#### All-In Rate Composition History")
    st.caption(
        f"How the all-in residential rate of {primary_utility} has evolved over time, showing the "
        "Base Rate at the bottom and each individual active rider stacked on top. Hover over the stack "
        "at any point to see the precise composition."
    )
    components_df = _components(str(DB_PATH), primary_utility)
    if components_df.empty:
        st.info(f"No itemized rider component history available for {primary_utility}.")
    else:
        st.plotly_chart(
            all_in_rate_history_stack(
                components_df,
                timeline_df,
                utility=primary_utility,
                database_path=Path(DB_PATH),
                interpolation=interpolation,
            ),
            use_container_width=True,
        )

    st.markdown("#### Your bill at historical rates")
    st.caption(
        f"What {monthly_kwh:,.0f} kWh/month would have cost you at each historical "
        f"rate-filing point for {primary_utility}. Energy-only — fixed customer charge not included."
    )
    util_history = timeline_df[timeline_df["utility"] == primary_utility].sort_values("effective_date").copy()
    util_history["energy_cost"] = util_history["all_in_cents_per_kwh"] * float(monthly_kwh) / 100.0
    util_history["base_cost"] = util_history["base_cents_per_kwh"] * float(monthly_kwh) / 100.0
    util_history["rider_cost"] = util_history["energy_cost"] - util_history["base_cost"]
    fig_bill_hist = go.Figure()
    fig_bill_hist.add_trace(
        go.Bar(
            x=util_history["effective_date"],
            y=util_history["base_cost"],
            name="Base",
            marker_color=CATEGORY_COLORS["base"],
            hovertemplate="<b>%{x|%b %Y}</b><br>Base: $%{y:.2f}<extra></extra>",
        )
    )
    fig_bill_hist.add_trace(
        go.Bar(
            x=util_history["effective_date"],
            y=util_history["rider_cost"],
            name="Riders",
            marker_color=CATEGORY_COLORS["fuel"],
            hovertemplate="<b>%{x|%b %Y}</b><br>Riders: $%{y:.2f}<extra></extra>",
        )
    )
    fig_bill_hist.update_layout(
        barmode="stack",
        title=f"{primary_utility} estimated monthly energy charge at {monthly_kwh:,.0f} kWh",
        xaxis_title="Effective date",
        yaxis_title="$ / month",
        template="plotly_dark",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, bgcolor="rgba(0,0,0,0)"),
        height=360,
        margin=dict(t=70, b=40),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
        xaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
        font=dict(color="#cbd5e1", family="Inter, sans-serif"),
    )
    st.plotly_chart(fig_bill_hist, use_container_width=True)


# ---------------------------------------------------------------------------
# Section 3 — Proposed rate case
# ---------------------------------------------------------------------------
with tab3:
    st.header("3 · Proposed rate case: accepted vs proposed")
    st.caption(
        "These rows come from proposed NCUC application exhibits and are not approved tariffs. "
        "By default the proposed all-in layers each filing's own **Summary of Rider Adjustments** "
        "total — the full proposed rider stack (fuel, EE/DSM, EDIT, decoupling, and the new riders) — "
        "so it compares apples-to-apples with the accepted all-in. Switch the basis below to see only "
        "the conservative validated subset of new riders."
    )

    rider_basis_label = st.radio(
        "Proposed rider basis",
        options=["Full rider stack (filing total)", "Validated new riders only"],
        horizontal=True,
        help=(
            "Full rider stack uses the utility's printed TOTAL cents/kWh from the Summary of Rider "
            "Adjustments sheet. Validated subset layers only PC/PTC/RAL/BPM-P, whose sign and "
            "residential applicability are individually checked — it understates the true all-in."
        ),
    )
    rider_basis = (
        "summary_total"
        if rider_basis_label.startswith("Full")
        else "validated_subset"
    )

    proposed_df = _proposed_comparison(str(DB_PATH), float(monthly_kwh), rider_basis)
    rider_summary_df = _proposed_riders(str(DB_PATH), float(monthly_kwh))
    revenue_df = _proposed_revenue(str(DB_PATH))
    class_impact_df = _proposed_class_impacts()

    if proposed_df.empty:
        st.warning("No proposed residential comparison data is available yet.")
    else:
        accepted_df = proposed_df[proposed_df["source_status"] == "accepted"].copy()
        future_df = proposed_df[proposed_df["source_status"] == "proposed"].copy()
        proposed_df["display_cents_per_kwh"] = proposed_df["all_in_cents_per_kwh"].fillna(
            proposed_df["base_cents_per_kwh"]
        )
        proposed_df["display_bill_amount"] = proposed_df["all_in_bill_amount"].fillna(
            proposed_df["base_bill_amount"]
        )
        accepted_df = proposed_df[proposed_df["source_status"] == "accepted"].copy()
        future_df = proposed_df[proposed_df["source_status"] == "proposed"].copy()

        st.markdown("#### Residential all-in trajectory with proposed demarcation")
        fig_prop = go.Figure()
        for utility in ["DEP", "DEC"]:
            accepted_rows = accepted_df[accepted_df["utility"] == utility].sort_values("effective_date")
            proposed_rows = future_df[future_df["utility"] == utility].sort_values(
                ["effective_date", "scenario_order"]
            )
            color = "#4facfe" if utility == "DEP" else "#00ffd0"
            if not accepted_rows.empty:
                fig_prop.add_trace(
                    go.Scatter(
                        x=accepted_rows["effective_date"],
                        y=accepted_rows["display_cents_per_kwh"],
                        mode="markers",
                        name=f"{utility} accepted latest",
                        marker=dict(size=11, color=color, symbol="circle"),
                        hovertemplate=(
                            "<b>%{fullData.name}</b><br>"
                            "%{x|%Y-%m-%d}<br>"
                            "Accepted all-in: %{y:.3f} c/kWh<extra></extra>"
                        ),
                    )
                )
            if not proposed_rows.empty:
                fig_prop.add_trace(
                    go.Scatter(
                        x=proposed_rows["effective_date"],
                        y=proposed_rows["display_cents_per_kwh"],
                        mode="lines+markers+text",
                        name=f"{utility} proposed",
                        text=proposed_rows["scenario_label"],
                        textposition="top center",
                        line=dict(color=color, width=2.5, dash="dash"),
                        marker=dict(size=9, color=color, symbol="circle-open"),
                        hovertemplate=(
                            "<b>%{text}</b><br>"
                            "%{x|%Y-%m-%d}<br>"
                            "Proposed shown: %{y:.3f} c/kWh<extra></extra>"
                        ),
                    )
                )

        if not accepted_df.empty:
            cutoff = pd.to_datetime(accepted_df["effective_date"]).max().to_pydatetime()
            fig_prop.add_shape(
                type="line",
                x0=cutoff,
                x1=cutoff,
                y0=0,
                y1=1,
                xref="x",
                yref="paper",
                line=dict(color="rgba(255,255,255,0.45)", width=1.5, dash="dot"),
            )
            fig_prop.add_annotation(
                x=cutoff,
                y=1,
                xref="x",
                yref="paper",
                text="latest accepted",
                showarrow=False,
                xanchor="left",
                yanchor="bottom",
                font=dict(color="#cbd5e1", size=12),
            )
        fig_prop.update_layout(
            template="plotly_dark",
            height=430,
            hovermode="x unified",
            yaxis_title="All-in c/kWh (base + proposed rider stack)",
            xaxis_title="Effective date",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, bgcolor="rgba(0,0,0,0)"),
            margin=dict(t=80, b=45),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
            xaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
            font=dict(color="#cbd5e1", family="Inter, sans-serif"),
        )
        st.plotly_chart(fig_prop, use_container_width=True)

        st.markdown("#### Before and after at your selected usage")
        compare_cols = st.columns(2)
        for col, utility in zip(compare_cols, ["DEP", "DEC"]):
            with col:
                utility_rows = proposed_df[proposed_df["utility"] == utility].copy()
                accepted_row = utility_rows[utility_rows["source_status"] == "accepted"]
                proposed_rows = utility_rows[utility_rows["source_status"] == "proposed"]
                if accepted_row.empty or proposed_rows.empty:
                    st.info(f"No accepted/proposed comparison available for {utility}.")
                    continue
                accepted_latest = accepted_row.iloc[0]
                proposed_latest = proposed_rows.sort_values(["effective_date", "scenario_order"]).iloc[-1]
                coverage = str(proposed_latest.get("proposed_rider_coverage") or "")
                if coverage == "catalog_only_no_values" and len(proposed_rows) > 1:
                    layered = proposed_rows[
                        proposed_rows["proposed_rider_coverage"] == "partial_validated"
                    ].sort_values(["effective_date", "scenario_order"])
                    if not layered.empty:
                        proposed_latest = layered.iloc[-1]
                        coverage = str(proposed_latest.get("proposed_rider_coverage") or "")
                delta_cents = float(proposed_latest["display_cents_per_kwh"]) - float(
                    accepted_latest["display_cents_per_kwh"]
                )
                delta_bill = float(proposed_latest["display_bill_amount"]) - float(
                    accepted_latest["display_bill_amount"]
                )
                rider_count = int(proposed_latest.get("proposed_rider_count") or 0)
                coverage_note = {
                    "summary_total": "all-in uses the filing's full rider-stack total",
                    "summary_total_carried_forward": (
                        "all-in carries the latest filing rider-stack total forward "
                        "(this rate year's summary sheet omits one)"
                    ),
                    "partial_validated": "latest scenario with validated rider values",
                    "projected_riders_carried_forward": (
                        "latest scenario uses Rate Year 1 rider values carried forward"
                    ),
                }.get(coverage, "latest scenario is base-only; rider values were not present")
                rider_descriptor = (
                    "full proposed rider stack"
                    if coverage.startswith("summary_total")
                    else f"{rider_count} validated proposed riders"
                )
                base_delta = float(proposed_latest["base_cents_per_kwh"]) - float(
                    accepted_latest["base_cents_per_kwh"]
                )
                tooltip = (
                    f"Accepted all-in {float(accepted_latest['display_cents_per_kwh']):.2f} "
                    f"to proposed {float(proposed_latest['display_cents_per_kwh']):.2f} c/kWh. "
                    f"Base rate change {base_delta:+.2f} c/kWh; rider layer "
                    f"{float(proposed_latest.get('rider_cents_per_kwh') or 0):.2f} c/kWh."
                )
                st.markdown(
                    f"""
                    <div class="metric-card" title="{tooltip}">
                        <div class="metric-title">{utility} proposed change</div>
                        <div class="metric-value">{delta_cents:+.2f} c/kWh</div>
                        <div class="metric-delta {'delta-positive' if delta_cents >= 0 else 'delta-negative'}">
                            {proposed_latest['scenario_label']} vs latest accepted · ${delta_bill:+.2f}/mo at {monthly_kwh:,.0f} kWh · {rider_descriptor}
                            <br><span style="font-size:0.85rem; opacity:0.78;">{coverage_note}</span>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        st.markdown("#### How the proposed all-in is built (base + rider stack)")
        st.caption(
            "Base rate at the bottom, each proposed rider stacked on top, per rate year — the same "
            "kind of composition as the historical all-in stack in Section 2. The dashed line marks "
            "today's accepted all-in for comparison. Rate Year 2 carries the latest rider stack "
            "forward where the filing omits its own summary sheet."
        )
        stack_df = _proposed_rider_stack(str(DB_PATH), float(monthly_kwh))
        summary_cmp = _proposed_comparison(str(DB_PATH), float(monthly_kwh), "summary_total")
        if stack_df.empty or summary_cmp.empty:
            st.info("No itemized proposed rider stack is available yet.")
        else:
            _rider_palette = [
                "#ff5a5f", "#a8ff35", "#f355da", "#00c6ff", "#ffd000", "#fb923c",
                "#10b981", "#38bdf8", "#ec4899", "#94a3b8", "#c084fc", "#f97316",
                "#22d3ee", "#facc15", "#4ade80", "#fca5a5",
            ]
            stack_cols = st.columns(2)
            for col, utility in zip(stack_cols, ["DEP", "DEC"]):
                with col:
                    u_stack = stack_df[stack_df["utility"] == utility]
                    u_cmp = summary_cmp[summary_cmp["utility"] == utility]
                    u_prop = u_cmp[u_cmp["source_status"] == "proposed"].sort_values("scenario_order")
                    if u_stack.empty or u_prop.empty:
                        st.info(f"No proposed rider stack for {utility}.")
                        continue
                    scen = u_prop.drop_duplicates("scenario_order")
                    orders = list(scen["scenario_order"])
                    x_labels = list(scen["scenario_label"])
                    base_by_order = dict(zip(scen["scenario_order"], scen["base_cents_per_kwh"]))
                    total_rider_by_order = dict(zip(scen["scenario_order"], scen["rider_cents_per_kwh"]))

                    fig_stack = go.Figure()
                    fig_stack.add_trace(
                        go.Bar(
                            x=x_labels,
                            y=[float(base_by_order.get(o) or 0.0) for o in orders],
                            name="Base rate",
                            marker_color=CATEGORY_COLORS["base"],
                            hovertemplate="<b>Base rate</b><br>%{y:.3f} c/kWh<extra></extra>",
                        )
                    )
                    riders = list(dict.fromkeys(u_stack["rider_label"]))
                    for i, rl in enumerate(riders):
                        rsub = u_stack[u_stack["rider_label"] == rl]
                        by_order = dict(zip(rsub["scenario_order"], rsub["cents_per_kwh"]))
                        fig_stack.add_trace(
                            go.Bar(
                                x=x_labels,
                                y=[float(by_order.get(o, 0.0)) for o in orders],
                                name=(rl[:30] + "…") if len(rl) > 31 else rl,
                                marker_color=_rider_palette[i % len(_rider_palette)],
                                hovertemplate=f"<b>{rl}</b><br>%{{y:.3f}} c/kWh<extra></extra>",
                            )
                        )
                    # Reconcile to the filing's printed TOTAL (DEC embeds some
                    # base fuel outside the itemized rider rows).
                    gap_y = []
                    for o in orders:
                        gap = float(total_rider_by_order.get(o) or 0.0) - float(
                            u_stack[u_stack["scenario_order"] == o]["cents_per_kwh"].sum()
                        )
                        gap_y.append(gap if gap > 0.005 else 0.0)
                    if any(gap_y):
                        fig_stack.add_trace(
                            go.Bar(
                                x=x_labels,
                                y=gap_y,
                                name="Embedded base fuel (per filing total)",
                                marker_color=CATEGORY_COLORS["residual"],
                                hovertemplate="<b>Embedded base fuel</b><br>%{y:.3f} c/kWh<extra></extra>",
                            )
                        )
                    accepted = u_cmp[u_cmp["source_status"] == "accepted"]
                    if not accepted.empty:
                        acc_allin = float(
                            accepted.iloc[0]["all_in_cents_per_kwh"]
                            or accepted.iloc[0]["base_cents_per_kwh"]
                        )
                        fig_stack.add_hline(
                            y=acc_allin,
                            line_dash="dot",
                            line_color="rgba(255,255,255,0.55)",
                            annotation_text=f"accepted all-in {acc_allin:.2f}",
                            annotation_position="top left",
                            annotation_font_color="#cbd5e1",
                        )
                    fig_stack.update_layout(
                        barmode="stack",
                        template="plotly_dark",
                        height=460,
                        title=f"{utility} proposed all-in composition",
                        yaxis_title="c/kWh",
                        legend=dict(orientation="h", yanchor="top", y=-0.18, x=0, font=dict(size=9), bgcolor="rgba(0,0,0,0)"),
                        margin=dict(t=60, b=120),
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(0,0,0,0)",
                        yaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
                        xaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
                        font=dict(color="#cbd5e1", family="Inter, sans-serif"),
                    )
                    st.plotly_chart(fig_stack, use_container_width=True)

        proposed_display = future_df.copy()
        proposed_display["effective_date"] = pd.to_datetime(proposed_display["effective_date"]).dt.date
        proposed_display = proposed_display[
            [
                "utility",
                "scenario_label",
                "effective_date",
                "schedule",
                "base_cents_per_kwh",
                "rider_cents_per_kwh",
                "all_in_cents_per_kwh",
                "base_bill_amount",
                "all_in_bill_amount",
                "fixed_monthly_charge",
                "proposed_rider_coverage",
                "proposed_rider_count",
                "docket_number",
                "source_page",
                "parser_confidence",
            ]
        ].rename(
            columns={
                "utility": "Utility",
                "scenario_label": "Scenario",
                "effective_date": "Proposed date",
                "schedule": "Schedule",
                "base_cents_per_kwh": "Base c/kWh equiv.",
                "rider_cents_per_kwh": "Proposed rider c/kWh",
                "all_in_cents_per_kwh": "Shown all-in c/kWh",
                "base_bill_amount": "$/mo base equiv.",
                "all_in_bill_amount": "$/mo shown all-in",
                "fixed_monthly_charge": "Fixed $/mo",
                "proposed_rider_coverage": "Rider coverage",
                "proposed_rider_count": "Riders layered",
                "docket_number": "Docket",
                "source_page": "Page",
                "parser_confidence": "Confidence",
            }
        )
        st.dataframe(
            proposed_display,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Base c/kWh equiv.": st.column_config.NumberColumn(
                    format="%.4f",
                    help="Proposed base rate spread over your usage: (fixed monthly charge + energy rate × kWh) ÷ kWh.",
                ),
                "Proposed rider c/kWh": st.column_config.NumberColumn(
                    format="%.4f",
                    help="Rider layer added to base. Under 'Full rider stack' this is the filing's TOTAL cents/kWh; under 'Validated' it is only the checked new riders.",
                ),
                "Shown all-in c/kWh": st.column_config.NumberColumn(
                    format="%.4f", help="Base equivalent + proposed rider layer."
                ),
                "$/mo base equiv.": st.column_config.NumberColumn(
                    format="$%.2f", help="Base-only bill at your selected monthly usage."
                ),
                "$/mo shown all-in": st.column_config.NumberColumn(
                    format="$%.2f", help="All-in bill (base + riders) at your selected monthly usage."
                ),
                "Fixed $/mo": st.column_config.NumberColumn(
                    format="$%.2f", help="Proposed fixed Basic Customer Charge, independent of usage."
                ),
                "Rider coverage": st.column_config.TextColumn(
                    help="summary_total = full filing rider stack; *_carried_forward = projected from the prior rate year; partial_validated = validated subset only.",
                ),
                "Confidence": st.column_config.NumberColumn(
                    format="%.2f", help="Parser confidence for the proposed base block (0–1)."
                ),
            },
        )

    st.markdown("#### Proposed and new riders")
    if rider_summary_df.empty:
        st.info("No proposed rider blocks are available yet.")
    else:
        new_only = st.toggle("Show only new proposed riders", value=True)
        rider_view = rider_summary_df.copy()
        if new_only and "is_new_rider" in rider_view.columns:
            rider_view = rider_view[rider_view["is_new_rider"]]
        if rider_view.empty:
            st.info("No riders match the selected filter.")
        else:
            rider_view["effective_date"] = pd.to_datetime(rider_view["effective_date"], errors="coerce").dt.date
            cols = [
                "utility",
                "scenario_label",
                "effective_date",
                "rider_code",
                "tariff_name",
                "rate_value",
                "rate_unit",
                "validated_status",
                "validated_rate_value",
                "validated_dollars",
                "projection_basis",
                "validated_reason",
                "raw_line",
                "docket_number",
                "start_page",
                "is_new_rider",
            ]
            available = [c for c in cols if c in rider_view.columns]
            display = rider_view[available].rename(
                columns={
                    "utility": "Utility",
                    "scenario_label": "Scenario",
                    "effective_date": "Proposed date",
                    "rider_code": "Rider",
                    "tariff_name": "Name",
                    "rate_value": "Parsed value",
                    "rate_unit": "Unit",
                    "validated_status": "Layer status",
                    "validated_rate_value": "Layered $/kWh",
                    "validated_dollars": "$/mo at usage",
                    "projection_basis": "Basis",
                    "validated_reason": "Validation note",
                    "raw_line": "Source line",
                    "docket_number": "Docket",
                    "start_page": "Page",
                    "is_new_rider": "New rider",
                }
            )
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Parsed value": st.column_config.NumberColumn(format="%.6f"),
                    "Layered $/kWh": st.column_config.NumberColumn(format="%.6f"),
                    "$/mo at usage": st.column_config.NumberColumn(format="$%.2f"),
                },
            )
            st.caption(
                "Only rows marked included are layered into the proposed all-in calculation. "
                "Excluded and catalog-only rows are retained here to show what still needs review."
            )

    st.markdown("#### Growth & the basis for the ask")
    st.caption(
        "Duke's own 'Spring 2025 Forecast' from the application: load grows mostly on the "
        "commercial class and an Economic Development (large-load / data-center) wedge. That growth "
        "is the stated basis for the plant investment and rate-base growth that drive the revenue ask."
    )
    forecast_df = _proposed_forecast(str(DB_PATH))
    capex_df = _proposed_capex_anchors(str(DB_PATH))
    if forecast_df.empty:
        st.info("Load forecast tables have not been parsed yet.")
    else:
        fc_cols = st.columns(2)
        _class_colors = {
            "Residential": CATEGORY_COLORS["base"],
            "Commercial": "#a8ff35",
            "Industrial": "#ffd000",
            "Other": "#94a3b8",
        }
        _driver_colors = {
            "Economic Development": "#ff5a5f",
            "Electric Vehicles": "#00c6ff",
            "Energy Efficiency": "#a8ff35",
            "Rooftop Solar": "#10b981",
            "Voltage Control": "#94a3b8",
        }
        for col, utility in zip(fc_cols, ["DEP", "DEC"]):
            with col:
                u_fc = forecast_df[forecast_df["utility"] == utility]
                rs = u_fc[u_fc["table_type"] == "retail_sales"]
                if rs.empty:
                    st.info(f"No retail-sales forecast for {utility}.")
                    continue
                label = str(rs["forecast_label"].iloc[0])
                fig_sales = go.Figure()
                for cls in ["Residential", "Commercial", "Industrial", "Other"]:
                    sub = rs[rs["segment"] == cls].sort_values("year")
                    if sub.empty:
                        continue
                    fig_sales.add_trace(
                        go.Scatter(
                            x=sub["year"],
                            y=sub["value"],
                            mode="lines",
                            name=cls,
                            stackgroup="sales",
                            line=dict(width=0.5, color=_class_colors.get(cls)),
                            fillcolor=_class_colors.get(cls),
                            hovertemplate=f"<b>{cls}</b> %{{x}}<br>%{{y:,.0f}} GWh<extra></extra>",
                        )
                    )
                total = rs[rs["segment"] == "Total"].sort_values("year")
                if not total.empty:
                    growth = total["value"].iloc[-1] / total["value"].iloc[0] - 1.0
                    title = (
                        f"{utility} forecast retail sales by class — "
                        f"{int(total['year'].iloc[0])}–{int(total['year'].iloc[-1])} "
                        f"(+{growth*100:.0f}%)"
                    )
                else:
                    title = f"{utility} forecast retail sales by class"
                fig_sales.update_layout(
                    template="plotly_dark",
                    height=340,
                    title=title,
                    yaxis_title="GWh",
                    hovermode="x unified",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=10)),
                    margin=dict(t=70, b=30),
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    yaxis=dict(gridcolor="rgba(255,255,255,0.05)", zeroline=False),
                    xaxis=dict(gridcolor="rgba(255,255,255,0.05)", zeroline=False),
                    font=dict(color="#cbd5e1", family="Inter, sans-serif"),
                )
                st.plotly_chart(fig_sales, use_container_width=True)

                # Gross-to-Net drivers: what bends the curve.
                g2n = u_fc[u_fc["table_type"] == "gross_to_net"]
                drivers = [
                    "Economic Development",
                    "Electric Vehicles",
                    "Energy Efficiency",
                    "Rooftop Solar",
                    "Voltage Control",
                ]
                fig_drv = go.Figure()
                for drv in drivers:
                    sub = g2n[g2n["segment"] == drv].sort_values("year")
                    if sub.empty:
                        continue
                    fig_drv.add_trace(
                        go.Bar(
                            x=sub["year"],
                            y=sub["value"],
                            name=drv,
                            marker_color=_driver_colors.get(drv),
                            hovertemplate=f"<b>{drv}</b> %{{x}}<br>%{{y:,.0f}} GWh<extra></extra>",
                        )
                    )
                fig_drv.update_layout(
                    barmode="relative",
                    template="plotly_dark",
                    height=320,
                    title=f"{utility} gross→net sales drivers (GWh vs gross)",
                    yaxis_title="GWh adjustment",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=9)),
                    margin=dict(t=70, b=30),
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    yaxis=dict(gridcolor="rgba(255,255,255,0.05)", zeroline=False),
                    xaxis=dict(gridcolor="rgba(255,255,255,0.05)", zeroline=False),
                    font=dict(color="#cbd5e1", family="Inter, sans-serif"),
                )
                st.plotly_chart(fig_drv, use_container_width=True)

        st.caption(f"Source: {label} forecast tables in the application exhibits.")

        # Capex anchors that connect the growth to the dollar ask.
        if not capex_df.empty:
            anchor_cols = st.columns(2)
            name_to_code = {"Duke Energy Progress": "DEP", "Duke Energy Carolinas": "DEC"}
            ordered = sorted(
                capex_df.to_dict("records"),
                key=lambda r: name_to_code.get(str(r.get("utility")), "ZZ"),
            )
            for col, rec in zip(anchor_cols, ordered):
                code = name_to_code.get(str(rec.get("utility")), str(rec.get("utility")))
                rb = rec.get("rate_base_thousands")
                pis = rec.get("plant_in_service_thousands")
                rr = rec.get("revenue_requirement_thousands")
                tip = (
                    "Test-year (Dec 31, 2024) NC-retail figures from Exhibit 2: the plant Duke "
                    "has built enters rate base; the allowed return on it plus expenses yields the "
                    "base revenue requirement increase."
                )
                col.markdown(
                    f"""
                    <div class="metric-card" title="{tip}">
                        <div class="metric-title">{code} — basis for the ask</div>
                        <div class="metric-value">${(rb or 0)/1e6:.1f}B rate base</div>
                        <div class="metric-delta">
                            Electric plant in service ${(pis or 0)/1e6:.1f}B ·
                            base revenue requirement +${(rr or 0)/1000:.0f}M/yr
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    st.markdown("#### Track record: how Duke's past forecasts trued up")
    st.caption(
        "Under PBR/MYRP several riders are Duke's own reconciliations of actual vs forecast, "
        "filed and Commission-approved — a forecast-error history straight from verified numbers. "
        "Fuel (DEC FCA / DEP EMF): positive = fuel ran above the projection in base rates. "
        "Decoupling (RDM): positive = residential sales came in below the target set last case "
        "(over-forecast). Earnings (ESM): positive = over-earned vs authorized ROE."
    )
    trueup_df = _trueup_series(str(DB_PATH))
    trueup_summary = _trueup_summary(str(DB_PATH))
    if trueup_df.empty:
        st.info("No true-up rider history available yet.")
    else:
        fig_tu = go.Figure()
        _tu_color = {"DEP": "#4facfe", "DEC": "#00ffd0"}
        fuel = trueup_df[trueup_df["category"] == "Fuel cost"]
        for utility in ["DEP", "DEC"]:
            sub = fuel[fuel["utility"] == utility].sort_values("effective_date")
            if sub.empty:
                continue
            fig_tu.add_trace(
                go.Scatter(
                    x=sub["effective_date"],
                    y=sub["cents_per_kwh"],
                    mode="lines+markers",
                    name=f"{utility} fuel true-up",
                    line=dict(color=_tu_color[utility], width=2.5, shape="hv"),
                    hovertemplate=(
                        f"<b>{utility} fuel true-up</b><br>%{{x|%b %Y}}<br>"
                        "%{y:+.3f} c/kWh<extra></extra>"
                    ),
                )
            )
        fig_tu.add_hline(y=0, line_color="rgba(255,255,255,0.35)", line_width=1)
        fig_tu.update_layout(
            template="plotly_dark",
            height=340,
            title="Fuel-cost true-up over time (actual vs projection)",
            yaxis_title="c/kWh true-up (+ = under-forecast)",
            xaxis_title="Effective date",
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
            margin=dict(t=70, b=40),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(gridcolor="rgba(255,255,255,0.05)", zeroline=False),
            xaxis=dict(gridcolor="rgba(255,255,255,0.05)", zeroline=False),
            font=dict(color="#cbd5e1", family="Inter, sans-serif"),
        )
        st.plotly_chart(fig_tu, use_container_width=True)

        if not trueup_summary.empty:
            tu_cards = st.columns(2)
            for col, utility in zip(tu_cards, ["DEP", "DEC"]):
                u = trueup_summary[trueup_summary["utility"] == utility]
                if u.empty:
                    continue

                def _cat(cat: str) -> str:
                    row = u[u["category"] == cat]
                    return f"{float(row['latest_cents'].iloc[0]):+.3f}" if not row.empty else "—"

                def _peak(cat: str) -> str:
                    row = u[u["category"] == cat]
                    if row.empty:
                        return "—"
                    return (
                        f"{float(row['peak_cents'].iloc[0]):+.3f} "
                        f"({str(row['peak_date'].iloc[0])[:7]})"
                    )

                col.markdown(
                    f"""
                    <div class="metric-card" title="Latest filed true-up values; positive fuel = fuel cost above forecast.">
                        <div class="metric-title">{utility} — latest true-ups (c/kWh)</div>
                        <div class="metric-value">Fuel {_cat('Fuel cost')}</div>
                        <div class="metric-delta">
                            Fuel peak {_peak('Fuel cost')} ·
                            Decoupling {_cat('Residential decoupling')} ·
                            Earnings {_cat('Earnings sharing')}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        st.caption(
            "Read: the 2023–24 fuel spikes (DEC peaked +2.30, DEP +1.19 c/kWh) show fuel cost ran "
            "far above the forecast embedded in rates and was later recovered. DEP's +0.23 decoupling "
            "rider means residential sales came in modestly below the prior case's target. ESM at 0 "
            "means no earnings above the authorized band. Full vintage-by-vintage backtesting is "
            "scoped in docs/FORECAST_ACCURACY_PLAN.md."
        )

        st.markdown("##### Is this forecast a break from history?")
        continuity = _growth_continuity(str(DB_PATH))
        cagr_df = _growth_cagr(str(DB_PATH))
        if continuity.empty:
            st.info("Not enough actual/forecast data to compare growth yet.")
        else:
            fig_acc = go.Figure()
            _series_style = {
                "NC actual (EIA)": dict(color="#94a3b8", dash="solid"),
                "Duke DEC+DEP forecast": dict(color="#ff5a5f", dash="dash"),
            }
            for series, style in _series_style.items():
                sub = continuity[continuity["series"] == series].sort_values("year")
                if sub.empty:
                    continue
                fig_acc.add_trace(
                    go.Scatter(
                        x=sub["year"],
                        y=sub["indexed"],
                        mode="lines+markers",
                        name=series,
                        line=dict(color=style["color"], width=2.5, dash=style["dash"]),
                        hovertemplate=(
                            f"<b>{series}</b><br>%{{x}}<br>index %{{y:.1f}} "
                            "(2025=100)<br>%{customdata:,.0f} GWh<extra></extra>"
                        ),
                        customdata=sub["gwh"],
                    )
                )
            fig_acc.add_vline(
                x=2025, line_color="rgba(255,255,255,0.35)", line_dash="dot",
                annotation_text="forecast begins", annotation_position="top left",
                annotation_font_color="#cbd5e1",
            )
            fig_acc.update_layout(
                template="plotly_dark",
                height=320,
                title="Realized NC load vs Duke forecast (indexed, 2025 = 100)",
                yaxis_title="Index (2025 = 100)",
                xaxis_title="Year",
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                margin=dict(t=70, b=40),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(gridcolor="rgba(255,255,255,0.05)", zeroline=False),
                xaxis=dict(gridcolor="rgba(255,255,255,0.05)", zeroline=False),
                font=dict(color="#cbd5e1", family="Inter, sans-serif"),
            )
            st.plotly_chart(fig_acc, use_container_width=True)

            if not cagr_df.empty:
                def _g(scope: str, basis_contains: str) -> str:
                    m = cagr_df[
                        (cagr_df["scope"] == scope)
                        & (cagr_df["basis"].str.contains(basis_contains))
                    ]
                    return f"{float(m['cagr_pct'].iloc[0]):+.2f}%/yr" if not m.empty else "—"

                st.markdown(
                    f"""
                    <div class="metric-card" title="EIA NC actuals are state-level (DEC+DEP+others); Duke forecast is DEC+DEP. Compared on growth, not level.">
                        <div class="metric-title">Forecast vs realized growth</div>
                        <div class="metric-value">{_g('Total','2019–24')} actual → {_g('Total','2025–40')} forecast</div>
                        <div class="metric-delta">
                            Near-term {_g('Total','near-term')} · Commercial {_g('Commercial','Duke')} ·
                            Residential {_g('Residential','Duke')} (a sharp acceleration vs flat history)
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            st.caption(
                "NC retail sales were essentially flat 2019–24 (+0.07%/yr) even as customers grew, "
                "yet the application forecasts ~+2.4%/yr (and +4.4%/yr near-term) — a ~5× acceleration "
                "concentrated in commercial / data-center load. Whether that surge materializes is the "
                "central risk in the dollar ask. (Level mismatch: EIA NC = all utilities; Duke = DEC+DEP, "
                "so this compares growth, not absolute levels. A true prior-vintage backtest needs the "
                "2022-case PDFs fetched from the NCUC portal.)"
            )

    st.markdown("#### Why Duke says these increases are needed")
    if revenue_df.empty:
        st.info(
            "Revenue requirement exhibits have not been parsed yet. The next extraction target is "
            "revenue requirement, rider-offset, and class-impact tables from the same proposed dockets."
        )
    else:
        total_rows = revenue_df[
            revenue_df["description"].isin(
                [
                    "Traditional Base Rate Revenue Requirement",
                    "Rate Year 1 - Total (L1 + L2)",
                    "Cumulative Rate year 2 Revenue Increase (L3 + L4)",
                ]
            )
        ].copy()
        if not total_rows.empty:
            fig_rev = go.Figure()
            for utility in ["Duke Energy Progress", "Duke Energy Carolinas"]:
                utility_rows = total_rows[total_rows["utility"] == utility]
                if utility_rows.empty:
                    continue
                label = "DEP" if "Progress" in utility else "DEC"
                fig_rev.add_trace(
                    go.Bar(
                        x=utility_rows["description"],
                        y=utility_rows["total_impact_millions"],
                        name=label,
                        hovertemplate=(
                            "<b>%{fullData.name}</b><br>%{x}<br>"
                            "Total impact: $%{y:.1f}M<extra></extra>"
                        ),
                    )
                )
            fig_rev.update_layout(
                template="plotly_dark",
                height=360,
                yaxis_title="Requested total impact ($ millions)",
                xaxis_title="Application scenario",
                barmode="group",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                margin=dict(t=70, b=90),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
                xaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
                font=dict(color="#cbd5e1", family="Inter, sans-serif"),
            )
            st.plotly_chart(fig_rev, use_container_width=True)

        revenue_display = revenue_df[
            [
                "utility",
                "description",
                "base_rates_millions",
                "over_amortization_rider_millions",
                "jaar_rider_millions",
                "ptc_rider_millions",
                "bpm_rider_millions",
                "edpr_rider_millions",
                "total_impact_millions",
                "source_page",
            ]
        ].rename(
            columns={
                "utility": "Utility",
                "description": "Revenue adjustment scenario",
                "base_rates_millions": "Base rates $M",
                "over_amortization_rider_millions": "RAL offset $M",
                "jaar_rider_millions": "JAAR offset $M",
                "ptc_rider_millions": "PTC offset $M",
                "bpm_rider_millions": "BPM offset $M",
                "edpr_rider_millions": "EDPR offset $M",
                "total_impact_millions": "Total impact $M",
                "source_page": "Page",
            }
        )
        st.dataframe(
            revenue_display,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Base rates $M": st.column_config.NumberColumn(format="$%.1fM"),
                "RAL offset $M": st.column_config.NumberColumn(format="$%.1fM"),
                "JAAR offset $M": st.column_config.NumberColumn(format="$%.1fM"),
                "PTC offset $M": st.column_config.NumberColumn(format="$%.1fM"),
                "BPM offset $M": st.column_config.NumberColumn(format="$%.1fM"),
                "EDPR offset $M": st.column_config.NumberColumn(format="$%.1fM"),
                "Total impact $M": st.column_config.NumberColumn(format="$%.1fM"),
            },
        )
        st.caption(
            "These figures are parsed from each application Exhibit 1 summary and are stated in "
            "millions of dollars. They describe Duke's requested revenue impacts, not approved outcomes."
        )

    st.markdown("#### Who absorbs the proposed increase")
    if class_impact_df.empty:
        st.info(
            "Class-level workpaper impacts are not parsed yet. The DEC E-1 Item 45 workpapers "
            "are the first target; DEP companion workpapers still need authenticated download."
        )
    else:
        scenario_options = list(class_impact_df["scenario_label"].dropna().unique())
        default_idx = scenario_options.index("MYRP Rate Year 2") if "MYRP Rate Year 2" in scenario_options else 0
        selected_scenario = st.selectbox(
            "Class-impact scenario",
            scenario_options,
            index=default_idx,
            key="proposed_class_impact_scenario",
        )
        class_view = class_impact_df[
            (class_impact_df["scenario_label"] == selected_scenario)
            & (class_impact_df["class_code"] != "Jur Retail")
        ].copy()
        class_view = class_view.sort_values("proposed_increase_millions", ascending=False)
        top_classes = class_view.head(12)
        fig_class = go.Figure(
            go.Bar(
                x=top_classes["class_name"],
                y=top_classes["proposed_increase_millions"],
                marker_color="#f472b6",
                customdata=top_classes[["percent_increase", "source_page"]],
                hovertemplate=(
                    "<b>%{x}</b><br>"
                    "Increase: $%{y:.1f}M<br>"
                    "Increase vs current class revenue: %{customdata[0]:.2f}%<br>"
                    "Source page: %{customdata[1]}<extra></extra>"
                ),
            )
        )
        fig_class.update_layout(
            template="plotly_dark",
            height=380,
            yaxis_title="Proposed class increase ($ millions)",
            xaxis_title="Customer class",
            margin=dict(t=30, b=115),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
            xaxis=dict(gridcolor="rgba(255, 255, 255, 0.05)", zeroline=False),
            font=dict(color="#cbd5e1", family="Inter, sans-serif"),
        )
        st.plotly_chart(fig_class, use_container_width=True)

        residential_rows = class_view[class_view["class_code"].isin(["NCRS", "NCRT", "NCRE"])]
        if not residential_rows.empty:
            residential_total = residential_rows["proposed_increase_millions"].sum()
            total_increase = class_view["proposed_increase_millions"].sum()
            share = (residential_total / total_increase * 100) if total_increase else 0
            st.caption(
                f"For {selected_scenario}, DEC residential-coded classes account for "
                f"${residential_total:,.1f}M of the parsed ${total_increase:,.1f}M class increase "
                f"({share:.1f}%). Source: local E-7 Sub 1329 E-1 Item 45 workpapers."
            )

        impact_display = class_view[
            [
                "utility",
                "scenario_label",
                "class_code",
                "class_name",
                "proposed_increase_millions",
                "current_revenue_millions",
                "proposed_revenue_millions",
                "percent_increase",
                "source_page",
            ]
        ].rename(
            columns={
                "utility": "Utility",
                "scenario_label": "Scenario",
                "class_code": "Class code",
                "class_name": "Class",
                "proposed_increase_millions": "Increase $M",
                "current_revenue_millions": "Current class revenue $M",
                "proposed_revenue_millions": "Proposed class revenue $M",
                "percent_increase": "Increase %",
                "source_page": "Page",
            }
        )
        st.dataframe(
            impact_display,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Increase $M": st.column_config.NumberColumn(format="$%.1fM"),
                "Current class revenue $M": st.column_config.NumberColumn(format="$%.1fM"),
                "Proposed class revenue $M": st.column_config.NumberColumn(format="$%.1fM"),
                "Increase %": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )


# ---------------------------------------------------------------------------
# Section 4 — What should you do?
# ---------------------------------------------------------------------------
with tab4:
    st.header("4 · What should you do?")
    st.caption(
        "Rank every eligible residential rate plan for your usage, see whether shifting "
        "to off-peak hours pays off, and estimate the payback period for rooftop solar. "
        "Plan ranking currently uses base schedule charges only; Section 1 remains the "
        "source for current all-in rider composition."
    )

    from duke_rates.billing.tariff_engine import BillInput  # noqa: E402

    peak_kw = st.number_input(
        "Peak demand kW (only matters for R-TOUD)",
        min_value=0.0,
        value=0.0,
        step=0.5,
        help="If you're not sure, leave at 0 — affects demand-metered residential plans only.",
        key="tou_demand_input"
    )

    with st.expander("TOU usage split (affects R-TOU / R-TOUD comparisons)", expanded=False):
        col_a, col_b = st.columns(2)
        with col_a:
            on_peak_pct = st.slider("On-peak % of usage", 0, 70, 30, 1, key="tou_on_peak_slider")
        with col_b:
            discount_pct = st.slider("Discount-period % of usage", 0, 40, 10, 1, key="tou_discount_slider")
        off_peak_pct = max(0, 100 - on_peak_pct - discount_pct)
        st.caption(f"Off-peak (remainder): {off_peak_pct}%")
        if on_peak_pct + discount_pct > 100:
            st.error("On-peak + discount exceeds 100%.")
            st.stop()

    on_peak_kwh = round(float(monthly_kwh) * on_peak_pct / 100, 1)
    off_peak_kwh = round(float(monthly_kwh) * off_peak_pct / 100, 1)
    discount_kwh = round(float(monthly_kwh) - on_peak_kwh - off_peak_kwh, 1)

    usage = BillInput(
        monthly_kwh=float(monthly_kwh),
        service_date=service_date,
        on_peak_kwh=on_peak_kwh,
        off_peak_kwh=off_peak_kwh,
        discount_kwh=discount_kwh,
        peak_kw=peak_kw if peak_kw > 0 else None,
    )

    repo, engine = _engine(str(DB_PATH))
    families = _residential_families(str(DB_PATH), state, company)
    if not families:
        st.warning(f"No residential schedules found for {state}/{company}.")
    else:
        st.warning(
            "Optimizer totals are shown base-only. The raw tariff engine can over-apply "
            "optional riders with `%` or `$ / block` units to every kWh, which inflates "
            "some rider-inclusive totals by thousands of dollars. Rider-inclusive plan "
            "ranking is disabled here until those applicability quantities are guarded."
        )
        results, partial = [], []
        for fk, _, _ in families:
            r = engine.calculate(fk, usage, customer_class="residential", include_riders=False)
            if any("Partial TOU coverage" in w for w in r.warnings):
                partial.append(r)
            elif r.base_subtotal > 0:
                results.append(r)
        results.sort(key=lambda r: r.total)

        if not results:
            st.warning("No schedules returned results for the current inputs.")
        else:
            res_result = next((r for r in results if r.family_key and "leaf-500" in r.family_key), None)
            if res_result is None:
                res_result = next(
                    (r for r in results if not any(i.charge_type == "tou_energy" for i in r.line_items)),
                    results[-1],
                )
            baseline_total = res_result.total if res_result else None

            rows = []
            for r in results:
                total = round(r.total, 2)
                if baseline_total is not None and total != baseline_total:
                    delta_mo = total - baseline_total
                    vs_baseline = f"{'−' if delta_mo < 0 else '+'}${abs(delta_mo):.2f}/mo"
                else:
                    vs_baseline = "— baseline"
                rows.append(
                    {
                        "Schedule": r.schedule_title or r.family_key,
                        "Base": round(r.base_subtotal, 2),
                        "Riders": "disabled",
                        "Total": total,
                        "vs flat RES": vs_baseline,
                        "Confidence": f"{r.source_confidence:.0%}",
                    }
                )
            df = pd.DataFrame(rows)
            cheapest_total = df["Total"].min()
            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Base": st.column_config.NumberColumn(format="$%.2f"),
                    "Total": st.column_config.NumberColumn(format="$%.2f"),
                },
            )
            cheapest = results[0]
            if baseline_total is not None and cheapest.total < baseline_total:
                savings_mo = round(baseline_total - cheapest.total, 2)
                st.success(
                    f"**Best plan: {cheapest.schedule_title or cheapest.family_key}** — "
                    f"saves **${savings_mo:.2f}/month** (${savings_mo * 12:.0f}/year) "
                    "vs. flat RES at your usage profile."
                )

            with st.expander("Line-item detail by schedule", expanded=False):
                for r in results:
                    title = r.schedule_title or r.family_key
                    st.markdown(f"**{title}** — ${r.total:.2f}/mo")
                    items = []
                    for it in r.line_items:
                        items.append(
                            {
                                "Description": it.label,
                                "Type": it.charge_type,
                                "Rate": f"{it.rate_value:.5f} {it.rate_unit}" if it.rate_value else "",
                                "Qty": f"{it.quantity:,.1f}" if it.quantity is not None else "",
                                "Amount": f"${it.amount:,.2f}",
                            }
                        )
                    st.dataframe(pd.DataFrame(items), use_container_width=True, hide_index=True)
                    if r.warnings:
                        for w in r.warnings:
                            st.caption(f"⚠ {w}")
                    st.markdown("---")

            if partial:
                with st.expander(f"Excluded schedules ({len(partial)})"):
                    for r in partial:
                        st.markdown(f"- **{r.schedule_title or r.family_key}**: " + "; ".join(r.warnings))


st.markdown("---")
with st.expander("Methodology & data freshness", expanded=False):
    st.markdown(
        f"""
- **Rate timeline**: built from parsed NCUC tariff filings (DEP RES + DEC RS).
  Latest filing in database: **{last_eff.strftime("%Y-%m-%d") if pd.notna(last_eff) else "unknown"}**.
- **Rider components**: DEP 2023-10+ comes from clean Leaf 600 rider summary sheets;
  pre-2023 is reconstructed from older filings. DEC component data is sparser
  (2018-08+, RS only).
- **Events**: stored in `legislative_actions` table; add rows to extend the
  annotated history with no code changes.
- **Bill calculator** (Section 3): uses the `TariffBillingEngine` with the
  parsed tariff_versions effective on your selected service date.
- For full audit/confidence detail use the standalone EIA app and the
  `streamlit_rate_comparison_app.py` calculator.
"""
    )

