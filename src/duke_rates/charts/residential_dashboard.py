"""Plotly chart helpers for the consolidated residential dashboard.

Lives alongside ``charts/plotly.py``. Keeps the new dashboard's chart
construction separate so the original chart module can stay focused on
its existing consumers.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# Color palette mapped to rider category with vibrant electric/neon colors
CATEGORY_COLORS = {
    "base": "#4facfe",         # Electric Blue
    "fuel": "#ff5a5f",         # Neon Coral/Red
    "renewable": "#00ffd0",    # Electric Mint/Green
    "efficiency": "#a8ff35",   # Bright Lime
    "tax": "#f355da",          # Electric Purple
    "performance": "#00c6ff",  # Bright Cyan
    "regulatory": "#94a3b8",   # Cool Slate
    "capital": "#ffd000",      # Neon Gold
    "affordability": "#38bdf8",# Sky Blue
    "storm": "#fb923c",        # Bright Orange
    "solar": "#10b981",        # Emerald Green
    "residual": "#475569",     # Slate
    "rider": "#ec4899",        # Neon Pink
}


def _hex_to_rgba(hex_str: str, alpha: float = 0.5) -> str:
    """Convert hex color string to rgba format."""
    h = hex_str.lstrip('#')
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r}, {g}, {b}, {alpha})"
    return hex_str


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
    customdata = df[["dollars", "cents_per_kwh", "category", "polarity", "label"]].to_numpy()

    fig = go.Figure(
        data=[
            go.Pie(
                labels=df["component"],
                values=df["abs_dollars"],
                hole=0.6,
                marker=dict(
                    colors=df["color"].tolist(),
                    line=dict(color="#0b0f19", width=2)
                ),
                customdata=customdata,
                hovertemplate=(
                    "<b>%{customdata[4]}</b><br>"
                    "$%{customdata[0]:.2f}/mo (%{customdata[3]})<br>"
                    "%{customdata[1]:.4f} ¢/kWh<br>"
                    "Category: %{customdata[2]}<extra></extra>"
                ),
                textinfo="percent",
                textposition="inside",
                sort=False,
            )
        ]
    )
    total = total_charges_signed
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#cbd5e1", family="Inter, sans-serif"),
        title=dict(
            text=f"{utility} Bill Breakdown at {monthly_kwh:,.0f} kWh — est. ${total:,.2f}/mo",
            font=dict(size=16, family="Plus Jakarta Sans, sans-serif", color="#f8fafc", weight="bold")
        ),
        annotations=[
            dict(
                text=f"<span style='color: #94a3b8; font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em;'>Est. Bill</span><br><b style='color: #f8fafc; font-size: 26px;'>${total:,.0f}</b><br><span style='color: #64748b; font-size: 11px;'>per month</span>",
                x=0.5,
                y=0.5,
                showarrow=False,
            )
        ],
        height=420,
        margin=dict(t=60, b=20, l=20, r=200),
        showlegend=True,
        legend=dict(
            orientation="v",
            yanchor="middle",
            y=0.5,
            xanchor="left",
            x=1.05,
            font=dict(size=11),
            bgcolor="rgba(0,0,0,0)",
        )
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
    interpolation: str = "spline",
) -> go.Figure:
    """Long timeline of all-in ¢/kWh with event annotations and fluid curves."""
    fig = go.Figure()

    # Electric glow palette for utilities
    utility_colors = {"DEP": "#00f2fe", "DEC": "#f355da"}
    for utility in utilities:
        sub = timeline_df[timeline_df["utility"] == utility].sort_values("effective_date")
        if sub.empty:
            continue
        color = utility_colors.get(utility, "#cbd5e1")
        
        # Dual-line glow effect if using spline (fluid mode)
        if interpolation == "spline":
            # 1. Broad translucent glow line
            fig.add_trace(
                go.Scatter(
                    x=sub["effective_date"],
                    y=sub["all_in_cents_per_kwh"],
                    mode="lines",
                    name=f"{utility} glow",
                    line=dict(color=color, width=8, shape="spline", smoothing=1.3),
                    opacity=0.15,
                    showlegend=False,
                    hoverinfo="skip"
                )
            )
            # 2. Translucent shaded area under the curve
            fig.add_trace(
                go.Scatter(
                    x=sub["effective_date"],
                    y=sub["all_in_cents_per_kwh"],
                    mode="lines",
                    line=dict(color="rgba(0,0,0,0)", shape="spline", smoothing=1.3),
                    fill="tozeroy",
                    fillcolor=_hex_to_rgba(color, 0.03),
                    showlegend=False,
                    hoverinfo="skip"
                )
            )

        # Main high-intensity line
        fig.add_trace(
            go.Scatter(
                x=sub["effective_date"],
                y=sub["all_in_cents_per_kwh"],
                mode="lines+markers",
                name=f"{utility} All-In",
                line=dict(color=color, width=3.5, shape=interpolation),
                marker=dict(size=6, symbol="circle", line=dict(color="#0b0f19", width=1.5)),
                hovertemplate=(
                    "<b>%{x|%b %Y}</b><br>"
                    f"{utility} all-in: %{{y:.3f}} ¢/kWh<extra></extra>"
                ),
            )
        )
        
        # Base rate reference line
        fig.add_trace(
            go.Scatter(
                x=sub["effective_date"],
                y=sub["base_cents_per_kwh"],
                mode="lines",
                name=f"{utility} Base Rate",
                line=dict(color=color, width=1.5, dash="dot", shape=interpolation),
                opacity=0.6,
                hovertemplate=(
                    "<b>%{x|%b %Y}</b><br>"
                    f"{utility} base: %{{y:.3f}} ¢/kWh<extra></extra>"
                ),
            )
        )

    # EIA comparisons (NC + US averages)
    if show_eia and eia_df is not None and not eia_df.empty:
        for state, dash, color, label in [
            ("NC", "dash", "#00ffd0", "NC State Avg (EIA)"), # neon mint
            ("US", "dot", "#ffd000", "US Nat'l Avg (EIA)"),  # neon gold
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
                    opacity=0.6,
                    hovertemplate=f"<b>{label}</b>: %{{y:.2f}} ¢/kWh<extra></extra>",
                )
            )

    if timeline_df.empty:
        y_max = 20.0
    else:
        y_max = float(timeline_df["all_in_cents_per_kwh"].max()) * 1.15

    # Glowing event vertical dividers
    category_colors = {
        "renewable_policy": "#00ffd0", # neon mint
        "carbon_policy": "#4facfe",    # electric blue
        "fuel_event": "#ff5a5f",       # neon coral
        "tax_policy": "#f355da",       # electric purple
        "rate_case": "#ffd000",        # neon gold
    }
    for _, ev in events_df.iterrows():
        ev_date = pd.to_datetime(ev["effective_date"])
        color = category_colors.get(ev["impact_category"], "#94a3b8")
        fig.add_vline(
            x=ev_date,
            line=dict(color=color, width=1, dash="dash"),
            opacity=0.4,
        )
        fig.add_annotation(
            x=ev_date,
            y=0.03,
            yref="paper",
            text=f"<b>{ev['bill_number']}</b>",
            showarrow=False,
            xanchor="left",
            yanchor="bottom",
            textangle=-90,
            xshift=4,
            font=dict(size=10, color=color, family="Plus Jakarta Sans"),
            hovertext=f"<b>{ev['short_title']}</b><br><br>{ev['summary']}",
            bgcolor="rgba(15, 23, 42, 0.8)",
            bordercolor=color,
            borderwidth=1,
            borderpad=3,
        )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#cbd5e1", family="Inter, sans-serif"),
        title=dict(
            text="DEP & DEC Residential Rate History vs. EIA Averages",
            font=dict(size=16, family="Plus Jakarta Sans, sans-serif", color="#f8fafc", weight="bold")
        ),
        xaxis=dict(
            title="Effective Date",
            gridcolor="rgba(255, 255, 255, 0.05)",
            zeroline=False,
            showgrid=True,
            linecolor="rgba(255, 255, 255, 0.1)",
        ),
        yaxis=dict(
            title="¢/kWh",
            range=[0, y_max * 1.05],
            gridcolor="rgba(255, 255, 255, 0.05)",
            zeroline=False,
            showgrid=True,
            linecolor="rgba(255, 255, 255, 0.1)",
        ),
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor="rgba(22, 28, 45, 0.95)",
            bordercolor="rgba(255, 255, 255, 0.1)",
            font=dict(color="#cbd5e1")
        ),
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.15,
            xanchor="center",
            x=0.5,
            bgcolor="rgba(0,0,0,0)",
        ),
        height=480,
        margin=dict(t=80, b=80, l=50, r=20),
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
    
    # Pivot and fill missing rider_code values with 0.0 to prevent NaN hovers on stacked area chart
    pivoted = df.pivot_table(
        index="effective_date",
        columns="rider_code",
        values="cents_per_kwh",
        aggfunc="sum",
        fill_value=0.0,
    )
    df = pivoted.reset_index().melt(
        id_vars="effective_date",
        value_vars=pivoted.columns,
        var_name="rider_code",
        value_name="cents_per_kwh",
    )
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


def all_in_rate_history_stack(
    components_df: pd.DataFrame,
    timeline_df: pd.DataFrame,
    *,
    utility: str,
    database_path: Path | None = None,
    interpolation: str = "spline",
) -> go.Figure:
    """Stacked-area chart of the ALL-IN rate over time: Base Rate + individual riders.

    Handles negative riders (credits) correctly by computing the cumulative sums
    ourselves and plotting them as overlapping area traces (Option A).
    """
    from duke_rates.analytics.residential_bill_breakdown import load_rider_glossary

    if components_df.empty or timeline_df.empty:
        fig = go.Figure()
        fig.update_layout(title=f"{utility}: no data available for all-in stack")
        return fig

    # 1. Clean and align data
    df_comp = components_df.copy()
    df_comp["effective_date"] = pd.to_datetime(df_comp["effective_date"])

    df_timeline = timeline_df[timeline_df["utility"] == utility].sort_values("effective_date").copy()
    df_timeline["effective_date"] = pd.to_datetime(df_timeline["effective_date"])

    if df_timeline.empty:
        fig = go.Figure()
        fig.update_layout(title=f"{utility}: no timeline data for base rates")
        return fig

    df_comp = df_comp.sort_values("effective_date")

    # 2. Reconstruct the grid of all active rates for each date
    all_riders = df_comp["rider_code"].unique()
    rider_dates = df_comp["effective_date"].unique()

    rider_rows = []
    for d in rider_dates:
        sub = df_comp[df_comp["effective_date"] == d]
        active_riders = set(sub["rider_code"])
        for r in all_riders:
            if r in active_riders:
                val = float(sub[sub["rider_code"] == r]["cents_per_kwh"].iloc[0])
            else:
                val = 0.0
            rider_rows.append({
                "effective_date": d,
                "component": r,
                "cents_per_kwh": val
            })

    df_riders_clean = pd.DataFrame(rider_rows)

    base_rows = []
    for _, row in df_timeline.iterrows():
        base_rows.append({
            "effective_date": row["effective_date"],
            "component": "Base Rate",
            "cents_per_kwh": float(row["base_cents_per_kwh"] or 0.0)
        })
    df_base = pd.DataFrame(base_rows)

    df_all_filings = pd.concat([df_riders_clean, df_base], ignore_index=True)

    pivot_df = df_all_filings.pivot_table(
        index="effective_date",
        columns="component",
        values="cents_per_kwh",
        aggfunc="last"
    )

    unique_dates = sorted(list(set(df_timeline["effective_date"]).union(set(df_comp["effective_date"]))))
    pivot_df = pivot_df.reindex(unique_dates)
    pivot_df = pivot_df.ffill().fillna(0.0)

    # 3. Classify and order columns: Base Rate -> Negative Riders -> Positive Riders
    other_cols = [c for c in pivot_df.columns if c != "Base Rate"]
    positive_riders = []
    negative_riders = []

    for col in other_cols:
        col_sum = pivot_df[col].sum()
        if col_sum >= 0:
            positive_riders.append(col)
        else:
            negative_riders.append(col)

    positive_riders = sorted(positive_riders)
    negative_riders = sorted(negative_riders)

    ordered_cols = ["Base Rate"] + negative_riders + positive_riders
    pivot_df = pivot_df[ordered_cols]

    # Compute cumulative values
    cum_df = pivot_df.cumsum(axis=1)

    # 4. Load glossary for names and categories
    glossary = load_rider_glossary(database_path=database_path)
    code_to_category = {}
    code_to_name = {}
    if not glossary.empty:
        code_to_category = glossary.drop_duplicates("rider_code").set_index("rider_code")["category"].to_dict()
        code_to_name = glossary.drop_duplicates("rider_code").set_index("rider_code")["short_name"].to_dict()

    def get_color(col):
        if col == "Base Rate":
            return CATEGORY_COLORS["base"]
        cat = code_to_category.get(col, "rider")
        return CATEGORY_COLORS.get(cat, CATEGORY_COLORS["rider"])

    def get_name(col):
        if col == "Base Rate":
            return "Base Rate"
        name = code_to_name.get(col, col)
        if name != col:
            return f"{col} — {name}"
        return col

    # 5. Create Plotly traces
    fig = go.Figure()

    # Trace 1: Base Rate
    fig.add_trace(
        go.Scatter(
            x=pivot_df.index,
            y=cum_df["Base Rate"],
            mode="lines",
            line=dict(width=0.8, shape=interpolation, color=get_color("Base Rate")),
            fill="tozeroy",
            fillcolor=_hex_to_rgba(get_color("Base Rate"), 0.3),
            name="Base Rate",
            customdata=pivot_df["Base Rate"],
            hovertemplate=(
                "<b>Base Rate</b><br>"
                "Rate: %{customdata:.4f} ¢/kWh<br>"
                "Cumulative: %{y:.4f} ¢/kWh<extra></extra>"
            ),
        )
    )

    # Subsequent traces
    for i in range(1, len(ordered_cols)):
        col = ordered_cols[i]
        fig.add_trace(
            go.Scatter(
                x=pivot_df.index,
                y=cum_df[col],
                mode="lines",
                line=dict(width=0.8, shape=interpolation, color=get_color(col)),
                fill="tonexty",
                fillcolor=_hex_to_rgba(get_color(col), 0.35),
                name=get_name(col),
                customdata=pivot_df[col],
                hovertemplate=(
                    f"<b>{get_name(col)}</b><br>"
                    "Rate: %{customdata:+.4f} ¢/kWh<br>"
                    "Cumulative: %{y:.4f} ¢/kWh<extra></extra>"
                ),
            )
        )

    # Add an overall All-In line trace on top for high-contrast visibility
    top_color = "#00f2fe" if utility == "DEP" else "#f355da"
    if interpolation == "spline":
        # Glowing shadow line
        fig.add_trace(
            go.Scatter(
                x=pivot_df.index,
                y=cum_df[ordered_cols[-1]],
                mode="lines",
                line=dict(color=top_color, width=6, shape="spline", smoothing=1.3),
                opacity=0.2,
                showlegend=False,
                hoverinfo="skip"
            )
        )

    fig.add_trace(
        go.Scatter(
            x=pivot_df.index,
            y=cum_df[ordered_cols[-1]],
            mode="lines",
            line=dict(color=top_color, width=2.5, shape=interpolation),
            name="All-In Rate Total",
            hovertemplate="<b>All-In Rate Total</b>: %{y:.3f} ¢/kWh<extra></extra>"
        )
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#cbd5e1", family="Inter, sans-serif"),
        title=dict(
            text=f"{utility} Residential All-In Rate Buildup Over Time (¢/kWh)",
            font=dict(size=16, family="Plus Jakarta Sans, sans-serif", color="#f8fafc", weight="bold")
        ),
        xaxis=dict(
            title="Effective Date",
            gridcolor="rgba(255, 255, 255, 0.05)",
            zeroline=False,
            showgrid=True,
            linecolor="rgba(255, 255, 255, 0.1)",
        ),
        yaxis=dict(
            title="¢/kWh",
            gridcolor="rgba(255, 255, 255, 0.05)",
            zeroline=False,
            showgrid=True,
            linecolor="rgba(255, 255, 255, 0.1)",
        ),
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor="rgba(22, 28, 45, 0.95)",
            bordercolor="rgba(255, 255, 255, 0.1)",
            font=dict(color="#cbd5e1")
        ),
        height=480,
        margin=dict(t=80, b=40, l=50, r=40),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=10)
        )
    )

    return fig

