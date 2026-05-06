from __future__ import annotations

import json
import json
import sqlite3
from pathlib import Path

from duke_rates.analytics.dep_storm_rider_audit import (
    build_dep_storm_rider_audit,
    export_dep_storm_rider_audit,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.tariff import RiderApplicabilityRecord, TariffChargeRecord, TariffFamilyRecord, TariffVersionRecord


def _make_repo(tmp_path: Path) -> tuple[Path, Repository]:
    db_path = tmp_path / "dep-storm-rider-audit.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()
    return db_path, Repository(str(db_path))


def _seed_family(repo: Repository, family_key: str, title: str) -> None:
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key=family_key,
            state="NC",
            company="progress",
            family_type="rider",
            title=title,
        )
    )


def _seed_version(
    repo: Repository,
    *,
    family_key: str,
    start: str | None,
    historical_document_id: int | None = None,
) -> int:
    repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key=family_key,
            historical_document_id=historical_document_id,
            effective_start=start,
            effective_end=None,
            source_type="regulator",
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
            charge_label="Rider Adjustment - Residential",
            rate_value=0.01,
            rate_unit="$/kWh",
            confidence_score=0.9,
        )
    )


def _seed_doc(
    repo: Repository,
    *,
    family_key: str,
    title: str,
    path: Path,
    start_page: int | None,
    end_page: int | None,
) -> int:
    path.write_text("placeholder", encoding="utf-8")
    doc_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=None,
            family_key=family_key,
            title=title,
            state="NC",
            company="progress",
            category="rider",
            kind="pdf",
            canonical_url=f"https://example.test/{path.name}",
            archived_url=f"https://archive.test/{path.name}",
            snapshot_timestamp="2026-04-07T00:00:00Z",
            local_path=str(path),
            content_hash=f"hash-{path.name}",
            direct_downloadable=True,
            effective_start="2025-01-01",
            retrieved_at="2026-04-07T00:00:00Z",
            metadata_json=json.dumps({"start_page": start_page, "end_page": end_page}),
        )
    )
    return int(doc_id)


def test_dep_storm_rider_audit_classifies_canonical_and_legacy_rows(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    for family_key, title in (
        ("nc-progress-leaf-607", "Storm Securitization Rider STS"),
        ("nc-progress-leaf-613", "Storm Securitization Rider"),
        ("nc-progress-doc-STORMRECOVERYRIDER", "Storm Recovery Rider"),
    ):
        _seed_family(repo, family_key, title)
    for base_family in (
        "nc-progress-leaf-500",
        "nc-progress-leaf-501",
        "nc-progress-leaf-502",
        "nc-progress-leaf-503",
        "nc-progress-leaf-504",
    ):
        repo.upsert_tariff_family(
            TariffFamilyRecord(
                family_key=base_family,
                state="NC",
                company="progress",
                family_type="rate_schedule",
                title=base_family,
            )
        )

    doc_607 = _seed_doc(
        repo,
        family_key="nc-progress-leaf-607",
        title="Storm Securitization Rider STS",
        path=tmp_path / "leaf607.pdf",
        start_page=2,
        end_page=3,
    )
    version_607 = _seed_version(
        repo,
        family_key="nc-progress-leaf-607",
        start="2026-01-01",
        historical_document_id=doc_607,
    )
    _seed_charge(repo, version_607, "nc-progress-leaf-607")
    repo.upsert_rider_applicability(
        RiderApplicabilityRecord(
            rider_family_key="nc-progress-leaf-607",
            applies_to_family_key="nc-progress-leaf-500",
            mandatory=True,
            in_rider_summary=True,
            source_type="manual",
            confidence_score=0.9,
        )
    )

    doc_613 = _seed_doc(
        repo,
        family_key="nc-progress-leaf-613",
        title="Storm Securitization Rider",
        path=tmp_path / "leaf613.pdf",
        start_page=1,
        end_page=1,
    )
    version_613 = _seed_version(
        repo,
        family_key="nc-progress-leaf-613",
        start="2025-11-01",
        historical_document_id=doc_613,
    )
    _seed_charge(repo, version_613, "nc-progress-leaf-613")

    doc_legacy = _seed_doc(
        repo,
        family_key="nc-progress-doc-STORMRECOVERYRIDER",
        title="Storm Recovery Rider",
        path=tmp_path / "legacy.pdf",
        start_page=None,
        end_page=None,
    )
    _seed_version(
        repo,
        family_key="nc-progress-doc-STORMRECOVERYRIDER",
        start=None,
        historical_document_id=doc_legacy,
    )

    report = build_dep_storm_rider_audit(db_path)
    rows = {row["family_key"]: row for row in report["rows"]}

    assert rows["nc-progress-leaf-607"]["audit_status"] == "charged_but_unbounded_history"
    assert rows["nc-progress-leaf-613"]["audit_status"] == "charged_but_unlinked_to_residential_schedules"
    assert rows["nc-progress-doc-STORMRECOVERYRIDER"]["audit_status"] == "legacy_duplicate_family"


def test_export_dep_storm_rider_audit_writes_markdown_and_json(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-607", "Storm Securitization Rider STS")
    doc_607 = _seed_doc(
        repo,
        family_key="nc-progress-leaf-607",
        title="Storm Securitization Rider STS",
        path=tmp_path / "leaf607.pdf",
        start_page=2,
        end_page=3,
    )
    version_607 = _seed_version(
        repo,
        family_key="nc-progress-leaf-607",
        start="2026-01-01",
        historical_document_id=doc_607,
    )
    _seed_charge(repo, version_607, "nc-progress-leaf-607")

    output_paths = export_dep_storm_rider_audit(tmp_path / "out", database_path=db_path)

    assert output_paths["markdown"].exists()
    assert output_paths["summary_json"].exists()
    payload = json.loads(output_paths["summary_json"].read_text(encoding="utf-8"))
    assert payload["family_count"] >= 1
    assert output_paths["markdown"].read_text(encoding="utf-8").startswith("# DEP Storm Rider Audit")
