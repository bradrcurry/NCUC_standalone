from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import fitz

from duke_rates.analytics.dep_storm_history_inventory import (
    build_dep_storm_history_inventory,
    export_dep_storm_history_inventory,
)
from duke_rates.db.repository import Repository
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.tariff import TariffChargeRecord, TariffFamilyRecord, TariffVersionRecord


def _make_repo(tmp_path: Path) -> tuple[Path, Repository]:
    db_path = tmp_path / "dep-storm-history-inventory.db"
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


def _seed_version(repo: Repository, family_key: str, historical_document_id: int, start: str) -> int:
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
            charge_label="Storm Rider - Residential",
            rate_value=0.01,
            rate_unit="$/kWh",
            confidence_score=0.9,
        )
    )


def _seed_historical_doc(repo: Repository, family_key: str, path: Path, title: str) -> int:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), title)
    doc.save(path)
    doc.close()
    return int(
        repo.upsert_historical_document(
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
                metadata_json=json.dumps({"start_page": 1, "end_page": 1}),
            )
        )
    )


def _write_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _insert_discovery_row(
    db_path: Path,
    *,
    docket_number: str,
    filing_title: str,
    filing_date: str,
    filing_classification: str,
    family_keys_json: str,
    local_path: str,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO ncuc_discovery_records (
            docket_number,
            utility,
            filing_title,
            filing_date,
            filing_classification,
            family_keys_json,
            acquisition_method,
            fetch_status,
            local_path,
            created_at
        ) VALUES (?, 'Duke Energy Progress', ?, ?, ?, ?, 'manual_seed', 'success', ?, '2026-04-07T00:00:00Z')
        """,
        (docket_number, filing_title, filing_date, filing_classification, family_keys_json, local_path),
    )
    conn.commit()
    conn.close()


def test_build_dep_storm_history_inventory_classifies_leaf_and_noise_candidates(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-607", "Storm Securitization Rider STS")
    _seed_family(repo, "nc-progress-leaf-613", "Storm Securitization Rider STS-2")
    _seed_family(repo, "nc-progress-doc-STORMRECOVERYRIDER", "Storm Recovery Rider")

    doc_607 = _seed_historical_doc(
        repo,
        "nc-progress-leaf-607",
        tmp_path / "leaf607_current.pdf",
        "Storm Securitization Rider STS",
    )
    version_607 = _seed_version(repo, "nc-progress-leaf-607", doc_607, "2025-07-01")
    _seed_charge(repo, version_607, "nc-progress-leaf-607")

    candidate_pdf = tmp_path / "historical_leaf607.pdf"
    _write_pdf(candidate_pdf, "Leaf No. 607 Storm Securitization Rider STS effective December 1, 2019")
    _insert_discovery_row(
        db_path,
        docket_number="E-2, Sub 1204",
        filing_title="DEP Compliance Tariffs for annual adjustments",
        filing_date="2019-11-27",
        filing_classification="tariff_sheets",
        family_keys_json='["nc-progress-leaf-607"]',
        local_path=str(candidate_pdf),
    )

    noise_pdf = tmp_path / "storm_order.pdf"
    _write_pdf(noise_pdf, "STATE OF NORTH CAROLINA Order scheduling hearing on fuel and fuel-related costs")
    _insert_discovery_row(
        db_path,
        docket_number="E-2, Sub 1204",
        filing_title="STATE OF NORTH CAROLINA",
        filing_date="2019-08-09",
        filing_classification="order",
        family_keys_json="[]",
        local_path=str(noise_pdf),
    )

    report = build_dep_storm_history_inventory(db_path)
    rows = {row["local_path"]: row for row in report["candidate_rows"]}

    assert rows[str(candidate_pdf)]["candidate_status"] == "historical_leaf_candidate"
    assert rows[str(candidate_pdf)]["candidate_family"] == "nc-progress-leaf-607"
    assert rows[str(noise_pdf)]["candidate_status"] == "procedural_noise"
    assert report["candidate_status_counts"]["historical_leaf_candidate"] >= 1


def test_export_dep_storm_history_inventory_writes_markdown_and_json(tmp_path: Path) -> None:
    db_path, repo = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-607", "Storm Securitization Rider STS")
    _seed_family(repo, "nc-progress-leaf-613", "Storm Securitization Rider STS-2")
    _seed_family(repo, "nc-progress-doc-STORMRECOVERYRIDER", "Storm Recovery Rider")
    doc_607 = _seed_historical_doc(
        repo,
        "nc-progress-leaf-607",
        tmp_path / "leaf607_current.pdf",
        "Storm Securitization Rider STS",
    )
    version_607 = _seed_version(repo, "nc-progress-leaf-607", doc_607, "2025-07-01")
    _seed_charge(repo, version_607, "nc-progress-leaf-607")

    candidate_pdf = tmp_path / "bundle.pdf"
    _write_pdf(candidate_pdf, "Compliance tariff bundle for storm securitization rider")
    _insert_discovery_row(
        db_path,
        docket_number="E-2, Sub 1204",
        filing_title="DEP Compliance Tariffs",
        filing_date="2019-11-27",
        filing_classification="tariff_sheets",
        family_keys_json="[]",
        local_path=str(candidate_pdf),
    )

    output_paths = export_dep_storm_history_inventory(tmp_path / "out", database_path=db_path)

    assert output_paths["markdown"].exists()
    assert output_paths["summary_json"].exists()
    payload = json.loads(output_paths["summary_json"].read_text(encoding="utf-8"))
    assert payload["canonical_family_count"] == 3
    assert output_paths["markdown"].read_text(encoding="utf-8").startswith("# DEP Storm History Inventory")
