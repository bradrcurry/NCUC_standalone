"""Local-first resolution of typed lineage gaps (Phase 2E D1).

Before any network search, look for the missing sheet in what is already on
disk: every native page artifact (no PDF conversion), plus the discovery
records for order gaps. Hits become ``gap_candidates`` rows with the exact
source pages, the matching clue ids, and a per-criterion rationale.

Nothing is bound, imported or promoted. A candidate is a pointer for review:

* ``full`` match: the page prints the expected revision, the gap's utility, and
  an effective date inside the gap's window, and shows no redline/proposal
  marker. The gap moves to ``verification_required``.
* Redline markup is read from the candidate's **own pages** (D2): coloured
  text with bars through or under it, strikeout flags or annotations. A page
  run with redline or suspect markup is never ``full``. When every printed
  occurrence of the identity text is struck through, the identity is not on
  the page: the candidate is kept for audit but marked ``identity_struck`` and
  does not advance the gap.
* The effective-date check uses the gap window, widened to the anchor
  versions' observed dates when ``effective_date_assessments`` reports a
  conflict (D2), so a wrong stored date cannot rule out a candidate.
* ``partial`` match: the revision is printed but a criterion is unknown or
  fails (other than utility). The gap moves to ``candidate_found``.
* Pages naming another utility are dropped (counted, never stored).

Every search is recorded in ``search_attempts`` (channel ``local``). Its
parameters include the archive state, so the same gap is searched again only
when the archive has changed (or ``force`` is set).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from duke_rates.historical.ncuc.clue_ledger import (
    BboxLocator,
    file_identity,
    is_index_page,
    load_pdf_pages,
    write_page_clues,
)
from duke_rates.historical.ncuc.regulatory_clues import EXTRACTOR_VERSION, extract_page_clues
from duke_rates.historical.ncuc.effective_date_reconciliation import evaluation_window
from duke_rates.historical.ncuc.search_ledger import SearchLedger
from duke_rates.parse.redline_detector import PageRedlineReader, summarize_page_evidence

# 1.1.0: redline fingerprints block a full match
# 1.2.0: page-level redline evidence; struck identity text; reconciled date window
RESOLVER_VERSION = "local_gap_resolution@1.2.0"
#: Gap types whose target can be recognised on a page.
PAGE_SEARCHABLE = (
    "missing_predecessor_revision",
    "missing_intermediate_revision",
    "missing_official_clean_tariff",
    "missing_successor_document",
)
_NATIVE = re.compile(r"^page_miner_v(\d+)$")
#: Set by a reviewer (or later): a rerun never moves a gap out of these.
#: ``verification_required`` is a machine status and is re-evaluated (1.2.0).
_LOCKED_STATUSES = ("verified", "resolved")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Archive index: revision/schedule identity -> pages
# ---------------------------------------------------------------------------


@dataclass
class ArchiveIndex:
    pages_by_token: dict
    pages_indexed: int
    state: dict


def archive_state(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM ncuc_page_artifacts WHERE artifact_version LIKE 'page_miner_v%'"
    ).fetchone()
    return {"native_page_artifacts": row[0], "max_page_artifact_id": row[1], "extractor": EXTRACTOR_VERSION}


def build_archive_index(conn: sqlite3.Connection) -> ArchiveIndex:
    """One pass over native page text; keeps, per PDF page, the newest page-miner version."""
    best: dict[tuple[str, int], tuple[int, int]] = {}
    for art_id, source_pdf, version, page in conn.execute(
        "SELECT id, source_pdf, artifact_version, page_number FROM ncuc_page_artifacts "
        "WHERE artifact_version LIKE 'page_miner_v%'"
    ):
        m = _NATIVE.match(version)
        if not m:
            continue
        rank = int(m.group(1))
        key = (source_pdf, page)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, art_id)
    chosen = {art_id for _, art_id in best.values()}
    pages_by_token: dict[tuple, set] = defaultdict(set)
    for art_id, source_pdf, page, text in conn.execute(
        "SELECT id, source_pdf, page_number, text_content FROM ncuc_page_artifacts "
        "WHERE artifact_version LIKE 'page_miner_v%' AND (text_content LIKE '%Leaf%' "
        "OR text_content LIKE '%chedule%' OR text_content LIKE '%CHEDULE%' OR text_content LIKE '%IDER%')"
    ):
        if art_id not in chosen or not text or is_index_page(text):
            continue
        for clue in extract_page_clues(text):
            if clue.kind == "revision" and clue.normalized_value:
                d = clue.normalized
                pages_by_token[("leaf", d["state"], d["leaf"], d["ordinal"])].add((source_pdf, page))
            elif clue.kind == "schedule_code" and re.search(r"-\d+[A-Z]?$", clue.normalized["code"]):
                pages_by_token[("schedule", clue.normalized["code"])].add((source_pdf, page))
    return ArchiveIndex(pages_by_token, len(chosen), archive_state(conn))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _target_tokens(gap: sqlite3.Row) -> list[tuple]:
    expected = json.loads(gap["expected_json"] or "{}")
    if expected.get("token"):
        return [tuple(expected["token"])]
    leaf_match = re.search(r"Leaf No\. (\S+)$", gap["expected_revision"] or "")
    if gap["gap_type"] == "missing_official_clean_tariff" and expected.get("likely_ordinal") is not None and leaf_match:
        return [("leaf", "NC", leaf_match.group(1), expected["likely_ordinal"])]
    if gap["gap_type"] == "missing_successor_document" and expected.get("after_ordinal") is not None and leaf_match:
        return [("leaf", "NC", leaf_match.group(1), expected["after_ordinal"] + 1)]
    return []


def _contiguous(pages: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for p in sorted(pages):
        if ranges and p == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], p)
        else:
            ranges.append((p, p))
    return ranges


def _bound_documents_by_pdf(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = defaultdict(list)
    for hid, path, start, end, family in conn.execute(
        "SELECT id, local_path, start_page, end_page, family_key FROM historical_documents WHERE local_path IS NOT NULL"
    ):
        out[path].append((hid, start or 1, end or start or 10 ** 6, family))
    return out


def reconciled_window(conn: sqlite3.Connection, gap: sqlite3.Row) -> dict:
    """The gap window, widened where an anchor version's stored date disagrees with its sheet."""
    ws, we = gap["window_start"], gap["window_end"]
    basis = []
    successor = gap["known_successor_version_id"] or gap["anchor_version_id"]
    for vid, side in ((gap["known_predecessor_version_id"], "start"), (successor, "end")):
        a = evaluation_window(conn, vid)
        if not a or a["status"] not in ("conflict", "ambiguous") or not a["window"]:
            continue
        if side == "start" and ws is not None and a["window"][0] < ws:
            ws = a["window"][0]
            basis.append({"side": "start", "version_id": vid, "status": a["status"], "stored": a["stored"]})
        if side == "end" and we is not None and a["window"][1] > we:
            we = a["window"][1]
            basis.append({"side": "end", "version_id": vid, "status": a["status"], "stored": a["stored"]})
    return {"window": [ws, we], "widened_by": basis}


def _evaluate(gap: sqlite3.Row, clue_rows: list[sqlite3.Row], window: dict | None = None) -> dict:
    utilities = {r["normalized_value"] for r in clue_rows if r["clue_kind"] == "utility" and r["role"] == "header"}
    gap_utility = gap["utility"]
    utility_match = None if not (utilities and gap_utility) else gap_utility in utilities
    dates = [r["normalized_value"].split("/")[0] for r in clue_rows
             if r["clue_kind"] == "effective_date" and r["normalized_value"]]
    ws, we = (window or {}).get("window") or (gap["window_start"], gap["window_end"])
    in_window = None
    if dates:
        in_window = any((ws is None or d >= ws) and (we is None or d < we) for d in dates)
    redline = [r["id"] for r in clue_rows if r["normalization"] in ("redline_ambiguous", "blank")
               or (r["clue_kind"] == "revision" and json.loads(r["normalized_json"]).get("proposed_marker"))]
    return {"utility_printed": sorted(utilities), "utility_match": utility_match,
            "effective_dates_printed": sorted(set(dates)), "effective_in_window": in_window,
            "window": [ws, we], "stored_window": [gap["window_start"], gap["window_end"]],
            "window_widened_by": (window or {}).get("widened_by", []),
            "redline_or_proposal_clue_ids": redline}


def resolve_gaps_locally(
    conn: sqlite3.Connection,
    *,
    created_by: str,
    gap_ids: list[int] | None = None,
    force: bool = False,
    index: ArchiveIndex | None = None,
) -> dict:
    ledger = SearchLedger(conn, created_by=created_by)
    sql = "SELECT * FROM lineage_gaps_typed WHERE active = 1"
    params: list = []
    if gap_ids:
        sql += f" AND id IN ({','.join('?' * len(gap_ids))})"
        params = list(gap_ids)
    gaps = conn.execute(sql + " ORDER BY id", params).fetchall()
    stats: dict = defaultdict(int)
    index = index or build_archive_index(conn)
    bound = _bound_documents_by_pdf(conn)
    locator = BboxLocator()
    redline_reader = PageRedlineReader()
    try:
        for gap in gaps:
            if gap["gap_type"] == "missing_regulatory_order":
                _resolve_order_gap(conn, ledger, gap, index.state, force, stats)
                continue
            if gap["gap_type"] not in PAGE_SEARCHABLE:
                stats["gaps_not_locally_searchable"] += 1
                continue
            tokens = _target_tokens(gap)
            if not tokens:
                stats["gaps_without_searchable_target"] += 1
                continue
            search_params = {"gap_uid": gap["gap_uid"], "tokens": [list(t) for t in tokens],
                             "window": [gap["window_start"], gap["window_end"]], "utility": gap["utility"],
                             "archive_state": index.state, "resolver": RESOLVER_VERSION}
            if not force and ledger.prior_completed("local", search_params):
                stats["repeated_searches_avoided"] += 1
                continue
            with ledger.attempt("local", search_params, caller=RESOLVER_VERSION, gap_id=gap["id"]) as attempt:
                candidate_ids = _search_pages(conn, gap, tokens, index, bound, locator, attempt.id, stats,
                                              redline_reader)
                attempt.finish(refs=candidate_ids)
            stats["local_searches_run"] += 1
            _update_status(conn, gap, candidate_ids)
    finally:
        locator.close()
        redline_reader.close()
        conn.commit()
    return {"stats": dict(stats), "archive_state": index.state, "pages_indexed": index.pages_indexed}


def _search_pages(conn, gap, tokens, index, bound, locator, attempt_id, stats, redline_reader) -> list[int]:
    anchor_pages = set()
    if gap["anchor_version_id"]:
        row = conn.execute(
            "SELECT hd.local_path, hd.start_page, hd.end_page FROM tariff_versions tv "
            "JOIN historical_documents hd ON hd.id = tv.historical_document_id WHERE tv.id = ?",
            (gap["anchor_version_id"],),
        ).fetchone()
        if row:
            anchor_pages = {(row[0], p) for p in range((row[1] or 1), (row[2] or row[1] or 1) + 1)}
    hits: dict[str, list[int]] = defaultdict(list)
    for token in tokens:
        for source_pdf, page in index.pages_by_token.get(token, ()):
            if (source_pdf, page) not in anchor_pages:
                hits[source_pdf].append(page)
    candidate_ids: list[int] = []
    seen: set[tuple] = set()
    window = reconciled_window(conn, gap)
    for source_pdf, pages in sorted(hits.items()):
        identity = file_identity(source_pdf)
        if identity.sha256 is None:
            stats["candidate_pdfs_unavailable"] += 1
            continue
        pdf_pages = load_pdf_pages(conn, source_pdf, identity)
        if pdf_pages.integrity_error:
            stats["candidate_pdfs_failed_integrity"] += 1
            continue
        for start, end in _contiguous(pages):
            key = (identity.sha256, start, end)
            if key in seen:
                stats["duplicate_file_candidates_collapsed"] += 1
                continue
            seen.add(key)
            clue_ids: list[int] = []
            for p in range(start, end + 1):
                if p in pdf_pages.pages:
                    clue_ids += write_page_clues(conn, source_pdf=source_pdf, identity=identity,
                                                 page=pdf_pages.pages[p], scan_reason="local_resolution",
                                                 locator=locator)
            if not clue_ids:
                stats["candidate_pages_without_verified_text"] += 1
                continue
            rows = conn.execute(
                f"SELECT * FROM regulatory_clues WHERE id IN ({','.join('?' * len(clue_ids))})", clue_ids
            ).fetchall()
            matched_rows = [r for r in rows if r["clue_kind"] in ("revision", "schedule_code")
                            and _row_token(r) in tokens]
            matched = [r["id"] for r in matched_rows]
            struck = [r["id"] for r in matched_rows
                      if redline_reader.text_struck(source_pdf, r["page"], r["printed_text"]) is True]
            match = _evaluate(gap, rows, window)
            match["redline_fingerprint"] = _redline_fingerprint(conn, source_pdf, start, end)
            match["page_redline"] = summarize_page_evidence(redline_reader.pages(source_pdf, start, end))
            match["struck_identity_clue_ids"] = struck
            match["identity_struck"] = bool(matched) and len(struck) == len(matched)
            if match["utility_match"] is False:
                stats["candidates_dropped_other_utility"] += 1
                continue
            full = match["utility_match"] is True and match["effective_in_window"] is True \
                and not match["redline_or_proposal_clue_ids"] and not match["redline_fingerprint"] \
                and match["page_redline"]["verdict"] not in ("redline", "suspect") and not match["identity_struck"]
            bound_here = [
                {"historical_document_id": hid, "family_key": fam}
                for hid, s, e, fam in bound.get(source_pdf, []) if s <= end and e >= start
            ]
            match["bound_elsewhere"] = bound_here
            rationale = (
                f"pages {start}-{end} print {gap['expected_revision'] or tokens[0]}; "
                f"utility={match['utility_match']}, effective_in_window={match['effective_in_window']}, "
                f"redline_or_proposal={bool(match['redline_or_proposal_clue_ids'])}, "
                f"page_redline={match['page_redline']['verdict']}"
                + ("; every printed occurrence of the identity is struck through" if match["identity_struck"] else "")
            )
            uid = hashlib.sha256(json.dumps([gap["gap_uid"], identity.sha256, start, end]).encode()).hexdigest()
            conn.execute(
                """
                INSERT INTO gap_candidates (candidate_uid, gap_id, search_attempt_id, source_pdf, file_sha256,
                    start_page, end_page, family_key_guess, bound_historical_document_ids_json, matched_clue_ids_json,
                    match_json, match_strength, rationale, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_uid) DO UPDATE SET search_attempt_id = excluded.search_attempt_id,
                    matched_clue_ids_json = excluded.matched_clue_ids_json, match_json = excluded.match_json,
                    match_strength = excluded.match_strength, rationale = excluded.rationale
                """,
                (uid, gap["id"], attempt_id, source_pdf, identity.sha256, start, end, gap["family_key"],
                 json.dumps([b["historical_document_id"] for b in bound_here]), json.dumps(matched),
                 json.dumps(match), "full" if full else "partial", rationale, _now()),
            )
            if match["identity_struck"]:
                stats["candidates_identity_struck"] += 1  # kept for audit; does not advance the gap
                continue
            candidate_ids.append(
                conn.execute("SELECT id FROM gap_candidates WHERE candidate_uid = ?", (uid,)).fetchone()[0])
            stats["candidates_full" if full else "candidates_partial"] += 1
            if match["page_redline"]["verdict"] in ("redline", "suspect"):
                stats[f"candidates_page_{match['page_redline']['verdict']}"] += 1
    return candidate_ids


def _redline_fingerprint(conn: sqlite3.Connection, source_pdf: str, start: int, end: int) -> dict | None:
    """The existing redline detector's verdict for these pages, when it flagged them."""
    row = conn.execute(
        """
        SELECT redline_confidence, redline_detector_version FROM document_fingerprints
        WHERE source_pdf = ? AND is_redline_candidate = 1 AND COALESCE(red_is_index_only, 0) = 0
          AND COALESCE(page_start, 1) <= ? AND COALESCE(page_end, 1000000) >= ?
        ORDER BY redline_confidence DESC LIMIT 1
        """,
        (source_pdf, end, start),
    ).fetchone()
    if row is None:
        return None
    return {"confidence": row[0], "label_source": f"machine:redline_detector@{row[1] or 'unknown'}"}


def _row_token(r: sqlite3.Row):
    d = json.loads(r["normalized_json"] or "{}")
    if r["clue_kind"] == "revision" and r["normalized_value"]:
        return ("leaf", d["state"], d["leaf"], d["ordinal"])
    if r["clue_kind"] == "schedule_code":
        return ("schedule", d.get("code"))
    return None


def _resolve_order_gap(conn, ledger, gap, state, force, stats) -> None:
    docket = json.loads(gap["expected_json"] or "{}").get("docket")
    m = re.match(r"([A-Z]+-\d+) Sub (\d+)$", docket or "")
    if not m:
        stats["gaps_without_searchable_target"] += 1
        return
    params = {"gap_uid": gap["gap_uid"], "docket": docket, "archive_state": state,
              "source": "ncuc_discovery_records", "resolver": RESOLVER_VERSION}
    if not force and ledger.prior_completed("local", params):
        stats["repeated_searches_avoided"] += 1
        return
    with ledger.attempt("local", params, caller=RESOLVER_VERSION, gap_id=gap["id"]) as attempt:
        rows = conn.execute(
            """
            SELECT id, local_path, filing_title, filing_date, fetch_status FROM ncuc_discovery_records
            WHERE REPLACE(UPPER(COALESCE(docket_number, '')), ',', '') = ?
              AND filing_classification = 'order'
            """,
            (f"{m.group(1)} SUB {m.group(2)}",),
        ).fetchall()
        refs = []
        for r in rows:
            if not r[1]:
                continue
            identity = file_identity(r[1])
            if identity.sha256 is None:
                continue
            uid = hashlib.sha256(json.dumps([gap["gap_uid"], identity.sha256, "order"]).encode()).hexdigest()
            conn.execute(
                """
                INSERT OR IGNORE INTO gap_candidates (candidate_uid, gap_id, search_attempt_id, source_pdf,
                    file_sha256, start_page, end_page, family_key_guess, match_json, match_strength, rationale,
                    created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 'partial', ?, ?)
                """,
                (uid, gap["id"], attempt.id, r[1], identity.sha256, identity.page_count or 1, gap["family_key"],
                 json.dumps({"discovery_record_id": r[0], "filing_title": r[2], "filing_date": r[3],
                             "fetch_status": r[4]}),
                 f"discovery record {r[0]} is classified 'order' in {docket}; order content not yet read", _now()),
            )
            refs.append(conn.execute("SELECT id FROM gap_candidates WHERE candidate_uid = ?", (uid,)).fetchone()[0])
        attempt.finish(refs=refs)
    stats["local_searches_run"] += 1
    stats["candidates_partial"] += len(refs)
    _update_status(conn, gap, refs)


def _update_status(conn: sqlite3.Connection, gap: sqlite3.Row, candidate_ids: list[int]) -> None:
    """Advance the lifecycle; never moves a gap out of a reviewed state."""
    if gap["status"] in _LOCKED_STATUSES:
        channels = sorted(set(json.loads(gap["searched_channels_json"] or "[]")) | {"local"})
        conn.execute("UPDATE lineage_gaps_typed SET searched_channels_json = ? WHERE id = ?",
                     (json.dumps(channels), gap["id"]))
        return
    strengths = [r[0] for r in conn.execute(
        f"SELECT match_strength FROM gap_candidates WHERE id IN ({','.join('?' * len(candidate_ids))})",
        candidate_ids)] if candidate_ids else []
    if "full" in strengths:
        status, reason = "verification_required", "local candidate matches revision, utility and window; needs source review"
    elif strengths:
        status, reason = "candidate_found", "local candidate prints the revision; other criteria unmet or unknown"
    else:
        status, reason = "searched_not_found", "local archive searched; no page prints the expected identity"
    channels = sorted(set(json.loads(gap["searched_channels_json"] or "[]")) | {"local"})
    conn.execute(
        "UPDATE lineage_gaps_typed SET status = ?, status_reason = ?, searched_channels_json = ?, updated_at = ? "
        "WHERE id = ?",
        (status, reason, json.dumps(channels), _now(), gap["id"]),
    )


def resolution_summary(conn: sqlite3.Connection) -> dict:
    q = conn.execute
    return {
        "gap_status": [dict(zip(("gap_type", "status", "n"), r)) for r in q(
            "SELECT gap_type, status, COUNT(*) FROM lineage_gaps_typed WHERE active = 1 GROUP BY 1, 2 ORDER BY 1, 2")],
        "candidates": dict(q("SELECT match_strength, COUNT(*) FROM gap_candidates GROUP BY 1").fetchall()),
        "gaps_with_candidates": q("SELECT COUNT(DISTINCT gap_id) FROM gap_candidates").fetchone()[0],
        "resolved_gaps": q("SELECT COUNT(*) FROM lineage_gaps_typed WHERE status IN ('verified', 'resolved')").fetchone()[0],
    }
