"""Stored vs printed effective dates for each tariff version (Phase 2E D2).

``tariff_versions.effective_start`` is metadata someone stored. The clue ledger
holds the dates the version's own sheet prints. This module compares the two
and keeps every disagreement for review. It writes only
``effective_date_assessments``; it never changes ``tariff_versions``.

Kinds of date, kept apart:

* **stored**: ``tariff_versions.effective_start``.
* **observed**: ``effective_date`` clues on the version's own sheet pages
  (``VersionEvidence.family_clues``: index pages, other leaves and pages headed
  by the other utility excluded), with a readable value. Redline-glued
  (``redline_ambiguous``) and blank clues are not observations.
* **inferred**: bounds from rule-verified lineage links (a predecessor's date is
  a lower bound, a successor's an upper bound). They never stand in for an
  observation.
* **reviewed**: ``reviewed_value`` set by a named reviewer; preserved on rebuild.

Statuses: ``agree``, ``conflict`` (one observed date, not the stored one),
``ambiguous`` (several observed dates), ``stored_only``, ``observed_only``,
``none``, ``reviewed``.

``evaluation_window`` is [earliest, latest] of the stored and observed dates.
Gap evaluation uses it so that a wrong stored date cannot, by itself, rule out
a candidate (D1 §13.3: version 6548 stores 2024-01-24, its sheet prints
2025-01-01).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

from duke_rates.historical.ncuc.clue_lineage import load_version_evidence

BUILDER_VERSION = "effective_date_reconciliation@1.0.0"
RULE = "own_sheet_effective_clues(normalized|exact|ocr_repaired) vs tariff_versions.effective_start"
_OBSERVED_NORMALIZATIONS = ("exact", "normalized", "ocr_repaired")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def assess(stored: str | None, observed: list[str]) -> str:
    dates = sorted(set(observed))
    if not dates:
        return "stored_only" if stored else "none"
    if not stored:
        return "observed_only" if len(dates) == 1 else "ambiguous"
    if len(dates) > 1:
        return "ambiguous"
    return "agree" if dates[0] == stored else "conflict"


def build_effective_date_assessments(conn: sqlite3.Connection) -> dict:
    versions = load_version_evidence(conn)
    observed_by_version: dict[int, list[dict]] = defaultdict(list)
    for vid, ev in versions.items():
        for c in ev.family_clues("effective_date"):
            if c.normalization in _OBSERVED_NORMALIZATIONS and c.value:
                observed_by_version[vid].append({
                    "date": c.value.split("/")[0], "clue_id": c.id, "file_sha256": c.file_sha256,
                    "page": c.page, "role": c.role, "normalization": c.normalization,
                })
    inferred: dict[int, list[dict]] = defaultdict(list)
    for link_id, succ, pred in conn.execute(
        "SELECT id, from_version_id, to_version_id FROM lineage_links WHERE status = 'verified' AND to_version_id IS NOT NULL"
    ):
        for vid, other, bound in ((succ, pred, "after"), (pred, succ, "before")):
            dates = sorted({o["date"] for o in observed_by_version.get(other, [])})
            if len(dates) == 1:
                inferred[vid].append({"bound": bound, "date": dates[0], "basis": f"verified_link:{link_id}",
                                      "other_version_id": other})

    counts: dict[str, int] = defaultdict(int)
    now = _now()
    for vid, ev in sorted(versions.items()):
        stored = ev.effective_start[:10] if ev.effective_start else None
        observed = observed_by_version.get(vid, [])
        dates = [o["date"] for o in observed]
        status = assess(stored, dates)
        window_dates = sorted(set(dates) | ({stored} if stored else set()))
        window = [window_dates[0], window_dates[-1]] if window_dates else []
        for b in inferred.get(vid, []):
            b["violated_by_observed"] = any(
                (d <= b["date"]) if b["bound"] == "after" else (d >= b["date"]) for d in set(dates))
        existing = conn.execute(
            "SELECT reviewed_value FROM effective_date_assessments WHERE tariff_version_id = ?", (vid,)
        ).fetchone()
        if existing and existing[0]:
            status = "reviewed"
        counts[status] += 1
        conn.execute(
            """
            INSERT INTO effective_date_assessments (tariff_version_id, family_key, stored_value, observed_json,
                inferred_json, status, evaluation_window_json, rule, builder_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tariff_version_id) DO UPDATE SET family_key = excluded.family_key,
                stored_value = excluded.stored_value, observed_json = excluded.observed_json,
                inferred_json = excluded.inferred_json, status = excluded.status,
                evaluation_window_json = excluded.evaluation_window_json, rule = excluded.rule,
                builder_version = excluded.builder_version, updated_at = excluded.updated_at
            """,
            (vid, ev.family_key, stored, json.dumps(observed), json.dumps(inferred.get(vid, [])), status,
             json.dumps(window), RULE, BUILDER_VERSION, now, now),
        )
    conn.commit()
    return {"versions_assessed": len(versions), "by_status": dict(sorted(counts.items())),
            "bound_violations": sum(1 for v in inferred.values() for b in v if b.get("violated_by_observed"))}


def evaluation_window(conn: sqlite3.Connection, version_id: int | None) -> dict | None:
    """The assessment for one version (``None`` without the table or a row)."""
    if version_id is None or not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'effective_date_assessments'"
    ).fetchone():
        return None
    row = conn.execute(
        "SELECT status, stored_value, evaluation_window_json, reviewed_value FROM effective_date_assessments "
        "WHERE tariff_version_id = ?", (version_id,)
    ).fetchone()
    if row is None:
        return None
    return {"status": row[0], "stored": row[1], "window": json.loads(row[2] or "[]"), "reviewed": row[3]}


def assessment_summary(conn: sqlite3.Connection) -> dict:
    q = conn.execute
    return {
        "by_status": dict(q("SELECT status, COUNT(*) FROM effective_date_assessments GROUP BY 1").fetchall()),
        "conflicts": [
            dict(zip(("version_id", "family_key", "stored", "observed"), r))
            for r in q("SELECT tariff_version_id, family_key, stored_value, observed_json FROM "
                       "effective_date_assessments WHERE status = 'conflict' ORDER BY family_key, tariff_version_id")
        ],
    }
