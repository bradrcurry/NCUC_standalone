"""Rider-family trust scoring for DEP and DEC schedules.

Produces a canonical trust table: one row per (utility, rider_code, effective_date,
rate_class_group) with a numeric ``trust_score`` (0.0 – 1.0) and human-readable
``trust_tier``.

Trust Scoring Model
-------------------
Each row starts at 0.0 and accumulates points across four dimensions:

    source_quality   (0.40 max)
        0.40 — clean_leaf600 (Leaf 600 clean summary, highest confidence)
        0.20 — provisional_ingest (pre-2023 DEP ingest, moderate confidence)

    date_completeness (0.25 max)
        0.25 — rider_effective_date is populated
        0.00 — rider_effective_date is missing (NaT / NULL)

    bill_support      (0.25 max)
        0.25 — this (rider_code, period) appears in at least one validated bill
        0.00 — no bill-backed evidence

    continuity        (0.10 max)
        0.10 — no timeline gap detected for this rider_code in the prior period
        0.00 — gap detected (rider code disappeared and reappeared)

Trust Tiers
-----------
    high        ≥ 0.80
    medium      ≥ 0.50
    low         ≥ 0.25
    unverified  < 0.25
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from duke_rates.analytics.canonical_rider_components import (
    load_dec_rs_canonical_rider_components,
    load_dep_lgs_canonical_rider_components,
    load_dep_mgs_d_canonical_rider_components,
    load_dep_mgs_nd_canonical_rider_components,
    load_dep_res_canonical_rider_components,
    load_dep_sgs_canonical_rider_components,
    load_dep_sgs_clr_canonical_rider_components,
)
from duke_rates.analytics.dep_progress import _connect, _require_pandas


# Weights must sum to 1.0
_WEIGHT_SOURCE = 0.40
_WEIGHT_DATE = 0.25
_WEIGHT_BILL = 0.25
_WEIGHT_CONTINUITY = 0.10

_SCORE_SOURCE = {
    "clean_leaf600": _WEIGHT_SOURCE,
    "provisional_ingest": _WEIGHT_SOURCE * 0.50,
}

_TIER_THRESHOLDS = [
    (0.80, "high"),
    (0.50, "medium"),
    (0.25, "low"),
    (0.00, "unverified"),
]


def load_rider_trust_table(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    """Return a trust-scored DataFrame for DEP and DEC schedules.

    Covers DEP residential (RES/R-TOU/R-TOUD), DEP small general service
    (SGS/SGS-TOUE), DEP SGS-TOU-CLR, DEP medium general service (MGS non-demand
    and demand), DEP large general service (LGS/LGS-TOU), and DEC residential (RS).

    Columns
    -------
    utility : str
    rate_class_group : str  — 'dep_residential' | 'dep_sgs' | 'dep_sgs_clr' | 'dep_mgs_nd' | 'dep_mgs_d' | 'dep_lgs' | 'dec_residential'
    rider_code : str
    effective_date : datetime
    rider_effective_date : datetime | NaT
    cents_per_kwh : float
    source_kind : str
    source_score : float
    date_score : float
    bill_score : float
    continuity_score : float
    trust_score : float  — weighted sum of all four dimensions
    trust_tier : str     — 'high' | 'medium' | 'low' | 'unverified'
    """
    pd = _require_pandas()

    # Load all four source frames and tag with utility + rate_class_group
    source_specs = [
        ("DEP", "dep_residential", load_dep_res_canonical_rider_components),
        ("DEP", "dep_sgs", load_dep_sgs_canonical_rider_components),
        ("DEP", "dep_sgs_clr", load_dep_sgs_clr_canonical_rider_components),
        ("DEP", "dep_mgs_nd", load_dep_mgs_nd_canonical_rider_components),
        ("DEP", "dep_mgs_d", load_dep_mgs_d_canonical_rider_components),
        ("DEP", "dep_lgs", load_dep_lgs_canonical_rider_components),
        ("DEC", "dec_residential", load_dec_rs_canonical_rider_components),
    ]

    frames = []
    for utility, group, loader in source_specs:
        frame = loader(database_path=database_path, start_date=start_date, end_date=end_date)
        if not frame.empty:
            frame = frame.copy()
            frame["utility"] = utility
            frame["rate_class_group"] = group
            frames.append(frame)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)

    # --- Source quality score ---
    df["source_score"] = df["source_kind"].map(_SCORE_SOURCE).fillna(0.0)

    # --- Date completeness score ---
    df["date_score"] = df["rider_effective_date"].notna().astype(float) * _WEIGHT_DATE

    # --- Bill support score ---
    # Key is (utility, rider_code) — SGS riders earn bill_support if they appear
    # in any validated bill block for that utility.
    bill_supported = _load_bill_supported_rider_periods(database_path=database_path)
    df["_bill_key"] = list(zip(df["utility"], df["rider_code"]))
    df["bill_score"] = df["_bill_key"].isin(bill_supported).astype(float) * _WEIGHT_BILL
    df = df.drop(columns=["_bill_key"])

    # --- Continuity score: scoped per (utility, rate_class_group, rider_code) ---
    df = _add_continuity_score(df, pd)

    # --- Composite trust score and tier ---
    df["trust_score"] = (
        df["source_score"]
        + df["date_score"]
        + df["bill_score"]
        + df["continuity_score"]
    ).round(4)
    df["trust_tier"] = df["trust_score"].map(_score_to_tier)

    return (
        df.sort_values(["utility", "rate_class_group", "rider_code", "effective_date"])
        .reset_index(drop=True)
    )


def export_rider_trust_table(
    output_path: Path,
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
) -> Path:
    """Write the trust table as a CSV and return the path."""
    df = load_rider_trust_table(
        database_path=database_path, start_date=start_date, end_date=end_date
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    return output_path


def trust_summary(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
) -> dict[str, Any]:
    """Return a compact summary dict suitable for a QA handoff report."""
    pd = _require_pandas()
    df = load_rider_trust_table(
        database_path=database_path, start_date=start_date, end_date=end_date
    )
    if df.empty:
        return {"total_rows": 0}

    tier_counts = df["trust_tier"].value_counts().to_dict()
    by_group = (
        df.groupby("rate_class_group")["trust_tier"]
        .value_counts()
        .unstack(fill_value=0)
        .to_dict()
    )

    high_confidence_codes = sorted(
        df[df["trust_tier"] == "high"]["rider_code"].unique().tolist()
    )
    unverified_codes = sorted(
        df[df["trust_tier"] == "unverified"]["rider_code"].unique().tolist()
    )
    mean_by_rider = (
        df.groupby(["utility", "rate_class_group", "rider_code"])["trust_score"]
        .mean()
        .round(3)
        .reset_index()
        .rename(columns={"trust_score": "mean_trust_score"})
        .to_dict(orient="records")
    )

    return {
        "total_rows": len(df),
        "tier_counts": tier_counts,
        "by_group": by_group,
        "high_confidence_rider_codes": high_confidence_codes,
        "unverified_rider_codes": unverified_codes,
        "mean_trust_score_by_rider": mean_by_rider,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_bill_supported_rider_periods(
    database_path: Path | None,
) -> set[tuple[str, str]]:
    """Return set of (utility, rider_code) pairs backed by parsed bill documents.

    A rider_code is considered "bill-supported" if it appears in
    ``rider_line_items`` rows that are tagged with a known ``utility`` value
    in the parent ``rider_summary_blocks`` row.  These summary blocks are
    derived from official Duke tariff filings, making them the closest
    available proxy for bill-backed evidence in the DB.
    """
    try:
        with _connect(database_path) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT b.utility, li.rider_code
                FROM rider_line_items li
                JOIN rider_summary_blocks b ON li.block_id = b.id
                WHERE li.rider_code IS NOT NULL
                  AND b.utility IS NOT NULL
                  AND li.is_section_header = 0
                  AND li.is_subtotal = 0
                  AND li.is_total = 0
                  AND li.cents_per_kwh IS NOT NULL
                """
            ).fetchall()
    except Exception:
        return set()

    return {(row["utility"], row["rider_code"]) for row in rows}


def _add_continuity_score(df, pd) -> Any:
    """Add a continuity_score column: 0.10 if rider was present in prior period.

    Continuity is scoped per (utility, rate_class_group, rider_code) so that
    gaps in SGS coverage do not penalise residential rows for the same rider
    code, and vice versa.
    """
    scores = []
    for (utility, rate_class_group, rider_code), group in df.groupby(
        ["utility", "rate_class_group", "rider_code"]
    ):
        sorted_dates = group["effective_date"].sort_values().tolist()
        for date in sorted_dates:
            idx = sorted_dates.index(date)
            if idx == 0:
                # First appearance — treat as continuous (no prior gap to detect)
                scores.append((utility, rate_class_group, rider_code, date, _WEIGHT_CONTINUITY))
            else:
                prev = sorted_dates[idx - 1]
                delta_months = (
                    (date.year - prev.year) * 12 + (date.month - prev.month)
                )
                # Flag a gap if more than 6 months between consecutive appearances
                has_gap = delta_months > 6
                scores.append(
                    (utility, rate_class_group, rider_code, date, 0.0 if has_gap else _WEIGHT_CONTINUITY)
                )

    continuity_df = pd.DataFrame(
        scores,
        columns=["utility", "rate_class_group", "rider_code", "effective_date", "continuity_score"],
    )
    return df.merge(
        continuity_df,
        on=["utility", "rate_class_group", "rider_code", "effective_date"],
        how="left",
    ).fillna({"continuity_score": _WEIGHT_CONTINUITY})


def _score_to_tier(score: float) -> str:
    for threshold, tier in _TIER_THRESHOLDS:
        if score >= threshold:
            return tier
    return "unverified"
