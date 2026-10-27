"""Regression guards for the restored 12-bill DEP reconciliation baseline.

``tests/fixtures/billing_baseline/progress_nc_12_bills.json`` is written by
``bench/billing_baseline.py``. The offline tests check that the frozen baseline
is self-consistent against what each bill PDF prints, so a billing component
dropped from both the parsed bill and the estimate cannot hide behind a
matching delta. The live tests (``DUKE_RATES_TEST_LIVE_DB``) re-run the
reconciliation and require it to reproduce the frozen numbers.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from conftest import LIVE_DB_PATH, requires_live_db  # noqa: E402
from duke_rates.analytics.bill_accuracy import _comparison_status
from duke_rates.parse.normalization import parse_effective_date

REPO = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO / "tests" / "fixtures" / "billing_baseline" / "progress_nc_12_bills.json"
BILL_TEXT_DIR = REPO / "data" / "processed" / "bills"
BASELINE = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["bills"]
BY_DATE = {bill["bill_date"]: bill for bill in BASELINE}

# Components every DEP residential electric bill in the set prints.
REQUIRED_COMPONENTS = {
    "customer_charge",
    "energy_conservation_credit",
    "storm_recovery_charge",
    "summary_rider_adjustments",
    "clean_energy_rider",
}


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2024-10-01", date(2024, 10, 1)),
        ("October 1, 2024", date(2024, 10, 1)),
        ("October 2024", date(2024, 10, 1)),
        ("", None),
        (None, None),
    ],
)
def test_parse_effective_date_reads_every_stored_format(value, expected) -> None:
    # ISO is what normalize-nc-effective-dates writes; dropping it silently
    # re-priced Apr-Sep 2025 bills at the 2025-10-01 rate.
    assert parse_effective_date(value) == expected


def test_line_item_fallback_total_is_never_a_match() -> None:
    # The 2026-03-20 bill matched at -0.01 while both sides omitted $31.99,
    # because the "actual" was a sum of the lines that happened to be parsed.
    assert _comparison_status(rate_code="R-TOU-CPP", total_delta=-0.01, actual_total=None) == "needs_review"
    assert _comparison_status(rate_code="RES", total_delta=0.0, actual_total=None) == "needs_review"
    assert _comparison_status(rate_code="RES", total_delta=-0.45, actual_total=188.78) == "good_match"
    assert _comparison_status(rate_code="RES", total_delta=-7.26, actual_total=223.61) == "poor_match"


def test_baseline_covers_all_twelve_bills() -> None:
    assert len(BASELINE) == 12
    assert [b["rate_code"] for b in BASELINE].count("R-TOU-CPP") == 1


@pytest.mark.parametrize("bill", BASELINE, ids=lambda b: b["bill_date"])
def test_baseline_actual_is_the_printed_total(bill) -> None:
    # Two independent printed figures must agree: the electric section's
    # "Total Current Charges" and the front page's "Current Electric Charges".
    assert bill["printed_electric_total"] is not None
    assert bill["printed_electric_total"] == bill["front_page_current_electric_charges"]
    actual_sum = round(sum(c["actual"] or 0.0 for c in bill["components"].values()), 2)
    assert actual_sum == pytest.approx(bill["printed_electric_total"], abs=0.005)


@pytest.mark.parametrize("bill", BASELINE, ids=lambda b: b["bill_date"])
def test_baseline_estimate_prices_every_billed_component(bill) -> None:
    components = bill["components"]
    assert REQUIRED_COMPONENTS <= set(components)
    for key, component in components.items():
        assert component["actual"] is not None, f"{key}: estimated but not billed"
        assert component["estimated"] is not None, f"{key}: billed but not estimated"
    estimated_sum = round(sum(c["estimated"] for c in components.values()), 2)
    assert estimated_sum == pytest.approx(bill["estimated_electric_total"], abs=0.005)
    assert bill["total_delta"] == pytest.approx(
        bill["printed_electric_total"] - bill["estimated_electric_total"], abs=0.005
    )
    assert abs(bill["total_delta"]) <= 0.45


def test_baseline_march_2026_tou_bill_includes_page_four_charges() -> None:
    bill = BY_DATE["2026-03-20"]
    assert bill["printed_electric_total"] == 177.97
    assert bill["components"]["storm_recovery_charge"]["actual"] == 4.93
    assert bill["components"]["summary_rider_adjustments"]["actual"] == 27.06
    assert bill["estimated_electric_total"] == pytest.approx(177.98, abs=0.005)


def test_baseline_base_tariff_selection() -> None:
    # Apr-Sep 2025 service is priced from the 2024-10-01 leaf-500 sheet
    # (12.119 c/kWh), not the 2025-10-01 current sheet (12.623 c/kWh).
    for bill_date in ("2025-04-21", "2025-05-20", "2025-06-19", "2025-07-21", "2025-08-20", "2025-09-19"):
        base = BY_DATE[bill_date]["base_tariff"]
        assert (base["family_key"], base["source_kind"], base["effective_start"]) == (
            "nc-progress-leaf-500", "historical", "2024-10-01",
        )
    # Current-site documents are keyed by URL path: doc 343 is the leaf-500
    # RES sheet effective 2025-10-01, doc 346 the leaf-503 R-TOU-CPP sheet.
    for bill_date in ("2025-11-19", "2025-12-18", "2026-01-21", "2026-02-18"):
        base = BY_DATE[bill_date]["base_tariff"]
        assert (base["source_kind"], base["document_id"]) == ("current", 343)
        assert "leaf-no-500" in base["family_key"]
    base = BY_DATE["2026-03-20"]["base_tariff"]
    assert (base["source_kind"], base["document_id"]) == ("current", 346)
    assert "leaf-no-503" in base["family_key"]


def test_baseline_records_observed_history_components() -> None:
    # Storm and Clean Energy are often priced from other saved bills, so a
    # match on them is not independent evidence; the baseline must say so.
    sources = {
        c["estimate_source"] for bill in BASELINE for c in bill["components"].values()
    }
    assert sources <= {"tariff", "observed_history", "same_bill_observed"}
    for bill in BASELINE:
        for key in ("customer_charge", "energy_charge", "summary_rider_adjustments"):
            if key in bill["components"]:
                assert bill["components"][key]["estimate_source"] == "tariff", (bill["bill_date"], key)


# ---------------------------------------------------------------------------
# Live: reproduce the frozen baseline from a DB snapshot
# ---------------------------------------------------------------------------


@requires_live_db
def test_live_stored_effective_dates_are_readable() -> None:
    conn = sqlite3.connect(f"file:{LIVE_DB_PATH}?mode=ro", uri=True)
    try:
        for table in ("historical_documents", "tariff_versions"):
            for column in ("effective_start", "effective_end"):
                values = conn.execute(
                    f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"
                ).fetchall()
                unreadable = [row for row in values if parse_effective_date(row[1]) is None]
                assert not unreadable, f"{table}.{column}: {unreadable[:5]}"
    finally:
        conn.close()


@requires_live_db
@pytest.mark.skipif(not BILL_TEXT_DIR.exists(), reason="bill raw text not available locally")
def test_live_reconciliation_reproduces_frozen_baseline() -> None:
    spec = importlib.util.spec_from_file_location("billing_baseline", REPO / "bench" / "billing_baseline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fresh = {bill["bill_date"]: bill for bill in module.build_baseline(LIVE_DB_PATH, BILL_TEXT_DIR)}

    assert set(fresh) == set(BY_DATE)
    for bill_date, frozen in BY_DATE.items():
        bill = fresh[bill_date]
        assert bill["base_tariff"] == frozen["base_tariff"], bill_date
        assert bill["printed_electric_total"] == frozen["printed_electric_total"], bill_date
        assert bill["estimated_electric_total"] == pytest.approx(frozen["estimated_electric_total"], abs=0.005), bill_date
        assert set(bill["components"]) == set(frozen["components"]), bill_date
        for key, component in frozen["components"].items():
            assert bill["components"][key]["estimated"] == pytest.approx(component["estimated"], abs=0.005), (bill_date, key)
