"""Reviewed document promotion: propose -> approve -> apply -> (rollback) (Phase 2E D2).

Turns a verified document finding into a corrected document relationship,
under explicit approval, with a snapshot and a way back.

Two proposal kinds:

* ``rebind_page_range``: bind a historical document to the page run that
  actually holds its sheet (D1: hd 7265 is bound to the bundle's index page;
  the clean First Revised Leaf 600 is pp.96-101).
* ``mark_redline_duplicate``: record that a span is the redline of a sheet
  already held clean elsewhere (D1: hd 2275 pp.100-105 is the redline of
  hd 7265's sheet). This writes only ``document_assertions``.

Checks run when proposing and again when applying (the data may have changed
in between). Each check is stored as pass/fail with its detail:

file identity and page bounds; destination family and utility as printed on
the pages; revision identity on the first page and no other leaf in the run;
effective date printed vs stored (a disagreement is reported, not blocking);
redline markup on the pages (blocking for a rebind: the target must be clean);
other documents bound to the same pages; conflicting lineage links; existing
redline-duplicate assertions; and the charges the legacy rebind would delete.

**Staged rebind.** ``lineage rebind-historical-page-range`` deletes the
version's ``tariff_charges`` (D1 §10). Applying a ``rebind_page_range``
proposal instead changes only the page bounds (and clears the cached text, so
extraction reads the new pages). Existing charges stay until a candidate run
over the new pages is extracted, validated and promoted through the existing
audited ``provenance promote``. Every row the apply touches is snapshotted
before and after, in ``promotion_snapshots``. ``rollback`` restores the
snapshot. It refuses while a later charge promotion is still in place; that
promotion is undone first with ``provenance rollback``.

Nothing here runs against production unless the caller passes the production
database explicitly, and every write needs a proposal approved by a named
reviewer other than the proposer.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from duke_rates.db.provenance import atomic, write_context
from duke_rates.historical.ncuc.clue_ledger import file_identity
from duke_rates.historical.ncuc.clue_lineage import family_leaf, family_leaf_hints, family_utility
from duke_rates.historical.ncuc.regulatory_clues import extract_page_clues
from duke_rates.parse.redline_detector import PageRedlineReader, summarize_page_evidence

WORKFLOW_VERSION = "document_promotion@1.0.0"
_HD_COLUMNS = ("id", "family_key", "local_path", "start_page", "end_page", "content_hash", "raw_text_path",
               "parsed_result_json", "evidence_json")
#: Columns a staged rebind changes (the rest of the row is only snapshotted).
_REBIND_SET = ("start_page", "end_page", "raw_text_path", "parsed_result_json", "evidence_json")


class PromotionError(RuntimeError):
    """The proposal cannot move to the requested state."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _hd(conn: sqlite3.Connection, hd_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM historical_documents WHERE id = ?", (hd_id,)).fetchone()
    if row is None:
        raise PromotionError(f"historical document {hd_id} does not exist")
    return row


def _versions(conn: sqlite3.Connection, hd_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tariff_versions WHERE historical_document_id = ? ORDER BY id",
                        (hd_id,)).fetchall()


def _check(ok: bool | None, blocking: bool, detail: Any) -> dict:
    return {"ok": ok, "blocking": blocking, "detail": detail}


# ---------------------------------------------------------------------------
# Page reading
# ---------------------------------------------------------------------------


def read_pages(pdf_path: str, start: int, end: int) -> list[dict]:
    """Clues printed on each page of the run (PDF text layer; no conversion artifact)."""
    import fitz

    out = []
    with fitz.open(pdf_path) as doc:
        for p in range(start, end + 1):
            if not 1 <= p <= len(doc):
                continue
            clues = extract_page_clues(doc[p - 1].get_text("text"))
            out.append({
                "page": p,
                "revisions": [c.normalized for c in clues if c.kind == "revision" and c.normalized_value],
                "revision_text": [c.printed_text for c in clues if c.kind == "revision"],
                "utilities": sorted({c.normalized_value for c in clues if c.kind == "utility" and c.normalized_value}),
                "effective": sorted({c.normalized_value.split("/")[0] for c in clues
                                     if c.kind == "effective_date" and c.normalized_value}),
                "dockets": sorted({c.normalized_value for c in clues if c.kind == "docket" and c.normalized_value}),
                "redline_glue": [c.printed_text for c in clues if c.normalization == "redline_ambiguous"],
            })
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _identity_checks(conn: sqlite3.Connection, hd: sqlite3.Row, start: int, end: int,
                     reader: PageRedlineReader) -> tuple[dict, list[dict]]:
    checks: dict[str, dict] = {}
    path = hd["local_path"]
    ident = file_identity(path)
    checks["file_present"] = _check(ident.sha256 is not None, True, {"path": path, "sha256": ident.sha256})
    if ident.sha256 is None:
        return checks, []
    stored = (hd["content_hash"] or "").lower()
    checks["file_matches_stored_hash"] = _check(
        None if not stored else stored in (ident.sha256, ident.md5), False,
        {"stored": stored or None, "computed_sha256": ident.sha256})
    checks["pages_in_file"] = _check(1 <= start <= end <= (ident.page_count or 0), True,
                                     {"start": start, "end": end, "page_count": ident.page_count})
    if not checks["pages_in_file"]["ok"]:
        return checks, []
    pages = read_pages(path, start, end)
    family = hd["family_key"]
    leaf = family_leaf(family) or family_leaf_hints(conn).get(family)
    utility = family_utility(family)
    first = pages[0] if pages else {}
    first_leaves = {r.get("leaf") for r in first.get("revisions", [])}
    checks["first_page_prints_family_leaf"] = _check(
        bool(leaf) and leaf in first_leaves, True,
        {"family_key": family, "family_leaf": leaf, "first_page_revisions": first.get("revision_text")})
    other = sorted({(p["page"], r.get("leaf")) for p in pages for r in p["revisions"]
                    if r.get("leaf") and r.get("leaf") != leaf})
    checks["no_other_leaf_in_run"] = _check(not other, True, {"other_leaf_pages": other})
    printed_utils = sorted({u for p in pages for u in p["utilities"]})
    checks["utility_matches_family"] = _check(
        None if not printed_utils or not utility else printed_utils == [utility], True,
        {"family_utility": utility, "printed": printed_utils})
    ordinals = sorted({r.get("ordinal") for p in pages for r in p["revisions"] if r.get("leaf") == leaf})
    checks["single_revision_identity"] = _check(len(ordinals) == 1, True, {"ordinals": ordinals})
    evidence = summarize_page_evidence(reader.pages(path, start, end))
    glue = [g for p in pages for g in p["redline_glue"]]
    checks["pages_not_redline"] = _check(
        evidence["verdict"] not in ("redline", "suspect") and not glue, True,
        {"page_evidence": evidence, "redline_glued_text": glue[:5]})
    return checks, pages


def _context_checks(conn: sqlite3.Connection, hd: sqlite3.Row, start: int, end: int, pages: list[dict]) -> dict:
    checks: dict[str, dict] = {}
    versions = _versions(conn, hd["id"])
    checks["single_tariff_version"] = _check(len(versions) == 1, True, {"version_ids": [v["id"] for v in versions]})
    printed = sorted({d for p in pages for d in p["effective"]})
    stored = versions[0]["effective_start"][:10] if versions and versions[0]["effective_start"] else None
    checks["effective_date_printed_vs_stored"] = _check(
        None if not printed or not stored else stored in printed, False,
        {"stored": stored, "printed": printed,
         "note": "a disagreement is reported for review; the stored value is not changed"})
    overlap = [dict(r) for r in conn.execute(
        "SELECT id, family_key, start_page, end_page FROM historical_documents WHERE local_path = ? AND id <> ? "
        "AND COALESCE(start_page, 1) <= ? AND COALESCE(end_page, start_page, 1) >= ?",
        (hd["local_path"], hd["id"], end, start))]
    checks["no_other_document_bound_to_pages"] = _check(not overlap, False, {"overlapping": overlap})
    if versions and conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'lineage_links'").fetchone():
        conflicts = [dict(r) for r in conn.execute(
            "SELECT id, from_version_id, to_version_id, status FROM lineage_links WHERE status = 'conflict' "
            "AND (from_version_id = ? OR to_version_id = ?)", (versions[0]["id"], versions[0]["id"]))]
        checks["no_conflicting_lineage_links"] = _check(not conflicts, False, {"conflicts": conflicts})
    asserted = [dict(r) for r in conn.execute(
        "SELECT id, assertion, target_historical_document_id FROM document_assertions "
        "WHERE historical_document_id = ? AND retracted_at IS NULL", (hd["id"],))]
    checks["no_redline_duplicate_assertion"] = _check(not asserted, True, {"assertions": asserted})
    charges = conn.execute(
        "SELECT COUNT(*) FROM tariff_charges WHERE version_id IN (SELECT id FROM tariff_versions "
        "WHERE historical_document_id = ?)", (hd["id"],)).fetchone()[0]
    checks["existing_charges_preserved"] = _check(True, False, {
        "tariff_charges_on_version": charges,
        "legacy_rebind_would_delete": charges,
        "staged_rebind_deletes": 0,
    })
    return checks


def _evaluate_rebind(conn: sqlite3.Connection, hd_id: int, start: int, end: int) -> dict:
    hd = _hd(conn, hd_id)
    reader = PageRedlineReader()
    try:
        checks, pages = _identity_checks(conn, hd, start, end, reader)
    finally:
        reader.close()
    checks.update(_context_checks(conn, hd, start, end, pages))
    return checks


def _evaluate_duplicate(conn: sqlite3.Connection, hd_id: int, start: int, end: int, duplicate_of: int) -> dict:
    """The span (pages ``start``-``end``) is a redline; ``duplicate_of`` holds the same sheet clean."""
    hd, target = _hd(conn, hd_id), _hd(conn, duplicate_of)
    reader = PageRedlineReader()
    try:
        own, own_pages = _identity_checks(conn, hd, start, end, reader)
        tgt, tgt_pages = _identity_checks(conn, target, target["start_page"] or 1,
                                          target["end_page"] or target["start_page"] or 1, reader)
    finally:
        reader.close()
    checks = {
        "same_family": _check(hd["family_key"] == target["family_key"], True,
                              {"family": hd["family_key"], "duplicate_of_family": target["family_key"]}),
        "span_is_redline": _check(own.get("pages_not_redline", {}).get("ok") is False, True,
                                  own.get("pages_not_redline", {}).get("detail")),
        "target_is_clean": _check(tgt.get("pages_not_redline", {}).get("ok") is True, True,
                                  tgt.get("pages_not_redline", {}).get("detail")),
        "target_prints_family_leaf": tgt.get("first_page_prints_family_leaf", _check(False, True, "not read")),
    }
    # The redline fuses old and new revision labels; the inserted (new) one must be the target's revision.
    target_ordinals = tgt.get("single_revision_identity", {}).get("detail", {}).get("ordinals", [])
    own_ordinals = sorted({r.get("ordinal") for p in own_pages for r in p["revisions"]})
    glued = [g for p in own_pages for g in p["redline_glue"]] + [t for p in own_pages for t in p["revision_text"]]
    checks["revision_consistent"] = _check(
        bool(target_ordinals) and (set(target_ordinals) <= set(own_ordinals) or bool(glued)), True,
        {"target_ordinals": target_ordinals, "span_ordinals": own_ordinals, "span_revision_text": glued[:4]})
    printed = sorted({d for p in tgt_pages for d in p["effective"]})
    checks["target_effective_printed"] = _check(bool(printed), False, {"printed": printed})
    return checks


def _blocking_failures(checks: dict) -> list[str]:
    return [name for name, c in checks.items() if c["blocking"] and c["ok"] is not True]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def propose(conn: sqlite3.Connection, kind: str, historical_document_id: int, *, params: dict,
            proposed_by: str, evidence: dict | None = None) -> dict:
    if not proposed_by.strip():
        raise PromotionError("a proposer name is required")
    if kind == "rebind_page_range":
        checks = _evaluate_rebind(conn, historical_document_id, params["start_page"], params["end_page"])
    elif kind == "mark_redline_duplicate":
        checks = _evaluate_duplicate(conn, historical_document_id, params["start_page"], params["end_page"],
                                     params["duplicate_of_historical_document_id"])
    else:
        raise PromotionError(f"unknown proposal kind {kind!r}")
    failures = _blocking_failures(checks)
    state = "blocked" if failures else "proposed"
    versions = _versions(conn, historical_document_id)
    cur = conn.execute(
        """
        INSERT INTO promotion_proposals (kind, historical_document_id, tariff_version_id, params_json, evidence_json,
            checks_json, state, state_reason, proposed_by, proposed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (kind, historical_document_id, versions[0]["id"] if len(versions) == 1 else None, json.dumps(params),
         json.dumps(evidence or {}), json.dumps(checks, default=str), state,
         ("blocking checks failed: " + ", ".join(failures)) if failures else "all blocking checks passed",
         proposed_by, _now()),
    )
    conn.commit()
    return {"proposal_id": cur.lastrowid, "state": state, "blocking_failures": failures,
            "checks": {k: v["ok"] for k, v in checks.items()}}


def _proposal(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM promotion_proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise PromotionError(f"no proposal {proposal_id}")
    return row


def approve(conn: sqlite3.Connection, proposal_id: int, *, approved_by: str, note: str) -> None:
    p = _proposal(conn, proposal_id)
    if p["state"] != "proposed":
        raise PromotionError(f"proposal {proposal_id} is {p['state']}; only a proposed (unblocked) one can be approved")
    if not approved_by.strip() or not note.strip():
        raise PromotionError("approval needs a reviewer name and a note")
    if approved_by.strip() == p["proposed_by"].strip():
        raise PromotionError("the approver must be someone other than the proposer")
    conn.execute("UPDATE promotion_proposals SET state = 'approved', approved_by = ?, approved_at = ?, "
                 "approval_note = ? WHERE id = ?", (approved_by, _now(), note, proposal_id))
    conn.commit()


def reject(conn: sqlite3.Connection, proposal_id: int, *, by: str, note: str) -> None:
    p = _proposal(conn, proposal_id)
    if p["state"] in ("applied", "rolled_back"):
        raise PromotionError(f"proposal {proposal_id} is {p['state']}")
    conn.execute("UPDATE promotion_proposals SET state = 'rejected', state_reason = ?, approved_by = ?, "
                 "approved_at = ? WHERE id = ?", (f"rejected: {note}", by, _now(), proposal_id))
    conn.commit()


def _snapshot(conn: sqlite3.Connection, proposal_id: int, phase: str, hd_id: int) -> dict:
    now = _now()
    counts: dict[str, int] = {}
    tables = {
        "historical_documents": conn.execute("SELECT * FROM historical_documents WHERE id = ?", (hd_id,)).fetchall(),
        "tariff_versions": _versions(conn, hd_id),
        "tariff_charges": conn.execute(
            "SELECT * FROM tariff_charges WHERE version_id IN (SELECT id FROM tariff_versions "
            "WHERE historical_document_id = ?) ORDER BY id", (hd_id,)).fetchall(),
        "document_assertions": conn.execute(
            "SELECT * FROM document_assertions WHERE historical_document_id = ?", (hd_id,)).fetchall(),
    }
    for table, rows in tables.items():
        counts[table] = len(rows)
        for r in rows:
            conn.execute("INSERT INTO promotion_snapshots (proposal_id, phase, table_name, row_key, row_json, taken_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (proposal_id, phase, table, str(r["id"]), json.dumps(dict(r), default=str), now))
    return counts


def _charge_state(conn: sqlite3.Connection, hd_id: int) -> list[str]:
    return [json.dumps(dict(r), sort_keys=True, default=str) for r in conn.execute(
        "SELECT * FROM tariff_charges WHERE version_id IN (SELECT id FROM tariff_versions "
        "WHERE historical_document_id = ?) ORDER BY id", (hd_id,))]


def apply(conn: sqlite3.Connection, proposal_id: int, *, applied_by: str) -> dict:
    p = _proposal(conn, proposal_id)
    if p["state"] != "approved":
        raise PromotionError(f"proposal {proposal_id} is {p['state']}; it must be approved by a named reviewer first")
    params = json.loads(p["params_json"])
    hd_id = p["historical_document_id"]
    checks = (_evaluate_rebind(conn, hd_id, params["start_page"], params["end_page"])
              if p["kind"] == "rebind_page_range" else
              _evaluate_duplicate(conn, hd_id, params["start_page"], params["end_page"],
                                  params["duplicate_of_historical_document_id"]))
    failures = _blocking_failures(checks)
    if failures:
        conn.execute("UPDATE promotion_proposals SET state = 'blocked', state_reason = ?, checks_json = ? WHERE id = ?",
                     ("re-check at apply failed: " + ", ".join(failures), json.dumps(checks, default=str), proposal_id))
        conn.commit()
        raise PromotionError(f"proposal {proposal_id}: blocking checks failed at apply time: {failures}")
    reason = f"promotion proposal {proposal_id} ({p['kind']}) approved by {p['approved_by']}: {p['approval_note']}"
    with atomic(conn), write_context(conn, f"{WORKFLOW_VERSION}:{applied_by}", evidence=reason):
        before = _snapshot(conn, proposal_id, "before", hd_id)
        if p["kind"] == "rebind_page_range":
            conn.execute(
                "UPDATE historical_documents SET start_page = ?, end_page = ?, raw_text_path = NULL, "
                "parsed_result_json = NULL, evidence_json = NULL WHERE id = ?",
                (params["start_page"], params["end_page"], hd_id))
            result = {"rebound": [params["start_page"], params["end_page"]],
                      "charges_kept": before["tariff_charges"], "charges_deleted": 0,
                      "next": "provenance extract-candidate on the new pages, compare, then provenance promote"}
        else:
            conn.execute(
                "INSERT INTO document_assertions (historical_document_id, assertion, target_historical_document_id, "
                "evidence_json, proposal_id, asserted_by, asserted_at) VALUES (?, 'redline_duplicate_of', ?, ?, ?, ?, ?)",
                (hd_id, params["duplicate_of_historical_document_id"],
                 json.dumps({"pages": [params["start_page"], params["end_page"]],
                             "span_is_redline": checks["span_is_redline"]["detail"]}, default=str),
                 proposal_id, applied_by, _now()))
            result = {"asserted": "redline_duplicate_of", "duplicate_of": params["duplicate_of_historical_document_id"],
                      "legacy_rows_changed": 0}
        after = _snapshot(conn, proposal_id, "after", hd_id)
        result |= {"snapshot_before": before, "snapshot_after": after}
        conn.execute("UPDATE promotion_proposals SET state = 'applied', applied_by = ?, applied_at = ?, "
                     "apply_result_json = ?, checks_json = ? WHERE id = ?",
                     (applied_by, _now(), json.dumps(result), json.dumps(checks, default=str), proposal_id))
    return result


def rollback(conn: sqlite3.Connection, proposal_id: int, *, by: str) -> dict:
    """Restore the rows an applied proposal changed, exactly as snapshotted."""
    p = _proposal(conn, proposal_id)
    if p["state"] != "applied":
        raise PromotionError(f"proposal {proposal_id} is {p['state']}; only an applied one can be rolled back")
    hd_id = p["historical_document_id"]
    snap = {(r["phase"], r["table_name"], r["row_key"]): r["row_json"] for r in conn.execute(
        "SELECT phase, table_name, row_key, row_json FROM promotion_snapshots WHERE proposal_id = ?", (proposal_id,))}
    after_charges = sorted(json.dumps(json.loads(v), sort_keys=True, default=str)
                           for (ph, t, _), v in snap.items() if ph == "after" and t == "tariff_charges")
    if sorted(_charge_state(conn, hd_id)) != after_charges:
        raise PromotionError(
            "tariff_charges changed after this proposal was applied (a charge promotion?); "
            "undo that first with `provenance rollback`, then roll back this proposal")
    with atomic(conn), write_context(conn, f"{WORKFLOW_VERSION}:{by}",
                                     evidence=f"rollback of promotion proposal {proposal_id}"):
        if p["kind"] == "rebind_page_range":
            before = json.loads(snap[("before", "historical_documents", str(hd_id))])
            conn.execute(
                f"UPDATE historical_documents SET {', '.join(f'{c} = ?' for c in _REBIND_SET)} WHERE id = ?",
                [before[c] for c in _REBIND_SET] + [hd_id])
        else:
            conn.execute("UPDATE document_assertions SET retracted_at = ? WHERE proposal_id = ? AND retracted_at IS NULL",
                         (_now(), proposal_id))
        conn.execute("UPDATE promotion_proposals SET state = 'rolled_back', rolled_back_by = ?, rolled_back_at = ? "
                     "WHERE id = ?", (by, _now(), proposal_id))
    restored = conn.execute("SELECT * FROM historical_documents WHERE id = ?", (hd_id,)).fetchone()
    before_row = json.loads(snap[("before", "historical_documents", str(hd_id))])
    return {"restored": all(str(dict(restored)[c]) == str(before_row[c]) for c in _REBIND_SET),
            "charges_unchanged": sorted(_charge_state(conn, hd_id)) == after_charges}


def redline_promotion_block(conn: sqlite3.Connection, version_id: int) -> dict | None:
    """Why a charge promotion for this version must be acknowledged as redline, or ``None``.

    Reads the version's bound pages for redline markup and checks for a
    ``redline_duplicate_of`` assertion. Used by ``provenance promote`` and the
    rehearsal; the bulk ingestion promotion path is not changed (see the D2 report).
    """
    row = conn.execute(
        "SELECT hd.id, hd.local_path, hd.start_page, hd.end_page FROM tariff_versions tv "
        "JOIN historical_documents hd ON hd.id = tv.historical_document_id WHERE tv.id = ?", (version_id,)).fetchone()
    if row is None or not row[1]:
        return None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'document_assertions'").fetchone():
        asserted = conn.execute("SELECT target_historical_document_id FROM document_assertions WHERE "
                                "historical_document_id = ? AND assertion = 'redline_duplicate_of' "
                                "AND retracted_at IS NULL", (row[0],)).fetchone()
        if asserted:
            return {"reason": "redline_duplicate_assertion", "duplicate_of": asserted[0]}
    reader = PageRedlineReader()
    try:
        evidence = summarize_page_evidence(reader.pages(row[1], row[2] or 1, row[3] or row[2] or 1))
    finally:
        reader.close()
    if evidence["verdict"] == "redline":
        return {"reason": "redline_markup_on_bound_pages", "evidence": evidence}
    return None


def proposals_summary(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, kind, historical_document_id, tariff_version_id, state, state_reason, proposed_by, approved_by, "
        "applied_by, rolled_back_by FROM promotion_proposals ORDER BY id")]
