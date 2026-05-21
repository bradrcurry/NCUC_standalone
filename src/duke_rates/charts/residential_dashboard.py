"""Plotly chart helpers for the consolidated residential dashboard.

Lives alongside ``charts/plotly.py``. Keeps the new dashboard's chart
construction separate so the original chart module can stay focused on
its existing consumers.
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go


# Color palette mapped to rider category so the same color means the same thing
# across the donut, the build-up area chart, and the comparison views.
CATEGORY_COLORS = {
    "base": "#1f4e79",
    "fuel": "#c0504d",
    "renewable": "#2e8b57",
    "efficiency": "#9bbb59",
    "tax": "#7030a0",
    "performance": "#4f81bd",
    "regulatory": "#a5a5a5",
    "capital": "#e8a33d",
    "affordability": "#17becf",
    "storm": "#bcbd22",
    "solar": "#3cb371",
    "residual": "#cccccc",
    "rider": "#8c564b",
}


def _category_color(category: str | None) -> str:
    if not category:
        return CATEGORY_COLORS["rider"]
    return CATEGORY_COLORS.get(category, CATEGORY_COLORS["rider"])


def rider_breakdown_donut(
    breakdown_df: pd.DataFrame,
    *,
    utility: str,
    monthly_kwh: float,
) -> go.Figure:
    """Donut chart of the latest residential bill: base + each rider in $.

    Credits (negative dollars) are shown as their absolute value but tagged
    in hover text so the polarity isn't lost.
    """
    if breakdown_df.empty:
        fig = go.Figure()
        fig.update_layout(title=f"{utility}: no breakdown data available")
        return fig

    df = breakdown_df.copy()
    df["abs_dollars"] = df["dollars"].abs()
    df = df[df["abs_dollars"] > 0].copy()
    if df.empty:
        fig = go.Figure()
        fig.update_layout(title=f"{utility}: no positive bill components to plot")
        return fig

    total_charges_signed = round(df["dollars"].sum(), 2)
    # Group very small slices (< 1% of the absolute pie) into a single "Other"
    # bucket so the chart labels don't pile on top of each other.
    abs_total = df["abs_dollars"].sum()
    threshold = max(abs_total * 0.01, 0.05)
    small = df[df["abs_dollars"] < threshold].copy()
    big = df[df["abs_dollars"] >= threshold].copy()
    if not small.empty:
        other_components = ", ".join(small["component"].tolist())
        other_row = {
            "component": "Other",
            "short_name": f"Other riders ({other_components})",
            "category": "rider",
            "cents_per_kwh": small["cents_per_kwh"].sum(),
            "dollars": small["dollars"].sum(),
            "abs_dollars": small["abs_dollars"].sum(),
        }
        big = pd.concat([big, pd.DataFrame([other_row])], ignore_index=True)
    df = big

    df["label"] = df.apply(
        lambda r: f"{r['component']} — {r['short_name']}"
        if r["component"] != r["short_name"]
        else r["component"],
        axis=1,
    )
    df["polarity"] = df["dollars"].apply(lambda v: "credit" if v < 0 else "charge")
    df["color"] = df["category"].apply(_category_color)

    # Plotly Pie expects customdata to be a 2D array shaped (n_slices, n_fields).
    # Passing a list of tuples renders blank in hover ("N/A"); using a numpy
    # array (or list-of-lists with proper shape) fixes it.
    customdata = df[["dollars", "cents_per_kwh", "category", "polarity"]].to_numpy()

    fig = go.Figure(
        data=[
            go.Pie(
                labels=df["label"],
                values=df["abs_dollars"],
                hole=0.55,
                marker=dict(colors=df["color"].tolist()),
                customdata=customdata,
                hovertemplate=(
                    "<b>%{label}</b><br>"
                    "$%{customdata[0]:.2f}/mo (%{customdata[3]})<br>"
                    "%{customdata[1]:.4f} ¢/kWh<br>"
                    "Category: %{customdata[2]}<extra></extra>"
                ),
                texttemplate="<b>%{label}</b><br>$%{customdata[0]:.2f} (%{percent})",
                textposition="outside",
                sort=False,
            )
        ]
    )
    total = total_charges_signed
    fig.update_layout(
        title=f"{utility} residential bill at {monthly_kwh:,.0f} kWh — est. ${total:,.2f}/mo (energy only)",
        annotations=[
            dict(
                text=f"<b>${total:,.0f}</b><br>per month",
                x=0.5,
                y=0.5,
                font=dict(size=18),
                showarrow=False,
            )
        ],
        height=460,
        margin=dict(t=70, b=20, l=20, r=20),
        showlegend=False,
    )
    return fig


def annotated_history_chart(
    timeline_df: pd.DataFrame,
    *,
    events_df: pd.DataFrame,
    utilities: list[str],
    monthly_kwh: float,
    show_eia: bool = True,
    eia_df: pd.DataFrame | None = None,
) -> go.Figure:
    """Long timeline of all-in ¢/kWh with event annotations."""
    fig = go.Figure()

    utility_colors = {"DEP": "#0f766e", "DEC": "#b45309"}
    for utility in utilities:
        sub = timeline_df[timeline_df["utility"] == utility].sort_values("effective_date")
        if sub.empty:
            continue
        color = utility_colors.get(utility, "#4a5568")
        fig.add_trace(
            go.Scatter(
                x=sub["effective_date"],
                y=sub["all_in_cents_per_kwh"],
                mode="lines+markers",
                name=f"{utility} all-in",
                line=dict(color=color, width=3, shape="hv"),
                marker=dict(size=6),
                hovertemplate=(
                    "<b>%{x|%b %Y}</b><br>"
                    f"{utility} all-in: %{{y:.3f}} ¢/kWh<extra></extra>"
                ),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=sub["effective_date"],
                y=sub["base_cents_per_kwh"],
                mode="lines",
                name=f"{utility} base",
                line=dict(color=color, width=1.5, dash="dot", shape="hv"),
                opacity=0.7,
                hovertemplate=(
                    "<b>%{x|%b %Y}</b><br>"
                    f"{utility} base: %{{y:.3f}} ¢/kWh<extra></extra>"
                ),
            )
        )

    if show_eia and eia_df is not None and not eia_df.empty:
        for state, dash, color, label in [
            ("NC", "dash", "#7f1d1d", "NC EIA avg"),
            ("US", "dot", "#1f2937", "US EIA avg"),
        ]:
            state_eia = eia_df[eia_df["state"] == state].sort_values("year")
            if state_eia.empty:
                continue
            xs: list[pd.Timestamp] = []
            ys: list[float] = []
            for _, row in state_eia.iterrows():
                year = int(row["year"])
                xs.extend(
                    [pd.Timestamp(year=year, month=1, day=1), pd.Timestamp(year=year, month=12, day=31)]
                )
                ys.extend([float(row["price_cents_per_kwh"])] * 2)
            fig.add_trace(
                go.Scatter(
                    x=xs,
                    y=ys,
                    mode="lines",
                    name=label,
                    line=dict(color=color, width=2, dash=dash),
                    opacity=0.65,
                    hovertemplate=f"<b>{label}</b>: %{{y:.2f}} ¢/kWh<extra></extra>",
                )
            )

    if timeline_df.empty:
        y_max = 20.0
    else:
        y_max = float(timeline_df["all_in_cents_per_kwh"].max()) * 1.15

    category_colors = {
        "renewable_policy": "#2e8b57",
        "carbon_policy": "#1f4e79",
        "fuel_event": "#c0504d",
        "tax_policy": "#7030a0",
        "rate_case": "#e8a33d",
    }
    for _, ev in events_df.iterrows():
        ev_date = pd.to_datetime(ev["effective_date"])
        color = category_colors.get(ev["impact_category"], "#666666")
        fig.add_vline(
            x=ev_date,
            line=dict(color=color, width=1, dash="dash"),
            opacity=0.5,
        )
        fig.add_annotation(
            x=ev_date,
            y=y_max,
            text=f"<b>{ev['bill_number']}</b>",
            showarrow=False,
            yanchor="bottom",
            font=dict(size=10, color=color),
            hovertext=f"{ev['short_title']}<br><br>{ev['summary']}",
        )

    fig.update_layout(
        title=f"DEP & DEC residential all-in rate — annotated with policy & market events",
        xaxis_title="Effective date",
        yaxis_title="¢/kWh",
        yaxis=dict(range=[0, y_max * 1.05]),
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        height=480,
        margin=dict(t=100, b=40),
    )
    return fig


def rider_buildup_area(
    components_df: pd.DataFrame,
    *,
    utility: str,
) -> go.Figure:
    """Stacked-area chart: rider components over time. Caller passes
    already-aligned components (one row per (effective_date, rider_code)).
    """
    if components_df.empty:
        fig = go.Figure()
        fig.update_layout(title=f"{utility}: no rider component history available")
        return fig
    df = components_df.copy()
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df = df.sort_values(["effective_date", "rider_code"])
    fig = px.area(
        df,
        x="effective_date",
        y="cents_per_kwh",
        color="rider_code",
        line_shape="hv",
        title=f"{utility} rider stack over time (¢/kWh)",
        labels={
            "effective_date": "Effective date",
            "cents_per_kwh": "¢/kWh",
            "rider_code": "Rider",
        },
    )
    fig.update_layout(
        height=420,
        margin=dict(t=70, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        template="plotly_white",
    )
    return fig
