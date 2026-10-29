"""A read-oriented inventory must never run schema migrations, even in a dry run."""

import hashlib
import sqlite3

from duke_rates.document_intelligence.acquisition import _inventory_fetch_eligible


def test_fetch_inventory_reads_existing_database_without_migrations(tmp_path, monkeypatch):
    path = tmp_path / "inventory.db"
    with sqlite3.connect(path) as conn:
        # Deliberately minimal existing schema. The reader must not extend it.
        conn.execute("CREATE TABLE ncuc_discovery_records "
                     "(docket_number TEXT, fetch_status TEXT, local_path TEXT)")
        conn.executemany("INSERT INTO ncuc_discovery_records VALUES (?, ?, ?)", [
            ("E-7 Sub 1026", "pending", None),
            ("E-7 Sub 1026", "failed", None),
            ("E-2 Sub 1354", "requires_browser", None),
            ("E-2 Sub 1354", None, None),
            ("E-2 Sub 1354", "fetched", "archived.pdf"),
        ])
    before = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns

    def no_migration(*args, **kwargs):
        raise AssertionError("read inventory called migration-capable connect")

    monkeypatch.setattr("duke_rates.db.sqlite.connect", no_migration)
    result = _inventory_fetch_eligible(str(path))
    assert result["total_eligible"] == 4
    assert result["distinct_dockets"] == 2
    assert result["by_status"] == {"pending": 1, "failed": 1, "requires_browser": 1, "<null>": 1}
    assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == before


def test_fetch_inventory_does_not_create_missing_database(tmp_path):
    path = tmp_path / "missing.db"
    assert _inventory_fetch_eligible(str(path))["total_eligible"] == 0
    assert not path.exists()
