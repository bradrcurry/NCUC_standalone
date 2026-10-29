"""Proposal research stays usable; unguarded accepted-lineage writes are refused."""

from __future__ import annotations

import sqlite3

import pytest

from duke_rates.document_intelligence.proposed_tariff_extractor import ensure_schema
from duke_rates.document_intelligence.proposed_tariff_promoter import (
    UnsafeProposalPromotionError,
    apply_promotion,
    plan_promotion,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tariff_families (
            family_key TEXT PRIMARY KEY,
            state TEXT,
            company TEXT,
            schedule_code TEXT,
            family_type TEXT,
            title TEXT,
            aliases_json TEXT,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_key TEXT,
            effective_start TEXT,
            docket_number TEXT,
            source_pdf TEXT,
            leaf_no TEXT,
            status TEXT,
            source_type TEXT,
            historical_document_id INTEGER,
            created_at TEXT,
            notes TEXT
        );
        CREATE TABLE tariff_charges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id INTEGER,
            charge_type TEXT,
            charge_label TEXT,
            rate_value REAL,
            rate_unit TEXT,
            source_snippet TEXT,
            notes TEXT
        );
        """
    )
    ensure_schema(conn)
    return conn


def _seed_dep_mrop_proposal(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        INSERT INTO tariff_families (family_key, state, company, schedule_code,
            family_type, title)
        VALUES ('nc-progress-leaf-661', 'NC', 'progress', 'RIDER_MROP_RY1',
                'rider', 'Meter-Related Optional Programs MROP');

        INSERT INTO proposed_tariff_documents
            (id, source_pdf, docket_number, utility, proposal_stage,
             source_record_id)
        VALUES (1, 'dep-1380.pdf', 'E-2 Sub 1380', 'Duke Energy Progress',
                'proposed', 7815);

        INSERT INTO proposed_tariff_blocks
            (id, proposed_document_id, source_pdf, start_page, end_page,
             exhibit_key, rate_year_context, tariff_name, tariff_kind,
             schedule_code, leaf_no, effective_start, confidence)
        VALUES
            (10, 1, 'dep-1380.pdf', 297, 297, 'B', 'Proposed Exhibit B',
             'RIDER MROP METER-RELATED OPTIONAL PROGRAMS', 'rider', 'MROP',
             661, '2027-01-01', 0.7);

        INSERT INTO proposed_tariff_charge_candidates
            (proposed_block_id, source_pdf, page_number, exhibit_key,
             rate_year_context, tariff_name, tariff_kind, charge_type,
             charge_label, rate_value, rate_unit, raw_line, confidence)
        VALUES
            (10, 'dep-1380.pdf', 297, 'B', 'Proposed Exhibit B',
             'RIDER MROP METER-RELATED OPTIONAL PROGRAMS', 'rider',
             'fixed', 'MRM Monthly Rate', 175.0, '$/month',
             'MRM Monthly Rate $175', 0.9);
        """
    )


def _seed_dep_pc_new_rider(conn: sqlite3.Connection) -> None:
    """Seed a new rider (PC) with NO matching family in tariff_families."""
    conn.executescript(
        """
        INSERT INTO proposed_tariff_documents
            (id, source_pdf, docket_number, utility, proposal_stage,
             source_record_id)
        VALUES (1, 'dep-1380.pdf', 'E-2 Sub 1380', 'Duke Energy Progress',
                'proposed', 7815);

        INSERT INTO proposed_tariff_blocks
            (id, proposed_document_id, source_pdf, start_page, end_page,
             exhibit_key, rate_year_context, tariff_name, tariff_kind,
             schedule_code, leaf_no, effective_start, confidence)
        VALUES (20, 1, 'dep-1380.pdf', 284, 284, 'B', 'Proposed Exhibit B',
                'RIDER PC PENSIONS COSTS', 'rider', 'PC', 614,
                '2027-01-01', 0.7);

        INSERT INTO proposed_tariff_charge_candidates
            (proposed_block_id, source_pdf, page_number, exhibit_key,
             rate_year_context, tariff_name, tariff_kind, charge_type,
             charge_label, rate_value, rate_unit, raw_line, confidence)
        VALUES (20, 'dep-1380.pdf', 284, 'B', 'Proposed Exhibit B',
                'RIDER PC PENSIONS COSTS', 'rider',
                'energy', 'kilowatt hour', 0.0, '$/kWh',
                '0.000¢ per kilowatt hour', 0.7);
        """
    )


def test_plan_promotion_marks_existing_family_as_actionable() -> None:
    conn = _conn()
    _seed_dep_mrop_proposal(conn)

    plan = plan_promotion(
        conn,
        docket_number="E-2 Sub 1380",
        utility="Duke Energy Progress",
        code_filter="MROP",
    )

    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.skip_reason is None
    assert action.family_key == "nc-progress-leaf-661"
    assert action.matched_existing_family is True
    assert action.effective_start == "2027-01-01"
    assert action.proposed_block_ids == [10]
    assert len(action.charges) == 1
    assert action.charges[0].rate_value == 175.0


def test_plan_promotion_skips_new_family_when_not_opted_in() -> None:
    conn = _conn()
    _seed_dep_pc_new_rider(conn)

    plan = plan_promotion(
        conn,
        docket_number="E-2 Sub 1380",
        utility="Duke Energy Progress",
        code_filter="PC",
        create_new_families=False,
    )

    assert len(plan.actions) == 1
    assert plan.actions[0].skip_reason is not None
    assert "create-new-families" in plan.actions[0].skip_reason


def test_plan_promotion_drafts_new_family_when_opted_in() -> None:
    conn = _conn()
    _seed_dep_pc_new_rider(conn)

    plan = plan_promotion(
        conn,
        docket_number="E-2 Sub 1380",
        utility="Duke Energy Progress",
        code_filter="PC",
        create_new_families=True,
    )

    action = plan.actions[0]
    assert action.skip_reason is None
    assert action.matched_existing_family is False
    assert action.family_to_create is not None
    assert action.family_to_create.family_key == "nc-progress-leaf-614"
    assert action.family_to_create.family_type == "rider"


def test_apply_promotion_refuses_unguarded_existing_family() -> None:
    conn = _conn()
    _seed_dep_mrop_proposal(conn)
    plan = plan_promotion(
        conn,
        docket_number="E-2 Sub 1380",
        utility="Duke Energy Progress",
        code_filter="MROP",
    )

    before = list(conn.iterdump())
    with pytest.raises(UnsafeProposalPromotionError, match="reviewed provenance promote"):
        apply_promotion(conn, plan)
    assert list(conn.iterdump()) == before
    assert plan.actionable[0].created_version_id is None
    assert plan.actionable[0].charges[0].rate_value == 175.0


def test_apply_promotion_refuses_unguarded_new_family() -> None:
    conn = _conn()
    _seed_dep_pc_new_rider(conn)
    plan = plan_promotion(
        conn,
        docket_number="E-2 Sub 1380",
        utility="Duke Energy Progress",
        code_filter="PC",
        create_new_families=True,
    )

    before = list(conn.iterdump())
    with pytest.raises(UnsafeProposalPromotionError):
        apply_promotion(conn, plan)
    assert list(conn.iterdump()) == before
    assert plan.actionable[0].family_to_create.family_key == "nc-progress-leaf-614"


def test_rejected_proposal_remains_available_for_research() -> None:
    conn = _conn()
    _seed_dep_mrop_proposal(conn)
    first = plan_promotion(
        conn,
        docket_number="E-2 Sub 1380",
        utility="Duke Energy Progress",
        code_filter="MROP",
    )
    with pytest.raises(UnsafeProposalPromotionError):
        apply_promotion(conn, first)

    second = plan_promotion(
        conn,
        docket_number="E-2 Sub 1380",
        utility="Duke Energy Progress",
        code_filter="MROP",
    )
    assert len(second.actions) == 1
    assert second.actionable[0].to_dict() == first.actionable[0].to_dict()
    assert conn.execute("SELECT COUNT(*) FROM tariff_versions").fetchone()[0] == 0
