from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from duke_rates.analytics.nc_coverage_assessment import (
    build_nc_coverage_assessment,
    export_nc_coverage_assessment,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.tariff import TariffChargeRecord, TariffFamilyRecord, TariffVersionRecord


def _make_repo(tmp_path: Path) -> tuple[Path, Repository]:
    db_path = tmp_path / "coverage.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()
    return db_path, Repository(str(db_path))


def _seed_family(repo: Repository, family_key: str, title: str, company: str) -> None:
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key=family_key,
            state="NC",
            company=company,
            family_type="rate_schedule",
            title=title,
        )
    )


def _seed_version(repo: Repository, family_key: str, start: str, end: str | None, source_type: str) -> int:
    repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key=family_key,
            effective_start=start,
            effective_end=end,
            source_type=source_type,
            confidence_score=0.9,
        )
    )
    return repo.list_tariff_versions(family_key)[-1].id


def _seed_charges(repo: Repository, version_id: int, family_key: str, count: int) -> None:
    for idx in range(count):
        repo.upsert_tariff_charge(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="energy_block",
                rate_value=0.1 + idx,
                rate_unit="$/kWh",
                confidence_score=0.9,
            )
        )


def test_build_nc_coverage_assessment_marks_carried_forward_years(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-500", "RES", "progress")
    version_id = _seed_version(repo, "nc-progress-leaf-500", "2015-12-01", None, "historical")
    _seed_charges(repo, version_id, "nc-progress-leaf-500", 4)

    report = build_nc_coverage_assessment(
        db_path,
        dep_years=range(2015, 2018),
        dec_years=range(2013, 2014),
    )

    dep_rows = report["dep_rows"]
    row_2015 = next(row for row in dep_rows if row["schedule_label"] == "RES" and row["target_year"] == 2015)
    row_2016 = next(row for row in dep_rows if row["schedule_label"] == "RES" and row["target_year"] == 2016)
    row_2017 = next(row for row in dep_rows if row["schedule_label"] == "RES" and row["target_year"] == 2017)

    assert row_2015["display"] == "—"
    assert row_2016["display"] == "(=15)"
    assert row_2017["display"] == "(=15)"


def test_build_nc_coverage_assessment_prefers_better_overlapping_version(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-carolinas-schedule-RS", "RS", "carolinas")
    utility_current_id = _seed_version(repo, "nc-carolinas-schedule-RS", "2026-01-01", None, "utility_current")
    regulator_id = _seed_version(repo, "nc-carolinas-schedule-RS", "2026-01-01", None, "regulator")
    _seed_charges(repo, utility_current_id, "nc-carolinas-schedule-RS", 2)
    _seed_charges(repo, regulator_id, "nc-carolinas-schedule-RS", 3)

    report = build_nc_coverage_assessment(
        db_path,
        dep_years=range(2015, 2016),
        dec_years=range(2026, 2027),
    )
    dec_rows = report["dec_rows"]
    row = next(row for row in dec_rows if row["schedule_label"] == "RS" and row["target_year"] == 2026)

    assert row["selected_version_id"] == regulator_id
    assert row["quality_symbol"] == "P"
    assert row["display"] == "P"


def test_export_nc_coverage_assessment_writes_markdown_and_json(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-500", "RES", "progress")
    version_id = _seed_version(repo, "nc-progress-leaf-500", "2025-10-01", None, "regulator")
    _seed_charges(repo, version_id, "nc-progress-leaf-500", 5)

    output_paths = export_nc_coverage_assessment(tmp_path / "out", database_path=db_path)

    assert output_paths["markdown"].exists()
    assert output_paths["summary_json"].exists()
    payload = json.loads(output_paths["summary_json"].read_text(encoding="utf-8"))
    assert "dep_rows" in payload
    assert output_paths["markdown"].read_text(encoding="utf-8").startswith("# NC Coverage Assessment")


def test_build_nc_coverage_assessment_reports_populated_core_families_missing_from_matrix(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-500", "RES", "progress")
    in_scope_id = _seed_version(repo, "nc-progress-leaf-500", "2025-10-01", None, "regulator")
    _seed_charges(repo, in_scope_id, "nc-progress-leaf-500", 5)

    _seed_family(repo, "nc-progress-leaf-504", "R-TOU-EV", "progress")
    omitted_id = _seed_version(repo, "nc-progress-leaf-504", "2025-10-01", None, "regulator")
    _seed_charges(repo, omitted_id, "nc-progress-leaf-504", 3)

    report = build_nc_coverage_assessment(
        db_path,
        dep_years=range(2025, 2026),
        dec_years=range(2025, 2026),
    )

    inventory_scope = report["inventory_scope"]
    omitted = inventory_scope["core_billing_missing_from_matrix"]

    assert inventory_scope["core_billing_missing_from_matrix_count"] == 1
    assert any(row["family_key"] == "nc-progress-leaf-504" for row in omitted)


def test_export_nc_coverage_assessment_markdown_includes_inventory_exceptions(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-500", "RES", "progress")
    in_scope_id = _seed_version(repo, "nc-progress-leaf-500", "2025-10-01", None, "regulator")
    _seed_charges(repo, in_scope_id, "nc-progress-leaf-500", 5)

    _seed_family(repo, "nc-progress-leaf-504", "R-TOU-EV", "progress")
    omitted_id = _seed_version(repo, "nc-progress-leaf-504", "2025-10-01", None, "regulator")
    _seed_charges(repo, omitted_id, "nc-progress-leaf-504", 2)

    output_paths = export_nc_coverage_assessment(tmp_path / "out", database_path=db_path)
    markdown = output_paths["markdown"].read_text(encoding="utf-8")

    assert "Inventory Exceptions" in markdown
    assert "nc-progress-leaf-504" in markdown


def test_build_nc_coverage_assessment_includes_r_tou_cpp_in_dep_matrix(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-503", "R-TOU-CPP", "progress")
    version_id = _seed_version(repo, "nc-progress-leaf-503", "2025-01-01", None, "regulator")
    _seed_charges(repo, version_id, "nc-progress-leaf-503", 5)

    report = build_nc_coverage_assessment(
        db_path,
        dep_years=range(2025, 2026),
        dec_years=range(2025, 2026),
    )

    row = next(row for row in report["dep_rows"] if row["family_key"] == "nc-progress-leaf-503")
    matrix_row = next(row for row in report["dep_matrix"] if row["family_key"] == "nc-progress-leaf-503")

    assert row["schedule_label"] == "R-TOU-CPP"
    assert row["quality_symbol"] == "F"
    assert matrix_row["2025"] == "F"
