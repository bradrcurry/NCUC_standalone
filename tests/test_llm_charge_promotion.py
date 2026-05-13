from __future__ import annotations

import json
import sqlite3

from duke_rates.document_intelligence.llm_charge_promotion import (
    ensure_promotion_tables,
    promote_llm_charge_proposals,
    propose_llm_charge_promotions,
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


def _insert_validated_row(conn, *, validation_id=1, extraction_id=10, historical_document_id=100):
    conn.execute(
        "INSERT INTO tariff_versions (id, family_key, historical_document_id, effective_start) VALUES (200, 'nc-test-fam', ?, '2025-01-01')",
        (historical_document_id,),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (id, historical_document_id, source_pdf, rate_rows_json)
        VALUES (?, ?, 'doc.pdf', ?)
        """,
        (
            extraction_id,
            historical_document_id,
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
        VALUES (?, ?, 0, ?, 'doc.pdf', 'Energy Charge', 6.037, '¢/kWh',
                '6.0370 per on-peak kWh', 'validated')
        """,
        (validation_id, extraction_id, historical_document_id),
    )


def test_propose_llm_charge_promotions_creates_eligible_proposal(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=True)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """
        SELECT version_id, family_key, charge_type, rate_value, rate_unit,
               eligibility_status, duplicate_status
        FROM llm_rate_charge_promotion_proposals
        """
    ).fetchone()
    conn.close()
    assert row == (200, "nc-test-fam", "Energy Charge", 6.037, "¢/kWh", "eligible", "novel")


def test_propose_llm_charge_promotions_blocks_duplicate_existing_charge(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.execute(
        """
        INSERT INTO tariff_charges
        (version_id, family_key, charge_type, rate_value, rate_unit, created_at)
        VALUES (200, 'nc-test-fam', 'Energy Charge', 6.037, '¢/kWh', datetime('now'))
        """
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"blocked": 1}
    assert report["rows"][0]["duplicate_status"] == "duplicate_existing"
    assert "duplicate_existing_charge" in report["rows"][0]["eligibility_issues"]


def test_promote_llm_charge_proposals_dry_run_and_execute(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.commit()
    conn.close()
    propose_llm_charge_promotions(db_path, limit=10, execute=True)

    dry_run = promote_llm_charge_proposals(db_path, limit=10, execute=False)
    assert dry_run["summary"]["promoted"] == 1
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tariff_charges").fetchone()[0] == 0
    conn.close()

    executed = promote_llm_charge_proposals(db_path, limit=10, execute=True)

    assert executed["summary"]["promoted"] == 1
    conn = sqlite3.connect(db_path)
    charge = conn.execute(
        "SELECT charge_type, rate_value, rate_unit FROM tariff_charges"
    ).fetchone()
    audit_count = conn.execute("SELECT COUNT(*) FROM llm_promoted_charge_audit").fetchone()[0]
    conn.close()
    assert charge == ("Energy Charge", 6.037, "¢/kWh")
    assert audit_count == 1


def test_propose_llm_charge_promotions_blocks_unqualified_units(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Basic Facilities Charge',
            value = 21.0,
            unit = '$',
            source_quote = '$21.00'
        WHERE id = 1
        """
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Basic Facilities Charge",
                        "value": 21.0,
                        "unit": "$",
                        "source_quote": "$21.00",
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"blocked": 1}
    assert "unqualified_rate_unit" in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_uses_inferred_unit_for_bare_original_unit(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Basic Facilities Charge',
            value = 21.0,
            unit = '$',
            inferred_unit = '$/month',
            inferred_unit_reason = 'fixed_charge_monthly_context',
            source_quote = 'A. Basic Customer Charge: $21.00'
        WHERE id = 1
        """
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Basic Facilities Charge",
                        "value": 21.0,
                        "unit": "$",
                        "source_quote": "A. Basic Customer Charge: $21.00",
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["rate_unit"] == "$/month"


def test_propose_llm_charge_promotions_infers_bare_unit_from_source_quote(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Basic Facilities Charge',
            value = 21.0,
            unit = '$',
            source_quote = 'A. Basic Customer Charge: $21.00 per month'
        WHERE id = 1
        """,
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Basic Facilities Charge",
                        "value": 21.0,
                        "unit": "$",
                        "source_quote": "A. Basic Customer Charge: $21.00 per month",
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["rate_unit"] == "$/month"
    assert "unqualified_rate_unit" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_refreshes_existing_null_repair_proposal(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.execute(
        """
        INSERT INTO llm_rate_charge_promotion_proposals (
            validation_id, extraction_id, row_index, repair_id,
            charge_type, rate_value, rate_unit, effective_status,
            eligibility_status, eligibility_issues_json,
            duplicate_status, conflict_status, promotion_status
        )
        VALUES (1, 10, 0, NULL, 'Energy Charge', 6.037, '$',
                'validated', 'blocked', '["unqualified_rate_unit"]',
                'novel', 'none', 'pending')
        """
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(
        db_path,
        limit=10,
        refresh_existing=True,
        execute=True,
    )

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT rate_unit, eligibility_status, eligibility_issues_json
        FROM llm_rate_charge_promotion_proposals
        WHERE validation_id = 1
        """
    ).fetchall()
    conn.close()
    assert rows == [("¢/kWh", "eligible", "[]")]


def test_propose_llm_charge_promotions_refresh_existing_skips_promoted_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.execute(
        """
        INSERT INTO llm_rate_charge_promotion_proposals (
            validation_id, extraction_id, row_index, repair_id,
            charge_type, rate_value, rate_unit, effective_status,
            eligibility_status, eligibility_issues_json,
            duplicate_status, conflict_status, promotion_status
        )
        VALUES (1, 10, 0, NULL, 'Energy Charge', 6.037, '$',
                'validated', 'blocked', '["unqualified_rate_unit"]',
                'novel', 'none', 'promoted')
        """
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(
        db_path,
        limit=10,
        refresh_existing=True,
        execute=True,
    )

    assert report["summary"]["evaluated"] == 0
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """
        SELECT rate_unit, eligibility_status, eligibility_issues_json, promotion_status
        FROM llm_rate_charge_promotion_proposals
        WHERE validation_id = 1
        """
    ).fetchone()
    conn.close()
    assert row == ("$", "blocked", '["unqualified_rate_unit"]', "promoted")


def test_propose_llm_charge_promotions_normalizes_storm_recovery_other_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = (
        "existing storm cost recovery charges of 0.300 cents per kilowatt hour "
        "(kWh) for Residential customers"
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Other',
            value = 0.3,
            unit = '¢/kWh',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Other",
                        "charge_label": "Other - Residential",
                        "customer_class": "Residential",
                        "value": 0.3,
                        "unit": "¢/kWh",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    row = report["rows"][0]
    assert row["charge_type"] == "Rider Adjustment"
    assert row["charge_label"] == "Storm Recovery Charge - Residential"
    assert "unsupported_charge_type" not in row["eligibility_issues"]


def test_propose_llm_charge_promotions_blocks_path_like_family_keys(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.execute(
        """
        UPDATE tariff_versions
        SET family_key = '/pdfs/g2-nc-schedule-sgs-toue-dep.pdf'
        WHERE id = 200
        """
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"blocked": 1}
    assert "malformed_family_key" in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_normalizes_regulatory_fee_other_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = "Taxes = NC Regulatory Fee (currently 0.1703%)"
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Other',
            value = 0.1703,
            unit = '%',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Other",
                        "value": 0.1703,
                        "unit": "%",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["charge_type"] == "Rider Adjustment"
    assert "unsupported_charge_type" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_reroutes_path_like_family_key_when_canonical_version_exists(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    conn.execute(
        """
        UPDATE tariff_versions
        SET family_key = '/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-r.pdf',
            effective_start = '2025-01-01'
        WHERE id = 200
        """
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (id, family_key, historical_document_id, effective_start)
        VALUES (201, 'nc-progress-leaf-500', 101, '2025-01-01')
        """
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    row = report["rows"][0]
    assert row["version_id"] == 201
    assert row["family_key"] == "nc-progress-leaf-500"
    assert "malformed_family_key" not in row["eligibility_issues"]


def test_propose_llm_charge_promotions_normalizes_lighting_monthly_other_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = "4,000 41 Suburban (1) $6.76 NA NA"
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Other',
            value = 6.76,
            unit = '$',
            inferred_unit = '$/month',
            inferred_unit_reason = 'lighting_table_per_month_per_luminaire',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Other",
                        "charge_label": "Other - Suburban (1)",
                        "value": 6.76,
                        "unit": "$/month",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    row = report["rows"][0]
    assert row["charge_type"] == "Lighting Charge"
    assert row["charge_label"] == "Lighting Charge - Suburban (1)"
    assert "unsupported_charge_type" not in row["eligibility_issues"]


def test_propose_llm_charge_promotions_normalizes_led_shoebox_other_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = "LED 220 Shoebox 24.51 79"
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Other',
            value = 24.51,
            unit = '$/month',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Other",
                        "charge_label": "Other - Residential",
                        "customer_class": "Residential",
                        "value": 24.51,
                        "unit": "$/month",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    row = report["rows"][0]
    assert row["charge_type"] == "Lighting Charge"
    assert row["charge_label"] == "Lighting Charge - Residential"
    assert "unsupported_charge_type" not in row["eligibility_issues"]


def test_propose_llm_charge_promotions_normalizes_all_customer_reduction(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = (
        "The reduction in rates applicable to all customers is 0.278 cents per kWh, "
        "including the regulatory fee."
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Other',
            value = 0.278,
            unit = '¢/kWh',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Other",
                        "value": 0.278,
                        "unit": "¢/kWh",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["charge_type"] == "Rider Adjustment"
    assert "unsupported_charge_type" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_normalizes_other_demand_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = "Billing Demand Charge: $12.50 per kW"
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Other',
            value = 12.5,
            unit = '$/kW',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Other",
                        "charge_label": "Billing Demand Charge",
                        "value": 12.5,
                        "unit": "$/kW",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["charge_type"] == "Demand Charge"
    assert "unsupported_charge_type" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_normalizes_incentive_other_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = (
        "All kWh savings shall be incentivized at up to $0.75/kWh in homes "
        "that consume natural gas for space heating with at least one unit."
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Other',
            value = 0.75,
            unit = '$/kWh',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Other",
                        "value": 0.75,
                        "unit": "$/kWh",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["charge_type"] == "Rider Adjustment"
    assert "unsupported_charge_type" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_normalizes_dsm_other_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = "0.049 (DSM Only) (0.005) (DSM"
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Other',
            value = 0.005,
            unit = '¢/kWh',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Other",
                        "value": 0.005,
                        "unit": "¢/kWh",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["charge_type"] == "Rider Adjustment"
    assert "unsupported_charge_type" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_normalizes_saved_other_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = "Up to $0.75/kWh saved"
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Other',
            value = 0.75,
            unit = '$/kWh',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Other",
                        "value": 0.75,
                        "unit": "$/kWh",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["charge_type"] == "Rider Adjustment"
    assert "unsupported_charge_type" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_allows_same_snippet_different_charge_type(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = "Energy Charge 6.037 cents per kWh; Demand Charge $12.50 per kW"
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Demand Charge',
            value = 12.5,
            unit = '$/kW',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        INSERT INTO tariff_charges
        (version_id, family_key, charge_type, rate_value, rate_unit, source_snippet, created_at)
        VALUES (200, 'nc-test-fam', 'Energy Charge', 6.037, '¢/kWh', ?, datetime('now'))
        """,
        (source_quote,),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["conflict_status"] == "none"


def test_propose_llm_charge_promotions_blocks_same_snippet_same_charge_type_conflict(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = "Energy Charge 6.037 cents per kWh; Energy Charge 7.001 cents per kWh"
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Energy Charge',
            value = 7.001,
            unit = '¢/kWh',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        INSERT INTO tariff_charges
        (version_id, family_key, charge_type, rate_value, rate_unit, source_snippet, created_at)
        VALUES (200, 'nc-test-fam', 'Energy Charge', 6.037, '¢/kWh', ?, datetime('now'))
        """,
        (source_quote,),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"blocked": 1}
    assert report["rows"][0]["conflict_status"] == "conflicting_same_source_snippet"


def test_propose_llm_charge_promotions_reroutes_missing_effective_bundle_row(tmp_path):
    db_path = tmp_path / "test.sqlite"
    source_path = tmp_path / "bundle.txt"
    source_quote = "12.119¢ per On Peak kWh"
    source_path.write_text(
        "\n".join(
            [
                "NC First Revised Leaf No. 500",
                "Effective for service rendered from October 1, 2024 through September 30, 2025",
                "MONTHLY RATE",
                "12.119¢\nper On-Peak kWh",
            ]
        ),
        encoding="utf-8",
    )
    conn = _init_db(db_path)
    conn.execute(
        """
        INSERT INTO tariff_versions (id, family_key, historical_document_id, effective_start)
        VALUES
          (300, 'nc-progress-leaf-601', 100, NULL),
          (200, 'nc-progress-leaf-500', 101, '2024-10-01')
        """
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (id, historical_document_id, source_pdf, rate_rows_json)
        VALUES (10, 100, ?, ?)
        """,
        (
            str(source_path),
            json.dumps(
                [
                    {
                        "charge_type": "Energy Charge",
                        "value": 12.119,
                        "unit": "¢/kWh",
                        "source_quote": source_quote,
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
        VALUES (1, 10, 0, 100, ?, 'Energy Charge', 12.119, '¢/kWh', ?, 'validated')
        """,
        (str(source_path), source_quote),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    row = report["rows"][0]
    assert row["version_id"] == 200
    assert row["family_key"] == "nc-progress-leaf-500"


def test_propose_llm_charge_promotions_reroutes_bundle_row_with_effective_date_variant(tmp_path):
    db_path = tmp_path / "test.sqlite"
    source_path = tmp_path / "bundle.txt"
    source_quote = "12.119¢ per On Peak kWh"
    source_path.write_text(
        "\n".join(
            [
                "NC First Revised Leaf No. 500",
                "Effective for bills rendered on and after October 1, 2024",
                "MONTHLY RATE",
                "12.119¢ per On-Peak kWh",
            ]
        ),
        encoding="utf-8",
    )
    conn = _init_db(db_path)
    conn.execute(
        """
        INSERT INTO tariff_versions (id, family_key, historical_document_id, effective_start)
        VALUES
          (300, 'nc-progress-leaf-601', 100, NULL),
          (200, 'nc-progress-leaf-500', 101, '2024-10-01')
        """
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (id, historical_document_id, source_pdf, rate_rows_json)
        VALUES (10, 100, ?, ?)
        """,
        (
            str(source_path),
            json.dumps(
                [
                    {
                        "charge_type": "Energy Charge",
                        "value": 12.119,
                        "unit": "¢/kWh",
                        "source_quote": source_quote,
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
        VALUES (1, 10, 0, 100, ?, 'Energy Charge', 12.119, '¢/kWh', ?, 'validated')
        """,
        (str(source_path), source_quote),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["version_id"] == 200


def test_propose_llm_charge_promotions_reroutes_null_effective_version_by_snapshot_date(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute(
        """
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            effective_start TEXT,
            snapshot_timestamp TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO historical_documents (id, effective_start, snapshot_timestamp) VALUES (100, NULL, '2023-10-01T00:00:00')"
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (id, family_key, historical_document_id, effective_start)
        VALUES
          (300, 'nc-progress-leaf-601', 100, NULL),
          (200, 'nc-progress-leaf-601', 101, '2023-10-01')
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
                        "value": 8.927,
                        "unit": "¢/kWh",
                        "source_quote": "8.927¢ per kWh",
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
        VALUES (1, 10, 0, 100, 'doc.pdf', 'Energy Charge', 8.927, '¢/kWh',
                '8.927¢ per kWh', 'validated')
        """
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["version_id"] == 200
    assert "missing_version_effective_start" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_blocks_ambiguous_multi_numeric_adjustment_row(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = "Residential 0.040 0.580 0.750 0.017 1.387"
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Energy Charge',
            value = 0.04,
            unit = '¢/kWh',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Energy Charge",
                        "value": 0.04,
                        "unit": "¢/kWh",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"blocked": 1}
    assert "ambiguous_numeric_table_row" in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_keeps_single_currency_lighting_table_row_promotable(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = "4,000 41 Suburban (1) $6.76 NA NA"
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Lighting Charge',
            value = 6.76,
            unit = '$/month',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Lighting Charge",
                        "value": 6.76,
                        "unit": "$/month",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert "ambiguous_numeric_table_row" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_allows_unique_lighting_summary_row(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    _insert_validated_row(conn)
    source_quote = (
        "The fuel rate included in base tariff rates effective October 1, 2023 "
        "are 2.808¢/kWh for RES, 3.097¢/kWh for SGS, 2.580¢/kWh for MGS, "
        "2.138¢/kWh for LGS and 3.377¢/kWh for Lighting, excluding the North "
        "Carolina regulatory fee."
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_row_validations
        SET charge_type = 'Lighting Charge',
            value = 3.377,
            unit = '¢/kWh',
            source_quote = ?
        WHERE id = 1
        """,
        (source_quote,),
    )
    conn.execute(
        """
        UPDATE llm_candidate_rate_extractions
        SET rate_rows_json = ?
        WHERE id = 10
        """,
        (
            json.dumps(
                [
                    {
                        "charge_type": "Lighting Charge",
                        "value": 3.377,
                        "unit": "¢/kWh",
                        "source_quote": source_quote,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert "ambiguous_numeric_table_row" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_reroutes_leaf601_from_unique_summary_line_date(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.executescript(
        """
        CREATE TABLE rider_summary_blocks (
            id INTEGER PRIMARY KEY,
            effective_date TEXT,
            utility TEXT
        );
        CREATE TABLE rider_line_items (
            id INTEGER PRIMARY KEY,
            block_id INTEGER NOT NULL,
            rider_code TEXT,
            cents_per_kwh REAL,
            dollars_per_kw REAL,
            line_effective_date TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (id, family_key, historical_document_id, effective_start)
        VALUES
          (300, 'nc-progress-leaf-601', 100, NULL),
          (200, 'nc-progress-leaf-601', 101, '2024-12-01')
        """
    )
    conn.execute(
        "INSERT INTO rider_summary_blocks (id, effective_date, utility) VALUES (1, '2025-04-01', 'DEP')"
    )
    conn.execute(
        """
        INSERT INTO rider_line_items
        (id, block_id, rider_code, cents_per_kwh, dollars_per_kw, line_effective_date)
        VALUES (1, 1, 'BA-Fuel', 0.04, NULL, '12/1/24')
        """
    )
    source_quote = "Fuel and Fuel-Related Adjustment Rate 0.040 cents per kWh"
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
                        "value": 0.04,
                        "unit": "¢/kWh",
                        "source_quote": source_quote,
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
        VALUES (1, 10, 0, 100, 'doc.pdf', 'Energy Charge', 0.04, '¢/kWh',
                ?, 'validated')
        """,
        (source_quote,),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"eligible": 1}
    assert report["rows"][0]["version_id"] == 200
    assert "missing_version_effective_start" not in report["rows"][0]["eligibility_issues"]


def test_propose_llm_charge_promotions_keeps_ambiguous_bundle_row_blocked(tmp_path):
    db_path = tmp_path / "test.sqlite"
    source_path = tmp_path / "bundle.txt"
    source_quote = "The bill computed for single-phase service plus $9.00."
    source_path.write_text(
        "\n".join(
            [
                "NC First Revised Leaf No. 500",
                "Effective for service rendered from October 1, 2024 through September 30, 2025",
                source_quote,
                "NC First Revised Leaf No. 501",
                "Effective for service rendered from October 1, 2024 through September 30, 2025",
                source_quote,
            ]
        ),
        encoding="utf-8",
    )
    conn = _init_db(db_path)
    conn.execute(
        """
        INSERT INTO tariff_versions (id, family_key, historical_document_id, effective_start)
        VALUES
          (300, 'nc-progress-leaf-601', 100, NULL),
          (200, 'nc-progress-leaf-500', 101, '2024-10-01'),
          (201, 'nc-progress-leaf-501', 102, '2024-10-01')
        """
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (id, historical_document_id, source_pdf, rate_rows_json)
        VALUES (10, 100, ?, ?)
        """,
        (
            str(source_path),
            json.dumps(
                [
                    {
                        "charge_type": "Basic Facilities Charge",
                        "value": 9.0,
                        "unit": "$",
                        "source_quote": source_quote,
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
        VALUES (1, 10, 0, 100, ?, 'Basic Facilities Charge', 9.0, '$', ?, 'validated')
        """,
        (str(source_path), source_quote),
    )
    conn.commit()
    conn.close()

    report = propose_llm_charge_promotions(db_path, limit=10, execute=False)

    assert report["summary"]["eligibility_counts"] == {"blocked": 1}
    row = report["rows"][0]
    assert row["version_id"] == 300
    assert "missing_version_effective_start" in row["eligibility_issues"]
