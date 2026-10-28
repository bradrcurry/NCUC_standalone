"""Persistent search-attempt ledger (Phase 2E D1).

One ``search_attempts`` row per executed query, keyed by a canonical
``params_hash`` so an equivalent search can be recognised later.

Outcomes:

* ``hits`` / ``no_results``: the search completed. ``no_results`` is negative
  evidence a gap report may rely on.
* ``incomplete``: results were returned but the search may have stopped early
  (for example, the result cap was reached). Not negative evidence.
* ``error`` / ``blocked``: the search failed (Cloudflare challenges are ``blocked``).

Only ``hits`` and ``no_results`` count as "already searched": failed and
incomplete attempts never suppress a retry.

From schema v4 (Phase 2E D2) a portal search also records how it stopped:
``stop_reason`` (``last_page``, ``stale_grid``, ``blocked_pagination``, ...),
``pages_reached`` / ``pages_total`` and the portal's own ``items_reported``,
plus ``filter_verified`` for filtered searches. The outcome then comes from the
searcher's stop reason, not from whether the result cap was reached.

The ledger only records. It never changes what a searcher does or returns.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterator

COMPLETED_OUTCOMES = ("hits", "no_results")
_BLOCKED_MARKERS = ("cloudflare", "just a moment", "checking your browser", "403")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_params(channel: str, params: dict[str, Any]) -> tuple[str, str]:
    """Stable JSON and hash for a query, ignoring key order, list order, case and padding."""

    def norm(value: Any) -> Any:
        if isinstance(value, str):
            return " ".join(value.split()).lower()
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items()) if v not in (None, "", [], {})}
        if isinstance(value, (list, tuple, set)):
            items = [norm(v) for v in value if v not in (None, "")]
            return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
        return value

    payload = json.dumps({"channel": channel, "params": norm(params)}, sort_keys=True, separators=(",", ":"))
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ledger_available(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'search_attempts'").fetchone() is not None


def classify_error(exc: BaseException) -> str:
    text = str(exc).lower()
    return "blocked" if any(marker in text for marker in _BLOCKED_MARKERS) else "error"


@dataclass
class Attempt:
    id: int
    params_hash: str
    result_count: int | None = None
    result_refs: list | None = None
    outcome: str | None = None
    pages_reached: int | None = None

    stop_reason: str | None = None
    pages_total: int | None = None
    items_reported: int | None = None
    filter_verified: bool | None = None

    def finish_outcome(self, outcome: Any, *, refs: list | None = None) -> None:
        """Record a ``DocParamSearchOutcome``: its own stop reason decides the outcome."""
        self.result_count = len(outcome.results)
        self.result_refs = list(refs if refs is not None else
                                [r.document_id or r.document_detail_url for r in outcome.results])
        self.outcome = outcome.ledger_outcome
        self.stop_reason = outcome.stop_reason
        self.pages_reached = outcome.pages_reached
        self.pages_total = outcome.pages_total
        self.items_reported = outcome.items_reported
        self.filter_verified = outcome.filter_verified

    def finish(self, results: list | None = None, *, refs: list | None = None, cap: int | None = None,
               pages_reached: int | None = None) -> None:
        count = len(results) if results is not None else len(refs or [])
        self.result_count = count
        self.result_refs = list(refs or [])
        self.pages_reached = pages_reached
        if count == 0:
            self.outcome = "no_results"
        elif cap is not None and count >= cap:
            self.outcome = "incomplete"
        else:
            self.outcome = "hits"


class SearchLedger:
    def __init__(self, conn: sqlite3.Connection, *, created_by: str):
        self.conn = conn
        self.created_by = created_by
        columns = {row[1] for row in conn.execute("PRAGMA table_info(search_attempts)")}
        self.records_stop_reason = "stop_reason" in columns  # schema v4

    def completed_hashes(self, channel: str) -> set[str]:
        """``params_hash`` of every completed search on a channel (for planning without a query each)."""
        return {row[0] for row in self.conn.execute(
            "SELECT DISTINCT params_hash FROM search_attempts WHERE channel = ? AND outcome IN ('hits', 'no_results')",
            (channel,))}

    def prior_completed(self, channel: str, params: dict[str, Any]) -> sqlite3.Row | None:
        _, digest = canonical_params(channel, params)
        return self.conn.execute(
            "SELECT * FROM search_attempts WHERE params_hash = ? AND outcome IN ('hits', 'no_results') "
            "ORDER BY id DESC LIMIT 1",
            (digest,),
        ).fetchone()

    @contextmanager
    def attempt(self, channel: str, params: dict[str, Any], *, caller: str,
                gap_id: int | None = None) -> Iterator[Attempt]:
        """Record one search. Exceptions are recorded as error/blocked and re-raised."""
        payload, digest = canonical_params(channel, params)
        cur = self.conn.execute(
            """
            INSERT INTO search_attempts (gap_id, channel, caller, params_json, params_hash, started_at,
                                         outcome, created_by)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (gap_id, channel, caller, payload, digest, _now(), self.created_by),
        )
        self.conn.commit()
        attempt = Attempt(cur.lastrowid, digest)
        try:
            yield attempt
        except BaseException as exc:
            portal_outcome = getattr(exc, "outcome", None)  # PortalSearchError carries its outcome
            if portal_outcome is not None:
                attempt.finish_outcome(portal_outcome, refs=[])
                self._close(attempt, portal_outcome.ledger_outcome, f"{type(exc).__name__}: {exc}"[:500])
            else:
                attempt.result_count, attempt.result_refs = None, []
                self._close(attempt, classify_error(exc), f"{type(exc).__name__}: {exc}"[:500])
            raise
        outcome = attempt.outcome or ("incomplete" if attempt.result_count is None else "hits")
        self._close(attempt, outcome, None)

    def _close(self, attempt: Attempt, outcome: str, error: str | None) -> None:
        self.conn.execute(
            """
            UPDATE search_attempts SET finished_at = ?, outcome = ?, result_count = ?, result_refs_json = ?,
                   pages_reached = ?, error_detail = ?
            WHERE id = ?
            """,
            (_now(), outcome, attempt.result_count, json.dumps(attempt.result_refs or [], default=str),
             attempt.pages_reached, error, attempt.id),
        )
        if self.records_stop_reason:
            self.conn.execute(
                "UPDATE search_attempts SET stop_reason = ?, pages_total = ?, items_reported = ?, "
                "filter_verified = ? WHERE id = ?",
                (attempt.stop_reason, attempt.pages_total, attempt.items_reported,
                 None if attempt.filter_verified is None else int(attempt.filter_verified), attempt.id),
            )
        self.conn.commit()


def search_ledger_summary(conn: sqlite3.Connection) -> dict:
    q = conn.execute
    return {
        "attempts": q("SELECT COUNT(*) FROM search_attempts").fetchone()[0],
        "by_channel_outcome": [
            dict(zip(("channel", "outcome", "n"), r))
            for r in q("SELECT channel, outcome, COUNT(*) FROM search_attempts GROUP BY 1, 2 ORDER BY 1, 2")
        ],
        "distinct_param_hashes": q("SELECT COUNT(DISTINCT params_hash) FROM search_attempts").fetchone()[0],
        "by_stop_reason": [
            dict(zip(("channel", "stop_reason", "n"), r))
            for r in q("SELECT channel, stop_reason, COUNT(*) FROM search_attempts WHERE stop_reason IS NOT NULL "
                       "GROUP BY 1, 2 ORDER BY 1, 2")
        ] if {row[1] for row in q("PRAGMA table_info(search_attempts)")} >= {"stop_reason"} else [],
    }
