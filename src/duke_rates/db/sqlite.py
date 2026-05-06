from __future__ import annotations

import sqlite3
from pathlib import Path

from duke_rates.db.schema import SCHEMA_SQL, migrate


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA synchronous=NORMAL;")
    connection.executescript(SCHEMA_SQL)
    migrate(connection)
    return connection
