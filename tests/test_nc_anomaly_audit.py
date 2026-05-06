from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from duke_rates.analytics.nc_anomaly_audit import (
    build_nc_anomaly_audit,
    export_nc_anomaly_audit,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.tariff import TariffChargeRecord, TariffFamilyRecord, TariffVersionRecord


def _make_repo(tmp_path: Path) -> tuple[Path, Repository]:
    db_path = tmp_path / "anomaly-audit.db"
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


def _seed_version(
    repo: Repository,
    family_key: str,
    start: str,
    end: str | None,
    source_type: str,
    *,
    historical_document_id: int | None = None,
    source_pdf: str | None = None,
) -> int:
    repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key=family_key,
            historical_document_id=historical_document_id,
            effective_start=start,
            effective_end=end,
            source_type=source_type,
            source_pdf=source_pdf,
            confidence_score=0.9,
        )
    )
    return repo.list_tariff_versions(family_key)[-1].id


def _seed_charge(
    repo: Repository,
    *,
    version_id: int,
    family_key: str,
    charge_type: str,
    rate_value: float | None = 0.1,
    rate_unit: str = "$/kWh",
    tou_period: str | None = None,
) -> None:
    repo.upsert_tariff_charge(
        TariffChargeRecord(
            version_id=version_id,
            family_key=family_key,
            charge_type=charge_type,
            rate_value=rate_value,
            rate_unit=rate_unit,
            tou_period=tou_period,
            confidence_score=0.9,
        )
    )


def _seed_historical_document(repo: Repository, family_key: str, local_name: str) -> int:
    return repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key=family_key,
            title=f"{family_key} historical",
            state="NC",
            company="progress",
            category="tariff",
            kind="schedule",
            canonical_url=f"https://example.com/{local_name}",
            archived_url=f"https://archive.example.com/{local_name}",
            snapshot_timestamp=datetime(2026, 4, 7, 0, 0, 0),
            local_path=Path(f"cache/{local_name}.pdf"),
            content_hash=f"hash-{local_name}",
            retrieved_at=datetime(2026, 4, 7, 0, 0, 0),
        )
    )


def test_build_nc_anomaly_audit_flags_zero_charge_and_duplicate_same_start(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    family_key = "nc-carolinas-schedule-RS"
    _seed_family(repo, family_key, "RS", "carolinas")
    doc_id = _seed_historical_document(repo, family_key, "rs-2025")
    zero_id = _seed_version(
        repo,
        family_key,
        "2025-01-01",
        None,
        "regulator",
        historical_document_id=doc_id,
        source_pdf="cache/rs-2025.pdf",
    )
    dup_id = _seed_version(repo, family_key, "2025-01-01", None, "utility_current")
    for _ in range(6):
        _seed_charge(repo, version_id=dup_id, family_key=family_key, charge_type="energy_block")

    report = build_nc_anomaly_audit(db_path)
    rows = report["rows"]

    zero_rows = [row for row in rows if row["version_id"] == zero_id]
    assert any(row["anomaly_type"] == "zero_charge_version" for row in zero_rows)
    assert any(row["anomaly_type"] == "duplicate_same_start_versions" for row in zero_rows)
    assert any(row["recommended_action"] == "reparse_with_updated_profile" for row in zero_rows)


def test_build_nc_anomaly_audit_flags_tou_and_sparse_shape(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    family_key = "nc-progress-leaf-501"
    _seed_family(repo, family_key, "R-TOUD", "progress")
    strong_id = _seed_version(repo, family_key, "2024-01-01", "2024-12-31", "regulator")
    sparse_id = _seed_version(repo, family_key, "2025-01-01", None, "regulator")

    for idx in range(10):
        _seed_charge(
            repo,
            version_id=strong_id,
            family_key=family_key,
            charge_type="tou_energy",
            rate_value=0.1 + idx,
            rate_unit="$/kWh",
            tou_period=f"period_{idx}",
        )
    _seed_charge(repo, version_id=strong_id, family_key=family_key, charge_type="demand", rate_unit="$/kW")

    _seed_charge(repo, version_id=sparse_id, family_key=family_key, charge_type="tou_energy", tou_period=None)
    _seed_charge(repo, version_id=sparse_id, family_key=family_key, charge_type="fixed", rate_unit="$/month")

    report = build_nc_anomaly_audit(db_path)
    sparse_rows = [row for row in report["rows"] if row["version_id"] == sparse_id]

    assert any(row["anomaly_type"] == "sparse_vs_family_peak" for row in sparse_rows)
    assert any(row["anomaly_type"] == "missing_tou_structure" for row in sparse_rows)
    assert any(row["anomaly_type"] == "missing_demand_rows" for row in sparse_rows)


def test_export_nc_anomaly_audit_writes_markdown_and_json(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    family_key = "nc-progress-leaf-500"
    _seed_family(repo, family_key, "RES", "progress")
    doc_id = _seed_historical_document(repo, family_key, "res-2025")
    _seed_version(
        repo,
        family_key,
        "2025-10-01",
        None,
        "regulator",
        historical_document_id=doc_id,
        source_pdf="cache/res-2025.pdf",
    )

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
        ) VALUES (?, ?, ?, 'extract', 'manual', 'completed', 'empty', 0, '[]', '{}', ?, ?)
        """,
        (doc_id, "cache/res-2025.pdf", family_key, "2026-04-07T00:00:00", "2026-04-07T00:05:00"),
    )
    conn.commit()
    conn.close()

    output_paths = export_nc_anomaly_audit(tmp_path / "out", database_path=db_path)

    assert output_paths["markdown"].exists()
    assert output_paths["summary_json"].exists()
    payload = json.loads(output_paths["summary_json"].read_text(encoding="utf-8"))
    assert payload["total_versions_scanned"] == 1
    assert payload["total_anomalies"] >= 1
    assert output_paths["markdown"].read_text(encoding="utf-8").startswith("# NC Anomaly Audit")
