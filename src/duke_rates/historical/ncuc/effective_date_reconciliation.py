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


# ---------------------------------------------------------------------------
# Conflict classification (Phase 2E D3)
# ---------------------------------------------------------------------------

CLASSIFIER_VERSION = "effective_date_conflict_classifier@1.0.0"
#: A stored date this close before the printed one, on a version whose document carries the same stored
#: date, is the filing / letter date of the compliance filing (D1: 6548 stores 2024-01-24, prints 2025-01-01).
_FILING_LAG_DAYS = 400


def _days(a: str, b: str) -> int:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


def classify_conflicts(conn: sqlite3.Connection, *, read_pages: bool = True) -> list[dict]:
    """Classify every ``conflict`` assessment by cause, from stored metadata and the sheet's own pages.

    Read-only. Causes, in precedence order (the first that holds is the cause; every test is reported):

    * ``redline_contamination``: the observed pages carry redline markup, so the printed date is unreliable.
    * ``proposed_or_unapproved_sheet``: the observed pages print a "(Proposed)" revision or a blank
      "Order dated ____": the printed date is a requested date, not an approved one (D3 spot check:
      version 5231's page is "Sixth (Proposed) Revised Leaf No. 64").
    * ``stored_year_digit_error``: same distinctive month and day (not the 1st, which most tariff dates
      share), another year (``2026-10-13`` vs ``2009-10-13``).
    * ``observed_period_held_by_sibling``: another version of the family already starts on the printed
      date. Changing this one's date would create two versions for one period: a duplicate registration
      or a wrong binding, never a date correction.
    * ``version_date_disagrees_with_its_document``: the bound document's own ``effective_start`` is the
      printed date; only the version row differs.
    * ``stored_filing_or_order_date``: the stored date is not the 1st of a month, is (within a week) the
      document's own date, and falls within ``_FILING_LAG_DAYS`` before the printed effective date: a
      filing, letter or order date stored as the effective date.
    * ``adjacent_rate_year``: both dates are the 1st of a month, about a year apart (multi-year rate plan
      bundles hold each rate year's sheet): the bound pages may be the other rate year's sheet.
    * ``bound_document_from_other_period``: the printed date is more than a year from the stored one:
      the bound pages are another revision's sheet, or the stored period is wrong.
    * ``unexplained``.

    ``proposed_correction`` is offered only for the year-digit, document-agrees and filing-date causes,
    with one observed date, no sibling holding it and clean pages; it is a proposal for
    ``provenance revise-metadata``, never applied here.
    """
    from duke_rates.parse.redline_detector import PageRedlineReader, summarize_page_evidence

    reader = PageRedlineReader() if read_pages else None
    out = []
    try:
        for a in conn.execute("SELECT * FROM effective_date_assessments WHERE status = 'conflict' ORDER BY id"
                              ).fetchall():
            obs = json.loads(a["observed_json"] or "[]")
            printed = sorted({o["date"] for o in obs})
            pages = sorted({o["page"] for o in obs})
            v = conn.execute(
                "SELECT tv.id, tv.effective_start, tv.status, tv.historical_document_id, hd.local_path, "
                "hd.start_page, hd.end_page, hd.effective_start AS hd_effective, "
                "(SELECT COUNT(*) FROM tariff_charges WHERE version_id = tv.id) AS charges "
                "FROM tariff_versions tv LEFT JOIN historical_documents hd ON hd.id = tv.historical_document_id "
                "WHERE tv.id = ?", (a["tariff_version_id"],)).fetchone()
            stored, obs_date = a["stored_value"], printed[0] if len(printed) == 1 else None
            siblings = [dict(r) for r in conn.execute(
                "SELECT tv.id, tv.effective_start, tv.status, tv.historical_document_id, hd.local_path, "
                "hd.start_page, hd.end_page FROM tariff_versions tv LEFT JOIN historical_documents hd "
                "ON hd.id = tv.historical_document_id WHERE tv.family_key = ? AND tv.id <> ? "
                "AND substr(tv.effective_start, 1, 10) = ?", (a["family_key"], a["tariff_version_id"], obs_date))]
            for s in siblings:
                s["same_file"] = bool(v["local_path"]) and s["local_path"] == v["local_path"]
            evidence = None
            if reader is not None and v["local_path"] and pages:
                evidence = summarize_page_evidence([e for p in pages for e in reader.pages(v["local_path"], p, p)])
            hd_eff = (v["hd_effective"] or "")[:10] or None
            marks = obs and conn.execute(
                f"SELECT COUNT(*) FROM regulatory_clues WHERE file_sha256 = ? AND page IN ({','.join('?' * len(pages))}) "
                "AND ((clue_kind = 'revision' AND json_extract(normalized_json, '$.proposed_marker') = 1) "
                "OR (clue_kind = 'order_date' AND normalization = 'blank'))", (obs[0]["file_sha256"], *pages)
            ).fetchone()[0]
            tests = {
                "redline_pages": bool(evidence and evidence["verdict"] == "redline"),
                "proposal_markers_on_pages": int(marks or 0),
                "year_digit": bool(obs_date and stored and obs_date[4:] == stored[4:] and obs_date[:4] != stored[:4]
                                   and stored[8:10] != "01"),
                "sibling_holds_printed_date": bool(siblings),
                "document_agrees_with_print": bool(obs_date and hd_eff == obs_date and stored != obs_date),
                "stored_is_document_date": bool(hd_eff and stored and abs(_days(hd_eff, stored)) <= 7),
                "days_stored_to_printed": _days(stored, obs_date) if (stored and obs_date) else None,
                "span_pages": (v["end_page"] or 0) - (v["start_page"] or 0) + 1 if v["start_page"] else None,
            }
            lag = tests["days_stored_to_printed"]
            if tests["redline_pages"]:
                cause = "redline_contamination"
            elif tests["proposal_markers_on_pages"]:
                cause = "proposed_or_unapproved_sheet"
            elif tests["year_digit"]:
                cause = "stored_year_digit_error"
            elif tests["sibling_holds_printed_date"]:
                cause = "observed_period_held_by_sibling"
            elif tests["document_agrees_with_print"]:
                cause = "version_date_disagrees_with_its_document"
            elif lag is not None and 0 < lag <= _FILING_LAG_DAYS and stored[8:10] != "01" and (
                    tests["stored_is_document_date"] or not hd_eff):
                cause = "stored_filing_or_order_date"
            elif lag is not None and 300 <= abs(lag) <= 400 and stored[8:10] == "01" and obs_date[8:10] == "01":
                cause = "adjacent_rate_year"
            elif lag is not None and abs(lag) > 366:
                cause = "bound_document_from_other_period"
            else:
                cause = "unexplained"
            correctable = cause in ("stored_year_digit_error", "version_date_disagrees_with_its_document",
                                    "stored_filing_or_order_date") and obs_date and not siblings \
                and not (evidence and evidence["verdict"] in ("redline", "suspect"))
            out.append({
                "tariff_version_id": a["tariff_version_id"], "family_key": a["family_key"], "stored": stored,
                "printed": printed, "pages": pages, "file_sha256": obs[0]["file_sha256"] if obs else None,
                "source_pdf": v["local_path"], "bound_pages": [v["start_page"], v["end_page"]],
                "document_effective_start": hd_eff, "version_status": v["status"], "charges": v["charges"],
                "page_evidence": {k: evidence.get(k) for k in ("verdict", "redline_pages", "suspect_pages")}
                if evidence else None,
                "siblings_at_printed_date": siblings, "tests": tests, "cause": cause,
                "confidence": "high" if correctable and cause != "stored_filing_or_order_date" else
                ("medium" if correctable else "needs_review"),
                "proposed_correction": {"table": "tariff_versions", "row_id": a["tariff_version_id"],
                                        "column": "effective_start", "from": stored, "to": obs_date,
                                        "via": "provenance revise-metadata (after review)"} if correctable else None,
                "classifier": CLASSIFIER_VERSION,
            })
    finally:
        if reader is not None:
            reader.close()
    return out
