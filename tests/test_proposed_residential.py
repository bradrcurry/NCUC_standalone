from __future__ import annotations

from pathlib import Path

import pytest

from duke_rates.analytics.proposed_residential import (
    _add_rate_year_2_carried_forward_riders,
    _validated_residential_rider_value,
    load_proposed_residential_rider_stack,
)
from duke_rates.db.sqlite import connect
from duke_rates.document_intelligence.proposed_tariff_extractor import ensure_schema


def _seed_summary_db(path: Path) -> None:
    conn = connect(str(path))
    ensure_schema(conn)
    conn.executescript(
        """
        INSERT INTO proposed_tariff_documents
            (id, source_pdf, docket_number, utility, proposal_stage)
        VALUES (1, 'dep.pdf', 'E-2 Sub 1380', 'Duke Energy Progress', 'proposed');
        INSERT INTO proposed_tariff_blocks
            (id, proposed_document_id, source_pdf, start_page, end_page,
             exhibit_key, rate_year_context, tariff_name, tariff_kind, confidence,
             effective_start)
        VALUES
            (10, 1, 'dep.pdf', 274, 274, 'B', 'Proposed Exhibit B',
             'SUMMARY OF RIDER ADJUSTMENTS', 'rider_summary', 0.85, '2027-01-01');
        INSERT INTO proposed_tariff_charge_candidates
            (proposed_block_id, source_pdf, page_number, exhibit_key,
             rate_year_context, tariff_name, tariff_kind, charge_type,
             charge_label, rate_value, rate_unit, raw_line, confidence)
        VALUES
            (10, 'dep.pdf', 274, 'B', 'B', 'SUMMARY OF RIDER ADJUSTMENTS',
             'rider_summary', 'adjustment',
             'Demand Side Management DSM & EE Rate [Residential Service Schedules]',
             0.00767, '$/kWh', 'x', 0.8),
            (10, 'dep.pdf', 274, 'B', 'B', 'SUMMARY OF RIDER ADJUSTMENTS',
             'rider_summary', 'adjustment',
             'Annual Billing Adjustments Rider BA - Net Adjustment [Residential Service Schedules]',
             0.01347, '$/kWh', 'x', 0.8),
            (10, 'dep.pdf', 274, 'B', 'B', 'SUMMARY OF RIDER ADJUSTMENTS',
             'rider_summary', 'adjustment',
             'Production Tax Credit Rider PTC [Residential Service Schedules]',
             -0.00101, '$/kWh', 'x', 0.8),
            (10, 'dep.pdf', 274, 'B', 'B', 'SUMMARY OF RIDER ADJUSTMENTS',
             'rider_summary', 'adjustment',
             'Fuel Rate [Small General Service Schedules]',
             0.005, '$/kWh', 'x', 0.8),
            (10, 'dep.pdf', 274, 'B', 'B', 'SUMMARY OF RIDER ADJUSTMENTS',
             'rider_summary', 'rider_total',
             'TOTAL cents/kWh [Residential Service Schedules]',
             0.01768, '$/kWh', 'x', 0.8);
        """
    )
    conn.commit()
    conn.close()


def _record(
    *,
    utility: str = "DEP",
    rider_code: str,
    rate_value: float | None,
    rate_unit: str | None = "$/kWh",
    raw_line: str = "",
    charge_label: str = "",
) -> dict[str, object]:
    return {
        "utility": utility,
        "rider_code": rider_code,
        "rate_value": rate_value,
        "rate_unit": rate_unit,
        "raw_line": raw_line,
        "charge_label": charge_label,
    }


def test_validated_residential_rider_value_turns_parenthesized_decrement_negative() -> None:
    value, status, _reason = _validated_residential_rider_value(
        _record(
            rider_code="PTC",
            rate_value=0.00101,
            raw_line=(
                "The current approved decremental rate, including regulatory fees, "
                "is (0.101¢) per kilowatt-hour."
            ),
        )
    )

    assert status == "included"
    assert value == -0.00101


def test_validated_residential_rider_value_includes_zero_pension_cost_rider() -> None:
    value, status, _reason = _validated_residential_rider_value(
        _record(
            rider_code="PC",
            rate_value=0.0,
            raw_line="including revenue-related taxes and regulatory fees is 0.000¢ per kilowatt hour.",
        )
    )

    assert status == "included"
    assert value == 0.0


def test_validated_residential_rider_value_uses_only_total_bpm_prospective_decrement() -> None:
    component_value, component_status, _reason = _validated_residential_rider_value(
        _record(
            rider_code="BPM-P",
            rate_value=0.00002,
            raw_line="Prospective Rider amounts be set at decrements of 0.002¢/kWh for BPM Net Revenues",
        )
    )
    total_value, total_status, _reason = _validated_residential_rider_value(
        _record(
            rider_code="BPM-P",
            rate_value=0.00005,
            raw_line="for NFPTP Transmission Revenues for a total decrement of 0.005¢/kWh including regulatory fee.",
        )
    )

    assert component_status == "excluded"
    assert component_value is None
    assert total_status == "included"
    assert total_value == -0.00005


def test_validated_residential_rider_value_excludes_optional_or_ambiguous_riders() -> None:
    value, status, reason = _validated_residential_rider_value(
        _record(
            rider_code="SS",
            rate_value=0.006,
            raw_line="0.6 cents per kWh of Incremental Load for the Incentive Margin",
        )
    )

    assert status == "excluded"
    assert value is None
    assert "Optional" in reason


def test_validated_residential_rider_value_excludes_non_kwh_units() -> None:
    value, status, _reason = _validated_residential_rider_value(
        _record(
            rider_code="MROP",
            rate_value=30.0,
            rate_unit="$/month",
            raw_line="$0.79 per month | $30.00",
        )
    )

    assert status == "excluded"
    assert value is None


def test_rate_year_2_riders_are_explicitly_carried_forward_when_values_are_missing() -> None:
    import pandas as pd

    riders = pd.DataFrame(
        [
            {
                "utility": "DEP",
                "exhibit_key": "B_1",
                "rate_year_context": "Rate Year 1",
                "effective_date": pd.to_datetime("2027-01-01"),
                "scenario_label": "Proposed Rate Year 1",
                "scenario_order": 2,
                "rider_code": "PTC",
                "tariff_name": "RIDER PTC PRODUCTION TAX CREDITS",
                "start_page": 433,
                "rate_value": 0.00101,
                "rate_unit": "$/kWh",
                "validated_status": "included",
                "validated_rate_value": -0.00101,
                "validated_cents_per_kwh": -0.101,
                "validated_dollars": -1.01,
                "validated_reason": "parsed",
                "projection_basis": "parsed",
            }
        ]
    )

    result = _add_rate_year_2_carried_forward_riders(pd, riders, 1000)

    carried = result[result["scenario_label"] == "Proposed Rate Year 2"].iloc[0]
    assert carried["rider_code"] == "PTC"
    assert carried["effective_date"] == pd.to_datetime("2028-01-01")
    assert carried["validated_status"] == "included"
    assert carried["validated_rate_value"] == -0.00101
    assert carried["validated_dollars"] == -1.01
    assert carried["projection_basis"] == "carried_forward_from_rate_year_1"
    assert "carrying forward" in carried["validated_reason"]


def test_proposed_rider_stack_itemizes_drops_subtotal_and_carries_forward(tmp_path) -> None:
    db = tmp_path / "stack.db"
    _seed_summary_db(db)
    stack = load_proposed_residential_rider_stack(database_path=db, representative_kwh=1000)
    assert not stack.empty

    dep = stack[stack["utility"] == "DEP"]
    labels = set(dep["rider_label"])
    # Residential group only — the Small General Service "Fuel Rate" row is excluded.
    assert "Fuel Rate" not in labels
    # The redundant BA Net Adjustment subtotal and the TOTAL line are dropped.
    assert not any("Net Adjustment" in lbl for lbl in labels)
    assert not any("TOTAL" in lbl.upper() for lbl in labels)
    # Itemized residential riders are kept, including the credit.
    assert "Demand Side Management DSM & EE Rate" in labels
    ptc = dep[dep["rider_label"] == "Production Tax Credit Rider PTC"]
    assert float(ptc.iloc[0]["cents_per_kwh"]) == pytest.approx(-0.101)

    # Rate Year 2 (B_2) has no summary sheet, so the latest stack carries forward.
    ry2 = dep[dep["scenario_order"] == 3]
    assert not ry2.empty
    assert (ry2["projection_basis"] == "carried_forward_from_rate_year_1").all()
