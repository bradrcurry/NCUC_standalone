from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from duke_rates.analytics.canonical_residential import (
    load_canonical_residential_timeline,
)
from duke_rates.analytics.dep_progress import DEFAULT_KWH, _require_pandas
from duke_rates.db.sqlite import connect


UTILITY_NAMES = {
    "DEP": "Duke Energy Progress",
    "DEC": "Duke Energy Carolinas",
}

RESIDENTIAL_SCHEDULES = {
    "DEP": "RES",
    "DEC": "RS",
}

_MAIN_ENERGY_LABELS = {
    "DEP": ("kwh for all kwh",),
    "DEC": ("energy charge per month, per kwh",),
}

_RESIDENTIAL_PROPOSED_RIDER_CODES = {
    "DEP": {"PC", "PTC", "BPM-P", "RAL-3"},
    "DEC": {"PC", "PTC", "RAL-2"},
}

_OPTIONAL_OR_AMBIGUOUS_RIDER_CODES = {
    "FCAR",
    "MROP",
    "NFS",
    "RECD",
    "SS",
}

_NEGATIVE_RIDER_TERMS = (
    "decrement",
    "decremental",
    "credit",
)


def load_proposed_residential_comparison(
    *,
    database_path: Path | None = None,
    representative_kwh: float = DEFAULT_KWH,
    rider_basis: str = "summary_total",
):
    """Return latest accepted rows plus proposed residential scenarios.

    ``rider_basis`` selects how the proposed all-in rider layer is built:

    * ``"summary_total"`` (default) — use the utility's own ``TOTAL cents/kWh``
      line from the Summary of Rider Adjustments sheet, i.e. the full proposed
      rider stack (fuel, EE/DSM, EDIT, decoupling, the new riders, …). This is
      apples-to-apples with the accepted all-in, which also carries every
      rider.
    * ``"validated_subset"`` — the conservative allow-list of individually
      validated new riders only (PC/PTC/RAL/BPM-P). Understates the all-in but
      every layered value is sign- and applicability-checked.
    """
    pd = _require_pandas()
    accepted = _latest_accepted_rows(
        pd,
        database_path=database_path,
        representative_kwh=representative_kwh,
    )
    proposed = _proposed_base_rows(
        pd,
        database_path=database_path,
        representative_kwh=representative_kwh,
    )
    if rider_basis == "summary_total":
        proposed = _apply_summary_rider_totals(
            pd,
            proposed,
            database_path=database_path,
            representative_kwh=representative_kwh,
        )
    else:
        proposed = _apply_validated_riders(
            pd,
            proposed,
            database_path=database_path,
            representative_kwh=representative_kwh,
        )
    frames = [df for df in (accepted, proposed) if not df.empty]
    if not frames:
        return pd.DataFrame()
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values(["utility", "effective_date", "scenario_order", "source_status"])
        .reset_index(drop=True)
    )


def _residential_summary_totals(pd, *, database_path: Path | None):
    """Return the residential-group ``TOTAL cents/kWh`` per (utility, exhibit).

    Reads the ``rider_total`` charge rows the extractor persists for each
    Summary of Rider Adjustments page and keeps only the residential schedule
    group (DEC ``Residential Schedules RS,...``; DEP ``Residential Service
    Schedules``).
    """
    db_path = _database_path(database_path)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT d.utility AS utility_name, b.exhibit_key, c.rate_value, c.raw_line
            FROM proposed_tariff_charge_candidates c
            JOIN proposed_tariff_blocks b ON b.id = c.proposed_block_id
            JOIN proposed_tariff_documents d ON d.id = b.proposed_document_id
            WHERE c.charge_type = 'rider_total'
              AND c.charge_label LIKE '%[Residential%'
            """
        ).fetchall()
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()
    records = []
    for row in rows:
        record = dict(row)
        records.append(
            {
                "utility": _utility_code(record["utility_name"]),
                "exhibit_key": str(record["exhibit_key"] or ""),
                "summary_rider_value": float(record["rate_value"]),
                "summary_rider_cents": float(record["rate_value"]) * 100.0,
            }
        )
    return pd.DataFrame(records)


def _apply_summary_rider_totals(
    pd,
    proposed,
    *,
    database_path: Path | None,
    representative_kwh: float,
):
    """Layer the residential summary TOTAL into proposed all-in pricing.

    Each proposed base row is matched to its rate year's residential summary
    total by ``(utility, exhibit_key)``. Rate years whose tariff copy omits a
    summary sheet (DEC/DEP Rate Year 2) carry the latest available total
    forward, mirroring how the validated-rider path projects RY2.
    """
    if proposed.empty:
        return proposed
    totals = _residential_summary_totals(pd, database_path=database_path)
    if totals.empty:
        # No summary totals parsed yet — fall back to the validated subset so
        # the dashboard still shows something rather than base-only.
        return _apply_validated_riders(
            pd,
            proposed,
            database_path=database_path,
            representative_kwh=representative_kwh,
        )

    carried_basis: dict[str, float] = {}
    out = proposed.copy()
    for idx, row in out.iterrows():
        utility = row["utility"]
        exhibit = str(row.get("exhibit_key") or "")
        match = totals[(totals["utility"] == utility) & (totals["exhibit_key"] == exhibit)]
        if not match.empty:
            cents = float(match.iloc[0]["summary_rider_cents"])
            value = float(match.iloc[0]["summary_rider_value"])
            carried_basis[utility] = cents
            coverage = "summary_total"
        elif utility in carried_basis:
            cents = carried_basis[utility]
            value = cents / 100.0
            coverage = "summary_total_carried_forward"
        else:
            continue
        out.at[idx, "rider_cents_per_kwh"] = cents
        out.at[idx, "all_in_cents_per_kwh"] = float(row["base_cents_per_kwh"]) + cents
        out.at[idx, "all_in_bill_amount"] = float(row["base_bill_amount"]) + value * float(
            representative_kwh
        )
        out.at[idx, "proposed_base_only"] = False
        out.at[idx, "proposed_rider_coverage"] = coverage
    return out


def load_proposed_rider_summary(
    *,
    database_path: Path | None = None,
    representative_kwh: float = DEFAULT_KWH,
):
    """Return proposed rider blocks and charge candidates for dashboard display."""
    pd = _require_pandas()
    db_path = _database_path(database_path)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                d.utility AS utility_name,
                d.docket_number,
                b.exhibit_key,
                b.rate_year_context,
                COALESCE(
                    b.effective_start,
                    CASE WHEN b.exhibit_key = 'B_2' THEN '2028-01-01' ELSE '2027-01-01' END
                ) AS effective_date,
                b.schedule_code AS rider_code,
                b.tariff_name,
                b.start_page,
                b.confidence AS block_confidence,
                c.charge_type,
                c.charge_label,
                c.rate_value,
                c.rate_unit,
                c.raw_line,
                c.confidence AS charge_confidence
            FROM proposed_tariff_blocks b
            JOIN proposed_tariff_documents d ON d.id = b.proposed_document_id
            LEFT JOIN proposed_tariff_charge_candidates c ON c.proposed_block_id = b.id
            WHERE b.tariff_kind = 'rider'
            ORDER BY d.utility, b.exhibit_key, b.start_page, b.schedule_code, c.id
            """
        ).fetchall()
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()

    records = []
    for row in rows:
        record = dict(row)
        record["utility"] = _utility_code(record.pop("utility_name"))
        record["effective_date"] = pd.to_datetime(record["effective_date"], errors="coerce")
        record["scenario_label"] = _scenario_label(
            str(record.get("exhibit_key") or ""),
            str(record.get("rate_year_context") or ""),
        )
        record["scenario_order"] = _scenario_order(str(record.get("exhibit_key") or ""))
        record["is_new_rider"] = _is_new_rider(
            record.get("utility"),
            str(record.get("rider_code") or ""),
            str(record.get("tariff_name") or ""),
        )
        value, status, reason = _validated_residential_rider_value(record)
        record["validated_status"] = status
        record["validated_reason"] = reason
        record["validated_rate_value"] = value
        record["validated_cents_per_kwh"] = (
            value * 100.0 if value is not None and record.get("rate_unit") == "$/kWh" else None
        )
        record["validated_dollars"] = (
            value * float(representative_kwh)
            if value is not None and record.get("rate_unit") == "$/kWh"
            else None
        )
        record["projection_basis"] = "parsed"
        records.append(record)
    df = pd.DataFrame(records)
    if df.empty:
        return df
    return _add_rate_year_2_carried_forward_riders(pd, df, representative_kwh)


def _latest_accepted_rows(
    pd,
    *,
    database_path: Path | None,
    representative_kwh: float,
):
    timeline = load_canonical_residential_timeline(
        database_path=database_path,
        representative_kwh=representative_kwh,
    )
    if timeline.empty:
        return pd.DataFrame()
    latest = (
        timeline.sort_values("effective_date")
        .groupby("utility", as_index=False)
        .tail(1)
        .copy()
    )
    latest["source_status"] = "accepted"
    latest["scenario_label"] = "Latest accepted"
    latest["scenario_order"] = 0
    latest["proposed_base_only"] = False
    latest["docket_number"] = None
    latest["source_page"] = None
    latest["parser_confidence"] = None
    latest["proposed_rider_coverage"] = "accepted_current"
    latest["proposed_rider_count"] = None
    return latest[
        [
            "utility",
            "schedule",
            "effective_date",
            "representative_kwh",
            "base_cents_per_kwh",
            "rider_cents_per_kwh",
            "all_in_cents_per_kwh",
            "base_bill_amount",
            "all_in_bill_amount",
            "fixed_monthly_charge",
            "source_status",
            "scenario_label",
            "scenario_order",
            "proposed_base_only",
            "docket_number",
            "source_pdf",
            "source_page",
            "parser_confidence",
            "proposed_rider_coverage",
            "proposed_rider_count",
        ]
    ]


def _proposed_base_rows(
    pd,
    *,
    database_path: Path | None,
    representative_kwh: float,
):
    db_path = _database_path(database_path)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                d.utility AS utility_name,
                d.docket_number,
                d.source_pdf,
                b.id AS block_id,
                b.exhibit_key,
                b.rate_year_context,
                COALESCE(
                    b.effective_start,
                    CASE WHEN b.exhibit_key = 'B_2' THEN '2028-01-01' ELSE '2027-01-01' END
                ) AS effective_date,
                b.schedule_code,
                b.tariff_name,
                b.start_page,
                b.confidence,
                b.block_json,
                c.charge_type,
                c.charge_label,
                c.rate_value,
                c.rate_unit,
                c.raw_line
            FROM proposed_tariff_blocks b
            JOIN proposed_tariff_documents d ON d.id = b.proposed_document_id
            LEFT JOIN proposed_tariff_charge_candidates c ON c.proposed_block_id = b.id
            WHERE b.tariff_kind = 'schedule'
              AND (
                    (d.utility = 'Duke Energy Progress' AND b.schedule_code = 'RES')
                 OR (d.utility = 'Duke Energy Carolinas' AND b.schedule_code = 'RS')
              )
            ORDER BY d.utility, b.exhibit_key, b.start_page, c.id
            """
        ).fetchall()
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        utility = _utility_code(record["utility_name"])
        if utility not in RESIDENTIAL_SCHEDULES:
            continue
        key = (
            utility,
            str(record["exhibit_key"] or ""),
            str(record["effective_date"] or ""),
        )
        item = grouped.setdefault(
            key,
            {
                "utility": utility,
                "schedule": RESIDENTIAL_SCHEDULES[utility],
                "effective_date": record["effective_date"],
                "representative_kwh": float(representative_kwh),
                "docket_number": record["docket_number"],
                "source_pdf": record["source_pdf"],
                "source_page": record["start_page"],
                "exhibit_key": record["exhibit_key"],
                "rate_year_context": record["rate_year_context"],
                "scenario_label": _scenario_label(
                    str(record["exhibit_key"] or ""),
                    str(record["rate_year_context"] or ""),
                ),
                "scenario_order": _scenario_order(str(record["exhibit_key"] or "")),
                "parser_confidence": record["confidence"],
                "fixed_monthly_charge": None,
                "main_energy_dollars_per_kwh": None,
            },
        )
        if item["fixed_monthly_charge"] is None:
            item["fixed_monthly_charge"] = _fixed_from_record(record)
        if item["main_energy_dollars_per_kwh"] is None:
            item["main_energy_dollars_per_kwh"] = _main_energy_from_record(
                utility,
                record,
            )

    records = []
    for item in grouped.values():
        energy = item.pop("main_energy_dollars_per_kwh")
        fixed = item.get("fixed_monthly_charge")
        if energy is None:
            continue
        fixed_value = float(fixed or 0.0)
        base_bill = fixed_value + float(energy) * float(representative_kwh)
        item["base_bill_amount"] = base_bill
        item["base_cents_per_kwh"] = base_bill / float(representative_kwh) * 100.0
        item["rider_cents_per_kwh"] = None
        item["all_in_cents_per_kwh"] = None
        item["all_in_bill_amount"] = None
        item["source_status"] = "proposed"
        item["proposed_base_only"] = True
        item["proposed_rider_coverage"] = "base_only_pending_validation"
        item["proposed_rider_count"] = 0
        records.append(item)

    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df["effective_date"] = pd.to_datetime(df["effective_date"], errors="coerce")
    return df[
        [
            "utility",
            "schedule",
            "effective_date",
            "representative_kwh",
            "base_cents_per_kwh",
            "rider_cents_per_kwh",
            "all_in_cents_per_kwh",
            "base_bill_amount",
            "all_in_bill_amount",
            "fixed_monthly_charge",
            "source_status",
            "scenario_label",
            "scenario_order",
            "exhibit_key",
            "proposed_base_only",
            "docket_number",
            "source_pdf",
            "source_page",
            "parser_confidence",
            "proposed_rider_coverage",
            "proposed_rider_count",
        ]
    ]


def _apply_validated_riders(
    pd,
    proposed,
    *,
    database_path: Path | None,
    representative_kwh: float,
):
    if proposed.empty:
        return proposed
    riders = load_proposed_rider_summary(
        database_path=database_path,
        representative_kwh=representative_kwh,
    )
    if riders.empty or "validated_status" not in riders.columns:
        return proposed
    proposed = _mark_catalog_only_rider_scenarios(pd, proposed, riders)
    included = riders[riders["validated_status"] == "included"].copy()
    if included.empty:
        return proposed

    included["effective_date"] = pd.to_datetime(included["effective_date"], errors="coerce")
    rider_totals = (
        included.groupby(["utility", "effective_date", "scenario_order"], dropna=False)
        .agg(
            rider_cents_per_kwh=("validated_cents_per_kwh", "sum"),
            rider_dollars=("validated_dollars", "sum"),
            proposed_rider_count=("validated_rate_value", "count"),
            projection_basis=("projection_basis", _combine_projection_basis),
        )
        .reset_index()
    )
    merged = proposed.merge(
        rider_totals,
        how="left",
        on=["utility", "effective_date", "scenario_order"],
        suffixes=("", "_validated"),
    )
    has_riders = merged["proposed_rider_count_validated"].notna()
    merged.loc[has_riders, "rider_cents_per_kwh"] = merged.loc[
        has_riders, "rider_cents_per_kwh_validated"
    ]
    merged.loc[has_riders, "all_in_cents_per_kwh"] = (
        merged.loc[has_riders, "base_cents_per_kwh"]
        + merged.loc[has_riders, "rider_cents_per_kwh_validated"]
    )
    merged.loc[has_riders, "all_in_bill_amount"] = (
        merged.loc[has_riders, "base_bill_amount"] + merged.loc[has_riders, "rider_dollars"]
    )
    merged.loc[has_riders, "proposed_base_only"] = False
    merged.loc[has_riders, "proposed_rider_coverage"] = "partial_validated"
    carried = has_riders & (merged["projection_basis"] == "carried_forward_from_rate_year_1")
    if carried.any():
        merged.loc[carried, "proposed_rider_coverage"] = "projected_riders_carried_forward"
    merged.loc[has_riders, "proposed_rider_count"] = merged.loc[
        has_riders, "proposed_rider_count_validated"
    ]
    drop_cols = [
        "rider_cents_per_kwh_validated",
        "rider_dollars",
        "proposed_rider_count_validated",
        "projection_basis",
    ]
    return merged.drop(columns=[col for col in drop_cols if col in merged.columns])


def _combine_projection_basis(values) -> str:
    values = {str(value) for value in values if value}
    if "carried_forward_from_rate_year_1" in values:
        return "carried_forward_from_rate_year_1"
    return "parsed"


def _add_rate_year_2_carried_forward_riders(pd, riders, representative_kwh: float):
    """Create explicitly labeled RY2 rider projections when tariff pages omit values.

    The proposed application repeats or catalogs these residential riders in the
    rate-case materials, but the Rate Year 2 tariff blocks currently do not carry
    parseable charge rows. We carry forward the latest validated Rate Year 1
    residential rider by utility/code so the dashboard can show a projected all-in
    view without pretending the RY2 rider value was directly parsed.
    """
    included = riders[riders["validated_status"] == "included"].copy()
    if included.empty:
        return riders
    b2_existing = included[included["scenario_order"] == 3]
    additions = []
    for utility, utility_rows in included.groupby("utility", dropna=False):
        if utility is None:
            continue
        if not b2_existing[b2_existing["utility"] == utility].empty:
            continue
        prior = utility_rows[utility_rows["scenario_order"] < 3].copy()
        if prior.empty:
            continue
        prior = prior.sort_values(["scenario_order", "start_page"]).groupby("rider_code", as_index=False).tail(1)
        for _, row in prior.iterrows():
            carried = row.copy()
            carried["exhibit_key"] = "B_2"
            carried["rate_year_context"] = "Rate Year 2"
            carried["effective_date"] = pd.to_datetime("2028-01-01")
            carried["scenario_label"] = "Proposed Rate Year 2"
            carried["scenario_order"] = 3
            carried["validated_reason"] = (
                "Projected by carrying forward the latest validated proposed rider value; "
                "Rate Year 2 tariff pages did not include a parseable replacement value."
            )
            carried["projection_basis"] = "carried_forward_from_rate_year_1"
            value = carried.get("validated_rate_value")
            carried["validated_cents_per_kwh"] = value * 100.0 if value is not None else None
            carried["validated_dollars"] = (
                value * float(representative_kwh) if value is not None else None
            )
            additions.append(carried)
    if not additions:
        return riders
    return pd.concat([riders, pd.DataFrame(additions)], ignore_index=True)


def _mark_catalog_only_rider_scenarios(pd, proposed, riders):
    catalog = riders[riders["validated_status"] == "catalog_only"].copy()
    if catalog.empty:
        return proposed
    catalog["effective_date"] = pd.to_datetime(catalog["effective_date"], errors="coerce")
    catalog_counts = (
        catalog.groupby(["utility", "effective_date", "scenario_order"], dropna=False)
        .agg(proposed_catalog_rider_count=("rider_code", "nunique"))
        .reset_index()
    )
    merged = proposed.merge(
        catalog_counts,
        how="left",
        on=["utility", "effective_date", "scenario_order"],
    )
    has_catalog = merged["proposed_catalog_rider_count"].fillna(0) > 0
    no_values = merged["proposed_rider_count"].fillna(0) == 0
    mark = has_catalog & no_values
    merged.loc[mark, "proposed_rider_coverage"] = "catalog_only_no_values"
    merged.loc[mark, "proposed_rider_count"] = 0
    return merged.drop(columns=["proposed_catalog_rider_count"])


def _validated_residential_rider_value(
    record: dict[str, Any],
) -> tuple[float | None, str, str]:
    utility = record.get("utility")
    code = str(record.get("rider_code") or "").upper()
    value = record.get("rate_value")
    unit = record.get("rate_unit")
    raw_line = str(record.get("raw_line") or "")
    text = f"{record.get('charge_label') or ''} {raw_line}".lower()

    if value is None:
        return None, "catalog_only", "No parsed charge candidate on this rider block."
    if unit != "$/kWh":
        return None, "excluded", "Only per-kWh proposed rider charges are layered."
    if code in _OPTIONAL_OR_AMBIGUOUS_RIDER_CODES:
        return None, "excluded", "Optional, non-residential, or class-ambiguous rider."
    if code not in _RESIDENTIAL_PROPOSED_RIDER_CODES.get(str(utility), set()):
        return None, "excluded", "Not in the conservative residential rider allow-list."
    if code == "BPM-P" and "total decrement" not in text:
        return None, "excluded", "Component decrement excluded to avoid double-counting the total."

    signed = float(value)
    if any(term in text for term in _NEGATIVE_RIDER_TERMS) or "(" in raw_line:
        signed = -abs(signed)
    return signed, "included", "Layered into proposed residential all-in as a validated per-kWh rider."


def _fixed_from_record(record: dict[str, Any]) -> float | None:
    if (
        record.get("charge_type") == "fixed"
        and record.get("rate_unit") == "$/month"
        and record.get("rate_value") is not None
    ):
        return float(record["rate_value"])
    try:
        block_json = json.loads(str(record.get("block_json") or "{}"))
    except Exception:
        return None
    value = block_json.get("basic_customer_charge")
    if value in {None, ""}:
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _main_energy_from_record(utility: str, record: dict[str, Any]) -> float | None:
    if record.get("charge_type") != "energy":
        return None
    if record.get("rate_unit") != "$/kWh":
        return None
    label = str(record.get("charge_label") or "").lower()
    raw_line = str(record.get("raw_line") or "").lower()
    candidates = _MAIN_ENERGY_LABELS.get(utility, ())
    if not any(token in label or token in raw_line for token in candidates):
        return None
    value = record.get("rate_value")
    if value is None:
        return None
    return float(value)


def _scenario_label(exhibit_key: str, context: str) -> str:
    if exhibit_key == "B_1":
        return "Proposed Rate Year 1"
    if exhibit_key == "B_2":
        return "Proposed Rate Year 2"
    if "rate year 0" in context.lower():
        return "Proposed Flat Alternative"
    return "Proposed Exhibit B"


def _scenario_order(exhibit_key: str) -> int:
    return {"B": 1, "B_1": 2, "B_2": 3}.get(exhibit_key, 9)


def _is_new_rider(utility: str | None, rider_code: str, tariff_name: str) -> bool:
    code = rider_code.upper()
    name = tariff_name.upper()
    if utility == "DEP":
        return code in {"PC", "PTC", "BPM", "BPM-P", "RAL-3"} or any(
            token in name
            for token in (
                "PENSIONS COSTS",
                "PRODUCTION TAX CREDITS",
                "BULK POWER MARKETING",
                "REGULATORY ASSET AND LIABILITY",
            )
        )
    if utility == "DEC":
        return code in {"PC", "PTC", "RAL-2"}
    return False


def _utility_code(utility_name: str | None) -> str | None:
    for code, name in UTILITY_NAMES.items():
        if utility_name == name:
            return code
    return utility_name


def _database_path(database_path: Path | None) -> Path:
    if database_path is not None:
        return Path(database_path)
    from duke_rates.config import get_settings

    return get_settings().database_path
