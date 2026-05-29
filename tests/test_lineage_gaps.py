from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.lineage_gaps import (
    apply_family_link_suggestions,
    build_lineage_gap_report,
    suggest_family_links,
)
from duke_rates.cli_commands.lineage import _repair_lineage_effective_start_gaps


def _seed_family(conn, *, family_key: str, schedule_code: str, title: str) -> None:
    now = datetime(2026, 4, 1, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, aliases_json, notes, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            family_key,
            "NC",
            "progress",
            family_key.split("nc-progress-")[-1],
            schedule_code,
            "rate_schedule",
            title,
            "[]",
            "Curated family.",
            now,
            now,
        ),
    )


def _seed_historical_document(
    conn,
    *,
    family_key: str,
    title: str,
    archived_url: str,
    local_path: str,
    content_hash: str,
    effective_start: str | None,
    leaf_no: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO historical_documents (
            current_document_id, family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path, raw_text_path,
            content_hash, content_type, direct_status_code, direct_downloadable,
            revision_label, supersedes_label, leaf_no, effective_start, effective_end,
            retrieved_at, metadata_json, parsed_result_json, start_page, end_page, evidence_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            None,
            family_key,
            title,
            "NC",
            "progress",
            "tariff",
            "pdf",
            archived_url,
            archived_url,
            "2024-01-01T00:00:00+00:00",
            local_path,
            None,
            content_hash,
            "application/pdf",
            200,
            1,
            None,
            None,
            leaf_no,
            effective_start,
            None,
            "2024-01-02T00:00:00+00:00",
            "{}",
            None,
            1,
            2,
            "{}",
        ),
    )
    return int(cur.lastrowid)


def _seed_tariff_version(
    conn,
    *,
    family_key: str,
    historical_document_id: int | None,
    effective_start: str,
    note: str = "Seeded version.",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, notes, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            family_key,
            historical_document_id,
            effective_start,
            "regulator",
            0.9,
            note,
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )
    return int(cur.lastrowid)


def _seed_charge(conn, *, version_id: int, family_key: str) -> None:
    conn.execute(
        """
        INSERT INTO tariff_charges (
            version_id, family_key, charge_type, charge_label, rate_value, rate_unit,
            source_snippet, confidence_score, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            version_id,
            family_key,
            "fixed",
            "Customer Charge",
            14.0,
            "month",
            "Customer Charge $14.00",
            0.9,
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )


def _seed_unlinked_discovery_with_span(conn, *, record_id: int, local_path: str) -> None:
    now = datetime(2026, 4, 1, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO ncuc_discovery_records (
            id, docket_number, sub_number, utility, filing_title, filing_date,
            proceeding_type, filing_classification, exhibit_label,
            referenced_schedule_codes_json, referenced_rider_codes_json,
            referenced_leaf_nos_json, family_keys_json, discovered_url, viewer_url,
            attachment_url, download_url, acquisition_method, fetch_status, local_path,
            content_hash, content_type, file_size_bytes, provenance_notes_json,
            search_query, page_title, error_detail, metadata_json, created_at, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            record_id,
            "E-2 SUB 1300",
            None,
            "Duke Energy Progress",
            "Schedule RES Residential Service Leaf No. 500",
            "2024-01-01",
            None,
            "tariff_sheets",
            None,
            "[]",
            "[]",
            "[]",
            "[]",
            None,
            None,
            None,
            None,
            "manual_seed",
            "success",
            local_path,
            f"hash-discovery-{record_id}",
            "application/pdf",
            1234,
            "[]",
            None,
            None,
            None,
            "{}",
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO ncuc_span_artifacts (
            discovery_record_id, source_pdf, file_hash, artifact_version, span_index,
            start_page, end_page, doc_type, confidence, extracted_leaf_nos_json,
            extracted_schedule_titles_json, header_footer_snippets_json, dates_json,
            evidence_score_breakdown_json, metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            record_id,
            local_path,
            f"hash-discovery-{record_id}",
            "test-v1",
            0,
            1,
            2,
            "tariff",
            0.9,
            json.dumps(["500"]),
            json.dumps(["Schedule RES Residential Service"]),
            "[]",
            "[]",
            "{}",
            "{}",
            now,
            now,
        ),
    )


def test_suggest_family_links_and_apply_updates_discovery_record(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    _seed_family(conn, family_key="nc-progress-leaf-500", schedule_code="RES", title="Residential Service")
    historical_id = _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-500",
        title="Residential Service",
        archived_url="https://example.com/leaf500.pdf",
        local_path=str(tmp_path / "leaf500.pdf"),
        content_hash="hash-leaf500",
        effective_start="2024-01-01",
        leaf_no="500",
    )
    version_id = _seed_tariff_version(
        conn,
        family_key="nc-progress-leaf-500",
        historical_document_id=historical_id,
        effective_start="2024-01-01",
    )
    _seed_charge(conn, version_id=version_id, family_key="nc-progress-leaf-500")
    _seed_unlinked_discovery_with_span(conn, record_id=101, local_path=str(tmp_path / "discovery.pdf"))
    conn.commit()
    conn.close()

    repo = Repository(db_path)
    suggestions = suggest_family_links(repo, limit=None)

    assert len(suggestions) == 1
    assert suggestions[0]["discovery_record_id"] == 101
    assert suggestions[0]["matches"][0]["family_key"] == "nc-progress-leaf-500"

    updated = apply_family_link_suggestions(repo, suggestions)
    assert updated == 1

    with repo._connect() as verify_conn:
        row = verify_conn.execute(
            """
            SELECT family_keys_json, provenance_notes_json
            FROM ncuc_discovery_records
            WHERE id = 101
            """
        ).fetchone()

    assert json.loads(row["family_keys_json"]) == ["nc-progress-leaf-500"]
    assert "family_keys_backfilled_from_span_artifacts" in json.loads(row["provenance_notes_json"])


def test_build_lineage_gap_report_counts_expected_gap_surfaces(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    _seed_family(conn, family_key="nc-progress-leaf-500", schedule_code="RES", title="Residential Service")
    _seed_family(conn, family_key="nc-progress-leaf-700", schedule_code="GS", title="General Service")
    _seed_family(conn, family_key="nc-progress-leaf-701", schedule_code="LGS", title="Large General Service")

    linked_doc_id = _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-500",
        title="Residential Service",
        archived_url="https://example.com/linked.pdf",
        local_path=str(tmp_path / "linked.pdf"),
        content_hash="hash-linked",
        effective_start="2024-01-01",
        leaf_no="500",
    )
    linked_version_id = _seed_tariff_version(
        conn,
        family_key="nc-progress-leaf-500",
        historical_document_id=linked_doc_id,
        effective_start="2024-01-01",
    )
    _seed_charge(conn, version_id=linked_version_id, family_key="nc-progress-leaf-500")

    _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-700",
        title="General Service Missing Date",
        archived_url="https://example.com/missing-date.pdf",
        local_path=str(tmp_path / "missing-date.pdf"),
        content_hash="hash-missing-date",
        effective_start=None,
        leaf_no="700",
    )
    _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-700",
        title="General Service Missing Version",
        archived_url="https://example.com/missing-version.pdf",
        local_path=str(tmp_path / "missing-version.pdf"),
        content_hash="hash-missing-version",
        effective_start="2024-02-01",
        leaf_no="700",
    )

    orphan_version_id = _seed_tariff_version(
        conn,
        family_key="nc-progress-leaf-701",
        historical_document_id=None,
        effective_start="2024-03-01",
        note="Orphan NC version.",
    )
    _seed_charge(conn, version_id=orphan_version_id, family_key="nc-progress-leaf-701")

    _seed_unlinked_discovery_with_span(conn, record_id=202, local_path=str(tmp_path / "discovery-gap.pdf"))

    conn.commit()
    conn.close()

    repo = Repository(db_path)
    report = build_lineage_gap_report(repo, limit=10)
    summary = report["summary"]

    assert summary["unlinked_discovery_records_count"] == 1
    assert summary["auto_matchable_discovery_records_count"] == 1
    assert summary["historical_missing_effective_start_count"] == 1
    assert summary["historical_missing_version_count"] == 1
    assert summary["versions_missing_historical_document_id_count"] == 1
    assert summary["families_without_charges_count"] == 1
    assert report["families_without_charges"][0]["family_key"] == "nc-progress-leaf-700"


def test_repair_lineage_effective_start_gaps_fills_single_date_pdf_and_creates_version(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    _seed_family(conn, family_key="nc-progress-leaf-800", schedule_code="RES", title="Residential Service")
    _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-800",
        title="Residential Service Dated",
        archived_url="https://example.com/dated.pdf",
        local_path=str(tmp_path / "bundle.pdf"),
        content_hash="hash-dated",
        effective_start="2024-01-01",
        leaf_no="800",
    )
    missing_id = _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-800",
        title="Residential Service Missing Date",
        archived_url="https://example.com/missing.pdf",
        local_path=str(tmp_path / "bundle.pdf"),
        content_hash="hash-missing",
        effective_start=None,
        leaf_no="800",
    )
    conn.commit()

    dry_run = _repair_lineage_effective_start_gaps(conn, dry_run=True, limit=10)

    assert dry_run["effective_starts_repaired"] == 1
    assert dry_run["versions_created"] == 1
    assert conn.execute(
        "SELECT effective_start FROM historical_documents WHERE id = ?",
        (missing_id,),
    ).fetchone()[0] is None

    executed = _repair_lineage_effective_start_gaps(conn, dry_run=False, limit=10)

    assert executed["effective_starts_repaired"] == 1
    assert executed["versions_created"] == 1
    assert conn.execute(
        "SELECT effective_start FROM historical_documents WHERE id = ?",
        (missing_id,),
    ).fetchone()[0] == "2024-01-01"
    version = conn.execute(
        """
        SELECT historical_document_id, effective_start, source_type
        FROM tariff_versions
        WHERE historical_document_id = ?
        """,
        (missing_id,),
    ).fetchone()
    assert dict(version) == {
        "historical_document_id": missing_id,
        "effective_start": "2024-01-01",
        "source_type": "regulator",
    }

    conn.close()


def test_repair_lineage_effective_start_gaps_skips_ambiguous_pdf_dates(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    _seed_family(conn, family_key="nc-progress-leaf-801", schedule_code="RES", title="Residential Service")
    for idx, effective_start in enumerate(["2024-01-01", "2025-01-01"], start=1):
        _seed_historical_document(
            conn,
            family_key="nc-progress-leaf-801",
            title=f"Residential Service Dated {idx}",
            archived_url=f"https://example.com/dated-{idx}.pdf",
            local_path=str(tmp_path / "bundle.pdf"),
            content_hash=f"hash-dated-{idx}",
            effective_start=effective_start,
            leaf_no="801",
        )
    missing_id = _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-801",
        title="Residential Service Missing Date",
        archived_url="https://example.com/missing.pdf",
        local_path=str(tmp_path / "bundle.pdf"),
        content_hash="hash-missing",
        effective_start=None,
        leaf_no="801",
    )
    conn.commit()

    result = _repair_lineage_effective_start_gaps(conn, dry_run=False, limit=10)

    assert result["candidates_found"] == 0
    assert conn.execute(
        "SELECT effective_start FROM historical_documents WHERE id = ?",
        (missing_id,),
    ).fetchone()[0] is None
    conn.close()


def test_deduplicate_historical_documents_uses_span_scoped_hash_groups(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    _seed_family(conn, family_key="nc-progress-leaf-900", schedule_code="RS", title="Residential Service")
    _seed_family(conn, family_key="nc-progress-leaf-901", schedule_code="GS", title="General Service")

    rows = [
        (1, "nc-progress-leaf-900", "Residential A", "https://example.com/a.pdf", "copy-a.pdf", 1, 2),
        (2, "nc-progress-leaf-900", "Residential A Copy", "https://example.com/b.pdf", "copy-b.pdf", 1, 2),
        (3, "nc-progress-leaf-900", "Residential B", "https://example.com/c.pdf", "span-b.pdf", 3, 4),
        (4, "nc-progress-leaf-901", "General A", "https://example.com/d.pdf", "other-family.pdf", 1, 2),
    ]
    for hd_id, family_key, title, archived_url, local_name, start_page, end_page in rows:
        conn.execute(
            """
            INSERT INTO historical_documents (
                id, current_document_id, family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp, local_path, raw_text_path,
                content_hash, content_type, direct_status_code, direct_downloadable,
                revision_label, supersedes_label, leaf_no, effective_start, effective_end,
                retrieved_at, metadata_json, parsed_result_json, start_page, end_page, evidence_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                hd_id,
                None,
                family_key,
                title,
                "NC",
                "progress",
                "tariff",
                "pdf",
                archived_url,
                archived_url,
                "2024-01-01T00:00:00+00:00",
                str(tmp_path / local_name),
                None,
                "shared-hash",
                "application/pdf",
                200,
                1,
                None,
                None,
                "900",
                "2024-01-01",
                None,
                "2024-01-02T00:00:00+00:00",
                "{}",
                None,
                start_page,
                end_page,
                "{}",
            ),
        )
    conn.commit()
    conn.close()

    repo = Repository(db_path)
    dry_run = repo.deduplicate_historical_documents(dry_run=True)

    assert dry_run["total_groups"] == 1
    assert dry_run["documents_removed"] == 1
    assert dry_run["per_group"][0]["removed_ids"] == [2]
    assert dry_run["per_group"][0]["start_page"] == 1
    assert dry_run["per_group"][0]["end_page"] == 2
