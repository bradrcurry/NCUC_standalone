"""Tests for the read-only proposed-vs-approved comparison helpers."""

from __future__ import annotations

import sqlite3

import pytest

from duke_rates.document_intelligence.proposed_tariff_extractor import ensure_schema
from duke_rates.document_intelligence.proposed_vs_approved import (
    _extract_code_token,
    _strip_rider_prefix,
    build_comparisons,
    match_family,
    utility_to_company,
)


def _make_conn() -> sqlite3.Connection:
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
            title TEXT
        );
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_key TEXT,
            effective_start TEXT,
            status TEXT
        );
        CREATE TABLE tariff_charges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id INTEGER,
            charge_type TEXT,
            charge_label TEXT,
            rate_value REAL,
            rate_unit TEXT
        );
        """
    )
    ensure_schema(conn)
    return conn


def test_extract_code_token_handles_rider_and_schedule_prefixes() -> None:
    assert _extract_code_token("RIDER PC PENSIONS COSTS") == "PC"
    assert _extract_code_token("SCHEDULE LGS-TOU") == "LGS-TOU"
    assert _extract_code_token("RIDER BPM-P BPM PROSPECTIVE") == "BPM-P"
    assert _extract_code_token("RESIDENTIAL SERVICE SCHEDULE RES") is None


def test_strip_rider_prefix_returns_descriptive_tail() -> None:
    assert _strip_rider_prefix("RIDER PC PENSIONS COSTS") == "PENSIONS COSTS"
    assert _strip_rider_prefix("SCHEDULE LGS-TOU") == ""


def test_utility_to_company_normalizes_known_aliases() -> None:
    assert utility_to_company("Duke Energy Progress") == "progress"
    assert utility_to_company("DEC") == "carolinas"
    assert utility_to_company(None) is None
    assert utility_to_company("Acme Power") is None


def test_match_family_matches_existing_dep_rider_by_code_suffix() -> None:
    conn = _make_conn()
    conn.executescript(
        """
        INSERT INTO tariff_families (family_key, state, company, schedule_code,
            family_type, title)
        VALUES
            ('nc-progress-leaf-661', 'NC', 'progress', 'RIDER_MROP_RY1', 'rider',
             'Meter-Related Optional Programs MROP'),
            ('nc-progress-leaf-640', 'NC', 'progress', 'RIDER_RECD', 'rider',
             'Residential Service Energy Conservation Discount Rider RECD');
        """
    )

    match = match_family(
        conn,
        tariff_kind="rider",
        schedule_code="MROP",
        tariff_name="RIDER MROP METER-RELATED OPTIONAL PROGRAMS",
        company="progress",
    )
    assert match is not None
    assert match.family_key == "nc-progress-leaf-661"
    assert match.match_strategy == "schedule_code_suffix_with_ry1"


def test_match_family_returns_none_for_new_proposed_rider_with_no_family() -> None:
    conn = _make_conn()
    match = match_family(
        conn,
        tariff_kind="rider",
        schedule_code="PC",
        tariff_name="RIDER PC PENSIONS COSTS",
        company="progress",
    )
    assert match is None


def test_build_comparisons_emits_one_row_per_tariff_with_proposed_charges() -> None:
    conn = _make_conn()
    conn.executescript(
        """
        INSERT INTO tariff_families (family_key, state, company, schedule_code,
            family_type, title)
        VALUES ('nc-progress-leaf-661', 'NC', 'progress', 'RIDER_MROP_RY1',
                'rider', 'Meter-Related Optional Programs MROP');
        INSERT INTO tariff_versions (id, family_key, effective_start, status)
        VALUES (5467, 'nc-progress-leaf-661', '2023-10-01', 'approved');
        INSERT INTO tariff_charges (version_id, charge_type, charge_label,
            rate_value, rate_unit)
        VALUES (5467, 'fixed', 'MRM Monthly Rate', 170.0, '$/month');

        INSERT INTO proposed_tariff_documents
            (id, source_pdf, docket_number, utility, proposal_stage)
        VALUES (1, 'dep-1380.pdf', 'E-2 Sub 1380', 'Duke Energy Progress',
                'proposed');
        INSERT INTO proposed_tariff_blocks
            (id, proposed_document_id, source_pdf, start_page, end_page,
             exhibit_key, rate_year_context, tariff_name, tariff_kind,
             schedule_code, confidence)
        VALUES
            (10, 1, 'dep-1380.pdf', 297, 297, 'B', 'Proposed Exhibit B',
             'RIDER MROP METER-RELATED OPTIONAL PROGRAMS', 'rider', 'MROP', 0.7),
            (11, 1, 'dep-1380.pdf', 298, 298, 'B', 'Proposed Exhibit B',
             'RIDER MROP METER-RELATED OPTIONAL PROGRAMS', 'rider', 'MROP', 0.7);
        INSERT INTO proposed_tariff_charge_candidates
            (proposed_block_id, source_pdf, page_number, exhibit_key,
             rate_year_context, tariff_name, tariff_kind, charge_type,
             charge_label, rate_value, rate_unit, raw_line, confidence)
        VALUES
            (10, 'dep-1380.pdf', 297, 'B', 'Proposed Exhibit B',
             'RIDER MROP METER-RELATED OPTIONAL PROGRAMS', 'rider',
             'fixed', 'MRM Monthly Rate', 175.0, '$/month', 'MRM Monthly Rate $175', 0.9);
        """
    )

    comparisons = build_comparisons(
        conn,
        docket_number="E-2 Sub 1380",
        utility="Duke Energy Progress",
    )
    assert len(comparisons) == 1
    c = comparisons[0]
    assert c.tariff_name == "RIDER MROP METER-RELATED OPTIONAL PROGRAMS"
    assert c.pages == [297, 298]
    assert len(c.proposed_charges) == 1
    assert c.proposed_charges[0].rate_value == 175.0
    assert c.family_match is not None
    assert c.family_match.family_key == "nc-progress-leaf-661"
    assert c.approved_version_id == 5467
    assert c.approved_effective_start == "2023-10-01"
    assert len(c.approved_charges) == 1
    assert c.approved_charges[0].rate_value == 170.0


def test_build_comparisons_marks_new_proposed_rider_as_unmatched() -> None:
    conn = _make_conn()
    conn.executescript(
        """
        INSERT INTO proposed_tariff_documents
            (id, source_pdf, docket_number, utility, proposal_stage)
        VALUES (1, 'dep-1380.pdf', 'E-2 Sub 1380', 'Duke Energy Progress',
                'proposed');
        INSERT INTO proposed_tariff_blocks
            (id, proposed_document_id, source_pdf, start_page, end_page,
             exhibit_key, rate_year_context, tariff_name, tariff_kind,
             schedule_code, confidence)
        VALUES (20, 1, 'dep-1380.pdf', 284, 284, 'B', 'Proposed Exhibit B',
                'RIDER PC PENSIONS COSTS', 'rider', 'PC', 0.7);
        INSERT INTO proposed_tariff_charge_candidates
            (proposed_block_id, source_pdf, page_number, exhibit_key,
             rate_year_context, tariff_name, tariff_kind, charge_type,
             charge_label, rate_value, rate_unit, raw_line, confidence)
        VALUES (20, 'dep-1380.pdf', 284, 'B', 'Proposed Exhibit B',
                'RIDER PC PENSIONS COSTS', 'rider',
                'energy', 'kilowatt hour', 0.0, '$/kWh', '0.000¢ per kilowatt hour', 0.7);
        """
    )

    comparisons = build_comparisons(
        conn,
        docket_number="E-2 Sub 1380",
        utility="Duke Energy Progress",
    )
    assert len(comparisons) == 1
    assert comparisons[0].family_match is None
    assert comparisons[0].approved_charges == []
    assert comparisons[0].proposed_charges[0].rate_value == 0.0
