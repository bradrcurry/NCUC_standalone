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
from duke_rates.analytics.rider_trust import load_rider_trust_table
from duke_rates.charts import (
    as_of_utility_comparison_chart,
    combined_utility_metric_chart,
    confidence_summary_chart,
    confidence_timeline_chart,
    utility_driver_chart,
)


CANONICAL_OUTPUT_DIR = ROOT / "data/processed/canonical_residential"
CANONICAL_CSV_PATH = CANONICAL_OUTPUT_DIR / "canonical_residential_timeline.csv"

UTILITY_LABELS = {
    "DEP": "DEP (Duke Energy Progress, RES)",
    "DEC": "DEC (Duke Energy Carolinas, RS)",
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


def render():

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
        return

    dashboard_df = _prepare_dashboard_df(canonical_df)
    snapshot_kwh = (
        int(float(dashboard_df["representative_kwh"].dropna().iloc[0]))
        if "representative_kwh" in dashboard_df.columns and not dashboard_df["representative_kwh"].dropna().empty
        else None
    )

    min_date = dashboard_df["effective_date"].min().date()
    max_date = dashboard_df["effective_date"].max().date()

    selected_utilities = st.sidebar.multiselect(
        "Utilities",
        options=sorted(dashboard_df["utility"].unique().tolist()),
        default=sorted(dashboard_df["utility"].unique().tolist()),
        format_func=lambda value: UTILITY_LABELS.get(value, value),
    )
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
        return

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
    st.caption("DEP RES vs DEC RS (2016–2026) — all-in rates, rider contribution, and data confidence")

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

    st.markdown("## Rate Timeline")
    show_base_overlay = st.toggle("Show base rate overlay", value=True)
    st.plotly_chart(
        combined_utility_metric_chart(
            filtered_df,
            metric_column=metric_config["metric_column"],
            title=f"{metric_choice} over time",
            yaxis_title=metric_config["yaxis_title"],
            show_base_overlay=show_base_overlay,
        ),
        use_container_width=True,
    )

    st.markdown("## Base vs Riders")
    driver_tabs = st.tabs(["DEP", "DEC"])
    for tab, utility in zip(driver_tabs, ["DEP", "DEC"], strict=True):
        with tab:
            utility_rows = filtered_df[filtered_df["utility"] == utility]
            if utility_rows.empty:
                st.info(f"No rows available for {utility} under current filters.")
            else:
                st.plotly_chart(
                    utility_driver_chart(
                        filtered_df,
                        utility=utility,
                        title=f"{utility} driver decomposition",
                        value_mode=metric_config["driver_mode"],
                    ),
                    use_container_width=True,
                )

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
