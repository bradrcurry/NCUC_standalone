import sqlite3

from duke_rates.document_intelligence.database_reports import (
    find_family_lineage_gaps,
    find_duplicate_documents,
    find_docket_coverage_summary,
    find_low_quality_parses,
    find_missing_docket_coverage,
    find_missing_versions,
    find_stale_artifacts,
    find_unknown_documents,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE parse_attempt_logs (
            id INTEGER PRIMARY KEY,
            source_pdf TEXT,
            parser_profile TEXT,
            status TEXT,
            confidence REAL,
            charge_count INTEGER,
            created_at TEXT
        );
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            local_path TEXT,
            family_key TEXT,
            title TEXT,
            effective_start TEXT
        );
        CREATE TABLE historical_reprocess_queue (
            id INTEGER PRIMARY KEY,
            historical_document_id INTEGER,
            source_pdf TEXT,
            status TEXT
        );
        """
    )
    return conn


def test_find_low_quality_parses_counts_only_actionable_unqueued_docs() -> None:
    conn = _conn()
    conn.executemany(
        """
        INSERT INTO historical_documents
            (id, local_path, family_key, title, effective_start)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, r"data\historical\nc\doc-a.pdf", "family-a", "Doc A", "2024-01-01"),
            (2, r"data\historical\nc\doc-b.pdf", "family-b", "Doc B", "2024-01-01"),
            (3, r"data\historical\nc\doc-c.pdf", "family-c", "Doc C", "2024-01-01"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO parse_attempt_logs
            (id, source_pdf, parser_profile, status, confidence, charge_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "data/historical/nc/doc-a.pdf", "generic_residential", "partial", 0.1, 0, "2026-01-01"),
            (2, r"data\historical\nc\doc-b.pdf", "unknown", "partial", 0.1, 0, "2026-01-01"),
            (3, r"data\historical\nc\doc-c.pdf", "unknown", "partial", 0.1, 0, "2026-01-01"),
            (4, r"data\orphan\doc-d.pdf", "tiered_ingest", "partial", 0.1, 0, "2026-01-01"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO historical_reprocess_queue
            (historical_document_id, source_pdf, status)
        VALUES (?, ?, ?)
        """,
        [
            (2, None, "completed"),
            (None, "data/historical/nc/doc-c.pdf", "completed"),
        ],
    )

    report = find_low_quality_parses(conn, limit=10, since="2025-01-01")

    assert report["summary"]["total_count"] == 1
    assert [row["source_pdf"] for row in report["rows"]] == [
        "data/historical/nc/doc-a.pdf"
    ]


def test_find_missing_versions_counts_bootstrap_actionable_docs_only() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            state TEXT,
            company TEXT,
            local_path TEXT,
            family_key TEXT,
            title TEXT,
            effective_start TEXT
        );
        CREATE TABLE tariff_families (
            family_key TEXT PRIMARY KEY,
            state TEXT,
            company TEXT
        );
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY,
            family_key TEXT,
            historical_document_id INTEGER,
            effective_start TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO tariff_families (family_key, state, company) VALUES (?, ?, ?)",
        ("family-a", "NC", "progress"),
    )
    conn.executemany(
        """
        INSERT INTO historical_documents
            (id, state, company, local_path, family_key, title, effective_start)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "NC", "progress", "doc-a.pdf", "family-a", "Doc A", "2024-01-01"),
            (2, "NC", "progress", "doc-b.pdf", "family-a", "Doc B", "2025-01-01"),
            (3, "NC", "progress", "doc-c.pdf", "family-a", "Doc C", None),
            (4, "SC", "progress", "doc-d.pdf", "family-a", "Doc D", "2024-01-01"),
        ],
    )
    conn.executemany(
        "INSERT INTO tariff_versions (id, family_key, historical_document_id, effective_start) VALUES (?, ?, ?, ?)",
        [
            (1, "family-a", 1, "2024-01-01"),
            (2, "family-a", None, "0000-01-01"),
        ],
    )

    report = find_missing_versions(conn, limit=10)

    assert report["summary"]["total_count"] == 1
    assert report["summary"]["historical_docs_missing_versions"] == 1
    assert report["rows"][0]["historical_document_id"] == 2
    assert report["rows"][0]["effective_start"] == "2025-01-01"


def test_find_family_lineage_gaps_ignores_current_versions_with_document_links() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            state TEXT,
            company TEXT,
            local_path TEXT,
            family_key TEXT,
            title TEXT,
            effective_start TEXT,
            retrieved_at TEXT
        );
        CREATE TABLE tariff_families (
            family_key TEXT PRIMARY KEY,
            state TEXT,
            company TEXT
        );
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY,
            family_key TEXT,
            document_id INTEGER,
            historical_document_id INTEGER,
            effective_start TEXT,
            source_type TEXT,
            notes TEXT,
            created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO tariff_families (family_key, state, company) VALUES (?, ?, ?)",
        ("family-a", "NC", "progress"),
    )
    conn.executemany(
        """
        INSERT INTO tariff_versions
            (id, family_key, document_id, historical_document_id, effective_start, source_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "family-a", 10, None, "2026-01-01", "utility_current", "2026-01-01"),
            (2, "family-a", None, None, "2024-01-01", "regulator", "2026-01-01"),
        ],
    )

    report = find_family_lineage_gaps(conn, limit=10)

    assert report["summary"]["total_version_no_doc"] == 1
    assert report["summary"]["total_count"] == 1
    assert report["rows"][0]["version_id"] == 2


def test_find_family_lineage_gaps_counts_only_repairable_missing_effective_start() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            state TEXT,
            company TEXT,
            local_path TEXT,
            family_key TEXT,
            title TEXT,
            effective_start TEXT,
            retrieved_at TEXT
        );
        CREATE TABLE tariff_families (
            family_key TEXT PRIMARY KEY,
            state TEXT,
            company TEXT
        );
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY,
            family_key TEXT,
            document_id INTEGER,
            historical_document_id INTEGER,
            effective_start TEXT,
            source_type TEXT,
            notes TEXT,
            created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO tariff_families (family_key, state, company) VALUES (?, ?, ?)",
        ("family-a", "NC", "progress"),
    )
    conn.executemany(
        """
        INSERT INTO historical_documents
            (id, state, company, local_path, family_key, title, effective_start, retrieved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "NC", "progress", "bundle-a.pdf", "family-a", "Known", "2024-01-01", "2026-01-01"),
            (2, "NC", "progress", "bundle-a.pdf", "family-b", "Repairable", None, "2026-01-01"),
            (3, "NC", "progress", "bundle-b.pdf", "family-c", "No sibling date", None, "2026-01-01"),
            (4, "NC", "progress", "bundle-c.pdf", "family-d", "Known linked", "2025-01-01", "2026-01-01"),
            (5, "NC", "progress", "bundle-c.pdf", "family-d", "Already linked date", None, "2026-01-01"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO tariff_versions
            (id, family_key, historical_document_id, effective_start, source_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "family-a", 1, "2024-01-01", "regulator", "2026-01-01"),
            (2, "family-d", 4, "2025-01-01", "regulator", "2026-01-01"),
        ],
    )

    report = find_family_lineage_gaps(conn, limit=10)

    assert report["summary"]["total_no_effective_start"] == 1
    assert report["summary"]["total_count"] == 1
    assert report["rows"][0]["historical_document_id"] == 2
    assert report["rows"][0]["effective_start"] == "2024-01-01"


def test_find_stale_artifacts_counts_only_backfillable_missing_evidence() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE historical_reprocess_queue (
            id INTEGER PRIMARY KEY,
            historical_document_id INTEGER,
            source_pdf TEXT,
            family_key TEXT,
            queue_reason TEXT,
            priority INTEGER,
            status TEXT,
            requested_at TEXT
        );
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            state TEXT,
            local_path TEXT,
            content_hash TEXT,
            family_key TEXT,
            evidence_json TEXT,
            retrieved_at TEXT
        );
        CREATE TABLE ncuc_span_artifacts (
            id INTEGER PRIMARY KEY,
            file_hash TEXT,
            evidence_score_breakdown_json TEXT
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO historical_documents
            (id, state, local_path, content_hash, family_key, evidence_json, retrieved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "NC", "doc-a.pdf", "hash-a", "family-a", "{}", "2026-01-01"),
            (2, "NC", "doc-b.pdf", "hash-b", "family-b", "{}", "2026-01-01"),
            (3, "NC", "doc-c.pdf", "hash-c", "family-c", '{"ok": true}', "2026-01-01"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO ncuc_span_artifacts (file_hash, evidence_score_breakdown_json)
        VALUES (?, ?)
        """,
        [
            ("hash-a", '{"family-a": {"explicit_leaf_hit": 40.0}}'),
            ("hash-b", "{}"),
            ("hash-c", '{"family-c": {"explicit_leaf_hit": 40.0}}'),
        ],
    )

    report = find_stale_artifacts(conn, limit=10)

    assert report["summary"]["total_count"] == 1
    assert report["summary"]["no_evidence_json_total"] == 1
    assert report["rows"][0]["historical_document_id"] == 1


def test_find_duplicate_documents_uses_span_scoped_hash_groups() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            state TEXT,
            content_hash TEXT,
            family_key TEXT,
            local_path TEXT,
            start_page INTEGER,
            end_page INTEGER,
            retrieved_at TEXT
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO historical_documents
            (id, state, content_hash, family_key, local_path, start_page, end_page, retrieved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "NC", "hash-a", "family-a", "bundle.pdf", 1, 2, "2026-01-01"),
            (2, "NC", "hash-a", "family-a", "bundle-copy.pdf", 1, 2, "2026-01-02"),
            (3, "NC", "hash-a", "family-a", "bundle.pdf", 3, 4, "2026-01-03"),
            (4, "NC", "hash-a", "family-b", "bundle.pdf", 1, 2, "2026-01-04"),
        ],
    )

    report = find_duplicate_documents(conn, limit=10)

    assert report["summary"]["total_count"] == 1
    assert report["summary"]["total_duplicate_instances"] == 2
    assert report["rows"][0]["historical_document_ids"] == [1, 2]
    assert report["rows"][0]["start_page"] == 1
    assert report["rows"][0]["end_page"] == 2


def test_missing_docket_coverage_uses_import_pipeline_pending_predicate() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ncuc_discovery_records (
            id INTEGER PRIMARY KEY,
            docket_number TEXT,
            utility TEXT,
            filing_title TEXT,
            filing_date TEXT,
            fetch_status TEXT,
            local_path TEXT,
            content_hash TEXT,
            error_detail TEXT
        );
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            state TEXT,
            local_path TEXT,
            content_hash TEXT
        );
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY,
            historical_document_id INTEGER,
            docket_number TEXT
        );
        CREATE TABLE tariff_charges (
            id INTEGER PRIMARY KEY,
            version_id INTEGER
        );
        CREATE TABLE document_classifications (
            id INTEGER PRIMARY KEY,
            subject_kind TEXT,
            subject_id TEXT,
            stage TEXT,
            label TEXT,
            superseded_by INTEGER
        );
        CREATE TABLE ncuc_span_artifacts (
            id INTEGER PRIMARY KEY,
            discovery_record_id INTEGER
        );
        CREATE TABLE regulatory_docket_leads (
            docket_number TEXT,
            utility TEXT,
            title TEXT,
            evidence_source TEXT,
            evidence_source_type TEXT,
            confidence_score REAL
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO ncuc_discovery_records
            (id, docket_number, utility, filing_title, filing_date, fetch_status, local_path, content_hash, error_detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "E-2 Sub 1", "DEP", "downloaded imported", "2026-01-01", "success", "doc-a.pdf", "hash-a", None),
            (2, "E-2 Sub 1", "DEP", "legacy downloaded status", "2026-01-02", "downloaded", "doc-b.pdf", "hash-b", None),
            (3, "E-2 Sub 1", "DEP", "pending", "2026-01-03", "pending", None, None, None),
            (4, "E-2 Sub 2", "DEP", "already success", "2026-01-04", "success", "doc-c.pdf", "hash-c", None),
            (5, "E-2 Sub 1", "DEP", "success pending import", "2026-01-05", "success", "doc-d.pdf", "hash-d", None),
            (6, "E-2 Sub 1", "DEP", "success import skipped", "2026-01-06", "success", "doc-e.pdf", "hash-e", "import_skipped_oversized_pdf_pages=100_max=75"),
        ],
    )
    conn.executemany(
        "INSERT INTO historical_documents (id, state, local_path, content_hash) VALUES (?, ?, ?, ?)",
        [
            (10, "NC", "doc-a.pdf", "hash-a"),
            (11, "NC", "doc-c.pdf", "hash-c"),
        ],
    )
    conn.executemany(
        "INSERT INTO tariff_versions (id, historical_document_id, docket_number) VALUES (?, ?, ?)",
        [(100, 10, "E-2 Sub 1"), (101, 10, "E-2 Sub 1"), (102, 11, "E-2 Sub 2")],
    )
    conn.executemany(
        "INSERT INTO tariff_charges (id, version_id) VALUES (?, ?)",
        [(1000, 100), (1001, 100), (1002, 101)],
    )
    conn.executemany(
        "INSERT INTO ncuc_span_artifacts (id, discovery_record_id) VALUES (?, ?)",
        [(1, 1), (2, 4)],
    )

    report = find_missing_docket_coverage(conn, limit=10)

    assert report["summary"]["total_recommendations"] == 1
    row = report["recommendations"][0]
    assert row["docket_number"] == "E-2 Sub 1"
    assert row["discovery_records_count"] == 5
    assert row["historical_docs_count"] == 1
    assert row["tariff_versions_count"] == 2
    assert row["tariff_charges_count"] == 3
    assert row["downloaded_count"] == 4
    assert row["downloaded_not_imported_count"] == 1
    assert row["fetch_eligible_count"] == 1
    assert row["recommended_action"] == "import"


def test_docket_coverage_summary_exposes_actionable_recommendations() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ncuc_discovery_records (
            id INTEGER PRIMARY KEY,
            docket_number TEXT,
            utility TEXT,
            filing_title TEXT,
            filing_date TEXT,
            fetch_status TEXT,
            local_path TEXT,
            content_hash TEXT,
            error_detail TEXT
        );
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            state TEXT,
            local_path TEXT,
            content_hash TEXT
        );
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY,
            historical_document_id INTEGER,
            docket_number TEXT
        );
        CREATE TABLE tariff_charges (
            id INTEGER PRIMARY KEY,
            version_id INTEGER
        );
        CREATE TABLE document_classifications (
            id INTEGER PRIMARY KEY,
            subject_kind TEXT,
            subject_id TEXT,
            stage TEXT,
            label TEXT,
            superseded_by INTEGER
        );
        CREATE TABLE ncuc_span_artifacts (
            id INTEGER PRIMARY KEY,
            discovery_record_id INTEGER
        );
        CREATE TABLE regulatory_docket_leads (
            docket_number TEXT,
            utility TEXT,
            title TEXT,
            evidence_source TEXT,
            evidence_source_type TEXT,
            confidence_score REAL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO ncuc_discovery_records
            (id, docket_number, utility, filing_title, filing_date, fetch_status, local_path, content_hash)
        VALUES (1, 'E-7 Sub 1', 'DEC', 'pending', '2026-01-01', 'requires_browser', NULL, NULL)
        """
    )
    conn.execute(
        """
        INSERT INTO regulatory_docket_leads
            (docket_number, utility, title, evidence_source, evidence_source_type, confidence_score)
        VALUES ('E-2 Sub 9', 'DEP', 'lead', 'source', 'test', 0.9)
        """
    )

    report = find_docket_coverage_summary(conn, limit=10)

    assert report["summary"]["total_count"] == 2
    assert report["summary"]["fetch_eligible_records"] == 1
    assert report["summary"]["dockets_requiring_fetch"] == 1
    assert report["summary"]["leads_without_discovery"] == 1


def test_find_unknown_documents_counts_unresolved_distinct_subjects_not_join_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE document_classifications (
            id INTEGER PRIMARY KEY,
            subject_kind TEXT,
            subject_id TEXT,
            stage TEXT,
            label TEXT,
            confidence REAL,
            classifier TEXT,
            classifier_version TEXT,
            superseded_by INTEGER,
            created_at TEXT
        );
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            title TEXT,
            local_path TEXT,
            family_key TEXT,
            effective_start TEXT
        );
        CREATE TABLE document_fingerprints_v2 (
            id INTEGER PRIMARY KEY,
            source_pdf TEXT,
            cluster_signature_v1 TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO historical_documents
            (id, title, local_path, family_key, effective_start)
        VALUES
            (1, 'Schedule 1', 'doc-a.pdf', 'family-a', '2024-01-01'),
            (2, 'Schedule 2', 'doc-b.pdf', 'family-b', '2024-01-01')
        """
    )
    conn.executemany(
        """
        INSERT INTO document_classifications
            (subject_kind, subject_id, stage, label, confidence, classifier,
             classifier_version, superseded_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("historical_document", "1", "document_type", "UNKNOWN", 0.1, "rule_document_type_v1", "v1", None, "2026-01-01"),
            ("historical_document", "1", "document_type", "TARIFF_SHEET", 0.8, "embedding_knn_v1", "v1", None, "2026-01-02"),
            ("historical_document", "1", "document_type", "TARIFF_SHEET", 0.9, "llm_qwen_v1", "v1", None, "2026-01-02"),
            ("historical_document", "2", "document_type", "UNKNOWN", 0.1, "rule_document_type_v1", "v1", None, "2026-01-01"),
            ("historical_document", "2", "document_type", "ORDER_FINAL", 0.2, "embedding_knn_v1", "v1", None, "2026-01-02"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO document_fingerprints_v2 (source_pdf, cluster_signature_v1)
        VALUES (?, ?)
        """,
        [
            ("doc-a.pdf", "cluster-a"),
            ("doc-a.pdf", "cluster-a"),
            ("doc-b.pdf", "cluster-b"),
            ("doc-b.pdf", "cluster-b"),
        ],
    )

    report = find_unknown_documents(conn, limit=10)

    assert report["summary"]["total_count"] == 1
    assert report["summary"]["largest_cluster_size"] == 1
    assert report["rows"][0]["documents"][0]["subject_id"] == "2"
    assert report["rows"][0]["documents"][0]["rule_label"] == "UNKNOWN"
    assert report["rows"][0]["documents"][0]["embedding_label"] == "ORDER_FINAL"
