from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from duke_rates.analytics.dep_residential_rider_repair_plan import (
    build_dep_residential_rider_repair_plan,
    export_dep_residential_rider_repair_plan,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.tariff import (
    RiderApplicabilityRecord,
    TariffChargeRecord,
    TariffFamilyRecord,
    TariffVersionRecord,
)


def _make_repo(tmp_path: Path) -> tuple[Path, Repository]:
    db_path = tmp_path / "dep-rider-repair-plan.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()
    return db_path, Repository(str(db_path))


def _seed_family(repo: Repository, family_key: str, family_type: str, title: str) -> None:
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key=family_key,
            state="NC",
            company="progress",
            family_type=family_type,
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
            charge_type="adjustment",
            rate_value=0.01,
            rate_unit="$/kWh",
            confidence_score=0.9,
        )
    )


def test_dep_residential_rider_repair_plan_builds_parser_guidance(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-503", "rate_schedule", "R-TOU-CPP")
    base_version = _seed_version(repo, "nc-progress-leaf-503", "2022-12-01", "regulator")
    _seed_charge(repo, base_version, "nc-progress-leaf-503")
    _seed_family(repo, "nc-progress-leaf-611", "rider", "CAR")
    rider_version = _seed_version(repo, "nc-progress-leaf-611", "2020-01-01", "regulator")
    repo.upsert_rider_applicability(
        RiderApplicabilityRecord(
            rider_family_key="nc-progress-leaf-611",
            applies_to_family_key="nc-progress-leaf-503",
            mandatory=True,
            in_rider_summary=True,
            source_type="manual",
            confidence_score=0.9,
        )
    )

    report = build_dep_residential_rider_repair_plan(db_path)

    row = next(item for item in report["rows"] if item["rider_family_key"] == "nc-progress-leaf-611")
    assert row["parser_profile"] == "progress_customer_assistance_recovery"
    assert row["recommended_action"] == "reparse_existing_rider_versions"
    assert row["zero_charge_version_count"] == 1
    assert "enqueue-profile-impact-nc" in row["command_hint"]


def test_export_dep_residential_rider_repair_plan_writes_markdown_and_json(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-503", "rate_schedule", "R-TOU-CPP")
    base_version = _seed_version(repo, "nc-progress-leaf-503", "2022-12-01", "regulator")
    _seed_charge(repo, base_version, "nc-progress-leaf-503")
    _seed_family(repo, "nc-progress-leaf-608", "rider", "RDM")
    repo.upsert_rider_applicability(
        RiderApplicabilityRecord(
            rider_family_key="nc-progress-leaf-608",
            applies_to_family_key="nc-progress-leaf-503",
            mandatory=True,
            in_rider_summary=True,
            source_type="manual",
            confidence_score=0.9,
        )
    )

    output_paths = export_dep_residential_rider_repair_plan(tmp_path / "out", database_path=db_path)

    assert output_paths["markdown"].exists()
    assert output_paths["summary_json"].exists()
    payload = json.loads(output_paths["summary_json"].read_text(encoding="utf-8"))
    assert payload["rider_family_count"] >= 1
    assert output_paths["markdown"].read_text(encoding="utf-8").startswith(
        "# DEP Residential Rider Repair Plan"
    )


def test_dep_residential_rider_repair_plan_omits_pre_intro_only_riders(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-503", "rate_schedule", "R-TOU-CPP")
    base_version = _seed_version(repo, "nc-progress-leaf-503", "2022-12-01", "regulator")
    _seed_charge(repo, base_version, "nc-progress-leaf-503")
    _seed_family(repo, "nc-progress-leaf-611", "rider", "CAR")
    rider_version = _seed_version(repo, "nc-progress-leaf-611", "2025-01-01", "regulator")
    _seed_charge(repo, rider_version, "nc-progress-leaf-611")
    repo.upsert_rider_applicability(
        RiderApplicabilityRecord(
            rider_family_key="nc-progress-leaf-611",
            applies_to_family_key="nc-progress-leaf-503",
            mandatory=True,
            in_rider_summary=True,
            source_type="manual",
            confidence_score=0.9,
        )
    )

    report = build_dep_residential_rider_repair_plan(db_path)

    assert report["rider_family_count"] == 0
