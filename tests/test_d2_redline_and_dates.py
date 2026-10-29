"""Phase 2E D2: redline page evidence, struck identity text, effective-date reconciliation.

Redline fixtures are single pages cut unchanged from the D1 failure PDFs
(``tests/fixtures/d2_redline/make_fixtures.py``); verdicts were checked
against the rendered page images.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import fitz
import pytest

from duke_rates.historical.ncuc import clue_ledger, clue_lineage, local_gap_resolution
from duke_rates.historical.ncuc.effective_date_reconciliation import (
    assess,
    build_effective_date_assessments,
    evaluation_window,
)
from duke_rates.parse.redline_detector import PageRedlineReader, detect_redline, summarize_page_evidence
from test_d1_clue_ledger import _sheet, archive  # noqa: F401  (fixture reuse)

FIX = Path(__file__).resolve().parent / "fixtures" / "d2_redline"
HD2275 = str(FIX / "hd2275_leaf600_redline_p100.pdf")
HD7265 = str(FIX / "hd7265_leaf600_clean_p96.pdf")
LEAF660 = str(FIX / "leaf660_redline_p221.pdf")
LEAF661 = str(FIX / "leaf661_redline_p226.pdf")


@pytest.fixture()
def reader():
    r = PageRedlineReader()
    yield r
    r.close()


# ---------------------------------------------------------------------------
# R1 / R2: the D1 failures are flagged, the clean control is not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [HD2275, LEAF660, LEAF661])
def test_d1_redline_failures_are_flagged(reader, path):
    ev = reader.page(path, 1)
    assert ev.verdict == "redline"
    assert ev.struck_spans > 0 and ev.inserted_spans > 0
    assert summarize_page_evidence([ev])["verdict"] == "redline"


def test_clean_rebind_target_is_clean(reader):
    ev = reader.page(HD7265, 1)
    assert ev.verdict == "clean"
    assert (ev.colored_spans, ev.struck_spans, ev.inserted_spans) == (0, 0, 0)


def test_hd2275_struck_ral2_value_is_struck(reader):
    ev = reader.page(HD2275, 1)
    assert "-0.009" in ev.struck_samples and "RAL-2 Rider" in ev.struck_samples
    assert reader.text_struck(HD2275, 1, "RAL-2 Rider") is True
    # The text layer fuses old and new ("First RevisedOriginal"); the bars still separate them.
    assert "First RevisedOriginal" in ev.struck_samples


def test_struck_identity_vs_inserted_reference(reader):
    # D1 #19: the footer and title "RIDER MROP-16A" are struck; the header reference is inserted.
    assert reader.text_struck(LEAF661, 1, "RIDER MROP-16A") is True
    assert reader.text_struck(LEAF661, 1, "Superseding NC Rider MROP-16A") is False
    assert reader.text_struck(LEAF661, 1, "NC Original Leaf No. 661") is False
    assert reader.text_struck(HD7265, 1, "First Revised Leaf No. 600") is False
    assert reader.text_struck(HD7265, 1, "text that is not on the page") is None


def test_old_detector_unchanged_on_bound_slice_but_page_reader_sees_run():
    # The stored fingerprint for hd 2275 came from its bound slice (the index page);
    # the page reader evaluates whatever pages a candidate or rebind names.
    assert detect_redline(HD7265).is_redline is False


# ---------------------------------------------------------------------------
# Resolver integration: a redline page run is never "full"
# ---------------------------------------------------------------------------


def _redraw_with_markup(path: Path, page_texts: list[str]) -> str:
    """Rewrite ``path``: same text, plus a red inserted (underlined) line on the last page."""
    doc = fitz.open()
    for i, text in enumerate(page_texts):
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=9)
        if i == len(page_texts) - 1:
            page.insert_text((72, 300), "Rider XYZ-2 0.123", fontsize=9, color=(1, 0, 0))
            page.draw_rect(fitz.Rect(72, 301.5, 160, 302.3), color=None, fill=(1, 0, 0))   # underline: inserted
            page.insert_text((72, 320), "Rider XYZ-1 0.100", fontsize=9, color=(1, 0, 0))
            page.draw_rect(fitz.Rect(72, 316.6, 160, 317.4), color=None, fill=(1, 0, 0))   # strike: deleted
    doc.save(path)
    doc.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_resolver_withholds_full_match_on_redline_pages(archive):  # noqa: F811
    conn, ids = archive
    loose_pages = ["Cover letter", _sheet("Third Revised", "Second Revised", effective="October 1, 2025")]
    sha = _redraw_with_markup(ids.paths.loose, loose_pages)
    conn.execute("UPDATE ncuc_page_artifacts SET file_hash = ? WHERE source_pdf = ?", (sha, str(ids.paths.loose)))
    conn.commit()
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    report = local_gap_resolution.resolve_gaps_locally(conn, created_by="test")
    gap = conn.execute("SELECT * FROM lineage_gaps_typed WHERE gap_type = 'missing_predecessor_revision' "
                       "AND anchor_version_id = ?", (ids.v_rev4,)).fetchone()
    cand = conn.execute("SELECT * FROM gap_candidates WHERE gap_id = ?", (gap["id"],)).fetchone()
    match = json.loads(cand["match_json"])
    assert match["page_redline"]["verdict"] == "redline" and match["page_redline"]["redline_pages"] == [2]
    assert match["utility_match"] is True and match["effective_in_window"] is True
    assert cand["match_strength"] == "partial"           # identity right, but not a clean sheet
    assert gap["status"] == "candidate_found"
    assert report["stats"]["candidates_page_redline"] == 1


# ---------------------------------------------------------------------------
# Effective-date reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stored,observed,status", [
    ("2025-01-01", ["2025-01-01"], "agree"),
    ("2024-01-24", ["2025-01-01"], "conflict"),
    ("2024-01-24", ["2025-01-01", "2024-01-24"], "ambiguous"),
    ("2024-01-24", [], "stored_only"),
    (None, ["2025-01-01"], "observed_only"),
    (None, [], "none"),
])
def test_assess(stored, observed, status):
    assert assess(stored, observed) == status


def test_conflicting_stored_date_widens_gap_window_but_is_not_overwritten(archive):  # noqa: F811
    conn, ids = archive
    # The rev-4 sheet prints October 1, 2026; store a wrong (earlier) date on its version,
    # earlier than the unbound rev-3 sheet (October 1, 2025).
    conn.execute("UPDATE tariff_versions SET effective_start = '2025-03-01' WHERE id = ?", (ids.v_rev4,))
    conn.commit()
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    summary = build_effective_date_assessments(conn)
    assert summary["by_status"].get("conflict", 0) >= 1
    a = evaluation_window(conn, ids.v_rev4)
    assert a["status"] == "conflict" and a["stored"] == "2025-03-01" and a["window"] == ["2025-03-01", "2026-10-01"]
    # The stored metadata is untouched.
    assert conn.execute("SELECT effective_start FROM tariff_versions WHERE id = ?",
                        (ids.v_rev4,)).fetchone()[0] == "2025-03-01"
    local_gap_resolution.resolve_gaps_locally(conn, created_by="test")
    gap = conn.execute("SELECT * FROM lineage_gaps_typed WHERE gap_type = 'missing_predecessor_revision' "
                       "AND anchor_version_id = ?", (ids.v_rev4,)).fetchone()
    assert gap["window_end"] == "2025-03-01"   # the gap row keeps the stored window
    match = json.loads(conn.execute("SELECT match_json FROM gap_candidates WHERE gap_id = ?",
                                    (gap["id"],)).fetchone()[0])
    assert match["stored_window"][1] == "2025-03-01" and match["window"][1] == "2026-10-01"
    assert match["window_widened_by"][0]["status"] == "conflict"
    assert match["effective_in_window"] is True    # the 2025-10-01 sheet is no longer ruled out


def test_reviewed_assessment_survives_rebuild(archive):  # noqa: F811
    conn, ids = archive
    clue_ledger.build_clue_ledger(conn)
    build_effective_date_assessments(conn)
    conn.execute("UPDATE effective_date_assessments SET reviewed_value = '2026-10-01', reviewed_by = 'r' "
                 "WHERE tariff_version_id = ?", (ids.v_rev4,))
    conn.commit()
    build_effective_date_assessments(conn)
    row = conn.execute("SELECT status, reviewed_value FROM effective_date_assessments WHERE tariff_version_id = ?",
                       (ids.v_rev4,)).fetchone()
    assert tuple(row) == ("reviewed", "2026-10-01")
