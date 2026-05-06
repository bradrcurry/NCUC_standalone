import sqlite3
from hashlib import sha256
from pathlib import Path

from duke_rates.historical.ncuc.smart_downloader import SmartNcucDownloader


def _create_minimal_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE ncuc_discovery_records (
            id INTEGER PRIMARY KEY,
            discovered_url TEXT,
            fetch_status TEXT,
            local_path TEXT,
            content_hash TEXT,
            filing_title TEXT,
            error_detail TEXT,
            fetched_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            local_path TEXT,
            content_hash TEXT,
            title TEXT,
            canonical_url TEXT
        )
        """
    )
    conn.commit()


def test_download_with_dedup_skips_duplicate_and_deletes_download(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    _create_minimal_tables(conn)

    existing_path = tmp_path / "existing.pdf"
    existing_bytes = b"%PDF-1.4 duplicate content"
    existing_hash = sha256(existing_bytes).hexdigest()
    existing_path.write_bytes(existing_bytes)

    conn.execute(
        """
        INSERT INTO historical_documents (id, local_path, content_hash, title, canonical_url)
        VALUES (1, ?, ?, 'Existing historical doc', 'https://example.com/existing')
        """,
        (
            str(existing_path),
            existing_hash,
        ),
    )
    conn.execute(
        """
        INSERT INTO ncuc_discovery_records (id, discovered_url, fetch_status, filing_title)
        VALUES (42, 'https://example.com/new', 'pending', 'Duplicate attachment')
        """
    )
    conn.commit()

    downloader = SmartNcucDownloader(conn, tmp_path / "downloads")

    def download_func(_url: str, dest_path: Path) -> int:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(existing_bytes)
        return dest_path.stat().st_size

    result = downloader.download_with_dedup(
        document_url="https://example.com/new",
        document_title="Duplicate attachment",
        docket_number="E-2 Sub 9999",
        discovery_record_id=42,
        download_func=download_func,
    )

    assert result.skipped is True
    assert result.success is False
    assert result.duplicate_of is not None
    assert result.file_path is None or not result.file_path.exists()

    row = conn.execute(
        "SELECT fetch_status, content_hash, error_detail FROM ncuc_discovery_records WHERE id = 42"
    ).fetchone()
    assert row is not None
    assert row[0] == "skipped_duplicate"
    assert row[1] == existing_hash
    assert row[2] == "duplicate_of:historical_documents:1"
