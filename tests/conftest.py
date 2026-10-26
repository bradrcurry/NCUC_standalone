"""Session-wide guards that keep the test suite away from the production DB.

``duke_rates.db.sqlite.connect`` runs ``SCHEMA_SQL`` + ``migrate()`` on every
connection, and some migrations delete rows (e.g. TD-002 rider-block dedupe).
A test that opens ``data/db/duke_rates.db`` through that helper can therefore
mutate production history.  This conftest:

1. Resolves ``LIVE_DB_PATH`` -- the database that "live-data" tests should read.
   It is ``$DUKE_RATES_TEST_LIVE_DB`` when set (point it at a frozen snapshot
   copy), otherwise ``None`` and live-data tests are skipped.
2. Points ``DUKE_RATES_DATABASE_PATH`` at that snapshot (or a throwaway file)
   so code paths that fall back to ``get_settings()`` never default to
   production.
3. Wraps ``sqlite3.connect`` so any write-capable connection to the production
   file raises immediately.  Read-only URIs (``mode=ro``) are still allowed.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB_PATH = (REPO_ROOT / "data" / "db" / "duke_rates.db").resolve()

_live = os.environ.get("DUKE_RATES_TEST_LIVE_DB")
LIVE_DB_PATH: Path | None = Path(_live).resolve() if _live else None
if LIVE_DB_PATH is not None and LIVE_DB_PATH == PRODUCTION_DB_PATH:
    raise RuntimeError(
        "DUKE_RATES_TEST_LIVE_DB must point at a snapshot copy, not the production DB"
    )

if LIVE_DB_PATH is not None:
    os.environ["DUKE_RATES_DATABASE_PATH"] = str(LIVE_DB_PATH)
else:
    os.environ["DUKE_RATES_DATABASE_PATH"] = str(
        Path(tempfile.gettempdir()) / "duke_rates_pytest_default.db"
    )


class ProductionDatabaseAccessError(RuntimeError):
    pass


def _targets_production(database: object, uri: bool) -> bool:
    raw = os.fspath(database) if isinstance(database, (str, os.PathLike)) else ""
    if not raw or raw == ":memory:":
        return False
    if uri or raw.startswith("file:"):
        path_part, _, query = raw.removeprefix("file:").partition("?")
        if "mode=ro" in query:
            return False
        raw = path_part
    try:
        return Path(raw).resolve() == PRODUCTION_DB_PATH
    except OSError:
        return False


_real_connect = sqlite3.connect


def _guarded_connect(database, *args, **kwargs):  # type: ignore[no-untyped-def]
    if _targets_production(database, bool(kwargs.get("uri", False))):
        raise ProductionDatabaseAccessError(
            f"test attempted a write-capable connection to production DB {database!r}; "
            "use LIVE_DB_PATH (a snapshot) or tmp_path instead"
        )
    return _real_connect(database, *args, **kwargs)


sqlite3.connect = _guarded_connect  # type: ignore[assignment]


requires_live_db = pytest.mark.skipif(
    LIVE_DB_PATH is None or not LIVE_DB_PATH.exists(),
    reason="set DUKE_RATES_TEST_LIVE_DB to a snapshot copy of duke_rates.db",
)
