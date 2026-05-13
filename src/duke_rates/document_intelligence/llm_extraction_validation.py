from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_NUM_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\$?\d+(?:,\d{3})*(?:\.\d+)?")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass
class RowValidation:
    row_index: int
    charge_type: str
    value: float | None
    unit: str
    confidence: float
    source_quote: str
    source_quote_grounded: bool
    value_grounded: bool
    unit_grounded: bool
    inferred_unit: str = ""
    inferred_unit_reason: str = ""
    issues: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def recommended_status(self) -> str:
        if self.score >= 0.8 and not self.issues:
            return "validated"
        if (
            "source_quote_not_grounded" in self.issues
            or "value_not_grounded" in self.issues
        ):
            return "rejected"
        return "review_candidate"


@dataclass
class ExtractionValidation:
    extraction_id: int
    historical_document_id: int | None
    source_pdf: str
    current_status: str
    recommended_status: str
    extraction_confidence: float
    row_count: int
    validated_row_count: int
    rejected_row_count: int
    needs_review_row_count: int
    score: float
    issues: list[str]
    row_results: list[RowValidation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "extraction_id": self.extraction_id,
            "historical_document_id": self.historical_document_id,
            "source_pdf": self.source_pdf,
            "current_status": self.current_status,
            "recommended_status": self.recommended_status,
            "extraction_confidence": self.extraction_confidence,
            "row_count": self.row_count,
            "validated_row_count": self.validated_row_count,
            "rejected_row_count": self.rejected_row_count,
            "needs_review_row_count": self.needs_review_row_count,
            "score": self.score,
            "issues": self.issues,
            "row_results": [
                {
                    "row_index": row.row_index,
                    "charge_type": row.charge_type,
                    "value": row.value,
                    "unit": row.unit,
                    "confidence": row.confidence,
                    "source_quote": row.source_quote,
                    "source_quote_grounded": row.source_quote_grounded,
                    "value_grounded": row.value_grounded,
                    "unit_grounded": row.unit_grounded,
                    "inferred_unit": row.inferred_unit,
                    "inferred_unit_reason": row.inferred_unit_reason,
                    "issues": row.issues,
                    "score": row.score,
                    "recommended_status": row.recommended_status,
                }
                for row in self.row_results
            ],
        }


def validate_candidate_extractions(
    db_path: Path,
    *,
    limit: int = 50,
    status: str = "candidate",
    extraction_id: int | None = None,
    historical_document_id: int | None = None,
    min_extraction_confidence: float = 0.0,
    execute: bool = False,
) -> dict[str, Any]:
    """Validate LLM candidate rate extractions against source text.

    The validator is intentionally conservative. ``--execute`` only updates
    candidate rows to ``validated``, ``review_candidate``, or ``rejected``; it
    never promotes rows into production tariff charges.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_row_validation_table(conn)
        candidates = _load_candidates(
            conn,
            limit=limit,
            status=status,
            extraction_id=extraction_id,
            historical_document_id=historical_document_id,
            min_extraction_confidence=min_extraction_confidence,
        )
        results = [_validate_extraction(conn, row) for row in candidates]

        updates: list[dict[str, Any]] = []
        row_validation_upserts = 0
        if execute:
            for result in results:
                row_validation_upserts += _persist_row_validations(conn, result)
                if result.recommended_status not in {"validated", "review_candidate", "rejected"}:
                    continue
                if result.current_status == result.recommended_status:
                    continue
                conn.execute(
                    """
                    UPDATE llm_candidate_rate_extractions
                    SET status = ?
                    WHERE id = ?
                    """,
                    (result.recommended_status, result.extraction_id),
                )
                updates.append(
                    {
                        "extraction_id": result.extraction_id,
                        "from": result.current_status,
                        "to": result.recommended_status,
                    }
                )
            conn.commit()

        return {
            "summary": _summarize(results, updates=updates, execute=execute),
            "rows": [result.to_dict() for result in results],
            "updates": updates,
            "row_validation_upserts": row_validation_upserts,
        }
    finally:
        conn.close()


def _load_candidates(
    conn: sqlite3.Connection,
    *,
    limit: int,
    status: str,
    extraction_id: int | None,
    historical_document_id: int | None,
    min_extraction_confidence: float,
) -> list[sqlite3.Row]:
    where = ["extraction_confidence >= ?"]
    params: list[Any] = [float(min_extraction_confidence)]
    if extraction_id is not None:
        where.append("id = ?")
        params.append(int(extraction_id))
    elif historical_document_id is not None:
        where.append("historical_document_id = ?")
        params.append(int(historical_document_id))
    elif status:
        where.append("status = ?")
        params.append(status)

    params.append(max(1, int(limit)))
    return conn.execute(
        f"""
        SELECT *
        FROM llm_candidate_rate_extractions
        WHERE {" AND ".join(where)}
        ORDER BY extraction_confidence DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()


def _validate_extraction(conn: sqlite3.Connection, row: sqlite3.Row) -> ExtractionValidation:
    rate_rows = _loads_list(row["rate_rows_json"])
    source_text = _load_source_text(
        conn,
        source_pdf=str(row["source_pdf"] or ""),
        historical_document_id=row["historical_document_id"],
    )
    issues: list[str] = []
    if not source_text.strip():
        issues.append("source_text_missing")

    row_results = [
        _validate_rate_row(i, item, source_text)
        for i, item in enumerate(rate_rows)
        if isinstance(item, dict)
    ]
    if not rate_rows:
        issues.append("no_rate_rows")
    if len(row_results) != len(rate_rows):
        issues.append("malformed_rate_rows")

    validated = [r for r in row_results if r.recommended_status == "validated"]
    rejected = [r for r in row_results if r.recommended_status == "rejected"]
    needs_review = [r for r in row_results if r.recommended_status == "review_candidate"]

    extraction_confidence = _to_float(row["extraction_confidence"])
    avg_score = _average([r.score for r in row_results])
    score = round((avg_score * 0.75) + (extraction_confidence * 0.25), 4)

    if not row_results:
        recommended_status = "rejected"
    elif len(validated) == len(row_results) and score >= 0.8:
        recommended_status = "validated"
    elif len(rejected) == len(row_results):
        recommended_status = "rejected"
    else:
        recommended_status = "review_candidate"

    return ExtractionValidation(
        extraction_id=int(row["id"]),
        historical_document_id=(
            int(row["historical_document_id"]) if row["historical_document_id"] is not None else None
        ),
        source_pdf=str(row["source_pdf"] or ""),
        current_status=str(row["status"] or ""),
        recommended_status=recommended_status,
        extraction_confidence=extraction_confidence,
        row_count=len(row_results),
        validated_row_count=len(validated),
        rejected_row_count=len(rejected),
        needs_review_row_count=len(needs_review),
        score=score,
        issues=issues,
        row_results=row_results,
    )


def _validate_rate_row(row_index: int, row: dict[str, Any], source_text: str) -> RowValidation:
    quote = str(row.get("source_quote") or "").strip()
    value = _to_optional_float(row.get("value"))
    unit = str(row.get("unit") or "").strip()
    confidence = _to_float(row.get("confidence"))
    issues: list[str] = []

    quote_grounded = bool(quote) and _quote_in_text(quote, source_text)
    if not quote:
        issues.append("source_quote_missing")
    elif not quote_grounded:
        issues.append("source_quote_not_grounded")

    value_grounded = value is not None and _value_in_quote(value, quote)
    if value is None:
        issues.append("value_missing")
    elif not value_grounded:
        issues.append("value_not_grounded")

    inferred_unit, inferred_unit_reason = _infer_unit(
        charge_type=str(row.get("charge_type") or ""),
        unit=unit,
        quote=quote,
        source_text=source_text,
    )
    unit_grounded = _unit_grounded(unit, quote, source_text)
    unit_supported_by_inference = bool(inferred_unit) and (
        not unit
        or unit == "$"
        or unit.lower() == inferred_unit.lower()
    )
    unit_conflicts_with_inference = bool(inferred_unit and unit) and (
        unit != "$" and unit.lower() != inferred_unit.lower()
    )
    if unit_supported_by_inference:
        unit_grounded = True
    elif unit_conflicts_with_inference:
        unit_grounded = False
    if not unit:
        if not inferred_unit:
            issues.append("unit_missing")
    elif not unit_grounded:
        issues.append("unit_not_grounded")
        if unit_conflicts_with_inference:
            issues.append("unit_conflicts_with_inferred")

    evidence_grounded = quote_grounded and value_grounded and unit_grounded
    if confidence < 0.5 and not evidence_grounded:
        issues.append("low_row_confidence")

    score = 0.0
    if quote_grounded:
        score += 0.4
    if value_grounded:
        score += 0.3
    if unit_grounded:
        score += 0.2
    score += min(max(confidence, 0.0), 1.0) * 0.1

    return RowValidation(
        row_index=row_index,
        charge_type=str(row.get("charge_type") or ""),
        value=value,
        unit=unit,
        confidence=confidence,
        source_quote=quote,
        source_quote_grounded=quote_grounded,
        value_grounded=value_grounded,
        unit_grounded=unit_grounded,
        inferred_unit=inferred_unit,
        inferred_unit_reason=inferred_unit_reason,
        issues=issues,
        score=round(score, 4),
    )


def _load_source_text(
    conn: sqlite3.Connection,
    *,
    source_pdf: str,
    historical_document_id: int | None,
) -> str:
    pages = conn.execute(
        """
        SELECT text_content
        FROM ncuc_page_artifacts
        WHERE source_pdf = ?
        ORDER BY page_number
        """,
        (source_pdf,),
    ).fetchall()
    page_text = "\n".join(str(row[0] or "") for row in pages)
    if page_text.strip():
        return page_text

    if historical_document_id is not None:
        raw = conn.execute(
            """
            SELECT raw_text_path
            FROM historical_documents
            WHERE id = ?
            """,
            (int(historical_document_id),),
        ).fetchone()
        if raw and raw[0]:
            return _read_text_file(str(raw[0]))
    return ""


def _quote_in_text(quote: str, source_text: str) -> bool:
    quote_norm = _normalize_text(quote)
    text_norm = _normalize_text(source_text)
    if not quote_norm or not text_norm:
        return False
    if quote_norm in text_norm:
        return True

    tokens = [tok for tok in _TOKEN_RE.findall(quote_norm) if len(tok) > 2]
    if len(tokens) < 3:
        return False
    present = sum(1 for tok in tokens if tok in text_norm)
    return present / len(tokens) >= 0.8


def _value_in_quote(value: float, quote: str) -> bool:
    for match in _NUM_RE.findall(quote):
        parsed = _to_optional_float(match.replace("$", "").replace(",", ""))
        if parsed is None:
            continue
        if math.isclose(parsed, value, rel_tol=1e-6, abs_tol=0.0005):
            return True
    return False


def _unit_grounded(unit: str, quote: str, source_text: str = "") -> bool:
    if not unit:
        return False
    q = quote.lower()
    context = _quote_context(quote, source_text).lower()
    haystack = f"{q} {context}".strip()
    normalized = unit.lower().replace("¢", "cents").replace("$", "dollars")
    checks = {
        "$/kwh": [r"\$/kwh\b", r"\$\s*\d[\d,]*(?:\.\d+)?\s*/\s*kwh\b", r"per\s+(?:[a-z-]+\s+){0,4}kwh\b", r"per\s+kilowatt-hour\b", r"dollars?\s+per\s+(?:[a-z-]+\s+){0,4}kwh\b", r"dollars?\s+per\s+kilowatt-hour\b"],
        "¢/kwh": [r"¢/kwh\b", r"¢\s+per\s+kilowatt-hour\b", r"cents?\s+per\s+(?:[a-z-]+\s+){0,4}kwh\b", r"cents?\s+per\s+kilowatt-hour\b", r"per\s+(?:[a-z-]+\s+){0,4}kwh\b", r"per\s+kilowatt-hour\b"],
        "$/kw": [r"\$/kw\b", r"\$\s*\d[\d,]*(?:\.\d+)?\s*/\s*kw\b", r"per\s+(?:[a-z-]+\s+){0,4}kw\b", r"per\s+kilowatt\b", r"dollars?\s+per\s+(?:[a-z-]+\s+){0,4}kw\b", r"dollars?\s+per\s+kilowatt\b"],
        "$/month": [r"\$/month\b", r"\$\s*\d[\d,]*(?:\.\d+)?\s*/\s*month\b", r"per\s+month\b", r"\bmonthly\b", r"\bmonth\b"],
        "$/bill": [r"\$/bill\b", r"\$\s*\d[\d,]*(?:\.\d+)?\s*/\s*bill\b", r"\bbill\s+credit\b", r"\bone-time\b", r"\bannual\s+bill\s+credit\b", r"\breturned\s+payment\b", r"\bincentive\b", r"\bfee\b", r"\bpenalty\b", r"\bconnection\b", r"\bdisconnect\b"],
        "$/day": [r"\$/day\b", r"per\s+day\b", r"\bdaily\b", r"\bday\b"],
        "kwh": ["kwh"],
        "kw": ["kw"],
        "$": [r"\$"],
        "¢": [r"¢", r"\bcents?\b"],
        "%": [r"%"],
    }
    probes = checks.get(unit.lower()) or checks.get(normalized) or [re.escape(unit.lower())]
    return any(re.search(probe, haystack) for probe in probes)


def _infer_unit(
    *,
    charge_type: str,
    unit: str,
    quote: str,
    source_text: str,
) -> tuple[str, str]:
    q = quote.lower()
    context = _quote_context(quote, source_text, window=700).lower()
    haystack = f"{context} {q}".strip()
    if not quote or not haystack:
        return "", ""

    has_dollar_amount = bool(re.search(r"\$\s*\d", q))
    has_cent_amount = bool(re.search(r"(?:¢|cents?)", q))
    has_numeric_amount = bool(re.search(r"\d+(?:\.\d+)?", q))
    normalized_charge_type = charge_type.lower()

    if re.search(r"per\s+(?:[a-z-]+\s+){0,4}kwh\b", q):
        if has_cent_amount or unit == "¢/kWh":
            return "¢/kWh", "explicit_per_kwh_quote"
        if has_dollar_amount or unit == "$/kWh":
            return "$/kWh", "explicit_per_kwh_quote"
        if not unit and has_numeric_amount:
            return "¢/kWh", "bare_numeric_per_kwh_assumed_cents"
    if re.search(r"\$\s*\d[\d,]*(?:\.\d+)?\s*/\s*kwh\b", q):
        return "$/kWh", "explicit_dollars_per_kwh_quote"
    if re.search(r"per\s+(?:[a-z-]+\s+){0,4}kw\b", q):
        if has_dollar_amount or unit == "$/kW":
            return "$/kW", "explicit_per_kw_quote"
    if re.search(r"\$\s*\d[\d,]*(?:\.\d+)?\s*/\s*kw\b", q):
        return "$/kW", "explicit_dollars_per_kw_quote"
    if has_dollar_amount and re.search(r"per\s+month\b|\bmonthly\b", q):
        return "$/month", "explicit_monthly_quote"
    if re.search(r"\$\s*\d[\d,]*(?:\.\d+)?\s*/\s*month\b", q):
        return "$/month", "explicit_dollars_per_month_quote"
    if re.search(r"\$\s*\d[\d,]*(?:\.\d+)?\s*/\s*bill\b", q):
        return "$/bill", "explicit_dollars_per_bill_quote"
    if has_cent_amount and re.search(r"per\s+kilowatt-hour\b", q):
        return "¢/kWh", "explicit_cents_per_kilowatt_hour_quote"
    nearest_header_unit, nearest_header_reason = _nearest_unit_header(quote, source_text)
    if nearest_header_unit:
        return nearest_header_unit, nearest_header_reason
    bill_level_context = any(
        token in haystack
        for token in (
            "bill credit",
            "one-time",
            "annual bill credit",
            "returned payment",
            "incentive",
            "rebate",
            "fee",
            "penalty",
            "connection",
            "disconnect",
        )
    )
    if has_dollar_amount and bill_level_context:
        return "$/bill", "bill_level_context"
    program_incentive_context = any(
        token in haystack
        for token in (
            "hero",
            "high efficiency air source heat pump",
            "central air conditioning",
            "heat pump",
        )
    )
    if has_dollar_amount and program_incentive_context:
        return "$/bill", "program_incentive_context"
    if has_dollar_amount and any(
        token in haystack
        for token in (
            "load control device",
            "thermostat",
            "evse",
            "gateway",
            "heat strip",
        )
    ):
        return "$/bill", "device_program_incentive_context"
    fixed_monthly_charge = any(
        token in normalized_charge_type
        for token in ("fixed", "basic", "facilities", "monthly", "minimum")
    )
    if has_dollar_amount and normalized_charge_type in (
        "basic facilities charge",
        "fixed monthly charge",
        "minimum bill",
    ):
        return "$/month", "fixed_charge_type_monthly_context"
    if has_dollar_amount and fixed_monthly_charge and (
        "monthly rate" in haystack
        or "basic customer charge" in haystack
        or "basic facilities charge" in haystack
        or "minimum bill" in haystack
        or "fixed monthly charge" in haystack
        or "customer charge" in haystack
    ):
        return "$/month", "fixed_charge_monthly_context"

    lighting_context = (
        "per month per luminaire" in haystack
        or ("monthly rate" in haystack and "monthly charge" in haystack)
        or ("monthly charge" in haystack and "per customer" in haystack)
        or ("lamp rating" in haystack and "per month" in haystack)
    )
    if has_numeric_amount and (
        "lighting" in normalized_charge_type or lighting_context
    ):
        if lighting_context:
            return "$/month", "lighting_table_per_month_per_luminaire"

    if re.search(r"per\s+(?:[a-z-]+\s+){0,4}kwh\b", haystack):
        if has_cent_amount or unit == "¢/kWh":
            return "¢/kWh", "explicit_per_kwh_context"
        if has_dollar_amount or unit == "$/kWh":
            return "$/kWh", "explicit_per_kwh_context"

    if re.search(r"per\s+(?:[a-z-]+\s+){0,4}kw\b", haystack):
        if has_dollar_amount or unit == "$/kW":
            return "$/kW", "explicit_per_kw_context"

    if has_dollar_amount and re.search(r"per\s+month\b|\bmonthly\b", haystack):
        return "$/month", "explicit_monthly_context"

    return "", ""


def _nearest_unit_header(quote: str, source_text: str) -> tuple[str, str]:
    if not quote or not source_text:
        return "", ""
    lines = source_text.splitlines()
    quote_norm = quote.strip().lower()
    row_index = -1
    for i, line in enumerate(lines):
        if quote_norm in line.lower():
            row_index = i
            break
        if i + 1 < len(lines):
            combined = _normalize_text(f"{line} {lines[i + 1]}")
            if quote_norm in combined:
                row_index = i + 1
                break
    if row_index < 0:
        return "", ""

    for i in range(row_index, max(-1, row_index - 80), -1):
        lower = lines[i].strip().lower()
        compact = lower.replace(" ", "")
        if "dollars per kilowatt-hour" in lower:
            return "$/kWh", "nearest_header_dollars_per_kwh"
        if "dollars per kilowatt" in lower and "kilowatt-hour" not in lower:
            return "$/kW", "nearest_header_dollars_per_kw"
        if "¢/kwh" in compact:
            return "¢/kWh", "nearest_header_cents_per_kwh"
        if "cents per kilowatt-hour" in lower or "cents per kwh" in lower:
            return "¢/kWh", "nearest_header_cents_per_kwh"
        if "monthly charge" in lower or "monthly rate" in lower:
            table_block = "\n".join(lines[max(0, i - 3): row_index + 1]).lower()
            if "lighting" in table_block or "light" in table_block or "per customer" in table_block:
                return "$/month", "nearest_header_monthly_lighting"
    return "", ""


def _quote_context(quote: str, source_text: str, *, window: int = 120) -> str:
    if not quote or not source_text:
        return ""
    quote_norm = _normalize_text(quote)
    text_norm = _normalize_text(source_text)
    idx = text_norm.find(quote_norm)
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(text_norm), idx + len(quote_norm) + window)
    return text_norm[start:end]


def _summarize(
    results: list[ExtractionValidation],
    *,
    updates: list[dict[str, Any]],
    execute: bool,
) -> dict[str, Any]:
    by_recommendation: dict[str, int] = {}
    by_row_recommendation: dict[str, int] = {}
    for result in results:
        by_recommendation[result.recommended_status] = (
            by_recommendation.get(result.recommended_status, 0) + 1
        )
        for row in result.row_results:
            by_row_recommendation[row.recommended_status] = (
                by_row_recommendation.get(row.recommended_status, 0) + 1
            )
    return {
        "evaluated": len(results),
        "execute": execute,
        "updates": len(updates),
        "recommended_status_counts": dict(sorted(by_recommendation.items())),
        "row_recommended_status_counts": dict(sorted(by_row_recommendation.items())),
        "validated_rows": sum(r.validated_row_count for r in results),
        "rejected_rows": sum(r.rejected_row_count for r in results),
        "review_candidate_rows": sum(r.needs_review_row_count for r in results),
    }


def _ensure_row_validation_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_candidate_rate_row_validations (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_id           INTEGER NOT NULL,
            row_index               INTEGER NOT NULL,
            historical_document_id  INTEGER,
            source_pdf              TEXT NOT NULL,
            charge_type             TEXT,
            value                   REAL,
            unit                    TEXT,
            inferred_unit           TEXT,
            inferred_unit_reason    TEXT,
            source_quote            TEXT,
            source_quote_grounded   INTEGER NOT NULL DEFAULT 0,
            value_grounded          INTEGER NOT NULL DEFAULT 0,
            unit_grounded           INTEGER NOT NULL DEFAULT 0,
            validation_score        REAL NOT NULL DEFAULT 0.0,
            recommended_status      TEXT NOT NULL,
            issues_json             TEXT NOT NULL DEFAULT '[]',
            validated_at            TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(extraction_id, row_index)
        );
        CREATE INDEX IF NOT EXISTS idx_llm_row_val_status
        ON llm_candidate_rate_row_validations(recommended_status, validated_at);
        CREATE INDEX IF NOT EXISTS idx_llm_row_val_hd
        ON llm_candidate_rate_row_validations(historical_document_id);
        """
    )
    _ensure_column(conn, "llm_candidate_rate_row_validations", "inferred_unit", "TEXT")
    _ensure_column(
        conn,
        "llm_candidate_rate_row_validations",
        "inferred_unit_reason",
        "TEXT",
    )


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


def _persist_row_validations(
    conn: sqlite3.Connection,
    result: ExtractionValidation,
) -> int:
    count = 0
    for row in result.row_results:
        conn.execute(
            """
            INSERT INTO llm_candidate_rate_row_validations (
                extraction_id, row_index, historical_document_id, source_pdf,
                charge_type, value, unit, inferred_unit, inferred_unit_reason,
                source_quote,
                source_quote_grounded, value_grounded, unit_grounded,
                validation_score, recommended_status, issues_json, validated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(extraction_id, row_index) DO UPDATE SET
                historical_document_id = excluded.historical_document_id,
                source_pdf = excluded.source_pdf,
                charge_type = excluded.charge_type,
                value = excluded.value,
                unit = excluded.unit,
                inferred_unit = excluded.inferred_unit,
                inferred_unit_reason = excluded.inferred_unit_reason,
                source_quote = excluded.source_quote,
                source_quote_grounded = excluded.source_quote_grounded,
                value_grounded = excluded.value_grounded,
                unit_grounded = excluded.unit_grounded,
                validation_score = excluded.validation_score,
                recommended_status = excluded.recommended_status,
                issues_json = excluded.issues_json,
                validated_at = excluded.validated_at
            """,
            (
                result.extraction_id,
                row.row_index,
                result.historical_document_id,
                result.source_pdf,
                row.charge_type,
                row.value,
                row.unit,
                row.inferred_unit,
                row.inferred_unit_reason,
                row.source_quote,
                int(row.source_quote_grounded),
                int(row.value_grounded),
                int(row.unit_grounded),
                row.score,
                row.recommended_status,
                json.dumps(row.issues),
            ),
        )
        count += 1
    return count


def _loads_list(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _to_float(value: Any) -> float:
    parsed = _to_optional_float(value)
    return parsed if parsed is not None else 0.0


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _read_text_file(path_value: str, max_chars: int = 500_000) -> str:
    path = Path(path_value)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except OSError:
        return ""
