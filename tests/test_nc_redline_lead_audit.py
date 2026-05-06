from __future__ import annotations

import sqlite3
from pathlib import Path

from duke_rates.analytics import nc_redline_lead_audit as lead_audit
from duke_rates.parse import redline_crossref
from duke_rates.parse.redline_crossref import RedlineCrossRef
from duke_rates.parse.redline_page_parser import RedlinePageParseResult, RedlineSpanClue


def test_extract_page_level_clues_recovers_leaf_revision_context(monkeypatch) -> None:
    def fake_parse_redline_page(pdf_path: str, *, page_number: int, max_clues: int = 16):
        return RedlinePageParseResult(
            pdf_path=pdf_path,
            page_number=page_number,
            changed_span_count=2,
            horizontal_line_count=1,
            clues=[
                RedlineSpanClue(
                    page_number=page_number,
                    text="Tenth Eleventh",
                    context_text="NC Tenth Eleventh Revised Leaf No. 133",
                    bbox=(0.0, 0.0, 1.0, 1.0),
                    is_red_text=True,
                ),
                RedlineSpanClue(
                    page_number=page_number,
                    text="ignored@example.com",
                    context_text="ignored@example.com",
                    bbox=(0.0, 1.0, 1.0, 2.0),
                    is_red_text=True,
                ),
            ],
        )

    monkeypatch.setattr(lead_audit, "parse_redline_page", fake_parse_redline_page)

    report = lead_audit._extract_page_level_clues(
        pdf_path="dummy.pdf",
        start_page=3,
        end_page=4,
    )

    assert report["actionable_clues"] == [
        "p3 | NC Tenth Eleventh Revised Leaf No. 133",
        "p4 | NC Tenth Eleventh Revised Leaf No. 133",
    ]


def test_build_rows_promotes_actionable_redline_clues() -> None:
    rows = lead_audit._build_rows(
        [
            {
                "utility": "DEC",
                "family_key": "nc-carolinas-rider-STS",
                "title": "Storm Securitization Rider",
                "confidence_score": 72.0,
                "confidence_tier": "medium",
                "timeline_status": "complete",
                "gap_opportunity_count": 0,
                "anomaly_count": 0,
                "redline_doc_count": 2,
                "corroborated_redline_doc_count": 0,
                "unpaired_redline_doc_count": 1,
                "redline_clue_doc_count": 0,
                "dual_rate_pair_doc_count": 0,
                "comparative_phrase_doc_count": 0,
                "insert_delete_marker_doc_count": 0,
                "supersession_clue_doc_count": 0,
                "max_dual_rate_pair_count": 0,
                "schedule_code": "STS",
            }
        ],
        crossrefs_by_family={"nc-carolinas-rider-STS": []},
        page_clues_by_family={
            "nc-carolinas-rider-STS": {
                "page_clue_doc_count": 1,
                "actionable_clue_count": 2,
                "top_actionable_clues": [
                    "p3 | NC Tenth Eleventh Revised Leaf No. 133",
                    "p3 | Superseding NC Ninth Tenth Revised Leaf No. 133",
                ],
            }
        },
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["recommended_action"] == "use_redline_clues_to_find_clean_companions"
    assert row["actionable_clue_count"] == 2
    assert "NC Tenth Eleventh Revised Leaf No. 133" in str(row["search_hint"])
    assert "top_clue=p3 | NC Tenth Eleventh Revised Leaf No. 133" in str(row["notes"])


def test_scan_redlines_for_crossrefs_uses_slice_bounds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "lead.db"
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE historical_documents (
                id INTEGER PRIMARY KEY,
                family_key TEXT,
                local_path TEXT,
                start_page INTEGER,
                end_page INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE document_fingerprints (
                id INTEGER PRIMARY KEY,
                source_pdf TEXT,
                page_start INTEGER,
                page_end INTEGER,
                is_redline_candidate INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO historical_documents (id, family_key, local_path, start_page, end_page)
            VALUES (1, 'nc-progress-leaf-704', ?, 3, 6)
            """,
            (str(pdf_path),),
        )
        conn.execute(
            """
            INSERT INTO document_fingerprints (id, source_pdf, page_start, page_end, is_redline_candidate)
            VALUES (1, ?, 3, 6, 1)
            """,
            (str(pdf_path),),
        )
        conn.commit()
    finally:
        conn.close()

    calls: list[tuple[int | None, int | None]] = []

    def fake_extract_crossref(
        pdf_path: str,
        max_pages: int = 3,
        *,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> RedlineCrossRef:
        calls.append((start_page, end_page))
        return RedlineCrossRef(
            source_pdf=pdf_path,
            docket_numbers=["E-2, Sub 1193"],
            leaf_nos=["704"],
        )

    monkeypatch.setattr(redline_crossref, "extract_crossref", fake_extract_crossref)

    results = redline_crossref.scan_redlines_for_crossrefs(
        str(db_path),
        family_key_pattern="nc-progress-leaf-704",
    )

    assert calls == [(3, 6)]
    assert results == [
        {
            "source_pdf": str(pdf_path),
            "page_start": 3,
            "page_end": 6,
            "docket_numbers": ["E-2, Sub 1193"],
            "filing_date": None,
            "leaf_nos": ["704"],
            "supersedes_leaf_nos": [],
            "old_effective_date": None,
            "new_effective_date": None,
            "utility": None,
        }
    ]
