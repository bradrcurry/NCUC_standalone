from __future__ import annotations

from typing import Any

UTILITY_COLORS = {
    "DEP": "#0F766E",
    "DEC": "#B45309",
}

CONFIDENCE_COLORS = {
    "same_day": "#2F855A",
    "carried_forward": "#DD6B20",
    "uncovered": "#C53030",
}

CONFIDENCE_LABELS = {
    "same_day": "Direct rider match",
    "carried_forward": "Prior rider carried forward",
    "uncovered": "No rider coverage",
}

SOURCE_LABELS = {
    "clean": "Directly parsed",
    "provisional": "Reconstructed",
}


def _require_pandas():
    try:
        import pandas as pd
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "pandas is required for duke_rates.charts.plotly. Install it with `pip install pandas`."
        ) from exc
    return pd


def _require_plotly():
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "plotly is required for duke_rates.charts.plotly. Install it with `pip install plotly`."
        ) from exc
    return go, make_subplots


def rate_history_chart(df, events_df=None, filters: dict[str, Any] | None = None):
    pd = _require_pandas()
    go, _ = _require_plotly()

    filters = filters or {}
    data = df.copy()
    if "start_date" in filters:
        data = data[data["effective_date"] >= pd.to_datetime(filters["start_date"])]
    if "end_date" in filters:
        data = data[data["effective_date"] <= pd.to_datetime(filters["end_date"])]

    default_columns = [
        "summer_base_cents_per_kwh",
        "winter_base_cents_per_kwh",
        "blended_base_cents_per_kwh",
        "total_rider_cents_per_kwh",
        "blended_all_in_cents_per_kwh",
    ]
    series_columns = filters.get(
        "columns",
        [column for column in default_columns if column in data.columns],
    )
    title = filters.get("title", "DEP RES Rate History")
    figure = go.Figure()
    status_colors = {
        "same_day": "#2f855a",
        "carried_forward": "#dd6b20",
        "uncovered": "#c53030",
    }

    for column in series_columns:
        figure.add_trace(
            go.Scatter(
                x=data["effective_date"],
                y=data[column],
                mode="lines+markers",
                name=column.replace("_", " ").title(),
            )
        )

    if {
        "rider_coverage_status",
        "total_rider_cents_per_kwh",
        "rider_effective_date",
    }.issubset(data.columns):
        coverage_points = data[data["total_rider_cents_per_kwh"].notna()].copy()
        for status in ["same_day", "carried_forward", "uncovered"]:
            subset = coverage_points[coverage_points["rider_coverage_status"] == status]
            if subset.empty:
                continue
            hover_text = [
                (
                    f"Base date: {base_date:%Y-%m-%d}<br>"
                    f"Rider date: {rider_date:%Y-%m-%d}<br>"
                    f"Rider source: {source_kind}<br>"
                    f"Coverage: {status_label.replace('_', ' ')}"
                )
                for base_date, rider_date, source_kind, status_label in zip(
                    subset["effective_date"],
                    subset["rider_effective_date"],
                    subset.get("rider_source_kind", ["unknown"] * len(subset)),
                    subset["rider_coverage_status"],
                    strict=True,
                )
            ]
            figure.add_trace(
                go.Scatter(
                    x=subset["effective_date"],
                    y=subset["total_rider_cents_per_kwh"],
                    mode="markers",
                    name=f"Rider coverage: {status.replace('_', ' ')}",
                    marker={
                        "size": 10,
                        "color": status_colors[status],
                        "symbol": "circle" if status == "same_day" else "diamond",
                        "line": {"width": 1, "color": "white"},
                    },
                    hovertemplate="%{text}<extra></extra>",
                    text=hover_text,
                )
            )

    if events_df is not None and not events_df.empty:
        for _, event in events_df.iterrows():
            event_date = pd.to_datetime(event["event_date"])
            label = event.get("label", "")
            figure.add_vline(x=event_date, line_dash="dot", line_color="#777")
            if label:
                figure.add_annotation(
                    x=event_date,
                    y=1,
                    yref="paper",
                    text=label,
                    showarrow=False,
                    xanchor="left",
                    yanchor="bottom",
                    font={"size": 11},
                )

    figure.update_layout(
        title=title,
        xaxis_title="Effective Date",
        yaxis_title="Cents per kWh",
        hovermode="x unified",
        template="plotly_white",
        legend_title_text="Series",
    )
    return figure


def rider_stack_chart(df, utility: str, schedule: str):
    pd = _require_pandas()
    go, _ = _require_plotly()

    if df.empty:
        return go.Figure()

    data = df.copy()
    data["effective_date"] = pd.to_datetime(data["effective_date"])
    pivoted = (
        data.pivot_table(
            index="effective_date",
            columns="rider_code",
            values="cents_per_kwh",
            aggfunc="sum",
            fill_value=0.0,
        )
        .sort_index()
        .reset_index()
    )

    figure = go.Figure()
    for rider_code in [column for column in pivoted.columns if column != "effective_date"]:
        figure.add_trace(
            go.Scatter(
                x=pivoted["effective_date"],
                y=pivoted[rider_code],
                stackgroup="riders",
                mode="lines",
                name=rider_code,
            )
        )

    figure.update_layout(
        title=f"{utility} {schedule} Rider Stack",
        xaxis_title="Effective Date",
        yaxis_title="Cents per kWh",
        hovermode="x unified",
        template="plotly_white",
        legend_title_text="Rider",
    )
    return figure


def regional_comparison_chart(df, as_of_date):
    pd = _require_pandas()
    go, _ = _require_plotly()

    data = df.copy()
    data["effective_date"] = pd.to_datetime(data["effective_date"])
    cutoff = pd.to_datetime(as_of_date)
    data = data[data["effective_date"] <= cutoff].sort_values("effective_date")
    latest = data.groupby("utility", as_index=False).tail(1).copy()
    status_colors = {
        "same_day": "#2f855a",
        "carried_forward": "#dd6b20",
        "uncovered": "#c53030",
    }
    latest["bar_color"] = latest.get("rider_coverage_status", "uncovered").map(status_colors).fillna("#4a5568")
    hover_text = [
        (
            f"Utility: {utility}<br>"
            f"Effective date: {effective_date:%Y-%m-%d}<br>"
            f"Metric: {metric_name}<br>"
            f"Value: {metric_value:.4f}<br>"
            f"Bill coverage: {bill_status}<br>"
            f"Rider coverage: {rider_status}<br>"
            f"Rider effective date: {rider_effective_date:%Y-%m-%d}"
            if pd.notna(rider_effective_date)
            else
            f"Utility: {utility}<br>"
            f"Effective date: {effective_date:%Y-%m-%d}<br>"
            f"Metric: {metric_name}<br>"
            f"Value: {metric_value:.4f}<br>"
            f"Bill coverage: {bill_status}<br>"
            f"Rider coverage: {rider_status}"
        )
        for utility, effective_date, metric_name, metric_value, bill_status, rider_status, rider_effective_date in zip(
            latest["utility"],
            latest["effective_date"],
            latest["metric_name"],
            latest["metric_value"],
            latest.get("bill_coverage_status", ["unknown"] * len(latest)),
            latest.get("rider_coverage_status", ["unknown"] * len(latest)),
            latest.get("rider_effective_date", [pd.NaT] * len(latest)),
            strict=True,
        )
    ]

    figure = go.Figure(
        data=[
            go.Bar(
                x=latest["utility"],
                y=latest["metric_value"],
                text=latest["metric_value"].round(3),
                textposition="outside",
                marker={"color": latest["bar_color"]},
                customdata=hover_text,
                hovertemplate="%{customdata}<extra></extra>",
            )
        ]
    )
    metric_name = latest["metric_name"].iloc[0] if not latest.empty and "metric_name" in latest else "Metric"
    figure.update_layout(
        title=f"Regional Comparison as of {cutoff.date()}",
        xaxis_title="Utility",
        yaxis_title=metric_name,
        template="plotly_white",
    )
    return figure


def combined_utility_metric_chart(
    df,
    *,
    metric_column: str,
    title: str,
    yaxis_title: str,
    show_base_overlay: bool = False,
):
    pd = _require_pandas()
    go, _ = _require_plotly()

    data = df.copy()
    if data.empty:
        return go.Figure()

    data["effective_date"] = pd.to_datetime(data["effective_date"])
    figure = go.Figure()

    for utility in data["utility"].dropna().unique():
        utility_df = data[data["utility"] == utility].sort_values("effective_date").copy()
        color = UTILITY_COLORS.get(utility, "#4A5568")
        customdata = _build_metric_hover_customdata(utility_df, metric_column=metric_column)
        figure.add_trace(
            go.Scatter(
                x=utility_df["effective_date"],
                y=utility_df[metric_column],
                mode="lines",
                name=f"{utility} {_metric_label(metric_column)}",
                line={"width": 3, "color": color},
                customdata=customdata,
                hovertemplate=_metric_hover_template(yaxis_title),
            )
        )

        if show_base_overlay:
            base_overlay_column = _base_overlay_column(metric_column)
            if base_overlay_column and base_overlay_column in utility_df.columns:
                figure.add_trace(
                    go.Scatter(
                        x=utility_df["effective_date"],
                        y=utility_df[base_overlay_column],
                        mode="lines",
                        name=f"{utility} base",
                        line={"width": 2, "color": color, "dash": "dash"},
                        opacity=0.7,
                        customdata=customdata,
                        hovertemplate=_metric_hover_template(yaxis_title),
                    )
                )

    figure.update_layout(
        title=title,
        xaxis_title="Effective Date",
        yaxis_title=yaxis_title,
        hovermode="x unified",
        template="plotly_white",
        legend_title_text="Series",
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
    )
    return figure


def utility_driver_chart(
    df,
    *,
    utility: str,
    title: str,
    value_mode: str = "cents",
):
    pd = _require_pandas()
    go, _ = _require_plotly()

    data = df[df["utility"] == utility].copy()
    if data.empty:
        return go.Figure()

    data["effective_date"] = pd.to_datetime(data["effective_date"])
    base_column, rider_column, yaxis_title = _driver_columns(value_mode)
    color = UTILITY_COLORS.get(utility, "#4A5568")

    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=data["effective_date"],
            y=data[base_column],
            name="Base",
            marker={"color": color},
        )
    )
    figure.add_trace(
        go.Bar(
            x=data["effective_date"],
            y=data[rider_column],
            name="Riders",
            marker={"color": _lighten_hex(color)},
        )
    )
    figure.update_layout(
        title=title,
        xaxis_title="Effective Date",
        yaxis_title=yaxis_title,
        barmode="stack",
        hovermode="x unified",
        template="plotly_white",
        legend_title_text="Driver",
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
    )
    return figure


def as_of_utility_comparison_chart(
    df,
    *,
    as_of_date,
    value_mode: str = "cents",
    title: str,
):
    pd = _require_pandas()
    go, _ = _require_plotly()

    data = df.copy()
    if data.empty:
        return go.Figure()

    data["effective_date"] = pd.to_datetime(data["effective_date"])
    cutoff = pd.to_datetime(as_of_date)
    latest = data[data["effective_date"] <= cutoff].sort_values("effective_date").groupby("utility", as_index=False).tail(1)
    if latest.empty:
        return go.Figure()

    base_column, rider_column, all_in_column, yaxis_title = _as_of_columns(value_mode)
    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=latest["utility"],
            y=latest[base_column],
            name="Base",
            marker={"color": "#718096"},
        )
    )
    figure.add_trace(
        go.Bar(
            x=latest["utility"],
            y=latest[rider_column],
            name="Riders",
            marker={"color": "#D69E2E"},
        )
    )
    figure.add_trace(
        go.Bar(
            x=latest["utility"],
            y=latest[all_in_column],
            name="All-in",
            marker={"color": [UTILITY_COLORS.get(value, "#4A5568") for value in latest["utility"]]},
        )
    )
    figure.update_layout(
        title=title,
        xaxis_title="Utility",
        yaxis_title=yaxis_title,
        barmode="group",
        template="plotly_white",
        legend_title_text="Component",
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
    )
    return figure


def confidence_timeline_chart(
    df,
    *,
    title: str = "Data Confidence Timeline",
):
    pd = _require_pandas()
    go, _ = _require_plotly()

    data = df.copy()
    if data.empty:
        return go.Figure()

    data["effective_date"] = pd.to_datetime(data["effective_date"])
    data["coverage_label"] = data["rider_coverage_status"].map(CONFIDENCE_LABELS).fillna("Unknown")
    data["source_label"] = data["rider_source_kind"].map(SOURCE_LABELS).fillna("Unknown")
    utility_positions = {utility: idx for idx, utility in enumerate(sorted(data["utility"].dropna().unique()), start=1)}
    data["utility_position"] = data["utility"].map(utility_positions)

    figure = go.Figure()
    for status in ["same_day", "carried_forward", "uncovered"]:
        subset = data[data["rider_coverage_status"] == status].copy()
        if subset.empty:
            continue
        figure.add_trace(
            go.Scatter(
                x=subset["effective_date"],
                y=subset["utility_position"],
                mode="markers",
                name=CONFIDENCE_LABELS.get(status, status),
                marker={
                    "size": 14,
                    "color": CONFIDENCE_COLORS.get(status, "#4A5568"),
                    "symbol": subset["rider_source_kind"].map({"clean": "square", "provisional": "diamond"}).fillna("circle"),
                    "line": {"width": 1, "color": "white"},
                },
                customdata=list(
                    zip(
                        subset["utility"],
                        subset["effective_date"].dt.strftime("%Y-%m-%d"),
                        subset["coverage_label"],
                        subset["source_label"],
                    )
                ),
                hovertemplate=(
                    "Utility: %{customdata[0]}<br>"
                    "Effective date: %{customdata[1]}<br>"
                    "Coverage: %{customdata[2]}<br>"
                    "Source: %{customdata[3]}<extra></extra>"
                ),
            )
        )

    figure.update_layout(
        title=title,
        xaxis_title="Effective Date",
        yaxis={
            "title": "Utility",
            "tickmode": "array",
            "tickvals": list(utility_positions.values()),
            "ticktext": list(utility_positions.keys()),
        },
        template="plotly_white",
        legend_title_text="Confidence",
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
        height=260,
    )
    return figure


def confidence_summary_chart(df, *, title: str = "Coverage Summary"):
    pd = _require_pandas()
    go, _ = _require_plotly()

    data = df.copy()
    if data.empty:
        return go.Figure()

    summary = (
        data.groupby(["utility", "rider_coverage_status"])
        .size()
        .rename("count")
        .reset_index()
    )
    total_by_utility = summary.groupby("utility")["count"].transform("sum")
    summary["share"] = summary["count"] / total_by_utility

    figure = go.Figure()
    for status in ["same_day", "carried_forward", "uncovered"]:
        subset = summary[summary["rider_coverage_status"] == status]
        if subset.empty:
            continue
        figure.add_trace(
            go.Bar(
                x=subset["utility"],
                y=subset["share"],
                name=CONFIDENCE_LABELS.get(status, status),
                marker={"color": CONFIDENCE_COLORS.get(status, "#4A5568")},
                text=(subset["share"] * 100).round(1).astype(str) + "%",
                textposition="inside",
            )
        )
    figure.update_layout(
        title=title,
        xaxis_title="Utility",
        yaxis_title="Share of filtered rows",
        yaxis_tickformat=".0%",
        barmode="stack",
        template="plotly_white",
        legend_title_text="Coverage",
        margin={"l": 40, "r": 20, "t": 60, "b": 40},
        height=260,
    )
    return figure


def _driver_columns(value_mode: str) -> tuple[str, str, str]:
    if value_mode == "bill":
        return "base_bill_amount", "rider_bill_amount", "Bill amount ($)"
    return "base_cents_per_kwh", "rider_cents_per_kwh", "Cents per kWh"


def _as_of_columns(value_mode: str) -> tuple[str, str, str, str]:
    if value_mode == "bill":
        return "base_bill_amount", "rider_bill_amount", "all_in_bill_amount", "Bill amount ($)"
    return "base_cents_per_kwh", "rider_cents_per_kwh", "all_in_cents_per_kwh", "Cents per kWh"


def _base_overlay_column(metric_column: str) -> str | None:
    mapping = {
        "all_in_cents_per_kwh": "base_cents_per_kwh",
        "all_in_bill_amount": "base_bill_amount",
    }
    return mapping.get(metric_column)


def _metric_label(metric_column: str) -> str:
    return {
        "all_in_cents_per_kwh": "all-in",
        "base_cents_per_kwh": "base",
        "rider_cents_per_kwh": "riders",
        "all_in_bill_amount": "all-in bill",
        "base_bill_amount": "base bill",
        "rider_bill_amount": "rider amount",
    }.get(metric_column, metric_column.replace("_", " "))


def _metric_hover_template(yaxis_title: str) -> str:
    return (
        "Utility: %{customdata[0]}<br>"
        "Effective date: %{x|%Y-%m-%d}<br>"
        f"Selected metric: %{{y:.4f}} {yaxis_title}<br>"
        "Base: %{customdata[1]}<br>"
        "Riders: %{customdata[2]}<br>"
        "All-in: %{customdata[3]}<br>"
        "Fixed charge: %{customdata[4]}<br>"
        "Coverage: %{customdata[5]}<br>"
        "Confidence: %{customdata[6]}<extra></extra>"
    )


def _build_metric_hover_customdata(df, *, metric_column: str):
    pd = _require_pandas()
    bill_mode = metric_column.endswith("_bill_amount")
    base_column = "base_bill_amount" if bill_mode else "base_cents_per_kwh"
    rider_column = "rider_bill_amount" if bill_mode else "rider_cents_per_kwh"
    all_in_column = "all_in_bill_amount" if bill_mode else "all_in_cents_per_kwh"
    suffix = " $" if bill_mode else " c/kWh"

    def _fmt(value: Any, suffix: str = "") -> str:
        if pd.isna(value):
            return "N/A"
        return f"{float(value):.4f}{suffix}"

    return list(
        zip(
            df["utility"],
            df.get(base_column, pd.Series([None] * len(df))).map(lambda value: _fmt(value, suffix)),
            df.get(rider_column, pd.Series([None] * len(df))).map(lambda value: _fmt(value, suffix)),
            df.get(all_in_column, pd.Series([None] * len(df))).map(lambda value: _fmt(value, suffix)),
            df.get("fixed_monthly_charge", pd.Series([None] * len(df))).map(lambda value: _fmt(value, " $/mo")),
            df.get("rider_coverage_status", pd.Series(["unknown"] * len(df))).map(lambda value: CONFIDENCE_LABELS.get(value, value)),
            df.get("rider_source_kind", pd.Series(["unknown"] * len(df))).map(lambda value: SOURCE_LABELS.get(value, value)),
        )
    )


def _lighten_hex(color: str) -> str:
    color = color.lstrip("#")
    if len(color) != 6:
        return "#CBD5E0"
    r = int(color[0:2], 16)
    g = int(color[2:4], 16)
    b = int(color[4:6], 16)
    r = min(255, int(r + (255 - r) * 0.45))
    g = min(255, int(g + (255 - g) * 0.45))
    b = min(255, int(b + (255 - b) * 0.45))
    return f"#{r:02X}{g:02X}{b:02X}"


def bill_waterfall_chart(df, period_a, period_b):
    pd = _require_pandas()
    go, _ = _require_plotly()

    data = df.copy()
    a = data[data["period"] == period_a].set_index("category")["amount"]
    b = data[data["period"] == period_b].set_index("category")["amount"]
    categories = sorted(set(a.index) | set(b.index))

    deltas = []
    for category in categories:
        deltas.append((b.get(category, 0.0) - a.get(category, 0.0)))

    figure = go.Figure(
        go.Waterfall(
            name="delta",
            orientation="v",
            measure=["relative"] * len(categories),
            x=categories,
            y=deltas,
            connector={"line": {"color": "rgb(63, 63, 63)"}},
        )
    )
    figure.update_layout(
        title=f"Bill Waterfall: {period_a} to {period_b}",
        xaxis_title="Charge Category",
        yaxis_title="Amount Change ($)",
        template="plotly_white",
    )
    return figure
