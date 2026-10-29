"""Prioritized evidence review queue (Phase 2E D3).

One table, ``evidence_review_items``, for every finding a person must decide:

* ``billed_charges_on_redline_source``: a version's current charges were extracted from pages that carry
  redline markup (the shared source audit, :func:`ingestion_validation.audit_version_sources`).
* ``effective_date_conflict``: stored vs printed effective date, with its classified cause
  (:func:`effective_date_reconciliation.classify_conflicts`).
* ``verified_document_unbound``: a page-reviewed local sheet that no version uses, with what a
  ``bind_unbound_document`` proposal would do (or why it is blocked).
* ``local_candidate_on_redline_pages``: a local gap candidate whose pages are a redline.
* page-reviewed findings recorded by hand (``manual``: for example a charge whose sign disagrees with the
  order that set it).

Priority 1 means the finding can change a billed amount; 2, a document or lineage relationship that
feeds billing; 3, discovery evidence. Each item carries its source documents with pages, the printed
evidence, the database values, an explanation, the proposed correction (never applied here), the
evidence level (``rule`` / ``assistant_page_review`` / ``human_review``) and the downstream impact.
Rebuilding keeps every review decision. Only :func:`review_item` with a named reviewer changes
``review_status``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, Iterable

BUILDER_VERSION = "evidence_review@1.0.0"
DECISIONS = ("accepted", "rejected", "deferred")

_DATE_EXPLANATION = {
    "redline_contamination": "the pages that print the date carry redline markup; the printed date may be a "
                             "struck or inserted value",
    "proposed_or_unapproved_sheet": "the sheet prints a (Proposed) revision or a blank order date: the printed "
                                    "date was requested, not approved",
    "stored_year_digit_error": "same month and day, another year: a keying error in the stored date",
    "observed_period_held_by_sibling": "another version already starts on the printed date: two versions claim "
                                       "one period (duplicate registration or wrong binding), not a date fix",
    "version_date_disagrees_with_its_document": "the bound document's own date is the printed one; only the "
                                                "version row differs",
    "stored_filing_or_order_date": "the stored date is the filing/letter/order date of the compliance filing, "
                                   "not the effective date printed on the sheet",
    "adjacent_rate_year": "first-of-month dates a year apart: the bound pages may be the other rate year's sheet",
    "bound_document_from_other_period": "the printed date is more than a year away: the bound pages are another "
                                        "revision's sheet, or the stored period is wrong",
    "unexplained": "no rule explains the difference",
}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _uid(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def _version_row(conn: sqlite3.Connection, vid: int) -> dict:
    row = conn.execute(
        "SELECT tv.id, tv.family_key, tv.effective_start, tv.status, tv.revision_label, tv.historical_document_id, "
        "hd.local_path, hd.content_hash, hd.start_page, hd.end_page, hd.effective_start AS document_effective_start, "
        "(SELECT COUNT(*) FROM tariff_charges WHERE version_id = tv.id) AS charges FROM tariff_versions tv "
        "LEFT JOIN historical_documents hd ON hd.id = tv.historical_document_id WHERE tv.id = ?", (vid,)).fetchone()
    return dict(row) if row else {}


def _billing_window(conn: sqlite3.Connection, family: str, a: str | None, b: str | None) -> dict:
    """Approved versions whose selection could change if a date moved between ``a`` and ``b``."""
    if not (a and b):
        return {}
    lo, hi = min(a, b), max(a, b)
    rows = conn.execute("SELECT id, effective_start FROM tariff_versions WHERE family_key = ? AND status = 'approved' "
                        "AND substr(effective_start, 1, 10) BETWEEN ? AND ? ORDER BY effective_start",
                        (family, lo, hi)).fetchall()
    return {"service_dates_affected": [lo, hi], "approved_versions_in_between": [dict(r) for r in rows]}


def _items_from_date_conflicts(conn, conflicts: Iterable[dict]) -> list[dict]:
    out = []
    for c in conflicts:
        v = _version_row(conn, c["tariff_version_id"])
        billed = (v.get("charges") or 0) > 0 and v.get("status") == "approved"
        out.append({
            "item_uid": _uid("effective_date_conflict", c["tariff_version_id"]),
            "problem_type": "effective_date_conflict", "priority": 1 if billed else 2,
            "family_key": c["family_key"], "tariff_version_id": c["tariff_version_id"],
            "historical_document_id": v.get("historical_document_id"),
            "source_documents": [{"path": c["source_pdf"], "sha256": c["file_sha256"], "pages": c["pages"],
                                  "bound_pages": c["bound_pages"]}],
            "printed_evidence": {"effective_dates_printed": c["printed"], "pages": c["pages"],
                                 "page_evidence": c["page_evidence"]},
            "db_values": {"tariff_versions.effective_start": c["stored"],
                          "historical_documents.effective_start": c["document_effective_start"],
                          "status": c["version_status"], "charges": c["charges"],
                          "siblings_at_printed_date": c["siblings_at_printed_date"]},
            "explanation": _DATE_EXPLANATION[c["cause"]], "cause": c["cause"],
            "proposed_correction": c["proposed_correction"],
            "evidence_level": "rule", "confidence": c["confidence"], "tests": c["tests"],
            "downstream": {"charges_on_version": c["charges"], "billable": billed} | _billing_window(
                conn, c["family_key"], c["stored"], (c["printed"] or [None])[0]),
        })
    return out


def _items_from_source_audit(conn, audit: Iterable[dict], grades: dict[int, str]) -> list[dict]:
    out = []
    for a in audit:
        if "bound_pages_not_redline" not in a["failed"]:
            continue
        v = _version_row(conn, a["tariff_version_id"])
        d = a["checks"]["bound_pages_not_redline"]["detail"]
        grade = grades.get(a["tariff_version_id"], "unknown")
        billed = v.get("status") == "approved"
        out.append(
            {
                "item_uid": _uid(
                    "billed_charges_on_redline_source",
                    a["tariff_version_id"],
                    v.get("historical_document_id"),
                ),
                "problem_type": "billed_charges_on_redline_source",
                "priority": 1 if billed and not grade.startswith(("weak", "fingerprint")) else 2,
                "family_key": a["family_key"],
                "tariff_version_id": a["tariff_version_id"],
                "historical_document_id": v.get("historical_document_id"),
                "source_documents": [
                    {
                        "path": v.get("local_path"),
                        "sha256": a["checks"]
                        .get("source_hash_matches", {})
                        .get("detail", {})
                        .get("file_sha256"),
                        "archive_identifier": v.get("content_hash"),
                        "pages": d["page_evidence"]["redline_pages"],
                        "bound_pages": [v.get("start_page"), v.get("end_page")],
                    }
                ],
                "printed_evidence": {
                    "page_evidence": d["page_evidence"],
                    "redline_fingerprint": d["redline_fingerprint"],
                    "signal_grade": grade,
                },
                "db_values": {
                    "charges": a["charges"],
                    "status": v.get("status"),
                    "effective_start": v.get("effective_start"),
                },
            "explanation": "a version carrying charges is bound to a source span "
            "with redline evidence. "
                "This is a source-exposure risk, not proof that a stored value is wrong or that "
                "a particular bill used it. Review the charge evidence and clean counterpart.",
                "cause": "redline_source",
                "proposed_correction": {
                    "action": "find the clean sheet for this revision (bind or rebind it), or record "
                    "mark_redline_duplicate; then re-extract and promote through the "
                    "validated path"
                },
                "evidence_level": "rule",
                "confidence": grade,
                "downstream": {
                    "charges_on_version": a["charges"],
                    "billable": billed,
                    "billing_usage_verified": False,
                    "unattended_repromotion": "refused by ingestion validation until resolved",
                },
            }
        )
    return out


def _items_from_candidates(conn) -> list[dict]:
    from duke_rates.historical.ncuc import document_binding

    out = []
    for r in conn.execute(
        "SELECT gc.*, g.status AS gap_status, g.gap_type, g.family_key AS gap_family, g.expected_revision "
        "FROM gap_candidates gc JOIN lineage_gaps_typed g ON g.id = gc.gap_id WHERE g.active = 1 ORDER BY gc.id"
    ).fetchall():
        m = json.loads(r["match_json"] or "{}")
        verdict = (m.get("page_redline") or {}).get("verdict")
        src = [{"path": r["source_pdf"], "sha256": r["file_sha256"], "pages": [r["start_page"], r["end_page"]]}]
        if verdict in ("redline", "suspect"):
            out.append({
                "item_uid": _uid("local_candidate_on_redline_pages", r["id"]),
                "problem_type": "local_candidate_on_redline_pages",
                "priority": 2 if r["verification_status"] == "verified" else 3,
                "family_key": r["gap_family"], "tariff_version_id": None, "historical_document_id": None,
                "source_documents": src,
                "printed_evidence": {"page_redline": m.get("page_redline"), "identity_struck": m.get("identity_struck"),
                                     "rationale": r["rationale"]},
                "db_values": {"gap_id": r["gap_id"], "gap_status": r["gap_status"], "match_strength": r["match_strength"],
                              "candidate_review": r["verification_status"]},
                "explanation": "the candidate's own pages are a redline: an identity match, not a clean sheet",
                "cause": "redline_candidate",
                "proposed_correction": {"action": "do not bind; look for the clean copy of "
                                                  f"{r['expected_revision'] or 'this sheet'}"},
                "evidence_level": "assistant_page_review" if r["reviewed_by"] else "rule",
                "confidence": verdict, "downstream": {"blocks": "binding and rebinding (pages_not_redline)"},
            })
        if r["verification_status"] == "verified" and r["gap_status"] not in ("resolved",) and verdict != "redline":
            params = document_binding.params_from_gap_candidate(conn, r["id"])
            checks, plan = document_binding.evaluate(conn, params)
            failed = [n for n, c in checks.items() if c["blocking"] and c["ok"] is not True]
            out.append({
                "item_uid": _uid("verified_document_unbound", r["id"]),
                "problem_type": "verified_document_unbound", "priority": 2,
                "family_key": r["gap_family"], "tariff_version_id": plan.get("target_version_id"),
                "historical_document_id": None, "source_documents": src,
                "printed_evidence": {"revision": plan.get("revision_value"), "effective": plan.get("printed_dates"),
                                     "provenance": plan.get("provenance")},
                "db_values": {"gap_id": r["gap_id"], "gap_type": r["gap_type"], "gap_status": r["gap_status"],
                              "same_revision_versions": plan.get("same_revision_versions"),
                              "same_date_versions": plan.get("same_date_versions")},
                "explanation": "a page-reviewed sheet in the archive that no version uses",
                "cause": "unbound_document",
                "proposed_correction": {"proposal": "bind_unbound_document", "mode": plan.get("mode"),
                                        "gap_candidate_id": r["id"], "blocked_by": failed,
                                        "blocked_reason": checks.get("binding_mode_allowed", {}).get("detail", {})
                                        .get("reason")},
                "evidence_level": "assistant_page_review", "confidence": "ready" if not failed else "blocked",
                "downstream": {"creates_billable_version": False,
                               "note": "a created version stays bound_pending_charges until charges are promoted"},
            })
    return out


def _upsert(conn: sqlite3.Connection, item: dict) -> str:
    now = _now()
    exists = conn.execute("SELECT id, evidence_level FROM evidence_review_items WHERE item_uid = ?",
                          (item["item_uid"],)).fetchone()
    cols = {
        "problem_type": item["problem_type"], "priority": item["priority"], "family_key": item.get("family_key"),
        "tariff_version_id": item.get("tariff_version_id"), "historical_document_id": item.get("historical_document_id"),
        "source_documents_json": json.dumps(item.get("source_documents") or [], default=str),
        "printed_evidence_json": json.dumps(item.get("printed_evidence") or {}, default=str),
        "db_values_json": json.dumps(item.get("db_values") or {}, default=str),
        "explanation": item["explanation"], "cause": item.get("cause"),
        "proposed_correction_json": json.dumps(item.get("proposed_correction"), default=str),
        "evidence_level": item["evidence_level"],
        "downstream_json": json.dumps((item.get("downstream") or {}) | {"confidence": item.get("confidence"),
                                                                         "tests": item.get("tests")}, default=str),
        "builder_version": BUILDER_VERSION, "updated_at": now,
    }
    if exists:  # review decisions are never overwritten
        if exists[1] == "human_review":
            cols.pop("evidence_level")
        conn.execute(f"UPDATE evidence_review_items SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                     [*cols.values(), exists[0]])
        return "updated"
    cols |= {"item_uid": item["item_uid"], "created_at": now}
    conn.execute(f"INSERT INTO evidence_review_items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 list(cols.values()))
    return "inserted"


def build_review_queue(conn: sqlite3.Connection, *, date_conflicts: list[dict] | None = None,
                       source_audit: list[dict] | None = None, redline_grades: dict[int, str] | None = None,
                       manual: list[dict] | None = None) -> dict:
    """Build or refresh the queue. Inputs not given are computed (the source audit reads every bound span)."""
    from duke_rates.historical.ncuc.effective_date_reconciliation import classify_conflicts
    from duke_rates.historical.ncuc.ingestion_validation import audit_version_sources

    conn.row_factory = sqlite3.Row
    date_conflicts = classify_conflicts(conn) if date_conflicts is None else date_conflicts
    source_audit = audit_version_sources(conn) if source_audit is None else source_audit
    items = (_items_from_source_audit(conn, source_audit, redline_grades or {})
             + _items_from_date_conflicts(conn, date_conflicts) + _items_from_candidates(conn))
    annotations = [m for m in manual or [] if m.get("annotate")]
    for m in manual or []:
        if not m.get("annotate"):
            items.append(m | {"item_uid": _uid("manual", m["problem_type"], m.get("tariff_version_id"),
                                               m.get("historical_document_id"), m.get("key"))})
    counts: dict[str, int] = {}
    for item in items:
        outcome = _upsert(conn, item)
        counts[outcome] = counts.get(outcome, 0) + 1
    for a in annotations:  # a page-image reading attached to a rule item (its evidence level rises)
        n = conn.execute(
            "UPDATE evidence_review_items SET evidence_level = ?, printed_evidence_json = json_set("
            "printed_evidence_json, '$.assistant_review', ?) WHERE problem_type = ? AND tariff_version_id = ? "
            "AND evidence_level <> 'human_review'",
            (a["evidence_level"], a["note"], a["problem_type"], a["tariff_version_id"])).rowcount
        counts["annotated"] = counts.get("annotated", 0) + n
    conn.commit()
    return {"build": counts} | queue_summary(conn)


def queue_summary(conn: sqlite3.Connection) -> dict:
    q = conn.execute
    return {
        "items": q("SELECT COUNT(*) FROM evidence_review_items").fetchone()[0],
        "by_type_priority": [dict(zip(("problem_type", "priority", "n", "charges"), r)) for r in q(
            "SELECT problem_type, priority, COUNT(*), SUM(CAST(json_extract(downstream_json, '$.charges_on_version') "
            "AS INTEGER)) FROM evidence_review_items GROUP BY 1, 2 ORDER BY 2, 1")],
        "by_status": dict(q("SELECT review_status, COUNT(*) FROM evidence_review_items GROUP BY 1").fetchall()),
    }


def list_items(conn: sqlite3.Connection, *, problem_type: str | None = None, priority: int | None = None,
               status: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM evidence_review_items WHERE 1 = 1", []
    for col, val in (("problem_type", problem_type), ("priority", priority), ("review_status", status)):
        if val is not None:
            sql += f" AND {col} = ?"
            params.append(val)
    rows = []
    for r in conn.execute(sql + " ORDER BY priority, problem_type, family_key, id", params):
        d = dict(r)
        for k in [k for k in d if k.endswith("_json")]:
            d[k[:-5]] = json.loads(d.pop(k) or "null")
        rows.append(d)
    return rows


def review_item(conn: sqlite3.Connection, item_id: int, *, decision: str, reviewer: str, note: str) -> None:
    """Record a person's decision. It changes the queue only; corrections go through their own workflows."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if not reviewer.strip() or not note.strip():
        raise ValueError("a review needs a reviewer name and a note")
    if conn.execute("UPDATE evidence_review_items SET review_status = ?, reviewed_by = ?, reviewed_at = ?, "
                    "review_note = ?, evidence_level = CASE WHEN ? = 'accepted' THEN 'human_review' "
                    "ELSE evidence_level END WHERE id = ?",
                    (decision, reviewer, _now(), note, decision, item_id)).rowcount != 1:
        raise ValueError(f"no review item {item_id}")
    conn.commit()
