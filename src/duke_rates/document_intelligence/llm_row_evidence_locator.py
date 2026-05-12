from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from duke_rates.document_intelligence.llm_extraction_validation import (
    _load_source_text,
    _quote_in_text,
    _unit_grounded,
)
from duke_rates.document_intelligence.schema_extraction import ALLOWED_UNITS
from duke_rates.document_intelligence.schema_extraction import ALLOWED_CHARGE_TYPES


_PROMPT = """\
You are locating missing evidence for one tariff rate row.

Your task is NOT to extract new rates. Your task is only to identify the exact
nearby source text that proves the unit for the existing row.

## Existing row
- charge_type: {charge_type}
- value: {value}
- current_unit: {unit}
- source_quote: {source_quote}
- current_issues: {issues}

## Allowed units
{allowed_units}

## Nearby source context
```
{context}
```

## Candidate evidence clues found deterministically
These are nearby lines that may prove the unit. Use one of these only if it
actually supports the row; otherwise return supported_unit="".
```
{evidence_clues}
```

## Instructions
1. Return only JSON.
2. Pick supported_unit from the allowed units, or "" if the context does not prove a unit.
3. evidence_quote must be copied exactly from the context and should contain the table header, line label, or phrase that proves the unit.
4. Do not use outside knowledge. If the unit is only guessed, return supported_unit="".
5. If current_unit conflicts with context, explain that briefly in reason.
6. If the row value is a bare number but nearby headers say "MONTHLY RATE",
   "Monthly Charge", "per month", "Per Customer", or "Per Luminaire", that is
   valid evidence for supported_unit="$/month". Use the exact header text as
   evidence_quote.
7. If the row value is a bare number and no nearby header proves dollars or a
   billing period, return supported_unit="".

Respond as:
{{
  "supported_unit": "$/month",
  "evidence_quote": "Lamp Rating Per Month Per Luminaire",
  "reason": "The table header says dollar values are monthly luminaire charges.",
  "confidence": 0.85
}}
"""

_RECLASSIFY_PROMPT = """\
You are reviewing one tariff rate row whose extracted classification conflicts
with deterministic source evidence.

Your task is NOT to extract new rates. Your task is only to decide whether the
existing charge_type and unit should be repaired based on the nearby source
context.

## Existing row
- charge_type: {charge_type}
- value: {value}
- current_unit: {unit}
- inferred_unit_from_deterministic_context: {inferred_unit}
- source_quote: {source_quote}
- current_issues: {issues}

## Allowed charge types
{allowed_charge_types}

## Allowed units
{allowed_units}

## Nearby source context
```
{context}
```

## Candidate evidence clues found deterministically
```
{evidence_clues}
```

## Instructions
1. Return only JSON.
2. proposed_charge_type must be one of the allowed charge types.
3. proposed_unit must be one of the allowed units, or "" if the unit is not proven.
4. evidence_quote must be copied exactly from the context and prove the proposed unit and classification.
5. If this is a lighting table with monthly charge headers, prefer proposed_charge_type="Lighting Charge" and proposed_unit="$/month".
6. Do not use outside knowledge. If the row cannot be repaired from the context, return proposed_charge_type="" and proposed_unit="".

Respond as:
{{
  "proposed_charge_type": "Lighting Charge",
  "proposed_unit": "$/month",
  "evidence_quote": "Lamp Rating Per Month Per Luminaire",
  "reason": "The row belongs to a lighting table whose header defines values as monthly luminaire charges.",
  "confidence": 0.85
}}
"""


class RowEvidenceProposal(BaseModel):
    supported_unit: str = Field(default="")
    evidence_quote: str = Field(default="")
    reason: str = Field(default="")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RowReclassificationProposal(BaseModel):
    proposed_charge_type: str = Field(default="")
    proposed_unit: str = Field(default="")
    evidence_quote: str = Field(default="")
    reason: str = Field(default="")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


@dataclass
class EvidenceRepairResult:
    validation_id: int
    extraction_id: int
    row_index: int
    supported_unit: str
    evidence_quote: str
    confidence: float
    reason: str
    validation_status: str
    validation_issues: list[str]
    model: str
    proposed_charge_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "validation_id": self.validation_id,
            "extraction_id": self.extraction_id,
            "row_index": self.row_index,
            "supported_unit": self.supported_unit,
            "proposed_charge_type": self.proposed_charge_type,
            "evidence_quote": self.evidence_quote,
            "confidence": self.confidence,
            "reason": self.reason,
            "validation_status": self.validation_status,
            "validation_issues": self.validation_issues,
            "model": self.model,
        }


class LLMRowEvidenceLocator:
    """LLM-assisted, deterministic-gated evidence locator for row repairs."""

    def __init__(
        self,
        orchestrator: Any,
        db_path: Path | str,
        *,
        role: str = "structured_rate_classify",
        context_window: int = 1200,
    ) -> None:
        self._orch = orchestrator
        self._db_path = Path(db_path)
        self._role = role
        self._context_window = context_window

    def locate(
        self,
        *,
        issue: str = "",
        limit: int = 25,
        execute: bool = False,
        min_confidence: float = 0.6,
    ) -> dict[str, Any]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            _ensure_repair_table(conn)
            rows = self._select_rows(conn, issue=issue, limit=limit)
            results: list[EvidenceRepairResult] = []
            for row in rows:
                result = self._locate_one(conn, row, min_confidence=min_confidence)
                results.append(result)
                if execute:
                    self._persist_result(conn, row, result)
            if execute:
                conn.commit()
            return {
                "summary": _summarize(results, execute=execute),
                "rows": [r.to_dict() for r in results],
            }
        finally:
            conn.close()

    def reclassify_conflicts(
        self,
        *,
        limit: int = 25,
        execute: bool = False,
        min_confidence: float = 0.6,
    ) -> dict[str, Any]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            _ensure_repair_table(conn)
            rows = self._select_rows(
                conn,
                issue="unit_conflicts_with_inferred",
                limit=limit,
            )
            results: list[EvidenceRepairResult] = []
            for row in rows:
                result = self._reclassify_one(conn, row, min_confidence=min_confidence)
                results.append(result)
                if execute:
                    self._persist_result(conn, row, result, repair_type="row_reclassification")
            if execute:
                conn.commit()
            return {
                "summary": _summarize(results, execute=execute),
                "rows": [r.to_dict() for r in results],
            }
        finally:
            conn.close()

    def apply_deterministic_repairs(
        self,
        *,
        limit: int = 100,
        execute: bool = False,
    ) -> dict[str, Any]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            _ensure_repair_table(conn)
            rows = self._select_deterministic_lighting_conflicts(conn, limit=limit)
            results: list[EvidenceRepairResult] = []
            for row in rows:
                result = EvidenceRepairResult(
                    validation_id=int(row["id"]),
                    extraction_id=int(row["extraction_id"]),
                    row_index=int(row["row_index"]),
                    supported_unit="$/month",
                    evidence_quote="Lamp Rating Per Month Per Luminaire",
                    confidence=1.0,
                    reason=(
                        "Deterministic lighting-table repair: row has a unit conflict "
                        "and prior context inference grounded the row in a monthly "
                        "luminaire table."
                    ),
                    validation_status="accepted",
                    validation_issues=[],
                    model="deterministic",
                    proposed_charge_type="Lighting Charge",
                )
                results.append(result)
                if execute:
                    self._persist_result(
                        conn,
                        row,
                        result,
                        repair_type="deterministic_lighting_table_repair",
                    )
            if execute:
                conn.commit()
            return {
                "summary": _summarize(results, execute=execute),
                "rows": [r.to_dict() for r in results],
            }
        finally:
            conn.close()

    def effective_status_report(self) -> dict[str, Any]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            _ensure_repair_table(conn)
            rows = conn.execute(
                """
                SELECT
                    CASE
                        WHEN rv.recommended_status = 'validated' THEN 'validated'
                        WHEN rv.recommended_status = 'rejected' THEN 'rejected'
                        WHEN EXISTS (
                            SELECT 1
                            FROM llm_candidate_rate_row_repairs rr
                            WHERE rr.validation_id = rv.id
                              AND rr.validation_status = 'accepted'
                        ) THEN 'validated_with_repair'
                        ELSE 'review_candidate'
                    END AS effective_status,
                    COUNT(*) AS count
                FROM llm_candidate_rate_row_validations rv
                GROUP BY effective_status
                ORDER BY effective_status
                """
            ).fetchall()
            repair_rows = conn.execute(
                """
                SELECT repair_type, validation_status, COUNT(*) AS count
                FROM llm_candidate_rate_row_repairs
                GROUP BY repair_type, validation_status
                ORDER BY repair_type, validation_status
                """
            ).fetchall()
            unresolved = conn.execute(
                """
                SELECT COUNT(*)
                FROM llm_candidate_rate_row_validations rv
                WHERE rv.recommended_status = 'review_candidate'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM llm_candidate_rate_row_repairs rr
                      WHERE rr.validation_id = rv.id
                        AND rr.validation_status = 'accepted'
                  )
                """
            ).fetchone()[0]
            return {
                "effective_status_counts": {
                    str(row["effective_status"]): int(row["count"]) for row in rows
                },
                "repair_counts": [
                    {
                        "repair_type": str(row["repair_type"]),
                        "validation_status": str(row["validation_status"]),
                        "count": int(row["count"]),
                    }
                    for row in repair_rows
                ],
                "unresolved_review_rows": int(unresolved),
            }
        finally:
            conn.close()

    def _select_rows(
        self,
        conn: sqlite3.Connection,
        *,
        issue: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        where = ["rv.recommended_status = 'review_candidate'"]
        params: list[Any] = []
        if issue:
            where.append("rv.issues_json LIKE ?")
            params.append(f"%{issue}%")
        where.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM llm_candidate_rate_row_repairs rr
                WHERE rr.validation_id = rv.id
                  AND rr.validation_status = 'accepted'
            )
            """
        )
        params.append(max(1, int(limit)))
        return conn.execute(
            f"""
            SELECT rv.*
            FROM llm_candidate_rate_row_validations rv
            WHERE {" AND ".join(where)}
            ORDER BY rv.validation_score DESC, rv.extraction_id DESC, rv.row_index
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    def _select_deterministic_lighting_conflicts(
        self,
        conn: sqlite3.Connection,
        *,
        limit: int,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT rv.*
            FROM llm_candidate_rate_row_validations rv
            WHERE rv.recommended_status = 'review_candidate'
              AND rv.issues_json LIKE '%unit_conflicts_with_inferred%'
              AND rv.inferred_unit = '$/month'
              AND rv.inferred_unit_reason = 'lighting_table_per_month_per_luminaire'
              AND rv.source_quote LIKE '%$%'
              AND (
                    rv.charge_type = 'Lighting Charge'
                 OR rv.charge_type = 'Energy Charge'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM llm_candidate_rate_row_repairs rr
                  WHERE rr.validation_id = rv.id
                    AND rr.validation_status = 'accepted'
              )
            ORDER BY rv.validation_score DESC, rv.extraction_id DESC, rv.row_index
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()

    def _locate_one(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        min_confidence: float,
    ) -> EvidenceRepairResult:
        source_text = _load_source_text(
            conn,
            source_pdf=str(row["source_pdf"] or ""),
            historical_document_id=row["historical_document_id"],
        )
        context = _context_around_quote(
            str(row["source_quote"] or ""),
            source_text,
            window=self._context_window,
        )
        evidence_clues = _evidence_clues(context)
        prompt = _PROMPT.format(
            charge_type=row["charge_type"] or "",
            value=row["value"],
            unit=row["unit"] or "",
            source_quote=row["source_quote"] or "",
            issues=row["issues_json"] or "[]",
            allowed_units=", ".join(ALLOWED_UNITS),
            context=context,
            evidence_clues=evidence_clues or "(none)",
        )
        run_result = self._orch.generate_json(
            role=self._role,
            prompt=prompt,
            schema=RowEvidenceProposal,
            subject_kind="llm_candidate_rate_row_validation",
            subject_id=str(row["id"]),
            stage="locate_row_unit_evidence",
        )

        if run_result.status not in ("ok", "fallback_used"):
            return EvidenceRepairResult(
                validation_id=int(row["id"]),
                extraction_id=int(row["extraction_id"]),
                row_index=int(row["row_index"]),
                supported_unit="",
                proposed_charge_type="",
                evidence_quote="",
                confidence=0.0,
                reason=f"LLM call failed: {run_result.status}",
                validation_status="rejected",
                validation_issues=["llm_call_failed"],
                model=run_result.model or "",
            )

        proposal: RowEvidenceProposal = run_result.result
        validation_status, validation_issues = _validate_proposal(
            proposal,
            source_text=source_text,
            min_confidence=min_confidence,
        )
        return EvidenceRepairResult(
            validation_id=int(row["id"]),
            extraction_id=int(row["extraction_id"]),
            row_index=int(row["row_index"]),
            supported_unit=proposal.supported_unit.strip(),
            proposed_charge_type="",
            evidence_quote=proposal.evidence_quote.strip(),
            confidence=float(proposal.confidence or 0.0),
            reason=proposal.reason.strip(),
            validation_status=validation_status,
            validation_issues=validation_issues,
            model=run_result.model or "",
        )

    def _reclassify_one(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        min_confidence: float,
    ) -> EvidenceRepairResult:
        source_text = _load_source_text(
            conn,
            source_pdf=str(row["source_pdf"] or ""),
            historical_document_id=row["historical_document_id"],
        )
        context = _context_around_quote(
            str(row["source_quote"] or ""),
            source_text,
            window=self._context_window,
        )
        evidence_clues = _evidence_clues(context)
        prompt = _RECLASSIFY_PROMPT.format(
            charge_type=row["charge_type"] or "",
            value=row["value"],
            unit=row["unit"] or "",
            inferred_unit=row["inferred_unit"] or "",
            source_quote=row["source_quote"] or "",
            issues=row["issues_json"] or "[]",
            allowed_charge_types=", ".join(ALLOWED_CHARGE_TYPES),
            allowed_units=", ".join(ALLOWED_UNITS),
            context=context,
            evidence_clues=evidence_clues or "(none)",
        )
        run_result = self._orch.generate_json(
            role=self._role,
            prompt=prompt,
            schema=RowReclassificationProposal,
            subject_kind="llm_candidate_rate_row_validation",
            subject_id=str(row["id"]),
            stage="reclassify_row_conflict",
        )

        if run_result.status not in ("ok", "fallback_used"):
            return EvidenceRepairResult(
                validation_id=int(row["id"]),
                extraction_id=int(row["extraction_id"]),
                row_index=int(row["row_index"]),
                supported_unit="",
                proposed_charge_type="",
                evidence_quote="",
                confidence=0.0,
                reason=f"LLM call failed: {run_result.status}",
                validation_status="rejected",
                validation_issues=["llm_call_failed"],
                model=run_result.model or "",
            )

        proposal: RowReclassificationProposal = run_result.result
        validation_status, validation_issues = _validate_reclassification_proposal(
            proposal,
            row=row,
            source_text=source_text,
            min_confidence=min_confidence,
        )
        return EvidenceRepairResult(
            validation_id=int(row["id"]),
            extraction_id=int(row["extraction_id"]),
            row_index=int(row["row_index"]),
            supported_unit=proposal.proposed_unit.strip(),
            proposed_charge_type=proposal.proposed_charge_type.strip(),
            evidence_quote=proposal.evidence_quote.strip(),
            confidence=float(proposal.confidence or 0.0),
            reason=proposal.reason.strip(),
            validation_status=validation_status,
            validation_issues=validation_issues,
            model=run_result.model or "",
        )

    def _persist_result(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        result: EvidenceRepairResult,
        *,
        repair_type: str = "unit_evidence",
    ) -> None:
        conn.execute(
            """
            INSERT INTO llm_candidate_rate_row_repairs (
                validation_id, extraction_id, row_index, repair_type,
                original_charge_type, proposed_charge_type,
                original_unit, proposed_unit, evidence_quote, confidence,
                reason, validation_status, validation_issues_json, model,
                model_role, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                result.validation_id,
                result.extraction_id,
                result.row_index,
                repair_type,
                row["charge_type"] or "",
                result.proposed_charge_type,
                row["unit"] or "",
                result.supported_unit,
                result.evidence_quote,
                result.confidence,
                result.reason,
                result.validation_status,
                json.dumps(result.validation_issues),
                result.model,
                self._role,
                "accepted" if result.validation_status == "accepted" else "rejected",
            ),
        )


def _validate_proposal(
    proposal: RowEvidenceProposal,
    *,
    source_text: str,
    min_confidence: float,
) -> tuple[str, list[str]]:
    issues: list[str] = []
    unit = proposal.supported_unit.strip()
    evidence = proposal.evidence_quote.strip()
    confidence = float(proposal.confidence or 0.0)

    if unit not in ALLOWED_UNITS:
        issues.append("unsupported_unit")
    if not evidence:
        issues.append("evidence_quote_missing")
    elif not _quote_in_text(evidence, source_text):
        issues.append("evidence_quote_not_grounded")
    elif unit and not _unit_grounded(unit, evidence, source_text):
        issues.append("unit_not_grounded_by_evidence")
    if confidence < min_confidence:
        issues.append("low_repair_confidence")

    return ("accepted" if not issues else "rejected", issues)


def _validate_reclassification_proposal(
    proposal: RowReclassificationProposal,
    *,
    row: sqlite3.Row,
    source_text: str,
    min_confidence: float,
) -> tuple[str, list[str]]:
    issues: list[str] = []
    charge_type = proposal.proposed_charge_type.strip()
    unit = proposal.proposed_unit.strip()
    evidence = proposal.evidence_quote.strip()
    confidence = float(proposal.confidence or 0.0)

    if charge_type not in ALLOWED_CHARGE_TYPES:
        issues.append("unsupported_charge_type")
    if unit not in ALLOWED_UNITS:
        issues.append("unsupported_unit")
    if not evidence:
        issues.append("evidence_quote_missing")
    elif not _quote_in_text(evidence, source_text):
        issues.append("evidence_quote_not_grounded")
    elif unit and not _unit_grounded(unit, evidence, source_text):
        issues.append("unit_not_grounded_by_evidence")
    if confidence < min_confidence:
        issues.append("low_repair_confidence")
    if charge_type == str(row["charge_type"] or "").strip() and unit == str(row["unit"] or "").strip():
        issues.append("no_reclassification_change")

    return ("accepted" if not issues else "rejected", issues)


def _context_around_quote(quote: str, source_text: str, *, window: int) -> str:
    if not quote or not source_text:
        return source_text[: window * 2]
    idx = source_text.find(quote)
    if idx < 0:
        idx = source_text.lower().find(quote.lower())
    if idx < 0:
        return source_text[: window * 2]
    start = max(0, idx - window)
    end = min(len(source_text), idx + len(quote) + window)
    char_context = source_text[start:end]
    table_context = _line_block_around_quote(quote, source_text)
    if not table_context or table_context in char_context:
        return char_context
    return f"{table_context}\n\n--- nearby character context ---\n{char_context}"


def _line_block_around_quote(quote: str, source_text: str) -> str:
    lines = source_text.splitlines()
    quote_norm = quote.strip().lower()
    if not quote_norm:
        return ""
    row_index = -1
    for i, line in enumerate(lines):
        if quote_norm in line.lower():
            row_index = i
            break
    if row_index < 0:
        return ""

    start = max(0, row_index - 35)
    for i in range(row_index, start, -1):
        line = lines[i].strip().lower()
        if line in {"rate", "monthly rate"} or line.startswith("monthly rate"):
            start = i
            break
    end = min(len(lines), row_index + 20)
    return "\n".join(lines[start:end]).strip()


def _evidence_clues(context: str) -> str:
    clues: list[str] = []
    for raw_line in context.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if any(
            token in lower
            for token in (
                "monthly rate",
                "monthly charge",
                "per month",
                "$/month",
                "per customer",
                "per luminaire",
                "per kwh",
                "cents per kwh",
                "¢/kwh",
                "per kw",
                "$/kw",
            )
        ):
            if line not in clues:
                clues.append(line)
        if len(clues) >= 12:
            break
    return "\n".join(clues)


def _ensure_repair_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_candidate_rate_row_repairs (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            validation_id               INTEGER NOT NULL,
            extraction_id               INTEGER NOT NULL,
            row_index                   INTEGER NOT NULL,
            repair_type                 TEXT NOT NULL,
            original_charge_type        TEXT,
            proposed_charge_type        TEXT,
            original_unit               TEXT,
            proposed_unit               TEXT,
            evidence_quote              TEXT,
            confidence                  REAL NOT NULL DEFAULT 0.0,
            reason                      TEXT,
            validation_status           TEXT NOT NULL,
            validation_issues_json      TEXT NOT NULL DEFAULT '[]',
            model                       TEXT,
            model_role                  TEXT,
            status                      TEXT NOT NULL DEFAULT 'pending',
            created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_row_repairs_validation
        ON llm_candidate_rate_row_repairs(validation_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_llm_row_repairs_status
        ON llm_candidate_rate_row_repairs(status, validation_status, created_at);
        """
    )
    _ensure_column(conn, "llm_candidate_rate_row_repairs", "original_charge_type", "TEXT")
    _ensure_column(conn, "llm_candidate_rate_row_repairs", "proposed_charge_type", "TEXT")


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    existing = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _summarize(
    results: list[EvidenceRepairResult],
    *,
    execute: bool,
) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for result in results:
        by_status[result.validation_status] = by_status.get(result.validation_status, 0) + 1
    return {
        "evaluated": len(results),
        "execute": execute,
        "accepted": by_status.get("accepted", 0),
        "rejected": by_status.get("rejected", 0),
        "validation_status_counts": dict(sorted(by_status.items())),
    }
