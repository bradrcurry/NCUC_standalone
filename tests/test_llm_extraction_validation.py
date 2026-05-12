from __future__ import annotations

import json
import sqlite3

from duke_rates.document_intelligence.llm_extraction_validation import (
    validate_candidate_extractions,
)


def _init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE llm_candidate_rate_extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            historical_document_id INTEGER,
            source_pdf TEXT NOT NULL,
            rate_rows_json TEXT NOT NULL,
            document_signals_json TEXT NOT NULL DEFAULT '{}',
            extraction_confidence REAL NOT NULL DEFAULT 0.0,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            model TEXT NOT NULL,
            model_role TEXT NOT NULL,
            prompt_version TEXT NOT NULL DEFAULT 'v1',
            ollama_run_id INTEGER,
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE ncuc_page_artifacts (
            source_pdf TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            text_content TEXT
        );
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            raw_text_path TEXT
        );
        """
    )
    return conn


def test_validate_candidate_extractions_accepts_grounded_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "Basic Facilities Charge $12.50 per month\nEnergy Charge 10.369 cents per kWh",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Fixed Monthly Charge",
                        "value": 12.50,
                        "unit": "$/month",
                        "source_quote": "Basic Facilities Charge $12.50 per month",
                        "confidence": 0.95,
                    },
                    {
                        "charge_type": "Energy Charge",
                        "value": 10.369,
                        "unit": "¢/kWh",
                        "source_quote": "Energy Charge 10.369 cents per kWh",
                        "confidence": 0.9,
                    },
                ]
            ),
            0.92,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=True)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    assert report["summary"]["updates"] == 1
    assert report["summary"]["review_candidate_rows"] == 0
    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT status FROM llm_candidate_rate_extractions").fetchone()[0]
    rows = conn.execute(
        """
        SELECT recommended_status, COUNT(*)
        FROM llm_candidate_rate_row_validations
        GROUP BY recommended_status
        """
    ).fetchall()
    conn.close()
    assert status == "validated"
    assert rows == [("validated", 2)]


def test_validate_candidate_extractions_rejects_ungrounded_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        ("doc.pdf", 1, "Basic Facilities Charge $12.50 per month"),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Energy Charge",
                        "value": 99.99,
                        "unit": "$/kWh",
                        "source_quote": "Energy Charge $99.99 per kWh",
                        "confidence": 0.9,
                    }
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=True)

    assert report["summary"]["recommended_status_counts"] == {"rejected": 1}
    assert report["summary"]["review_candidate_rows"] == 0
    assert report["rows"][0]["row_results"][0]["issues"] == ["source_quote_not_grounded"]
    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT status FROM llm_candidate_rate_extractions").fetchone()[0]
    conn.close()
    assert status == "rejected"


def test_validate_candidate_extractions_accepts_tou_unit_phrases(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "Energy Charges\nCritical Peak Energy 39.614¢ per Critical Peak kWh",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Energy Charge",
                        "value": 39.614,
                        "unit": "¢/kWh",
                        "source_quote": "39.614¢ per Critical Peak kWh",
                        "confidence": 0.95,
                    }
                ]
            ),
            0.95,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    assert report["rows"][0]["row_results"][0]["unit_grounded"] is True


def test_validate_candidate_extractions_uses_nearby_context_for_monthly_units(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        ("doc.pdf", 1, "Basic Customer Charge, per month $14.00"),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Fixed Monthly Charge",
                        "value": 14.0,
                        "unit": "$/month",
                        "source_quote": "$14.00",
                        "confidence": 0.95,
                    }
                ]
            ),
            0.95,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    assert report["rows"][0]["row_results"][0]["unit_grounded"] is True


def test_validate_candidate_extractions_persists_mixed_row_statuses(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "Basic Facilities Charge $12.50 per month\n"
            "The currently approved cents/kWh rider increment or decrement must be added.",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Fixed Monthly Charge",
                        "value": 12.50,
                        "unit": "$/month",
                        "source_quote": "Basic Facilities Charge $12.50 per month",
                        "confidence": 0.95,
                    },
                    {
                        "charge_type": "Rider Adjustment",
                        "value": 0.0,
                        "unit": "¢/kWh",
                        "source_quote": "The currently approved cents/kWh rider increment or decrement must be added.",
                        "confidence": 0.9,
                    },
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=True)

    assert report["summary"]["recommended_status_counts"] == {"review_candidate": 1}
    assert report["summary"]["row_recommended_status_counts"] == {
        "rejected": 1,
        "validated": 1,
    }
    assert report["row_validation_upserts"] == 2
    conn = sqlite3.connect(db_path)
    statuses = conn.execute(
        """
        SELECT row_index, recommended_status
        FROM llm_candidate_rate_row_validations
        ORDER BY row_index
        """
    ).fetchall()
    extraction_status = conn.execute(
        "SELECT status FROM llm_candidate_rate_extractions"
    ).fetchone()[0]
    conn.close()
    assert statuses == [(0, "validated"), (1, "rejected")]
    assert extraction_status == "review_candidate"


def test_validate_candidate_extractions_low_model_confidence_does_not_block_grounded_row(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        ("doc.pdf", 1, "CPRE Factor 0.006¢/kWh"),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Rider Adjustment",
                        "value": 0.006,
                        "unit": "¢/kWh",
                        "source_quote": "CPRE Factor 0.006¢/kWh",
                        "confidence": 0.2,
                    }
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    assert report["rows"][0]["row_results"][0]["issues"] == []


def test_validate_candidate_extractions_infers_lighting_monthly_unit_from_table_header(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "RATE\nAll-night street lighting service.\n"
            "Lamp Rating Per Month Per Luminaire\n"
            "kWh Per New Pole Served\n"
            "Lumens Month Style Existing Pole New Pole Underground\n"
            "9,500 47 Urban $11.61 NA NA\n"
            "7,500 75 Urban (4) $10.60 NA NA",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Lighting Charge",
                        "value": 11.61,
                        "unit": "$/month",
                        "source_quote": "9,500 47 Urban $11.61 NA NA",
                        "confidence": 0.9,
                    },
                    {
                        "charge_type": "Energy Charge",
                        "value": 10.60,
                        "unit": "$/kWh",
                        "source_quote": "7,500 75 Urban (4) $10.60 NA NA",
                        "confidence": 0.9,
                    },
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=True)

    row_results = report["rows"][0]["row_results"]
    assert report["summary"]["recommended_status_counts"] == {"review_candidate": 1}
    assert row_results[0]["recommended_status"] == "validated"
    assert row_results[0]["inferred_unit"] == "$/month"
    assert row_results[0]["inferred_unit_reason"] == "lighting_table_per_month_per_luminaire"
    assert row_results[1]["recommended_status"] == "review_candidate"
    assert row_results[1]["inferred_unit"] == "$/month"
    assert "unit_conflicts_with_inferred" in row_results[1]["issues"]
    conn = sqlite3.connect(db_path)
    persisted = conn.execute(
        """
        SELECT inferred_unit, inferred_unit_reason, recommended_status
        FROM llm_candidate_rate_row_validations
        ORDER BY row_index
        """
    ).fetchall()
    conn.close()
    assert persisted == [
        ("$/month", "lighting_table_per_month_per_luminaire", "validated"),
        ("$/month", "lighting_table_per_month_per_luminaire", "review_candidate"),
    ]


def test_validate_candidate_extractions_infers_missing_kw_unit_from_explicit_context(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        ("doc.pdf", 1, "On-Peak Demand per month, per kW $2.53"),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Demand Charge",
                        "value": 2.53,
                        "unit": "",
                        "source_quote": "On-Peak Demand per month, per kW $2.53",
                        "confidence": 0.9,
                    }
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    row = report["rows"][0]["row_results"][0]
    assert row["inferred_unit"] == "$/kW"
    assert row["issues"] == []


def test_validate_candidate_extractions_prefers_fixed_charge_context_over_nearby_energy(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "MONTHLY RATE\n"
            "I. For Single-Phase Service:\n"
            "A. $22.00 Basic Customer Charge\n"
            "B. Kilowatt-Hour Energy Charge:\n"
            "12.664¢ per kWh for the first 750 kWh",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Fixed Monthly Charge",
                        "value": 22.0,
                        "unit": "$/month",
                        "source_quote": "$22.00",
                        "confidence": 0.9,
                    }
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    row = report["rows"][0]["row_results"][0]
    assert row["inferred_unit"] == "$/month"
    assert row["inferred_unit_reason"] == "fixed_charge_monthly_context"
    assert row["issues"] == []


def test_validate_candidate_extractions_infers_bare_lighting_monthly_table_values(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "MONTHLY RATE\n"
            "The following amount will be added to each monthly bill:\n"
            "Monthly Charge\n"
            "Per Customer\n"
            "LED 50 light emitting diode 2.42",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Lighting Charge",
                        "value": 2.42,
                        "unit": "$",
                        "source_quote": "LED 50 light emitting diode 2.42",
                        "confidence": 0.9,
                    }
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    row = report["rows"][0]["row_results"][0]
    assert row["inferred_unit"] == "$/month"
    assert row["inferred_unit_reason"] in {
        "lighting_table_per_month_per_luminaire",
        "nearest_header_monthly_lighting",
    }
    assert row["issues"] == []


def test_validate_candidate_extractions_accepts_compact_unit_notation(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "Commercial/Governmental Classification - $6.11/month\n"
            "Transmission Service Transformation Discount $0.48/kW\n"
            "Distribution Service Transformation Discount $0.0001/kWh",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Fixed Monthly Charge",
                        "value": 6.11,
                        "unit": "$/month",
                        "source_quote": "Commercial/Governmental Classification - $6.11/month",
                        "confidence": 0.9,
                    },
                    {
                        "charge_type": "Demand Charge",
                        "value": 0.48,
                        "unit": "",
                        "source_quote": "Transmission Service Transformation Discount $0.48/kW",
                        "confidence": 0.9,
                    },
                    {
                        "charge_type": "Energy Charge",
                        "value": 0.0001,
                        "unit": "",
                        "source_quote": "Distribution Service Transformation Discount $0.0001/kWh",
                        "confidence": 0.9,
                    },
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    rows = report["rows"][0]["row_results"]
    assert rows[0]["unit_grounded"] is True
    assert rows[1]["inferred_unit"] == "$/kW"
    assert rows[2]["inferred_unit"] == "$/kWh"
    assert all(row["issues"] == [] for row in rows)


def test_validate_candidate_extractions_infers_units_from_nearest_table_headers(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "Rate Class Applicable Schedule(s) Incremental Rate*\n"
            "Non-Demand Rate Class (dollars per kilowatt-hour)\n"
            "Residential RES, R-TOUD, R-TOU, 0.00464\n"
            "Demand Rate Classes (dollars per kilowatt)\n"
            "Medium General Service MGS, GS-TES, APH-TES, MGS- 0.92\n"
            "Rate Class Applicable Schedules Billing Rate\n"
            "(¢/kWh)\n"
            "Industrial HP, I, HLF, OPT-V, PG, SGSTC 0.0066",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Energy Charge",
                        "value": 0.00464,
                        "unit": "$/kWh",
                        "source_quote": "Residential RES, R-TOUD, R-TOU, 0.00464",
                        "confidence": 0.9,
                    },
                    {
                        "charge_type": "Demand Charge",
                        "value": 0.92,
                        "unit": "$/kW",
                        "source_quote": "Medium General Service MGS, GS-TES, APH-TES, MGS- 0.92",
                        "confidence": 0.9,
                    },
                    {
                        "charge_type": "Rider Adjustment",
                        "value": 0.0066,
                        "unit": "¢/kWh",
                        "source_quote": "Industrial HP, I, HLF, OPT-V, PG, SGSTC 0.0066",
                        "confidence": 0.9,
                    },
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    rows = report["rows"][0]["row_results"]
    assert rows[0]["inferred_unit"] == "$/kWh"
    assert rows[1]["inferred_unit"] == "$/kW"
    assert rows[2]["inferred_unit"] == "¢/kWh"
    assert all(row["issues"] == [] for row in rows)


def test_validate_candidate_extractions_bare_numeric_per_kwh_defaults_to_cents(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        ("doc.pdf", 1, "C. kWh Energy Charge:\n6.0370 per on-peak kWh"),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Energy Charge",
                        "value": 6.037,
                        "unit": "",
                        "source_quote": "6.0370 per on-peak kWh",
                        "confidence": 0.9,
                    }
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    row = report["rows"][0]["row_results"][0]
    assert row["inferred_unit"] == "¢/kWh"
    assert row["inferred_unit_reason"] == "bare_numeric_per_kwh_assumed_cents"
    assert row["issues"] == []


def test_validate_candidate_extractions_explicit_quote_unit_beats_nearby_header(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "Rate Class Applicable Schedules Billing Rate\n"
            "(¢/kWh)\n"
            "VI. Incremental Demand Charge = $0.96 per kW of Incremental Demand",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Demand Charge",
                        "value": 0.96,
                        "unit": "$/kW",
                        "source_quote": "VI. Incremental Demand Charge = $0.96 per kW of Incremental Demand",
                        "confidence": 0.9,
                    }
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    row = report["rows"][0]["row_results"][0]
    assert row["inferred_unit"] == "$/kW"
    assert row["inferred_unit_reason"] == "explicit_per_kw_quote"
    assert row["issues"] == []


def test_validate_candidate_extractions_matches_split_line_table_rows(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts (source_pdf, page_number, text_content) VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "Non-Demand Rate Class (dollars per kilowatt-hour)\n"
            "Seasonal and Intermittent SI\n"
            "Service 0.01075",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_extractions
        (historical_document_id, source_pdf, rate_rows_json, extraction_confidence, model, model_role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "doc.pdf",
            json.dumps(
                [
                    {
                        "charge_type": "Energy Charge",
                        "value": 0.01075,
                        "unit": "$/kWh",
                        "source_quote": "Seasonal and Intermittent SI Service 0.01075",
                        "confidence": 0.9,
                    }
                ]
            ),
            0.9,
            "test-model",
            "structured_rate_extraction",
        ),
    )
    conn.commit()
    conn.close()

    report = validate_candidate_extractions(db_path, execute=False)

    assert report["summary"]["recommended_status_counts"] == {"validated": 1}
    row = report["rows"][0]["row_results"][0]
    assert row["inferred_unit"] == "$/kWh"
    assert row["inferred_unit_reason"] == "nearest_header_dollars_per_kwh"
    assert row["issues"] == []
