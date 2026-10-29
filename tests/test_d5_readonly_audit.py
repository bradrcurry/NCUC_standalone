"""A source audit must never invoke migrations or create a database."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest
from test_extraction_provenance import db, migrated  # noqa: F401

from bench.redline_exposure import run


def test_shared_audit_wrapper_preserves_database_bytes(migrated):  # noqa: F811
    conn, _, path, _ = migrated
    conn.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = run(path)
    assert report["read_only"] is True and report["human_verified"] is False
    assert report["summary"]["versions_with_charges"] == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_missing_audit_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        run(path)
    assert not path.exists()
