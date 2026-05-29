import subprocess
import sqlite3
from types import SimpleNamespace

from duke_rates.db.sqlite import connect
from duke_rates.document_intelligence import acquisition
from duke_rates.document_intelligence.action_registry import (
    _parse_effective_count,
    decide_actions,
)


def test_parse_ncuc_imported_count_reads_download_summary() -> None:
    assert (
        acquisition._parse_ncuc_imported_count(
            "Imported 13/15 downloaded NCUC records.\n"
        )
        == 13
    )


def test_parse_ncuc_imported_count_returns_none_for_unknown_output() -> None:
    assert acquisition._parse_ncuc_imported_count("nothing actionable") is None


def test_filter_fetch_inventory_cooldowns_removes_cooling_dockets() -> None:
    inventory = {
        "total_eligible": 3,
        "top_dockets": [
            {"docket_number": "E-7 Sub 145", "eligible_count": 6},
            {"docket_number": "E-2, Sub 1204", "eligible_count": 5},
        ],
    }

    filtered = acquisition._filter_fetch_inventory_cooldowns(
        inventory,
        {"E-7 Sub 145": 4, "expired": 0},
    )

    assert filtered["top_dockets"] == [
        {"docket_number": "E-2, Sub 1204", "eligible_count": 5}
    ]
    assert filtered["cooldown_excluded_dockets"] == ["E-7 Sub 145"]
    assert inventory["top_dockets"][0]["docket_number"] == "E-7 Sub 145"


def test_filter_fetch_inventory_removes_same_cycle_attempted_dockets() -> None:
    inventory = {
        "total_eligible": 3,
        "top_dockets": [
            {"docket_number": "E-7, Sub 1100", "eligible_count": 5},
            {"docket_number": "E-2, Sub 1204", "eligible_count": 5},
            {"docket_number": "E-2, Sub 1174", "eligible_count": 4},
        ],
    }

    filtered = acquisition._filter_fetch_inventory_cooldowns(
        inventory,
        {},
        excluded_dockets={"E-7, Sub 1100", "E-2, Sub 1204"},
    )

    assert filtered["top_dockets"] == [
        {"docket_number": "E-2, Sub 1174", "eligible_count": 4}
    ]
    assert filtered["same_cycle_excluded_dockets"] == [
        "E-2, Sub 1204",
        "E-7, Sub 1100",
    ]


def test_acquire_one_marks_fetch_no_progress_when_inventory_unchanged(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "fetch.db"
    conn = connect(db_path)
    conn.executemany(
        """
        INSERT INTO ncuc_discovery_records
            (id, docket_number, fetch_status, local_path, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, "E-7 Sub 145", "pending", None, "2026-01-01T00:00:00Z"),
            (2, "E-7 Sub 145", "pending", None, "2026-01-01T00:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Found 2 documents\n\nDone. 2 records persisted.\n",
            stderr="",
        )

    monkeypatch.setattr(acquisition.subprocess, "run", fake_run)

    result = acquisition._acquire_one(
        docket_number="E-7 Sub 145",
        docket_uuid="12345678-1234-1234-1234-123456789abc",
        action="fetch",
        database_path=str(db_path),
        run_global_post_steps=False,
    )

    assert result.outcome == "no_progress"
    assert result.fetch_eligible_before == 2
    assert result.fetch_eligible_after == 2
    assert result.fetch_eligible_delta == 0
    assert "did not reduce" in (result.error or "")
    assert result.stage_outcomes[-1]["fetch_eligible_delta"] == 0


def test_acquire_one_keeps_acquired_when_fetch_inventory_decreases(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "fetch.db"
    conn = connect(db_path)
    conn.executemany(
        """
        INSERT INTO ncuc_discovery_records
            (id, docket_number, fetch_status, local_path, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, "E-2 Sub 1354", "pending", None, "2026-01-01T00:00:00Z"),
            (2, "E-2 Sub 1354", "pending", None, "2026-01-01T00:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()

    def fake_run(cmd, **kwargs):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE ncuc_discovery_records SET fetch_status='success', local_path='doc.pdf' WHERE id=1"
        )
        conn.commit()
        conn.close()
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Found 2 documents\n\nDone. 2 records persisted.\n",
            stderr="",
        )

    monkeypatch.setattr(acquisition.subprocess, "run", fake_run)

    result = acquisition._acquire_one(
        docket_number="E-2 Sub 1354",
        docket_uuid="12345678-1234-1234-1234-123456789abc",
        action="fetch",
        database_path=str(db_path),
        run_global_post_steps=False,
    )

    assert result.outcome == "acquired"
    assert result.fetch_eligible_before == 2
    assert result.fetch_eligible_after == 1
    assert result.fetch_eligible_delta == 1
    assert result.stage_outcomes[-1]["fetch_eligible_delta"] == 1


def test_fetch_record_level_dockets_accumulates_fetch_delta(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "fetch.db"
    conn = connect(db_path)
    conn.executemany(
        """
        INSERT INTO ncuc_discovery_records
            (id, docket_number, fetch_status, local_path, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, "E-2 Sub 1354", "pending", None, "2026-01-01T00:00:00Z"),
            (2, "E-2 Sub 1354", "pending", None, "2026-01-01T00:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(acquisition, "_resolve_docket_uuid", lambda docket, timeout_s=60: "12345678-1234-1234-1234-123456789abc")

    def fake_run(cmd, **kwargs):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE ncuc_discovery_records SET fetch_status='success', local_path='doc.pdf' WHERE id=1"
        )
        conn.commit()
        conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="Found 2 documents\n", stderr="")

    monkeypatch.setattr(acquisition.subprocess, "run", fake_run)

    result = acquisition.fetch_record_level_dockets(
        {
            "top_dockets": [
                {"docket_number": "E-2 Sub 1354", "eligible_count": 2},
            ],
        },
        database_path=str(db_path),
        max_dockets=1,
        dry_run=False,
    )

    assert result.total_fetch_eligible_delta == 1
    assert result.results[0].fetch_eligible_delta == 1


def test_get_docket_recommendations_filters_cooling_dockets_without_starving(tmp_path) -> None:
    db_path = tmp_path / "recommendations.db"
    conn = connect(db_path)
    conn.executemany(
        """
        INSERT INTO ncuc_discovery_records
            (id, docket_number, utility, filing_title, filing_date, fetch_status, local_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "E-7 Sub 145", "DEC", "top pending 1", "2026-01-01", "pending", None, "2026-01-01T00:00:00Z"),
            (2, "E-7 Sub 145", "DEC", "top pending 2", "2026-01-02", "pending", None, "2026-01-01T00:00:00Z"),
            (3, "E-2 Sub 1354", "DEP", "next pending", "2026-01-03", "pending", None, "2026-01-01T00:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()

    recs = acquisition._get_docket_recommendations(
        str(db_path),
        limit=1,
        excluded_dockets={"E-7 Sub 145"},
    )

    assert [r["docket_number"] for r in recs] == ["E-2 Sub 1354"]


def test_bootstrap_created_versions_requires_positive_effective_count() -> None:
    assert not acquisition._bootstrap_created_versions([
        SimpleNamespace(
            success=True,
            return_code=0,
            cli_command="bootstrap-missing-versions-nc",
            effective_count=0,
        )
    ])
    assert acquisition._bootstrap_created_versions([
        SimpleNamespace(
            success=True,
            return_code=0,
            cli_command="bootstrap-missing-versions-nc",
            effective_count=3,
        )
    ])


def test_no_work_stop_does_not_fire_when_measurement_improved() -> None:
    assert not acquisition._is_no_work_stop(
        work_attempted=False,
        improvement_observed=True,
    )
    assert acquisition._is_no_work_stop(
        work_attempted=False,
        improvement_observed=False,
    )


def test_parse_effective_count_reads_evidence_backfill_summary() -> None:
    assert _parse_effective_count("  Would backfill:         6\n") == 6


def test_parse_effective_count_reads_lineage_gap_repair_summary() -> None:
    assert _parse_effective_count("  Repaired lineage gaps: moved=22\n") == 22


def test_parse_effective_count_reads_ncuc_import_summary() -> None:
    assert _parse_effective_count("Imported 13/15 downloaded NCUC records.\n") == 13


def test_decide_actions_routes_docket_coverage_on_importable_subcount() -> None:
    report = {
        "summary_counts": {"docket_coverage": 26},
        "docket_coverage": {
            "summary": {
                "total_count": 26,
                "fetch_eligible_records": 79,
                "downloaded_not_imported_records": 0,
            },
            "rows": [],
        },
    }

    assert decide_actions(report, include_categories={"docket_coverage"}) == []

    report["docket_coverage"]["summary"]["downloaded_not_imported_records"] = 3
    actions = decide_actions(report, include_categories={"docket_coverage"})

    assert len(actions) == 1
    assert actions[0].finding_count == 3
    assert actions[0].action.cli_command == "ncuc import-pipeline"


def test_run_global_post_steps_skips_extract_when_import_reports_zero(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        cmd_args = list(cmd[3:])
        calls.append(cmd_args)
        stdout = ""
        if cmd_args == ["ncuc", "import-pipeline", "--all-downloaded"]:
            stdout = "Imported 0/0 downloaded NCUC records.\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(acquisition.subprocess, "run", fake_run)

    result = acquisition._run_global_post_steps(
        database_path=":memory:",
        timeout_per_stage_s=10,
    )

    assert result.docs_imported == 0
    assert ["extract-rates-nc", "--limit", "200", "--progress"] not in calls
    assert [entry["stage"] for entry in result.stage_outcomes] == [
        "import",
        "bootstrap",
        "extract",
    ]
    assert result.stage_outcomes[0]["docs_imported"] == 0
    assert result.stage_outcomes[-1]["skipped"] is True
    assert result.stage_outcomes[-1]["reason"] == "no_imported_documents"


def test_run_global_post_steps_runs_extract_when_import_reports_work(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        cmd_args = list(cmd[3:])
        calls.append(cmd_args)
        stdout = ""
        if cmd_args == ["ncuc", "import-pipeline", "--all-downloaded"]:
            stdout = "Imported 2/2 downloaded NCUC records.\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(acquisition.subprocess, "run", fake_run)

    result = acquisition._run_global_post_steps(
        database_path=":memory:",
        timeout_per_stage_s=10,
    )

    assert result.docs_imported == 2
    assert ["extract-rates-nc", "--limit", "200", "--progress"] in calls
    assert [entry["stage"] for entry in result.stage_outcomes] == [
        "import",
        "bootstrap",
        "extract",
    ]
