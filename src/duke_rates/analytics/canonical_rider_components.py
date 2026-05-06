"""Canonical rider-component history for DEP and DEC schedules.

This module unifies three source kinds into a single per-component DataFrame
with a ``source_kind`` discriminator column:

    clean_leaf600
        Component rows from ``rider_line_items`` joined to ``rider_summary_blocks``
        (Leaf 600 clean summary; highest confidence).
        DEP: 2023-10+ ; DEC: 2018-08+

    provisional_ingest
        Component rows from ``dep_provisional_rider_components``
        (older DEP periods before clean Leaf 600 coverage).
        DEP: 2016-12 – 2022-12

Entry points
------------
``load_dep_res_canonical_rider_components()``
    DEP residential (RES + R-TOU + R-TOUD) — all share the
    "Residential Service Schedules" Leaf 600 summary page.

``load_dec_rs_canonical_rider_components()``
    DEC residential RS — clean_leaf600 only.

``load_dec_gs_canonical_rider_components()``
    DEC general service (SGS, LGS, ES) — "General Service Schedules"
    Leaf 600 summary page.  clean_leaf600 only (2018-08+).

``load_dec_industrial_canonical_rider_components()``
    DEC industrial (I) — "Industrial Schedules" Leaf 600 summary page.
    clean_leaf600 only (2018-08+).

``load_dep_sgs_canonical_rider_components()``
    DEP small general service (SGS + SGS-TOUE) — "Small General Service
    Schedules" Leaf 600 summary page.

``load_dep_sgs_clr_canonical_rider_components()``
    DEP SGS-TOU-CLR — "Small General Service - Constant Load Schedule"
    Leaf 600 summary page.

``load_dep_mgs_nd_canonical_rider_components()``
    DEP non-demand medium general service (MGS / MGS-TOU non-demand variant) —
    "Non-Demand: Medium General Service Schedules" Leaf 600 summary page.

``load_dep_mgs_d_canonical_rider_components()``
    DEP demand medium general service (MGS / MGS-TOU demand variant) —
    "Demand: Medium General Service Schedules" Leaf 600 summary page.

``load_dep_lgs_canonical_rider_components()``
    DEP large general service (LGS / LGS-TOU) — "Large General Service Schedules"
    Leaf 600 summary page.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from duke_rates.analytics.dep_progress import _connect, _require_pandas


_DEP_RESIDENTIAL_RATE_CLASS = "Residential Service Schedules"
_DEC_RESIDENTIAL_RATE_CLASS = "Residential Schedules"
_DEC_GENERAL_SERVICE_RATE_CLASS = "General Service Schedules"
_DEC_INDUSTRIAL_RATE_CLASS = "Industrial Schedules"
_DEP_SGS_RATE_CLASS = "Small General Service Schedules"
_DEP_SGS_CLR_RATE_CLASS = "Small General Service - Constant Load Schedule"
_DEP_MGS_ND_RATE_CLASS = "Non-Demand: Medium General Service Schedules"
_DEP_MGS_D_RATE_CLASS = "Demand: Medium General Service Schedules"
_DEP_LGS_RATE_CLASS = "Large General Service Schedules"


def load_dep_res_canonical_rider_components(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    """Return a unified per-component DataFrame for DEP RES riders.

    Columns
    -------
    effective_date : datetime
        The sheet-level effective date (when this rider total applied).
    rider_code : str
        Rider identifier (e.g. 'BA-DSM', 'CPRE').
    rider_effective_date : datetime | NaT
        Component-level effective date when this specific rider rate changed.
        May differ from ``effective_date``.  NaT when not available.
    cents_per_kwh : float
        Rate in cents per kWh.
    source_kind : str
        ``'clean_leaf600'`` or ``'provisional_ingest'``.
    source_pdf : str | None
    docket_dir : str | None
    """
    pd = _require_pandas()

    clean_rows = _load_clean_leaf600_components(
        database_path=database_path,
        rate_class=_DEP_RESIDENTIAL_RATE_CLASS,
        utility="DEP",
        start_date=start_date,
        end_date=end_date,
    )
    provisional_rows = _load_provisional_ingest_components(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )

    _COLUMNS = ["effective_date", "rider_code", "rider_effective_date",
                "cents_per_kwh", "source_kind", "source_pdf", "docket_dir"]
    if not clean_rows and not provisional_rows:
        return pd.DataFrame(columns=_COLUMNS)

    frames = []
    if clean_rows:
        frames.append(pd.DataFrame(clean_rows))
    if provisional_rows:
        frames.append(pd.DataFrame(provisional_rows))

    df = pd.concat(frames, ignore_index=True)
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce", format="mixed")
    return df.sort_values(["effective_date", "rider_code"]).reset_index(drop=True)


def load_dec_rs_canonical_rider_components(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    """Return a per-component DataFrame for DEC RS riders (clean_leaf600 only).

    Columns are identical to ``load_dep_res_canonical_rider_components``.
    """
    pd = _require_pandas()

    _COLUMNS = ["effective_date", "rider_code", "rider_effective_date",
                "cents_per_kwh", "source_kind", "source_pdf", "docket_dir"]
    rows = _load_clean_leaf600_components(
        database_path=database_path,
        rate_class=_DEC_RESIDENTIAL_RATE_CLASS,
        utility="DEC",
        start_date=start_date,
        end_date=end_date,
    )
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)

    df = pd.DataFrame(rows)
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce", format="mixed")
    return df.sort_values(["effective_date", "rider_code"]).reset_index(drop=True)


def load_dec_gs_canonical_rider_components(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    """Return a per-component DataFrame for DEC General Service riders.

    Source: ``rider_summary_blocks`` rate_class = "General Service Schedules"
    (covers SGS, LGS, ES schedules — they share the same Leaf 600 summary page).
    Only ``clean_leaf600`` data is available (2018-08+).

    Columns are identical to ``load_dep_res_canonical_rider_components``.
    """
    pd = _require_pandas()

    _COLUMNS = ["effective_date", "rider_code", "rider_effective_date",
                "cents_per_kwh", "source_kind", "source_pdf", "docket_dir"]
    rows = _load_clean_leaf600_components(
        database_path=database_path,
        rate_class=_DEC_GENERAL_SERVICE_RATE_CLASS,
        utility="DEC",
        start_date=start_date,
        end_date=end_date,
    )
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)

    df = pd.DataFrame(rows)
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce", format="mixed")
    return df.sort_values(["effective_date", "rider_code"]).reset_index(drop=True)


def load_dec_industrial_canonical_rider_components(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    """Return a per-component DataFrame for DEC Industrial (Schedule I) riders.

    Source: ``rider_summary_blocks`` rate_class = "Industrial Schedules".
    Only ``clean_leaf600`` data is available (2018-08+).

    Columns are identical to ``load_dep_res_canonical_rider_components``.
    """
    pd = _require_pandas()

    _COLUMNS = ["effective_date", "rider_code", "rider_effective_date",
                "cents_per_kwh", "source_kind", "source_pdf", "docket_dir"]
    rows = _load_clean_leaf600_components(
        database_path=database_path,
        rate_class=_DEC_INDUSTRIAL_RATE_CLASS,
        utility="DEC",
        start_date=start_date,
        end_date=end_date,
    )
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)

    df = pd.DataFrame(rows)
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce", format="mixed")
    return df.sort_values(["effective_date", "rider_code"]).reset_index(drop=True)


def load_dep_sgs_canonical_rider_components(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    """Return a per-component DataFrame for DEP SGS / SGS-TOUE riders.

    Source: ``rider_summary_blocks`` rate_class = "Small General Service Schedules"
    (covers SGS and SGS-TOUE, which share the same Leaf 600 summary page).
    Only ``clean_leaf600`` data is available — no provisional path exists for SGS.

    Columns are identical to ``load_dep_res_canonical_rider_components``.
    """
    pd = _require_pandas()

    _COLUMNS = ["effective_date", "rider_code", "rider_effective_date",
                "cents_per_kwh", "source_kind", "source_pdf", "docket_dir"]
    rows = _load_clean_leaf600_components(
        database_path=database_path,
        rate_class=_DEP_SGS_RATE_CLASS,
        utility="DEP",
        start_date=start_date,
        end_date=end_date,
    )
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)

    df = pd.DataFrame(rows)
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce", format="mixed")
    return df.sort_values(["effective_date", "rider_code"]).reset_index(drop=True)


def load_dep_sgs_clr_canonical_rider_components(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    """Return a per-component DataFrame for DEP SGS-TOU-CLR riders.

    Source: ``rider_summary_blocks`` rate_class =
    "Small General Service - Constant Load Schedule".
    Only ``clean_leaf600`` data is available.

    Columns are identical to ``load_dep_res_canonical_rider_components``.
    """
    pd = _require_pandas()

    _COLUMNS = ["effective_date", "rider_code", "rider_effective_date",
                "cents_per_kwh", "source_kind", "source_pdf", "docket_dir"]
    rows = _load_clean_leaf600_components(
        database_path=database_path,
        rate_class=_DEP_SGS_CLR_RATE_CLASS,
        utility="DEP",
        start_date=start_date,
        end_date=end_date,
    )
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)

    df = pd.DataFrame(rows)
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce", format="mixed")
    return df.sort_values(["effective_date", "rider_code"]).reset_index(drop=True)


def load_dep_mgs_nd_canonical_rider_components(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    """Return a per-component DataFrame for DEP non-demand MGS / MGS-TOU riders.

    Source: ``rider_summary_blocks`` rate_class =
    "Non-Demand: Medium General Service Schedules".
    Only ``clean_leaf600`` data is available (2023-10+).

    Columns are identical to ``load_dep_res_canonical_rider_components``.
    """
    pd = _require_pandas()

    _COLUMNS = ["effective_date", "rider_code", "rider_effective_date",
                "cents_per_kwh", "source_kind", "source_pdf", "docket_dir"]
    rows = _load_clean_leaf600_components(
        database_path=database_path,
        rate_class=_DEP_MGS_ND_RATE_CLASS,
        utility="DEP",
        start_date=start_date,
        end_date=end_date,
    )
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)

    df = pd.DataFrame(rows)
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce", format="mixed")
    return df.sort_values(["effective_date", "rider_code"]).reset_index(drop=True)


def load_dep_mgs_d_canonical_rider_components(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    """Return a per-component DataFrame for DEP demand MGS / MGS-TOU riders.

    Source: ``rider_summary_blocks`` rate_class =
    "Demand: Medium General Service Schedules".
    Only ``clean_leaf600`` data is available (2023-10+).

    Columns are identical to ``load_dep_res_canonical_rider_components``.
    """
    pd = _require_pandas()

    _COLUMNS = ["effective_date", "rider_code", "rider_effective_date",
                "cents_per_kwh", "source_kind", "source_pdf", "docket_dir"]
    rows = _load_clean_leaf600_components(
        database_path=database_path,
        rate_class=_DEP_MGS_D_RATE_CLASS,
        utility="DEP",
        start_date=start_date,
        end_date=end_date,
    )
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)

    df = pd.DataFrame(rows)
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce", format="mixed")
    return df.sort_values(["effective_date", "rider_code"]).reset_index(drop=True)


def load_dep_lgs_canonical_rider_components(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    """Return a per-component DataFrame for DEP LGS / LGS-TOU riders.

    Source: ``rider_summary_blocks`` rate_class =
    "Large General Service Schedules".
    Only ``clean_leaf600`` data is available (2023-10+).

    Columns are identical to ``load_dep_res_canonical_rider_components``.
    """
    pd = _require_pandas()

    _COLUMNS = ["effective_date", "rider_code", "rider_effective_date",
                "cents_per_kwh", "source_kind", "source_pdf", "docket_dir"]
    rows = _load_clean_leaf600_components(
        database_path=database_path,
        rate_class=_DEP_LGS_RATE_CLASS,
        utility="DEP",
        start_date=start_date,
        end_date=end_date,
    )
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)

    df = pd.DataFrame(rows)
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df["rider_effective_date"] = pd.to_datetime(df["rider_effective_date"], errors="coerce", format="mixed")
    return df.sort_values(["effective_date", "rider_code"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_clean_leaf600_components(
    *,
    database_path: Path | None,
    rate_class: str,
    utility: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    with _connect(database_path) as conn:
        rows = conn.execute(
            """
            SELECT b.effective_date, b.source_pdf, b.docket_dir,
                   li.rider_code, li.cents_per_kwh, li.line_effective_date
            FROM rider_line_items li
            JOIN rider_summary_blocks b ON li.block_id = b.id
            WHERE b.rate_class = ?
              AND b.utility = ?
              AND b.effective_date IS NOT NULL
              AND b.effective_date BETWEEN ? AND ?
              AND li.is_section_header = 0
              AND li.is_subtotal = 0
              AND li.is_total = 0
              AND li.rider_code IS NOT NULL
              AND li.cents_per_kwh IS NOT NULL
              AND ABS(li.cents_per_kwh) <= 5.0
            ORDER BY b.effective_date, li.id
            """,
            (rate_class, utility, start_date, end_date),
        ).fetchall()

    # Dedup: for each (effective_date, rider_code), keep the row from the best
    # block (most recently inserted block for that date, i.e., highest block id).
    # This mirrors the deduplication done in load_dep_res_rider_history.
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["effective_date"], row["rider_code"])
        if key not in seen:
            seen[key] = {
                "effective_date": row["effective_date"],
                "rider_code": row["rider_code"],
                "rider_effective_date": row["line_effective_date"],
                "cents_per_kwh": row["cents_per_kwh"],
                "source_kind": "clean_leaf600",
                "source_pdf": row["source_pdf"],
                "docket_dir": row["docket_dir"],
            }
    return list(seen.values())


def _load_provisional_ingest_components(
    *,
    database_path: Path | None,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    with _connect(database_path) as conn:
        rows = conn.execute(
            """
            SELECT effective_date, rider_code, rider_effective_date,
                   cents_per_kwh, source_pdf, docket_dir
            FROM dep_provisional_rider_components
            WHERE effective_date IS NOT NULL
              AND effective_date BETWEEN ? AND ?
            ORDER BY effective_date, rider_code
            """,
            (start_date, end_date),
        ).fetchall()

    return [
        {
            "effective_date": row["effective_date"],
            "rider_code": row["rider_code"],
            "rider_effective_date": row["rider_effective_date"],
            "cents_per_kwh": row["cents_per_kwh"],
            "source_kind": "provisional_ingest",
            "source_pdf": row["source_pdf"],
            "docket_dir": row["docket_dir"],
        }
        for row in rows
    ]
