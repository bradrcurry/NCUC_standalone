"""Phase 2E D1: regulatory clue ledger, lineage links, typed gaps, local resolution, search ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from duke_rates.db.provenance_schema import (
    PROVENANCE_SCHEMA_VERSION,
    ProvenanceSchemaInUseError,
    drop_provenance_schema,
    ensure_provenance_schema,
)
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc import clue_ledger, clue_lineage, local_gap_resolution
from duke_rates.historical.ncuc.regulatory_clues import extract_page_clues, read_date
from duke_rates.historical.ncuc.search_ledger import SearchLedger, canonical_params
from duke_rates.parse.heuristics import extract_docket_footer, extract_supersedes

fitz = pytest.importorskip("fitz")


# ---------------------------------------------------------------------------
# Page clue extraction
# ---------------------------------------------------------------------------

DEP_PAGE = (
    "Duke Energy Progress, LLC NC Third Revised Leaf No. 605\n"
    "(North Carolina Only) Superseding NC Second Revised Leaf No 605\n"
    "RIDER CPRE\n"
    "Effective for service rendered on and after December 1, 2025\n"
    "NCUC Docket No. E-2 Sub 1355, Order dated November 13, 2025"
)


def _by_kind(text):
    out = {}
    for c in extract_page_clues(text):
        out.setdefault(c.kind, []).append(c)
    return out


def test_every_clue_is_an_exact_slice_of_the_page():
    for text in (DEP_PAGE, "North Carolina Fifth Revised LeafNo. 99\nSuperseding North Carolina Fourth Revised LeafNo. 99"):
        for clue in extract_page_clues(text):
            assert text[clue.char_start:clue.char_end] == clue.printed_text


def test_dep_footer_clues():
    k = _by_kind(DEP_PAGE)
    assert [c.normalized_value for c in k["revision"]] == ["NC:leaf-605:rev-3"]
    assert [c.normalized_value for c in k["supersedes"]] == ["NC:leaf-605:rev-2"]
    assert [c.normalized_value for c in k["docket"]] == ["E-2 Sub 1355"]
    assert [c.normalized_value for c in k["order_date"]] == ["2025-11-13"]
    assert [c.normalized_value for c in k["effective_date"]] == ["2025-12-01"]
    assert k["docket"][0].role == "footer"
    assert [c.normalized_value for c in k["utility"]] == ["DEP"]


def test_dec_north_carolina_supersedes_is_read():
    text = "Duke Energy Carolinas, LLC\nNorth Carolina Fifth Revised LeafNo. 99\nSuperseding North Carolina Fourth Revised LeafNo. 99"
    k = _by_kind(text)
    assert k["revision"][0].normalized_value == "NC:leaf-99:rev-5"
    assert k["supersedes"][0].normalized_value == "NC:leaf-99:rev-4"
    # "LeafNo." is OCR/glue damage: the reading is labelled, not presented as exact.
    assert k["supersedes"][0].normalization == "ocr_repaired"
    assert k["utility"][0].normalized_value == "DEC"
    # The old extractor cannot read this form at all (the D1 measurement gap).
    assert extract_supersedes(text) != "North Carolina Fourth Revised LeafNo. 99"


@pytest.mark.parametrize(
    "text, kind",
    [
        ("NC First RevisedOriginal Leaf No. 600", "revision"),
        ("NC Original First Revised Leaf No. 660", "revision"),
        ("NCUC Docket No. E-7, Sub 12431214, Order dated July 13, 2021", "docket"),
        ("Effective for service rendered on and after January 1, 2019November 16, 2019", "effective_date"),
        ("NCUC Docket No. E-2, Sub 1, Order dated September 30, 2024December 10, 2024", "order_date"),
        ("Superseding NC ThirdFourth Revised Leaf No. 600", "supersedes"),
    ],
)
def test_redline_glue_is_kept_ambiguous_not_guessed(text, kind):
    clues = _by_kind(text)[kind]
    assert clues and all(c.normalization == "redline_ambiguous" and c.normalized_value is None for c in clues)


def test_blank_order_date_is_recorded_as_blank():
    k = _by_kind("NCUC Docket No. E-7, Sub 1276, Order dated ____")
    assert k["order_date"][0].normalization == "blank"
    assert k["approval_reference"][0].normalization == "blank"
    assert k["docket"][0].normalized_value == "E-7 Sub 1276"


def test_docket_lists_give_one_clue_per_docket():
    k = _by_kind("NCUC Docket No.E-7 Sub 1058 and M-100 Sub 138, Order dated June 17, 2014")
    assert [c.normalized_value for c in k["docket"]] == ["E-7 Sub 1058", "M-100 Sub 138"]
    k = _by_kind("NCUC Docket No. E-2, Subs 1356 and 1358, Order dated November 13, 2025")
    assert [c.normalized_value for c in k["docket"]] == ["E-2 Sub 1356", "E-2 Sub 1358"]


def test_effective_range_and_proposed_header():
    k = _by_kind("Effective for service rendered from October 1, 2024 through September 30, 2025")
    assert k["effective_date"][0].normalized_value == "2024-10-01/2025-09-30"
    k = _by_kind("North Carolina (Proposed) Original Leaf No. 137")
    assert k["revision"][0].normalized["proposed_marker"] is True
    assert k["revision"][0].normalized_value == "NC:leaf-137:rev-0"


def test_schedule_supersedes_and_own_schedule_are_distinct():
    k = _by_kind("Supersedes Schedule RES-56\nSCHEDULE RES-57\nResidential Service")
    assert [c.normalized_value for c in k["supersedes"]] == ["schedule:RES-56"]
    assert [c.normalized_value for c in k["schedule_code"]] == ["schedule:RES-57"]
    k = _by_kind("(North Carolina Only) Superseding NC Rider GP-3")
    assert k["supersedes"][0].normalized_value == "schedule:GP-3"


def test_ordinal_must_not_match_inside_words():
    assert "revision" not in _by_kind("This has been in place since First Revised Leaf No. 5")


def test_new_patterns_cover_the_existing_extractors():
    """Every docket/supersedes the existing heuristics find is also a ledger clue."""
    for text in (DEP_PAGE, "Supersedes Schedule RES-77", "Superseding NC Original Leaf No. 500",
                 "NCUC Docket No. E-2, Sub 1300, Order dated August 18, 2023"):
        k = _by_kind(text)
        docket, order = extract_docket_footer(text)
        if docket:
            assert docket.replace(",", "") in {c.normalized_value for c in k["docket"]}
        if order:
            assert any(c.printed_text == order for c in k["order_date"])
        sup = extract_supersedes(text)
        if sup:
            assert any(sup in c.printed_text for c in k["supersedes"])


def test_read_date_flags_glue_and_repairs():
    assert read_date("August 18, 2023").iso == "2023-08-18"
    assert read_date("June 28,2011").normalization == "ocr_repaired"
    assert read_date("JanuaryJuly 15, 2024").normalization == "redline_ambiguous"
    assert read_date("October 1, 20245").iso is None


def test_leaf_indexing_footer_is_not_an_index_page():
    assert clue_ledger.is_index_page("Index of Tariffs - Rate Year 2\nSchedules, Riders, and Other Tariffs")
    assert not clue_ledger.is_index_page(
        "NC Original Leaf No. 642\nLeaf Indexing is effective as of October 1, 2023, in Compliance with NCUC Docket No. E-2. Sub 1300")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_v3_installs_and_drop_protects_search_history(tmp_path: Path):
    conn = connect(tmp_path / "s.db")
    ensure_provenance_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"regulatory_clues", "clue_associations", "lineage_links", "lineage_gaps_typed",
            "gap_candidates", "search_attempts", "clue_scan_pages"} <= names
    assert conn.execute("SELECT value FROM provenance_schema_meta WHERE key='schema_version'").fetchone()[0] == str(
        PROVENANCE_SCHEMA_VERSION)
    ensure_provenance_schema(conn)  # idempotent
    SearchLedger(conn, created_by="t").attempt("local", {"q": 1}, caller="t").__enter__()
    with pytest.raises(ProvenanceSchemaInUseError):
        drop_provenance_schema(conn)
    drop_provenance_schema(conn, force=True)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "regulatory_clues" not in names and "search_attempts" not in names


# ---------------------------------------------------------------------------
# Synthetic archive
# ---------------------------------------------------------------------------


def _insert(conn: sqlite3.Connection, table: str, **values) -> int:
    for _, name, typ, notnull, default, pk in conn.execute(f"PRAGMA table_info({table})"):
        if notnull and default is None and not pk and name not in values:
            values[name] = 0 if "INT" in (typ or "").upper() or "REAL" in (typ or "").upper() else "x"
    cols = ", ".join(values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(values))})", list(values.values()))
    return cur.lastrowid


def _pdf(path: Path, pages: list[str]) -> tuple[str, str]:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=9)
    doc.save(path)
    doc.close()
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()


def _artifacts(conn, path: Path, pages: list[str], file_hash: str, version="page_miner_v6", layer="native_text"):
    for i, text in enumerate(pages, start=1):
        _insert(conn, "ncuc_page_artifacts", source_pdf=str(path), file_hash=file_hash, artifact_version=version,
                page_number=i, text_length=len(text), text_content=text,
                metadata_json=json.dumps({"artifact_source": layer}), created_at="t", updated_at="t")


def _sheet(ordinal_word, supersedes=None, effective="October 1, 2024", utility="Duke Energy Progress, LLC",
           leaf="600"):
    lines = [f"{utility} NC {ordinal_word} Leaf No. {leaf}"]
    if supersedes:
        lines.append(f"(North Carolina Only) Superseding NC {supersedes} Leaf No. {leaf}")
    lines += ["SUMMARY OF RIDER ADJUSTMENTS",
              f"Effective for service rendered on and after {effective}",
              "NCUC Docket No. E-2, Sub 1300, Order dated August 18, 2023"]
    return "\n".join(lines)


@pytest.fixture()
def archive(tmp_path: Path):
    db = tmp_path / "scratch.db"
    conn = connect(db)
    ensure_provenance_schema(conn)
    family = "nc-progress-leaf-600"
    _insert(conn, "tariff_families", family_key=family, tariff_identifier="leaf-600", schedule_code="SUMMARY_OF_RIDERS",
            state="NC", company="progress", family_type="rider", title="Leaf 600", created_at="t", updated_at="t")

    # Bundle: index page, rev 2 (supersedes rev 1), another sheet with its own supersedes.
    bundle_pages = [
        "Index of Tariffs - Rate Year 2\nSchedules, Riders, and Other Tariffs\nLeaf 600 Summary of Rider Adjustments",
        _sheet("Second Revised", "First Revised", effective="October 1, 2024"),
        "Duke Energy Progress, LLC NC Fourth Revised Leaf No. 601\nSuperseding NC Third Revised Leaf No. 601\n"
        "Supersedes Schedule SGS-56",
    ]
    bundle = tmp_path / "bundle.pdf"
    sha, md5 = _pdf(bundle, bundle_pages)
    _artifacts(conn, bundle, bundle_pages, md5)
    _artifacts(conn, bundle, ["CHUNK RELATIVE GARBAGE NC Ninth Revised Leaf No. 600"] * 3, sha,
               version="docling_page_miner_v1")
    _artifacts(conn, bundle, ["STALE NC Ninth Revised Leaf No. 600"] * 3, "0" * 64, version="page_miner_v7")

    # Earlier clean sheet (rev 1) and a later sheet (rev 4, supersedes missing rev 3).
    rev1 = tmp_path / "rev1.pdf"
    rev1_pages = [_sheet("First Revised", "Original", effective="October 1, 2023")]
    sha1, _ = _pdf(rev1, rev1_pages)
    _artifacts(conn, rev1, rev1_pages, sha1)
    rev4 = tmp_path / "rev4.pdf"
    rev4_pages = [_sheet("Fourth Revised", "Third Revised", effective="October 1, 2026")]
    sha4, _ = _pdf(rev4, rev4_pages)
    _artifacts(conn, rev4, rev4_pages, sha4)

    # Unbound archive PDF that prints the missing rev 3, and a DEC page printing the same identity.
    loose = tmp_path / "loose.pdf"
    loose_pages = ["Cover letter", _sheet("Third Revised", "Second Revised", effective="October 1, 2025")]
    shal, _ = _pdf(loose, loose_pages)
    _artifacts(conn, loose, loose_pages, shal)
    other = tmp_path / "other_utility.pdf"
    other_pages = [_sheet("Third Revised", "Second Revised", effective="October 1, 2025",
                          utility="Duke Energy Carolinas, LLC")]
    shao, _ = _pdf(other, other_pages)
    _artifacts(conn, other, other_pages, shao)

    def hd(path, start, end, sha_):
        return _insert(conn, "historical_documents", family_key=family, local_path=str(path), start_page=start,
                       end_page=end, content_hash=sha_, title="t", state="NC", company="progress",
                       category="rider", kind="pdf", canonical_url=f"{path}#{start}", archived_url=f"{path}#{start}",
                       snapshot_timestamp="t", retrieved_at="t", status="approved")

    def version(hd_id, eff):
        return _insert(conn, "tariff_versions", family_key=family, historical_document_id=hd_id,
                       effective_start=eff, source_type="t", created_at="t", status="approved")

    ids = SimpleNamespace()
    ids.hd_index = hd(bundle, 1, 1, sha)          # bound to the index page (the D1 defect)
    ids.v_index = version(ids.hd_index, "2024-10-01")
    ids.hd_rev2 = hd(bundle, 2, 3, sha)           # span includes the next sheet (leaf 601)
    ids.v_rev2 = version(ids.hd_rev2, "2024-10-01")
    ids.v_rev1 = version(hd(rev1, 1, 1, sha1), "2023-10-01")
    ids.v_rev4 = version(hd(rev4, 1, 1, sha4), "2026-10-01")
    ids.paths = SimpleNamespace(bundle=bundle, loose=loose, other=other)
    conn.commit()
    yield conn, ids
    conn.close()


def test_ledger_reads_only_verified_native_artifacts_and_attributes_by_span(archive):
    conn, ids = archive
    report = clue_ledger.build_clue_ledger(conn)
    assert report["stats"]["artifact_rows_rejected_non_native_artifact"] == 3
    assert report["stats"]["artifact_rows_rejected_hash_mismatch"] == 3
    printed = [r[0] for r in conn.execute("SELECT printed_text FROM regulatory_clues")]
    assert not any("GARBAGE" in p or "STALE" in p or "Ninth" in p for p in printed)
    grounding = clue_ledger.verify_source_grounding(conn)
    assert grounding["ok"] and grounding["checked"] > 0
    # Index page clues are tagged and attributed only to the document bound to page 1.
    index_roles = {r[0] for r in conn.execute(
        "SELECT c.role FROM regulatory_clues c JOIN clue_associations a ON a.clue_id = c.id "
        "WHERE a.historical_document_id = ?", (ids.hd_index,))}
    assert index_roles == {"index"} or index_roles == set()
    pages_rev2 = {r[0] for r in conn.execute(
        "SELECT DISTINCT c.page FROM regulatory_clues c JOIN clue_associations a ON a.clue_id = c.id "
        "WHERE a.historical_document_id = ?", (ids.hd_rev2,))}
    assert pages_rev2 == {2, 3}
    located = conn.execute("SELECT COUNT(*) FROM regulatory_clues WHERE bbox_status = 'located'").fetchone()[0]
    assert located > 0
    # Idempotent.
    before = conn.execute("SELECT COUNT(*) FROM regulatory_clues").fetchone()[0]
    clue_ledger.build_clue_ledger(conn)
    assert conn.execute("SELECT COUNT(*) FROM regulatory_clues").fetchone()[0] == before


def test_links_gaps_and_review_preservation(archive):
    conn, ids = archive
    clue_ledger.build_clue_ledger(conn)
    report = clue_lineage.rebuild_lineage(conn)
    links = {r["from_version_id"]: dict(r) for r in conn.execute("SELECT * FROM lineage_links")}

    verified = links[ids.v_rev2]
    assert verified["status"] == "verified" and verified["to_version_id"] == ids.v_rev1
    checks = json.loads(verified["rule_checks_json"])
    assert checks["evaluated"][0]["date_order"] is True
    # The leaf-601 sheet and "Supersedes Schedule SGS-56" in the same span are not attributed to leaf 600.
    assert all(json.loads(r[0])["token"][0] != "schedule" for r in conn.execute(
        "SELECT expected_json FROM lineage_links WHERE from_version_id = ?", (ids.v_rev2,)))
    assert links[ids.v_rev4]["status"] == "explicit_unresolved"
    # rev1 prints "Superseding NC Original": no rev 0 in the family.
    assert links[ids.v_rev1]["status"] == "explicit_unresolved"
    assert report["links"]["links_by_status"].get("verified") == 1

    gaps = {(r["gap_type"], r["anchor_version_id"]): dict(r) for r in conn.execute(
        "SELECT * FROM lineage_gaps_typed WHERE active = 1")}
    pred = gaps[("missing_predecessor_revision", ids.v_rev4)]
    assert pred["expected_revision"] == "NC Third Revised Leaf No. 600"
    assert pred["evidence_basis"] == "explicit"
    assert pred["window_start"] == "2024-10-01" and pred["window_end"] == "2026-10-01"
    assert json.loads(pred["supporting_clue_ids_json"])
    index_gap = gaps[("unresolved_version_identity", ids.v_index)]
    assert "index page" in index_gap["status_reason"]

    # A human review survives a rebuild; derived status is recomputed.
    conn.execute("UPDATE lineage_links SET human_review = 'confirmed', reviewed_by = 'tester' WHERE from_version_id = ?",
                 (ids.v_rev2,))
    conn.commit()
    clue_lineage.rebuild_lineage(conn)
    row = conn.execute("SELECT human_review, status FROM lineage_links WHERE from_version_id = ? "
                       "AND status = 'verified'", (ids.v_rev2,)).fetchone()
    assert tuple(row) == ("confirmed", "verified")


def test_chronological_adjacency_alone_never_links(archive):
    conn, ids = archive
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    # rev4 follows rev2 in date order, but prints "Superseding Third": no link to rev2 may exist.
    assert conn.execute("SELECT COUNT(*) FROM lineage_links WHERE from_version_id = ? AND to_version_id = ?",
                        (ids.v_rev4, ids.v_rev2)).fetchone()[0] == 0


def test_date_reversal_is_a_conflict(archive):
    conn, ids = archive
    conn.execute("UPDATE tariff_versions SET effective_start = '2025-06-01' WHERE id = ?", (ids.v_rev1,))
    conn.commit()
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    status = conn.execute("SELECT status FROM lineage_links WHERE from_version_id = ?", (ids.v_rev2,)).fetchone()[0]
    assert status == "conflict"


def test_local_resolution_finds_unbound_sheet_and_dedupes_searches(archive):
    conn, ids = archive
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    first = local_gap_resolution.resolve_gaps_locally(conn, created_by="test")
    assert first["stats"]["candidates_dropped_other_utility"] >= 1
    gap = conn.execute("SELECT * FROM lineage_gaps_typed WHERE gap_type = 'missing_predecessor_revision' "
                       "AND anchor_version_id = ?", (ids.v_rev4,)).fetchone()
    cands = conn.execute("SELECT * FROM gap_candidates WHERE gap_id = ?", (gap["id"],)).fetchall()
    assert len(cands) == 1
    c = cands[0]
    assert Path(c["source_pdf"]) == ids.paths.loose and (c["start_page"], c["end_page"]) == (2, 2)
    assert c["match_strength"] == "full" and c["verification_status"] == "unreviewed"
    assert json.loads(c["matched_clue_ids_json"])
    # A candidate never resolves a gap.
    assert gap["status"] == "verification_required"
    assert conn.execute("SELECT COUNT(*) FROM lineage_gaps_typed WHERE status IN ('verified', 'resolved')").fetchone()[0] == 0
    # Nothing was bound: no new historical document points at the loose PDF.
    assert conn.execute("SELECT COUNT(*) FROM historical_documents WHERE local_path = ?",
                        (str(ids.paths.loose),)).fetchone()[0] == 0
    second = local_gap_resolution.resolve_gaps_locally(conn, created_by="test")
    assert second["stats"]["repeated_searches_avoided"] == first["stats"]["local_searches_run"]
    assert second["stats"].get("local_searches_run", 0) == 0
    forced = local_gap_resolution.resolve_gaps_locally(conn, created_by="test", force=True)
    assert forced["stats"]["local_searches_run"] == first["stats"]["local_searches_run"]


# ---------------------------------------------------------------------------
# Search ledger
# ---------------------------------------------------------------------------


def test_canonical_params_ignore_order_case_and_padding():
    a = canonical_params("portal_params", {"docket_number": "E-2 Sub 1300", "filing_types": ["TARIFF", "ORDER"]})
    b = canonical_params("portal_params", {"filing_types": ["order", "tariff"], "docket_number": " e-2  sub 1300 "})
    c = canonical_params("portal_params", {"docket_number": "E-2 Sub 1301", "filing_types": ["TARIFF", "ORDER"]})
    assert a[1] == b[1] != c[1]


def test_search_ledger_outcomes_and_retry_policy(tmp_path: Path):
    conn = connect(tmp_path / "l.db")
    ensure_provenance_schema(conn)
    ledger = SearchLedger(conn, created_by="t")
    params = {"q": "leaf 600"}
    with pytest.raises(RuntimeError):
        with ledger.attempt("zoom", params, caller="t"):
            raise RuntimeError("Cloudflare blocked DocumentsParameterSearch (title='Just a moment')")
    with pytest.raises(ValueError):
        with ledger.attempt("zoom", params, caller="t"):
            raise ValueError("timeout")
    with ledger.attempt("zoom", params, caller="t") as att:
        att.finish(list(range(5)), cap=5)
    # blocked / error / incomplete never count as "already searched".
    assert ledger.prior_completed("zoom", params) is None
    with ledger.attempt("zoom", params, caller="t") as att:
        att.finish([])
    assert ledger.prior_completed("zoom", params)["outcome"] == "no_results"
    outcomes = [r[0] for r in conn.execute("SELECT outcome FROM search_attempts ORDER BY id")]
    assert outcomes == ["blocked", "error", "incomplete", "no_results"]
    assert conn.execute("SELECT COUNT(DISTINCT params_hash) FROM search_attempts").fetchone()[0] == 1


def test_missing_clean_doc_search_records_one_attempt_per_query(monkeypatch, tmp_path: Path):
    from duke_rates.historical.ncuc import missing_clean_doc_search as mod
    from tests.test_missing_clean_doc_search import FakeRepository

    monkeypatch.setattr(mod, "build_nc_missing_clean_doc_audit", lambda database_path=None: {
        "generated_at": "x",
        "rows": [{"family_key": "nc-progress-leaf-500", "utility": "DEP", "title": "Schedule RES",
                  "family_type": "rate_schedule", "schedule_code": "RES", "missing_kind": "m",
                  "priority_band": "high", "priority_score": 88, "suggested_dockets": '["E-2, Sub 976"]',
                  "suggested_query_terms": '["RES-12"]', "suggested_portal_filing_types": '["TARIFF"]',
                  "suggested_date_after": "2010-10-01", "suggested_date_before": "2010-12-15"}]})
    monkeypatch.setattr(mod, "create_authenticated_context", lambda settings: ("pw", "ctx", object()))
    monkeypatch.setattr(mod, "close_authenticated_context", lambda pw, ctx: None)
    calls = {"portal": 0, "zoom": 0}

    class FakeSearcher:
        def __init__(self, settings):
            pass

        def search(self, page, **kwargs):
            calls["portal"] += 1
            if calls["portal"] == 1:
                raise RuntimeError("Cloudflare blocked DocumentsParameterSearch")
            return []

        def enrich_with_document_details(self, page, results, *, delay_seconds=0.5):
            return results

    class FakeDiscovery:
        def __init__(self, settings):
            pass

        def search_public_site(self, query, max_results=20):
            calls["zoom"] += 1
            return iter(())

        def close(self):
            return None

    monkeypatch.setattr(mod, "DocumentParamSearcher", FakeSearcher)
    monkeypatch.setattr(mod, "NcucDiscoveryService", FakeDiscovery)
    settings = SimpleNamespace(ncid_username="u", ncid_password="p")
    conn = connect(tmp_path / "a8.db")
    ensure_provenance_schema(conn)
    ledger = SearchLedger(conn, created_by="t")
    with pytest.raises(RuntimeError):  # the searcher's own failure still propagates unchanged
        mod.search_nc_missing_clean_documents(settings, FakeRepository(), persist=False, save_manifest=False,
                                              search_ledger=ledger)
    assert [tuple(r) for r in conn.execute("SELECT channel, outcome FROM search_attempts")] == [("portal_params", "blocked")]
    report = mod.search_nc_missing_clean_documents(settings, FakeRepository(), persist=False, save_manifest=False,
                                                   search_ledger=ledger)
    rows = conn.execute("SELECT channel, outcome, params_hash FROM search_attempts WHERE id > 1").fetchall()
    assert len(rows) == calls["portal"] - 1 + calls["zoom"]
    assert all(r[2] and len(r[2]) == 64 for r in rows)
    assert {r[1] for r in rows} == {"no_results"}
    assert report["lead_count"] == 1


@pytest.mark.parametrize(
    "text, kind, expected",
    [
        # Found by the hand-verified benchmark (bench/clues/sample_50.yaml).
        ("NCUC Docket No. E-7, Subs 487, 828, 1026, 1146, 1214, and 1276, Orders dated June 12, 2024 and June 13, 2024",
         "docket", ["E-7 Sub 487", "E-7 Sub 828", "E-7 Sub 1026", "E-7 Sub 1146", "E-7 Sub 1214", "E-7 Sub 1276"]),
        ("NCUC Docket No. E-7, Subs 487, 828, and 1276, Orders dated June 12, 2024 and June 13, 2024",
         "order_date", ["2024-06-12", "2024-06-13"]),
        ("North Carolina Fifth (Proposed) Revised Leaf No. 63", "revision", ["NC:leaf-63:rev-5"]),
        ("Superseding North Carolinas Second Revised Leaf No. 62", "supersedes", ["NC:leaf-62:rev-2"]),
        ("x\nEffective on or after November 25, 2020", "effective_date", ["2020-11-25"]),
    ],
)
def test_benchmark_found_formats(text, kind, expected):
    assert [c.normalized_value for c in _by_kind(text)[kind]] == expected


@pytest.mark.parametrize("text", ["NCUC Docket No. E-7, Sub 1026, Order dated\nPage 1 of 5",
                                  "NCUC Docket No. E-7, Sub 1305, Order dated __"])
def test_order_dated_without_a_date_is_blank(text):
    k = _by_kind(text)
    assert [c.normalization for c in k["order_date"]] == ["blank"]
    assert k["approval_reference"][0].normalized_value.endswith("@blank")


def test_effective_sentence_in_body_is_not_a_footer_clue():
    text = "\n".join(["NC Eleventh Revised Leaf No. 133", "RIDER STS", "APPLICABILITY"] + ["body"] * 12 + [
        "Effective for service rendered on and after January 1, 2025, the incremental rate applies."] + ["body"] * 12 + [
        "NC Eleventh Revised Leaf No. 133", "Effective for service on and after July 1, 2025",
        "NCUC Docket No. E-7, Sub 1243, Order dated July 13, 2021", "Page 1 of 1"])
    roles = {c.normalized_value: c.role for c in _by_kind(text)["effective_date"]}
    assert roles == {"2025-01-01": "body", "2025-07-01": "footer"}


def test_other_utility_pages_in_a_joint_span_do_not_set_identity(archive):
    conn, ids = archive
    # Re-bind the rev-2 document to a span whose page names the other utility only.
    other = ids.paths.other
    conn.execute("UPDATE historical_documents SET local_path = ?, start_page = 1, end_page = 1, content_hash = ? "
                 "WHERE id = ?", (str(other), hashlib.sha256(other.read_bytes()).hexdigest(), ids.hd_rev2))
    conn.commit()
    clue_ledger.build_clue_ledger(conn)
    versions = clue_lineage.load_version_evidence(conn)
    assert versions[ids.v_rev2].identity_status == "none"


def test_redline_fingerprint_blocks_a_full_candidate(archive):
    conn, ids = archive
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    _insert(conn, "document_fingerprints", source_pdf=str(ids.paths.loose), page_start=1, page_end=2,
            is_redline_candidate=1, redline_confidence=0.9, red_is_index_only=0, created_at="t")
    conn.commit()
    local_gap_resolution.resolve_gaps_locally(conn, created_by="test")
    row = conn.execute("SELECT match_strength, match_json FROM gap_candidates WHERE source_pdf = ?",
                       (str(ids.paths.loose),)).fetchone()
    assert row["match_strength"] == "partial"
    assert json.loads(row["match_json"])["redline_fingerprint"]["confidence"] == 0.9
