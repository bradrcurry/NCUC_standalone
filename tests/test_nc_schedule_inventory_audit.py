from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from duke_rates.analytics.nc_schedule_inventory_audit import (
    build_nc_schedule_inventory_audit,
    export_nc_schedule_inventory_audit,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.tariff import TariffChargeRecord, TariffFamilyRecord, TariffVersionRecord


def _make_repo(tmp_path: Path) -> tuple[Path, Repository]:
    db_path = tmp_path / "schedule-inventory.db"
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


def _seed_version(repo: Repository, family_key: str, start: str, source_type: str) -> int:
    repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key=family_key,
            effective_start=start,
            effective_end=None,
            source_type=source_type,
            confidence_score=0.9,
        )
    )
    return repo.list_tariff_versions(family_key)[-1].id


def _seed_charge(repo: Repository, version_id: int, family_key: str) -> None:
    repo.upsert_tariff_charge(
        TariffChargeRecord(
            version_id=version_id,
            family_key=family_key,
            charge_type="energy_block",
            rate_value=0.12,
            rate_unit="$/kWh",
            confidence_score=0.9,
        )
    )


def test_schedule_inventory_audit_flags_populated_family_missing_from_matrix(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    family_key = "nc-progress-leaf-504"
    _seed_family(repo, family_key, "R-TOU-EV", "progress")
    version_id = _seed_version(repo, family_key, "2025-01-01", "regulator")
    _seed_charge(repo, version_id, family_key)

    report = build_nc_schedule_inventory_audit(db_path)
    row = next(item for item in report["rows"] if item["family_key"] == family_key)

    assert row["matrix_scope_status"] == "missing_from_matrix"
    assert row["billing_class"] == "core_billing_schedule"
    assert row["tracking_status"] == "missing_from_matrix_but_db_populated"
    assert row["recommended_action"] == "expand_coverage_scope"


def test_schedule_inventory_audit_flags_legacy_doc_family(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    family_key = "nc-carolinas-doc-SCHEDULEOPTE"
    _seed_family(repo, family_key, "Schedule OPT-E", "carolinas")
    version_id = _seed_version(repo, family_key, "2015-01-01", "historical")
    _seed_charge(repo, version_id, family_key)

    report = build_nc_schedule_inventory_audit(db_path)
    row = next(item for item in report["rows"] if item["family_key"] == family_key)

    assert row["billing_class"] == "legacy_or_malformed_family"
    assert row["tracking_status"] == "legacy_duplicate_or_needs_reclassification"
    assert row["recommended_action"] == "review_family_classification"


def test_export_nc_schedule_inventory_audit_writes_markdown_and_json(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    family_key = "nc-progress-leaf-504"
    _seed_family(repo, family_key, "R-TOU-EV", "progress")
    version_id = _seed_version(repo, family_key, "2025-01-01", "regulator")
    _seed_charge(repo, version_id, family_key)

    output_paths = export_nc_schedule_inventory_audit(tmp_path / "out", database_path=db_path)

    assert output_paths["markdown"].exists()
    assert output_paths["summary_json"].exists()
    payload = json.loads(output_paths["summary_json"].read_text(encoding="utf-8"))
    assert payload["total_families"] == 1
    assert output_paths["markdown"].read_text(encoding="utf-8").startswith("# NC Schedule Inventory Audit")
