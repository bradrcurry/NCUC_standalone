from __future__ import annotations

import json
import math
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass
class PromotionProposal:
    validation_id: int
    extraction_id: int
    row_index: int
    repair_id: int | None
    historical_document_id: int | None
    version_id: int | None
    family_key: str
    charge_type: str
    charge_label: str
    rate_value: float | None
    rate_unit: str
    tou_period: str
    season: str
    customer_class: str
    source_quote: str
    evidence_quote: str
    effective_status: str
    eligibility_status: str
    eligibility_issues: list[str]
    duplicate_status: str
    conflict_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "validation_id": self.validation_id,
            "extraction_id": self.extraction_id,
            "row_index": self.row_index,
            "repair_id": self.repair_id,
            "historical_document_id": self.historical_document_id,
            "version_id": self.version_id,
            "family_key": self.family_key,
            "charge_type": self.charge_type,
            "charge_label": self.charge_label,
            "rate_value": self.rate_value,
            "rate_unit": self.rate_unit,
            "tou_period": self.tou_period,
            "season": self.season,
            "customer_class": self.customer_class,
            "source_quote": self.source_quote,
            "evidence_quote": self.evidence_quote,
            "effective_status": self.effective_status,
            "eligibility_status": self.eligibility_status,
            "eligibility_issues": self.eligibility_issues,
            "duplicate_status": self.duplicate_status,
            "conflict_status": self.conflict_status,
        }


def propose_llm_charge_promotions(
    db_path: Path | str,
    *,
    limit: int = 100,
    include_repaired: bool = True,
    refresh_existing: bool = False,
    execute: bool = False,
) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_promotion_tables(conn)
        rows = _load_effective_rows(
            conn,
            limit=limit,
            include_repaired=include_repaired,
            refresh_existing=refresh_existing,
        )
        proposals: list[PromotionProposal] = []
        for row in rows:
            proposal = _build_proposal(conn, row)
            proposals.append(proposal)
            if execute:
                _persist_proposal(conn, proposal)
                conn.commit()
        return {
            "summary": _summarize_proposals(proposals, execute=execute),
            "rows": [p.to_dict() for p in proposals],
        }
    finally:
        conn.close()


def promote_llm_charge_proposals(
    db_path: Path | str,
    *,
    limit: int = 25,
    execute: bool = False,
) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    promoted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        ensure_promotion_tables(conn)
        proposals = conn.execute(
            """
            SELECT *
            FROM llm_rate_charge_promotion_proposals
            WHERE promotion_status = 'pending'
              AND eligibility_status = 'eligible'
              AND duplicate_status = 'novel'
              AND conflict_status = 'none'
            ORDER BY id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for proposal in proposals:
            if _proposal_now_duplicate(conn, proposal):
                skipped.append({"proposal_id": int(proposal["id"]), "reason": "duplicate_existing"})
                if execute:
                    conn.execute(
                        """
                        UPDATE llm_rate_charge_promotion_proposals
                        SET promotion_status = 'skipped_duplicate'
                        WHERE id = ?
                        """,
                        (int(proposal["id"]),),
                    )
                continue
            if not execute:
                promoted.append({"proposal_id": int(proposal["id"]), "mode": "dry_run"})
                continue
            charge_id = _insert_tariff_charge(conn, proposal)
            conn.execute(
                """
                UPDATE llm_rate_charge_promotion_proposals
                SET promotion_status = 'promoted',
                    promoted_at = datetime('now'),
                    tariff_charge_id = ?
                WHERE id = ?
                """,
                (charge_id, int(proposal["id"])),
            )
            conn.execute(
                """
                INSERT INTO llm_promoted_charge_audit (
                    proposal_id, tariff_charge_id, validation_id, repair_id,
                    extraction_id, row_index, source_quote, evidence_quote,
                    promoted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    int(proposal["id"]),
                    charge_id,
                    int(proposal["validation_id"]),
                    proposal["repair_id"],
                    int(proposal["extraction_id"]),
                    int(proposal["row_index"]),
                    proposal["source_quote"] or "",
                    proposal["evidence_quote"] or "",
                ),
            )
            promoted.append({"proposal_id": int(proposal["id"]), "tariff_charge_id": charge_id})
        if execute:
            conn.commit()
        return {
            "summary": {
                "evaluated": len(proposals),
                "execute": execute,
                "promoted": len(promoted),
                "skipped": len(skipped),
            },
            "promoted": promoted,
            "skipped": skipped,
        }
    finally:
        conn.close()


def ensure_promotion_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_rate_charge_promotion_proposals (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            validation_id               INTEGER NOT NULL,
            extraction_id               INTEGER NOT NULL,
            row_index                   INTEGER NOT NULL,
            repair_id                   INTEGER,
            historical_document_id      INTEGER,
            version_id                  INTEGER,
            family_key                  TEXT,
            charge_type                 TEXT NOT NULL,
            charge_label                TEXT,
            rate_value                  REAL,
            rate_unit                   TEXT,
            tou_period                  TEXT,
            season                      TEXT,
            customer_class              TEXT,
            source_quote                TEXT,
            evidence_quote              TEXT,
            effective_status            TEXT NOT NULL,
            eligibility_status          TEXT NOT NULL,
            eligibility_issues_json     TEXT NOT NULL DEFAULT '[]',
            duplicate_status            TEXT NOT NULL,
            conflict_status             TEXT NOT NULL,
            promotion_status            TEXT NOT NULL DEFAULT 'pending',
            tariff_charge_id            INTEGER,
            created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
            promoted_at                 TEXT,
            UNIQUE(validation_id, repair_id)
        );
        CREATE INDEX IF NOT EXISTS idx_llm_charge_prop_status
        ON llm_rate_charge_promotion_proposals(
            promotion_status, eligibility_status, duplicate_status, conflict_status
        );
        CREATE INDEX IF NOT EXISTS idx_llm_charge_prop_version
        ON llm_rate_charge_promotion_proposals(version_id);

        CREATE TABLE IF NOT EXISTS llm_promoted_charge_audit (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id          INTEGER NOT NULL,
            tariff_charge_id     INTEGER NOT NULL,
            validation_id        INTEGER NOT NULL,
            repair_id            INTEGER,
            extraction_id        INTEGER NOT NULL,
            row_index            INTEGER NOT NULL,
            source_quote         TEXT,
            evidence_quote       TEXT,
            promoted_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_promoted_charge_audit_charge
        ON llm_promoted_charge_audit(tariff_charge_id);
        """
    )


def _load_effective_rows(
    conn: sqlite3.Connection,
    *,
    limit: int,
    include_repaired: bool,
    refresh_existing: bool,
) -> list[sqlite3.Row]:
    repaired_filter = "" if include_repaired else "AND rr.id IS NULL"
    existing_filter = """
          AND EXISTS (
              SELECT 1
              FROM llm_rate_charge_promotion_proposals pp
              WHERE pp.validation_id = rv.id
                AND (
                      (pp.repair_id IS NULL AND rr.id IS NULL)
                   OR pp.repair_id = rr.id
                )
                AND pp.promotion_status = 'pending'
          )
    """ if refresh_existing else """
          AND NOT EXISTS (
              SELECT 1
              FROM llm_rate_charge_promotion_proposals pp
              WHERE pp.validation_id = rv.id
                AND (
                      (pp.repair_id IS NULL AND rr.id IS NULL)
                   OR pp.repair_id = rr.id
                )
          )
    """
    return conn.execute(
        f"""
        WITH accepted_repair AS (
            SELECT rr.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY rr.validation_id
                       ORDER BY
                         CASE rr.repair_type
                           WHEN 'deterministic_lighting_table_repair' THEN 1
                           WHEN 'row_reclassification' THEN 2
                           WHEN 'unit_evidence' THEN 3
                           ELSE 9
                         END,
                         rr.id DESC
                   ) AS rn
            FROM llm_candidate_rate_row_repairs rr
            WHERE rr.validation_status = 'accepted'
        )
        SELECT rv.*,
               lcre.rate_rows_json,
               rr.id AS repair_id,
               rr.proposed_charge_type,
               rr.proposed_unit,
               rr.evidence_quote,
               CASE
                 WHEN rv.recommended_status = 'validated' THEN 'validated'
                 WHEN rr.id IS NOT NULL THEN 'validated_with_repair'
                 ELSE rv.recommended_status
               END AS effective_status
        FROM llm_candidate_rate_row_validations rv
        JOIN llm_candidate_rate_extractions lcre ON lcre.id = rv.extraction_id
        LEFT JOIN accepted_repair rr ON rr.validation_id = rv.id AND rr.rn = 1
        WHERE (rv.recommended_status = 'validated' OR rr.id IS NOT NULL)
          {repaired_filter}
          {existing_filter}
        ORDER BY rv.extraction_id, rv.row_index
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()


def _build_proposal(conn: sqlite3.Connection, row: sqlite3.Row) -> PromotionProposal:
    rate_row = _rate_row_from_json(row["rate_rows_json"], int(row["row_index"]))
    charge_type = (row["proposed_charge_type"] or row["charge_type"] or "").strip()
    charge_type = _normalize_charge_type(charge_type, row, rate_row)
    rate_unit = _effective_unit(row)
    rate_value = _to_float(row["value"])
    historical_document_id = (
        int(row["historical_document_id"]) if row["historical_document_id"] is not None else None
    )
    version = _find_version(conn, historical_document_id)
    version = _reroute_version(conn, row, version, rate_value=rate_value, rate_unit=rate_unit)
    version_id = int(version["id"]) if version else None
    family_key = str(version["family_key"] if version else "")
    issues: list[str] = []
    if historical_document_id is None:
        issues.append("missing_historical_document_id")
    if version_id is None:
        issues.append("missing_tariff_version")
    elif not version["effective_start"]:
        issues.append("missing_version_effective_start")
    if not family_key:
        issues.append("missing_family_key")
    elif _malformed_family_key(family_key):
        issues.append("malformed_family_key")
    if not charge_type or charge_type == "Other":
        issues.append("unsupported_charge_type")
    if rate_value is None or rate_value <= 0:
        issues.append("missing_or_nonpositive_rate_value")
    if not rate_unit:
        issues.append("missing_rate_unit")
    elif rate_unit in {"$", "¢"}:
        issues.append("unqualified_rate_unit")
    if not (row["source_quote"] or "").strip():
        issues.append("missing_source_quote")
    if _ambiguous_numeric_table_row(
        source_quote=str(row["source_quote"] or ""),
        charge_type=charge_type,
        rate_unit=rate_unit,
        rate_value=rate_value,
    ):
        issues.append("ambiguous_numeric_table_row")

    duplicate_status, conflict_status = _duplicate_and_conflict(
        conn,
        version_id=version_id,
        charge_type=charge_type,
        rate_value=rate_value,
        rate_unit=rate_unit,
        source_quote=str(row["source_quote"] or ""),
        validation_id=int(row["id"]) if row["id"] is not None else None,
    )
    if duplicate_status == "duplicate_existing":
        issues.append("duplicate_existing_charge")
    if conflict_status != "none":
        issues.append(conflict_status)

    return PromotionProposal(
        validation_id=int(row["id"]),
        extraction_id=int(row["extraction_id"]),
        row_index=int(row["row_index"]),
        repair_id=int(row["repair_id"]) if row["repair_id"] is not None else None,
        historical_document_id=historical_document_id,
        version_id=version_id,
        family_key=family_key,
        charge_type=charge_type,
        charge_label=_charge_label(charge_type, rate_row),
        rate_value=rate_value,
        rate_unit=rate_unit,
        tou_period=str(rate_row.get("tou_period") or ""),
        season=str(rate_row.get("season") or ""),
        customer_class=str(rate_row.get("customer_class") or ""),
        source_quote=str(row["source_quote"] or ""),
        evidence_quote=str(row["evidence_quote"] or ""),
        effective_status=str(row["effective_status"] or ""),
        eligibility_status="blocked" if issues else "eligible",
        eligibility_issues=issues,
        duplicate_status=duplicate_status,
        conflict_status=conflict_status,
    )


def _persist_proposal(conn: sqlite3.Connection, proposal: PromotionProposal) -> None:
    existing = conn.execute(
        """
        SELECT id
        FROM llm_rate_charge_promotion_proposals
        WHERE validation_id = ?
          AND (
                (repair_id IS NULL AND ? IS NULL)
             OR repair_id = ?
          )
        ORDER BY id
        LIMIT 1
        """,
        (proposal.validation_id, proposal.repair_id, proposal.repair_id),
    ).fetchone()
    params = (
        proposal.validation_id,
        proposal.extraction_id,
        proposal.row_index,
        proposal.repair_id,
        proposal.historical_document_id,
        proposal.version_id,
        proposal.family_key,
        proposal.charge_type,
        proposal.charge_label,
        proposal.rate_value,
        proposal.rate_unit,
        proposal.tou_period,
        proposal.season,
        proposal.customer_class,
        proposal.source_quote,
        proposal.evidence_quote,
        proposal.effective_status,
        proposal.eligibility_status,
        json.dumps(proposal.eligibility_issues),
        proposal.duplicate_status,
        proposal.conflict_status,
    )
    if existing:
        conn.execute(
            """
            UPDATE llm_rate_charge_promotion_proposals
            SET extraction_id = ?,
                row_index = ?,
                repair_id = ?,
                historical_document_id = ?,
                version_id = ?,
                family_key = ?,
                charge_type = ?,
                charge_label = ?,
                rate_value = ?,
                rate_unit = ?,
                tou_period = ?,
                season = ?,
                customer_class = ?,
                source_quote = ?,
                evidence_quote = ?,
                effective_status = ?,
                eligibility_status = ?,
                eligibility_issues_json = ?,
                duplicate_status = ?,
                conflict_status = ?
            WHERE id = ?
            """,
            (*params[1:], int(existing["id"])),
        )
        return

    conn.execute(
        """
        INSERT INTO llm_rate_charge_promotion_proposals (
            validation_id, extraction_id, row_index, repair_id,
            historical_document_id, version_id, family_key,
            charge_type, charge_label, rate_value, rate_unit,
            tou_period, season, customer_class,
            source_quote, evidence_quote, effective_status,
            eligibility_status, eligibility_issues_json,
            duplicate_status, conflict_status, promotion_status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', datetime('now'))
        """,
        params,
    )


def _find_version(conn: sqlite3.Connection, historical_document_id: int | None) -> sqlite3.Row | None:
    if historical_document_id is None:
        return None
    return conn.execute(
        """
        SELECT id, family_key, effective_start
        FROM tariff_versions
        WHERE historical_document_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (historical_document_id,),
    ).fetchone()


def _reroute_version(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    version: sqlite3.Row | None,
    *,
    rate_value: float | None,
    rate_unit: str,
) -> sqlite3.Row | None:
    if version is None:
        return None
    normalized = _reroute_malformed_family_version(conn, version)
    if normalized:
        version = normalized
    dated_sibling = _reroute_same_family_snapshot_version(conn, row, version)
    if dated_sibling:
        version = dated_sibling
    summary_sibling = _reroute_summary_line_version(
        conn,
        row,
        version,
        rate_value=rate_value,
        rate_unit=rate_unit,
    )
    if summary_sibling:
        version = summary_sibling
    if version["effective_start"]:
        return version
    source_quote = str(row["source_quote"] or "").strip()
    source_pdf = str(row["source_pdf"] or "").strip()
    if not source_quote or not source_pdf:
        return version

    text = _read_source_text(source_pdf)
    if not text:
        return version
    anchors = _unique_quote_anchors(text, source_quote)
    candidates: dict[int, sqlite3.Row] = {}
    current_family = str(version["family_key"] or "")
    company_prefix = _family_company_prefix(current_family)
    for leaf_no, effective_start in anchors:
        target_family = f"{company_prefix}-leaf-{leaf_no}" if company_prefix else f"nc-progress-leaf-{leaf_no}"
        for candidate in _find_versions_by_family_start(conn, target_family, effective_start):
            candidates[int(candidate["id"])] = candidate
        if not company_prefix:
            for candidate in _find_unique_versions_by_leaf_start(conn, leaf_no, effective_start):
                candidates[int(candidate["id"])] = candidate
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    return version


def _reroute_malformed_family_version(
    conn: sqlite3.Connection,
    version: sqlite3.Row,
) -> sqlite3.Row | None:
    family_key = str(version["family_key"] or "")
    effective_start = str(version["effective_start"] or "")
    if not effective_start or not _malformed_family_key(family_key):
        return None
    canonical = _canonical_family_key_from_path(family_key)
    if not canonical:
        return None
    candidates = _find_versions_by_family_start(conn, canonical, effective_start)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _reroute_same_family_snapshot_version(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    version: sqlite3.Row,
) -> sqlite3.Row | None:
    if version["effective_start"]:
        return None
    family_key = str(version["family_key"] or "")
    if not family_key or _malformed_family_key(family_key):
        return None
    historical_document_id = row["historical_document_id"]
    if historical_document_id is None:
        return None
    for effective_start in _historical_effective_candidates(conn, int(historical_document_id)):
        candidates = [
            candidate
            for candidate in _find_versions_by_family_start(conn, family_key, effective_start)
            if int(candidate["id"]) != int(version["id"])
        ]
        if len(candidates) == 1:
            return candidates[0]
    return None


def _reroute_summary_line_version(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    version: sqlite3.Row,
    *,
    rate_value: float | None,
    rate_unit: str,
) -> sqlite3.Row | None:
    if version["effective_start"] or rate_value is None:
        return None
    family_key = str(version["family_key"] or "")
    if family_key != "nc-progress-leaf-601":
        return None
    rider_codes = _summary_rider_codes_from_quote(str(row["source_quote"] or ""))
    if not rider_codes:
        return None
    value_column = "li.cents_per_kwh" if "kwh" in rate_unit.lower() else "li.dollars_per_kw"
    placeholders = ",".join("?" for _ in rider_codes)
    try:
        matches = conn.execute(
            f"""
            SELECT li.line_effective_date,
                   rb.effective_date AS block_effective_date
            FROM rider_line_items li
            JOIN rider_summary_blocks rb ON rb.id = li.block_id
            WHERE rb.utility IN ('DEP', 'progress', 'Progress')
              AND li.rider_code IN ({placeholders})
              AND {value_column} IS NOT NULL
              AND ABS({value_column} - ?) < 0.00001
            """,
            (*rider_codes, rate_value),
        ).fetchall()
    except sqlite3.Error:
        return None
    dates: set[str] = set()
    for match in matches:
        line_date = _parse_effective_candidate(str(match["line_effective_date"] or ""))
        block_date = _parse_effective_candidate(str(match["block_effective_date"] or ""))
        if line_date:
            dates.add(line_date)
        elif block_date:
            dates.add(block_date)
    if len(dates) != 1:
        return None
    candidates = [
        candidate
        for candidate in _find_versions_by_family_start(conn, family_key, next(iter(dates)))
        if int(candidate["id"]) != int(version["id"])
    ]
    return candidates[0] if len(candidates) == 1 else None


def _summary_rider_codes_from_quote(source_quote: str) -> tuple[str, ...]:
    quote = source_quote.lower()
    codes: list[str] = []
    if "fuel" in quote:
        codes.append("BA-Fuel")
    if "experience modification" in quote or "emf" in quote:
        codes.append("BA-EMF")
    if "demand side management" in quote or re.search(r"\bdsm\b", quote):
        codes.append("BA-DSM")
    if "energy efficiency" in quote or re.search(r"\bee\b", quote):
        codes.append("BA-EE")
    return tuple(dict.fromkeys(codes))


def _historical_effective_candidates(
    conn: sqlite3.Connection,
    historical_document_id: int,
) -> list[str]:
    try:
        row = conn.execute(
            """
            SELECT effective_start, snapshot_timestamp
            FROM historical_documents
            WHERE id = ?
            """,
            (historical_document_id,),
        ).fetchone()
    except sqlite3.Error:
        return []
    if not row:
        return []
    candidates: list[str] = []
    for value in (row["effective_start"], row["snapshot_timestamp"]):
        parsed = _parse_effective_candidate(str(value or ""))
        if parsed and parsed not in candidates:
            candidates.append(parsed)
    return candidates


def _parse_effective_candidate(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
    if iso_match:
        return iso_match.group(1)
    slash_match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", value)
    if slash_match:
        month, day, year = slash_match.groups()
        year_value = int(year)
        if len(year) == 2:
            year_value += 2000
        return f"{year_value:04d}-{int(month):02d}-{int(day):02d}"
    month_match = re.search(r"\b([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b", value)
    return _parse_month_date(month_match.group(1)) if month_match else None


def _find_versions_by_family_start(
    conn: sqlite3.Connection,
    family_key: str,
    effective_start: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, family_key, historical_document_id, effective_start
        FROM tariff_versions
        WHERE family_key = ?
          AND effective_start = ?
        ORDER BY id DESC
        """,
        (family_key, effective_start),
    ).fetchall()


def _find_unique_versions_by_leaf_start(
    conn: sqlite3.Connection,
    leaf_no: str,
    effective_start: str,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT id, family_key, historical_document_id, effective_start
        FROM tariff_versions
        WHERE family_key LIKE ?
          AND effective_start = ?
        ORDER BY id DESC
        """,
        (f"%-leaf-{leaf_no}", effective_start),
    ).fetchall()
    by_id: dict[int, sqlite3.Row] = {}
    for row in rows:
        if not _malformed_family_key(str(row["family_key"] or "")):
            by_id[int(row["id"])] = row
    return list(by_id.values()) if len(by_id) == 1 else []


def _find_version_by_family_start(
    conn: sqlite3.Connection,
    family_key: str,
    effective_start: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, family_key, historical_document_id, effective_start
        FROM tariff_versions
        WHERE family_key = ?
          AND effective_start = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (family_key, effective_start),
    ).fetchone()


def _unique_quote_anchors(text: str, source_quote: str) -> list[tuple[str, str]]:
    text = _normalize_text_symbols(text)
    quote = _normalize_text_symbols(source_quote.strip())
    if len(quote) < 8:
        return []
    starts = _quote_match_starts(text, quote)
    anchors: list[tuple[str, str]] = []
    for start in starts:
        before = text[max(0, start - 4000): start]
        after = text[start + len(quote): start + len(quote) + 1200]
        leaf_no = _nearest_leaf_no(before)
        effective_start = _nearest_effective_start(before) or _nearest_effective_start(after)
        if leaf_no and effective_start:
            anchors.append((leaf_no, effective_start))
    return sorted(set(anchors))


def _quote_match_starts(text: str, quote: str) -> list[int]:
    lowered = text.lower()
    needle = quote.lower()
    exact = [match.start() for match in re.finditer(re.escape(needle), lowered)]
    if exact:
        return exact
    tokens = re.findall(r"[A-Za-z0-9.]+|[$¢%]", quote)
    if len(tokens) < 4:
        return []
    pattern = r"[\s\-/–—]*".join(re.escape(token) for token in tokens)
    return [match.start() for match in re.finditer(pattern, text, re.IGNORECASE)]


def _nearest_leaf_no(context: str) -> str | None:
    matches = list(re.finditer(r"\bLeaf\s+No\.?\s+(\d{3})\b", context, re.IGNORECASE))
    return matches[-1].group(1) if matches else None


def _nearest_effective_start(context: str) -> str | None:
    patterns = (
        r"Effective\s+for\s+service\s+rendered\s+(?:from|between)\s+"
        r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
        r"Effective\s+(?:for\s+)?(?:bills|service)\s+(?:rendered\s+)?"
        r"(?:on\s+and\s+after|from)\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
        r"Effective\s+(?:Date\s*)?:?\s*([A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, context, re.IGNORECASE)
        if match:
            return _parse_month_date(match.group(1))
    iso_match = re.search(r"\bEffective\b[^\n]{0,80}\b(\d{4}-\d{2}-\d{2})\b", context, re.IGNORECASE)
    return iso_match.group(1) if iso_match else None


def _parse_month_date(value: str) -> str | None:
    months = {
        "january": "01",
        "february": "02",
        "march": "03",
        "april": "04",
        "may": "05",
        "june": "06",
        "july": "07",
        "august": "08",
        "september": "09",
        "october": "10",
        "november": "11",
        "december": "12",
    }
    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", value.strip())
    if not match:
        return None
    month = months.get(match.group(1).lower())
    if not month:
        return None
    return f"{match.group(3)}-{month}-{int(match.group(2)):02d}"


def _family_company_prefix(family_key: str) -> str:
    if family_key.startswith("nc-carolinas-"):
        return "nc-carolinas"
    if family_key.startswith("nc-progress-"):
        return "nc-progress"
    return ""


@lru_cache(maxsize=64)
def _read_source_text(source_pdf: str) -> str:
    path = Path(source_pdf)
    if not path.exists():
        return ""
    if path.suffix.lower() != ".pdf":
        try:
            return _normalize_text_symbols(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            return ""
    try:
        completed = subprocess.run(
            ["pdftotext", str(path), "-"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _normalize_text_symbols(completed.stdout or "") if completed.returncode == 0 else ""


def _normalize_text_symbols(value: str) -> str:
    return (
        value.replace("\u00c2\u00a2", "\u00a2")
        .replace("\u0412\u045e", "\u00a2")
        .replace("\u045e", "\u00a2")
    )


def _duplicate_and_conflict(
    conn: sqlite3.Connection,
    *,
    version_id: int | None,
    charge_type: str,
    rate_value: float | None,
    rate_unit: str,
    source_quote: str,
    validation_id: int | None = None,
) -> tuple[str, str]:
    if version_id is None or rate_value is None:
        return "unknown", "none"
    existing = conn.execute(
        """
        SELECT charge_type, rate_value, rate_unit, source_snippet
        FROM tariff_charges
        WHERE version_id = ?
        """,
        (version_id,),
    ).fetchall()
    normalized_source = _normalize_snippet_for_conflict(source_quote)
    for row in existing:
        same_type = str(row["charge_type"] or "") == charge_type
        same_unit = str(row["rate_unit"] or "") == rate_unit
        same_value = _float_close(row["rate_value"], rate_value)
        if same_type and same_unit and same_value:
            return "duplicate_existing", "none"
        existing_snippet = str(row["source_snippet"] or "")
        same_snippet = (
            normalized_source
            and normalized_source == _normalize_snippet_for_conflict(existing_snippet)
        )
        if same_snippet and same_type:
            return "novel", "conflicting_same_source_snippet"
    # Also check for already-proposed rows with the same signature to avoid inter-proposal duplicates.
    proposed = conn.execute(
        """
        SELECT id FROM llm_rate_charge_promotion_proposals
        WHERE version_id = ?
          AND charge_type = ?
          AND ABS(CAST(rate_value AS REAL) - ?) < 0.00001
          AND rate_unit = ?
          AND promotion_status IN ('pending', 'promoted')
          AND eligibility_status = 'eligible'
          AND (? IS NULL OR validation_id != ?)
        LIMIT 1
        """,
        (version_id, charge_type, rate_value, rate_unit,
         validation_id, validation_id),
    ).fetchone()
    if proposed:
        return "duplicate_existing", "none"
    return "novel", "none"


def _normalize_snippet_for_conflict(value: str) -> str:
    normalized = _normalize_text_symbols(value or "").lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" .;:")


def _ambiguous_numeric_table_row(
    *,
    source_quote: str,
    charge_type: str,
    rate_unit: str,
    rate_value: float | None,
) -> bool:
    quote = _normalize_text_symbols(source_quote or "")
    if not quote:
        return False
    numeric_tokens = re.findall(r"(?<![A-Za-z])(?:\(?-?\d+(?:,\d{3})*(?:\.\d+)?\)?)(?![A-Za-z])", quote)
    if len(numeric_tokens) < 5:
        return False
    unit = rate_unit.strip()
    normalized_quote = quote.lower()
    explicit_unit = any(marker in normalized_quote for marker in ("kwh", "kw", "¢", "cents", "$/", "per "))
    if rate_value is not None and explicit_unit and charge_type in {"Energy Charge", "Lighting Charge", "Rider Adjustment"}:
        exact_matches = 0
        for token in numeric_tokens:
            token_value = str(token).strip("()")
            if _float_close(token_value, rate_value):
                exact_matches += 1
        if exact_matches == 1 and any(
            marker in normalized_quote
            for marker in ("energy", "lighting", "rider", "adjustment", "fuel")
        ):
            return False
    money_values = re.findall(r"\$\s*\d+(?:,\d{3})*(?:\.\d+)?", quote)
    cent_values = re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?\s*(?:¢|cents?)", normalized_quote)
    if unit == "$/month" and len(money_values) == 1:
        return False
    if len(money_values) + len(cent_values) > 1:
        return True
    if charge_type in {"Energy Charge", "Lighting Charge", "Rider Adjustment"}:
        return not explicit_unit or len(numeric_tokens) >= 6
    return False


def _proposal_now_duplicate(conn: sqlite3.Connection, proposal: sqlite3.Row) -> bool:
    duplicate_status, _ = _duplicate_and_conflict(
        conn,
        version_id=int(proposal["version_id"]),
        charge_type=str(proposal["charge_type"]),
        rate_value=_to_float(proposal["rate_value"]),
        rate_unit=str(proposal["rate_unit"] or ""),
        source_quote=str(proposal["source_quote"] or ""),
        validation_id=int(proposal["validation_id"]) if proposal["validation_id"] is not None else None,
    )
    return duplicate_status == "duplicate_existing"


def _insert_tariff_charge(conn: sqlite3.Connection, proposal: sqlite3.Row) -> int:
    conn.execute(
        """
        INSERT INTO tariff_charges (
            version_id, family_key, charge_type, charge_label,
            rate_value, rate_unit, tou_period, season, customer_class,
            source_snippet, confidence_score, notes, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            int(proposal["version_id"]),
            proposal["family_key"],
            proposal["charge_type"],
            proposal["charge_label"],
            proposal["rate_value"],
            proposal["rate_unit"],
            proposal["tou_period"] or None,
            proposal["season"] or None,
            proposal["customer_class"] or None,
            proposal["source_quote"],
            0.92 if proposal["effective_status"] == "validated" else 0.88,
            f"Promoted from LLM effective row proposal {proposal['id']}; validation_id={proposal['validation_id']}; repair_id={proposal['repair_id']}",
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _rate_row_from_json(value: str | None, row_index: int) -> dict[str, Any]:
    try:
        rows = json.loads(value or "[]")
    except json.JSONDecodeError:
        return {}
    if not isinstance(rows, list) or row_index < 0 or row_index >= len(rows):
        return {}
    row = rows[row_index]
    return row if isinstance(row, dict) else {}


def _effective_unit(row: sqlite3.Row) -> str:
    proposed = str(row["proposed_unit"] or "").strip()
    original = str(row["unit"] or "").strip()
    inferred = str(row["inferred_unit"] or "").strip()
    if proposed:
        return proposed
    if original in {"$", "¢"} and inferred:
        return inferred
    if original in {"$", "¢", ""}:
        derived_unit, _ = _infer_unit_from_row(row)
        if derived_unit:
            return derived_unit
    return original or inferred


def _infer_unit_from_row(row: sqlite3.Row) -> tuple[str, str]:
    charge_type = str(row["charge_type"] or "")
    quote = str(row["source_quote"] or "")
    evidence = " ".join(
        str(value or "")
        for value in (
            row["source_quote"],
            row["evidence_quote"],
        )
    ).lower()
    if not quote and not evidence:
        return "", ""
    has_dollar_amount = "$" in quote or "$" in evidence
    has_cent_amount = "¢" in quote or "cents" in evidence
    if re.search(r"per\s+(?:[a-z-]+\s+){0,4}kwh\b", evidence):
        if has_cent_amount or str(row["unit"] or "").strip() == "¢/kWh":
            return "¢/kWh", "explicit_per_kwh_quote"
        if has_dollar_amount or str(row["unit"] or "").strip() == "$/kWh":
            return "$/kWh", "explicit_per_kwh_quote"
    if re.search(r"per\s+(?:[a-z-]+\s+){0,4}kw\b", evidence):
        if has_dollar_amount or str(row["unit"] or "").strip() == "$/kW":
            return "$/kW", "explicit_per_kw_quote"
    if has_dollar_amount and re.search(r"per\s+month\b|\bmonthly\b", evidence):
        return "$/month", "explicit_monthly_quote"
    fixed_monthly_charge = any(
        token in charge_type.lower()
        for token in ("fixed", "basic", "facilities", "monthly", "minimum")
    )
    if has_dollar_amount and fixed_monthly_charge and (
        "monthly rate" in evidence
        or "basic customer charge" in evidence
        or "basic facilities charge" in evidence
        or "customer charge" in evidence
    ):
        return "$/month", "fixed_charge_monthly_context"
    if ("lighting" in charge_type.lower() or "lighting" in evidence) and has_dollar_amount:
        return "$/month", "lighting_table_per_month_per_luminaire"
    return "", ""


def _normalize_charge_type(
    charge_type: str,
    row: sqlite3.Row,
    rate_row: dict[str, Any],
) -> str:
    if charge_type != "Other":
        return charge_type
    evidence = " ".join(
        str(value or "")
        for value in (
            row["source_quote"],
            row["evidence_quote"],
            rate_row.get("charge_label"),
            rate_row.get("label"),
        )
    ).lower()
    if _looks_like_rider_adjustment(evidence):
        return "Rider Adjustment"
    if (
        "storm recovery" in evidence
        or "storm cost recovery" in evidence
        or "storm securitization" in evidence
    ):
        return "Rider Adjustment"
    if "fuel" in evidence and ("adjustment" in evidence or "cost" in evidence):
        return "Rider Adjustment"
    if "demand" in evidence and ("kw" in evidence or "kilowatt" in evidence):
        return "Demand Charge"
    if (
        ("basic" in evidence or "customer" in evidence or "facilities" in evidence)
        and _effective_unit(row) == "$/month"
    ):
        return "Basic Facilities Charge"
    if "reduction in rates applicable to all customers" in evidence and "per kwh" in evidence:
        return "Rider Adjustment"
    if "energy" in evidence or "per kwh" in evidence or "per kilowatt-hour" in evidence:
        return "Energy Charge"
    if _is_lighting_monthly_row(rate_row, row):
        return "Lighting Charge"
    return charge_type


def _looks_like_rider_adjustment(evidence: str) -> bool:
    if not evidence:
        return False
    if "regulatory fee" in evidence:
        return True
    if any(
        token in evidence
        for token in (
            "adjustment",
            "surcharge",
            "recovery",
            "adder",
            "incentiv",
            "discount",
            "rebate",
            "dsm",
            "saved",
            "savings",
        )
    ):
        return True
    if "credit" in evidence and any(
        token in evidence
        for token in ("energy", "load", "participant", "monthly", "rider", "conservation")
    ):
        return True
    return False


def _malformed_family_key(family_key: str) -> bool:
    normalized = family_key.strip().lower().replace("\\", "/")
    return (
        not normalized.startswith("nc-")
        or normalized.startswith("/")
        or "/pdfs/" in normalized
        or normalized.endswith(".pdf")
    )


def _canonical_family_key_from_path(family_key: str) -> str | None:
    normalized = family_key.strip().lower().replace("\\", "/")
    leaf_match = re.search(r"leaf[-_\s]*(?:no[-_\s]*)?(\d{3})", normalized)
    if not leaf_match:
        return None
    company = _company_prefix_from_text(normalized)
    if not company:
        return None
    return f"{company}-leaf-{leaf_match.group(1)}"


def _company_prefix_from_text(value: str) -> str:
    normalized = value.lower()
    if "dep-nc" in normalized or "progress" in normalized:
        return "nc-progress"
    if "dec-nc" in normalized or "carolinas" in normalized or "electric-nc" in normalized:
        return "nc-carolinas"
    return ""


def _charge_label(charge_type: str, rate_row: dict[str, Any]) -> str:
    if charge_type == "Rider Adjustment" and _is_storm_recovery_row(rate_row):
        pieces = ["Storm Recovery Charge"]
        customer_class = str(rate_row.get("customer_class") or "").strip()
        if customer_class:
            pieces.append(customer_class)
        return " - ".join(pieces)
    if charge_type == "Lighting Charge":
        suffix = _other_label_suffix(rate_row)
        return f"Lighting Charge - {suffix}" if suffix else "Lighting Charge"
    pieces = [charge_type]
    for key in ("season", "tou_period", "customer_class"):
        value = str(rate_row.get(key) or "").strip()
        if value:
            pieces.append(value)
    return " - ".join(pieces)


def _is_storm_recovery_row(rate_row: dict[str, Any]) -> bool:
    evidence = " ".join(
        str(value or "")
        for value in (
            rate_row.get("source_quote"),
            rate_row.get("charge_label"),
            rate_row.get("label"),
        )
    ).lower()
    return (
        "storm recovery" in evidence
        or "storm cost recovery" in evidence
        or "storm securitization" in evidence
    )


def _is_lighting_monthly_row(rate_row: dict[str, Any], row: sqlite3.Row) -> bool:
    if _effective_unit(row) != "$/month":
        return False
    family_key = str(row["source_pdf"] or "").lower()
    evidence = " ".join(
        str(value or "")
        for value in (
            row["source_quote"],
            rate_row.get("source_quote"),
            rate_row.get("charge_label"),
            rate_row.get("label"),
        )
    ).lower()
    return (
        "suburban" in evidence
        or "luminaire" in evidence
        or "lighting" in evidence
        or "schedule-pl" in family_key
    )


def _other_label_suffix(rate_row: dict[str, Any]) -> str:
    label = str(rate_row.get("charge_label") or rate_row.get("label") or "").strip()
    if label.lower().startswith("other - "):
        return label[8:].strip()
    customer_class = str(rate_row.get("customer_class") or "").strip()
    return customer_class


def _summarize_proposals(proposals: list[PromotionProposal], *, execute: bool) -> dict[str, Any]:
    by_eligibility: dict[str, int] = {}
    by_duplicate: dict[str, int] = {}
    by_effective: dict[str, int] = {}
    for proposal in proposals:
        by_eligibility[proposal.eligibility_status] = by_eligibility.get(proposal.eligibility_status, 0) + 1
        by_duplicate[proposal.duplicate_status] = by_duplicate.get(proposal.duplicate_status, 0) + 1
        by_effective[proposal.effective_status] = by_effective.get(proposal.effective_status, 0) + 1
    return {
        "evaluated": len(proposals),
        "execute": execute,
        "eligibility_counts": dict(sorted(by_eligibility.items())),
        "duplicate_counts": dict(sorted(by_duplicate.items())),
        "effective_status_counts": dict(sorted(by_effective.items())),
    }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_close(a: Any, b: float) -> bool:
    parsed = _to_float(a)
    return parsed is not None and math.isclose(parsed, b, rel_tol=1e-7, abs_tol=0.00001)
