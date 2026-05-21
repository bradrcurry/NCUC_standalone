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
from duke_rates.charts.residential_dashboard import (
    CATEGORY_COLORS,
    annotated_history_chart,
    rider_breakdown_donut,
    rider_buildup_area,
)

DB_PATH = ROOT / "data" / "db" / "duke_rates.db"

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
def _breakdown(db_path: str, utility: str, monthly_kwh: float) -> pd.DataFrame:
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
        help="Sections 1 and 3 focus on this utility. Section 2 always compares both.",
    )
    state, company = _STATE_COMPANY_OPTIONS[utility_label]
    primary_utility = "DEP" if company == "progress" else "DEC"

    st.markdown("---")
    show_eia_overlay = st.toggle("Show NC + US EIA averages in Section 2", value=True)


# ---------------------------------------------------------------------------
# Hero metrics
# ---------------------------------------------------------------------------

st.title("Duke Energy NC — what you actually pay")
st.caption(
    "A residential-customer view of the DEP and DEC rate stack: not just the base rate, "
    "but every named rider that lands on your bill, where it came from, and what your "
    "options are."
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
    kpi_cols[0].metric(
        "DEP all-in ¢/kWh",
        f"{float(dep_latest['all_in_cents_per_kwh']):.2f}",
        f"{delta_dep:+.2f} since {pd.to_datetime(dep_first['effective_date']).year}",
    )
else:
    kpi_cols[0].metric("DEP all-in ¢/kWh", "—")

if dec_latest is not None and dec_first is not None:
    delta_dec = float(dec_latest["all_in_cents_per_kwh"]) - float(dec_first["all_in_cents_per_kwh"])
    kpi_cols[1].metric(
        "DEC all-in ¢/kWh",
        f"{float(dec_latest['all_in_cents_per_kwh']):.2f}",
        f"{delta_dec:+.2f} since {pd.to_datetime(dec_first['effective_date']).year}",
    )
else:
    kpi_cols[1].metric("DEC all-in ¢/kWh", "—")

# Rider share for featured utility
if primary_utility in latest_per_utility.index:
    row = latest_per_utility.loc[primary_utility]
    base = float(row["base_cents_per_kwh"] or 0.0)
    all_in = float(row["all_in_cents_per_kwh"] or 0.0)
    rider_share = (all_in - base) / all_in * 100.0 if all_in else 0.0
    kpi_cols[2].metric(
        f"{primary_utility} rider share",
        f"{rider_share:.1f}%",
        help="What share of your per-kWh charge comes from riders rather than base rates.",
    )
else:
    kpi_cols[2].metric("Rider share", "—")

# Last data refresh: max effective_date in timeline
last_eff = pd.to_datetime(timeline_df["effective_date"]).max()
kpi_cols[3].metric(
    "Latest rate-filing date",
    last_eff.strftime("%b %Y") if pd.notna(last_eff) else "—",
    help="Most recent residential tariff version known to the database.",
)


# ---------------------------------------------------------------------------
# Section 1 — Where your dollar actually goes
# ---------------------------------------------------------------------------

st.markdown("---")
st.header("1 · Where your dollar actually goes")
st.caption(
    f"Breakdown of the most recent {primary_utility} residential bill at "
    f"{monthly_kwh:,.0f} kWh/month. Base rate is the part you'd see in a rate-case "
    "headline; the surrounding wedges are the named riders that show up on the "
    "second page of your bill — fuel adjustments, solar program costs, energy-efficiency "
    "fees, EDIT tax credits, and more."
)

breakdown_df = _breakdown(str(DB_PATH), primary_utility, float(monthly_kwh))
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
        st.markdown(f"### What this view shows")
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

    # --- "Meet your riders" — readable card layout for what each rider does ---
    rider_rows = breakdown_df[breakdown_df["component_kind"] == "rider"].copy()
    if not rider_rows.empty:
        st.markdown("### Meet your riders")
        st.caption(
            "Plain-English explanation of each named rider on your current bill, "
            "sorted by impact. Click to learn what it is and why it's there."
        )
        rider_rows = rider_rows.reindex(
            rider_rows["dollars"].abs().sort_values(ascending=False).index
        )
        for _, row in rider_rows.iterrows():
            code = row["component"]
            name = row["short_name"] if row["short_name"] != code else ""
            dollars = float(row["dollars"])
            cents = float(row["cents_per_kwh"])
            category = row["category"] or "rider"
            description = (row["description"] or "").strip()
            polarity = "credit" if dollars < 0 else "charge"
            header = f"**{code}** "
            if name:
                header += f"— {name} "
            header += f"·  ${dollars:+,.2f}/mo  ·  {cents:+.4f} ¢/kWh  ·  _{category}_"
            with st.expander(header, expanded=False):
                if description:
                    st.markdown(description)
                else:
                    st.caption("No plain-English description on file for this rider yet.")
                st.caption(
                    f"At {monthly_kwh:,.0f} kWh this rider contributes "
                    f"**${dollars:+,.2f}/month** ({polarity}). "
                    f"Source: `rider_descriptions` table — add or improve entries there "
                    f"and they'll show up here automatically."
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

st.markdown("---")
st.header("2 · How we got here")
st.caption(
    "DEP and DEC residential all-in rates over time, annotated with the laws and "
    "market events that drove the major changes. Dashed verticals are events — "
    "hover for the story behind each one."
)

eia_df = _eia(start_year=2016) if show_eia_overlay else pd.DataFrame()
st.plotly_chart(
    annotated_history_chart(
        timeline_df,
        events_df=events_df,
        utilities=["DEP", "DEC"],
        monthly_kwh=float(monthly_kwh),
        show_eia=show_eia_overlay,
        eia_df=eia_df,
    ),
    use_container_width=True,
)

if not events_df.empty:
    with st.expander("Event details (timeline annotations)", expanded=False):
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
            "Add more rows there (or via a future seed script) and they'll appear automatically."
        )

st.markdown("#### Rider stack history")
st.caption(
    f"How the rider portion of the {primary_utility} bill has grown over time, "
    "broken out by named rider. Pre-2023 DEP data is reconstructed from older filings; "
    "DEC component-level data is sparser (post-2018 only)."
)
components_df = _components(str(DB_PATH), primary_utility)
if components_df.empty:
    st.info(f"No itemized rider component history available for {primary_utility}.")
else:
    st.plotly_chart(
        rider_buildup_area(components_df, utility=primary_utility),
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
    template="plotly_white",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    height=360,
    margin=dict(t=70, b=40),
)
st.plotly_chart(fig_bill_hist, use_container_width=True)


# ---------------------------------------------------------------------------
# Section 3 — What should you do?
# ---------------------------------------------------------------------------

st.markdown("---")
st.header("3 · What should you do?")
st.caption(
    "Rank every eligible residential rate plan for your usage, see whether shifting "
    "to off-peak hours pays off, and estimate the payback period for rooftop solar."
)

from duke_rates.billing.tariff_engine import BillInput  # noqa: E402


peak_kw = st.number_input(
    "Peak demand kW (only matters for R-TOUD)",
    min_value=0.0,
    value=0.0,
    step=0.5,
    help="If you're not sure, leave at 0 — affects demand-metered residential plans only.",
)

with st.expander("TOU usage split (affects R-TOU / R-TOUD comparisons)", expanded=False):
    col_a, col_b = st.columns(2)
    with col_a:
        on_peak_pct = st.slider("On-peak % of usage", 0, 70, 30, 1)
    with col_b:
        discount_pct = st.slider("Discount-period % of usage", 0, 40, 10, 1)
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
    results, partial = [], []
    for fk, title, _ in families:
        r = engine.calculate(fk, usage, customer_class="residential", include_riders=True)
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
                    "Riders": round(r.rider_subtotal, 2),
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
                "Riders": st.column_config.NumberColumn(format="$%.2f"),
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
