from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from duke_rates.db.parse_review import (
    build_parse_review_summary,
    list_parse_review_queue,
    reconcile_skipped_rule_reviews,
    record_parse_review_outcome,
)
from duke_rates.db.sqlite import connect


def _seed_parse_attempt(conn, *, status: str = "parsed", review_flags: list[str] | None = None) -> int:
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "data/historical/ncuc/e-2-sub-1300/sample.pdf",
            "e-2-sub-1300",
            1,
            2,
            "historical_bulk",
            "generic_residential",
            status,
            0.44,
            "DEP",
            "RES",
            "2024-01-01",
            1,
            json.dumps(review_flags or ["generic_fallback_selected"]),
            json.dumps({"family_key": "nc-progress-leaf-500"}, sort_keys=True),
            now,
        ),
    )
    return int(cur.lastrowid)


def _seed_historical_document(
    conn,
    *,
    family_key: str,
    company: str = "progress",
    state: str = "NC",
    archived_url: str = "https://example.com/archive.pdf",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO historical_documents (
            current_document_id, family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path, raw_text_path,
            content_hash, content_type, direct_status_code, direct_downloadable,
            revision_label, supersedes_label, leaf_no, effective_start, effective_end,
            retrieved_at, metadata_json, parsed_result_json, start_page, end_page, evidence_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            None,
            family_key,
            family_key,
            state,
            company,
            "tariff",
            "pdf",
            archived_url,
            archived_url,
            "2024-01-01T00:00:00+00:00",
            f"data/historical/{family_key}.pdf",
            None,
            f"hash-{family_key}",
            "application/pdf",
            200,
            1,
            None,
            None,
            None,
            "2024-01-01",
            None,
            "2024-01-02T00:00:00+00:00",
            "{}",
            None,
            None,
            None,
            "{}",
        ),
    )
    return int(cur.lastrowid)


def test_record_parse_review_outcome_accepts_manual_correction_and_removes_from_queue(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    attempt_id = _seed_parse_attempt(conn)
    conn.execute(
        """
        INSERT INTO parse_review_outcomes (
            parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
            parser_stage, parser_profile, utility, review_source, outcome,
            correction_count, notes_json, corrections_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt_id,
            "data/historical/ncuc/e-2-sub-1300/sample.pdf",
            "e-2-sub-1300",
            1,
            2,
            "historical_bulk",
            "generic_residential",
            "DEP",
            "rule",
            "needs_review",
            0,
            json.dumps({"review_flags": ["generic_fallback_selected"]}, sort_keys=True),
            "{}",
            datetime(2026, 3, 26, tzinfo=UTC).isoformat(),
        ),
    )
    conn.commit()

    queued = list_parse_review_queue(conn)
    assert [row["parse_attempt_id"] for row in queued] == [attempt_id]

    review_id = record_parse_review_outcome(
        conn,
        parse_attempt_id=attempt_id,
        outcome="corrected",
        notes={"note": "Verified against original table", "correction_count": 2},
        corrections={"fixed_charge": 14.0, "energy_charge": 0.1234},
    )
    conn.commit()

    assert review_id > 0
    latest = conn.execute(
        """
        SELECT review_source, outcome, correction_count, notes_json, corrections_json
        FROM parse_review_outcomes
        WHERE parse_attempt_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (attempt_id,),
    ).fetchone()
    assert latest is not None
    assert latest["review_source"] == "human"
    assert latest["outcome"] == "corrected"
    assert latest["correction_count"] == 2
    notes = json.loads(latest["notes_json"])
    assert notes["note"] == "Verified against original table"
    assert notes["correction_fields"] == ["energy_charge", "fixed_charge"]
    assert notes["correction_categories"] == ["charge_value"]
    assert json.loads(latest["corrections_json"])["fixed_charge"] == 14.0

    assert list_parse_review_queue(conn) == []
    conn.close()


def test_record_parse_review_outcome_rejects_unknown_outcome(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    attempt_id = _seed_parse_attempt(conn, review_flags=[])
    with pytest.raises(ValueError, match="Unsupported review outcome"):
        record_parse_review_outcome(conn, parse_attempt_id=attempt_id, outcome="maybe")
    conn.close()


def test_build_parse_review_summary_groups_corrections_by_profile_and_family(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    attempt_1 = _seed_parse_attempt(conn)
    attempt_2 = _seed_parse_attempt(conn)
    attempt_3 = _seed_parse_attempt(conn, review_flags=[])

    conn.execute(
        "UPDATE parse_attempt_logs SET source_pdf = ?, page_start = ?, page_end = ? WHERE id = ?",
        ("data/historical/ncuc/e-2-sub-1300/sample-1.pdf", 1, 2, attempt_1),
    )
    conn.execute(
        "UPDATE parse_attempt_logs SET source_pdf = ?, page_start = ?, page_end = ? WHERE id = ?",
        ("data/historical/ncuc/e-2-sub-1300/sample-2.pdf", 3, 4, attempt_2),
    )
    conn.execute(
        "UPDATE parse_attempt_logs SET source_pdf = ?, page_start = ?, page_end = ? WHERE id = ?",
        ("data/historical/ncuc/e-2-sub-1300/sample-3.pdf", 5, 6, attempt_3),
    )

    conn.execute(
        """
        UPDATE parse_attempt_logs
        SET parser_profile = ?, metadata_json = ?, utility = ?, confidence = ?
        WHERE id = ?
        """,
        (
            "progress_residential_tou",
            json.dumps({"family_key": "nc-progress-leaf-502", "company": "progress"}, sort_keys=True),
            "DEP",
            0.91,
            attempt_1,
        ),
    )
    conn.execute(
        """
        UPDATE parse_attempt_logs
        SET parser_profile = ?, metadata_json = ?, utility = ?, confidence = ?
        WHERE id = ?
        """,
        (
            "generic_residential",
            json.dumps({"family_key": "nc-progress-leaf-500", "company": "progress"}, sort_keys=True),
            "DEP",
            0.42,
            attempt_2,
        ),
    )
    conn.execute(
        """
        UPDATE parse_attempt_logs
        SET parser_profile = ?, metadata_json = ?, utility = ?, confidence = ?
        WHERE id = ?
        """,
        (
            "carolinas_residential_flat",
            json.dumps({"family_key": "nc-carolinas-schedule-RS", "company": "carolinas"}, sort_keys=True),
            "DEC",
            0.88,
            attempt_3,
        ),
    )

    for attempt_id, outcome, source, correction_count in (
        (attempt_1, "corrected", "human", 2),
        (attempt_2, "needs_review", "rule", 0),
        (attempt_3, "rejected", "human", 1),
    ):
        corrections_payload = {"dummy": correction_count} if correction_count else {}
        conn.execute(
            """
            INSERT INTO parse_review_outcomes (
                parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
                parser_stage, parser_profile, utility, review_source, outcome,
                correction_count, notes_json, corrections_json, created_at
            )
            SELECT
                id, source_pdf, docket_dir, page_start, page_end,
                parser_stage, parser_profile, utility, ?, ?,
                ?, ?, ?, ?
            FROM parse_attempt_logs
            WHERE id = ?
            """,
            (
                source,
                outcome,
                correction_count,
                json.dumps({"seeded": True}, sort_keys=True),
                json.dumps(corrections_payload, sort_keys=True),
                datetime(2026, 3, 26, tzinfo=UTC).isoformat(),
                attempt_id,
            ),
        )

    conn.commit()

    report = build_parse_review_summary(conn, top_n=5)
    assert report["summary"]["reviewed_attempt_count"] == 3
    assert report["summary"]["outstanding_needs_review"] == 1
    assert report["summary"]["corrected_count"] == 1
    assert report["summary"]["rejected_count"] == 1
    assert report["summary"]["human_review_count"] == 2
    assert report["summary"]["total_corrections_applied"] == 3
    assert report["top_correction_categories"][0] == {"category": "other", "count": 2}
    assert report["top_correction_fields"][0] == {"field": "dummy", "count": 2}
    assert report["top_root_causes"][0] == {"root_cause": "generic_fallback_selected", "count": 1}

    top_profiles = {row["parser_profile"]: row for row in report["top_profiles"]}
    assert top_profiles["generic_residential"]["needs_review"] == 1
    assert top_profiles["progress_residential_tou"]["corrected"] == 1
    assert top_profiles["progress_residential_tou"]["correction_count"] == 2
    assert top_profiles["progress_residential_tou"]["top_correction_categories"] == [{"category": "other", "count": 1}]
    assert top_profiles["progress_residential_tou"]["top_root_causes"] == []
    assert top_profiles["carolinas_residential_flat"]["rejected"] == 1

    top_families = {row["family_key"]: row for row in report["top_families"]}
    assert top_families["nc-progress-leaf-502"]["corrected"] == 1
    assert top_families["nc-progress-leaf-500"]["needs_review"] == 1
    assert top_families["nc-carolinas-schedule-RS"]["rejected"] == 1
    conn.close()


def test_build_parse_review_summary_groups_needs_review_root_causes(tmp_path) -> None:
    conn = connect(tmp_path / "root-causes.db")
    attempts = [
        _seed_parse_attempt(conn, review_flags=["no_charges_extracted"]),
        _seed_parse_attempt(conn, review_flags=["generic_fallback_selected"]),
        _seed_parse_attempt(conn, review_flags=["low_selector_confidence"]),
        _seed_parse_attempt(conn, review_flags=["sparse_charge_set"]),
        _seed_parse_attempt(conn, status="skipped_order", review_flags=["skipped_order"]),
    ]

    for index, attempt_id in enumerate(attempts, start=1):
        conn.execute(
            "UPDATE parse_attempt_logs SET source_pdf = ?, page_start = ?, page_end = ? WHERE id = ?",
            (f"data/historical/ncuc/e-2-sub-1300/root-{index}.pdf", index, index, attempt_id),
        )
        conn.execute(
            """
            INSERT INTO parse_review_outcomes (
                parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
                parser_stage, parser_profile, utility, review_source, outcome,
                correction_count, notes_json, corrections_json, created_at
            )
            SELECT
                id, source_pdf, docket_dir, page_start, page_end,
                parser_stage, parser_profile, utility, 'rule', 'needs_review', 0, '{}', '{}', ?
            FROM parse_attempt_logs
            WHERE id = ?
            """,
            (
                datetime(2026, 3, 26, tzinfo=UTC).isoformat(),
                attempt_id,
            ),
        )
    conn.commit()

    report = build_parse_review_summary(conn, top_n=5)

    root_causes = {row["root_cause"]: row["count"] for row in report["top_root_causes"]}
    assert root_causes["no_charges_extracted"] == 1
    assert root_causes["generic_fallback_selected"] == 1
    assert root_causes["low_selector_confidence"] == 1
    assert root_causes["sparse_charge_set"] == 1
    assert root_causes["skipped_status"] == 1

    top_profiles = {row["parser_profile"]: row for row in report["top_profiles"]}
    assert top_profiles["generic_residential"]["top_root_causes"][0]["root_cause"] == "no_charges_extracted"
    conn.close()


def test_reconcile_skipped_rule_reviews_accepts_stale_skipped_attempts(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    skipped_attempt_id = _seed_parse_attempt(conn, status="skipped_order", review_flags=["skipped_order"])
    parsed_attempt_id = _seed_parse_attempt(conn, status="parsed", review_flags=["generic_fallback_selected"])

    conn.execute(
        "UPDATE parse_attempt_logs SET source_pdf = ?, page_start = ?, page_end = ? WHERE id = ?",
        ("data/historical/ncuc/e-2-sub-1300/skipped.pdf", 1, 2, skipped_attempt_id),
    )
    conn.execute(
        "UPDATE parse_attempt_logs SET source_pdf = ?, page_start = ?, page_end = ? WHERE id = ?",
        ("data/historical/ncuc/e-2-sub-1300/parsed.pdf", 3, 4, parsed_attempt_id),
    )

    for attempt_id, outcome in (
        (skipped_attempt_id, "needs_review"),
        (parsed_attempt_id, "needs_review"),
    ):
        conn.execute(
            """
            INSERT INTO parse_review_outcomes (
                parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
                parser_stage, parser_profile, utility, review_source, outcome,
                correction_count, notes_json, corrections_json, created_at
            )
            SELECT
                id, source_pdf, docket_dir, page_start, page_end,
                parser_stage, parser_profile, utility, 'rule', ?, 0, ?, '{}', ?
            FROM parse_attempt_logs
            WHERE id = ?
            """,
            (
                outcome,
                json.dumps({"seeded": True}, sort_keys=True),
                datetime(2026, 3, 26, tzinfo=UTC).isoformat(),
                attempt_id,
            ),
        )
    conn.commit()

    report = reconcile_skipped_rule_reviews(conn)
    conn.commit()

    assert report["reconciled"] == 1
    assert report["parse_attempt_ids"] == [skipped_attempt_id]

    latest_skipped = conn.execute(
        """
        SELECT outcome, review_source, notes_json
        FROM parse_review_outcomes
        WHERE parse_attempt_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (skipped_attempt_id,),
    ).fetchone()
    assert latest_skipped is not None
    assert latest_skipped["outcome"] == "accepted"
    assert latest_skipped["review_source"] == "rule"
    notes = json.loads(latest_skipped["notes_json"])
    assert notes["reconciled_reason"] == "skipped_status_now_accepted"
    assert notes["status"] == "skipped_order"

    queued = list_parse_review_queue(conn)
    assert [row["parse_attempt_id"] for row in queued] == [parsed_attempt_id]
    conn.close()


def test_parse_review_queue_and_summary_only_count_latest_attempt_per_source_span(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    source_pdf = "data/historical/ncuc/e-2-sub-1300/sample.pdf"
    created_at = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    old_attempt = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_pdf,
            "e-2-sub-1300",
            1,
            2,
            "historical_bulk",
            "generic_residential",
            "parsed",
            0.25,
            "DEP",
            "RES",
            "2024-01-01",
            0,
            json.dumps(["generic_fallback_selected"]),
            json.dumps({"family_key": "nc-progress-leaf-500"}, sort_keys=True),
            created_at,
        ),
    ).lastrowid
    new_attempt = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_pdf,
            "e-2-sub-1300",
            1,
            2,
            "historical_bulk",
            "progress_residential_flat",
            "parsed",
            0.91,
            "DEP",
            "RES",
            "2024-01-01",
            2,
            "[]",
            json.dumps({"family_key": "nc-progress-leaf-500"}, sort_keys=True),
            datetime(2026, 3, 27, tzinfo=UTC).isoformat(),
        ),
    ).lastrowid

    for attempt_id, outcome in (
        (old_attempt, "needs_review"),
        (new_attempt, "accepted"),
    ):
        conn.execute(
            """
            INSERT INTO parse_review_outcomes (
                parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
                parser_stage, parser_profile, utility, review_source, outcome,
                correction_count, notes_json, corrections_json, created_at
            )
            SELECT
                id, source_pdf, docket_dir, page_start, page_end,
                parser_stage, parser_profile, utility, 'rule', ?, 0, '{}', '{}', ?
            FROM parse_attempt_logs
            WHERE id = ?
            """,
            (
                outcome,
                datetime(2026, 3, 27, tzinfo=UTC).isoformat(),
                attempt_id,
            ),
        )
    conn.commit()

    assert list_parse_review_queue(conn) == []

    report = build_parse_review_summary(conn, top_n=5)
    assert report["summary"]["reviewed_attempt_count"] == 1
    assert report["summary"]["outstanding_needs_review"] == 0
    assert report["summary"]["accepted_count"] == 1
    top_profiles = {row["parser_profile"]: row for row in report["top_profiles"]}
    assert top_profiles["progress_residential_flat"]["accepted"] == 1
    assert "generic_residential" not in top_profiles
    conn.close()


def test_list_parse_review_queue_supports_family_profile_and_source_filters(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    created_at = datetime(2026, 3, 27, tzinfo=UTC).isoformat()

    attempts: list[tuple[int, str, str, str]] = []
    for source_pdf, family_key, parser_profile in (
        ("data/historical/ncuc/one.pdf", "nc-progress-leaf-500", "generic_residential"),
        ("data/historical/ncuc/two.pdf", "nc-progress-leaf-672", "generic_residential"),
        ("data/historical/ncuc/three.pdf", "nc-progress-leaf-717", "progress_demand_response_automation"),
    ):
        attempt_id = conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, docket_dir, page_start, page_end, parser_stage,
                parser_profile, status, confidence, utility, schedule_code,
                effective_date, charge_count, review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_pdf,
                "test-docket",
                1,
                1,
                "historical_bulk",
                parser_profile,
                "parsed",
                0.40,
                "DEP",
                None,
                "2024-01-01",
                0,
                json.dumps(["generic_fallback_selected"]),
                json.dumps({"family_key": family_key}, sort_keys=True),
                created_at,
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO parse_review_outcomes (
                parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
                parser_stage, parser_profile, utility, review_source, outcome,
                correction_count, notes_json, corrections_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                attempt_id,
                source_pdf,
                "test-docket",
                1,
                1,
                "historical_bulk",
                parser_profile,
                "DEP",
                "rule",
                "needs_review",
                0,
                "{}",
                "{}",
                created_at,
            ),
        )
        attempts.append((attempt_id, source_pdf, family_key, parser_profile))
    conn.commit()

    by_family = list_parse_review_queue(conn, limit=10, family_key="nc-progress-leaf-672")
    assert [row["parse_attempt_id"] for row in by_family] == [attempts[1][0]]

    by_profile = list_parse_review_queue(conn, limit=10, parser_profile="progress_demand_response_automation")
    assert [row["parse_attempt_id"] for row in by_profile] == [attempts[2][0]]

    by_source = list_parse_review_queue(conn, limit=10, source_pdf="data/historical/ncuc/one.pdf")
    assert [row["parse_attempt_id"] for row in by_source] == [attempts[0][0]]
    conn.close()


def test_parse_review_queue_ignores_deleted_historical_documents(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    created_at = datetime(2026, 3, 27, tzinfo=UTC).isoformat()

    attempt_id = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "data/historical/ncuc/deleted.pdf",
            "test-docket",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "parsed",
            0.22,
            "DEP",
            None,
            "2024-01-01",
            0,
            json.dumps(["generic_fallback_selected"]),
            json.dumps(
                {
                    "family_key": "nc-progress-leaf-500",
                    "company": "progress",
                    "historical_document_id": 999999,
                },
                sort_keys=True,
            ),
            created_at,
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO parse_review_outcomes (
            parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
            parser_stage, parser_profile, utility, review_source, outcome,
            correction_count, notes_json, corrections_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt_id,
            "data/historical/ncuc/deleted.pdf",
            "test-docket",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "DEP",
            "rule",
            "needs_review",
            0,
            "{}",
            "{}",
            created_at,
        ),
    )
    conn.commit()

    assert list_parse_review_queue(conn) == []
    report = build_parse_review_summary(conn, top_n=5)
    assert report["summary"]["reviewed_attempt_count"] == 0
    assert report["summary"]["outstanding_needs_review"] == 0
    conn.close()


def test_parse_review_summary_uses_current_historical_family_metadata(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    created_at = datetime(2026, 3, 27, tzinfo=UTC).isoformat()
    historical_id = _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-661",
        archived_url="https://example.com/leaf-661.pdf",
    )

    attempt_id = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "data/historical/ncuc/moved.pdf",
            "test-docket",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "parsed",
            0.31,
            "DEP",
            None,
            "2024-01-01",
            0,
            json.dumps(["generic_fallback_selected"]),
            json.dumps(
                {
                    "family_key": "nc-progress-leaf-500",
                    "company": "carolinas",
                    "historical_document_id": historical_id,
                },
                sort_keys=True,
            ),
            created_at,
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO parse_review_outcomes (
            parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
            parser_stage, parser_profile, utility, review_source, outcome,
            correction_count, notes_json, corrections_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt_id,
            "data/historical/ncuc/moved.pdf",
            "test-docket",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "DEP",
            "rule",
            "needs_review",
            0,
            "{}",
            "{}",
            created_at,
        ),
    )
    conn.commit()

    queue = list_parse_review_queue(conn, family_key="nc-progress-leaf-661")
    assert [row["parse_attempt_id"] for row in queue] == [attempt_id]
    assert list_parse_review_queue(conn, family_key="nc-progress-leaf-500") == []

    report = build_parse_review_summary(conn, top_n=5)
    top_families = {row["family_key"]: row for row in report["top_families"]}
    assert "nc-progress-leaf-500" not in top_families
    assert top_families["nc-progress-leaf-661"]["company"] == "progress"
    assert top_families["nc-progress-leaf-661"]["needs_review"] == 1
    conn.close()


def test_parse_review_summary_collapses_to_latest_attempt_per_historical_document(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    created_at = datetime(2026, 3, 27, tzinfo=UTC).isoformat()
    historical_id = _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-640",
        archived_url="https://example.com/leaf-640.pdf",
    )

    old_attempt = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "data/historical/ncuc/old-source.pdf",
            "test-docket",
            1,
            4,
            "historical_bulk",
            "generic_residential",
            "parsed",
            0.20,
            "DEP",
            None,
            "2024-01-01",
            0,
            json.dumps(["generic_fallback_selected"]),
            json.dumps(
                {
                    "family_key": "nc-progress-leaf-640",
                    "company": "progress",
                    "historical_document_id": historical_id,
                },
                sort_keys=True,
            ),
            created_at,
        ),
    ).lastrowid
    new_attempt = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "data/historical/ncuc/new-source.pdf",
            "test-docket",
            1,
            4,
            "historical_bulk",
            None,
            "skipped_testimony",
            0.0,
            "DEP",
            None,
            "2024-01-01",
            0,
            json.dumps(["skipped_testimony"]),
            json.dumps(
                {
                    "family_key": "nc-progress-leaf-640",
                    "company": "progress",
                    "historical_document_id": historical_id,
                },
                sort_keys=True,
            ),
            created_at,
        ),
    ).lastrowid

    for attempt_id, source_pdf, outcome in (
        (old_attempt, "data/historical/ncuc/old-source.pdf", "needs_review"),
        (new_attempt, "data/historical/ncuc/new-source.pdf", "accepted"),
    ):
        conn.execute(
            """
            INSERT INTO parse_review_outcomes (
                parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
                parser_stage, parser_profile, utility, review_source, outcome,
                correction_count, notes_json, corrections_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                attempt_id,
                source_pdf,
                "test-docket",
                1,
                4,
                "historical_bulk",
                "generic_residential",
                "DEP",
                "rule",
                outcome,
                0,
                "{}",
                "{}",
                created_at,
            ),
        )
    conn.commit()

    assert list_parse_review_queue(conn) == []

    report = build_parse_review_summary(conn, top_n=5)
    assert report["summary"]["reviewed_attempt_count"] == 1
    assert report["summary"]["outstanding_needs_review"] == 0
    assert report["summary"]["accepted_count"] == 1
    top_families = {row["family_key"]: row for row in report["top_families"]}
    assert top_families["nc-progress-leaf-640"]["accepted"] == 1
    assert top_families["nc-progress-leaf-640"]["needs_review"] == 0
    conn.close()
