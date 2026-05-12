from __future__ import annotations

import json
import sqlite3

from duke_rates.document_intelligence.llm_charge_promotion import (
    ensure_promotion_tables,
    propose_llm_charge_promotions,
)
from duke_rates.document_intelligence.llm_promotion_overnight import (
    run_llm_promotion_overnight,
)


def _init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY,
            family_key TEXT NOT NULL,
            historical_document_id INTEGER,
            effective_start TEXT
        );
        CREATE TABLE tariff_charges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id INTEGER NOT NULL,
            family_key TEXT NOT NULL,
            charge_type TEXT NOT NULL,
            charge_label TEXT,
            rate_value REAL,
            rate_unit TEXT,
            tier_min REAL,
            tier_max REAL,
            tou_period TEXT,
            season TEXT,
            customer_class TEXT,
            source_snippet TEXT,
            confidence_score REAL NOT NULL DEFAULT 0.0,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE llm_candidate_rate_extractions (
            id INTEGER PRIMARY KEY,
            historical_document_id INTEGER,
            source_pdf TEXT NOT NULL,
            rate_rows_json TEXT NOT NULL,
            document_signals_json TEXT NOT NULL DEFAULT '{}',
            extraction_confidence REAL NOT NULL DEFAULT 0.0,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            model TEXT NOT NULL DEFAULT 'test',
            model_role TEXT NOT NULL DEFAULT 'test',
            prompt_version TEXT NOT NULL DEFAULT 'v1',
            status TEXT NOT NULL DEFAULT 'validated'
        );
        CREATE TABLE llm_candidate_rate_row_validations (
            id INTEGER PRIMARY KEY,
            extraction_id INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            historical_document_id INTEGER,
            source_pdf TEXT NOT NULL,
            charge_type TEXT,
            value REAL,
            unit TEXT,
            source_quote TEXT,
            source_quote_grounded INTEGER NOT NULL DEFAULT 1,
            value_grounded INTEGER NOT NULL DEFAULT 1,
            unit_grounded INTEGER NOT NULL DEFAULT 1,
            validation_score REAL NOT NULL DEFAULT 1.0,
            recommended_status TEXT NOT NULL,
            issues_json TEXT NOT NULL DEFAULT '[]',
            validated_at TEXT NOT NULL DEFAULT (datetime('now')),
            inferred_unit TEXT,
            inferred_unit_reason TEXT
        );
        CREATE TABLE llm_candidate_rate_row_repairs (
            id INTEGER PRIMARY KEY,
            validation_id INTEGER NOT NULL,
            extraction_id INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            repair_type TEXT NOT NULL,
            original_charge_type TEXT,
            proposed_charge_type TEXT,
            original_unit TEXT,
            proposed_unit TEXT,
            evidence_quote TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            reason TEXT,
            validation_status TEXT NOT NULL,
            validation_issues_json TEXT NOT NULL DEFAULT '[]',
            model TEXT,
            model_role TEXT,
            status TEXT NOT NULL DEFAULT 'accepted',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    ensure_promotion_tables(conn)
    return conn


def _insert_validated_row(conn):
    conn.execute(
        """
        INSERT INTO tariff_versions
        (id, family_key, historical_document_id, effective_start)
        VALUES (200, 'nc-test-fam', 100, '2025-01-01')
        """
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (id, historical_document_id, source_pdf, rate_rows_json)
        VALUES (10, 100, 'doc.pdf', ?)
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Energy Charge",
                        "value": 6.037,
                        "unit": "¢/kWh",
                        "source_quote": "6.0370 per on-peak kWh",
                        "tou_period": "On-Peak",
                    }
                ]
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_row_validations
        (id, extraction_id, row_index, historical_document_id, source_pdf,
         charge_type, value, unit, source_quote, recommended_status)
        VALUES (1, 10, 0, 100, 'doc.pdf', 'Energy Charge', 6.037, '¢/kWh',
                '6.0370 per on-peak kWh', 'validated')
        """
    )


def test_run_llm_promotion_overnight_executes_only_guarded_promotions(tmp_path):
    db_path = tmp_path / "test.sqlite"
    output_dir = tmp_path / "reports"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.commit()
    conn.close()
    propose_llm_charge_promotions(db_path, limit=10, execute=True)

    report = run_llm_promotion_overnight(
        db_path,
        validation_limit=10,
        repair_limit=10,
        proposal_limit=10,
        promotion_limit=10,
        execute_safe=True,
        output_dir=output_dir,
    )

    assert report["promotion_dry_run"]["promoted"] == 1
    assert report["promotion_execute"] == {
        "evaluated": 1,
        "execute": True,
        "promoted": 1,
        "skipped": 0,
    }
    assert report["delta"]["tariff_charges"] == 1
    assert report["delta"]["promoted_audit"] == 1
    assert output_dir.exists()
    assert report["report_path"]

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tariff_charges").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM llm_promoted_charge_audit").fetchone()[0] == 1
    conn.close()


def test_run_llm_promotion_overnight_creates_new_proposals_before_refresh(tmp_path):
    db_path = tmp_path / "test.sqlite"
    output_dir = tmp_path / "reports"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.commit()
    conn.close()

    report = run_llm_promotion_overnight(
        db_path,
        validation_limit=10,
        repair_limit=10,
        proposal_limit=10,
        promotion_limit=10,
        execute_safe=False,
        output_dir=output_dir,
    )

    assert report["proposal_create"]["eligibility_counts"] == {"eligible": 1}
    assert report["proposal_refresh"]["eligibility_counts"] == {"eligible": 1}
    assert report["promotion_dry_run"]["promoted"] == 1
    assert report["delta"]["pending_promotable"] == 1

    conn = sqlite3.connect(db_path)
    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM llm_rate_charge_promotion_proposals
        WHERE promotion_status = 'pending'
          AND eligibility_status = 'eligible'
          AND duplicate_status = 'novel'
          AND conflict_status = 'none'
        """
    ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tariff_charges").fetchone()[0] == 0
    conn.close()
