"""Reviewed binding of an archived, unbound sheet to a tariff version (Phase 2E D3).

D1/D2 found sheets already on disk that no tariff version uses (D1: 16 gaps
``verified`` by page-image review). This is the ``bind_unbound_document``
proposal kind of :mod:`document_promotion`, with the same lifecycle
(propose → approve → apply → rollback) and snapshots.

The mode is decided by the checks, never by the caller alone:

* ``attach``: an existing version has no source document (for example a
  ``pending_document`` bootstrap placeholder). A historical-document row is
  created for the page run and the version points at it.
* ``replace_redline_source``: the existing version's bound pages are a redline
  (page evidence) and the candidate is the clean sheet with the same revision
  and date. The version points at the clean sheet; the redline row is kept.
* ``create_version``: no version of the family prints this revision or starts
  on the printed date. A new version is created with status
  ``bound_pending_charges``: billing selects only ``approved`` versions
  (``select_version_with_coverage``), so a version without reviewed charges
  can never price a bill.

Anything else is blocked: a version at the date that already has a source, a
version that already prints the revision, the same pages already bound, a
redline candidate. **No charge is created or deleted.** Charges come later from
``provenance extract-candidate`` over the bound pages and the validated
``provenance promote``. A gap moves to ``resolved`` only after the apply
re-reads the bound row and its pages successfully; rollback restores the gap.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from duke_rates.historical.ncuc.clue_ledger import file_identity
from duke_rates.historical.ncuc.clue_lineage import family_leaf, family_leaf_hints, family_utility
from duke_rates.historical.ncuc.regulatory_clues import extract_page_clues

BINDING_VERSION = "document_binding@1.0.0"
NEW_VERSION_STATUS = "bound_pending_charges"
_ORDINAL_WORDS = {1: "First", 2: "Second", 3: "Third", 4: "Fourth", 5: "Fifth", 6: "Sixth", 7: "Seventh",
                  8: "Eighth", 9: "Ninth", 10: "Tenth"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _check(ok: bool | None, blocking: bool, detail: Any) -> dict:
    return {"ok": ok, "blocking": blocking, "detail": detail}


def _page_identity(pdf_path: str, start: int, end: int) -> list[dict]:
    """Revision, supersedes, effective date and docket printed on each page (text layer)."""
    import fitz

    out = []
    with fitz.open(pdf_path) as doc:
        for p in range(start, min(end + 1, len(doc) + 1)):
            clues = extract_page_clues(doc[p - 1].get_text("text"))
            out.append({
                "page": p,
                "revision_values": sorted({c.normalized_value for c in clues
                                           if c.kind == "revision" and c.normalized_value}),
                "supersedes_values": sorted({c.normalized_value for c in clues
                                             if c.kind == "supersedes" and c.normalized_value}),
                "revision_text": [c.printed_text for c in clues if c.kind == "revision"][:3],
            })
    return out


def _versions_printing(conn: sqlite3.Connection, family: str, revision_value: str) -> list[int]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'clue_associations'").fetchone():
        return []
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT a.tariff_version_id FROM regulatory_clues r JOIN clue_associations a ON a.clue_id = r.id "
        "WHERE r.clue_kind = 'revision' AND r.role = 'header' AND r.normalized_value = ? AND a.family_key = ? "
        "AND a.tariff_version_id IS NOT NULL", (revision_value, family))]


def evaluate(conn: sqlite3.Connection, params: dict) -> tuple[dict, dict]:
    """All checks for binding pages ``start_page``-``end_page`` of ``source_pdf`` to ``family_key``.

    Returns ``(checks, plan)``; ``plan["mode"]`` is ``None`` when no mode is allowed.
    """
    from duke_rates.historical.ncuc.document_promotion import _identity_checks
    from duke_rates.parse.redline_detector import PageRedlineReader, summarize_page_evidence

    family, path = params["family_key"], params["source_pdf"]
    start, end = int(params["start_page"]), int(params["end_page"])
    pseudo = {"id": None, "family_key": family, "local_path": path, "content_hash": params.get("file_sha256") or ""}
    reader = PageRedlineReader()
    try:
        checks, pages = _identity_checks(conn, pseudo, start, end, reader)
        ident = file_identity(path)
        checks["file_is_the_reviewed_file"] = _check(
            bool(ident.sha256) and ident.sha256 == (params.get("file_sha256") or "").lower(), True,
            {"expected_sha256": params.get("file_sha256"), "computed_sha256": ident.sha256})
        plan: dict[str, Any] = {"mode": None, "target_version_id": None}
        if not checks.get("pages_in_file", {}).get("ok"):
            return checks, plan
        leaf = family_leaf(family) or family_leaf_hints(conn).get(family)
        ids = _page_identity(path, start, end)
        revs = sorted({v for p in ids for v in p["revision_values"] if f":leaf-{leaf}:" in v})
        revision = revs[0] if len(revs) == 1 else None
        expected = params.get("expected_revision_value")
        checks["printed_revision_is_expected"] = _check(
            bool(revision) and (not expected or revision == expected), True,
            {"printed": revs, "expected": expected, "revision_text": ids[0]["revision_text"] if ids else []})
        printed_dates = sorted({d for p in pages for d in p["effective"]})
        effective = printed_dates[0] if len(printed_dates) == 1 else None
        checks["single_printed_effective_date"] = _check(effective is not None, True, {"printed": printed_dates})
        # The run must end where the sheet ends: the next page must not continue the same revision.
        nxt = _page_identity(path, end + 1, end + 1) if end + 1 <= (ident.page_count or 0) else []
        checks["page_range_complete"] = _check(
            not (nxt and revision and revision in nxt[0]["revision_values"]), True,
            {"next_page": nxt[0] if nxt else None})
        supers = sorted({v for p in ids for v in p["supersedes_values"]})
        predecessors = [vid for s in supers for vid in _versions_printing(conn, family, s)]
        checks["supersession_predecessor_on_file"] = _check(
            bool(predecessors) if supers else None, False,
            {"supersedes_printed": supers, "versions_printing_predecessor": sorted(set(predecessors))})
        bound = [dict(r) for r in conn.execute(
            "SELECT id, family_key, start_page, end_page FROM historical_documents WHERE (local_path = ? OR "
            "content_hash = ?) AND COALESCE(start_page, 1) <= ? AND COALESCE(end_page, start_page, 1) >= ?",
            (path, ident.sha256, end, start))]
        checks["pages_not_already_bound_to_family"] = _check(
            not [b for b in bound if b["family_key"] == family], True, {"bound": bound})
        same_rev = _versions_printing(conn, family, revision) if revision else []
        same_rev += [r[0] for r in conn.execute(
            "SELECT id FROM tariff_versions WHERE family_key = ? AND revision_label = ?",
            (family, _display(revision)))] if revision else []
        same_date = [r[0] for r in conn.execute(
            "SELECT id FROM tariff_versions WHERE family_key = ? AND substr(effective_start, 1, 10) = ?",
            (family, effective))] if effective else []
        provenance = [dict(r) for r in conn.execute(
            "SELECT id, docket_number, filing_title, filing_date, discovered_url FROM ncuc_discovery_records "
            "WHERE content_hash = ? OR local_path = ? LIMIT 5", (ident.sha256, path))]
        checks["source_provenance_known"] = _check(bool(provenance), False, {"discovery_records": provenance})
        plan |= {"revision_value": revision, "effective_start": effective, "printed_dates": printed_dates,
                 "same_revision_versions": sorted(set(same_rev)), "same_date_versions": sorted(set(same_date)),
                 "provenance": provenance[:1]}
        _choose_mode(conn, params, checks, plan, reader, summarize_page_evidence)
    finally:
        reader.close()
    return checks, plan


def _display(revision_value: str | None) -> str | None:
    if not revision_value:
        return None
    state, leaf, rev = revision_value.split(":")
    n = int(rev.split("-")[1])
    word = "Original" if n == 0 else f"{_ORDINAL_WORDS.get(n, str(n))} Revised"
    return f"{state} {word} Leaf No. {leaf.split('-')[1]}"


def _choose_mode(conn, params, checks, plan, reader, summarize) -> None:
    family = params["family_key"]
    target = params.get("target_version_id")
    candidates = sorted(set(plan["same_revision_versions"]) | set(plan["same_date_versions"]) | (
        {int(target)} if target else set()))
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT tv.id, tv.family_key, tv.historical_document_id, tv.effective_start, tv.status, hd.local_path, "
        f"hd.start_page, hd.end_page FROM tariff_versions tv LEFT JOIN historical_documents hd "
        f"ON hd.id = tv.historical_document_id WHERE tv.id IN ({','.join('?' * len(candidates)) or 'NULL'})",
        candidates)}
    detail: dict[str, Any] = {"existing_versions": [dict(r) for r in rows.values()]}
    mode = None
    if not rows:
        mode = "create_version"
    elif len(rows) == 1:
        (v,) = rows.values()
        plan["target_version_id"] = v["id"]
        if v["family_key"] != family:
            detail["reason"] = "target version belongs to another family"
        elif v["historical_document_id"] is None:
            mode = "attach"
        else:
            ev = summarize(reader.pages(v["local_path"], v["start_page"] or 1, v["end_page"] or v["start_page"] or 1)) \
                if v["local_path"] and file_identity(v["local_path"]).sha256 else {"verdict": "unknown"}
            detail["current_source_page_evidence"] = {k: ev.get(k) for k in ("verdict", "redline_pages")}
            if ev["verdict"] == "redline":
                mode = "replace_redline_source"
            else:
                detail["reason"] = ("an existing version already has a clean source for this revision/date; "
                                    "record a lineage link or a duplicate instead of binding")
    else:
        detail["reason"] = "several existing versions match this revision/date; resolve them first"
    if mode == "create_version" and target:
        mode, detail["reason"] = None, "target version given but it does not match the printed revision/date"
    plan["mode"] = mode
    checks["binding_mode_allowed"] = _check(mode is not None, True, detail | {"mode": mode})


# ---------------------------------------------------------------------------
# Apply / rollback (called by document_promotion inside its transaction)
# ---------------------------------------------------------------------------


def _family_defaults(conn: sqlite3.Connection, family: str) -> dict:
    row = conn.execute("SELECT category, company, state FROM historical_documents WHERE family_key = ? "
                       "GROUP BY category, company, state ORDER BY COUNT(*) DESC LIMIT 1", (family,)).fetchone()
    fam = conn.execute("SELECT * FROM tariff_families WHERE family_key = ?", (family,)).fetchone()
    title = (fam["title"] if fam is not None and "title" in fam.keys() else None) or family
    util = family_utility(family)
    return {"category": row["category"] if row else "rate", "company": row["company"] if row else
            {"DEP": "progress", "DEC": "carolinas"}.get(util or "", None), "state": row["state"] if row else "NC",
            "title": title}


def apply_binding(conn: sqlite3.Connection, proposal_id: int, params: dict, plan: dict, applied_by: str) -> dict:
    family, path = params["family_key"], params["source_pdf"]
    ident = file_identity(path)
    d = _family_defaults(conn, family)
    now = _now()
    prov = (plan.get("provenance") or [{}])[0]
    archived = f"local-binding:{ident.sha256}#pages={params['start_page']}-{params['end_page']}"
    hd_id = conn.execute(
        "INSERT INTO historical_documents (family_key, title, state, company, category, kind, canonical_url, "
        "archived_url, snapshot_timestamp, local_path, content_hash, content_type, revision_label, leaf_no, "
        "effective_start, retrieved_at, metadata_json, start_page, end_page, evidence_json, status) "
        "VALUES (?, ?, ?, ?, ?, 'pdf', ?, ?, ?, ?, ?, 'application/pdf', ?, ?, ?, ?, ?, ?, ?, ?, 'approved')",
        (family, f"{d['title']} ({_display(plan['revision_value'])})", d["state"], d["company"], d["category"],
         prov.get("discovered_url") or archived, archived, now, path, ident.sha256, _display(plan["revision_value"]),
         family_leaf(family) or family_leaf_hints(conn).get(family), plan["effective_start"], now,
         json.dumps({"bound_by": BINDING_VERSION, "proposal_id": proposal_id, "discovery_record": prov or None,
                     "gap_id": params.get("gap_id"), "candidate_id": params.get("candidate_id")}),
         params["start_page"], params["end_page"],
         json.dumps({"binding": {"printed_revision": plan["revision_value"],
                                 "printed_effective": plan["printed_dates"]}})),
    ).lastrowid
    result: dict[str, Any] = {"mode": plan["mode"], "historical_document_id": hd_id, "charges_created": 0,
                              "charges_deleted": 0}
    if plan["mode"] == "create_version":
        vid = conn.execute(
            "INSERT INTO tariff_versions (family_key, historical_document_id, effective_start, revision_label, "
            "source_type, confidence_score, notes, created_at, leaf_no, source_pdf, status) "
            "VALUES (?, ?, ?, ?, 'reviewed_binding', 0.0, ?, ?, ?, ?, ?)",
            (family, hd_id, plan["effective_start"], _display(plan["revision_value"]),
             f"created by promotion proposal {proposal_id}; no charges until a candidate run is promoted", now,
             family_leaf(family) or family_leaf_hints(conn).get(family), path, NEW_VERSION_STATUS),
        ).lastrowid
        result |= {"tariff_version_id": vid, "created_version": True, "version_status": NEW_VERSION_STATUS}
    else:
        vid = plan["target_version_id"]
        before = conn.execute("SELECT historical_document_id FROM tariff_versions WHERE id = ?", (vid,)).fetchone()[0]
        conn.execute("UPDATE tariff_versions SET historical_document_id = ? WHERE id = ?", (hd_id, vid))
        result |= {"tariff_version_id": vid, "created_version": False, "previous_historical_document_id": before}
    return result


def post_apply_validation(conn: sqlite3.Connection, result: dict, params: dict) -> dict:
    """Read back what the apply wrote: the version points at the new row, and its pages print the revision."""
    row = conn.execute("SELECT tv.historical_document_id, hd.local_path, hd.start_page, hd.end_page, hd.content_hash "
                       "FROM tariff_versions tv JOIN historical_documents hd ON hd.id = tv.historical_document_id "
                       "WHERE tv.id = ?", (result["tariff_version_id"],)).fetchone()
    ok = row is not None and row[0] == result["historical_document_id"] and row[4] == params["file_sha256"]
    pages = _page_identity(row[1], row[2], row[3]) if ok else []
    printed = {v for p in pages for v in p["revision_values"]}
    ok = ok and params.get("expected_revision_value") in (printed | {None})
    return {"ok": bool(ok), "pages_read": len(pages), "printed_revisions": sorted(printed)}


def rollback_binding(conn: sqlite3.Connection, result: dict) -> None:
    vid, hd_id = result["tariff_version_id"], result["historical_document_id"]
    if conn.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (vid,)).fetchone()[0] and \
            result.get("created_version"):
        raise RuntimeError(f"version {vid} has charges now; roll those back first (`provenance rollback`)")
    if result.get("created_version"):
        conn.execute("DELETE FROM tariff_versions WHERE id = ?", (vid,))
    else:
        conn.execute("UPDATE tariff_versions SET historical_document_id = ? WHERE id = ?",
                     (result.get("previous_historical_document_id"), vid))
    conn.execute("DELETE FROM historical_documents WHERE id = ?", (hd_id,))


def params_from_gap_candidate(conn: sqlite3.Connection, candidate_id: int, *,
                              target_version_id: int | None = None) -> dict:
    """Binding parameters from a ``gap_candidates`` row: its file, pages, family and the gap's expected revision.

    The candidate's review state is carried as evidence; it is not a check: a
    hand-verified identity does not make a sheet authoritative.
    """
    row = conn.execute(
        "SELECT gc.*, g.family_key AS gap_family, g.expected_json, g.gap_type FROM gap_candidates gc "
        "JOIN lineage_gaps_typed g ON g.id = gc.gap_id WHERE gc.id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"no gap candidate {candidate_id}")
    token = json.loads(row["expected_json"] or "{}").get("token") or []
    expected = f"{token[1]}:leaf-{token[2]}:rev-{token[3]}" if len(token) == 4 and token[0] == "leaf" else None
    return {"source_pdf": row["source_pdf"], "file_sha256": row["file_sha256"], "start_page": row["start_page"],
            "end_page": row["end_page"], "family_key": row["gap_family"], "expected_revision_value": expected,
            "gap_id": row["gap_id"], "candidate_id": candidate_id, "target_version_id": target_version_id,
            "candidate_review": {"verification_status": row["verification_status"],
                                 "reviewed_by": row["reviewed_by"], "match_strength": row["match_strength"]}}
