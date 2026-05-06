from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from duke_rates.analytics.dep_compliance_bundle_audit import (
    build_dep_compliance_bundle_audit,
    export_dep_compliance_bundle_audit,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.tariff import TariffChargeRecord, TariffFamilyRecord, TariffVersionRecord


def _make_repo(tmp_path: Path) -> tuple[Path, Repository]:
    db_path = tmp_path / "dep-compliance-bundle-audit.db"
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


def _seed_historical_document(
    repo: Repository,
    family_key: str,
    local_name: str,
    *,
    bounded: bool,
) -> int:
    document_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key=family_key,
            title=f"{family_key} historical",
            state="NC",
            company="progress",
            category="tariff",
            kind="rider",
            canonical_url=f"https://example.com/{local_name}",
            archived_url=f"https://archive.example.com/{local_name}",
            snapshot_timestamp=datetime(2026, 4, 7, 0, 0, 0),
            local_path=Path(f"cache/{local_name}.pdf"),
            content_hash=f"hash-{local_name}",
            retrieved_at=datetime(2026, 4, 7, 0, 0, 0),
            start_page=2 if bounded else None,
            end_page=4 if bounded else None,
        )
    )
    return document_id


def _seed_version(
    repo: Repository,
    family_key: str,
    historical_document_id: int | None,
    *,
    source_pdf: str,
) -> int:
    repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key=family_key,
            historical_document_id=historical_document_id,
            effective_start="2025-01-01",
            effective_end=None,
            source_type="regulator",
            source_pdf=source_pdf,
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


def _seed_discovery_record(
    db_path: Path,
    family_key: str,
    *,
    fetch_status: str = "success",
    local_path: str | None = "cache/file.pdf",
    content_hash: str | None = "hash-file",
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO ncuc_discovery_records (
            docket_number,
            utility,
            filing_title,
            filing_classification,
            family_keys_json,
            fetch_status,
            local_path,
            content_hash,
            created_at
        ) VALUES ('E-2', 'Duke Energy Progress', 'Compliance filing', 'tariff_sheets', ?, ?, ?, ?, '2026-04-07T00:00:00')
        """,
        (json.dumps([family_key]), fetch_status, local_path, content_hash),
    )
    conn.commit()
    conn.close()


def _seed_processing_run(
    db_path: Path,
    historical_document_id: int,
    family_key: str,
    *,
    outcome_quality: str,
    charge_count: int,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO historical_processing_runs (
            historical_document_id,
            source_pdf,
            family_key,
            parser_stage,
            processing_mode,
            status,
            outcome_quality,
            charge_count,
            review_flags_json,
            metadata_json,
            started_at,
            completed_at
        ) VALUES (?, ?, ?, 'extract', 'manual', 'completed', ?, ?, '[]', '{}', ?, ?)
        """,
        (
            historical_document_id,
            f"cache/{family_key}.pdf",
            family_key,
            outcome_quality,
            charge_count,
            "2026-04-07T00:00:00",
            "2026-04-07T00:05:00",
        ),
    )
    conn.commit()
    conn.close()


def test_build_dep_compliance_bundle_audit_flags_missing_discovery(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-604", "EDIT-4")

    report = build_dep_compliance_bundle_audit(db_path)
    row = next(item for item in report["rows"] if item["family_key"] == "nc-progress-leaf-604")

    assert row["audit_status"] == "missing_from_discovery"
    assert row["recommended_action"] == "authenticated_dragnet_search"


def test_build_dep_compliance_bundle_audit_flags_downloaded_not_imported(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-605", "CPRE")
    _seed_discovery_record(db_path, "nc-progress-leaf-605")

    report = build_dep_compliance_bundle_audit(db_path)
    row = next(item for item in report["rows"] if item["family_key"] == "nc-progress-leaf-605")

    assert row["audit_status"] == "downloaded_not_imported"
    assert row["recommended_action"] == "import_discovered_bundle"


def test_build_dep_compliance_bundle_audit_flags_partial_bounded_family(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    family_key = "nc-progress-leaf-611"
    _seed_family(repo, family_key, "CAR")
    _seed_discovery_record(db_path, family_key, content_hash="hash-1")
    _seed_discovery_record(db_path, family_key, content_hash="hash-2")
    doc_id = _seed_historical_document(repo, family_key, "car-2025", bounded=True)
    version_id = _seed_version(repo, family_key, doc_id, source_pdf="cache/car-2025.pdf")
    _seed_charge(repo, version_id, family_key)
    _seed_processing_run(db_path, doc_id, family_key, outcome_quality="weak", charge_count=1)

    report = build_dep_compliance_bundle_audit(db_path)
    row = next(item for item in report["rows"] if item["family_key"] == family_key)

    assert row["audit_status"] == "bounded_but_partial"
    assert row["recommended_action"] == "audit_bundle_quality_and_reparse"
    assert row["versions_with_charges"] == 1


def test_export_dep_compliance_bundle_audit_writes_markdown_and_json(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-608", "RDM")

    output_paths = export_dep_compliance_bundle_audit(tmp_path / "out", database_path=db_path)

    assert output_paths["markdown"].exists()
    assert output_paths["summary_json"].exists()
    payload = json.loads(output_paths["summary_json"].read_text(encoding="utf-8"))
    assert payload["family_count"] == 1
    assert output_paths["markdown"].read_text(encoding="utf-8").startswith(
        "# DEP Compliance Bundle Audit"
    )
