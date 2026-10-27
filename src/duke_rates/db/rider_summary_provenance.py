"""Versioned rider-summary extraction (Phase 2C) on the Phase 2A run model.

A run's target is one rider-summary *source*: a ``(docket_dir, source_pdf)``
pair in ``rider_summary_sources``. Its output is a set of blocks
(``extracted_rider_blocks``) with their line items
(``extracted_rider_line_items``), immutable once the run is sealed.

``rider_summary_blocks`` / ``rider_line_items`` stay the read model that the
residential timeline and analytics query. They change only when a run is
promoted (``provenance.promote``), which logs every replaced block and item
so ``provenance.rollback`` restores them with their original ids. Out-of-band
updates and deletes of the read model are captured in
``rider_summary_revisions`` by triggers.

Typical flow::

    run_ids = record_rider_summary_runs(conn, records, created_by="loader")
    compare_run(conn, run_ids[0])          # vs. the live blocks of that source
    promote(conn, run_ids[0], promoted_by="alice", reason="reviewed")
    rollback(conn, "rider_summary_source", source_id, by="alice", reason="...")
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from duke_rates.db.provenance_schema import RIDER_BLOCK_COLUMNS, RIDER_ITEM_COLUMNS

TARGET_KIND = "rider_summary_source"
_VALUE_TOLERANCE = 1e-9
_CHECKSUM_TOLERANCE = 0.002


def _now() -> str:
    return datetime.now(UTC).isoformat()


def source_id(conn: sqlite3.Connection, docket_dir: str, source_pdf: str, *, create: bool = True) -> int | None:
    row = conn.execute(
        "SELECT id FROM rider_summary_sources WHERE docket_dir = ? AND source_pdf = ?",
        (docket_dir, source_pdf),
    ).fetchone()
    if row is not None:
        return int(row[0])
    if not create:
        return None
    cursor = conn.execute(
        "INSERT INTO rider_summary_sources(docket_dir, source_pdf, created_at) VALUES (?, ?, ?)",
        (docket_dir, source_pdf, _now()),
    )
    return int(cursor.lastrowid)


def _source(conn: sqlite3.Connection, sid: int) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM rider_summary_sources WHERE id = ?", (sid,)).fetchone()
    if row is None:
        from duke_rates.db.provenance import ProvenanceError

        raise ProvenanceError(f"rider summary source {sid} does not exist")
    return row


# ---------------------------------------------------------------------------
# Recording runs
# ---------------------------------------------------------------------------


def _clean(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _item_evidence(item: dict) -> tuple[int | None, str | None, str | None]:
    ev = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    values = ev.get("values") or ev.get("cells") or []
    first = values[0] if values else {}
    page = ev.get("page") or first.get("page")
    bbox = ev.get("bbox") or first.get("bbox")
    printed = ev.get("printed_value_text") or first.get("text")
    quote = ev.get("text") or ev.get("quote")
    if quote and printed and printed not in quote:
        quote = None  # a quote must print the value
    return (int(page) if page is not None else None), (json.dumps(bbox) if bbox else None), quote


def record_rider_summary_runs(
    conn: sqlite3.Connection,
    records: list[dict],
    *,
    created_by: str,
    utility: str | None = None,
    parser_name: str = "rider_summary",
    parser_version: str | None = None,
    extraction_method: str = "rider_summary_parse",
    config: dict[str, Any] | None = None,
) -> list[int]:
    """One sealed candidate run per (docket_dir, source_pdf) in serialized records.

    ``records`` are ``RiderSummaryResult``-shaped dicts, as
    ``ncuc_loader.persist_rider_summary_records`` takes them. Blocks the
    parser could not tie to a rate class are kept out, as the loader does,
    and counted in the run metrics. The read model is not touched.
    """
    from duke_rates.db.ncuc_loader import (
        _docket_dir_from_pdf,
        _infer_utility_from_source,
        _normalize_date,
    )
    from duke_rates.db.provenance import git_producer_version, seal_run, start_run

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for rec in records:
        source_pdf = rec.get("source_pdf", "")
        groups[(_docket_dir_from_pdf(source_pdf), source_pdf)].append(rec)

    run_ids: list[int] = []
    for (docket_dir, source_pdf), recs in groups.items():
        with conn:
            sid = source_id(conn, docket_dir, source_pdf)
        run_id = start_run(
            conn,
            target_kind=TARGET_KIND,
            target_id=sid,
            parser_name=parser_name,
            parser_version=parser_version or git_producer_version(),
            extraction_method=extraction_method,
            created_by=created_by,
            source_document_kind="rider_summary_source",
            source_document_id=sid,
            source_path=source_pdf if source_pdf and _exists(source_pdf) else None,
            config=config,
        )
        skipped = 0
        ordinal = 0
        with conn:
            for rec in recs:
                utility_value = utility or _infer_utility_from_source(source_pdf, docket_dir)
                effective_date = _normalize_date(rec.get("effective_date") or "")
                for rc in rec.get("rate_classes", []):
                    if rc.get("ambiguous_rate_class"):
                        skipped += 1
                        continue
                    cursor = conn.execute(
                        """
                        INSERT INTO extracted_rider_blocks (
                            run_id, ordinal, docket_dir, source_pdf, leaf_no, effective_date,
                            docket_number, order_date, supersedes, rate_class,
                            applicable_schedules_json, total_cents_per_kwh, total_dollars_per_kw,
                            utility, checksum_ok, evidence_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id, ordinal, docket_dir, source_pdf, rec.get("leaf_no"),
                            effective_date, rec.get("docket_number"), rec.get("order_date"),
                            rec.get("supersedes"), rc.get("rate_class", ""),
                            json.dumps(rc.get("applicable_schedules") or []),
                            _clean(rc.get("total_cents_per_kwh")),
                            _clean(rc.get("total_dollars_per_kw")), utility_value,
                            None if rc.get("checksum_ok") is None else int(bool(rc.get("checksum_ok"))),
                            json.dumps(rc.get("evidence"), default=str) if rc.get("evidence") else None,
                        ),
                    )
                    ordinal += 1
                    block_id = int(cursor.lastrowid)
                    for n, item in enumerate(rc.get("line_items", [])):
                        page, bbox, quote = _item_evidence(item)
                        conn.execute(
                            """
                            INSERT INTO extracted_rider_line_items (
                                run_id, block_id, ordinal, label, rider_code, cents_per_kwh,
                                dollars_per_kw, line_effective_date, is_section_header,
                                is_subtotal, is_total, page_number, page_status, bbox_json,
                                source_quote, quote_status, evidence_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                run_id, block_id, n, item.get("label", ""), item.get("rider_code"),
                                _clean(item.get("cents_per_kwh")), _clean(item.get("dollars_per_kw")),
                                item.get("effective_date"),
                                int(bool(item.get("is_section_header"))),
                                int(bool(item.get("is_subtotal"))), int(bool(item.get("is_total"))),
                                page, "available" if page is not None else "unavailable", bbox,
                                quote, "available" if quote else "unavailable",
                                json.dumps(item.get("evidence"), default=str) if item.get("evidence") else None,
                            ),
                        )
        seal_run(conn, run_id, metrics={"blocks": ordinal, "ambiguous_blocks_skipped": skipped})
        run_ids.append(run_id)
    return run_ids


def _exists(path: str) -> bool:
    from pathlib import Path

    try:
        return Path(path).is_file()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Validation (called from provenance.seal_run)
# ---------------------------------------------------------------------------


def validate_run(conn: sqlite3.Connection, run_id: int) -> tuple[int, list[dict[str, Any]]]:
    """(block count, run issues) for a rider-summary run."""
    blocks = conn.execute(
        "SELECT * FROM extracted_rider_blocks WHERE run_id = ? ORDER BY ordinal", (run_id,)
    ).fetchall()
    items = defaultdict(list)
    for item in conn.execute(
        "SELECT * FROM extracted_rider_line_items WHERE run_id = ? ORDER BY block_id, ordinal", (run_id,)
    ):
        items[item["block_id"]].append(item)
    issues: list[dict[str, Any]] = []
    keys = defaultdict(list)
    for b in blocks:
        keys[(b["rate_class"], b["effective_date"])].append(b)
        components = [
            i for i in items[b["id"]]
            if not i["is_total"] and not i["is_section_header"] and not i["is_subtotal"]
        ]
        for total, column in (
            (b["total_cents_per_kwh"], "cents_per_kwh"),
            (b["total_dollars_per_kw"], "dollars_per_kw"),
        ):
            values = [i[column] for i in components if i[column] is not None]
            if total is not None and values and abs(sum(values) - total) > _CHECKSUM_TOLERANCE:
                issues.append({
                    "level": "warning",
                    "code": "rider_total_mismatch",
                    "detail": f"{b['rate_class']!r} {column}: components {sum(values):.4f} vs TOTAL {total}",
                })
        if not items[b["id"]]:
            issues.append({"level": "warning", "code": "rider_block_empty", "detail": f"{b['rate_class']!r} has no line items"})
        evidence = json.loads(b["evidence_json"]) if b["evidence_json"] else {}
        if evidence.get("unvouched_table_rows"):
            issues.append({
                "level": "warning",
                "needs_review": True,
                "code": "parser_conflict",
                "detail": f"{b['rate_class']!r}: table-only rows not vouched by the TOTAL",
            })
    for (rate_class, eff), dupes in keys.items():
        if len(dupes) > 1:
            issues.append({
                "level": "warning",
                "needs_review": True,
                "code": "rider_duplicate_block",
                "detail": f"{rate_class!r} effective {eff} appears {len(dupes)} times",
            })
    return len(blocks), issues


# ---------------------------------------------------------------------------
# Live state, comparison
# ---------------------------------------------------------------------------


def _live_rows(conn: sqlite3.Connection, sid: int) -> tuple[list[dict], list[dict]]:
    src = _source(conn, sid)
    bcols, icols = ", ".join(RIDER_BLOCK_COLUMNS), ", ".join(f"li.{c}" for c in RIDER_ITEM_COLUMNS)
    blocks = [
        dict(zip(RIDER_BLOCK_COLUMNS, tuple(r), strict=True))
        for r in conn.execute(
            f"SELECT {bcols} FROM rider_summary_blocks WHERE docket_dir = ? AND source_pdf = ? ORDER BY id",
            (src["docket_dir"], src["source_pdf"]),
        )
    ]
    ids = [b["id"] for b in blocks]
    items: list[dict] = []
    if ids:
        marks = ",".join("?" * len(ids))
        items = [
            dict(zip(RIDER_ITEM_COLUMNS, tuple(r), strict=True))
            for r in conn.execute(
                f"SELECT {icols} FROM rider_line_items li WHERE li.block_id IN ({marks}) ORDER BY li.id", ids
            )
        ]
    return blocks, items


def state_sha(conn: sqlite3.Connection, sid: int) -> str:
    from duke_rates.db.provenance import canonical_json, sha256_text

    blocks, items = _live_rows(conn, sid)
    return sha256_text(canonical_json({"blocks": blocks, "items": items}))


def _block_key(b) -> str:
    name = re.sub(r"[^a-z0-9]+", " ", (b["rate_class"] or "").lower().replace("*", "")).strip()
    return f"{name}|{b['effective_date']}"


def _item_keys(items) -> list[str]:
    keys, seen = [], defaultdict(int)
    for i in items:
        base = (
            f"code:{i['rider_code']}|{'sub' if i['is_subtotal'] else 'tot' if i['is_total'] else 'row'}"
            if i["rider_code"]
            else "label:" + " ".join(re.sub(r"[^a-z0-9]+", " ", (i["label"] or "").lower()).split())
        )
        seen[base] += 1
        keys.append(f"{base}#{seen[base]}")
    return keys


def _differs(a, b) -> bool:
    if a is None or b is None:
        return a is not b
    return abs(float(a) - float(b)) > _VALUE_TOLERANCE


def compare_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Diff a rider-summary run against the live blocks of its source."""
    run = conn.execute("SELECT * FROM extraction_runs WHERE id = ?", (run_id,)).fetchone()
    sid = run["target_id"]
    live_blocks, live_items = _live_rows(conn, sid)
    live_by_block = defaultdict(list)
    for i in live_items:
        live_by_block[i["block_id"]].append(i)
    cand_blocks = [dict(r) for r in conn.execute(
        "SELECT * FROM extracted_rider_blocks WHERE run_id = ? ORDER BY ordinal", (run_id,)
    )]
    cand_by_block = defaultdict(list)
    for i in conn.execute(
        "SELECT * FROM extracted_rider_line_items WHERE run_id = ? ORDER BY block_id, ordinal", (run_id,)
    ):
        cand_by_block[i["block_id"]].append(dict(i))
    live_index = defaultdict(list)
    for b in live_blocks:
        live_index[_block_key(b)].append(b)
    added_blocks, value_changes, total_changes, added_items, removed_items = [], [], [], [], []
    unchanged_blocks = 0
    matched_live: set[int] = set()
    for cb in cand_blocks:
        bucket = live_index.get(_block_key(cb))
        if not bucket:
            added_blocks.append({"rate_class": cb["rate_class"], "effective_date": cb["effective_date"]})
            continue
        lb = bucket.pop(0)
        matched_live.add(lb["id"])
        changed = False
        for col in ("total_cents_per_kwh", "total_dollars_per_kw"):
            if _differs(lb[col], cb[col]):
                total_changes.append({"rate_class": cb["rate_class"], "field": col, "change": [lb[col], cb[col]]})
                changed = True
        l_items = dict(zip(_item_keys(live_by_block[lb["id"]]), live_by_block[lb["id"]], strict=True))
        c_items = dict(zip(_item_keys(cand_by_block[cb["id"]]), cand_by_block[cb["id"]], strict=True))
        for key, ci in c_items.items():
            li = l_items.get(key)
            if li is None:
                added_items.append({"rate_class": cb["rate_class"], "label": ci["label"], "cents_per_kwh": ci["cents_per_kwh"], "dollars_per_kw": ci["dollars_per_kw"]})
                changed = True
                continue
            for col in ("cents_per_kwh", "dollars_per_kw"):
                if _differs(li[col], ci[col]):
                    value_changes.append({"rate_class": cb["rate_class"], "label": ci["label"], "field": col, "change": [li[col], ci[col]]})
                    changed = True
        for key, li in l_items.items():
            if key not in c_items:
                removed_items.append({"rate_class": lb["rate_class"], "label": li["label"], "cents_per_kwh": li["cents_per_kwh"]})
                changed = True
        unchanged_blocks += int(not changed)
    removed_blocks = [
        {"rate_class": b["rate_class"], "effective_date": b["effective_date"], "rider_summary_block_id": b["id"]}
        for b in live_blocks if b["id"] not in matched_live
    ]
    return {
        "summary": {
            "active_blocks": len(live_blocks),
            "candidate_blocks": len(cand_blocks),
            "unchanged_blocks": unchanged_blocks,
            "added_blocks": len(added_blocks),
            "removed_blocks": len(removed_blocks),
            "value_changes": len(value_changes),
            "total_changes": len(total_changes),
            "added_items": len(added_items),
            "removed_items": len(removed_items),
        },
        "added_blocks": added_blocks,
        "removed_blocks": removed_blocks,
        "value_changes": value_changes,
        "total_changes": total_changes,
        "added_items": added_items,
        "removed_items": removed_items,
    }


# ---------------------------------------------------------------------------
# Promotion and rollback (called from provenance.promote / rollback)
# ---------------------------------------------------------------------------


def _insert(conn: sqlite3.Connection, table: str, columns: tuple[str, ...], row: dict, *, keep_id: bool) -> int:
    cols = columns if keep_id else columns[1:]
    cursor = conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(row.get(c) for c in cols),
    )
    return int(cursor.lastrowid)


def promote_rider_run(conn, run, current_run_id, promoted_by, reason, diff, operation) -> dict[str, Any]:
    """Replace a source's live blocks with the run's; log everything replaced."""
    from duke_rates.db.provenance import _log_promotion, _set_active, canonical_json

    sid = run["target_id"]
    before_sha = state_sha(conn, sid)
    blocks, items = _live_rows(conn, sid)
    for i in items:
        conn.execute("DELETE FROM rider_line_items WHERE id = ?", (i["id"],))
    for b in blocks:
        conn.execute("DELETE FROM rider_summary_blocks WHERE id = ?", (b["id"],))
    now = _now()
    inserted_blocks: list[int] = []
    inserted_items: list[int] = []
    materialized: list[tuple[int, int]] = []
    cand_items = defaultdict(list)
    for i in conn.execute(
        "SELECT * FROM extracted_rider_line_items WHERE run_id = ? ORDER BY block_id, ordinal", (run["id"],)
    ):
        cand_items[i["block_id"]].append(i)
    for eb in conn.execute(
        "SELECT * FROM extracted_rider_blocks WHERE run_id = ? ORDER BY ordinal", (run["id"],)
    ).fetchall():
        block_id = _insert(conn, "rider_summary_blocks", RIDER_BLOCK_COLUMNS, {**dict(eb), "created_at": now}, keep_id=False)
        inserted_blocks.append(block_id)
        materialized.append((block_id, eb["id"]))
        for ei in cand_items[eb["id"]]:
            row = {**dict(ei), "block_id": block_id, "created_at": now}
            inserted_items.append(_insert(conn, "rider_line_items", RIDER_ITEM_COLUMNS, row, keep_id=False))
    log_id = _log_promotion(
        conn,
        operation_uuid=operation,
        action="promote",
        target_kind=TARGET_KIND,
        target_id=sid,
        from_run_id=current_run_id,
        to_run_id=run["id"],
        at=now,
        by=promoted_by,
        reason=reason,
        diff_json=canonical_json(diff),
        before_state_sha256=before_sha,
        after_state_sha256=state_sha(conn, sid),
        inserted_row_ids_json=canonical_json({"blocks": inserted_blocks, "items": inserted_items}),
        deleted_rows_json=canonical_json({"blocks": blocks, "items": items}),
    )
    conn.executemany(
        "INSERT INTO materialized_rider_blocks(rider_summary_block_id, extracted_block_id, promotion_log_id) VALUES (?, ?, ?)",
        [(b, e, log_id) for b, e in materialized],
    )
    _set_active(conn, TARGET_KIND, sid, run["id"], log_id, promoted_by, reason)
    return {
        "promotion_log_id": log_id,
        "from_run_id": current_run_id,
        "to_run_id": run["id"],
        "deleted_blocks": len(blocks),
        "deleted_items": len(items),
        "inserted_blocks": len(inserted_blocks),
        "inserted_items": len(inserted_items),
        "diff": diff,
    }


def rollback_rider(conn, entry) -> str:
    """Restore the blocks and items a promotion replaced, with their ids. Returns the state sha."""
    from duke_rates.db.provenance import PromotionConflictError

    sid = entry["target_id"]
    if state_sha(conn, sid) != entry["after_state_sha256"]:
        raise PromotionConflictError(
            f"rider summary source {sid} changed after promotion {entry['id']}"
        )
    inserted = json.loads(entry["inserted_row_ids_json"])
    for item_id in inserted.get("items", []):
        conn.execute("DELETE FROM rider_line_items WHERE id = ?", (item_id,))
    for block_id in inserted.get("blocks", []):
        conn.execute("DELETE FROM materialized_rider_blocks WHERE rider_summary_block_id = ?", (block_id,))
        conn.execute("DELETE FROM rider_summary_blocks WHERE id = ?", (block_id,))
    deleted = json.loads(entry["deleted_rows_json"])
    for block in deleted.get("blocks", []):
        _insert(conn, "rider_summary_blocks", RIDER_BLOCK_COLUMNS, block, keep_id=True)
    for item in deleted.get("items", []):
        _insert(conn, "rider_line_items", RIDER_ITEM_COLUMNS, item, keep_id=True)
    return state_sha(conn, sid)
