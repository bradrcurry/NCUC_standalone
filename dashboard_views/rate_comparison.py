"""Rate Plan Comparison + TOU Shift Simulator — Streamlit app.

Given a monthly usage profile, calculates what the bill would be under each
available Duke rate schedule and ranks them by total cost.  The TOU shift
simulator lets you drag on-peak % to explore breakeven and savings vs. the
flat RES schedule.

Supports uploading a Duke Energy ESPI/Green Button XML export (15-minute
interval data) to automatically populate actual TOU breakdowns per month.

Run with:
    streamlit run streamlit_rate_comparison_app.py
"""
from __future__ import annotations

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
    "Residential + Small General Service": "residential,sgs",
    "Small General Service only": "sgs",
    "Medium General Service": "mgs",
    "Large General Service": "lgs",
    "All schedules": "all",
}

_CHARGE_TYPE_COLORS = {
    "fixed": "#4e79a7",
    "energy_block": "#59a14f",
    "tou_energy": "#f28e2b",
    "demand": "#e15759",
    "adjustment": "#76b7b2",
    "minimum": "#edc948",
    "credit": "#b07aa1",
}

_CHARGE_TYPE_LABELS = {
    "fixed": "Fixed / Customer Charge",
    "energy_block": "Energy (block rate)",
    "tou_energy": "Energy (TOU)",
    "demand": "Demand",
    "adjustment": "Rider Adjustments",
    "minimum": "Minimum Charge",
    "credit": "Credit",
}


@st.cache_resource(show_spinner=False)
def _get_engine(db_path: str):
    from duke_rates.billing.tariff_engine import TariffBillingEngine
    from duke_rates.db.repository import Repository
    repo = Repository(db_path)
    return repo, TariffBillingEngine(repo)


@st.cache_data(show_spinner=False)
def _get_eligible_families(db_path: str, state: str, company: str, group: str):
    """Return list of (family_key, title, schedule_code) tuples for the group."""
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
def _get_optional_riders(db_path: str, state: str, company: str, group: str):
    """Return optional rider records for the schedule group.

    Queries rider_applicability for non-mandatory riders linked to any rate
    schedule in the group, deduplicating by rider_family_key.
    Returns list of dicts with keys: family_key, title, enrollment_type, notes.
    """
    from duke_rates.db.repository import Repository
    from duke_rates.billing.tariff_engine import schedule_group_for
    repo = Repository(db_path)
    all_fams = repo.list_tariff_families(state=state, company=company, family_type="rate_schedule")
    if group == "all":
        schedule_keys = [f.family_key for f in all_fams]
    else:
        allowed = {g.strip() for g in group.split(",")}
        schedule_keys = [
            f.family_key for f in all_fams
            if schedule_group_for(f.schedule_code) in allowed
        ]
    seen: dict[str, dict] = {}
    for sk in schedule_keys:
        links = repo.list_rider_applicability(applies_to_family_key=sk)
        for link in links:
            if link.mandatory:
                continue
            if link.rider_family_key in seen:
                continue
            rider_fam = repo.get_tariff_family(link.rider_family_key)
            if rider_fam is None:
                continue
            seen[link.rider_family_key] = {
                "family_key": link.rider_family_key,
                "title": rider_fam.title or link.rider_family_key,
                "enrollment_type": link.enrollment_type,
                "notes": link.applicability_notes or "",
            }
    return list(seen.values())


def _calc_all(engine, families, usage, customer_class: str, extra_riders: list[str] | None = None):
    """Run calculate() for each family; return (results, partial)."""
    results, partial = [], []
    for fk, title, _ in families:
        r = engine.calculate(
            fk, usage, customer_class=customer_class,
            include_riders=True, extra_riders=extra_riders,
        )
        if any("Partial TOU coverage" in w for w in r.warnings):
            partial.append(r)
        elif r.base_subtotal > 0:
            results.append(r)
    results.sort(key=lambda r: r.total)
    return results, partial


def _results_to_df(results, baseline_total: float | None) -> pd.DataFrame:
    rows = []
    for r in results:
        total = round(r.total, 2)
        if baseline_total is not None and total != baseline_total:
            delta_mo = total - baseline_total
            vs_baseline = f"{'−' if delta_mo < 0 else '+'}${abs(delta_mo):.2f}/mo  ({'−' if delta_mo < 0 else '+'}${abs(delta_mo*12):.0f}/yr)"
        else:
            vs_baseline = "— baseline"
        rows.append({
            "Schedule": r.schedule_title or r.family_key,
            "family_key": r.family_key,
            "Base": round(r.base_subtotal, 2),
            "Riders": round(r.rider_subtotal, 2),
            "Total": total,
            "vs RES": vs_baseline,
            "Confidence": f"{r.source_confidence:.0%}",
        })
    return pd.DataFrame(rows)


def _bar_chart(results) -> go.Figure:
    short_names = [
        (r.schedule_title or r.family_key)[:32] + ("…" if len(r.schedule_title or r.family_key) > 33 else "")
        for r in results
    ]
    charge_types = ["fixed", "energy_block", "tou_energy", "demand", "adjustment", "minimum", "credit"]
    type_totals: dict[str, list[float]] = {ct: [] for ct in charge_types}
    for r in results:
        sums = {ct: 0.0 for ct in charge_types}
        for item in r.line_items:
            if item.charge_type in sums:
                sums[item.charge_type] += item.amount
        for ct in charge_types:
            type_totals[ct].append(round(sums[ct], 2))

    fig = go.Figure()
    for ct in charge_types:
        vals = type_totals[ct]
        if any(v != 0 for v in vals):
            fig.add_trace(go.Bar(
                name=_CHARGE_TYPE_LABELS.get(ct, ct),
                x=short_names,
                y=vals,
                marker_color=_CHARGE_TYPE_COLORS.get(ct, "#aaa"),
            ))
    fig.update_layout(
        barmode="stack",
        title="Monthly Bill by Charge Component",
        yaxis_title="$ / month",
        xaxis_title="",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        height=400,
        margin=dict(t=80, b=20),
    )
    return fig


def _breakeven_chart(engine, families, baseline_fk: str, kwh: float,
                     service_date, peak_kw: float | None,
                     extra_riders: list[str] | None = None) -> go.Figure:
    """Line chart: cost vs. on-peak % for each TOU schedule + RES baseline."""
    from duke_rates.billing.tariff_engine import BillInput

    # Sweep on-peak % from 0 to 70% in 2pp steps; remainder split 80/20 off-peak/discount
    on_peak_pcts = list(range(0, 72, 2))
    schedule_traces: dict[str, list[float]] = {}

    for fk, title, sc in families:
        costs = []
        for pct in on_peak_pcts:
            op_kwh = round(kwh * pct / 100, 1)
            remaining = kwh - op_kwh
            disc_kwh = round(remaining * 0.15, 1)  # 15% of remainder goes to discount
            offp_kwh = round(remaining - disc_kwh, 1)
            u = BillInput(
                monthly_kwh=kwh,
                service_date=service_date,
                on_peak_kwh=op_kwh,
                off_peak_kwh=offp_kwh,
                discount_kwh=disc_kwh,
                peak_kw=peak_kw,
            )
            r = engine.calculate(fk, u, customer_class="residential", include_riders=True, extra_riders=extra_riders)
            if any("Partial TOU coverage" in w for w in r.warnings):
                costs.append(None)
            else:
                costs.append(round(r.total, 2))
        short = (title or fk)[:40]
        schedule_traces[short] = costs

    # RES baseline (flat — doesn't change with TOU split)
    res_costs = []
    for _ in on_peak_pcts:
        u = BillInput(monthly_kwh=kwh, service_date=service_date, peak_kw=peak_kw)
        r = engine.calculate(baseline_fk, u, customer_class="residential", include_riders=True, extra_riders=extra_riders)
        res_costs.append(round(r.total, 2))

    fig = go.Figure()
    # RES as a dashed reference line
    if res_costs:
        res_title = "RES (flat baseline)"
        fig.add_trace(go.Scatter(
            x=on_peak_pcts, y=res_costs,
            mode="lines", name=res_title,
            line=dict(color="#888888", dash="dash", width=2),
        ))

    colors = ["#e15759", "#4e79a7", "#f28e2b", "#59a14f", "#76b7b2", "#edc948", "#b07aa1"]
    for i, (name, costs) in enumerate(schedule_traces.items()):
        # Skip if all None (partial coverage throughout)
        if all(c is None for c in costs):
            continue
        fig.add_trace(go.Scatter(
            x=on_peak_pcts, y=costs,
            mode="lines+markers", name=name,
            line=dict(color=colors[i % len(colors)], width=2),
            marker=dict(size=4),
        ))

    fig.update_layout(
        title="Cost vs. On-Peak % of Total Usage",
        xaxis_title="On-peak % of monthly kWh",
        yaxis_title="$ / month",
        xaxis=dict(ticksuffix="%"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        height=420,
        margin=dict(t=90, b=40),
    )
    return fig


def _line_item_df(result) -> pd.DataFrame:
    rows = []
    for item in result.line_items:
        rows.append({
            "Description": item.label,
            "Type": _CHARGE_TYPE_LABELS.get(item.charge_type, item.charge_type),
            "Rate": f"{item.rate_value:.5f} {item.rate_unit}" if item.rate_value else "",
            "Qty": f"{item.quantity:,.1f}" if item.quantity is not None else "",
            "Amount": f"${item.amount:,.2f}",
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------

def render():
    st.title("Duke Rate Plan Comparison")
    st.caption("Compare rate plans and simulate energy shifting to find breakeven and savings.")

    # ---------------------------------------------------------------------------
    # Sidebar: optional ESPI XML upload
    # ---------------------------------------------------------------------------
    st.sidebar.header("Upload Usage Data (optional)")
    st.sidebar.caption(
        "Upload a Duke Energy XML export (Green Button / ESPI format) to auto-populate "
        "actual monthly TOU usage. Download from MyAccount → Usage → Export."
    )

    uploaded_file = st.sidebar.file_uploader("Duke Energy XML export", type=["xml"])

    _parsed_profile = None
    _selected_month: "MonthlyUsageSummary | None" = None  # type: ignore[name-defined]

    if uploaded_file is not None:
        try:
            from duke_rates.billing.espi_parser import parse_espi_xml
            _parsed_profile = parse_espi_xml(uploaded_file.read())
            st.sidebar.success(
                f"Parsed {_parsed_profile.interval_count:,} intervals · "
                f"{_parsed_profile.total_kwh:,.0f} kWh · "
                f"{len(_parsed_profile.months)} months"
            )
            if _parsed_profile.warnings:
                for w in _parsed_profile.warnings[:3]:
                    st.sidebar.warning(w)
        except Exception as exc:
            st.sidebar.error(f"Could not parse XML: {exc}")
            _parsed_profile = None

    if _parsed_profile is not None and _parsed_profile.months:
        month_labels = [f"{m.year}-{m.month:02d}" for m in _parsed_profile.months]
        # Default to most recent complete month (second-to-last; last may be partial)
        default_idx = max(0, len(month_labels) - 2)
        selected_label = st.sidebar.selectbox(
            "Select month to analyze", month_labels, index=default_idx,
            help="Choose which month's actual usage to load into the comparison.",
        )
        _selected_month = next(
            m for m in _parsed_profile.months
            if f"{m.year}-{m.month:02d}" == selected_label
        )
        if st.sidebar.button("Load selected month →"):
            st.session_state["_espi_kwh"] = round(_selected_month.total_kwh, 0)
            st.session_state["_espi_on_peak_pct"] = round(_selected_month.on_peak_pct)
            st.session_state["_espi_discount_pct"] = round(_selected_month.discount_pct)
            st.session_state["_espi_peak_kw"] = round(_selected_month.peak_kw, 1)
            st.session_state["_espi_service_date"] = _selected_month.service_date
            st.rerun()

    st.sidebar.markdown("---")
    # --- Sidebar: core inputs ---
    st.sidebar.header("Usage Profile")

    utility_label = st.sidebar.selectbox("Utility", list(_STATE_COMPANY_OPTIONS.keys()), index=0)
    state, company = _STATE_COMPANY_OPTIONS[utility_label]

    kwh = st.sidebar.number_input(
        "Monthly kWh", min_value=1.0, max_value=50000.0,
        value=float(st.session_state.get("_espi_kwh", 1000.0)), step=50.0,
        help="Total kWh from your Duke bill for this month.",
    )

    service_date = st.sidebar.date_input(
        "Service date",
        value=st.session_state.get("_espi_service_date", datetime.date.today().replace(day=1)),
        help="Month/year of the bill — used for season and effective tariff version.",
    )

    peak_kw = st.sidebar.number_input(
        "Peak demand kW", min_value=0.0,
        value=float(st.session_state.get("_espi_peak_kw", 0.0)), step=0.5,
        help="Only needed for demand-based schedules (R-TOUD, LGS, etc.).",
    )

    group_label = st.sidebar.selectbox("Schedule group", list(_GROUP_OPTIONS.keys()), index=0)
    group = _GROUP_OPTIONS[group_label]

    st.sidebar.markdown("---")
    st.sidebar.header("TOU Shift Simulator")
    st.sidebar.caption(
        "Drag sliders to redistribute your kWh across TOU periods. "
        "On-peak is the most expensive period — shifting usage away from it saves money on TOU plans."
    )

    on_peak_pct = st.sidebar.slider(
        "On-peak %", min_value=0, max_value=70,
        value=min(70, int(st.session_state.get("_espi_on_peak_pct", 30))), step=1,
        help="% of monthly kWh consumed during on-peak hours (weekdays ~2–9pm)",
    )
    discount_pct = st.sidebar.slider(
        "Discount-period %", min_value=0, max_value=40,
        value=min(40, int(st.session_state.get("_espi_discount_pct", 10))), step=1,
        help="% consumed during optional discount hours (varies by plan)",
    )
    # Off-peak is the remainder
    off_peak_pct = max(0, 100 - on_peak_pct - discount_pct)

    st.sidebar.markdown(
        f"**Off-peak: {off_peak_pct}%** &nbsp;*(remainder after on-peak + discount)*",
        unsafe_allow_html=True,
    )
    if on_peak_pct + discount_pct > 100:
        st.sidebar.error("On-peak + discount exceeds 100%. Reduce one of the sliders.")
        return

    on_peak_kwh = round(kwh * on_peak_pct / 100, 1)
    off_peak_kwh = round(kwh * off_peak_pct / 100, 1)
    discount_kwh = round(kwh - on_peak_kwh - off_peak_kwh, 1)

    st.sidebar.caption(
        f"{on_peak_kwh:,.0f} kWh on-peak · {off_peak_kwh:,.0f} kWh off-peak · {discount_kwh:,.0f} kWh discount"
    )

    st.sidebar.markdown("---")

    # --- Sidebar: optional riders ---
    _optional_rider_records = _get_optional_riders(DB_PATH, state, company, group)

    if _optional_rider_records:
        st.sidebar.header("Optional Riders")
        st.sidebar.caption(
            "These riders are not applied to every customer. "
            "Enable the ones that apply to your account."
        )
        _ENROLLMENT_LABELS = {
            "opt_in": "Opt-in",
            "conditional": "Conditional",
            "opt_out": "Opt-out",
            "geographic": "Geographic",
        }
        # Group by enrollment_type
        _by_type: dict[str, list] = {}
        for rec in _optional_rider_records:
            _by_type.setdefault(rec["enrollment_type"], []).append(rec)

        selected_extra_riders: list[str] = []
        for etype in ("opt_in", "conditional", "opt_out", "geographic"):
            if etype not in _by_type:
                continue
            st.sidebar.markdown(f"**{_ENROLLMENT_LABELS.get(etype, etype.title())} programs**")
            for rec in _by_type[etype]:
                checked = st.sidebar.checkbox(
                    rec["title"],
                    value=False,
                    key=f"opt_rider_{rec['family_key']}",
                    help=rec["notes"] or None,
                )
                if checked:
                    selected_extra_riders.append(rec["family_key"])
    else:
        selected_extra_riders = []

    st.sidebar.markdown("---")
    st.sidebar.header("Data Updates")


    @st.cache_data(show_spinner=False, ttl=300)
    def _load_update_status(db_path: str, _state: str, _company: str):
        """Query DB for document last-retrieved dates and tariff version counts."""
        import sqlite3 as _sq
        import datetime as _dt
        try:
            conn = _sq.connect(db_path)
            conn.row_factory = _sq.Row
            # Most recent retrieved_at across documents for this state/company
            row = conn.execute(
                """
                SELECT MAX(retrieved_at) AS last_crawl,
                       COUNT(*) AS n_docs
                FROM documents
                WHERE (state = ? OR state IS NULL)
                  AND (company = ? OR company IS NULL)
                """,
                (_state.upper(), _company.lower()),
            ).fetchone()
            last_crawl = row["last_crawl"] if row else None
            n_docs = row["n_docs"] if row else 0
            # Count tariff families and version coverage
            row2 = conn.execute(
                """
                SELECT COUNT(*) AS n_families,
                       SUM(CASE WHEN tv.family_key IS NOT NULL THEN 1 ELSE 0 END) AS n_parsed
                FROM tariff_families tf
                LEFT JOIN (
                    SELECT DISTINCT family_key FROM tariff_versions
                ) tv ON tf.family_key = tv.family_key
                WHERE tf.state = ? AND tf.company = ?
                """,
                (_state.upper(), _company.lower()),
            ).fetchone()
            n_families = row2["n_families"] if row2 else 0
            n_parsed = row2["n_parsed"] if row2 else 0
            conn.close()
            return last_crawl, n_docs, n_families, n_parsed
        except Exception:
            return None, 0, 0, 0


    _last_crawl, _n_docs, _n_families, _n_parsed = _load_update_status(
        str(DB_PATH), state, company
    )

    if _last_crawl:
        try:
            import datetime as _dt2
            _crawl_dt = _dt2.datetime.fromisoformat(_last_crawl[:19])
            _age_days = (datetime.datetime.now() - _crawl_dt).days
            _age_label = f"{_age_days}d ago" if _age_days > 0 else "today"
            st.sidebar.caption(
                f"Last crawl: {_crawl_dt.strftime('%Y-%m-%d')} ({_age_label})  \n"
                f"{_n_docs} docs archived · {_n_parsed}/{_n_families} families parsed"
            )
            if _age_days >= 30:
                st.sidebar.warning(f"Data is {_age_days} days old. Consider re-crawling.")
        except Exception:
            st.sidebar.caption(f"{_n_docs} docs archived · {_n_parsed}/{_n_families} families parsed")
    else:
        st.sidebar.caption(f"{_n_docs} docs archived · {_n_parsed}/{_n_families} families parsed")

    st.sidebar.caption(
        "To check for updates, run:  \n"
        "`duke-rates tariff-update --state NC --company progress`  \n"
        "Add `--auto-parse` to re-parse changed documents automatically."
    )

    # ---------------------------------------------------------------------------
    # Calculate (reactive — runs on every slider/input change)
    # ---------------------------------------------------------------------------

    from duke_rates.billing.tariff_engine import BillInput  # noqa: E402

    usage = BillInput(
        monthly_kwh=kwh,
        service_date=service_date,
        on_peak_kwh=on_peak_kwh,
        off_peak_kwh=off_peak_kwh,
        discount_kwh=discount_kwh,
        peak_kw=peak_kw if peak_kw > 0 else None,
    )

    repo, engine = _get_engine(str(DB_PATH))
    families = _get_eligible_families(str(DB_PATH), state, company, group)

    if not families:
        st.warning(f"No rate schedules found for {state}/{company} in group '{group_label}'. "
                   "Check that the DB is populated.")
        return

    results, partial = _calc_all(
        engine, families, usage, customer_class="residential",
        extra_riders=selected_extra_riders or None,
    )

    if not results:
        st.warning("No schedules returned results for the current inputs.")
        return

    # Find RES baseline (flat residential — insensitive to TOU split)
    res_result = next((r for r in results if r.family_key and "leaf-500" in r.family_key), None)
    if res_result is None:
        # Fallback: find the cheapest non-TOU result as baseline
        res_result = next(
            (r for r in results if not any(i.charge_type == "tou_energy" for i in r.line_items)),
            results[-1],  # last resort: most expensive
        )
    baseline_total = res_result.total if res_result else None

    # ---------------------------------------------------------------------------
    # Main panel: two tabs
    # ---------------------------------------------------------------------------

    tab_compare, tab_shift, tab_usage, tab_solar, tab_history = st.tabs(
        ["Rate Comparison", "Shift Simulator", "Monthly Usage", "Solar Sizing", "Rate History"]
    )

    with tab_compare:
        st.markdown(
            f"**{state}/{company} · {kwh:,.0f} kWh · {service_date} · {group_label}** — "
            f"on-peak {on_peak_pct}% / off-peak {off_peak_pct}% / discount {discount_pct}%"
        )
        if selected_extra_riders:
            _opt_titles = [
                r["title"] for r in _optional_rider_records
                if r["family_key"] in selected_extra_riders
            ]
            st.info(f"Optional riders included: {', '.join(_opt_titles)}", icon="ℹ️")

        # Summary table
        df = _results_to_df(results, baseline_total)
        cheapest_total = df["Total"].min()

        def _row_style(row):
            if row["Total"] == cheapest_total:
                return ["background-color: #d4edda"] * len(row)
            return [""] * len(row)

        st.dataframe(
            df[["Schedule", "Base", "Riders", "Total", "vs RES", "Confidence"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "Base": st.column_config.NumberColumn("Base ($)", format="$%.2f"),
                "Riders": st.column_config.NumberColumn("Riders ($)", format="$%.2f"),
                "Total": st.column_config.NumberColumn("Total ($)", format="$%.2f"),
            },
        )

        cheapest = results[0]
        if baseline_total is not None and cheapest.total < baseline_total:
            savings_mo = round(baseline_total - cheapest.total, 2)
            savings_yr = round(savings_mo * 12, 0)
            st.success(
                f"**Best plan: {cheapest.schedule_title or cheapest.family_key}**  —  "
                f"saves **${savings_mo:.2f}/month** (${savings_yr:.0f}/year) vs. RES at current usage profile."
            )
        elif cheapest.total == baseline_total:
            st.info("RES is the cheapest plan at this usage profile. Shift more usage off-peak to unlock TOU savings.")

        # Stacked bar chart
        st.plotly_chart(_bar_chart(results), use_container_width=True)

        # Line-item detail
        with st.expander("Line-item detail by schedule"):
            for r in results:
                title = r.schedule_title or r.family_key
                st.markdown(f"**{title}** — ${r.total:.2f}/mo")
                st.dataframe(_line_item_df(r), use_container_width=True, hide_index=True)
                if r.warnings:
                    for w in r.warnings:
                        st.caption(f"⚠ {w}")
                st.markdown("---")

        if partial:
            with st.expander(f"Excluded schedules ({len(partial)}) — incomplete charge data"):
                for r in partial:
                    st.markdown(f"- **{r.schedule_title or r.family_key}**: " + "; ".join(r.warnings))


    with tab_shift:
        st.markdown("### How much do you need to shift to save money?")
        st.caption(
            "This chart sweeps on-peak % from 0% to 70% while keeping total kWh fixed. "
            "The dashed line is the flat RES plan. Where a TOU line drops below it is the breakeven point."
        )

        # Find the RES family key for the breakeven chart
        res_fk = res_result.family_key if res_result else None
        if res_fk is None:
            st.info("No flat RES schedule found — breakeven chart requires a flat-rate baseline.")
        else:
            with st.spinner("Computing breakeven curve…"):
                fig_shift = _breakeven_chart(
                    engine, families, res_fk, kwh, service_date,
                    peak_kw if peak_kw > 0 else None,
                    extra_riders=selected_extra_riders or None,
                )
            st.plotly_chart(fig_shift, use_container_width=True)

            # Annotate current position
            st.markdown(
                f"**Your current position:** on-peak = {on_peak_pct}% "
                f"({on_peak_kwh:,.0f} kWh) — marked by the sliders on the left."
            )

            # Find breakeven for each TOU schedule
            st.markdown("#### Breakeven thresholds")
            st.caption(
                "The on-peak % below which each TOU plan becomes cheaper than RES "
                "(assuming off-peak gets 85% of remainder, discount gets 15%)."
            )

            from duke_rates.billing.tariff_engine import BillInput as _BI

            rows = []
            for fk, title, sc in families:
                if fk == res_fk:
                    continue
                # Check if TOU is ever cheaper (at 0% on-peak = all off-peak/discount)
                u0 = _BI(monthly_kwh=kwh, service_date=service_date,
                         on_peak_kwh=0, off_peak_kwh=round(kwh * 0.85, 1),
                         discount_kwh=round(kwh * 0.15, 1),
                         peak_kw=peak_kw if peak_kw > 0 else None)
                r0 = engine.calculate(fk, u0, customer_class="residential", include_riders=True, extra_riders=selected_extra_riders or None)
                r0_res = engine.calculate(res_fk, u0, customer_class="residential", include_riders=True, extra_riders=selected_extra_riders or None)
                if any("Partial TOU coverage" in w for w in r0.warnings):
                    breakeven = None
                elif r0.total >= r0_res.total:
                    # TOU is never cheaper even at 0% on-peak — no breakeven exists
                    breakeven = -1  # sentinel: never cheaper
                else:
                    # Binary search: find highest on-peak % where TOU is still cheaper
                    lo, hi = 0, 70
                    breakeven = 0
                    for _ in range(12):
                        mid = (lo + hi) // 2
                        op = round(kwh * mid / 100, 1)
                        disc = round((kwh - op) * 0.15, 1)
                        offp = round(kwh - op - disc, 1)
                        u = _BI(monthly_kwh=kwh, service_date=service_date,
                                on_peak_kwh=op, off_peak_kwh=offp, discount_kwh=disc,
                                peak_kw=peak_kw if peak_kw > 0 else None)
                        r_tou = engine.calculate(fk, u, customer_class="residential", include_riders=True, extra_riders=selected_extra_riders or None)
                        r_res = engine.calculate(res_fk, u, customer_class="residential", include_riders=True, extra_riders=selected_extra_riders or None)
                        if r_tou.total < r_res.total:
                            # Still cheaper — push breakeven up
                            lo = mid
                            breakeven = mid
                        else:
                            hi = mid
                        if hi - lo <= 1:
                            break

                if breakeven is None:
                    rows.append({
                        "Schedule": (title or fk)[:50],
                        "Breakeven on-peak %": "N/A (incomplete data)",
                        "Breakeven on-peak kWh": "—",
                        "Currently saving?": "unknown",
                    })
                elif breakeven == -1:
                    rows.append({
                        "Schedule": (title or fk)[:50],
                        "Breakeven on-peak %": "Never cheaper than RES",
                        "Breakeven on-peak kWh": "—",
                        "Currently saving?": "no",
                    })
                else:
                    op_be = round(kwh * breakeven / 100, 1)
                    currently_cheaper = "✓ yes" if on_peak_pct <= breakeven else "✗ no"
                    rows.append({
                        "Schedule": (title or fk)[:50],
                        "Breakeven on-peak %": f"≤ {breakeven}%",
                        "Breakeven on-peak kWh": f"{op_be:,.0f} kWh",
                        "Currently saving?": currently_cheaper,
                    })

            if rows:
                be_df = pd.DataFrame(rows)
                st.dataframe(
                    be_df[["Schedule", "Breakeven on-peak %", "Breakeven on-peak kWh", "Currently saving?"]],
                    use_container_width=True, hide_index=True,
                )
            else:
                st.info("No TOU schedules to compare breakeven against.")

            st.markdown("""
    **How to read this:**
    - If your on-peak % is *below* the breakeven threshold, the TOU plan is cheaper than RES
    - To shift usage off-peak: run dishwasher, laundry, and EV charging overnight or early morning
    - On-peak hours on Duke TOU plans are approximately weekdays 2–9 pm
    """)

    with tab_usage:
        if _parsed_profile is None:
            st.info(
                "Upload a Duke Energy XML export in the sidebar to see your actual monthly usage breakdown. "
                "Download it from MyAccount → My Usage → Export Data → Energy Usage."
            )
        else:
            st.markdown(
                f"### Usage data: {_parsed_profile.service_point_id or 'unknown service point'}  "
                f"({_parsed_profile.date_range_start} – {_parsed_profile.date_range_end})"
            )
            col_a, col_b, col_c = st.columns(3)
            col_a.metric("Total kWh", f"{_parsed_profile.total_kwh:,.0f}")
            col_b.metric("Intervals", f"{_parsed_profile.interval_count:,}")
            col_c.metric("Months", len(_parsed_profile.months))

            # Monthly breakdown table
            month_rows = []
            for m in _parsed_profile.months:
                month_rows.append({
                    "Month": f"{m.year}-{m.month:02d}",
                    "Total kWh": round(m.total_kwh, 1),
                    "On-peak kWh": round(m.on_peak_kwh, 1),
                    "Off-peak kWh": round(m.off_peak_kwh, 1),
                    "Discount kWh": round(m.discount_kwh, 1),
                    "On-peak %": round(m.on_peak_pct, 1),
                    "Discount %": round(m.discount_pct, 1),
                    "Peak kW": round(m.peak_kw, 2),
                    "Intervals": m.interval_count,
                })
            month_df = pd.DataFrame(month_rows)

            # Highlight the currently selected month
            selected_month_label = (
                f"{_selected_month.year}-{_selected_month.month:02d}"
                if _selected_month else None
            )

            def _highlight_selected(row):
                if row["Month"] == selected_month_label:
                    return ["background-color: #fff3cd"] * len(row)
                return [""] * len(row)

            st.dataframe(
                month_df.style.apply(_highlight_selected, axis=1),
                use_container_width=True, hide_index=True,
                column_config={
                    "Total kWh": st.column_config.NumberColumn(format="%.1f"),
                    "On-peak kWh": st.column_config.NumberColumn(format="%.1f"),
                    "Off-peak kWh": st.column_config.NumberColumn(format="%.1f"),
                    "Discount kWh": st.column_config.NumberColumn(format="%.1f"),
                    "On-peak %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Discount %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Peak kW": st.column_config.NumberColumn(format="%.2f"),
                },
            )

            if selected_month_label:
                st.caption(f"Highlighted row = currently loaded month ({selected_month_label})")

            # Stacked area chart of kWh by TOU period over time
            fig_usage = go.Figure()
            months_sorted = sorted(_parsed_profile.months, key=lambda m: (m.year, m.month))
            labels = [f"{m.year}-{m.month:02d}" for m in months_sorted]
            fig_usage.add_trace(go.Bar(
                x=labels, y=[m.on_peak_kwh for m in months_sorted],
                name="On-peak", marker_color="#e15759",
            ))
            fig_usage.add_trace(go.Bar(
                x=labels, y=[m.off_peak_kwh for m in months_sorted],
                name="Off-peak", marker_color="#4e79a7",
            ))
            fig_usage.add_trace(go.Bar(
                x=labels, y=[m.discount_kwh for m in months_sorted],
                name="Discount", marker_color="#59a14f",
            ))
            fig_usage.update_layout(
                barmode="stack",
                title="Monthly kWh by TOU Period",
                xaxis_title="Month",
                yaxis_title="kWh",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                height=380,
                margin=dict(t=80, b=40),
            )
            st.plotly_chart(fig_usage, use_container_width=True)

            # On-peak % trend line
            fig_pct = go.Figure()
            fig_pct.add_trace(go.Scatter(
                x=labels, y=[round(m.on_peak_pct, 1) for m in months_sorted],
                mode="lines+markers", name="On-peak %",
                line=dict(color="#e15759", width=2),
                marker=dict(size=6),
            ))
            fig_pct.add_hline(
                y=sum(m.on_peak_pct for m in months_sorted) / len(months_sorted),
                line_dash="dash", line_color="#888",
                annotation_text="avg",
            )
            fig_pct.update_layout(
                title="On-peak % Over Time",
                xaxis_title="Month", yaxis_title="On-peak %",
                yaxis=dict(ticksuffix="%"),
                height=280,
                margin=dict(t=60, b=40),
            )
            st.plotly_chart(fig_pct, use_container_width=True)

            if _parsed_profile.warnings:
                with st.expander("Parser warnings"):
                    for w in _parsed_profile.warnings:
                        st.warning(w)

    # ---------------------------------------------------------------------------
    # Tab 4: Solar Sizing
    # ---------------------------------------------------------------------------

    with tab_solar:
        st.markdown("### Solar PV Sizing & ROI Estimator")
        st.caption(
            "Estimates monthly generation, bill savings, and payback period for a rooftop "
            "solar system. Uses Duke NC net metering: generation offsets consumption at retail "
            "rates; annual surplus credited at ~$0.04/kWh avoided cost."
        )

        # Solar uses the best-available profile: uploaded ESPI or synthetic from sidebar inputs
        if _parsed_profile is not None and len(_parsed_profile.months) >= 6:
            solar_profile = _parsed_profile
            st.info(
                f"Using uploaded usage data: {len(solar_profile.months)} months "
                f"({solar_profile.date_range_start} – {solar_profile.date_range_end})"
            )
        else:
            # Build synthetic profile from sidebar inputs (12 months, uniform)
            from duke_rates.billing.espi_parser import MonthlyUsageSummary as _MUS, UsageProfile as _UP
            _syn_months = []
            for _i in range(12):
                _y, _mo = divmod(_i, 12)
                _year = service_date.year + _y
                _month = _mo + 1
                _on  = round(kwh * on_peak_pct  / 100, 1)
                _off = round(kwh * off_peak_pct / 100, 1)
                _disc = round(kwh - _on - _off, 1)
                _syn_months.append(_MUS(
                    year=_year, month=_month,
                    total_kwh=kwh,
                    on_peak_kwh=_on, off_peak_kwh=_off, discount_kwh=_disc,
                    peak_kw=peak_kw if peak_kw > 0 else 0.0,
                ))
            solar_profile = _UP(months=_syn_months, total_kwh=kwh * 12)
            st.info(
                "No usage file uploaded — using sidebar inputs (uniform 12-month profile). "
                "Upload a Duke Energy XML export for more accurate results."
            )

        # Solar-specific sidebar inputs
        col_s1, col_s2, col_s3 = st.columns(3)
        with col_s1:
            solar_kw_max = st.number_input(
                "Max system size (kW)", min_value=2.0, max_value=40.0, value=16.0, step=1.0,
                help="Maximum DC nameplate to evaluate in the sweep.",
            )
        with col_s2:
            cost_per_watt = st.number_input(
                "Installed cost ($/watt)", min_value=1.0, max_value=8.0, value=3.50, step=0.25,
                help="All-in cost per watt DC including equipment, labor, and permits.",
            )
        with col_s3:
            solar_derate = st.slider(
                "System efficiency (derate)", min_value=0.60, max_value=0.97,
                value=0.80, step=0.01,
                help="DC-to-AC conversion factor: 0.80 = typical string inverter; 0.90 = microinverters.",
            )

        # Pick which schedule to evaluate solar against
        solar_family_options = {
            (r.schedule_title or r.family_key): r.family_key
            for r in results
            if not any("Partial TOU coverage" in w for w in r.warnings)
        }
        solar_schedule_label = st.selectbox(
            "Rate schedule for solar analysis",
            list(solar_family_options.keys()),
            help="Which rate plan to assume when calculating bill savings from solar.",
        )
        solar_fk = solar_family_options[solar_schedule_label]

        if st.button("Run solar sizing sweep", type="primary"):
            from duke_rates.billing.solar_sizing import sweep_system_sizes

            sweep_sizes = list(range(2, int(solar_kw_max) + 1))
            with st.spinner("Computing solar sizing sweep…"):
                sweep = sweep_system_sizes(
                    solar_profile, solar_fk, engine,
                    sizes=sweep_sizes,
                    cost_per_watt=cost_per_watt,
                    derate=solar_derate,
                    customer_class="residential",
                    include_riders=True,
                )

            # Summary sweep table
            sweep_rows = []
            for r in sweep:
                sweep_rows.append({
                    "System (kW)": r.system_kw,
                    "Annual Gen (kWh)": round(r.annual_generation_kwh, 0),
                    "Annual Offset (kWh)": round(r.annual_offset_kwh, 0),
                    "Annual Export (kWh)": round(r.annual_export_kwh, 0),
                    "Annual Savings ($)": round(r.annual_savings, 2),
                    "System Cost ($)": round(r.cost_dollars, 0) if r.cost_dollars else None,
                    "Payback (yrs)": r.payback_years,
                })
            sweep_df = pd.DataFrame(sweep_rows)

            # Highlight the "knee" — first size where marginal savings per kW drops below 50% of initial
            marginals = [0.0] + [
                sweep[i].annual_savings - sweep[i - 1].annual_savings
                for i in range(1, len(sweep))
            ]
            if marginals[1] > 0:
                first_half_marginal = next(
                    (i for i, m in enumerate(marginals) if i > 0 and m < marginals[1] * 0.5),
                    None,
                )
                if first_half_marginal:
                    knee_kw = sweep[first_half_marginal - 1].system_kw
                    st.success(
                        f"**Recommended size: {knee_kw:.0f} kW** — "
                        f"marginal savings per additional kW begins to diminish significantly above this point."
                    )

            st.dataframe(
                sweep_df,
                use_container_width=True, hide_index=True,
                column_config={
                    "Annual Gen (kWh)": st.column_config.NumberColumn(format="%.0f"),
                    "Annual Offset (kWh)": st.column_config.NumberColumn(format="%.0f"),
                    "Annual Export (kWh)": st.column_config.NumberColumn(format="%.0f"),
                    "Annual Savings ($)": st.column_config.NumberColumn(format="$%.2f"),
                    "System Cost ($)": st.column_config.NumberColumn(format="$%.0f"),
                    "Payback (yrs)": st.column_config.NumberColumn(format="%.1f yrs"),
                },
            )

            # Savings vs system size chart
            fig_solar = go.Figure()
            fig_solar.add_trace(go.Bar(
                x=[r.system_kw for r in sweep],
                y=[r.annual_savings for r in sweep],
                name="Annual Savings ($)",
                marker_color="#59a14f",
            ))
            fig_solar.add_trace(go.Scatter(
                x=[r.system_kw for r in sweep],
                y=marginals[1:],
                name="Marginal savings per kW ($)",
                mode="lines+markers",
                line=dict(color="#e15759", dash="dot", width=2),
                yaxis="y2",
            ))
            fig_solar.update_layout(
                title="Annual Savings vs. System Size",
                xaxis_title="System size (kW DC)",
                yaxis=dict(title="Annual savings ($)", tickprefix="$"),
                yaxis2=dict(title="Marginal $/kW", overlaying="y", side="right",
                            tickprefix="$", showgrid=False),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                height=380,
                margin=dict(t=80, b=40),
            )
            st.plotly_chart(fig_solar, use_container_width=True)

            # Payback chart
            payback_vals = [r.payback_years for r in sweep]
            if any(v is not None for v in payback_vals):
                fig_pb = go.Figure()
                fig_pb.add_trace(go.Scatter(
                    x=[r.system_kw for r in sweep],
                    y=payback_vals,
                    mode="lines+markers",
                    name="Payback (years)",
                    line=dict(color="#4e79a7", width=2),
                    marker=dict(size=6),
                ))
                fig_pb.update_layout(
                    title=f"Simple Payback Period at ${cost_per_watt:.2f}/W",
                    xaxis_title="System size (kW DC)",
                    yaxis_title="Payback (years)",
                    height=280,
                    margin=dict(t=60, b=40),
                )
                st.plotly_chart(fig_pb, use_container_width=True)

            # Per-month detail for the recommended or largest evaluated size
            detail_kw = knee_kw if 'knee_kw' in dir() else sweep[-1].system_kw
            detail_result = next(r for r in sweep if r.system_kw == detail_kw)
            with st.expander(f"Month-by-month detail for {detail_kw:.0f} kW system"):
                month_detail_rows = []
                for sm in detail_result.months:
                    month_detail_rows.append({
                        "Month": f"{sm.year}-{sm.month:02d}",
                        "Generation (kWh)": round(sm.generation_kwh, 0),
                        "Offset (kWh)": round(sm.offset_kwh, 0),
                        "Export (kWh)": round(sm.export_kwh, 0),
                        "Net Usage (kWh)": round(sm.net_usage_kwh, 0),
                        "Bill without solar ($)": round(sm.bill_without, 2),
                        "Bill with solar ($)": round(sm.bill_with, 2),
                        "Monthly savings ($)": round(sm.savings, 2),
                    })
                st.dataframe(
                    pd.DataFrame(month_detail_rows),
                    use_container_width=True, hide_index=True,
                    column_config={
                        "Bill without solar ($)": st.column_config.NumberColumn(format="$%.2f"),
                        "Bill with solar ($)": st.column_config.NumberColumn(format="$%.2f"),
                        "Monthly savings ($)": st.column_config.NumberColumn(format="$%.2f"),
                    },
                )

            st.markdown("""
    **Notes:**
    - NC capacity factors are approximate (NREL PVWatts typical values for central NC, south-facing 4° tilt)
    - Demand charges (peak kW) are not reduced by solar — savings for demand-metered schedules are conservative
    - Payback assumes constant rates; does not include ITC (30% federal tax credit), inflation, or degradation
    - Duke NC net metering: monthly offset at retail; annual surplus at ~$0.04/kWh avoided cost
    """)
        else:
            st.caption(
                "Configure inputs above and click **Run solar sizing sweep** to compute results."
            )

    # ---------------------------------------------------------------------------
    # Tab 5: Rate History
    # ---------------------------------------------------------------------------

    with tab_history:
        st.markdown("### Duke Energy Rate History")
        st.caption(
            "Historical all-in residential rate (base + riders) for DEP (NC Progress) and "
            "DEC (NC Carolinas) from the canonical rate timeline. "
            "Uses parsed NCUC tariff leaf data and rider summary history."
        )

        @st.cache_data(show_spinner=False)
        def _load_rate_history(db_path: str):
            from pathlib import Path
            from duke_rates.analytics.canonical_residential import load_canonical_residential_timeline
            try:
                df = load_canonical_residential_timeline(database_path=Path(db_path))
                return df
            except Exception as exc:
                return None, str(exc)

        hist_result = _load_rate_history(str(DB_PATH))
        if hist_result is None or (hasattr(hist_result, "empty") and hist_result.empty):
            st.info(
                "No historical rate data found. Run `duke-rates recover-history-progress-nc` "
                "to populate the historical timeline."
            )
        else:
            hist_df = hist_result

            # Filter to NC utilities only (DEP = NC Progress, DEC = NC Carolinas)
            hist_df = hist_df[hist_df["utility"].isin(["DEP", "DEC"])].copy()
            hist_df["effective_date"] = pd.to_datetime(hist_df["effective_date"])
            hist_df = hist_df.sort_values(["utility", "effective_date"])

            # --- All-in rate timeline chart ---
            fig_hist = go.Figure()
            colors_hist = {"DEP": "#e15759", "DEC": "#4e79a7"}
            for util in ["DEP", "DEC"]:
                sub = hist_df[hist_df["utility"] == util]
                if sub.empty:
                    continue
                label = "NC Progress (DEP)" if util == "DEP" else "NC Carolinas (DEC)"
                fig_hist.add_trace(go.Scatter(
                    x=sub["effective_date"],
                    y=sub["all_in_cents_per_kwh"],
                    mode="lines+markers",
                    name=f"{label} — All-in",
                    line=dict(color=colors_hist[util], width=2),
                    marker=dict(size=6),
                ))
                fig_hist.add_trace(go.Scatter(
                    x=sub["effective_date"],
                    y=sub["base_cents_per_kwh"],
                    mode="lines",
                    name=f"{label} — Base only",
                    line=dict(color=colors_hist[util], width=1, dash="dot"),
                    opacity=0.6,
                ))

            fig_hist.update_layout(
                title="Duke Energy NC Residential Rate History (All-in ¢/kWh)",
                xaxis_title="Effective date",
                yaxis_title="¢/kWh",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                height=380,
                margin=dict(t=80, b=40),
            )
            st.plotly_chart(fig_hist, use_container_width=True)

            # --- Escalation summary ---
            dep_rows = hist_df[hist_df["utility"] == "DEP"].sort_values("effective_date")
            if len(dep_rows) >= 2:
                first_rate = dep_rows.iloc[0]["all_in_cents_per_kwh"]
                last_rate  = dep_rows.iloc[-1]["all_in_cents_per_kwh"]
                first_date = dep_rows.iloc[0]["effective_date"].strftime("%b %Y")
                last_date  = dep_rows.iloc[-1]["effective_date"].strftime("%b %Y")
                pct_change = (last_rate - first_rate) / first_rate * 100
                col_h1, col_h2, col_h3 = st.columns(3)
                col_h1.metric(f"DEP rate ({first_date})", f"{first_rate:.2f} ¢/kWh")
                col_h2.metric(f"DEP rate ({last_date})", f"{last_rate:.2f} ¢/kWh",
                              delta=f"{last_rate - first_rate:+.2f} ¢/kWh")
                col_h3.metric("Total change", f"{pct_change:+.1f}%")

            # --- Your bill at historical rates ---
            st.markdown("#### Your monthly bill at historical rates")
            st.caption(
                f"Bill estimate for **{kwh:,.0f} kWh/month** at each historical rate period. "
                "Uses blended (summer/winter average) all-in rate. "
                "Fixed customer charge (~$14/month) is not included in the historical rider data — "
                "add ~$14 for a full bill estimate."
            )

            hist_bill_rows = []
            for _, row in hist_df.iterrows():
                rate_cpkwh = row["all_in_cents_per_kwh"]
                energy_cost = round(kwh * rate_cpkwh / 100, 2)
                # Approximate fixed charge (not in historical rate data)
                fixed_approx = 14.0
                total_approx = round(energy_cost + fixed_approx, 2)
                hist_bill_rows.append({
                    "Effective date": row["effective_date"].strftime("%Y-%m-%d"),
                    "Utility": "NC Progress" if row["utility"] == "DEP" else "NC Carolinas",
                    "All-in ¢/kWh": round(rate_cpkwh, 3),
                    "Base ¢/kWh": round(row["base_cents_per_kwh"], 3),
                    "Rider ¢/kWh": round(row["rider_cents_per_kwh"], 3),
                    f"Energy cost ({kwh:,.0f} kWh)": f"${energy_cost:,.2f}",
                    "Est. total (+ ~$14 fixed)": f"${total_approx:,.2f}",
                })

            hist_bill_df = pd.DataFrame(hist_bill_rows)
            st.dataframe(hist_bill_df, use_container_width=True, hide_index=True)

            # --- Bill escalation bar chart ---
            dep_bill = hist_df[hist_df["utility"] == "DEP"].copy()
            if not dep_bill.empty:
                dep_bill["energy_cost"] = round(dep_bill["all_in_cents_per_kwh"] * kwh / 100, 2)
                dep_bill["total_est"] = dep_bill["energy_cost"] + 14.0

                fig_bill_hist = go.Figure()
                fig_bill_hist.add_trace(go.Bar(
                    x=dep_bill["effective_date"].dt.strftime("%Y-%m"),
                    y=dep_bill["energy_cost"],
                    name="Energy charges",
                    marker_color="#e15759",
                ))
                fig_bill_hist.add_trace(go.Bar(
                    x=dep_bill["effective_date"].dt.strftime("%Y-%m"),
                    y=[14.0] * len(dep_bill),
                    name="Fixed charge (~$14)",
                    marker_color="#4e79a7",
                ))
                # Overlay current sidebar rate
                current_total = res_result.total if res_result else None
                if current_total:
                    fig_bill_hist.add_hline(
                        y=current_total,
                        line_dash="dash", line_color="#59a14f",
                        annotation_text=f"Current (tariff engine): ${current_total:.2f}",
                        annotation_position="top right",
                    )
                fig_bill_hist.update_layout(
                    barmode="stack",
                    title=f"DEP Estimated Monthly Bill at {kwh:,.0f} kWh — Historical Rates",
                    xaxis_title="Rate effective date",
                    yaxis_title="$ / month",
                    xaxis=dict(tickangle=-45),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                    height=380,
                    margin=dict(t=80, b=60),
                )
                st.plotly_chart(fig_bill_hist, use_container_width=True)

            # --- If ESPI data uploaded: overlay actual monthly bills on rate timeline ---
            if _parsed_profile is not None and len(_parsed_profile.months) >= 3:
                st.markdown("#### Actual monthly bills from uploaded usage data")
                st.caption(
                    "Each month's actual kWh multiplied by the prevailing all-in rate at that time. "
                    "Fixed charge (~$14) is added. This is an approximation — "
                    "use the Rate Comparison tab for exact tariff-engine billing."
                )

                # Build a rate lookup: for each month in ESPI data, find the prevailing DEP rate
                dep_rates = hist_df[hist_df["utility"] == "DEP"].sort_values("effective_date")
                actual_bill_rows = []
                for m in sorted(_parsed_profile.months, key=lambda x: (x.year, x.month)):
                    month_date = pd.Timestamp(m.year, m.month, 1)
                    # Find the most recent rate effective on or before this month
                    applicable = dep_rates[dep_rates["effective_date"] <= month_date]
                    if applicable.empty:
                        continue
                    rate_row = applicable.iloc[-1]
                    rate_cpkwh = rate_row["all_in_cents_per_kwh"]
                    energy_cost = round(m.total_kwh * rate_cpkwh / 100, 2)
                    est_total = round(energy_cost + 14.0, 2)
                    actual_bill_rows.append({
                        "Month": f"{m.year}-{m.month:02d}",
                        "Actual kWh": round(m.total_kwh, 0),
                        "Rate (¢/kWh)": round(rate_cpkwh, 3),
                        "Energy cost": f"${energy_cost:,.2f}",
                        "Est. total": f"${est_total:,.2f}",
                    })

                if actual_bill_rows:
                    actual_df = pd.DataFrame(actual_bill_rows)
                    st.dataframe(actual_df, use_container_width=True, hide_index=True)

            st.markdown("""
    **Notes:**
    - Rate data from NCUC tariff leaves (DEP 2016–present, DEC partial coverage)
    - Rider data uses canonical summary from Leaf 600 (2023+) and provisional ingest (2016–2022)
    - Blended ¢/kWh = weighted average of summer/winter rates at a representative usage level
    - Fixed customer charge (~$14/month) is excluded from the historical rider data; added as an approximation
    - For exact billing use the **Rate Comparison** tab with the tariff engine
    """)
