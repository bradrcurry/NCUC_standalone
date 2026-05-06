from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from duke_rates.analytics import nc_missing_clean_doc_audit
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.tariff import TariffChargeRecord, TariffFamilyRecord, TariffVersionRecord


def _make_repo(tmp_path: Path) -> tuple[Path, Repository]:
    db_path = tmp_path / "missing-clean-doc.db"
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
            snapshot_timestamp=datetime(2026, 4, 20, 0, 0, 0),
            local_path=Path(f"cache/{local_name}.pdf"),
            content_hash=f"hash-{local_name}",
            effective_start="2020-01-01",
            retrieved_at=datetime(2026, 4, 20, 0, 0, 0),
        )
    )


def _seed_version(
    repo: Repository,
    family_key: str,
    effective_start: str,
    revision_label: str,
    supersedes_label: str | None,
    *,
    docket_number: str,
    historical_document_id: int | None = None,
) -> int:
    repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key=family_key,
            historical_document_id=historical_document_id,
            effective_start=effective_start,
            revision_label=revision_label,
            supersedes_label=supersedes_label,
            docket_number=docket_number,
            source_type="regulator",
            confidence_score=0.9,
            source_pdf=f"cache/{family_key}-{effective_start}.pdf",
        )
    )
    return repo.list_tariff_versions(family_key)[-1].id


def _seed_charge(repo: Repository, version_id: int, family_key: str) -> None:
    repo.upsert_tariff_charge(
        TariffChargeRecord(
            version_id=version_id,
            family_key=family_key,
            charge_type="energy_block",
            rate_value=0.1,
            rate_unit="$/kWh",
            confidence_score=0.9,
        )
    )


def test_build_nc_missing_clean_doc_audit_surfaces_chain_and_redline_clues(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path, repo = _make_repo(tmp_path)
    family_key = "nc-progress-leaf-500"
    _seed_family(repo, family_key, "Residential Service Schedule RES", "progress")
    doc_id = _seed_historical_document(repo, family_key, "res-gap")

    v1 = _seed_version(
        repo,
        family_key,
        "2020-01-01",
        "NC First Revised Leaf No. 500",
        "NC Original Leaf No. 500",
        docket_number="E-2, Sub 1000",
        historical_document_id=doc_id,
    )
    _seed_charge(repo, v1, family_key)

    v2 = _seed_version(
        repo,
        family_key,
        "2022-01-01",
        "NC Fourth Revised Leaf No. 500",
        "NC Third Revised Leaf No. 500",
        docket_number="E-2, Sub 1100",
        historical_document_id=doc_id,
    )
    _seed_charge(repo, v2, family_key)

    monkeypatch.setattr(
        nc_missing_clean_doc_audit,
        "build_nc_redline_lead_audit",
        lambda database_path=None: {
            "rows": [
                {
                    "family_key": family_key,
                    "docket_numbers": json.dumps(["E-2, Sub 1098"]),
                    "top_actionable_clues": json.dumps(["Supersedes Schedule RES-3"]),
                    "search_hint": "Search E-2, Sub 1098 around 2021 filings",
                    "unpaired_redline_doc_count": 1,
                    "redline_clue_doc_count": 1,
                    "actionable_clue_count": 1,
                }
            ]
        },
    )

    report = nc_missing_clean_doc_audit.build_nc_missing_clean_doc_audit(db_path)
    assert report["total_rows"] >= 1

    row = next(item for item in report["rows"] if item["family_key"] == family_key)
    assert row["missing_kind"] == "missing_clean_companion"
    assert row["missing_supersedes_count"] == 2
    assert row["missing_ordinal_count"] == 2
    assert row["largest_effective_gap_days"] >= 700
    assert "E-2, Sub 1098" in json.loads(row["suggested_dockets"])
    assert "Leaf No. 500" in json.loads(row["suggested_query_terms"])
    assert row["suggested_date_after"] == "2020-01-01"
    assert row["suggested_date_before"] == "2022-01-01"


def test_export_nc_missing_clean_doc_audit_writes_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path, repo = _make_repo(tmp_path)
    family_key = "nc-progress-leaf-601"
    _seed_family(repo, family_key, "Annual Billing Adjustments Rider BA", "progress")
    v1 = _seed_version(
        repo,
        family_key,
        "2024-01-01",
        "NC Sixth Revised Leaf No. 601",
        "NC Fifth Revised Leaf No. 601",
        docket_number="E-2, Sub 1206",
    )
    _seed_charge(repo, v1, family_key)

    monkeypatch.setattr(
        nc_missing_clean_doc_audit,
        "build_nc_redline_lead_audit",
        lambda database_path=None: {"rows": []},
    )

    paths = nc_missing_clean_doc_audit.export_nc_missing_clean_doc_audit(
        tmp_path / "out",
        database_path=db_path,
    )

    assert paths["markdown"].exists()
    assert paths["summary_json"].exists()
    payload = json.loads(paths["summary_json"].read_text(encoding="utf-8"))
    assert payload["total_rows"] == 1
    assert paths["markdown"].read_text(encoding="utf-8").startswith("# NC Missing Clean Document Audit")
