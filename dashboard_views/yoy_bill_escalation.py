import datetime
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data/db/duke_rates.db"

_STATE_COMPANY_OPTIONS = {
    "NC Progress (DEP)": ("NC", "progress"),
    "NC Carolinas (DEC)": ("NC", "carolinas"),
    "SC Progress": ("SC", "progress"),
    "SC Carolinas": ("SC", "carolinas"),
    "FL": ("FL", "florida"),
    "IN": ("IN", "indiana"),
    "KY": ("KY", "kentucky"),
    "OH": ("OH", "ohio"),
}

_GROUP_OPTIONS = {
    "Residential only": "residential",
    "Small General Service only": "sgs",
    "Medium General Service": "mgs",
    "Large General Service": "lgs",
}


@st.cache_resource(show_spinner=False)
def _get_engine(db_path: str):
    from duke_rates.billing.tariff_engine import TariffBillingEngine
    from duke_rates.db.repository import Repository
    repo = Repository(db_path)
    return repo, TariffBillingEngine(repo)


@st.cache_data(show_spinner=False)
def _get_eligible_families(db_path: str, state: str, company: str, group: str):
    from duke_rates.billing.tariff_engine import schedule_group_for
    from duke_rates.db.repository import Repository
    repo = Repository(db_path)
    all_fams = repo.list_tariff_families(state=state, company=company, family_type="rate_schedule")
    if group == "all":
        return [(f.family_key, f.title or f.family_key, f.schedule_code) for f in all_fams]
    allowed = {g.strip() for g in group.split(",")}
    return [
        (f.family_key, f.title or f.family_key, f.schedule_code)
        for f in all_fams
        if schedule_group_for(f.schedule_code) in allowed
    ]


@st.cache_data(show_spinner=False)
def _get_earliest_date(db_path: str, family_key: str) -> datetime.date | None:
    from duke_rates.db.repository import Repository
    repo = Repository(db_path)
    versions = repo.list_tariff_versions(family_key)
    if not versions:
        return None
    valid_starts = [v.effective_start for v in versions if v.effective_start]
    if not valid_starts:
        return None
    try:
        earliest = min(valid_starts)
        return datetime.datetime.fromisoformat(earliest[:10]).date()
    except Exception:
        return None


def render():
    st.title("Year-Over-Year Bill Escalation")
    st.caption("Hold usage constant and simulate exactly what your bill would have been every month since a rate's inception.")

    st.sidebar.header("Geography & Scale")
    utility_label = st.sidebar.selectbox("Utility", list(_STATE_COMPANY_OPTIONS.keys()), index=0)
    state, company = _STATE_COMPANY_OPTIONS[utility_label]
    group_label = st.sidebar.selectbox("Schedule group", list(_GROUP_OPTIONS.keys()), index=0)
    group = _GROUP_OPTIONS[group_label]

    st.sidebar.markdown("---")
    st.sidebar.header("Usage Profile")
    kwh = st.sidebar.number_input(
        "Constant Monthly kWh", min_value=1.0, max_value=50000.0,
        value=1000.0, step=50.0,
    )
    
    # TOU Inputs
    on_peak_pct = st.sidebar.slider("On-peak %", 0, 100, 20, step=1)
    off_peak_pct = 100 - on_peak_pct
    
    on_peak_kwh = round(kwh * on_peak_pct / 100, 1)
    off_peak_kwh = round(kwh * off_peak_pct / 100, 1)

    peak_kw = st.sidebar.number_input("Peak demand kW (if applicable)", min_value=0.0, value=0.0, step=0.5)

    families = _get_eligible_families(str(DB_PATH), state, company, group)
    if not families:
        st.warning(f"No rate schedules found for {state}/{company} in group '{group_label}'.")
        return

    st.sidebar.markdown("---")
    family_options = {f[0]: f"{f[1]} ({f[2] or f[0]})" for f in families}
    selected_family = st.selectbox("Select Target Rate Schedule", list(family_options.keys()), format_func=lambda x: family_options[x])

    min_date = _get_earliest_date(str(DB_PATH), selected_family)
    if not min_date:
        st.warning("No version history available for this schedule.")
        return

    st.markdown(f"**Earliest recorded data:** {min_date.strftime('%b %Y')}. Generating monthly timeline...")

    # Ensure start date is 1st of the month
    start_date = min_date.replace(day=1)
    today = datetime.date.today()
    if start_date > today:
        st.warning("Start date is in the future. Cannot chart.")
        return

    # Generate range of months
    months = []
    curr = start_date
    while curr <= today:
        months.append(curr)
        # Advance 1 month
        if curr.month == 12:
            curr = curr.replace(year=curr.year + 1, month=1)
        else:
            curr = curr.replace(month=curr.month + 1)

    repo, engine = _get_engine(str(DB_PATH))
    from duke_rates.billing.tariff_engine import BillInput

    with st.spinner(f"Computing engine across {len(months)} months..."):
        results = []
        for d in months:
            usage = BillInput(
                monthly_kwh=kwh,
                service_date=d,
                on_peak_kwh=on_peak_kwh,
                off_peak_kwh=off_peak_kwh,
                discount_kwh=0.0,
                peak_kw=peak_kw if peak_kw > 0 else None,
            )
            r = engine.calculate(selected_family, usage, customer_class="residential", effective_date=d)
            if not r.warnings or not any("No tariff version found" in w for w in r.warnings):
                results.append({
                    "Date": d,
                    "Base Rate ($)": r.base_subtotal,
                    "Riders ($)": r.rider_subtotal,
                    "Total Bill ($)": r.total
                })

    if not results:
        st.error("No valid bill results generated for this timeline.")
        return

    df = pd.DataFrame(results)

    # Plotly Stacked Area Chart
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["Date"], y=df["Base Rate ($)"],
        mode='lines',
        name='Base Rates',
        stackgroup='one',
        line=dict(width=0.5, color='rgb(111, 231, 219)'),
        fillcolor='rgba(111, 231, 219, 0.7)'
    ))
    fig.add_trace(go.Scatter(
        x=df["Date"], y=df["Riders ($)"],
        mode='lines',
        name='Rider Adjustments',
        stackgroup='one',
        line=dict(width=0.5, color='rgb(131, 90, 241)'),
        fillcolor='rgba(131, 90, 241, 0.7)'
    ))

    # Add a phantom trace for Total Bill just for the hover template if preferred,
    # but stacked area naturally shows total.
    fig.add_trace(go.Scatter(
        x=df["Date"], y=df["Total Bill ($)"],
        mode='lines',
        name='Total Bill',
        line=dict(width=2, color='rgb(255, 100, 100)'),
    ))

    fig.update_layout(
        title=f"Historical Escalation: {family_options[selected_family]}",
        xaxis_title="Date",
        yaxis_title="Monthly Bill ($)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )

    st.plotly_chart(fig, use_container_width=True)

    # Metrics
    first_bill = df.iloc[0]["Total Bill ($)"]
    last_bill = df.iloc[-1]["Total Bill ($)"]
    pct_change = ((last_bill - first_bill) / first_bill) * 100 if first_bill > 0 else 0
    
    col1, col2, col3 = st.columns(3)
    col1.metric(f"Bill at {df.iloc[0]['Date'].strftime('%b %Y')}", f"${first_bill:.2f}")
    col2.metric(f"Bill at {df.iloc[-1]['Date'].strftime('%b %Y')}", f"${last_bill:.2f}")
    col3.metric("Total Escalation", f"{pct_change:+.1f}%")

    with st.expander("Show raw data"):
        st.dataframe(df)
