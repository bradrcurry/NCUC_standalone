"""D6 source integrity, cache invalidation, and authority boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_d1_clue_ledger import _artifacts, _insert, _pdf, archive  # noqa: F401

from duke_rates.db.provenance_schema import ensure_provenance_schema, provenance_usage
from duke_rates.download.storage import preserve_pdf_blob
from duke_rates.historical.ncuc import clue_ledger, clue_lineage
from duke_rates.historical.ncuc import local_gap_resolution as lr
from duke_rates.historical.ncuc import remote_discovery as rd
from duke_rates.historical.ncuc.document_references import order_reference_index
from duke_rates.historical.ncuc.document_relationships import build_document_relationships


def test_cache_invalidates_in_place_text_catalog_and_source_changes(archive):  # noqa: F811
    conn, ids = archive
    before = lr.archive_state(conn)
    conn.execute(
        "UPDATE ncuc_page_artifacts SET text_content=text_content || ' changed' WHERE id=1"
    )
    after_text = lr.archive_state(conn)
    assert after_text != before
    assert after_text["native_page_artifacts"] == before["native_page_artifacts"]
    _insert(
        conn,
        "ncuc_discovery_records",
        docket_number="E-7 Sub 1026",
        filing_classification="order",
        created_at="t",
    )
    after_catalog = lr.archive_state(conn)
    assert after_catalog != after_text
    ids.paths.loose.unlink()
    assert lr.archive_state(conn) != after_catalog


def test_unavailable_candidate_is_incomplete_and_retried(archive):  # noqa: F811
    conn, ids = archive
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    ids.paths.loose.unlink()
    gap = conn.execute(
        "SELECT id FROM lineage_gaps_typed WHERE gap_type='missing_predecessor_revision' "
        "AND anchor_version_id=?",
        (ids.v_rev4,),
    ).fetchone()[0]
    lr.resolve_gaps_locally(conn, created_by="test", gap_ids=[gap])
    row = conn.execute(
        "SELECT outcome,stop_reason FROM search_attempts WHERE gap_id=? ORDER BY id DESC", (gap,)
    ).fetchone()
    assert tuple(row) == ("incomplete", "source_evidence_unavailable")
    assert (
        conn.execute("SELECT status FROM lineage_gaps_typed WHERE id=?", (gap,)).fetchone()[0]
        == "open"
    )
    assert not rd.local_search_complete(conn, gap)
    assert (
        lr.resolve_gaps_locally(conn, created_by="test", gap_ids=[gap])["stats"][
            "repeated_searches_avoided"
        ]
        == 1
    )
    assert (
        lr.resolve_gaps_locally(conn, created_by="test", gap_ids=[gap], force=True)["stats"][
            "local_searches_run"
        ]
        == 1
    )


def test_multi_docket_exact_evidence_preserves_filing_identity_and_reviews(archive, tmp_path):  # noqa: F811
    conn, _ = archive
    path = tmp_path / "joint.pdf"
    texts = [
        "STATE OF NORTH CAROLINA\nDOCKET NO. E-7, SUB 1026\nDOCKET NO. E-7, SUB 1214\nORDER",
        "The earlier order in DOCKET NO. E-7, SUB 487 is discussed here.",
    ]
    sha, _ = _pdf(path, texts)
    _artifacts(conn, path, texts, sha)
    rid = _insert(
        conn,
        "ncuc_discovery_records",
        docket_number="E-7, Sub 1214",
        local_path=str(path),
        filing_classification="order",
        fetch_status="fetched",
        created_at="t",
    )
    result = order_reference_index(conn, persist=True)
    assert result["references"]["E-7 Sub 1026"][0]["role"] == "caption"
    body = result["references"]["E-7 Sub 487"][0]
    assert body["role"] == "body_reference" and body["page"] == 2
    assert body["file_sha256"] == sha and body["bbox_status"] == "located"
    assert texts[1][body["char_start"] : body["char_end"]] == body["printed_text"]
    assert not body["authority_scope_established"]
    cid = body["clue_id"]
    conn.execute(
        "UPDATE regulatory_clues SET verification_status='verified',"
        "verified_by='independent reviewer' "
        "WHERE id=?",
        (cid,),
    )
    order_reference_index(conn, persist=True)
    assert (
        conn.execute("SELECT verified_by FROM regulatory_clues WHERE id=?", (cid,)).fetchone()[0]
        == "independent reviewer"
    )
    assert (
        conn.execute(
            "SELECT docket_number FROM ncuc_discovery_records WHERE id=?", (rid,)
        ).fetchone()[0]
        == "E-7, Sub 1214"
    )
    build_document_relationships(conn)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM document_relationships WHERE status='verified'"
        ).fetchone()[0]
        == 0
    )


def test_blob_preserves_bytes_deduplicates_and_refuses_conflict(tmp_path):
    incoming = tmp_path / "original.pdf"
    _pdf(incoming, ["Evidence"])
    before = incoming.read_bytes()
    path, sha, reused = preserve_pdf_blob(incoming, tmp_path / "archive")
    assert not reused and path.read_bytes() == before
    assert sha == hashlib.sha256(before).hexdigest() and path.stem == sha
    assert preserve_pdf_blob(incoming, tmp_path / "archive") == (path, sha, True)
    path.write_bytes(b"conflicting existing evidence")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        preserve_pdf_blob(incoming, tmp_path / "archive")
    assert path.read_bytes() == b"conflicting existing evidence"


def test_relationship_reviews_survive_rebuild_and_block_drop(archive):  # noqa: F811
    conn, _ = archive
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    row = conn.execute("SELECT id FROM document_relationships LIMIT 1").fetchone()
    assert row
    conn.execute(
        "UPDATE document_relationships SET human_review='rejected',reviewed_by='person' WHERE id=?",
        (row[0],),
    )
    build_document_relationships(conn)
    assert provenance_usage(conn)["reviewed_document_relationships"] == 1
    assert conn.execute(
        "SELECT human_review,reviewed_by FROM document_relationships WHERE id=?", (row[0],)
    ).fetchone()[:] == ("rejected", "person")


def test_schema_upgrade_is_additive_and_idempotent(archive):  # noqa: F811
    conn, _ = archive
    conn.execute("DROP TABLE document_relationships")
    conn.execute("UPDATE provenance_schema_meta SET value='5' WHERE key='schema_version'")
    before = {
        r[0]: r[1] for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
    }
    rows = conn.execute("SELECT COUNT(*) FROM historical_documents").fetchone()[0]
    ensure_provenance_schema(conn)
    ensure_provenance_schema(conn)
    after = {
        r[0]: r[1] for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
    }
    assert all(after[name] == ddl for name, ddl in before.items())
    assert set(after) - set(before) == {"document_relationships"}
    assert conn.execute("SELECT COUNT(*) FROM historical_documents").fetchone()[0] == rows


def test_proposed_header_cannot_verify_actual_supersession():
    version = clue_lineage.VersionEvidence(
        1, "nc-progress-leaf-600", None, "2024-01-01", "proposed"
    )
    version.redline_markers = [{"marker": "proposed_header", "clue_id": 12}]
    status, target, clue, checks = clue_lineage._evaluate_target(
        version, ("leaf", "NC", "600", 1), [], {}, False
    )
    assert status == "candidate" and target is None and clue is None
    assert checks["proposed_header"] is True


def test_incomplete_local_attempt_can_plan_targeted_fallback(archive):  # noqa: F811
    conn, ids = archive
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    ids.paths.loose.unlink()
    lr.resolve_gaps_locally(conn, created_by="test")
    gap = conn.execute(
        "SELECT id FROM lineage_gaps_typed WHERE gap_type='missing_predecessor_revision' "
        "AND anchor_version_id=?",
        (ids.v_rev4,),
    ).fetchone()[0]
    targets = [
        target
        for query in rd.plan_network_searches(conn)["queries"]
        for target in query["targets"]
        if target.get("gap_id") == gap
    ]
    assert targets and all(t["local_search_complete"] is False for t in targets)


def test_order_negative_cache_does_not_cross_connections_or_catalog_updates(archive):  # noqa: F811
    conn, _ = archive
    assert not clue_lineage._local_order_exists(conn, "E-7 Sub 1026")
    _insert(
        conn,
        "ncuc_discovery_records",
        docket_number="E-7 Sub 1026",
        sub_number="1026",
        filing_classification="order",
        fetch_status="fetched",
        created_at="t",
    )
    assert clue_lineage._local_order_exists(conn, "E-7 Sub 1026")


def test_explicit_implements_statement_retains_quote_without_approval(archive, tmp_path):  # noqa: F811
    from duke_rates.historical.ncuc.document_references import extract_document_references

    conn, _ = archive
    text = "This tariff implements the order in DOCKET NO. E-7, SUB 1026."
    clues = extract_document_references(text, 1)
    claims = [c for c in clues if c.kind == "approval_reference"]
    assert len(claims) == 1
    assert claims[0].normalized["relationship"] == "implements"
    assert text[claims[0].char_start : claims[0].char_end] == claims[0].printed_text
    path = tmp_path / "implementation.pdf"
    sha, _ = _pdf(path, [text])
    _artifacts(conn, path, [text], sha)
    identity = clue_ledger.file_identity(str(path))
    page = clue_ledger.load_pdf_pages(conn, str(path), identity).pages[1]
    clue_ledger.write_page_clues(
        conn,
        source_pdf=str(path),
        identity=identity,
        page=page,
        scan_reason="test",
        locator=None,
        page_clues=clues,
        producer="document_references",
        version="1.0.0",
    )
    build_document_relationships(conn)
    row = conn.execute(
        "SELECT evidence_basis,status,checks_json FROM document_relationships "
        "WHERE relationship_kind='implements'"
    ).fetchone()
    assert tuple(row[:2]) == ("source_asserted", "candidate")
    assert json.loads(row[2])["authority_scope_established"] is False


def test_retrieval_rechecks_cached_bytes_and_archives_originals(archive, tmp_path):  # noqa: F811
    conn, ids = archive
    url = "https://example.test/order.pdf"

    def candidate(uid):
        return _insert(
            conn,
            "remote_document_candidates",
            candidate_uid=uid,
            kind="order",
            status="listed",
            params_hash=uid,
            channel="portal_orders",
            search_attempt_id=attempt.id,
            view_file_urls_json=json.dumps([url]),
            target_json="{}",
            created_at="t",
            updated_at="t",
        )

    from duke_rates.historical.ncuc.search_ledger import SearchLedger

    with SearchLedger(conn, created_by="test").attempt(
        "portal_orders", {"docket": "E-7 Sub 1026"}, caller="test"
    ) as attempt:
        attempt.finish(refs=[])
    source = tmp_path / "source.pdf"
    _pdf(source, ["DOCKET NO. E-7, SUB 1026\nORDER"])
    calls = []

    def download(_url, destination):
        calls.append(_url)
        Path(destination).write_bytes(source.read_bytes())
        return source.stat().st_size

    first = candidate("first")
    rd.retrieve_and_check(conn, [first], download=download, dest_dir=tmp_path / "archive")
    row = conn.execute(
        "SELECT local_path,file_sha256 FROM remote_document_candidates WHERE id=?", (first,)
    ).fetchone()
    assert Path(row[0]).stem == row[1] and Path(row[0]).read_bytes() == source.read_bytes()
    assert (
        conn.execute("SELECT COUNT(*) FROM document_blobs WHERE sha256=?", (row[1],)).fetchone()[0]
        == 1
    )
    # A missing cached copy causes another download; source bytes are not silently trusted.
    Path(row[0]).unlink()
    second = candidate("second")
    stats = rd.retrieve_and_check(conn, [second], download=download, dest_dir=tmp_path / "archive")
    assert stats["invalid_cached_files"] == 1 and len(calls) == 2
    Path(row[0]).write_bytes(b"mutated source")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        rd.review_candidate(
            conn, second, decision="linked", reviewer="person", version_id=ids.v_rev2
        )


def test_stale_higher_artifact_version_cannot_hide_current_file_identity(archive):  # noqa: F811
    conn, ids = archive
    _artifacts(
        conn,
        ids.paths.loose,
        ["Cover", "Duke Energy Progress, LLC NC Ninth Revised Leaf No. 600"],
        "0" * 64,
        version="page_miner_v99",
    )
    index = lr.build_archive_index(conn)
    assert (str(ids.paths.loose), 2) in index.pages_by_token[("leaf", "NC", "600", 3)]


def test_unchanged_repeat_does_not_build_page_index(archive, monkeypatch):  # noqa: F811
    conn, _ = archive
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    lr.resolve_gaps_locally(conn, created_by="test")

    def unexpected(*args, **kwargs):
        pytest.fail("unchanged completed search rebuilt the page index")

    monkeypatch.setattr(lr, "build_archive_index", unexpected)
    report = lr.resolve_gaps_locally(conn, created_by="test")
    assert report["stats"]["repeated_searches_avoided"] > 0 and report["pages_indexed"] == 0


def test_proposed_revision_relationship_is_retained_without_actual_supersession(archive, tmp_path):  # noqa: F811
    conn, ids = archive
    text = (
        "Duke Energy Progress, LLC NC (Proposed) Fourth Revised Leaf No. 600\n"
        "Superseding NC Second Revised Leaf No. 600\n"
        "Effective for service rendered on and after October 1, 2027"
    )
    path = tmp_path / "proposal.pdf"
    sha, _ = _pdf(path, [text])
    _artifacts(conn, path, [text], sha)
    hid = _insert(
        conn,
        "historical_documents",
        family_key="nc-progress-leaf-600",
        local_path=str(path),
        content_hash=sha,
        start_page=1,
        end_page=1,
        status="proposed",
    )
    vid = _insert(
        conn,
        "tariff_versions",
        family_key="nc-progress-leaf-600",
        historical_document_id=hid,
        effective_start="2027-10-01",
        status="proposed",
    )
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    link = conn.execute(
        "SELECT status FROM lineage_links WHERE from_version_id=?", (vid,)
    ).fetchone()
    assert link and link[0] == "candidate"
    relation = conn.execute(
        "SELECT status,evidence_basis,human_review FROM document_relationships "
        "WHERE source_version_id=? AND relationship_kind='proposed_revision_of'",
        (vid,),
    ).fetchone()
    assert tuple(relation) == ("candidate", "source_asserted", "unreviewed")


def test_research_benchmark_refuses_production_and_hardlink_alias_before_open(
    tmp_path, monkeypatch
):
    import os
    import runpy
    import sqlite3

    benchmark = Path(__file__).resolve().parents[1] / "bench/ncuc_discovery_evidence.py"
    namespace = runpy.run_path(str(benchmark))
    open_snapshot = namespace["open_snapshot"]
    fake_root = tmp_path / "workspace"
    production = fake_root / "data/db/duke_rates.db"
    production.parent.mkdir(parents=True)
    production.write_bytes(b"protected database bytes")
    open_snapshot.__globals__["__file__"] = str(fake_root / "bench/ncuc_discovery_evidence.py")
    alias = tmp_path / "production_alias.db"
    os.link(production, alias)

    def unexpected(*args, **kwargs):
        pytest.fail("benchmark opened production before refusal")

    monkeypatch.setattr(sqlite3, "connect", unexpected)
    for path in (production, alias):
        with pytest.raises(ValueError, match="refusing to write production"):
            open_snapshot(path, writable=True)
    assert production.read_bytes() == b"protected database bytes"


def test_blank_order_page_is_incomplete_coverage(archive, tmp_path):  # noqa: F811
    conn, _ = archive
    path = tmp_path / "blank_order.pdf"
    _pdf(path, [""])
    _insert(
        conn,
        "ncuc_discovery_records",
        docket_number="E-7 Sub 1026",
        filing_classification="order",
        local_path=str(path),
        created_at="t",
    )
    result = order_reference_index(conn)
    coverage = next(c for c in result["coverage"] if c["source_pdf"] == str(path))
    assert coverage["complete"] is False and coverage["pages_without_native_text"] == [1]
