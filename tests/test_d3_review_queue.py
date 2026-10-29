"""Phase 2E D3: effective-date conflict classification and the evidence review queue."""

from __future__ import annotations

import json

import pytest

from duke_rates.historical.ncuc import evidence_review as er
from duke_rates.historical.ncuc.effective_date_reconciliation import classify_conflicts
from test_d1_clue_ledger import _insert, archive  # noqa: F401  (fixture reuse)

FAMILY = "nc-progress-leaf-600"


def _conflict(conn, vid, stored, printed, page=1, sha="s"):
    _insert(conn, "effective_date_assessments", tariff_version_id=vid, family_key=FAMILY, stored_value=stored,
            observed_json=json.dumps([{"date": printed, "clue_id": 1, "file_sha256": sha, "page": page,
                                       "role": "footer", "normalization": "exact"}]),
            inferred_json="[]", status="conflict", evaluation_window_json="[]", rule="r", builder_version="b",
            created_at="t", updated_at="t")


def _set(conn, vid, stored, doc_date=None):
    conn.execute("UPDATE tariff_versions SET effective_start = ? WHERE id = ?", (stored, vid))
    conn.execute("UPDATE historical_documents SET effective_start = ? WHERE id = (SELECT historical_document_id "
                 "FROM tariff_versions WHERE id = ?)", (doc_date, vid))


@pytest.mark.parametrize("stored,doc_date,printed,cause,correctable", [
    ("2026-10-13", None, "2009-10-13", "stored_year_digit_error", True),
    ("2023-09-24", "2023-09-22", "2023-10-01", "stored_filing_or_order_date", True),   # D1: 6548's filing date
    ("2022-10-01", "2023-10-01", "2023-10-01", "version_date_disagrees_with_its_document", True),
    ("2022-10-01", "2022-10-01", "2023-10-01", "adjacent_rate_year", False),
    ("2019-10-01", None, "2023-10-01", "bound_document_from_other_period", False),
])
def test_conflict_causes(archive, stored, doc_date, printed, cause, correctable):  # noqa: F811
    conn, ids = archive
    _set(conn, ids.v_rev1, stored, doc_date)
    _conflict(conn, ids.v_rev1, stored, printed)
    conn.commit()
    (c,) = classify_conflicts(conn)
    assert c["cause"] == cause
    assert (c["proposed_correction"] is not None) == correctable
    if correctable:
        assert c["proposed_correction"]["to"] == printed and c["proposed_correction"]["from"] == stored
    # Nothing is written to tariff_versions.
    assert conn.execute("SELECT effective_start FROM tariff_versions WHERE id = ?", (ids.v_rev1,)).fetchone()[0] == stored


def test_sibling_holding_the_printed_date_is_never_a_date_correction(archive):  # noqa: F811
    conn, ids = archive
    _set(conn, ids.v_rev1, "2023-09-24", "2023-09-24")
    conn.execute("UPDATE tariff_versions SET effective_start = '2023-10-01' WHERE id = ?", (ids.v_rev4,))
    _conflict(conn, ids.v_rev1, "2023-09-24", "2023-10-01")
    conn.commit()
    (c,) = classify_conflicts(conn)
    assert c["cause"] == "observed_period_held_by_sibling" and c["proposed_correction"] is None
    assert [s["id"] for s in c["siblings_at_printed_date"]] == [ids.v_rev4]


def test_proposed_sheet_date_is_not_corrected(archive):  # noqa: F811
    conn, ids = archive
    _set(conn, ids.v_rev1, "2026-10-13")
    _conflict(conn, ids.v_rev1, "2026-10-13", "2009-10-13", sha="p")
    _insert(conn, "regulatory_clues", clue_uid="c1", source_pdf="x", file_sha256="p", page=1, clue_kind="order_date",
            role="footer", printed_text="Order dated", normalization="blank", char_start=0, char_end=1,
            file_sha256_status="computed", text_layer="native_text", bbox_status="located",
            created_at="t")
    conn.commit()
    (c,) = classify_conflicts(conn)
    assert c["cause"] == "proposed_or_unapproved_sheet" and c["proposed_correction"] is None


def test_queue_rebuild_keeps_review_decisions_and_requires_a_named_reviewer(archive):  # noqa: F811
    conn, ids = archive
    _set(conn, ids.v_rev1, "2026-10-13")
    _conflict(conn, ids.v_rev1, "2026-10-13", "2009-10-13")
    conn.commit()
    manual = [{"problem_type": "charge_sign_conflict", "priority": 1, "key": "k", "family_key": FAMILY,
               "tariff_version_id": ids.v_rev1, "explanation": "order prints a decrement", "evidence_level":
               "assistant_page_review", "source_documents": [{"path": "o.pdf", "pages": [1]}]},
              {"annotate": True, "problem_type": "effective_date_conflict", "tariff_version_id": ids.v_rev1,
               "evidence_level": "assistant_page_review", "note": "footer read"}]
    out = er.build_review_queue(conn, source_audit=[], manual=manual)
    assert out["build"] == {"inserted": 2, "annotated": 1}
    items = {i["problem_type"]: i for i in er.list_items(conn)}
    date = items["effective_date_conflict"]
    assert date["cause"] == "stored_year_digit_error" and date["evidence_level"] == "assistant_page_review"
    assert date["source_documents"][0]["pages"] == [1] and date["proposed_correction"]["to"] == "2009-10-13"
    with pytest.raises(ValueError):
        er.review_item(conn, date["id"], decision="accepted", reviewer=" ", note="x")
    with pytest.raises(ValueError):
        er.review_item(conn, date["id"], decision="applied", reviewer="r", note="x")
    er.review_item(conn, date["id"], decision="accepted", reviewer="reviewer", note="footer read on the image")
    again = er.build_review_queue(conn, source_audit=[], manual=manual)
    assert again["build"].get("inserted", 0) == 0
    kept = conn.execute("SELECT review_status, reviewed_by, evidence_level FROM evidence_review_items WHERE id = ?",
                        (date["id"],)).fetchone()
    assert tuple(kept) == ("accepted", "reviewer", "human_review")   # neither the rebuild nor the annotation undid it
    # The queue never corrects the version itself.
    assert conn.execute("SELECT effective_start FROM tariff_versions WHERE id = ?", (ids.v_rev1,)).fetchone()[0] \
        == "2026-10-13"
