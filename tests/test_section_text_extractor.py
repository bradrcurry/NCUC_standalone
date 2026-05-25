"""Tests for section_text_extractor against in-memory fixtures."""

from __future__ import annotations

import sqlite3

import pytest

from duke_rates.document_intelligence.section_text_extractor import (
    PAGE_SEPARATOR,
    fetch_section_text,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE ncuc_page_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT NOT NULL,
            artifact_version TEXT,
            page_number INTEGER NOT NULL,
            text_content TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    return c


def _insert(conn: sqlite3.Connection, source: str, page: int, text: str, version: str = "v1") -> None:
    conn.execute(
        "INSERT INTO ncuc_page_artifacts(source_pdf, artifact_version, page_number, text_content) VALUES(?,?,?,?)",
        (source, version, page, text),
    )


class TestFetchSectionText:
    def test_basic_range_concatenates(self, conn: sqlite3.Connection) -> None:
        _insert(conn, "a.pdf", 1, "page1")
        _insert(conn, "a.pdf", 2, "page2")
        _insert(conn, "a.pdf", 3, "page3")
        result = fetch_section_text(conn, "a.pdf", 1, 3)
        assert result.text == f"page1{PAGE_SEPARATOR}page2{PAGE_SEPARATOR}page3"
        assert result.page_count == 3
        assert result.missing_pages == ()

    def test_inclusive_endpoints(self, conn: sqlite3.Connection) -> None:
        _insert(conn, "a.pdf", 1, "p1")
        _insert(conn, "a.pdf", 2, "p2")
        _insert(conn, "a.pdf", 3, "p3")
        # range [2, 3] excludes page 1
        result = fetch_section_text(conn, "a.pdf", 2, 3)
        assert "p1" not in result.text
        assert "p2" in result.text
        assert "p3" in result.text

    def test_missing_pages_reported(self, conn: sqlite3.Connection) -> None:
        _insert(conn, "a.pdf", 1, "p1")
        _insert(conn, "a.pdf", 3, "p3")
        # Page 2 missing
        result = fetch_section_text(conn, "a.pdf", 1, 3)
        assert result.page_count == 2
        assert result.missing_pages == (2,)

    def test_different_pdf_excluded(self, conn: sqlite3.Connection) -> None:
        _insert(conn, "a.pdf", 1, "from_a")
        _insert(conn, "b.pdf", 1, "from_b")
        result = fetch_section_text(conn, "a.pdf", 1, 1)
        assert result.text == "from_a"
        assert result.page_count == 1

    def test_latest_artifact_version_wins(self, conn: sqlite3.Connection) -> None:
        # Both rows for same page; latest created_at should win.
        _insert(conn, "a.pdf", 1, "old", version="v1")
        # Bump created_at by re-inserting with a later timestamp.
        conn.execute(
            "INSERT INTO ncuc_page_artifacts(source_pdf, artifact_version, page_number, text_content, created_at) "
            "VALUES('a.pdf', 'v2', 1, 'new', datetime('now', '+1 hour'))"
        )
        result = fetch_section_text(conn, "a.pdf", 1, 1)
        assert result.text == "new"

    def test_max_chars_truncates(self, conn: sqlite3.Connection) -> None:
        _insert(conn, "a.pdf", 1, "abcdefghij")
        result = fetch_section_text(conn, "a.pdf", 1, 1, max_chars=4)
        assert result.text == "abcd"

    def test_empty_range_returns_empty(self, conn: sqlite3.Connection) -> None:
        result = fetch_section_text(conn, "a.pdf", 5, 10)
        assert result.text == ""
        assert result.page_count == 0
        assert result.missing_pages == (5, 6, 7, 8, 9, 10)

    def test_null_text_skipped(self, conn: sqlite3.Connection) -> None:
        _insert(conn, "a.pdf", 1, "p1")
        conn.execute(
            "INSERT INTO ncuc_page_artifacts(source_pdf, page_number, text_content) VALUES('a.pdf', 2, NULL)"
        )
        _insert(conn, "a.pdf", 3, "p3")
        result = fetch_section_text(conn, "a.pdf", 1, 3)
        # Page 2 had NULL text but row exists, so it counts toward page_count
        # but contributes nothing to the joined string.
        assert result.text == f"p1{PAGE_SEPARATOR}p3"

    def test_invalid_range_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="end_page"):
            fetch_section_text(conn, "a.pdf", 5, 3)
