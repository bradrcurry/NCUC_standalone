"""Safety guards against accidental destruction of historical tariff data.

* ``BulkExtractor.insert_charges`` replaced every charge of a version on
  re-extraction, erasing manual corrections and LLM-promoted rows (only 55 of
  331 promotions survive in the production DB).  Re-extraction now replaces
  only rows the deterministic extractor wrote.
* ``replace_parsed_tariff_data`` (``tariff-update --auto-parse``) deletes all
  versions of a family; it now refuses when that would delete versions bound
  to historical documents, unless explicitly overridden.
* Migration TD-002 deleted duplicate ``rider_summary_blocks`` without their
  ``rider_line_items`` (1,204 orphans); it must not orphan line items.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.historical.ncuc.pipeline.rate_extractor import ExtractedCharge


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "guards.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.commit()
    conn.close()
    return path


def _charge(value: float, label: str = "Basic Customer Charge") -> ExtractedCharge:
    return ExtractedCharge(
        charge_type="basic_customer", charge_label=label, rate_value=value, rate_unit="$/month",
        season="all_year", tou_period=None, tier_min=None, tier_max=None,
        source_snippet=label, confidence_score=0.9,
    )


def _rows(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT charge_label, rate_value, notes FROM tariff_charges ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_reextraction_preserves_manual_and_llm_promoted_charges(db_path: Path) -> None:
    extractor = BulkExtractor(db_path=str(db_path))
    assert extractor.insert_charges(7, "nc-progress-leaf-500", [_charge(14.0)]) == 1

    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO tariff_charges (version_id, family_key, charge_type, charge_label,
               rate_value, rate_unit, notes, created_at)
           VALUES (7, 'nc-progress-leaf-500', 'energy', 'Manual fix', 0.1, '$/kWh',
                   'OCR cent-symbol recovered manually', 'now')"""
    )
    cur = conn.execute(
        """INSERT INTO tariff_charges (version_id, family_key, charge_type, charge_label,
               rate_value, rate_unit, created_at)
           VALUES (7, 'nc-progress-leaf-500', 'rider', 'LLM promoted', 0.2, '$/kWh', 'now')"""
    )
    conn.execute(
        """INSERT INTO llm_promoted_charge_audit (proposal_id, tariff_charge_id, validation_id,
               extraction_id, row_index, promoted_at)
           VALUES (1, ?, 1, 1, 0, 'now')""",
        (cur.lastrowid,),
    )
    conn.commit()
    conn.close()

    # Re-extraction with a new parser result, and a forced clear with no result.
    assert extractor.insert_charges(7, "nc-progress-leaf-500", [_charge(15.0)]) == 1
    labels = [(r[0], r[1]) for r in _rows(db_path)]
    assert ("Basic Customer Charge", 14.0) not in labels
    assert ("Basic Customer Charge", 15.0) in labels
    assert ("Manual fix", 0.1) in labels and ("LLM promoted", 0.2) in labels

    extractor.insert_charges(7, "nc-progress-leaf-500", [], force_clear=True)
    assert sorted(r[0] for r in _rows(db_path)) == ["LLM promoted", "Manual fix"]


def test_replace_parsed_tariff_data_refuses_to_delete_historical_versions(tmp_path: Path) -> None:
    from duke_rates.db.repository import HistoricalDataLossError, Repository
    from duke_rates.models.tariff import TariffVersionRecord

    from duke_rates.db.sqlite import connect

    db = tmp_path / "repo.db"
    repo = Repository(db)
    connect(db).close()  # create the schema
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO tariff_versions (family_key, historical_document_id, effective_start,
               source_type, confidence_score, created_at)
           VALUES ('nc-progress-leaf-600', 42, '2024-01-01', 'regulator', 0.9, 'now')"""
    )
    conn.commit()
    conn.close()

    new_version = TariffVersionRecord(family_key="nc-progress-leaf-600", source_type="utility_current")
    with pytest.raises(HistoricalDataLossError):
        repo.replace_parsed_tariff_data("nc-progress-leaf-600", new_version, [], [])

    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM tariff_versions WHERE family_key='nc-progress-leaf-600'"
    ).fetchone()[0] == 1
    conn.close()

    # A family with only crawl-derived versions is still replaced as before.
    repo.replace_parsed_tariff_data(
        "nc-progress-leaf-999",
        TariffVersionRecord(family_key="nc-progress-leaf-999", source_type="utility_current"),
        [], [],
    )


def test_td002_dedupe_does_not_orphan_line_items(tmp_path: Path) -> None:
    path = tmp_path / "td002.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    # Simulate legacy duplicates: drop the unique index so two identical blocks exist.
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='rider_summary_blocks' AND sql IS NOT NULL"
    ).fetchall():
        conn.execute(f"DROP INDEX {name}")
    for _ in range(2):
        cur = conn.execute(
            """INSERT INTO rider_summary_blocks (docket_dir, source_pdf, leaf_no, effective_date,
                   rate_class, applicable_schedules_json, created_at)
               VALUES ('d', 'a.pdf', '600', '2024-01-01', 'Residential', '[]', 'now')"""
        )
        conn.execute(
            """INSERT INTO rider_line_items (block_id, label, cents_per_kwh, is_section_header,
                   is_subtotal, is_total, created_at)
               VALUES (?, 'EDIT-4 Rider', -0.249, 0, 0, 0, 'now')""",
            (cur.lastrowid,),
        )
    conn.commit()
    migrate(conn)
    conn.commit()
    orphans = conn.execute(
        """SELECT COUNT(*) FROM rider_line_items li
           LEFT JOIN rider_summary_blocks b ON b.id = li.block_id WHERE b.id IS NULL"""
    ).fetchone()[0]
    blocks = conn.execute("SELECT COUNT(*) FROM rider_summary_blocks").fetchone()[0]
    conn.close()
    assert orphans == 0
    assert blocks >= 1
