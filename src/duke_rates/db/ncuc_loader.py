"""Load NCUC ingest results into the SQLite database.

Reads ``ingest_results_all.json`` and ``rider_summaries.json`` produced by
``ingest-ncuc`` and populates:

* ``ncuc_ingest_segments``  — one row per parsed leaf segment
* ``rider_summary_blocks``  — one row per rate-class block from Leaf 600
* ``rider_line_items``      — individual rider line items within each block
* ``rider_descriptions``    — static human-readable descriptions (seeded once)
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from duke_rates.billing.calculators import apply_block_tiers
from duke_rates.billing.season_utils import season_matches
from duke_rates.models.pipeline import (
    DocumentFingerprint,
    ParseAttemptLog,
    ParseReviewOutcome,
)
from duke_rates.utils.duke_company import normalize_duke_company

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def load_ingest_results(
    conn: sqlite3.Connection,
    ingest_json: Path,
    *,
    replace: bool = False,
    utility: str | None = None,
) -> tuple[int, int]:
    """Load ``ingest_results_all.json`` into ``ncuc_ingest_segments``.

    Args:
        utility: Optional utility discriminator to stamp on every inserted row
                 (e.g. ``"DEP"`` for E-2 dockets, ``"DEC"`` for E-7 dockets).
                 When omitted the ``utility`` column is left NULL.

    Returns ``(inserted, skipped)`` counts.
    """
    with open(ingest_json, encoding="utf-8") as fh:
        records = json.load(fh)

    return persist_ingest_result_records(
        conn,
        records,
        replace=replace,
        utility=utility,
    )


def persist_ingest_result_records(
    conn: sqlite3.Connection,
    records: list[dict],
    *,
    replace: bool = False,
    utility: str | None = None,
) -> tuple[int, int]:
    """Persist serialized ingest records directly into SQLite."""
    inserted = 0
    skipped = 0

    for rec in records:
        written, utility_value = _upsert_ingest_result_record(
            conn,
            rec,
            replace=replace,
            utility=utility,
        )
        if written:
            inserted += 1
        else:
            skipped += 1
        _persist_ingest_diagnostics(conn, rec, utility=utility_value)

    conn.commit()
    return inserted, skipped


def load_rider_summaries(
    conn: sqlite3.Connection,
    rider_json: Path,
    *,
    replace: bool = False,
    utility: str | None = None,
) -> tuple[int, int]:
    """Load ``rider_summaries.json`` into ``rider_summary_blocks`` + ``rider_line_items``.

    Args:
        utility: Optional utility discriminator to stamp on every inserted block
                 (e.g. ``"DEP"`` for Leaf 600 blocks, ``"DEC"`` for Leaf 99 blocks).
                 When omitted the ``utility`` column is left NULL.

    Returns ``(blocks_inserted, blocks_skipped)`` counts.
    """
    with open(rider_json, encoding="utf-8") as fh:
        records = json.load(fh)

    return persist_rider_summary_records(
        conn,
        records,
        replace=replace,
        utility=utility,
    )


def persist_rider_summary_records(
    conn: sqlite3.Connection,
    records: list[dict],
    *,
    replace: bool = False,
    utility: str | None = None,
) -> tuple[int, int]:
    """Persist serialized rider summary records directly into SQLite."""
    blocks_inserted = 0
    blocks_skipped = 0

    for rec in records:
        source_pdf = rec.get("source_pdf", "")
        docket_dir = _docket_dir_from_pdf(source_pdf)
        utility_value = utility or _infer_utility_from_source(source_pdf, docket_dir)
        leaf_no = rec.get("leaf_no")
        effective_date = _normalize_date(rec.get("effective_date") or "")
        docket_number = rec.get("docket_number")
        order_date = rec.get("order_date")
        supersedes = rec.get("supersedes")

        for rc in rec.get("rate_classes", []):
            rate_class = rc.get("rate_class", "")
            total_cents = _clean_rate(rc.get("total_cents_per_kwh"))
            total_kw = _clean_rate(rc.get("total_dollars_per_kw"))

            existing = conn.execute(
                """
                SELECT id FROM rider_summary_blocks
                WHERE docket_dir=? AND source_pdf=? AND rate_class=? AND effective_date IS ?
                """,
                (docket_dir, source_pdf, rate_class, effective_date),
            ).fetchone()

            if existing and not replace:
                blocks_skipped += 1
                continue

            now = datetime.now(UTC).isoformat()
            if existing and replace:
                block_id = existing["id"]
                conn.execute(
                    """
                    UPDATE rider_summary_blocks SET
                        leaf_no=?, docket_number=?, order_date=?, supersedes=?,
                        applicable_schedules_json=?, total_cents_per_kwh=?,
                        total_dollars_per_kw=?, utility=?, created_at=?
                    WHERE id=?
                    """,
                    (
                        leaf_no, docket_number, order_date, supersedes,
                        json.dumps(rc.get("applicable_schedules") or []),
                        total_cents, total_kw, utility_value, now, block_id,
                    ),
                )
                conn.execute("DELETE FROM rider_line_items WHERE block_id=?", (block_id,))
            else:
                cur = conn.execute(
                    """
                    INSERT INTO rider_summary_blocks (
                        docket_dir, source_pdf, leaf_no, effective_date,
                        docket_number, order_date, supersedes,
                        rate_class, applicable_schedules_json,
                        total_cents_per_kwh, total_dollars_per_kw, utility, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        docket_dir, source_pdf, leaf_no, effective_date,
                        docket_number, order_date, supersedes,
                        rate_class,
                        json.dumps(rc.get("applicable_schedules") or []),
                        total_cents, total_kw, utility_value, now,
                    ),
                )
                block_id = cur.lastrowid

            # Insert line items
            for item in rc.get("line_items", []):
                conn.execute(
                    """
                    INSERT INTO rider_line_items (
                        block_id, label, rider_code,
                        cents_per_kwh, dollars_per_kw, line_effective_date,
                        is_section_header, is_subtotal, is_total, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        block_id,
                        item.get("label", ""),
                        item.get("rider_code"),
                        _clean_rate(item.get("cents_per_kwh")),
                        _clean_rate(item.get("dollars_per_kw")),
                        item.get("effective_date"),
                        int(bool(item.get("is_section_header"))),
                        int(bool(item.get("is_subtotal"))),
                        int(bool(item.get("is_total"))),
                        now,
                    ),
                )

            blocks_inserted += 1

    conn.commit()
    return blocks_inserted, blocks_skipped


def seed_rider_descriptions(conn: sqlite3.Connection) -> int:
    """Insert the canonical rider descriptions (idempotent).

    Returns number of rows inserted (0 if already seeded).
    """
    inserted = 0
    for row in _RIDER_DESCRIPTIONS:
        existing = conn.execute(
            "SELECT 1 FROM rider_descriptions WHERE rider_code=?",
            (row["rider_code"],),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO rider_descriptions (
                rider_code, short_name, full_name, description, category,
                created_by_event, rate_type, applies_to_schedules_json,
                notes, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["rider_code"],
                row["short_name"],
                row["full_name"],
                row["description"],
                row["category"],
                row.get("created_by_event"),
                row.get("rate_type", "cents_per_kwh"),
                json.dumps(row.get("applies_to_schedules", [])),
                row.get("notes"),
                datetime.now(UTC).isoformat(),
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def _upsert_ingest_result_record(
    conn: sqlite3.Connection,
    rec: dict,
    *,
    replace: bool,
    utility: str | None,
) -> tuple[bool, str | None]:
    seg = rec.get("segment", {})
    source_pdf = rec.get("source_pdf", "")
    docket_dir = _docket_dir_from_pdf(source_pdf)
    utility_value = utility or _infer_utility_from_source(source_pdf, docket_dir)
    pr = rec.get("page_range") or [None, None]
    page_start = pr[0] if pr else None
    page_end = pr[1] if len(pr) > 1 else None

    norm_eff = _normalize_date(rec.get("effective_date") or "")

    existing = conn.execute(
        """
        SELECT id FROM ncuc_ingest_segments
        WHERE docket_dir = ? AND source_pdf = ? AND leaf_no IS ?
          AND schedule_code IS ? AND effective_date IS ?
        """,
        (docket_dir, source_pdf, seg.get("leaf_no"), seg.get("schedule_code"), norm_eff),
    ).fetchone()

    if existing and not replace:
        return False, utility_value

    now = datetime.now(UTC).isoformat()
    params = (
        docket_dir,
        source_pdf,
        seg.get("leaf_no"),
        seg.get("schedule_code"),
        norm_eff,
        seg.get("revision"),
        rec.get("supersedes"),
        rec.get("docket_number"),
        rec.get("order_date"),
        rec.get("tier", 1),
        rec.get("confidence", 0.0),
        rec.get("status", "empty"),
        page_start,
        page_end,
        json.dumps(rec.get("energy_charges") or []),
        json.dumps(rec.get("fixed_charges") or []),
        json.dumps(rec.get("demand_charges") or []),
        json.dumps(seg),
        utility_value,
        now,
    )

    if existing and replace:
        update_params = params[3:] + (existing["id"],)
        conn.execute(
            """
            UPDATE ncuc_ingest_segments SET
                schedule_code=?, effective_date=?, revision_label=?,
                supersedes=?, docket_number=?, order_date=?,
                tier=?, confidence=?, status=?,
                page_start=?, page_end=?,
                energy_charges_json=?, fixed_charges_json=?, demand_charges_json=?,
                raw_segment_json=?, utility=?, created_at=?
            WHERE id=?
            """,
            update_params,
        )
    else:
        conn.execute(
            """
            INSERT INTO ncuc_ingest_segments (
                docket_dir, source_pdf, leaf_no, schedule_code,
                effective_date, revision_label, supersedes,
                docket_number, order_date,
                tier, confidence, status,
                page_start, page_end,
                energy_charges_json, fixed_charges_json, demand_charges_json,
                raw_segment_json, utility, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            params,
        )
    return True, utility_value


def _persist_ingest_diagnostics(
    conn: sqlite3.Connection,
    rec: dict,
    *,
    utility: str | None,
) -> None:
    fingerprint = _build_document_fingerprint(rec)
    attempt = _build_parse_attempt_log(rec, utility=utility)
    _upsert_document_fingerprint(conn, fingerprint)
    parse_attempt_id = _insert_parse_attempt_log(conn, attempt)
    review = _build_parse_review_outcome(
        rec,
        parse_attempt_id=parse_attempt_id,
        utility=utility,
    )
    _insert_parse_review_outcome(conn, review)


def _build_document_fingerprint(rec: dict) -> DocumentFingerprint:
    seg = rec.get("segment", {})
    pr = rec.get("page_range") or [None, None]
    metadata = {
        "schedule_title": rec.get("schedule_title"),
        "customer_class": rec.get("customer_class"),
        "tier": rec.get("tier", 1),
        "status": rec.get("status", "empty"),
        "charge_count": _count_charge_rows(rec),
    }
    if rec.get("has_rider_summary"):
        metadata["rider_summary_rate_classes"] = len((rec.get("rider_summary") or {}).get("rate_classes", []))
    return DocumentFingerprint(
        source_pdf=rec.get("source_pdf", ""),
        docket_dir=_docket_dir_from_pdf(rec.get("source_pdf", "")),
        page_start=pr[0] if pr else None,
        page_end=pr[1] if len(pr) > 1 else None,
        leaf_no=seg.get("leaf_no"),
        schedule_code=rec.get("schedule_code") or seg.get("schedule_code"),
        title=seg.get("title"),
        text_length=int(rec.get("text_length") or 0),
        line_count=int(rec.get("line_count") or 0),
        numeric_line_count=int(rec.get("numeric_line_count") or 0),
        has_table_rows=bool(rec.get("table_rows")),
        has_rider_summary=bool(rec.get("has_rider_summary")),
        review_flags=list(rec.get("review_flags") or []),
        metadata=metadata,
    )


def _build_parse_attempt_log(rec: dict, *, utility: str | None) -> ParseAttemptLog:
    seg = rec.get("segment", {})
    pr = rec.get("page_range") or [None, None]
    parser_stage = {
        0: "empty",
        1: "heuristic",
        2: "table",
        3: "llm",
    }.get(int(rec.get("tier", 0) or 0), "unknown")
    parser_profile = "rider_summary" if rec.get("has_rider_summary") else "tiered_ingest"
    return ParseAttemptLog(
        source_pdf=rec.get("source_pdf", ""),
        docket_dir=_docket_dir_from_pdf(rec.get("source_pdf", "")),
        page_start=pr[0] if pr else None,
        page_end=pr[1] if len(pr) > 1 else None,
        parser_stage=parser_stage,
        parser_profile=parser_profile,
        status=rec.get("status", "empty"),
        confidence=float(rec.get("confidence", 0.0) or 0.0),
        utility=utility,
        schedule_code=rec.get("schedule_code") or seg.get("schedule_code"),
        effective_date=_normalize_date(rec.get("effective_date") or "") or rec.get("effective_date"),
        charge_count=_count_charge_rows(rec),
        review_flags=list(rec.get("review_flags") or []),
        metadata={
            "leaf_no": seg.get("leaf_no"),
            "revision": seg.get("revision"),
            "title": seg.get("title"),
            "docket_number": rec.get("docket_number"),
            "order_date": rec.get("order_date"),
        },
    )


def _build_parse_review_outcome(
    rec: dict,
    *,
    parse_attempt_id: int,
    utility: str | None,
) -> ParseReviewOutcome:
    pr = rec.get("page_range") or [None, None]
    review_flags = list(rec.get("review_flags") or [])
    status = rec.get("status", "empty")
    outcome = "accepted" if status == "parsed" and not review_flags else "needs_review"
    return ParseReviewOutcome(
        parse_attempt_id=parse_attempt_id,
        source_pdf=rec.get("source_pdf", ""),
        docket_dir=_docket_dir_from_pdf(rec.get("source_pdf", "")),
        page_start=pr[0] if pr else None,
        page_end=pr[1] if len(pr) > 1 else None,
        parser_stage={
            0: "empty",
            1: "heuristic",
            2: "table",
            3: "llm",
        }.get(int(rec.get("tier", 0) or 0), "unknown"),
        parser_profile="rider_summary" if rec.get("has_rider_summary") else "tiered_ingest",
        utility=utility,
        review_source="rule",
        outcome=outcome,
        notes={
            "status": status,
            "review_flags": review_flags,
            "charge_count": _count_charge_rows(rec),
            "effective_date": _normalize_date(rec.get("effective_date") or "") or rec.get("effective_date"),
        },
    )


def _upsert_document_fingerprint(conn: sqlite3.Connection, fingerprint: DocumentFingerprint) -> None:
    existing = conn.execute(
        """
        SELECT id FROM document_fingerprints
        WHERE source_pdf = ? AND page_start IS ? AND page_end IS ?
          AND leaf_no IS ? AND schedule_code IS ?
        """,
        (
            fingerprint.source_pdf,
            fingerprint.page_start,
            fingerprint.page_end,
            fingerprint.leaf_no,
            fingerprint.schedule_code,
        ),
    ).fetchone()
    now = datetime.now(UTC).isoformat()
    params = (
        fingerprint.source_pdf,
        fingerprint.docket_dir,
        fingerprint.page_start,
        fingerprint.page_end,
        fingerprint.leaf_no,
        fingerprint.schedule_code,
        fingerprint.title,
        fingerprint.text_length,
        fingerprint.line_count,
        fingerprint.numeric_line_count,
        int(fingerprint.has_table_rows),
        int(fingerprint.has_rider_summary),
        json.dumps(fingerprint.review_flags),
        json.dumps(fingerprint.metadata, sort_keys=True),
        now,
    )
    if existing:
        conn.execute(
            """
            UPDATE document_fingerprints SET
                docket_dir=?, page_start=?, page_end=?, leaf_no=?, schedule_code=?,
                title=?, text_length=?, line_count=?, numeric_line_count=?,
                has_table_rows=?, has_rider_summary=?, review_flags_json=?,
                metadata_json=?, created_at=?
            WHERE id=?
            """,
            params[1:] + (existing["id"],),
        )
    else:
        conn.execute(
            """
            INSERT INTO document_fingerprints (
                source_pdf, docket_dir, page_start, page_end, leaf_no,
                schedule_code, title, text_length, line_count, numeric_line_count,
                has_table_rows, has_rider_summary, review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            params,
        )


def _insert_parse_attempt_log(conn: sqlite3.Connection, attempt: ParseAttemptLog) -> int:
    cur = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt.source_pdf,
            attempt.docket_dir,
            attempt.page_start,
            attempt.page_end,
            attempt.parser_stage,
            attempt.parser_profile,
            attempt.status,
            attempt.confidence,
            attempt.utility,
            attempt.schedule_code,
            attempt.effective_date,
            attempt.charge_count,
            json.dumps(attempt.review_flags),
            json.dumps(attempt.metadata, sort_keys=True),
            datetime.now(UTC).isoformat(),
        ),
    )
    return int(cur.lastrowid)


def _insert_parse_review_outcome(conn: sqlite3.Connection, outcome: ParseReviewOutcome) -> int:
    cur = conn.execute(
        """
        INSERT INTO parse_review_outcomes (
            parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
            parser_stage, parser_profile, utility, review_source, outcome,
            correction_count, notes_json, corrections_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            outcome.parse_attempt_id,
            outcome.source_pdf,
            outcome.docket_dir,
            outcome.page_start,
            outcome.page_end,
            outcome.parser_stage,
            outcome.parser_profile,
            outcome.utility,
            outcome.review_source,
            outcome.outcome,
            outcome.correction_count,
            json.dumps(outcome.notes, sort_keys=True),
            json.dumps(outcome.corrections, sort_keys=True),
            datetime.now(UTC).isoformat(),
        ),
    )
    return int(cur.lastrowid)


def _count_charge_rows(rec: dict) -> int:
    return (
        len(rec.get("energy_charges") or [])
        + len(rec.get("fixed_charges") or [])
        + len(rec.get("demand_charges") or [])
    )


def _infer_utility_from_source(source_pdf: str, docket_dir: str | None = None) -> str | None:
    """Infer the short-form utility discriminator used by ingest tables.

    Returns:
        ``"DEP"`` for Progress/CP&L / E-2 content,
        ``"DEC"`` for Carolinas/Duke Power / E-7 content,
        or ``None`` when no reasonable inference is available.
    """
    probe = " ".join(part for part in (source_pdf, docket_dir) if part)
    canonical = normalize_duke_company(probe, fallback=None, state="NC")
    if canonical == "progress":
        return "DEP"
    if canonical == "carolinas":
        return "DEC"

    lowered = probe.lower()
    if re.search(r"(?:^|[^0-9])e[-_ ]?2(?:[^0-9]|$)", lowered):
        return "DEP"
    if re.search(r"(?:^|[^0-9])e[-_ ]?7(?:[^0-9]|$)", lowered):
        return "DEC"
    return None


# ---------------------------------------------------------------------------
# Bill calculator
# ---------------------------------------------------------------------------

def calculate_bill(
    conn: sqlite3.Connection,
    *,
    schedule_code: str,
    effective_date: str,
    kwh: float,
    kw: float | None = None,
    include_riders: bool = True,
    breakdown: bool = True,
    utility: str | None = None,
) -> dict:
    """Reconstruct a Duke Energy NC bill for a given schedule and usage period.

    Args:
        conn:           Open DB connection.
        schedule_code:  Rate schedule (e.g. "RES", "SGS", "MGS").
        effective_date: Bill service date in YYYY-MM-DD or "YYYY-MM" format.
        kwh:            Total kWh consumed in the billing period.
        kw:             Peak demand in kW (for demand-metered schedules).
        include_riders: Add Leaf 600 rider adders to the base bill.
        breakdown:      Return per-line attribution in the result.
        utility:        Optional utility discriminator (e.g. "DEP", "DEC").
                        When supplied, restricts base-rate and rider queries to
                        rows with a matching ``utility`` column value.  When
                        omitted, falls back to the legacy schedule-code-only
                        match for backwards compatibility.

    Returns:
        Dict with keys: ``schedule_code``, ``effective_date``, ``kwh``,
        ``base_charges``, ``rider_charges``, ``total_cents_per_kwh``,
        ``total_amount``, ``line_items`` (if breakdown=True).
    """
    # Normalize effective_date to YYYY-MM-DD
    eff = _normalize_date(effective_date)

    # --- Fetch base rate charges ---
    # Prefer exact schedule code match; fall back to LIKE.
    # Filter to plausible energy rates (< 0.50 $/kWh = 50¢/kWh) to exclude garbage parses.
    # When utility is supplied, restrict to that utility's rows only.
    _utility_clause = "AND s.utility = ?" if utility else ""
    _base_params = [schedule_code, f"%{schedule_code}%", eff]
    if utility:
        _base_params.append(utility)
    base_rows = conn.execute(
        f"""
        SELECT s.schedule_code, s.effective_date, s.energy_charges_json,
               s.fixed_charges_json, s.demand_charges_json, s.confidence,
               CASE WHEN s.schedule_code = ? THEN 1 ELSE 0 END AS exact_match
        FROM ncuc_ingest_segments s
        WHERE s.schedule_code LIKE ?
          AND s.status IN ('parsed','partial')
          AND s.effective_date <= ?
          AND s.effective_date IS NOT NULL
          AND json_extract(s.energy_charges_json, '$[0].rate') < 0.5
          {_utility_clause}
        ORDER BY exact_match DESC, s.effective_date DESC, s.confidence DESC, s.id DESC
        LIMIT 1
        """,
        _base_params,
    ).fetchone()

    if not base_rows:
        return {"error": f"No parsed rate data found for {schedule_code} on or before {effective_date}"}

    energy_charges = json.loads(base_rows["energy_charges_json"] or "[]")
    fixed_charges = json.loads(base_rows["fixed_charges_json"] or "[]")
    demand_charges = json.loads(base_rows["demand_charges_json"] or "[]")
    base_eff = base_rows["effective_date"]

    line_items: list[dict] = []
    total_amount = 0.0

    # Fixed charges (customer charge $/month)
    for fc in fixed_charges:
        amt = float(fc.get("amount", 0))
        total_amount += amt
        if breakdown:
            line_items.append({
                "category": "fixed",
                "label": fc.get("label", "Customer Charge"),
                "unit": "$/month",
                "quantity": 1,
                "rate": amt,
                "amount": amt,
            })

    # Energy charges — stored as $/kWh in the ingest JSON.
    # Filter to the applicable season based on billing month.
    billing_month = _month_from_date(eff)
    energy_charges = _filter_seasonal_charges(energy_charges, billing_month)

    # Normalise units to $/kWh before passing to the shared block-tier engine.
    normalised_charges = []
    for ec in energy_charges:
        rate_dollars = float(ec.get("rate", 0))
        unit = ec.get("unit", "$/kWh")
        if "cent" in unit.lower():
            rate_dollars = rate_dollars / 100.0
        normalised_charges.append({**ec, "rate": rate_dollars, "unit": "$/kWh"})

    # Delegate to the shared block-tier implementation (TD-005).
    energy_total = 0.0
    tier_results = apply_block_tiers(normalised_charges, kwh)
    for tr in tier_results:
        amt = tr["amount"]
        energy_total += amt
        total_amount += amt
        if breakdown:
            season = next(
                (ec.get("season") for ec in normalised_charges if ec.get("label") == tr["label"]),
                None,
            )
            label = tr["label"]
            if season:
                label += f" ({season})"
            line_items.append({
                "category": "energy",
                "label": label,
                "unit": "$/kWh",
                "quantity": tr["quantity"],
                "rate": tr["rate"],
                "amount": round(amt, 4),
            })

    # Demand charges ($/kW)
    if kw and demand_charges:
        for dc in demand_charges:
            rate_kw = float(dc.get("rate", 0))
            amt = kw * rate_kw
            total_amount += amt
            if breakdown:
                line_items.append({
                    "category": "demand",
                    "label": dc.get("label", "Demand Charge"),
                    "unit": "$/kW",
                    "quantity": kw,
                    "rate": rate_kw,
                    "amount": round(amt, 4),
                })

    # --- Rider adders from Leaf 600 ---
    rider_total_cents = 0.0
    if include_riders:
        rate_class = _schedule_to_rate_class(schedule_code)
        _rider_utility_clause = "AND b.utility = ?" if utility else ""
        _rider_params = [f"%{rate_class}%", eff]
        if utility:
            _rider_params.append(utility)
        rider_row = conn.execute(
            f"""
            SELECT b.total_cents_per_kwh, b.total_dollars_per_kw,
                   b.effective_date, b.docket_number, b.id
            FROM rider_summary_blocks b
            WHERE b.rate_class LIKE ?
              AND b.effective_date <= ?
              {_rider_utility_clause}
            ORDER BY b.effective_date DESC
            LIMIT 1
            """,
            _rider_params,
        ).fetchone()

        if rider_row:
            total_r_cents = rider_row["total_cents_per_kwh"] or 0.0
            rider_total_cents = total_r_cents
            rider_amt = kwh * total_r_cents / 100.0
            total_amount += rider_amt

            if breakdown:
                line_items.append({
                    "category": "riders_total",
                    "label": f"Rider Adjustments (Leaf 600 — {rate_class})",
                    "unit": "¢/kWh",
                    "quantity": kwh,
                    "rate": total_r_cents,
                    "amount": round(rider_amt, 4),
                    "rider_effective_date": rider_row["effective_date"],
                })

                # Per-rider breakdown
                items = conn.execute(
                    """
                    SELECT li.label, li.rider_code, li.cents_per_kwh,
                           li.dollars_per_kw, li.line_effective_date,
                           li.is_section_header, li.is_subtotal, li.is_total,
                           rd.short_name, rd.description
                    FROM rider_line_items li
                    LEFT JOIN rider_descriptions rd ON rd.rider_code = li.rider_code
                    WHERE li.block_id = ?
                      AND li.is_section_header = 0
                    """,
                    (rider_row["id"],),
                ).fetchall()

                for it in items:
                    if it["is_total"]:
                        continue
                    cents = it["cents_per_kwh"] or 0.0
                    amt = kwh * cents / 100.0
                    line_items.append({
                        "category": "rider",
                        "label": it["label"],
                        "rider_code": it["rider_code"],
                        "short_name": it["short_name"],
                        "description": it["description"],
                        "unit": "¢/kWh",
                        "quantity": kwh,
                        "rate": cents,
                        "amount": round(amt, 4),
                        "effective_date": it["line_effective_date"],
                        "is_subtotal": bool(it["is_subtotal"]),
                    })

            # $/kW rider component
            if kw and rider_row["total_dollars_per_kw"]:
                r_kw = rider_row["total_dollars_per_kw"]
                rider_kw_amt = kw * r_kw
                total_amount += rider_kw_amt
                if breakdown:
                    line_items.append({
                        "category": "riders_demand",
                        "label": f"Rider Adjustments $/kW ({rate_class})",
                        "unit": "$/kW",
                        "quantity": kw,
                        "rate": r_kw,
                        "amount": round(rider_kw_amt, 4),
                    })

    # Energy total is in $; convert to ¢/kWh for summary
    base_energy_cents = (energy_total / kwh * 100) if kwh else 0.0
    # rider_total_cents is already in ¢/kWh from Leaf 600

    result: dict = {
        "schedule_code": schedule_code,
        "rate_effective_date": base_eff,
        "billing_date": effective_date,
        "kwh": kwh,
        "kw": kw,
        "base_energy_cents_per_kwh": round(base_energy_cents, 4),
        "rider_cents_per_kwh": round(rider_total_cents, 4),
        "total_cents_per_kwh": round(base_energy_cents + rider_total_cents, 4),
        "total_amount": round(total_amount, 4),
    }
    if breakdown:
        result["line_items"] = line_items

    return result


# ---------------------------------------------------------------------------
# Rate comparison
# ---------------------------------------------------------------------------

def compare_schedules(
    conn: sqlite3.Connection,
    *,
    effective_date: str,
    kwh: float,
    kw: float | None = None,
    schedules: list[str] | None = None,
) -> list[dict]:
    """Calculate bills for multiple rate schedules and return sorted comparison.

    Args:
        conn:           Open DB connection.
        effective_date: Billing date for all calculations.
        kwh:            kWh usage.
        kw:             Peak demand kW (optional).
        schedules:      List of schedule codes to compare. If None, uses all
                        residential and small/medium commercial schedules.

    Returns:
        List of result dicts sorted by total_amount ascending, each containing
        the full calculate_bill output plus a ``rank`` field.
    """
    eff = _normalize_date(effective_date) or effective_date

    if schedules is None:
        # Auto-discover available schedules for the given date
        rows = conn.execute(
            """
            SELECT DISTINCT schedule_code
            FROM ncuc_ingest_segments
            WHERE status IN ('parsed','partial')
              AND effective_date <= ?
              AND effective_date IS NOT NULL
              AND json_extract(energy_charges_json, '$[0].rate') < 0.5
            ORDER BY schedule_code
            """,
            (eff,),
        ).fetchall()
        schedules = [r["schedule_code"] for r in rows if r["schedule_code"]]

    results = []
    for code in schedules:
        bill = calculate_bill(
            conn,
            schedule_code=code,
            effective_date=effective_date,
            kwh=kwh,
            kw=kw,
            include_riders=True,
            breakdown=False,
        )
        if "error" in bill:
            continue
        # Skip implausible results: base energy > 50¢/kWh is a parse artifact
        if bill.get("base_energy_cents_per_kwh", 0) > 50:
            continue
        # Skip schedules where total bill / kWh > $0.50/kWh (50¢ all-in including fixed)
        # This excludes schedules with large per-kVA or per-outlet fixed charges that
        # are not comparable to energy-based residential/small-commercial rates.
        if kwh > 0 and bill.get("total_amount", 0) / kwh > 0.50:
            continue
        bill["schedule_code"] = code
        results.append(bill)

    results.sort(key=lambda r: r.get("total_amount", float("inf")))
    for i, r in enumerate(results, 1):
        r["rank"] = i

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _month_from_date(date_str: str) -> int:
    """Return month number (1-12) from a YYYY-MM-DD string, or 0 if unparseable."""
    try:
        return int(date_str[5:7])
    except (IndexError, ValueError):
        return 0


def _filter_seasonal_charges(charges: list[dict], billing_month: int) -> list[dict]:
    """Keep only the charges applicable to the given billing month.

    Delegates season label matching to the shared ``season_matches()`` function
    in ``billing.season_utils``.  That function also logs a WARNING for any
    unrecognized season label.

    If a charge has no season label, it applies year-round.
    If multiple seasons exist, keep only the matching one.
    """
    if billing_month == 0:
        return charges

    seasons_present = {c.get("season") for c in charges if c.get("season")}
    if not seasons_present:
        return charges  # no seasonal split — all charges apply

    applicable = [c for c in charges if season_matches(c.get("season"), billing_month)]
    # If filtering left nothing (e.g. all charges had unknown seasons that
    # season_matches returned True for), return what we have; otherwise fall
    # back to the full list to avoid an empty energy charge.
    return applicable if applicable else charges


def _docket_dir_from_pdf(source_pdf: str) -> str:
    """Extract docket directory name from a full PDF path."""
    parts = Path(source_pdf).parts
    for i, part in enumerate(parts):
        if part == "ncuc" and i + 1 < len(parts):
            return parts[i + 1]
    # Fallback: second-to-last directory
    p = Path(source_pdf)
    return p.parent.name if p.parent.name != "ncuc" else p.parent.parent.name


def _clean_rate(value) -> float | None:
    """Sanitize rate values — truncate garbage trailing digits from redlined PDFs."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    # Values > 100 ¢/kWh are artifacts of redlined PDF concatenation
    if v > 100:
        m = re.search(r"(\d{1,2}\.\d{3,4})$", f"{v:.6f}")
        if m:
            candidate = float(m.group(1))
            if 0.0 <= candidate <= 100:
                return round(candidate, 4)
        return None
    return round(v, 6)


def _normalize_date(date_str: str) -> str | None:
    """Normalize various date formats to YYYY-MM-DD for SQL comparison.

    Returns None if the string cannot be parsed to a plausible date.
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    # Already YYYY-MM-DD or YYYY-MM
    if re.match(r"^\d{4}-\d{2}", s):
        if len(s) == 7:
            return s + "-01"
        return s[:10]
    # "October 1, 2024" / "October 1, 20243" (redlined — take rightmost 4-digit year)
    import calendar
    year_m = re.search(r"(\d{4})\d*$", s)  # last 4-digit year
    year_str = year_m.group(1) if year_m else None
    for i, month in enumerate(calendar.month_name[1:], 1):
        if month.lower() in s.lower():
            day_m = re.search(r"\b(\d{1,2}),", s)
            day = int(day_m.group(1)) if day_m else 1
            if year_str:
                return f"{year_str}-{i:02d}-{day:02d}"
    # If string contains 2+ month names (redlined), return None
    month_count = sum(1 for m in calendar.month_name[1:] if m.lower() in s.lower())
    if month_count >= 2:
        return None
    return None


_SCHEDULE_TO_CLASS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^R-TOU|^R-TOU-CPP|^R-TOUD", re.I), "Residential Service Schedules"),
    (re.compile(r"^RES\b", re.I), "Residential Service Schedules"),
    (re.compile(r"^SGS-TOU-CLR", re.I), "Small General Service - Constant Load Schedule"),
    (re.compile(r"^SGS-TOU|^SGS-CPP|^SGS-TOUE", re.I), "Small General Service Schedules"),
    (re.compile(r"^SGS\b", re.I), "Small General Service Schedules"),
    (re.compile(r"^MGS-TOU|^CH-TOUE|^GS-TES|^APH-TES", re.I), "Demand: Medium General Service Schedules"),
    (re.compile(r"^MGS\b|^CSG\b|^CSE\b", re.I), "Non-Demand: Medium General Service Schedules"),
    (re.compile(r"^SI\b", re.I), "Seasonal or Intermittent Service Schedule"),
    (re.compile(r"^LGS-RTP|^HP\b|^LGS-HLF", re.I), "Schedule HP & Schedule LGS-RTP"),
    (re.compile(r"^LGS\b", re.I), "Large General Service Schedules"),
    (re.compile(r"^ALS|^SLS|^SLR|^TFS", re.I), "Outdoor Lighting Schedules"),
    (re.compile(r"^SFLS", re.I), "Sports Field Lighting Schedule"),
    (re.compile(r"^TSS", re.I), "Traffic Signal Schedules"),
]


def _schedule_to_rate_class(schedule_code: str) -> str:
    """Map a schedule code to its Leaf 600 rate class name."""
    code = re.sub(r"-\d+$", "", schedule_code.strip())  # strip revision suffix
    for pattern, class_name in _SCHEDULE_TO_CLASS:
        if pattern.match(code):
            return class_name
    return "Residential Service Schedules"


# ---------------------------------------------------------------------------
# Static rider descriptions
# ---------------------------------------------------------------------------

_RIDER_DESCRIPTIONS: list[dict] = [
    {
        "rider_code": "BA-Fuel",
        "short_name": "Fuel Adjustment",
        "full_name": "Fuel and Fuel-Related Adjustment Rate",
        "description": (
            "Recovers (or returns) the difference between actual fuel costs and "
            "the fuel costs embedded in base rates. Reset annually based on projected "
            "fuel costs for the upcoming rate year. A positive value means actual fuel "
            "costs exceeded the base rate allowance; a negative value returns over-recovery."
        ),
        "category": "fuel",
        "created_by_event": "Ongoing annual adjustment mechanism",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS", "SI", "HP"],
        "notes": "Sub-component of Rider BA. Can swing significantly with natural gas prices.",
    },
    {
        "rider_code": "BA-EMF",
        "short_name": "EMF",
        "full_name": "Fuel Experience Modification Factor",
        "description": (
            "Trues up the prior year's fuel cost over- or under-recovery. If Duke "
            "collected more fuel revenue than it actually spent, EMF returns the surplus; "
            "if it under-collected, EMF recoups the shortfall. Typically set each December 1."
        ),
        "category": "fuel",
        "created_by_event": "Annual fuel true-up mechanism",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS", "SI", "HP"],
        "notes": "Sub-component of Rider BA. Winter Storm Uri (Feb 2021) caused a spike in this rate.",
    },
    {
        "rider_code": "BA-DSM",
        "short_name": "DSM Rate",
        "full_name": "Demand Side Management Rate",
        "description": (
            "Funds Duke Energy's energy efficiency and demand response programs "
            "such as home energy audits, appliance rebates, and smart thermostat programs. "
            "Approved as part of the Integrated Resource Plan (IRP) and adjusted annually."
        ),
        "category": "efficiency",
        "created_by_event": "NC DSM/EE program requirements",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS"],
        "notes": "Customers who opt out of DSM programs may receive a reduced or zero rate.",
    },
    {
        "rider_code": "BA-EE",
        "short_name": "EE Rate",
        "full_name": "Energy Efficiency Rate",
        "description": (
            "Separate component of the Annual Billing Adjustment (BA) that recovers "
            "costs specifically attributable to the Energy Efficiency portfolio, including "
            "commercial and industrial efficiency programs."
        ),
        "category": "efficiency",
        "created_by_event": "NC energy efficiency legislation (S.L. 2007-397)",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["SGS", "MGS", "LGS"],
        "notes": "Not applicable to residential customers who use the combined DSM & EE rate.",
    },
    {
        "rider_code": "BA",
        "short_name": "Rider BA",
        "full_name": "Annual Billing Adjustments Rider BA",
        "description": (
            "An umbrella rider that combines all annual billing adjustments: the Fuel "
            "Adjustment Rate, Experience Modification Factor (EMF), and DSM/EE rates. "
            "The 'BA - Net Adjustment' line on Leaf 600 is the sum of these components."
        ),
        "category": "fuel",
        "created_by_event": "Long-standing NCUC-approved annual adjustment mechanism",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS", "SI", "HP", "OL", "SFLS", "TSS"],
        "notes": "Detailed in Leaf No. 601.",
    },
    {
        "rider_code": "RAL-2",
        "short_name": "RAL-2",
        "full_name": "Regulatory Asset and Liability Rider RAL-2",
        "description": (
            "Returns or recovers deferred regulatory assets and liabilities created "
            "during rate cases. RAL-2 specifically covers items authorized in the 2019 "
            "rate case settlement (E-2, Sub 1142). Negative values indicate a credit "
            "to customers from over-recovered regulatory costs."
        ),
        "category": "regulatory",
        "created_by_event": "2019 NC rate case settlement (E-2, Sub 1142)",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS", "SI", "HP"],
        "notes": "Detailed in Leaf No. 612. Typically negative (credit to customers).",
    },
    {
        "rider_code": "RAL",
        "short_name": "RAL",
        "full_name": "Regulatory Asset and Liability Rider RAL",
        "description": (
            "Original version of the regulatory asset and liability rider, predating "
            "RAL-2. Covers deferred costs and credits from earlier rate proceedings."
        ),
        "category": "regulatory",
        "created_by_event": "Prior NC rate case regulatory accounting",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS"],
    },
    {
        "rider_code": "EDIT-4",
        "short_name": "EDIT-4",
        "full_name": "Excess Deferred Income Tax Rider EDIT-4",
        "description": (
            "Returns excess accumulated deferred income taxes (ADIT) to customers "
            "following the Tax Cuts and Jobs Act of 2017, which reduced the federal "
            "corporate tax rate from 35% to 21%. Utilities had over-collected taxes "
            "embedded in rates; EDIT riders return this surplus. EDIT-4 is the "
            "most recent tranche authorized under the 2023 MYRP (E-2, Sub 1300)."
        ),
        "category": "tax",
        "created_by_event": "Tax Cuts and Jobs Act (Dec 2017), authorized via NCUC order",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS", "SI"],
        "notes": "Negative value = credit to customers. Authorized in Leaf No. 614.",
    },
    {
        "rider_code": "EDIT-3",
        "short_name": "EDIT-3",
        "full_name": "Excess Deferred Income Tax Rider EDIT-3",
        "description": (
            "Third tranche of EDIT tax credits returned to customers following the "
            "2017 Tax Cuts and Jobs Act. Authorized in the 2021 rate case (E-2, Sub 1219)."
        ),
        "category": "tax",
        "created_by_event": "2021 NC rate case (E-2, Sub 1219)",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS"],
        "notes": "Negative value = credit to customers.",
    },
    {
        "rider_code": "EDIT-1",
        "short_name": "EDIT-1",
        "full_name": "Excess Deferred Income Tax Rider EDIT-1",
        "description": (
            "First tranche of TCJA tax credits returned to customers. Created shortly "
            "after passage of the Tax Cuts and Jobs Act in 2018."
        ),
        "category": "tax",
        "created_by_event": "Tax Cuts and Jobs Act (Dec 2017), first return tranche",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS"],
        "notes": "Negative value = credit to customers.",
    },
    {
        "rider_code": "JAA",
        "short_name": "Rider JAA",
        "full_name": "Joint Agency Asset Rider",
        "description": (
            "Recovers costs related to jointly-owned generating assets (primarily the "
            "Catawba Nuclear Station jointly owned with Dominion Energy South Carolina "
            "and the Keowee-Toxaway hydroelectric complex). This rider covers Duke's "
            "proportional share of capital and operating costs for these shared facilities."
        ),
        "category": "capital",
        "created_by_event": "Joint ownership agreements for nuclear/hydro assets",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "LGS", "SI"],
        "notes": "Detailed in Leaf No. 613. Also has a $/kW component for demand-metered schedules.",
    },
    {
        "rider_code": "CPRE",
        "short_name": "Rider CPRE",
        "full_name": "Competitive Procurement of Renewable Energy Rider",
        "description": (
            "Recovers costs of renewable energy purchased through competitive solicitations "
            "(RFPs) under North Carolina's Renewable Energy Portfolio Standard (REPS) law "
            "(S.L. 2007-397). Covers power purchase agreements with wind, solar, and "
            "biomass facilities procured competitively."
        ),
        "category": "renewable",
        "created_by_event": "NC REPS law (S.L. 2007-397), competitive RFP contracts",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS"],
        "notes": "Detailed in Leaf No. 617.",
    },
    {
        "rider_code": "RDM",
        "short_name": "Rider RDM",
        "full_name": "Residential Decoupling Mechanism Rider",
        "description": (
            "Breaks the link between Duke's revenue and the volume of electricity sold "
            "to residential customers. Without decoupling, utilities have a financial "
            "disincentive to promote conservation (selling less power means less revenue). "
            "RDM allows revenue to track the authorized level regardless of actual sales, "
            "removing the disincentive. A positive value means Duke under-collected "
            "(customers used less than forecast); negative means over-collected."
        ),
        "category": "regulatory",
        "created_by_event": "2012 NC rate case, NCUC decoupling order",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES"],
        "notes": "Applies to residential schedules only. Can be positive or negative each year.",
    },
    {
        "rider_code": "ESM",
        "short_name": "Rider ESM",
        "full_name": "Earnings Sharing Mechanism Rider",
        "description": (
            "Part of the 2023 Multiyear Rate Plan (MYRP) performance framework. If Duke's "
            "actual return on equity (ROE) exceeds the authorized band in a rate year, "
            "customers receive a credit through ESM. If Duke earns below the floor, "
            "the difference is deferred. Designed to share financial risk and reward "
            "between the utility and its customers."
        ),
        "category": "performance",
        "created_by_event": "2023 MYRP (E-2, Sub 1300, Order Aug 18 2023)",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS"],
        "notes": "New rider created under the PBR framework. Initially set to zero.",
    },
    {
        "rider_code": "PIM",
        "short_name": "Rider PIM",
        "full_name": "Performance Incentive Mechanism Rider",
        "description": (
            "Awards or penalizes Duke based on performance against specific metrics "
            "in the 2023 MYRP: reliability (SAIDI/SAIFI), customer satisfaction, "
            "and clean energy transition targets. If Duke meets or exceeds targets, "
            "it earns additional ROE; if it falls short, ROE is reduced. "
            "The financial impact flows to customers through this rider."
        ),
        "category": "performance",
        "created_by_event": "2023 MYRP (E-2, Sub 1300) performance-based regulation framework",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS"],
        "notes": "New rider under PBR framework. Metrics include reliability, J.D. Power scores, clean energy.",
    },
    {
        "rider_code": "CAR",
        "short_name": "Rider CAR",
        "full_name": "Customer Affordability Rider",
        "description": (
            "Funds low-income customer assistance programs including bill payment "
            "assistance, weatherization, and energy efficiency services for qualifying "
            "households. Authorized under the 2023 MYRP as part of Duke's commitment "
            "to address energy affordability for vulnerable customers."
        ),
        "category": "affordability",
        "created_by_event": "2023 MYRP (E-2, Sub 1300)",
        "rate_type": "fixed_per_bill",
        "applies_to_schedules": ["SGS", "MGS", "LGS"],
        "notes": "Fixed $/bill charge for non-residential schedules. Set to zero until Jan 1, 2024.",
    },
    {
        "rider_code": "STS",
        "short_name": "Rider STS",
        "full_name": "Storm Securitization Rider",
        "description": (
            "Recovers costs of securitized storm damage bonds. After major hurricanes "
            "(e.g., Florence 2018, Dorian 2019, Isaias 2020), Duke's restoration costs "
            "were financed through asset-backed securities approved by NCUC. This rider "
            "services the debt on those bonds over 10–15 years, typically at a lower "
            "carrying cost than traditional rate base treatment."
        ),
        "category": "storm",
        "created_by_event": "Hurricanes Florence (2018), Dorian (2019), Isaias (2020) storm recovery orders",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS"],
        "notes": "Securitized under NC storm securitization statute (G.S. 62-172).",
    },
    {
        "rider_code": "NM",
        "short_name": "Rider NM",
        "full_name": "Net Metering Rider",
        "description": (
            "Facilitates compensation for excess electricity generated by customer-sited "
            "solar panels and other distributed generation that flows back to the grid. "
            "Net metering credits are calculated at the full retail rate and applied "
            "monthly. This rider handles the accounting mechanism."
        ),
        "category": "solar",
        "created_by_event": "NC net metering rules (G.S. 62-133.8)",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS"],
        "notes": "NC net metering rules changed significantly under S.L. 2021-165.",
    },
    {
        "rider_code": "REPS",
        "short_name": "Rider REPS",
        "full_name": "Renewable Energy Portfolio Standard Rider",
        "description": (
            "Funds compliance with NC's Renewable Energy Portfolio Standard, which "
            "requires utilities to supply a rising percentage of retail electricity sales "
            "from renewable sources (12.5% by 2021 for investor-owned utilities). Covers "
            "renewable energy credits (RECs), solar contracts, and compliance costs."
        ),
        "category": "renewable",
        "created_by_event": "NC REPS law (S.L. 2007-397, 'Renewable Energy and Energy Efficiency Portfolio Standard')",
        "rate_type": "cents_per_kwh",
        "applies_to_schedules": ["RES", "SGS", "MGS", "LGS"],
        "notes": "One of the first state RPS laws in the Southeast.",
    },
]
