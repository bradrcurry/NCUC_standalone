"""Additive D6 document relationships alongside existing version supersession."""

DOCUMENT_RELATIONSHIP_DDL = """
CREATE TABLE IF NOT EXISTS document_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relationship_uid TEXT NOT NULL UNIQUE,
    relationship_kind TEXT NOT NULL CHECK(relationship_kind IN
        ('proposed_revision_of','approved_by','implements','references','clean_counterpart_of')),
    source_sha256 TEXT NOT NULL,
    source_version_id INTEGER REFERENCES tariff_versions(id),
    target_sha256 TEXT,
    target_version_id INTEGER REFERENCES tariff_versions(id),
    target_reference_json TEXT NOT NULL,
    supporting_clue_ids_json TEXT NOT NULL,
    evidence_basis TEXT NOT NULL
        CHECK(evidence_basis IN ('source_asserted','inferred','source_corroborated')),
    status TEXT NOT NULL CHECK(status IN ('candidate','explicit_unresolved','verified','conflict')),
    checks_json TEXT NOT NULL,
    builder_version TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    human_review TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK(human_review IN ('unreviewed','confirmed','rejected')),
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_relationship_source
ON document_relationships(source_sha256, active);
"""


def document_relationship_usage(conn) -> dict[str, int]:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='document_relationships'"
    ).fetchone():
        return {}
    return {
        "reviewed_document_relationships": conn.execute(
            "SELECT COUNT(*) FROM document_relationships WHERE human_review <> 'unreviewed'"
        ).fetchone()[0]
    }
