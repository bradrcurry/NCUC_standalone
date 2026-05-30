from __future__ import annotations

import sqlite3
from pathlib import Path

from duke_rates.document_intelligence.proposed_tariff_extractor import (
    _register_proposed_pdf_in_historical_documents,
    ensure_schema,
    extract_charge_candidates,
)
from duke_rates.document_intelligence.proposed_tariff_detector import (
    ProposedTariffBlock,
)


def test_extract_charge_candidates_reads_basic_and_energy_lines() -> None:
    charges = extract_charge_candidates(
        """
        Basic Customer Charge $15.50 per month
        Kilowatt-Hour Charge 12.3456 cents per kWh
        On-Peak Demand Charge $4.20 per kW
        """
    )

    by_type = {c.charge_type: c for c in charges}
    assert by_type["fixed"].rate_value == 15.50
    assert by_type["fixed"].rate_unit == "$/month"
    assert by_type["energy"].rate_value == 0.123456
    assert by_type["energy"].rate_unit == "$/kWh"
    assert by_type["demand"].rate_value == 4.20
    assert by_type["demand"].rate_unit == "$/kw"


def test_extract_charge_candidates_keeps_rider_adjustment_separate() -> None:
    charges = extract_charge_candidates(
        "Rider Adjustment Residential 0.123 cents per kWh"
    )

    assert len(charges) == 1
    assert charges[0].charge_type == "adjustment"
    assert charges[0].rate_value == 0.00123


def _hd_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            state TEXT,
            company TEXT,
            category TEXT,
            kind TEXT,
            local_path TEXT,
            content_hash TEXT,
            content_type TEXT,
            status TEXT,
            retrieved_at TEXT,
            snapshot_timestamp TEXT,
            effective_start TEXT,
            requested_effective_date TEXT,
            metadata_json TEXT
        );
        """
    )
    ensure_schema(conn)
    return conn


def _block(effective_start: str | None = "2027-01-01") -> ProposedTariffBlock:
    return ProposedTariffBlock(
        source_pdf="dep-1380.pdf",
        section_id=None,
        section_index=1,
        start_page=1,
        end_page=1,
        section_type="rider",
        exhibit_key="B",
        rate_year_context="Proposed Exhibit B",
        schedule_name="RIDER PC PENSIONS COSTS",
        basic_customer_charge=None,
        volumetric_energy_charge_lines=[],
        time_of_use_lines=[],
        has_interclass_impact_table=False,
        confidence=0.7,
        evidence=[],
        leaf_no=614,
        effective_start=effective_start,
    )


def test_register_proposed_pdf_inserts_new_historical_document(tmp_path: Path) -> None:
    conn = _hd_conn()
    pdf = tmp_path / "candidate.pdf"
    pdf.write_bytes(b"%PDF-1.4 test bytes")

    record_id = _register_proposed_pdf_in_historical_documents(
        conn,
        pdf=pdf,
        docket_number="E-2 Sub 1380",
        utility="Duke Energy Progress",
        blocks=[_block()],
        now="2026-05-30T00:00:00+00:00",
    )

    assert record_id is not None
    row = conn.execute(
        "SELECT title, state, company, category, kind, status, "
        "content_type, effective_start, content_hash "
        "FROM historical_documents WHERE id = ?",
        (record_id,),
    ).fetchone()
    assert row["state"] == "NC"
    assert row["company"] == "progress"
    assert row["category"] == "rate_case_application"
    assert row["kind"] == "pdf"
    assert row["status"] == "proposed"
    assert row["content_type"] == "application/pdf"
    assert row["effective_start"] == "2027-01-01"
    assert row["content_hash"] and len(row["content_hash"]) == 64
    assert "Proposed Tariff Application" in row["title"]


def test_register_proposed_pdf_reuses_existing_row_by_content_hash(
    tmp_path: Path,
) -> None:
    conn = _hd_conn()
    pdf = tmp_path / "candidate.pdf"
    pdf.write_bytes(b"%PDF-1.4 second")

    first = _register_proposed_pdf_in_historical_documents(
        conn,
        pdf=pdf,
        docket_number="E-7 Sub 1329",
        utility="Duke Energy Carolinas",
        blocks=[_block()],
        now="2026-05-30T00:00:00+00:00",
    )
    second = _register_proposed_pdf_in_historical_documents(
        conn,
        pdf=pdf,
        docket_number="E-7 Sub 1329",
        utility="Duke Energy Carolinas",
        blocks=[_block()],
        now="2026-05-30T00:00:00+00:00",
    )

    assert first is not None
    assert first == second
    n_rows = conn.execute(
        "SELECT COUNT(*) FROM historical_documents"
    ).fetchone()[0]
    assert n_rows == 1
