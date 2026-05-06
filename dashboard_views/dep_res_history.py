from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pandas as pd
import streamlit as st

from duke_rates.analytics.dep_progress import load_dep_res_all_in_history
from duke_rates.charts import rate_history_chart, rider_stack_chart


@st.cache_data(show_spinner=False)
def _cached_all_in_history(database_path: str, representative_kwh: float):
    return load_dep_res_all_in_history(
        database_path=Path(database_path),
        representative_kwh=representative_kwh,
    )


def render():
    st.title("Duke Energy Progress RES History")

    database_path = ROOT / "data/db/duke_rates.db"
    representative_kwh = st.sidebar.number_input(
        "Representative kWh",
        min_value=100,
        max_value=5000,
        value=1000,
        step=100,
    )

    all_in_df, rider_totals_df, rider_components_df = _cached_all_in_history(
        str(database_path),
        float(representative_kwh),
    )

    coverage_counts = (
        all_in_df["rider_coverage_status"].value_counts().to_dict()
        if "rider_coverage_status" in all_in_df.columns
        else {}
    )

    metric_a, metric_b, metric_c = st.columns(3)
    metric_a.metric("Base history rows", len(all_in_df))
    metric_b.metric("Clean rider rows", len(rider_totals_df))
    metric_c.metric("Carried-forward rows", int(coverage_counts.get("carried_forward", 0)))

    if coverage_counts:
        st.caption(
            "Rider coverage status: "
            + ", ".join(f"{key}={value}" for key, value in sorted(coverage_counts.items()))
        )

    left, right = st.columns(2)
    with left:
        st.plotly_chart(
            rate_history_chart(
                all_in_df,
                filters={
                    "title": "Base, Rider, and All-In History",
                    "columns": [
                        "summer_base_cents_per_kwh",
                        "winter_base_cents_per_kwh",
                        "total_rider_cents_per_kwh",
                        "blended_all_in_cents_per_kwh",
                    ],
                },
            ),
            use_container_width=True,
        )

    with right:
        st.plotly_chart(
            rider_stack_chart(
                rider_components_df,
                utility="Duke Energy Progress",
                schedule="RES",
            ),
            use_container_width=True,
        )

    st.subheader("All-In Dataset")
    display_df = all_in_df.copy()
    display_df["effective_date"] = pd.to_datetime(display_df["effective_date"]).dt.date
    if "rider_effective_date" in display_df.columns:
        display_df["rider_effective_date"] = pd.to_datetime(
            display_df["rider_effective_date"]
        ).dt.date
    st.dataframe(display_df, use_container_width=True)
