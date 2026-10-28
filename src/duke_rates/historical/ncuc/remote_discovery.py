"""Network discovery for typed gaps: docket-scoped orders and missing tariff documents (Phase 2E D2).

Planning, execution and retrieval are separate steps. Each is bounded,
resumable through the search ledger, and auditable.

1. **Plan** (:func:`plan_network_searches`, read-only). Targets come from:

   * ``missing_regulatory_order`` gaps (channel ``portal_orders``): docket,
     plus a filed-date window around the anchor version's printed order date,
     or else its effective date;
   * blank order-date clues (``Order dated ____``) on bound sheets: the
     sheet's docket, around its effective date (``inferred``);
   * page-searchable gaps whose **local** search completed against the current
     archive state and found nothing (``searched_not_found``), via
     ``portal_params`` (docket and window) and ``zoom`` (printed revision text).

   A gap without a completed local search is never planned (local first).
   Queries are canonical: gaps that need the same query share one, and a query
   whose ``params_hash`` already completed is skipped. Explicit gaps go first.

2. **Execute** (:func:`execute_plan`). Runs at most ``max_queries`` queries
   through :class:`DocumentParamSearcher.search_with_outcome` or the public
   search. Each is recorded in ``search_attempts`` with its stop reason. The
   run stops at the first blocked search: no retry loop against Cloudflare.
   Every listed document becomes a ``remote_document_candidates`` row
   (``listed``) with its search attempt, query hash, portal ids/URLs and a
   per-criterion score. A docket match never makes a candidate more than
   ``listed``.

3. **Retrieve and check** (:func:`retrieve_and_check`). Downloads are
   deduplicated by URL and SHA-256 against the archive. The candidate's own
   pages are then read: printed docket, order wording and dates, or the
   expected revision. The candidate moves to ``needs_review``. Only
   :func:`review_candidate`, with a reviewer name, sets ``linked`` or
   ``rejected``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from duke_rates.historical.ncuc.search_ledger import SearchLedger, canonical_params

PLANNER_VERSION = "remote_discovery@1.0.0"
COMPANY = {"DEP": "Duke Energy Progress", "DEC": "Duke Energy Carolinas"}
NETWORK_GAP_TYPES = (
    "missing_official_clean_tariff",
    "missing_predecessor_revision",
    "missing_successor_document",
    "missing_intermediate_revision",
)
_TYPE_RANK = {t: i for i, t in enumerate(("missing_regulatory_order",) + NETWORK_GAP_TYPES)}
_DOCKET_RE = re.compile(r"^([A-Z]+)-(\d+)(?: Sub (\d+))?$")
_ORDER_WORDS = re.compile(r"\bORDER\b", re.I)
#: Order captions print the docket in capitals ("DOCKET NO. E-2, SUB 1300"), which the
#: tariff-footer clue patterns do not read.
_CAPTION_DOCKET = re.compile(r"DOCKET\s+NO\.?\s*([A-Z]+-\d+)\s*,?\s*SUB\s+(\d+)", re.I)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _mdy(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def _shift(iso: str, days: int) -> date:
    return date.fromisoformat(iso[:10]) + timedelta(days=days)


def company_for(docket: str | None, utility: str | None) -> str:
    """Company filter for a docket. Commission-wide (M-100, ...) dockets get none: they are not filed by Duke."""
    m = _DOCKET_RE.match(docket or "")
    if m and m.group(1) == "E":
        return {"2": COMPANY["DEP"], "7": COMPANY["DEC"]}.get(m.group(2), COMPANY.get(utility or "", ""))
    if m:
        return ""
    return COMPANY.get(utility or "", "")


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class PlannedQuery:
    channel: str                      # portal_orders | portal_params | zoom
    params: dict
    params_hash: str
    priority: tuple
    targets: list[dict] = field(default_factory=list)   # one per gap / clue this query serves
    status: str = "planned"           # planned | already_completed

    def to_json(self) -> dict:
        return {"channel": self.channel, "params": self.params, "params_hash": self.params_hash,
                "priority": list(self.priority), "status": self.status, "targets": self.targets}


def local_search_complete(conn: sqlite3.Connection, gap_id: int) -> bool:
    """A completed ``local`` attempt for this gap against the current archive state and resolver."""
    from duke_rates.historical.ncuc.local_gap_resolution import RESOLVER_VERSION, archive_state

    state = archive_state(conn)
    for (params_json,) in conn.execute(
        "SELECT params_json FROM search_attempts WHERE gap_id = ? AND channel = 'local' "
        "AND outcome IN ('hits', 'no_results')", (gap_id,)
    ):
        params = json.loads(params_json)["params"]
        if params.get("resolver") == RESOLVER_VERSION.lower() and params.get("archive_state") == {
            k: (v.lower() if isinstance(v, str) else v) for k, v in state.items()
        }:
            return True
    return False


def _version_clue_dates(conn: sqlite3.Connection, version_id: int | None, kind: str) -> list[str]:
    if version_id is None:
        return []
    return sorted({r[0].split("/")[0] for r in conn.execute(
        "SELECT DISTINCT c.normalized_value FROM regulatory_clues c JOIN clue_associations a ON a.clue_id = c.id "
        "WHERE a.tariff_version_id = ? AND c.clue_kind = ? AND c.normalized_value IS NOT NULL "
        "AND c.normalization IN ('exact', 'normalized', 'ocr_repaired')", (version_id, kind))})


def _stored_effective(conn: sqlite3.Connection, version_id: int | None) -> str | None:
    if version_id is None:
        return None
    row = conn.execute("SELECT effective_start FROM tariff_versions WHERE id = ?", (version_id,)).fetchone()
    return row[0][:10] if row and row[0] else None


def _order_window(conn: sqlite3.Connection, version_id: int | None) -> tuple[str, str, str] | None:
    """Filed-date window for the order, only around a printed order date.

    Without one the search is docket-only: a sheet effective in 2024 may cite a
    2013 order, so a window guessed from the effective date could turn a real
    order into a completed ``no_results``, which is false negative evidence.
    """
    order_dates = _version_clue_dates(conn, version_id, "order_date")
    if order_dates:
        return _mdy(_shift(order_dates[0], -7)), _mdy(_shift(order_dates[-1], 7)), "printed_order_date"
    return None


def _add(queries: dict, channel: str, params: dict, priority: tuple, target: dict) -> None:
    _, digest = canonical_params(channel, params)
    q = queries.get(digest)
    if q is None:
        q = queries[digest] = PlannedQuery(channel, params, digest, priority)
    q.priority = min(q.priority, priority)
    q.targets.append(target)


def plan_network_searches(conn: sqlite3.Connection, *, include_blank_order_clues: bool = True,
                          include_inferred: bool = True) -> dict:
    """Plan every network query the gap inventory justifies. Read-only."""
    from duke_rates.historical.ncuc.local_gap_resolution import reconciled_window

    queries: dict[str, PlannedQuery] = {}
    skipped: dict[str, int] = defaultdict(int)
    gaps = conn.execute(
        "SELECT * FROM lineage_gaps_typed WHERE active = 1 AND status = 'searched_not_found' ORDER BY id"
    ).fetchall()
    for gap in gaps:
        if gap["evidence_basis"] == "inferred" and not include_inferred:
            skipped["inferred_excluded"] += 1
            continue
        if not local_search_complete(conn, gap["id"]):
            skipped["local_search_not_complete"] += 1
            continue
        rank = (0 if gap["evidence_basis"] == "explicit" else 1, _TYPE_RANK.get(gap["gap_type"], 9), gap["id"])
        target = {"gap_id": gap["id"], "gap_uid": gap["gap_uid"], "gap_type": gap["gap_type"],
                  "basis": gap["evidence_basis"], "family_key": gap["family_key"], "utility": gap["utility"],
                  "expected_revision": gap["expected_revision"]}
        dockets = json.loads(gap["docket_candidates_json"] or "[]")
        if gap["gap_type"] == "missing_regulatory_order":
            docket = json.loads(gap["expected_json"] or "{}").get("docket") or (dockets[0] if dockets else None)
            if not docket:
                skipped["order_gap_without_docket"] += 1
                continue
            window = _order_window(conn, gap["anchor_version_id"])
            params = {"docket_number": docket, "company_name": company_for(docket, gap["utility"]),
                      "filing_types": ["ORDER"], "date_after": window[0] if window else "",
                      "date_before": window[1] if window else ""}
            _add(queries, "portal_orders", params, rank,
                 target | {"docket": docket, "window_basis": window[2] if window else "docket_only"})
            continue
        if gap["gap_type"] not in NETWORK_GAP_TYPES:
            skipped["gap_type_not_network_searchable"] += 1
            continue
        w = reconciled_window(conn, gap)["window"]
        after = _mdy(_shift(w[0], -120)) if w[0] else ""
        before = _mdy(_shift(w[1], 31)) if w[1] else ""
        planned = False
        for docket in dockets[:3]:
            params = {"docket_number": docket, "company_name": company_for(docket, gap["utility"]),
                      "filing_types": [], "date_after": after, "date_before": before}
            _add(queries, "portal_params", params, rank, target | {"docket": docket, "window": w})
            planned = True
        if gap["expected_revision"] and (after or before or dockets):
            query_text = f'"{gap["expected_revision"]}" {COMPANY.get(gap["utility"] or "", "Duke Energy")}'
            _add(queries, "zoom", {"query_text": query_text, "docket_hint": dockets[0] if dockets else None},
                 rank, target | {"window": w})
            planned = True
        if not planned:
            skipped["no_docket_window_or_revision"] += 1

    if include_blank_order_clues:
        for vid, family, docket in conn.execute(
            """
            SELECT DISTINCT a.tariff_version_id, a.family_key, d.normalized_value
            FROM regulatory_clues b
            JOIN clue_associations a ON a.clue_id = b.id
            JOIN regulatory_clues d ON d.file_sha256 = b.file_sha256 AND d.page = b.page AND d.clue_kind = 'docket'
                                    AND d.normalized_value IS NOT NULL
            WHERE b.clue_kind = 'order_date' AND b.normalization = 'blank' AND a.tariff_version_id IS NOT NULL
            """
        ):
            window = _order_window(conn, vid)
            util = "DEP" if "progress" in (family or "") else "DEC" if "carolinas" in (family or "") else None
            params = {"docket_number": docket, "company_name": company_for(docket, util),
                      "filing_types": ["ORDER"], "date_after": window[0] if window else "",
                      "date_before": window[1] if window else ""}
            _add(queries, "portal_orders", params, (2, 0, vid),
                 {"tariff_version_id": vid, "family_key": family, "basis": "blank_order_date_clue",
                  "docket": docket, "window_basis": window[2] if window else "docket_only"})

    ledger = SearchLedger(conn, created_by="planner")
    completed = {ch: ledger.completed_hashes(ch) for ch in ("portal_orders", "portal_params", "zoom")}
    ordered = sorted(queries.values(), key=lambda q: q.priority)
    for q in ordered:
        if q.params_hash in completed[q.channel]:
            q.status = "already_completed"
    by_channel: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for q in ordered:
        by_channel[q.channel][q.status] += 1
    return {
        "planner": PLANNER_VERSION,
        "gaps_considered": len(gaps),
        "skipped": dict(skipped),
        "targets": sum(len(q.targets) for q in ordered),
        "distinct_queries": len(ordered),
        "by_channel": {k: dict(v) for k, v in by_channel.items()},
        "queries": [q.to_json() for q in ordered],
    }


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


def score_listing(row: Any, target: dict, params: dict) -> dict:
    """Per-criterion ranking of one search row against what was sought (machine; never authoritative)."""
    desc = f"{getattr(row, 'description', '')} {getattr(row, 'synopsis', '')}"
    docket = target.get("docket")
    row_docket = (getattr(row, "docket_number", "") or "").replace(",", "")
    s = {
        "docket_match": None if not docket else row_docket.upper() == docket.upper(),
        "order_wording": bool(_ORDER_WORDS.search(desc)) or (getattr(row, "doc_type", "") or "").upper() == "ORDER",
        "date_in_window": None,
    }
    filed = getattr(row, "date_filed", "")
    if filed and (params.get("date_after") or params.get("date_before")):
        try:
            d = datetime.strptime(filed, "%m/%d/%Y").date()
            lo = datetime.strptime(params["date_after"], "%m/%d/%Y").date() if params.get("date_after") else None
            hi = datetime.strptime(params["date_before"], "%m/%d/%Y").date() if params.get("date_before") else None
            s["date_in_window"] = (lo is None or d >= lo) and (hi is None or d <= hi)
        except ValueError:
            pass
    rev = (target.get("expected_revision") or "").lower()
    leaf = re.search(r"leaf no\. (\S+)", rev)
    s["mentions_leaf"] = bool(leaf and re.search(rf"\bleaf\s*(?:no\.?)?\s*{re.escape(leaf.group(1))}\b", desc, re.I))
    s["plausible"] = s["docket_match"] is not False and s["date_in_window"] is not False
    return s


def _store_listing(conn: sqlite3.Connection, query: dict, attempt_id: int, row: Any, target: dict) -> int:
    kind = "order" if query["channel"] == "portal_orders" else "tariff_document"
    ref = getattr(row, "document_id", "") or getattr(row, "document_detail_url", "") or getattr(row, "url", "")
    uid = hashlib.sha256(json.dumps([kind, target.get("gap_uid") or target.get("tariff_version_id"), ref]).encode()
                         ).hexdigest()
    listing = {k: v for k, v in getattr(row, "__dict__", {}).items() if isinstance(v, (str, int, float, list, type(None)))}
    now = _now()
    conn.execute(
        """
        INSERT INTO remote_document_candidates (candidate_uid, kind, gap_id, target_json, search_attempt_id, channel,
            params_hash, portal_document_id, detail_url, view_file_urls_json, description, filing_type, docket_number,
            date_filed, listing_json, score_json, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'listed', ?, ?)
        ON CONFLICT(candidate_uid) DO UPDATE SET search_attempt_id = excluded.search_attempt_id,
            listing_json = excluded.listing_json, score_json = excluded.score_json, updated_at = excluded.updated_at
        """,
        (uid, kind, target.get("gap_id"), json.dumps(target, default=str), attempt_id, query["channel"],
         query["params_hash"], getattr(row, "document_id", None) or None,
         getattr(row, "document_detail_url", None) or getattr(row, "url", None),
         json.dumps(getattr(row, "view_file_urls", []) or []), getattr(row, "description", None)
         or getattr(row, "title", None), getattr(row, "doc_type", None), getattr(row, "docket_number", None),
         getattr(row, "date_filed", None), json.dumps(listing, default=str),
         json.dumps(score_listing(row, target, query["params"])), now, now),
    )
    return conn.execute("SELECT id FROM remote_document_candidates WHERE candidate_uid = ?", (uid,)).fetchone()[0]


def _advance_gap(conn: sqlite3.Connection, gap_id: int, channel: str, plausible: bool) -> None:
    gap = conn.execute("SELECT status, searched_channels_json FROM lineage_gaps_typed WHERE id = ?", (gap_id,)).fetchone()
    channels = sorted(set(json.loads(gap[1] or "[]")) | {channel})
    if plausible and gap[0] == "searched_not_found":
        conn.execute("UPDATE lineage_gaps_typed SET status = 'candidate_found', status_reason = ?, "
                     "searched_channels_json = ?, updated_at = ? WHERE id = ?",
                     (f"{channel} listing found; document content not yet read", json.dumps(channels), _now(), gap_id))
    else:
        conn.execute("UPDATE lineage_gaps_typed SET searched_channels_json = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(channels), _now(), gap_id))


def execute_plan(
    conn: sqlite3.Connection,
    plan: dict,
    *,
    created_by: str,
    portal_search: Callable[[dict], Any] | None,
    public_search: Callable[[dict], list] | None = None,
    max_queries: int = 10,
    channels: tuple[str, ...] = ("portal_orders", "portal_params", "zoom"),
) -> dict:
    """Run up to ``max_queries`` planned queries. ``portal_search(params)`` returns a ``DocParamSearchOutcome``."""
    ledger = SearchLedger(conn, created_by=created_by)
    stats: dict[str, int] = defaultdict(int)
    for query in plan["queries"]:
        if query["channel"] not in channels:
            continue
        if stats["queries_run"] >= max_queries:
            stats["queries_deferred"] += 1
            continue
        if ledger.prior_completed(query["channel"], query["params"]):
            stats["repeated_searches_avoided"] += 1
            continue
        gap_ids = [t["gap_id"] for t in query["targets"] if t.get("gap_id")]
        stats["queries_run"] += 1
        with ledger.attempt(query["channel"], query["params"], caller=PLANNER_VERSION,
                            gap_id=gap_ids[0] if gap_ids else None) as attempt:
            if query["channel"] == "zoom":
                if public_search is None:
                    raise RuntimeError("public search not configured")
                rows = public_search(query["params"])
                attempt.finish(rows, refs=[getattr(r, "url", None) for r in rows])
                outcome_kind = attempt.outcome
            else:
                if portal_search is None:
                    raise RuntimeError("portal search not configured")
                outcome = portal_search(query["params"])
                attempt.finish_outcome(outcome)
                rows = outcome.results
                outcome_kind = outcome.ledger_outcome
        stats[f"outcome_{outcome_kind}"] += 1
        for target in query["targets"]:
            ids = [_store_listing(conn, query, attempt.id, row, target) for row in rows]
            stats["candidates_listed"] += len(ids)
            plausible = any(json.loads(r[0])["plausible"] for r in conn.execute(
                f"SELECT score_json FROM remote_document_candidates WHERE id IN ({','.join('?' * len(ids))})", ids)
            ) if ids else False
            if target.get("gap_id") and outcome_kind in ("hits", "no_results"):
                _advance_gap(conn, target["gap_id"], query["channel"], plausible)
        conn.commit()
        if outcome_kind == "blocked":
            stats["stopped_on_block"] = 1  # back off: never loop against a challenge
            break
    return dict(stats)


# ---------------------------------------------------------------------------
# Retrieve, check identity, review
# ---------------------------------------------------------------------------


def _archive_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    for sha, path in conn.execute("SELECT content_hash, local_path FROM historical_documents WHERE content_hash IS NOT NULL"):
        out.setdefault(sha, path)
    for sha, path in conn.execute(
        "SELECT content_hash, local_path FROM ncuc_discovery_records WHERE content_hash IS NOT NULL AND local_path IS NOT NULL"
    ):
        out.setdefault(sha, path)
    return out


def check_identity(pdf_path: str, kind: str, target: dict, *, max_pages: int = 6) -> dict:
    """Read the candidate's first pages: what they print about docket, order, dates, revision."""
    import fitz

    from duke_rates.historical.ncuc.regulatory_clues import extract_page_clues

    found: dict[str, list] = defaultdict(list)
    with fitz.open(pdf_path) as doc:
        pages = len(doc)
        for i in range(min(max_pages, pages)):
            text = doc[i].get_text("text")
            for c in extract_page_clues(text):
                if c.kind in ("docket", "order_date", "effective_date", "revision") and c.normalized_value:
                    found[c.kind].append({"page": i + 1, "printed": c.printed_text, "value": c.normalized_value})
            for m in _CAPTION_DOCKET.finditer(text):
                found["docket"].append({"page": i + 1, "printed": m.group(0),
                                        "value": f"{m.group(1).upper()} Sub {m.group(2)}"})
            if kind == "order" and i == 0:
                found["order_title"] = [ln.strip() for ln in text.splitlines()[:40] if _ORDER_WORDS.search(ln)][:3]
    docket = (target.get("docket") or "").upper()
    dockets = {d["value"].upper() for d in found["docket"]}
    checks = {"pages": pages, "docket_printed": (docket in dockets) if docket else None,
              "order_wording_printed": bool(found.get("order_title")) if kind == "order" else None}
    if kind == "tariff_document" and target.get("expected_revision"):
        checks["expected_revision_printed"] = any(
            target["expected_revision"].lower() in r["printed"].lower() for r in found["revision"])
    return {"checks": checks, "observed": dict(found), "label_source": f"machine:{PLANNER_VERSION}"}


def retrieve_and_check(
    conn: sqlite3.Connection,
    candidate_ids: list[int],
    *,
    download: Callable[[str, Path], int],
    dest_dir: Path,
) -> dict:
    """Download candidates once (dedup by URL and SHA-256), then read their own pages."""
    stats: dict[str, int] = defaultdict(int)
    known = _archive_hashes(conn)
    by_url = {u: (p, s) for u, p, s in conn.execute(
        "SELECT retrieved_from, local_path, file_sha256 FROM remote_document_candidates WHERE file_sha256 IS NOT NULL")}
    dest_dir.mkdir(parents=True, exist_ok=True)
    for cid in candidate_ids:
        row = conn.execute("SELECT * FROM remote_document_candidates WHERE id = ?", (cid,)).fetchone()
        urls = json.loads(row["view_file_urls_json"] or "[]")
        if row["status"] not in ("listed", "retrieved") or not urls:
            stats["skipped_no_file_or_status"] += 1
            continue
        url = urls[0]
        retrieval: dict = {"url": url}
        if url in by_url:
            path, sha = by_url[url]
            retrieval["dedupe"] = "same_url_already_retrieved"
            stats["download_avoided_same_url"] += 1
        else:
            path = dest_dir / f"{row['portal_document_id'] or row['candidate_uid'][:16]}.pdf"
            retrieval["bytes"] = download(url, path)
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            stats["downloaded"] += 1
            if sha in known:
                retrieval["dedupe"] = f"same_sha256_as_archive:{known[sha]}"
                stats["already_in_archive"] += 1
            by_url[url] = (str(path), sha)
        identity = check_identity(str(path), row["kind"], json.loads(row["target_json"]))
        conn.execute(
            "UPDATE remote_document_candidates SET local_path = ?, file_sha256 = ?, retrieved_from = ?, retrieved_at = ?, "
            "retrieval_json = ?, identity_json = ?, status = 'needs_review', updated_at = ? WHERE id = ?",
            (str(path), sha, url, _now(), json.dumps(retrieval), json.dumps(identity), _now(), cid),
        )
        stats["identity_checked"] += 1
    conn.commit()
    return dict(stats)


def review_candidate(conn: sqlite3.Connection, candidate_id: int, *, decision: str, reviewer: str,
                     note: str = "", version_id: int | None = None) -> None:
    """The only path to ``linked`` / ``rejected``. Linking needs a read file and a named version."""
    if decision not in ("linked", "rejected"):
        raise ValueError("decision must be 'linked' or 'rejected'")
    if not reviewer.strip():
        raise ValueError("a reviewer name is required")
    row = conn.execute("SELECT status, file_sha256 FROM remote_document_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"no remote candidate {candidate_id}")
    if decision == "linked" and (row["status"] != "needs_review" or not row["file_sha256"] or version_id is None):
        raise ValueError("only a retrieved, identity-checked candidate can be linked, to a named tariff version")
    conn.execute(
        "UPDATE remote_document_candidates SET status = ?, linked_version_id = ?, reviewed_by = ?, reviewed_at = ?, "
        "review_note = ?, updated_at = ? WHERE id = ?",
        (decision, version_id if decision == "linked" else None, reviewer, _now(), note, _now(), candidate_id),
    )
    conn.commit()


def discovery_summary(conn: sqlite3.Connection) -> dict:
    q = conn.execute
    return {
        "remote_candidates": [dict(zip(("kind", "status", "n"), r)) for r in q(
            "SELECT kind, status, COUNT(*) FROM remote_document_candidates GROUP BY 1, 2 ORDER BY 1, 2")],
        "gap_status": [dict(zip(("gap_type", "status", "n"), r)) for r in q(
            "SELECT gap_type, status, COUNT(*) FROM lineage_gaps_typed WHERE active = 1 "
            "AND gap_type IN ('missing_regulatory_order', " + ", ".join(f"'{t}'" for t in NETWORK_GAP_TYPES) +
            ") GROUP BY 1, 2 ORDER BY 1, 2")],
    }
