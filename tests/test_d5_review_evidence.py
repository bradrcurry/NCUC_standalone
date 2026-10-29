"""The existing review queue must distinguish source identity and risk from proof."""

from __future__ import annotations

import hashlib
from pathlib import Path

from test_d3_ingestion_validation import _use_source
from test_extraction_provenance import db, migrated  # noqa: F401

from duke_rates.historical.ncuc import evidence_review
from duke_rates.historical.ncuc.ingestion_validation import audit_version_sources


def test_queue_keeps_actual_sha_separate_from_archive_identifier(migrated, tmp_path):  # noqa: F811
    conn, ids, _, _ = migrated
    source = tmp_path / "marked.pdf"
    _use_source(
        conn,
        ids,
        Path(__file__).parent / "fixtures/d2_redline/hd2275_leaf600_redline_p100.pdf",
        source,
    )
    conn.execute(
        "UPDATE historical_documents SET content_hash = 'portal:legacy-identifier' WHERE id = ?",
        (ids["hd_id"],),
    )
    conn.commit()
    audit = audit_version_sources(conn)
    (item,) = evidence_review._items_from_source_audit(conn, audit, {ids["version_id"]: "strong"})
    (document,) = item["source_documents"]
    assert document["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert document["archive_identifier"] == "portal:legacy-identifier"
    assert item["downstream"]["billing_usage_verified"] is False
    assert "not proof that a stored value is wrong" in item["explanation"]
