import json

from duke_rates.db.ncuc_loader import (
    persist_ingest_result_records,
    persist_rider_summary_records,
)
from duke_rates.db.sqlite import connect


def test_persist_ingest_result_records_writes_segments_and_diagnostics(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    record = {
        "segment": {
            "leaf_no": "500",
            "schedule_code": "RES",
            "revision": "RES-86",
            "title": "Residential Service",
            "page_range": [1, 2],
            "source_pdf": "data/historical/ncuc/e-2-sub-1300/progress-energy-carolinas-leaf-500.pdf",
        },
        "source_pdf": "data/historical/ncuc/e-2-sub-1300/progress-energy-carolinas-leaf-500.pdf",
        "tier": 2,
        "status": "parsed",
        "confidence": 0.91,
        "schedule_code": "RES",
        "schedule_title": "Residential Service",
        "effective_date": "2024-01-01",
        "customer_class": "Residential",
        "energy_charges": [{"label": "All kWh", "rate": 0.1234, "unit": "$/kWh"}],
        "fixed_charges": [{"label": "Customer Charge", "amount": 14.0, "unit": "$/month"}],
        "demand_charges": [],
        "riders": ["RIDER BA"],
        "table_rows": [["Customer Charge", "$14.00"]],
        "review_flags": ["table_extraction"],
        "page_range": [1, 2],
        "text_length": 512,
        "line_count": 18,
        "numeric_line_count": 10,
        "has_rider_summary": False,
        "supersedes": "RES-85",
        "docket_number": "E-2, Sub 1300",
        "order_date": "2024-01-15",
    }

    inserted, skipped = persist_ingest_result_records(conn, [record])
    assert (inserted, skipped) == (1, 0)

    inserted, skipped = persist_ingest_result_records(conn, [record])
    assert (inserted, skipped) == (0, 1)

    segment = conn.execute(
        """
        SELECT schedule_code, utility, status, confidence, effective_date
        FROM ncuc_ingest_segments
        """
    ).fetchone()
    assert segment is not None
    assert segment["schedule_code"] == "RES"
    assert segment["utility"] == "DEP"
    assert segment["status"] == "parsed"
    assert segment["effective_date"] == "2024-01-01"

    fingerprint = conn.execute(
        """
        SELECT text_length, line_count, numeric_line_count, review_flags_json, metadata_json
        FROM document_fingerprints
        """
    ).fetchone()
    assert fingerprint is not None
    assert fingerprint["text_length"] == 512
    assert fingerprint["line_count"] == 18
    assert json.loads(fingerprint["review_flags_json"]) == ["table_extraction"]
    assert json.loads(fingerprint["metadata_json"])["charge_count"] == 2

    attempts = conn.execute(
        """
        SELECT parser_stage, parser_profile, utility, charge_count
        FROM parse_attempt_logs
        ORDER BY id
        """
    ).fetchall()
    assert len(attempts) == 2
    assert all(row["parser_stage"] == "table" for row in attempts)
    assert all(row["parser_profile"] == "tiered_ingest" for row in attempts)
    assert all(row["utility"] == "DEP" for row in attempts)
    assert all(row["charge_count"] == 2 for row in attempts)

    review = conn.execute(
        """
        SELECT parse_attempt_id, review_source, outcome, notes_json
        FROM parse_review_outcomes
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()
    assert review is not None
    assert review["parse_attempt_id"] == 1
    assert review["review_source"] == "rule"
    assert review["outcome"] == "needs_review"
    assert json.loads(review["notes_json"])["review_flags"] == ["table_extraction"]

    conn.close()


def test_persist_rider_summary_records_infers_utility_and_skips_duplicates(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    record = {
        "source_pdf": "data/historical/ncuc/e-7-sub-111/duke-power-leaf-99.pdf",
        "leaf_no": "99",
        "effective_date": "2024-02-01",
        "docket_number": "E-7, Sub 111",
        "order_date": "2024-02-15",
        "supersedes": "Leaf 98",
        "rate_classes": [
            {
                "rate_class": "Residential Schedules",
                "applicable_schedules": ["RS"],
                "total_cents_per_kwh": "1.234",
                "total_dollars_per_kw": None,
                "line_items": [
                    {
                        "label": "Rider BA",
                        "rider_code": "BA",
                        "cents_per_kwh": "0.456",
                        "dollars_per_kw": None,
                        "effective_date": "2024-02-01",
                    },
                    {
                        "label": "Rider EE",
                        "rider_code": "EE",
                        "cents_per_kwh": "0.778",
                        "dollars_per_kw": None,
                        "effective_date": "2024-02-01",
                        "is_total": True,
                    },
                ],
            }
        ],
    }

    inserted, skipped = persist_rider_summary_records(conn, [record])
    assert (inserted, skipped) == (1, 0)

    inserted, skipped = persist_rider_summary_records(conn, [record])
    assert (inserted, skipped) == (0, 1)

    block = conn.execute(
        """
        SELECT rate_class, utility, total_cents_per_kwh
        FROM rider_summary_blocks
        """
    ).fetchone()
    assert block is not None
    assert block["rate_class"] == "Residential Schedules"
    assert block["utility"] == "DEC"
    assert block["total_cents_per_kwh"] == 1.234

    counts = conn.execute(
        """
        SELECT COUNT(*) AS block_count,
               (SELECT COUNT(*) FROM rider_line_items) AS line_count
        FROM rider_summary_blocks
        """
    ).fetchone()
    assert counts["block_count"] == 1
    assert counts["line_count"] == 2

    conn.close()
