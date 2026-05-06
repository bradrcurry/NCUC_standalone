from __future__ import annotations

import sqlite3
from pathlib import Path

from duke_rates.analytics.dep_residential_rider_applicability import (
    seed_dep_residential_rider_applicability,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.tariff import TariffFamilyRecord


def _make_repo(tmp_path: Path) -> tuple[Path, Repository]:
    db_path = tmp_path / "dep-rider-links.db"
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


def test_seed_dep_residential_rider_applicability_inserts_expected_rows(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    for family_key in (
        "nc-progress-leaf-500",
        "nc-progress-leaf-501",
        "nc-progress-leaf-502",
        "nc-progress-leaf-503",
        "nc-progress-leaf-504",
    ):
        _seed_family(repo, family_key, "rate_schedule", family_key)
    for rider_key in (
        "nc-progress-leaf-601",
        "nc-progress-leaf-602",
        "nc-progress-leaf-604",
        "nc-progress-leaf-605",
        "nc-progress-leaf-608",
        "nc-progress-leaf-609",
        "nc-progress-leaf-610",
        "nc-progress-leaf-611",
    ):
        _seed_family(repo, rider_key, "rider", rider_key)

    report = seed_dep_residential_rider_applicability(db_path)

    assert report["inserted"] == 40
    assert report["skipped"] == 0
    links = repo.list_rider_applicability(applies_to_family_key="nc-progress-leaf-503")
    assert len(links) == 8
    assert {link.rider_family_key for link in links} == {
        "nc-progress-leaf-601",
        "nc-progress-leaf-602",
        "nc-progress-leaf-604",
        "nc-progress-leaf-605",
        "nc-progress-leaf-608",
        "nc-progress-leaf-609",
        "nc-progress-leaf-610",
        "nc-progress-leaf-611",
    }
    assert all(link.mandatory for link in links)
    assert all(link.in_rider_summary for link in links)


def test_seed_dep_residential_rider_applicability_is_idempotent(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    for family_key in (
        "nc-progress-leaf-500",
        "nc-progress-leaf-501",
        "nc-progress-leaf-502",
        "nc-progress-leaf-503",
        "nc-progress-leaf-504",
    ):
        _seed_family(repo, family_key, "rate_schedule", family_key)
    for rider_key in (
        "nc-progress-leaf-601",
        "nc-progress-leaf-602",
        "nc-progress-leaf-604",
        "nc-progress-leaf-605",
        "nc-progress-leaf-608",
        "nc-progress-leaf-609",
        "nc-progress-leaf-610",
        "nc-progress-leaf-611",
    ):
        _seed_family(repo, rider_key, "rider", rider_key)

    first = seed_dep_residential_rider_applicability(db_path)
    second = seed_dep_residential_rider_applicability(db_path)

    assert first["inserted"] == 40
    assert second["inserted"] == 0
    assert second["skipped"] == 40
