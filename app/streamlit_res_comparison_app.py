from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pandas as pd
import streamlit as st

from duke_rates.analytics.canonical_residential import (
    export_canonical_residential_timeline,
    load_canonical_residential_timeline,
)
from duke_rates.analytics.canonical_rider_components import (
    load_dep_res_canonical_rider_components,
    load_dec_rs_canonical_rider_components,
)
from duke_rates.analytics.eia_analytics import load_price_history
from duke_rates.analytics.rider_trust import load_rider_trust_table
from duke_rates.charts import (
    as_of_utility_comparison_chart,
    combined_utility_metric_chart,
    confidence_summary_chart,
    confidence_timeline_chart,
    utility_driver_chart,
)
import plotly.express as px
import plotly.graph_objects as go


CANONICAL_OUTPUT_DIR = ROOT / "data/processed/canonical_residential"
CANONICAL_CSV_PATH = CANONICAL_OUTPUT_DIR / "canonical_residential_timeline.csv"

UTILITY_LABELS = {
    "DEP": "DEP (Duke Energy Progress, RES)",
    "DEC": "DEC (Duke Energy Carolinas, RS)",
}

UTILITY_COLORS = {
    "DEP": "#0F766E",
    "DEC": "#B45309",
}

METRIC_OPTIONS = {
    "All-in rate": {
        "metric_column": "all_in_cents_per_kwh",
        "base_overlay_column": "base_cents_per_kwh",
        "yaxis_title": "Cents per kWh",
        "driver_mode": "cents",
        "kpi_column": "all_in_cents_per_kwh",
        "is_bill_metric": False,
    },
    "Base rate": {
        "metric_column": "base_cents_per_kwh",
        "base_overlay_column": None,
        "yaxis_title": "Cents per kWh",
        "driver_mode": "cents",
        "kpi_column": "base_cents_per_kwh",
        "is_bill_metric": False,
    },
    "Rider contribution": {
        "metric_column": "rider_cents_per_kwh",
        "base_overlay_column": None,
        "yaxis_title": "Cents per kWh",
        "driver_mode": "cents",
        "kpi_column": "rider_cents_per_kwh",
        "is_bill_metric": False,
    },
    "Bill amount": {
        "metric_column": "all_in_bill_amount",
        "base_overlay_column": "base_bill_amount",
        "yaxis_title": "Bill amount ($)",
        "driver_mode": "bill",
        "kpi_column": "all_in_bill_amount",
        "is_bill_metric": True,
    },
}

COVERAGE_LABELS = {
    "same_day": "Direct rider match",
    "carried_forward": "Prior rider carried forward",
    "uncovered": "No rider coverage",
}

SOURCE_LABELS = {
    "clean": "Directly parsed",
    "provisional": "Reconstructed",
}


_TRUST_TIER_COLORS = {
    "high": "#2ecc71",
    "medium": "#f39c12",
    "low": "#e74c3c",
    "unverified": "#95a5a6",
}

_RATE_CLASS_GROUP_LABELS = {
    "dep_residential": "DEP Residential (RES / R-TOU / R-TOUD)",
    "dep_sgs": "DEP Small Commercial (SGS / SGS-TOUE)",
    "dep_sgs_clr": "DEP SGS Constant Load (SGS-TOU-CLR)",
    "dec_residential": "DEC Residential (RS)",
}


@st.cache_data(show_spinner=False)
def _load_trust_table(database_path: str) -> pd.DataFrame:
    df = load_rider_trust_table(database_path=Path(database_path))
    if not df.empty and "effective_date" in df.columns:
        df["effective_date"] = pd.to_datetime(df["effective_date"])
    return df


@st.cache_data(show_spinner=False)
def _load_canonical_from_csv(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    if "effective_date" in df.columns:
        df["effective_date"] = pd.to_datetime(df["effective_date"])
    if "rider_effective_date" in df.columns:
        df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce")
    return df

@st.cache_data(show_spinner=False)
def _load_rider_components(database_path: str, utility: str) -> pd.DataFrame:
    if utility == "DEP":
        return load_dep_res_canonical_rider_components(database_path=Path(database_path))
    elif utility == "DEC":
        return load_dec_rs_canonical_rider_components(database_path=Path(database_path))
    return pd.DataFrame()

@st.cache_data(show_spinner=False)
def _load_eia_data() -> pd.DataFrame:
    try:
        us_avg = load_price_history(
            states=["US"],
            sector="RES",
            frequency="annual",
            start_year=2016,
        )
        nc_avg = load_price_history(
            states=["NC"],
            sector="RES",
            frequency="annual",
            start_year=2016,
        )
        if not us_avg.empty and not nc_avg.empty:
            df = pd.concat([us_avg, nc_avg], ignore_index=True)
            return df
    except Exception:
        pass
    return pd.DataFrame()


def _load_canonical_data(*, database_path: Path, representative_kwh: float) -> tuple[pd.DataFrame, str, str | None]:
    if CANONICAL_CSV_PATH.exists():
        built_at = pd.Timestamp(CANONICAL_CSV_PATH.stat().st_mtime, unit="s").strftime("%Y-%m-%d %H:%M:%S")
        return _load_canonical_from_csv(str(CANONICAL_CSV_PATH)), "cached CSV", built_at
    df = load_canonical_residential_timeline(
        database_path=database_path,
        representative_kwh=representative_kwh,
    )
    return df, "live DB", None


def _prepare_dashboard_df(canonical_df: pd.DataFrame) -> pd.DataFrame:
    df = canonical_df.copy()
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    if "rider_effective_date" in df.columns:
        df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce")
    df["rider_bill_amount"] = df["all_in_bill_amount"] - df["base_bill_amount"]
    df["utility_label"] = df["utility"].map(UTILITY_LABELS).fillna(df["utility"])
    df["coverage_label"] = df["rider_coverage_status"].map(COVERAGE_LABELS).fillna("Unknown coverage")
    df["source_label"] = df["rider_source_kind"].map(SOURCE_LABELS).fillna("Unknown source")
    df["confidence_label"] = df.apply(_confidence_label, axis=1)
    df["high_confidence"] = df["confidence_label"].eq("High confidence")
    return df


def _build_aligned_component_timeline(
    utility_rows: pd.DataFrame,
    components_df: pd.DataFrame,
) -> pd.DataFrame:
    """Project rider-component snapshots onto each canonical timeline date.

    The canonical timeline can carry a prior rider snapshot forward when no same-day
    rider-summary filing exists. The old UI only plotted component rows whose
    ``effective_date`` exactly matched the base-rate row, which hid legitimate
    carried-forward rider contributions. This helper aligns component rows to the
    canonical row's ``rider_effective_date`` and adds a residual bucket when the
    rider total cannot be fully explained by known per-component rows.
    """
    if utility_rows.empty:
        return pd.DataFrame(
            columns=[
                "effective_date",
                "component",
                "cents_per_kwh",
                "source_kind",
                "component_effective_date",
                "coverage_status",
                "component_class",
            ]
        )

    rows: list[dict[str, object]] = []
    component_lookup: dict[pd.Timestamp, pd.DataFrame] = {}
    if not components_df.empty:
        temp = components_df.copy()
        temp["effective_date"] = pd.to_datetime(temp["effective_date"])
        for effective_date, snapshot in temp.groupby("effective_date", sort=True):
            component_lookup[pd.Timestamp(effective_date)] = snapshot.copy()

    for _, timeline_row in utility_rows.sort_values("effective_date").iterrows():
        plotted_date = pd.Timestamp(timeline_row["effective_date"])
        rider_effective_date = pd.to_datetime(timeline_row.get("rider_effective_date"), errors="coerce")
        rider_total = float(timeline_row.get("rider_cents_per_kwh") or 0.0)
        coverage_status = timeline_row.get("rider_coverage_status", "unknown")
        source_kind = timeline_row.get("rider_source_kind", "unknown")
        snapshot = component_lookup.get(pd.Timestamp(rider_effective_date)) if pd.notna(rider_effective_date) else None

        explained_total = 0.0
        if snapshot is not None and not snapshot.empty:
            for _, component_row in snapshot.iterrows():
                cents = float(component_row["cents_per_kwh"] or 0.0)
                explained_total += cents
                rows.append(
                    {
                        "effective_date": plotted_date,
                        "component": component_row["rider_code"],
                        "cents_per_kwh": cents,
                        "source_kind": component_row.get("source_kind", source_kind),
                        "component_effective_date": rider_effective_date,
                        "coverage_status": coverage_status,
                        "component_class": "rider",
                    }
                )

        residual = round(rider_total - explained_total, 6)
        if abs(residual) > 0.0001:
            rows.append(
                {
                    "effective_date": plotted_date,
                    "component": "Residual / non-itemized",
                    "cents_per_kwh": residual,
                    "source_kind": source_kind,
                    "component_effective_date": rider_effective_date,
                    "coverage_status": coverage_status,
                    "component_class": "residual",
                }
            )

    if not rows:
        return pd.DataFrame(
            columns=[
                "effective_date",
                "component",
                "cents_per_kwh",
                "source_kind",
                "component_effective_date",
                "coverage_status",
                "component_class",
            ]
        )

    aligned = pd.DataFrame(rows)
    aligned["effective_date"] = pd.to_datetime(aligned["effective_date"])
    aligned["component_effective_date"] = pd.to_datetime(
        aligned["component_effective_date"], errors="coerce"
    )
    return aligned.sort_values(["effective_date", "component"]).reset_index(drop=True)


def _build_all_in_vs_eia_chart(
    utility_rows: pd.DataFrame,
    *,
    utility: str,
    eia_df: pd.DataFrame,
) -> go.Figure:
    fig = go.Figure()
    utility_rows = utility_rows.sort_values("effective_date").copy()
    utility_label = UTILITY_LABELS.get(utility, utility)

    fig.add_trace(
        go.Scatter(
            x=utility_rows["effective_date"],
            y=utility_rows["all_in_cents_per_kwh"],
            mode="lines+markers",
            name=f"{utility} all-in",
            line=dict(color=UTILITY_COLORS.get(utility, "#4A5568"), width=3, shape="hv"),
            marker=dict(size=7),
            hovertemplate="<b>%{x|%Y-%m-%d}</b><br>All-in: %{y:.3f} ¢/kWh<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=utility_rows["effective_date"],
            y=utility_rows["base_cents_per_kwh"],
            mode="lines",
            name=f"{utility} base",
            line=dict(color=UTILITY_COLORS.get(utility, "#4A5568"), width=2, dash="dot", shape="hv"),
            opacity=0.8,
            hovertemplate="<b>%{x|%Y-%m-%d}</b><br>Base: %{y:.3f} ¢/kWh<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=utility_rows["effective_date"],
            y=utility_rows["rider_cents_per_kwh"],
            mode="lines",
            name=f"{utility} riders",
            line=dict(color="#D69E2E", width=2, dash="dash", shape="hv"),
            hovertemplate="<b>%{x|%Y-%m-%d}</b><br>Riders: %{y:.3f} ¢/kWh<extra></extra>",
        )
    )

    if not eia_df.empty:
        for state, color, dash, label in [
            ("NC", "#7F1D1D", "dot", "NC EIA average"),
            ("US", "#1F2937", "dash", "US EIA average"),
        ]:
            state_eia = eia_df[eia_df["state"] == state].sort_values("year")
            if state_eia.empty:
                continue
            x_values: list[pd.Timestamp] = []
            y_values: list[float] = []
            for _, row in state_eia.iterrows():
                year = int(row["year"])
                x_values.extend(
                    [pd.Timestamp(year=year, month=1, day=1), pd.Timestamp(year=year, month=12, day=31)]
                )
                y_values.extend([float(row["price_cents_per_kwh"])] * 2)
            fig.add_trace(
                go.Scatter(
                    x=x_values,
                    y=y_values,
                    mode="lines",
                    name=label,
                    line=dict(color=color, width=2.5, dash=dash),
                    hovertemplate=f"<b>{label}</b>: %{{y:.2f}} ¢/kWh<extra></extra>",
                )
            )

    fig.update_layout(
        title=f"{utility_label} all-in timeline vs NC and US EIA residential averages",
        xaxis_title="Effective date",
        yaxis_title="Cents per kWh",
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(t=80, b=40),
    )
    return fig


def _confidence_label(row: pd.Series) -> str:
    if row.get("rider_coverage_status") == "uncovered":
        return "No rider coverage"
    if row.get("rider_source_kind") == "clean" and row.get("rider_coverage_status") == "same_day":
        return "High confidence"
    if row.get("rider_source_kind") == "clean":
        return "Moderate confidence"
    if row.get("rider_source_kind") == "provisional":
        return "Reconstructed history"
    return "Unknown confidence"


def _latest_as_of(df: pd.DataFrame, as_of_date) -> pd.DataFrame:
    cutoff = pd.to_datetime(as_of_date)
    eligible = df[df["effective_date"] <= cutoff].sort_values("effective_date")
    if eligible.empty:
        return eligible
    return eligible.groupby("utility", as_index=False).tail(1)


def _starting_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df.sort_values("effective_date").groupby("utility", as_index=False).head(1)


st.set_page_config(page_title="Duke NC Residential Rate Timeline", layout="wide")

database_path = ROOT / "data/db/duke_rates.db"

st.sidebar.header("Core Controls")
representative_kwh = st.sidebar.number_input(
    "Representative kWh",
    min_value=100,
    max_value=5000,
    value=1000,
    step=100,
)
metric_choice = st.sidebar.selectbox(
    "Metric",
    options=list(METRIC_OPTIONS.keys()),
    index=0,
)

if st.sidebar.button("Refresh Canonical CSV From DB", use_container_width=True):
    with st.spinner("Refreshing canonical residential timeline from SQLite..."):
        export_canonical_residential_timeline(
            CANONICAL_OUTPUT_DIR,
            database_path=database_path,
            representative_kwh=float(representative_kwh),
        )
        _load_canonical_from_csv.clear()
    st.sidebar.success("Canonical CSV refreshed.")

canonical_df, data_source_label, built_at = _load_canonical_data(
    database_path=database_path,
    representative_kwh=float(representative_kwh),
)

if canonical_df.empty:
    st.error("No canonical residential timeline data is available.")
    st.stop()

dashboard_df = _prepare_dashboard_df(canonical_df)
snapshot_kwh = (
    int(float(dashboard_df["representative_kwh"].dropna().iloc[0]))
    if "representative_kwh" in dashboard_df.columns and not dashboard_df["representative_kwh"].dropna().empty
    else None
)

min_date = dashboard_df["effective_date"].min().date()
max_date = dashboard_df["effective_date"].max().date()

primary_utility = st.sidebar.selectbox(
    "Utility",
    options=sorted(dashboard_df["utility"].unique().tolist()),
    index=0,
    format_func=lambda value: UTILITY_LABELS.get(value, value),
)
selected_utilities = [primary_utility]
date_range = st.sidebar.date_input(
    "Date range",
    value=(min_date, max_date),
    min_value=min_date,
    max_value=max_date,
)
as_of_date = st.sidebar.date_input(
    "Comparison as-of date",
    value=max_date,
    min_value=min_date,
    max_value=max_date,
)

with st.sidebar.expander("Advanced Controls", expanded=False):
    selected_coverage = st.multiselect(
        "Rider coverage status",
        options=sorted(dashboard_df["rider_coverage_status"].dropna().unique().tolist()),
        default=sorted(dashboard_df["rider_coverage_status"].dropna().unique().tolist()),
        format_func=lambda value: COVERAGE_LABELS.get(value, value),
    )
    selected_sources = st.multiselect(
        "Rider source kind",
        options=sorted(dashboard_df["rider_source_kind"].dropna().unique().tolist()),
        default=sorted(dashboard_df["rider_source_kind"].dropna().unique().tolist()),
        format_func=lambda value: SOURCE_LABELS.get(value, value),
    )
    include_reconstructed = st.toggle("Include reconstructed rows", value=True)
    include_uncovered = st.toggle("Include uncovered rows", value=True)
    high_confidence_only = st.toggle("Show high-confidence only", value=False)

status_lines = [
    f"Data snapshot built: {built_at or 'live session'}",
    f"Source: {data_source_label}",
    f"Row count: {len(dashboard_df)}",
]
if snapshot_kwh is not None:
    status_lines.append(f"Snapshot representative kWh: {snapshot_kwh}")
st.sidebar.info("\n\n".join(status_lines))

if snapshot_kwh is not None and int(representative_kwh) != snapshot_kwh:
    st.sidebar.warning(
        f"Current snapshot was built at {snapshot_kwh} kWh. "
        "Use the refresh button to rebuild the canonical CSV for the selected value."
    )

start_date, end_date = date_range if isinstance(date_range, tuple) and len(date_range) == 2 else (min_date, max_date)
filtered_df = dashboard_df[
    dashboard_df["utility"].isin(selected_utilities)
    & (dashboard_df["effective_date"].dt.date >= start_date)
    & (dashboard_df["effective_date"].dt.date <= end_date)
    & dashboard_df["rider_coverage_status"].isin(selected_coverage)
    & dashboard_df["rider_source_kind"].isin(selected_sources)
].copy()

if not include_reconstructed:
    filtered_df = filtered_df[filtered_df["rider_source_kind"] != "provisional"]
if not include_uncovered:
    filtered_df = filtered_df[filtered_df["rider_coverage_status"] != "uncovered"]
if high_confidence_only:
    filtered_df = filtered_df[filtered_df["high_confidence"]]

if filtered_df.empty:
    st.warning("No rows match the selected filters.")
    st.stop()

metric_config = METRIC_OPTIONS[metric_choice]
latest_rows = _latest_as_of(filtered_df, as_of_date)
start_rows = _starting_rows(filtered_df)

dep_latest = latest_rows.loc[latest_rows["utility"] == "DEP", "all_in_cents_per_kwh"]
dec_latest = latest_rows.loc[latest_rows["utility"] == "DEC", "all_in_cents_per_kwh"]
dep_latest_value = float(dep_latest.iloc[0]) if not dep_latest.empty else None
dec_latest_value = float(dec_latest.iloc[0]) if not dec_latest.empty else None
gap_value = dep_latest_value - dec_latest_value if dep_latest_value is not None and dec_latest_value is not None else None

rider_share = None
if not latest_rows.empty:
    rider_share_series = latest_rows["rider_cents_per_kwh"] / latest_rows["all_in_cents_per_kwh"]
    rider_share = float(rider_share_series.mean() * 100.0) if not rider_share_series.dropna().empty else None

change_since_start = None
if not latest_rows.empty and not start_rows.empty:
    start_metric = start_rows.set_index("utility")[metric_config["kpi_column"]]
    latest_metric = latest_rows.set_index("utility")[metric_config["kpi_column"]]
    aligned = latest_metric.to_frame("latest").join(start_metric.to_frame("start"), how="inner")
    if not aligned.empty:
        change_since_start = float((aligned["latest"] - aligned["start"]).mean())

high_conf_pct = float(filtered_df["high_confidence"].mean() * 100.0) if not filtered_df.empty else None

st.title("Duke NC Residential Rate Timeline")
st.caption(
    "Residential-only view: DEP RES vs DEC RS (2016–2026). "
    "This app does not currently represent DEP SGS/LGS/MGS or DEC non-RS schedules."
)

if snapshot_kwh is not None and int(representative_kwh) != snapshot_kwh:
    st.warning(
        f"Displayed data is currently based on a {snapshot_kwh} kWh snapshot. "
        f"Refresh from DB to rebuild the canonical CSV for {int(representative_kwh)} kWh."
    )

st.markdown("## Current Snapshot")
metric_cols = st.columns(6)
metric_cols[0].metric("DEP latest all-in rate", f"{dep_latest_value:.3f} c/kWh" if dep_latest_value is not None else "N/A")
metric_cols[1].metric("DEC latest all-in rate", f"{dec_latest_value:.3f} c/kWh" if dec_latest_value is not None else "N/A")
metric_cols[2].metric("DEP–DEC gap", f"{gap_value:.3f} c/kWh" if gap_value is not None else "N/A")
metric_cols[3].metric("Rider share", f"{rider_share:.1f}%" if rider_share is not None else "N/A")
metric_cols[4].metric(
    "Change since period start",
    (
        f"{change_since_start:.3f} {'$' if metric_config['is_bill_metric'] else 'c/kWh'}"
        if change_since_start is not None
        else "N/A"
    ),
)
metric_cols[5].metric("High-confidence coverage", f"{high_conf_pct:.1f}%" if high_conf_pct is not None else "N/A")

action_col_a, action_col_b = st.columns(2)
with action_col_a:
    st.download_button(
        "Download filtered data",
        data=filtered_df.to_csv(index=False).encode("utf-8"),
        file_name="filtered_residential_timeline.csv",
        mime="text/csv",
        use_container_width=True,
    )
with action_col_b:
    st.download_button(
        "Download canonical snapshot",
        data=dashboard_df.to_csv(index=False).encode("utf-8"),
        file_name="canonical_residential_timeline.csv",
        mime="text/csv",
        use_container_width=True,
    )

st.markdown("## Rate Timeline (All-in Components & EIA Context)")
eia_df = _load_eia_data()

utility = primary_utility
utility_rows = filtered_df[filtered_df["utility"] == utility].copy()
if utility_rows.empty:
    st.info(f"No rows available for {utility} under current filters.")
else:
    components_df = _load_rider_components(str(database_path), utility)
    aligned_components_df = _build_aligned_component_timeline(utility_rows, components_df)
    
    if metric_choice != "All-in rate" or components_df.empty:
        # Fall back to base vs driver chart if we aren't looking at "All-in rate" or lack component data
        st.plotly_chart(
            utility_driver_chart(
                filtered_df,
                utility=utility,
                title=f"{utility} driver decomposition",
                value_mode=metric_config["driver_mode"],
            ),
            use_container_width=True,
        )
    else:
        st.plotly_chart(
            _build_all_in_vs_eia_chart(utility_rows, utility=utility, eia_df=eia_df),
            use_container_width=True,
        )

        residual_rows = aligned_components_df[
            aligned_components_df["component_class"] == "residual"
        ].copy()
        if not residual_rows.empty:
            unexplained_dates = residual_rows["effective_date"].dt.strftime("%Y-%m-%d").tolist()
            st.info(
                "Some timeline dates include a `Residual / non-itemized` rider bucket. "
                "That means the canonical rider total is larger or smaller than the sum of currently itemized rider components "
                f"for dates like {', '.join(unexplained_dates[:5])}"
                + (" ..." if len(unexplained_dates) > 5 else "")
                + "."
            )

        st.markdown("### Layered Rate Build-Up")
        base_rates = utility_rows[["effective_date", "base_cents_per_kwh"]].copy()
        base_rates["component"] = "0_Base Rate"
        base_rates["cents_per_kwh"] = base_rates["base_cents_per_kwh"]
        base_rates["source_kind"] = "canonical"
        base_rates["component_effective_date"] = base_rates["effective_date"]
        base_rates["coverage_status"] = "base"
        base_rates["component_class"] = "base"
        base_rates = base_rates.drop(columns=["base_cents_per_kwh"])

        active_components = aligned_components_df[
            [
                "effective_date",
                "component",
                "cents_per_kwh",
                "source_kind",
                "component_effective_date",
                "coverage_status",
                "component_class",
            ]
        ].copy()

        stacked_df = pd.concat([base_rates, active_components], ignore_index=True)
        stacked_df = stacked_df.sort_values(["effective_date", "component"])

        fig = px.area(
            stacked_df,
            x="effective_date",
            y="cents_per_kwh",
            color="component",
            line_shape="hv",
            title=f"{utility} layered rate build-up",
            labels={"effective_date": "Date", "cents_per_kwh": "Cents per kWh", "component": "Component"},
            hover_data=["source_kind", "component_effective_date", "coverage_status", "component_class"],
        )
        fig.update_layout(
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
                traceorder="normal"
            ),
            margin=dict(t=80)
        )
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("### Isolated Rider Contributions")
        active_components = active_components.sort_values(["effective_date", "component"])
        fig_riders = px.area(
            active_components,
            x="effective_date",
            y="cents_per_kwh",
            color="component",
            line_shape="hv",
            title=f"{utility} isolated rider breakdown",
            labels={"effective_date": "Date", "cents_per_kwh": "Cents per kWh", "component": "Component"},
            hover_data=["source_kind", "component_effective_date", "coverage_status", "component_class"],
        )
        fig_riders.update_layout(
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
                traceorder="normal"
            ),
            margin=dict(t=80)
        )
        st.plotly_chart(fig_riders, use_container_width=True)

        with st.expander("Component Alignment Audit", expanded=False):
            audit_df = (
                utility_rows[["effective_date", "rider_effective_date", "rider_cents_per_kwh", "rider_coverage_status", "rider_source_kind"]]
                .merge(
                    aligned_components_df.groupby("effective_date", as_index=False)["cents_per_kwh"]
                    .sum()
                    .rename(columns={"cents_per_kwh": "component_sum_cents_per_kwh"}),
                    on="effective_date",
                    how="left",
                )
                .fillna({"component_sum_cents_per_kwh": 0.0})
            )
            audit_df["unexplained_delta_cents_per_kwh"] = (
                audit_df["rider_cents_per_kwh"] - audit_df["component_sum_cents_per_kwh"]
            ).round(6)
            audit_df["effective_date"] = audit_df["effective_date"].dt.date
            audit_df["rider_effective_date"] = pd.to_datetime(
                audit_df["rider_effective_date"], errors="coerce"
            ).dt.date
            st.dataframe(audit_df, use_container_width=True, hide_index=True)

st.markdown("## Utility Comparison As Of Selected Date")
st.plotly_chart(
    as_of_utility_comparison_chart(
        filtered_df,
        as_of_date=as_of_date,
        value_mode=metric_config["driver_mode"],
        title="Utility comparison as of selected date",
    ),
    use_container_width=True,
)

st.markdown("## Data Confidence")
st.plotly_chart(
    confidence_timeline_chart(filtered_df, title="Coverage and provenance by effective date"),
    use_container_width=True,
)
st.plotly_chart(
    confidence_summary_chart(filtered_df, title="Coverage summary across filtered rows"),
    use_container_width=True,
)

with st.expander("Confidence Guide", expanded=False):
    st.markdown(
        """
        - `Direct rider match`: rider filing exists on the same effective date as the base rate.
        - `Prior rider carried forward`: no same-day rider filing; most recent prior rider snapshot is used.
        - `No rider coverage`: no rider value available for the billing point.
        - `Directly parsed`: taken from rider-summary sheets like DEP Leaf 600 or DEC Leaf 99.
        - `Reconstructed`: inferred from older rider-specific filings and treated as lower-confidence than direct rider summaries.
        """
    )

st.markdown("## Rider Trust Quality")

trust_df = _load_trust_table(str(database_path))

if trust_df.empty:
    st.info("No rider trust data available.")
else:
    tier_counts = trust_df["trust_tier"].value_counts()
    total = len(trust_df)
    trust_kpi_cols = st.columns(4)
    for col, tier in zip(trust_kpi_cols, ["high", "medium", "low", "unverified"]):
        count = int(tier_counts.get(tier, 0))
        pct = count / total * 100 if total else 0
        color = _TRUST_TIER_COLORS[tier]
        col.markdown(
            f"<div style='border-left:4px solid {color}; padding-left:8px'>"
            f"<b>{tier.capitalize()}</b><br>{count} rows ({pct:.0f}%)"
            "</div>",
            unsafe_allow_html=True,
        )

    st.caption(
        f"Trust table covers {total} (utility, rate_class_group, rider_code, effective_date) rows "
        f"across {trust_df['rate_class_group'].nunique()} rate class groups and "
        f"{trust_df['rider_code'].nunique()} unique rider codes."
    )

    trust_group_filter = st.multiselect(
        "Filter by rate class group",
        options=sorted(trust_df["rate_class_group"].unique()),
        default=sorted(trust_df["rate_class_group"].unique()),
        format_func=lambda v: _RATE_CLASS_GROUP_LABELS.get(v, v),
        key="trust_group_filter",
    )
    trust_tier_filter = st.multiselect(
        "Filter by trust tier",
        options=["high", "medium", "low", "unverified"],
        default=["high", "medium", "low", "unverified"],
        key="trust_tier_filter",
    )

    filtered_trust = trust_df[
        trust_df["rate_class_group"].isin(trust_group_filter)
        & trust_df["trust_tier"].isin(trust_tier_filter)
    ].copy()
    filtered_trust["rate_class_group_label"] = filtered_trust["rate_class_group"].map(
        _RATE_CLASS_GROUP_LABELS
    )

    with st.expander("Trust score detail by rider", expanded=False):
        summary_cols = [
            "utility", "rate_class_group_label", "rider_code",
            "source_score", "date_score", "bill_score", "continuity_score",
            "trust_score", "trust_tier",
        ]
        available_cols = [c for c in summary_cols if c in filtered_trust.columns]
        mean_trust = (
            filtered_trust.groupby(["utility", "rate_class_group", "rate_class_group_label", "rider_code", "trust_tier"])[
                ["source_score", "date_score", "bill_score", "continuity_score", "trust_score"]
            ]
            .mean()
            .round(3)
            .reset_index()
            .sort_values(["utility", "rate_class_group", "trust_score"], ascending=[True, True, False])
        )
        display_cols = [c for c in summary_cols if c in mean_trust.columns]
        st.dataframe(
            mean_trust[display_cols].rename(columns={"rate_class_group_label": "rate_class_group"}),
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("Trust tier counts by rate class group", expanded=False):
        pivot = (
            filtered_trust.groupby(["rate_class_group", "trust_tier"])
            .size()
            .unstack(fill_value=0)
            .rename(index=_RATE_CLASS_GROUP_LABELS)
        )
        st.dataframe(pivot, use_container_width=True)

    with st.expander("Confidence Guide — Trust Scoring Model", expanded=False):
        st.markdown(
            """
            Each row in the trust table is scored 0.0–1.0 across four dimensions:

            | Dimension | Max | Basis |
            |---|---|---|
            | Source quality | 0.40 | `clean_leaf600` = 0.40; `provisional_ingest` = 0.20 |
            | Date completeness | 0.25 | `rider_effective_date` populated = 0.25 |
            | Bill support | 0.25 | Rider code appears in a validated bill block = 0.25 |
            | Continuity | 0.10 | No gap > 6 months in rider timeline = 0.10 |

            **Tiers:** high ≥ 0.80 · medium ≥ 0.50 · low ≥ 0.25 · unverified < 0.25

            **Rate class groups:** Each group is scored independently so that commercial riders
            (e.g. BA-EE in SGS) do not affect residential trust scores for the same code.
            """
        )


st.markdown("## Audit / Source-Backed Data")
default_table_columns = [
    "utility",
    "schedule",
    "effective_date",
    "rider_effective_date",
    "base_cents_per_kwh",
    "rider_cents_per_kwh",
    "all_in_cents_per_kwh",
    "base_bill_amount",
    "all_in_bill_amount",
    "coverage_label",
    "source_label",
    "confidence_label",
    "rider_quality_flag",
    "source_pdf",
    "rider_source_pdf",
]
available_table_columns = [column for column in default_table_columns if column in filtered_df.columns]
selected_table_columns = st.multiselect(
    "Visible table columns",
    options=list(filtered_df.columns),
    default=available_table_columns,
)

display_df = filtered_df.copy()
display_df["effective_date"] = display_df["effective_date"].dt.date
if "rider_effective_date" in display_df.columns:
    display_df["rider_effective_date"] = pd.to_datetime(display_df["rider_effective_date"], errors="coerce").dt.date
st.dataframe(display_df[selected_table_columns], use_container_width=True)
