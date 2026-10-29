"""Phase 2A write API for immutable extraction runs, promotion and audit.

Every extraction attempt is stored as an ``extraction_runs`` row with its
output (``extracted_charges`` or ``extracted_documents``). Nothing a run
records is ever overwritten. The legacy tables stay the read model that
billing and analytics use; they change only when a run is **explicitly
promoted**, and every promotion can be rolled back exactly.

Typical flow::

    run_id = start_run(conn, target_kind="tariff_version", target_id=vid, ...)
    record_charges(conn, run_id, [CandidateCharge(...), ...])
    seal_run(conn, run_id)                 # validates; run is now immutable
    diff = compare_run(conn, run_id)       # vs. what billing reads today
    promote(conn, run_id, promoted_by="alice", reason="reviewed diff #12")
    rollback(conn, "tariff_version", vid, by="alice", reason="regressed bill 3")

Materialisation rules (``tariff_version`` targets):

* Rows the engine's curators own are never deleted by a promotion: rows with
  non-empty ``notes`` (manual corrections, sibling copies, overrides) and rows
  linked from ``llm_promoted_charge_audit``. A reviewer may name specific
  protected rows in ``supersede_protected_ids`` to replace them.
* A candidate row whose natural key matches a protected row that stays is not
  inserted, so a correction and a re-extraction never double-bill.
* Active ``charge_overrides`` are applied to the candidate rows.

The schema must be installed first (``ensure_provenance_schema``).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from duke_rates.db.provenance_schema import (
    AUDITED_METADATA_COLUMNS,
    TARIFF_CHARGE_COLUMNS,
    provenance_schema_installed,
)
from duke_rates.parse.canonical import parse_decimal, parse_unit

LEGACY_PARSER = "legacy"

PAYLOAD_TARGETS: dict[str, tuple[str, str, str]] = {
    # target_kind: (table, column, payload_kind)
    "historical_document": ("historical_documents", "parsed_result_json", "parsed_result_json"),
    "bill_statement": ("bill_statements", "statement_json", "statement_json"),
}
#: Rider-summary sources (Phase 2C): blocks + line items, see rider_summary_provenance.
RIDER_TARGET = "rider_summary_source"
TARGET_KINDS = ("tariff_version", *PAYLOAD_TARGETS, RIDER_TARGET)

_UNSET: Any = object()
_FLOAT_TOLERANCE = 1e-9


class ProvenanceError(RuntimeError):
    """A provenance operation was refused; nothing was written."""


class PromotionConflictError(ProvenanceError):
    """The live data no longer matches what the operation expects."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str | None) -> str:
    return hashlib.sha256(("\x00NULL" if text is None else text).encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    digest = hashlib.sha256()
    with p.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_producer_version(repo_root: str | Path | None = None) -> str:
    """``<git sha>`` plus ``-dirty`` when the tree has uncommitted changes."""
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[3]
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha}-dirty" if dirty else sha


def code_fingerprint(paths: Iterable[str | Path]) -> str:
    """Hash of the producer's source files, so uncommitted edits are visible."""
    digest = hashlib.sha256()
    for path in sorted(str(p) for p in paths):
        digest.update(path.replace("\\", "/").rsplit("/src/", 1)[-1].encode("utf-8"))
        file_hash = sha256_file(path) or "missing"
        digest.update(file_hash.encode("ascii"))
    return digest.hexdigest()


def config_fingerprint(config: dict[str, Any] | None) -> str:
    return sha256_text(canonical_json(config or {}))


def _require_schema(conn: sqlite3.Connection) -> None:
    # Every query below reads columns by name.
    conn.row_factory = sqlite3.Row
    if not provenance_schema_installed(conn):
        raise ProvenanceError(
            "provenance schema is not installed; run `duke-rates provenance migrate` "
            "on this database first"
        )


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """One transaction (or a savepoint inside the caller's transaction)."""
    if conn.in_transaction:
        name = f"sp_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


@contextmanager
def write_context(
    conn: sqlite3.Connection,
    actor: str,
    *,
    operation_id: str | None = None,
    evidence: str | None = None,
    revision_operation: str | None = None,
) -> Iterator[str]:
    """Attribute trigger-written receipts in this transaction to ``actor``.

    Must be used inside :func:`atomic` so the context row never outlives the
    transaction that set it.
    """
    if not conn.in_transaction:
        raise ProvenanceError("write_context() must run inside a transaction")
    op = operation_id or uuid.uuid4().hex
    conn.execute(
        "INSERT OR REPLACE INTO provenance_write_context "
        "(id, actor, operation_id, evidence, revision_operation, set_at) VALUES (1, ?, ?, ?, ?, ?)",
        (actor, op, evidence, revision_operation, _now()),
    )
    try:
        yield op
    finally:
        conn.execute("DELETE FROM provenance_write_context WHERE id = 1")


# ---------------------------------------------------------------------------
# Units and natural keys
# ---------------------------------------------------------------------------

def canonicalize_unit(
    value: float | None, unit: str | None
) -> tuple[float | None, str | None, str]:
    """Return ``(canonical_value, canonical_unit, method)``.

    Delegates to ``duke_rates.parse.canonical``: cents become dollars exactly,
    and units that are not recognised are left without a canonical form
    (``method='unrecognized_unit'``) rather than guessed.
    """
    if value is None:
        return None, None, "no_value"
    parsed = parse_unit(unit)
    if parsed.warning == "unit_missing":
        return None, None, "unit_unavailable"
    if not parsed.recognized:
        return None, None, "unrecognized_unit"
    canonical = parse_decimal(value) * parsed.factor
    method = "identity" if parsed.factor == 1 else "cents_to_dollars"
    return float(canonical), parsed.canonical.value, method


_LABEL_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _norm_label(label: str | None) -> str:
    return _LABEL_NORMALIZE_RE.sub(" ", (label or "").lower()).strip()


def _norm_season(season: str | None) -> str:
    return season or "all_year"


def natural_key(row: dict[str, Any], *, with_class: bool = True) -> str:
    # The canonical unit separates a section's kWh and kW TOTAL rows, which
    # share a label (Phase 2B).
    unit = parse_unit(row.get("rate_unit")).canonical
    parts = [
        unit.value if unit else (row.get("rate_unit") or ""),
        row.get("customer_class") or "" if with_class else "*",
        row.get("charge_type") or "",
        row.get("tou_period") or "",
        _norm_season(row.get("season")),
        row.get("tier_min"),
        row.get("tier_max"),
        _norm_label(row.get("charge_label")),
    ]
    return canonical_json(parts)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@dataclass
class CandidateCharge:
    """One extracted charge with whatever provenance the producer really has.

    Leave a provenance field ``None`` when it is not known: it is then stored
    as ``unavailable``. Never pass a placeholder page, bbox or quote.
    ``source_quote`` must be text printed in the source that contains the
    value; a label-only snippet belongs in ``extractor_snippet``.
    """

    charge_type: str
    charge_label: str | None
    rate_value: float | None
    rate_unit: str | None
    tier_min: float | None = None
    tier_max: float | None = None
    tou_period: str | None = None
    season: str | None = None
    customer_class: str | None = None
    printed_value_text: str | None = None
    printed_unit: str | None = None
    applicability: dict[str, Any] | None = None
    effective_start: str | None = None
    effective_end: str | None = None
    source_pdf_path: str | None = None
    source_pdf_sha256: str | None = None
    page_number: int | None = None
    bbox: dict[str, Any] | None = None
    source_quote: str | None = None
    extractor_snippet: str | None = None
    # Structured source location: table id, row, column, backend and version.
    table_ref: dict[str, Any] | None = None
    extraction_method: str | None = None
    confidence_score: float = 0.0
    # Legacy backfill only.
    legacy_tariff_charge_id: int | None = None
    legacy_row: dict[str, Any] | None = None
    legacy_fields: frozenset[str] = field(default_factory=frozenset)

    def as_row(self) -> dict[str, Any]:
        return {
            "charge_type": self.charge_type,
            "charge_label": self.charge_label,
            "rate_value": self.rate_value,
            "rate_unit": self.rate_unit,
            "tier_min": self.tier_min,
            "tier_max": self.tier_max,
            "tou_period": self.tou_period,
            "season": self.season,
            "customer_class": self.customer_class,
        }


def start_run(
    conn: sqlite3.Connection,
    *,
    target_kind: str,
    target_id: int,
    parser_name: str,
    parser_version: str,
    extraction_method: str,
    created_by: str,
    family_key: str | None = None,
    version_id: int | None = None,
    source_document_kind: str | None = None,
    source_document_id: int | None = None,
    source_path: str | None = None,
    document_sha256: str | None = None,
    document_sha256_status: str | None = None,
    code_fingerprint: str | None = None,
    model_id: str | None = None,
    model_digest: str | None = None,
    config: dict[str, Any] | None = None,
    text_source: str | None = None,
    interpreted_effective_start: str | None = None,
    interpreted_effective_end: str | None = None,
    notes: str | None = None,
    started_at: str | None = None,
) -> int:
    """Open a run. Hashes ``source_path`` unless a hash is supplied."""
    _require_schema(conn)
    if target_kind not in TARGET_KINDS:
        raise ProvenanceError(f"unknown target_kind {target_kind!r}")
    if document_sha256 is None and source_path:
        document_sha256 = sha256_file(source_path)
        status = "computed" if document_sha256 else "unavailable"
    else:
        status = document_sha256_status or ("computed" if document_sha256 else "unavailable")
    with atomic(conn):
        cursor = conn.execute(
            """
            INSERT INTO extraction_runs (
                run_uuid, target_kind, target_id, family_key, version_id,
                source_document_kind, source_document_id, source_path,
                document_sha256, document_sha256_status, parser_name, parser_version,
                code_fingerprint, model_id, model_digest, config_fingerprint, config_json,
                text_source, extraction_method, interpreted_effective_start,
                interpreted_effective_end, started_at, created_by, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                target_kind,
                target_id,
                family_key,
                version_id,
                source_document_kind,
                source_document_id,
                source_path,
                document_sha256,
                status,
                parser_name,
                parser_version,
                code_fingerprint,
                model_id,
                model_digest,
                config_fingerprint(config),
                canonical_json(config or {}),
                text_source,
                extraction_method,
                interpreted_effective_start,
                interpreted_effective_end,
                started_at or _now(),
                created_by,
                notes,
            ),
        )
        if document_sha256 and source_path and status == "computed":
            size = Path(source_path).stat().st_size
            conn.execute(
                "INSERT OR IGNORE INTO document_blobs(sha256, size_bytes, first_seen_path, first_seen_at) "
                "VALUES (?, ?, ?, ?)",
                (document_sha256, size, source_path, _now()),
            )
        return int(cursor.lastrowid)


def _run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM extraction_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ProvenanceError(f"extraction run {run_id} does not exist")
    return row


def _status(value: Any, legacy: bool) -> str:
    if value is None:
        return "unavailable"
    return "legacy_unverified" if legacy else "available"


def _canonical_from_printed(charge: "CandidateCharge") -> tuple[float, str] | None:
    """(value, unit) computed from the printed value and printed unit, if both parse."""
    if not charge.printed_value_text or not charge.printed_unit:
        return None
    number = parse_decimal(charge.printed_value_text.split()[0])
    header = charge.printed_unit.lower()
    for word in ("baseline", "incremental"):
        header = header.replace(word, "")
    parsed = parse_unit(" ".join(header.split()).replace(" /", "/"))
    if number is None or not parsed.recognized:
        return None
    return float(number * parsed.factor), parsed.canonical.value


def record_charges(
    conn: sqlite3.Connection, run_id: int, charges: Sequence[CandidateCharge]
) -> list[int]:
    run = _run(conn, run_id)
    if run["target_kind"] != "tariff_version":
        raise ProvenanceError("charges can only be recorded on tariff_version runs")
    start = conn.execute(
        "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM extracted_charges WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    ids: list[int] = []
    with atomic(conn):
        for offset, charge in enumerate(charges):
            canonical_value, canonical_unit, method = canonicalize_unit(
                charge.rate_value, charge.rate_unit
            )
            exact = _canonical_from_printed(charge)
            if exact is not None and canonical_unit == exact[1]:
                if canonical_value is not None and abs(canonical_value - exact[0]) > 1e-9:
                    method = f"{method}:printed_disagrees"
                else:
                    # Exact Decimal from the printed text, not the float.
                    canonical_value, method = exact[0], f"{method}:from_printed"
            legacy = charge.legacy_fields
            cursor = conn.execute(
                """
                INSERT INTO extracted_charges (
                    run_id, ordinal, version_id, family_key, charge_type, charge_label,
                    rate_value, rate_unit, printed_value_text, printed_unit,
                    canonical_value, canonical_unit, unit_normalization,
                    tier_min, tier_max, tou_period, season, customer_class,
                    applicability_json, effective_start, effective_end,
                    source_pdf_path, source_pdf_sha256, source_pdf_status,
                    page_number, page_status, bbox_json, bbox_status,
                    source_quote, quote_status, extractor_snippet, extraction_method,
                    confidence_score, natural_key, legacy_tariff_charge_id, legacy_row_json,
                    table_ref_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    start + offset,
                    run["version_id"] or run["target_id"],
                    run["family_key"] or "",
                    charge.charge_type,
                    charge.charge_label,
                    charge.rate_value,
                    charge.rate_unit,
                    charge.printed_value_text,
                    charge.printed_unit,
                    canonical_value,
                    canonical_unit,
                    method,
                    charge.tier_min,
                    charge.tier_max,
                    charge.tou_period,
                    charge.season,
                    charge.customer_class,
                    canonical_json(charge.applicability)
                    if charge.applicability is not None
                    else None,
                    charge.effective_start,
                    charge.effective_end,
                    charge.source_pdf_path,
                    charge.source_pdf_sha256,
                    _status(charge.source_pdf_path, "source_pdf" in legacy),
                    charge.page_number,
                    _status(charge.page_number, "page" in legacy),
                    canonical_json(charge.bbox) if charge.bbox is not None else None,
                    _status(charge.bbox, "bbox" in legacy),
                    charge.source_quote,
                    _status(charge.source_quote, "quote" in legacy),
                    charge.extractor_snippet,
                    charge.extraction_method or run["extraction_method"],
                    charge.confidence_score,
                    natural_key(charge.as_row()),
                    charge.legacy_tariff_charge_id,
                    canonical_json(charge.legacy_row) if charge.legacy_row is not None else None,
                    canonical_json(charge.table_ref) if charge.table_ref is not None else None,
                ),
            )
            ids.append(int(cursor.lastrowid))
    return ids


def record_document(conn: sqlite3.Connection, run_id: int, payload_json: str) -> None:
    run = _run(conn, run_id)
    spec = PAYLOAD_TARGETS.get(run["target_kind"])
    if spec is None:
        raise ProvenanceError("documents can only be recorded on payload runs")
    with atomic(conn):
        conn.execute(
            "INSERT INTO extracted_documents(run_id, payload_kind, payload_json, payload_sha256) "
            "VALUES (?, ?, ?, ?)",
            (run_id, spec[2], payload_json, sha256_text(payload_json)),
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_KNOWN_CANONICAL_UNITS = re.compile(r"^(\$|%)")
_MAX_DOLLARS_PER_KWH = 1.0


def _validate_charge(row: sqlite3.Row) -> list[dict[str, str]]:
    from duke_rates.billing.charge_types import classify_charge

    issues: list[dict[str, str]] = []
    if row["rate_value"] is None:
        issues.append({"level": "error", "code": "missing_value", "detail": "rate_value is NULL"})
    if row["unit_normalization"] in ("unit_unavailable", "unrecognized_unit"):
        issues.append(
            {
                "level": "warning",
                "code": row["unit_normalization"],
                "detail": f"unit {row['rate_unit']!r} has no canonical form",
            }
        )
    if (
        row["canonical_unit"] == "$/kWh"
        and row["canonical_value"] is not None
        and abs(row["canonical_value"]) > _MAX_DOLLARS_PER_KWH
    ):
        issues.append(
            {
                "level": "error",
                "code": "implausible_magnitude",
                "detail": f"{row['canonical_value']} $/kWh exceeds {_MAX_DOLLARS_PER_KWH}",
            }
        )
    category = classify_charge(
        row["charge_type"],
        tou_period=row["tou_period"],
        rate_unit=row["rate_unit"],
        charge_label=row["charge_label"],
    ).category
    if category not in ("priced", "excluded_informational"):
        issues.append(
            {
                "level": "warning",
                "code": "unsupported_charge_type",
                "detail": f"charge_type {row['charge_type']!r} is {category}",
            }
        )
    for column in ("effective_start", "effective_end"):
        value = row[column]
        if value and not _parses_as_date(value):
            issues.append(
                {
                    "level": "error",
                    "code": "unreadable_date",
                    "detail": f"{column}={value!r} does not parse",
                }
            )
    missing = [
        name for name in ("source_pdf", "page", "quote") if row[f"{name}_status"] == "unavailable"
    ]
    if missing:
        issues.append(
            {
                "level": "info",
                "code": "provenance_incomplete",
                "detail": "unavailable: " + ", ".join(missing),
            }
        )
    return issues


def _parses_as_date(value: str) -> bool:
    from duke_rates.parse.normalization import parse_effective_date

    try:
        return parse_effective_date(value) is not None
    except Exception:
        return False


def _rollup(levels: Iterable[str]) -> str:
    levels = set(levels)
    if "error" in levels:
        return "failed"
    if "warning" in levels:
        return "warnings"
    return "passed"


def seal_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str | None = None,
    metrics: dict[str, Any] | None = None,
    extra_issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Validate a run and make it immutable. Returns the validation summary.

    ``extra_issues`` are run-level findings from the producer (structural
    validators); level ``review`` counts as a warning that needs a person.
    """
    run = _run(conn, run_id)
    if run["sealed_at"]:
        raise ProvenanceError(f"run {run_id} is already sealed")
    run_issues: list[dict[str, str]] = []
    per_charge: dict[int, list[dict[str, str]]] = {}
    if run["target_kind"] == "tariff_version":
        rows = conn.execute(
            "SELECT * FROM extracted_charges WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall()
        for row in rows:
            per_charge[row["id"]] = _validate_charge(row)
        keys = defaultdict(int)
        for row in rows:
            keys[row["natural_key"]] += 1
        dupes = [k for k, n in keys.items() if n > 1]
        if dupes:
            run_issues.append(
                {
                    "level": "warning",
                    "code": "duplicate_natural_key",
                    "detail": f"{len(dupes)} natural key(s) occur more than once",
                }
            )
        count = len(rows)
    elif run["target_kind"] == RIDER_TARGET:
        from duke_rates.db import rider_summary_provenance as rsp

        count, rider_issues = rsp.validate_run(conn, run_id)
        run_issues.extend(rider_issues)
    else:
        doc = conn.execute(
            "SELECT payload_json FROM extracted_documents WHERE run_id = ?", (run_id,)
        ).fetchone()
        count = 1 if doc else 0
        if doc:
            try:
                json.loads(doc["payload_json"])
            except ValueError:
                run_issues.append(
                    {"level": "error", "code": "invalid_json", "detail": "payload is not JSON"}
                )
    for value in (run["interpreted_effective_start"], run["interpreted_effective_end"]):
        if value and not _parses_as_date(value):
            run_issues.append(
                {
                    "level": "error",
                    "code": "unreadable_date",
                    "detail": f"interpreted effective date {value!r} does not parse",
                }
            )
    for issue in extra_issues or []:
        level = issue.get("level", "warning")
        # "review" rolls up as a warning, but the flag keeps it visible.
        run_issues.append({**issue, "level": "warning" if level == "review" else level,
                           "needs_review": level == "review"})
    final_status = status or ("ok" if count else "empty")
    if count == 0:
        run_issues.append(
            {"level": "warning", "code": "empty_run", "detail": "run produced no output"}
        )
    charge_levels = [i["level"] for issues in per_charge.values() for i in issues]
    validation_status = _rollup([i["level"] for i in run_issues] + charge_levels)
    summary = {
        "run_issues": run_issues,
        "charges_with_errors": sum(
            1 for issues in per_charge.values() if any(i["level"] == "error" for i in issues)
        ),
        "charges_with_warnings": sum(
            1 for issues in per_charge.values() if any(i["level"] == "warning" for i in issues)
        ),
        "output_count": count,
    }
    # extracted_charges is immutable, so per-charge results live in the run's
    # validation_json keyed by charge id.
    detail = {"summary": summary, "charges": {str(k): v for k, v in per_charge.items() if v}}
    now = _now()
    with atomic(conn):
        conn.execute(
            """
            UPDATE extraction_runs
            SET status = ?, finished_at = ?, sealed_at = ?, validation_status = ?,
                validation_json = ?, metrics_json = ?
            WHERE id = ?
            """,
            (
                final_status,
                run["finished_at"] or now,
                now,
                validation_status,
                canonical_json(detail),
                canonical_json(metrics or {}),
                run_id,
            ),
        )
    return {"status": final_status, "validation_status": validation_status, **summary}


# ---------------------------------------------------------------------------
# Live state (what billing reads today)
# ---------------------------------------------------------------------------


def _tariff_charge_rows(conn: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
    cols = ", ".join(TARIFF_CHARGE_COLUMNS)
    rows = conn.execute(
        f"SELECT {cols} FROM tariff_charges WHERE version_id = ? ORDER BY id", (version_id,)
    ).fetchall()
    return [dict(zip(TARIFF_CHARGE_COLUMNS, tuple(r), strict=True)) for r in rows]


def _state_sha(rows: list[dict[str, Any]]) -> str:
    return sha256_text(canonical_json(rows))


def _protected_ids(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> set[int]:
    protected = {r["id"] for r in rows if (r.get("notes") or "").strip()}
    has_audit = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_promoted_charge_audit'"
    ).fetchone()
    if has_audit and rows:
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        protected |= {
            int(r[0])
            for r in conn.execute(
                f"SELECT tariff_charge_id FROM llm_promoted_charge_audit WHERE tariff_charge_id IN ({marks})",
                ids,
            )
        }
    return protected


def _payload_value(conn: sqlite3.Connection, target_kind: str, target_id: int) -> str | None:
    table, column, _ = PAYLOAD_TARGETS[target_kind]
    row = conn.execute(f"SELECT {column} FROM {table} WHERE id = ?", (target_id,)).fetchone()
    if row is None:
        raise ProvenanceError(f"{table} row {target_id} does not exist")
    return row[0]


def active_run(conn: sqlite3.Connection, target_kind: str, target_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM active_results WHERE target_kind = ? AND target_id = ?",
        (target_kind, target_id),
    ).fetchone()


def review_findings(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    """Run-level findings a person must decide (validators' ``review`` level)."""
    run = _run(conn, run_id)
    try:
        detail = json.loads(run["validation_json"] or "{}")
    except ValueError:
        return []
    issues = detail.get("summary", {}).get("run_issues", []) if isinstance(detail, dict) else []
    return [i for i in issues if i.get("needs_review")]


def run_review_status(conn: sqlite3.Connection, run_id: int) -> str:
    """``active`` | ``previously_active`` | ``rejected`` | ``candidate`` | ``open``."""
    run = _run(conn, run_id)
    if not run["sealed_at"]:
        return "open"
    current = active_run(conn, run["target_kind"], run["target_id"])
    if current and current["run_id"] == run_id:
        return "active"
    if conn.execute(
        "SELECT 1 FROM promotion_log WHERE to_run_id = ? AND action = 'reject'", (run_id,)
    ).fetchone():
        return "rejected"
    if conn.execute(
        "SELECT 1 FROM promotion_log WHERE to_run_id = ? AND action IN ('promote','activate_legacy')",
        (run_id,),
    ).fetchone():
        return "previously_active"
    return "candidate"


def _live_charge_view(conn: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
    """Live tariff_charges rows with the provenance of the run that produced them."""
    rows = _tariff_charge_rows(conn, version_id)
    protected = _protected_ids(conn, rows)
    current = active_run(conn, "tariff_version", version_id)
    by_tc: dict[int, sqlite3.Row] = {}
    if rows:
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        for ec in conn.execute(
            f"""
            SELECT mc.tariff_charge_id AS tc_id, ec.* FROM materialized_charges mc
            JOIN extracted_charges ec ON ec.id = mc.extracted_charge_id
            WHERE mc.tariff_charge_id IN ({marks})
            """,
            ids,
        ):
            by_tc[ec["tc_id"]] = ec
        if current is not None:
            for ec in conn.execute(
                f"""
                SELECT legacy_tariff_charge_id AS tc_id, * FROM extracted_charges
                WHERE run_id = ? AND legacy_tariff_charge_id IN ({marks})
                """,
                [current["run_id"], *ids],
            ):
                by_tc.setdefault(ec["tc_id"], ec)
    view = []
    for r in rows:
        ec = by_tc.get(r["id"])
        canonical_value, canonical_unit, _ = canonicalize_unit(r["rate_value"], r["rate_unit"])
        view.append(
            {
                **r,
                "canonical_value": canonical_value,
                "canonical_unit": canonical_unit,
                "protected": r["id"] in protected,
                "applicability_json": ec["applicability_json"] if ec is not None else None,
                "effective_start": ec["effective_start"] if ec is not None else None,
                "provenance": _provenance_of(ec),
                "tariff_charge_id": r["id"],
            }
        )
    return view


def _provenance_of(ec: sqlite3.Row | None) -> dict[str, Any]:
    if ec is None:
        return {"tracked": False}
    return {
        "tracked": True,
        "run_id": ec["run_id"],
        "source_pdf_status": ec["source_pdf_status"],
        "source_pdf_sha256": ec["source_pdf_sha256"],
        "page_status": ec["page_status"],
        "page_number": ec["page_number"],
        "bbox_status": ec["bbox_status"],
        "quote_status": ec["quote_status"],
        "extraction_method": ec["extraction_method"],
    }


def _candidate_view(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM extracted_charges WHERE run_id = ? ORDER BY ordinal", (run_id,)
    ).fetchall()
    return [
        {
            **{k: r[k] for k in r.keys()},
            "extracted_charge_id": r["id"],
            "provenance": _provenance_of(r),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

_SUMMARY_FIELDS = (
    "charge_type",
    "charge_label",
    "rate_value",
    "rate_unit",
    "tier_min",
    "tier_max",
    "tou_period",
    "season",
    "customer_class",
)


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in _SUMMARY_FIELDS}
    for k in ("tariff_charge_id", "extracted_charge_id", "protected"):
        if k in row:
            out[k] = row[k]
    return out


def _floats_differ(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is not b
    return abs(float(a) - float(b)) > _FLOAT_TOLERANCE


def _pair_changes(
    old: dict[str, Any], new: dict[str, Any], version_effective_start: str | None
) -> dict[str, list[Any]]:
    changes: dict[str, list[Any]] = {}
    old_val = (
        old.get("canonical_value")
        if old.get("canonical_value") is not None
        else old.get("rate_value")
    )
    new_val = (
        new.get("canonical_value")
        if new.get("canonical_value") is not None
        else new.get("rate_value")
    )
    if _floats_differ(old_val, new_val):
        changes["value"] = [old.get("rate_value"), new.get("rate_value")]
    if (old.get("rate_unit") or "") != (new.get("rate_unit") or "") or (
        old.get("canonical_unit") != new.get("canonical_unit")
    ):
        changes["unit"] = [old.get("rate_unit"), new.get("rate_unit")]
    old_eff = old.get("effective_start") or version_effective_start
    new_eff = new.get("effective_start") or version_effective_start
    if old_eff != new_eff:
        changes["effective_start"] = [old_eff, new_eff]
    if (old.get("applicability_json") or None) != (new.get("applicability_json") or None):
        changes["applicability"] = [old.get("applicability_json"), new.get("applicability_json")]
    if (old.get("customer_class") or None) != (new.get("customer_class") or None):
        changes["rate_class"] = [old.get("customer_class"), new.get("customer_class")]
    old_p, new_p = old.get("provenance") or {}, new.get("provenance") or {}
    prov_fields = (
        "source_pdf_status",
        "source_pdf_sha256",
        "page_status",
        "page_number",
        "bbox_status",
        "quote_status",
        "extraction_method",
    )
    prov_diff = {
        f: [old_p.get(f), new_p.get(f)] for f in prov_fields if old_p.get(f) != new_p.get(f)
    }
    if not old_p.get("tracked"):
        prov_diff["tracked"] = [False, True]
    if prov_diff:
        changes["provenance"] = prov_diff
    return changes


def _match(
    old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    pairs: list[tuple[dict, dict]] = []
    remaining_old = list(old_rows)
    remaining_new = list(new_rows)
    for with_class in (True, False):
        index: dict[str, list[dict]] = defaultdict(list)
        for row in remaining_old:
            index[natural_key(row, with_class=with_class)].append(row)
        unmatched_new = []
        for row in remaining_new:
            bucket = index.get(natural_key(row, with_class=with_class))
            if bucket:
                old = bucket.pop(0)
                pairs.append((old, row))
            else:
                unmatched_new.append(row)
        matched_old = {id(o) for o, _ in pairs}
        remaining_old = [r for r in remaining_old if id(r) not in matched_old]
        remaining_new = unmatched_new
    return pairs, remaining_old, remaining_new


def _payload_paths(
    old: Any, new: Any, prefix: str = "$", out: list[str] | None = None, limit: int = 200
) -> list[str]:
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            _payload_paths(
                old.get(key, _UNSET), new.get(key, _UNSET), f"{prefix}.{key}", out, limit
            )
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (a, b) in enumerate(zip(old, new, strict=True)):
            _payload_paths(a, b, f"{prefix}[{i}]", out, limit)
    elif old != new:
        out.append(prefix)
    return out


def compare_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Diff a run against what billing reads today for the same target."""
    _require_schema(conn)
    run = _run(conn, run_id)
    target_kind, target_id = run["target_kind"], run["target_id"]
    current = active_run(conn, target_kind, target_id)
    base = {
        "target_kind": target_kind,
        "target_id": target_id,
        "candidate_run_id": run_id,
        "active_run_id": current["run_id"] if current else None,
        "candidate_validation_status": run["validation_status"],
    }
    if target_kind == RIDER_TARGET:
        from duke_rates.db import rider_summary_provenance as rsp

        return {**base, **rsp.compare_run(conn, run_id)}
    if target_kind in PAYLOAD_TARGETS:
        live = _payload_value(conn, target_kind, target_id)
        doc = conn.execute(
            "SELECT payload_json FROM extracted_documents WHERE run_id = ?", (run_id,)
        ).fetchone()
        new = doc["payload_json"] if doc else None
        try:
            paths = _payload_paths(
                json.loads(live) if live else None, json.loads(new) if new else None
            )
        except ValueError:
            paths = ["$"]
        return {
            **base,
            "live_sha256": sha256_text(live),
            "candidate_sha256": sha256_text(new),
            "identical": live == new,
            "changed_paths": paths,
        }

    version = conn.execute(
        "SELECT effective_start, effective_end FROM tariff_versions WHERE id = ?", (target_id,)
    ).fetchone()
    version_start = version["effective_start"] if version else None
    old_rows = _live_charge_view(conn, target_id)
    new_rows = _candidate_view(conn, run_id)
    pairs, removed, added = _match(old_rows, new_rows)

    value_changes, unit_changes, date_changes = [], [], []
    class_changes, applicability_changes, provenance_diffs = [], [], []
    unchanged = 0
    for old, new in pairs:
        changes = _pair_changes(old, new, version_start)
        entry = {"active": _brief(old), "candidate": _brief(new)}
        material = False
        if "value" in changes:
            value_changes.append({**entry, "change": changes["value"]})
            material = True
        if "unit" in changes:
            unit_changes.append({**entry, "change": changes["unit"]})
            material = True
        if "effective_start" in changes:
            date_changes.append({**entry, "change": changes["effective_start"]})
            material = True
        if "rate_class" in changes:
            class_changes.append({**entry, "change": changes["rate_class"]})
            material = True
        if "applicability" in changes:
            applicability_changes.append({**entry, "change": changes["applicability"]})
            material = True
        if "provenance" in changes:
            provenance_diffs.append({**entry, "change": changes["provenance"]})
        if not material:
            unchanged += 1

    target_dates = {}
    for col, run_col in (
        ("effective_start", "interpreted_effective_start"),
        ("effective_end", "interpreted_effective_end"),
    ):
        live_value = version[col] if version else None
        if run[run_col] is not None and run[run_col] != live_value:
            target_dates[col] = [live_value, run[run_col]]

    return {
        **base,
        "version_effective_start": version_start,
        "target_effective_date_changes": target_dates,
        "summary": {
            "active_rows": len(old_rows),
            "candidate_rows": len(new_rows),
            "unchanged": unchanged,
            "added": len(added),
            "removed": len(removed),
            "removed_but_protected": sum(1 for r in removed if r.get("protected")),
            "value_changes": len(value_changes),
            "unit_changes": len(unit_changes),
            "effective_date_changes": len(date_changes) + len(target_dates),
            "rate_class_changes": len(class_changes),
            "applicability_changes": len(applicability_changes),
            "provenance_differences": len(provenance_diffs),
        },
        "added": [_brief(r) for r in added],
        "removed": [_brief(r) for r in removed],
        "value_changes": value_changes,
        "unit_changes": unit_changes,
        "effective_date_changes": date_changes,
        "rate_class_changes": class_changes,
        "applicability_changes": applicability_changes,
        "provenance_differences": provenance_diffs,
    }


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def add_override(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    action: str,
    source_kind: str,
    source_ref: str,
    approved_by: str,
    payload: dict[str, Any] | None = None,
    natural_key_row: dict[str, Any] | None = None,
    evidence_pdf_sha256: str | None = None,
    evidence_page: int | None = None,
    evidence_quote: str | None = None,
) -> int:
    """Register a curated add/replace/suppress for a version (applied on promote)."""
    _require_schema(conn)
    if action in ("replace", "suppress") and natural_key_row is None:
        raise ProvenanceError(f"{action} overrides need natural_key_row")
    if action in ("add", "replace") and not payload:
        raise ProvenanceError(f"{action} overrides need a payload")
    key = (
        natural_key(natural_key_row)
        if natural_key_row is not None
        else (natural_key(payload) if payload else None)
    )
    with atomic(conn):
        cursor = conn.execute(
            """
            INSERT INTO charge_overrides (
                version_id, action, natural_key, payload_json, source_kind, source_ref,
                evidence_pdf_sha256, evidence_page, evidence_quote, approved_by, approved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                action,
                key,
                canonical_json(payload) if payload else None,
                source_kind,
                source_ref,
                evidence_pdf_sha256,
                evidence_page,
                evidence_quote,
                approved_by,
                _now(),
            ),
        )
        return int(cursor.lastrowid)


def retire_override(conn: sqlite3.Connection, override_id: int, *, by: str, reason: str) -> None:
    with atomic(conn):
        updated = conn.execute(
            "UPDATE charge_overrides SET retired_at = ?, retired_by = ?, retire_reason = ? "
            "WHERE id = ? AND retired_at IS NULL",
            (_now(), by, reason, override_id),
        ).rowcount
        if not updated:
            raise ProvenanceError(f"override {override_id} does not exist or is already retired")


def _active_overrides(conn: sqlite3.Connection, version_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM charge_overrides WHERE version_id = ? AND retired_at IS NULL ORDER BY id",
        (version_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Promotion and rollback
# ---------------------------------------------------------------------------

_INSERT_COLUMNS = TARIFF_CHARGE_COLUMNS[1:]  # everything but id


def _insert_tariff_charge(conn: sqlite3.Connection, row: dict[str, Any], *, keep_id: bool) -> int:
    cols = TARIFF_CHARGE_COLUMNS if keep_id else _INSERT_COLUMNS
    cursor = conn.execute(
        f"INSERT INTO tariff_charges ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(row.get(c) for c in cols),
    )
    return int(cursor.lastrowid)


def _log_promotion(conn: sqlite3.Connection, **values: Any) -> int:
    cols = ", ".join(values)
    cursor = conn.execute(
        f"INSERT INTO promotion_log ({cols}) VALUES ({', '.join('?' * len(values))})",
        tuple(values.values()),
    )
    return int(cursor.lastrowid)


def _set_active(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: int,
    run_id: int | None,
    log_id: int,
    by: str,
    reason: str,
) -> None:
    if run_id is None:
        conn.execute(
            "DELETE FROM active_results WHERE target_kind = ? AND target_id = ?",
            (target_kind, target_id),
        )
        return
    conn.execute(
        """
        INSERT INTO active_results (target_kind, target_id, run_id, promotion_log_id,
                                    promoted_at, promoted_by, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_kind, target_id) DO UPDATE SET
            run_id = excluded.run_id, promotion_log_id = excluded.promotion_log_id,
            promoted_at = excluded.promoted_at, promoted_by = excluded.promoted_by,
            reason = excluded.reason
        """,
        (target_kind, target_id, run_id, log_id, _now(), by, reason),
    )


def promote(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    promoted_by: str,
    reason: str,
    expected_active_run_id: Any = _UNSET,
    supersede_protected_ids: Sequence[int] = (),
    allow_empty: bool = False,
    dry_run: bool = False,
    acknowledge_review: bool = False,
    pathway: str = "automated:direct",
    validation_overrides: Sequence[str] = (),
) -> dict[str, Any]:
    """Make ``run_id`` the active result and materialise it into the read model.

    Atomic: either the legacy table, ``active_results`` and the log all
    change, or nothing does. Refuses unsealed, failed or validation-failed
    runs, empty runs (unless ``allow_empty``), runs with findings that need
    a person (unless ``acknowledge_review``) and a stale
    ``expected_active_run_id``.

    Phase 2E D3: ``tariff_version`` and rider-summary runs must also pass the
    shared source-evidence validation
    (:mod:`duke_rates.historical.ncuc.ingestion_validation`): redline markup or
    a redline-duplicate assertion on the source pages, an effective date the
    pages do not print, changed source bytes. ``pathway`` names the caller.
    Unattended pathways (the default) fail closed; only ``pathway="reviewed"``
    accepts ``validation_overrides`` (named checks), and each is recorded.
    """
    _require_schema(conn)
    if not promoted_by.strip() or not reason.strip():
        raise ProvenanceError("promotion needs promoted_by and a reason")
    run = _run(conn, run_id)
    if not run["sealed_at"]:
        raise ProvenanceError(f"run {run_id} is not sealed; seal (validate) it first")
    if run["status"] == "failed":
        raise ProvenanceError(f"run {run_id} failed and cannot be promoted")
    if run["status"] == "empty" and not allow_empty:
        raise ProvenanceError(f"run {run_id} is empty; pass allow_empty to promote it")
    if run["validation_status"] == "failed":
        raise ProvenanceError(f"run {run_id} failed validation and cannot be promoted")
    if run_review_status(conn, run_id) == "rejected":
        raise ProvenanceError(f"run {run_id} was rejected")
    review = review_findings(conn, run_id)
    if review and not acknowledge_review and not dry_run:
        raise ProvenanceError(
            f"run {run_id} has {len(review)} finding(s) that need review "
            f"({', '.join(sorted({f.get('code', '?') for f in review}))}); "
            "inspect them and pass acknowledge_review to promote anyway"
        )
    target_kind, target_id = run["target_kind"], run["target_id"]
    current = active_run(conn, target_kind, target_id)
    current_run_id = current["run_id"] if current else None
    if expected_active_run_id is not _UNSET and expected_active_run_id != current_run_id:
        raise PromotionConflictError(
            f"active run for {target_kind} {target_id} is {current_run_id}, "
            f"expected {expected_active_run_id}"
        )
    if current_run_id == run_id:
        raise ProvenanceError(f"run {run_id} is already active")
    validation = None
    if target_kind in ("tariff_version", RIDER_TARGET):
        from duke_rates.historical.ncuc.ingestion_validation import enforce

        validation = enforce(conn, run_id, pathway=pathway, requested_by=promoted_by, reason=reason,
                             overrides=validation_overrides, dry_run=dry_run)
    diff = compare_run(conn, run_id)
    if dry_run:
        return {"dry_run": True, "diff": diff} | ({"validation": validation} if validation else {})

    operation = uuid.uuid4().hex
    with (
        atomic(conn),
        write_context(
            conn, f"provenance.promote:{promoted_by}", operation_id=operation, evidence=reason
        ),
    ):
        if target_kind == RIDER_TARGET:
            from duke_rates.db import rider_summary_provenance as rsp

            result = rsp.promote_rider_run(
                conn, run, current_run_id, promoted_by, reason, diff, operation
            )
        elif target_kind in PAYLOAD_TARGETS:
            result = _promote_payload(
                conn, run, current_run_id, promoted_by, reason, diff, operation
            )
        else:
            result = _promote_charges(
                conn,
                run,
                current_run_id,
                promoted_by,
                reason,
                diff,
                operation,
                supersede_protected_ids,
            )
    if validation is not None:
        result["validation"] = {k: validation.get(k) for k in ("validation_id", "verdict", "failed", "overrides")}
    return result


def _promote_payload(conn, run, current_run_id, promoted_by, reason, diff, operation):
    table, column, _ = PAYLOAD_TARGETS[run["target_kind"]]
    before = _payload_value(conn, run["target_kind"], run["target_id"])
    doc = conn.execute(
        "SELECT payload_json FROM extracted_documents WHERE run_id = ?", (run["id"],)
    ).fetchone()
    if doc is None:
        raise ProvenanceError(f"run {run['id']} recorded no document payload")
    conn.execute(
        f"UPDATE {table} SET {column} = ? WHERE id = ?", (doc["payload_json"], run["target_id"])
    )
    log_id = _log_promotion(
        conn,
        operation_uuid=operation,
        action="promote",
        target_kind=run["target_kind"],
        target_id=run["target_id"],
        from_run_id=current_run_id,
        to_run_id=run["id"],
        at=_now(),
        by=promoted_by,
        reason=reason,
        diff_json=canonical_json(diff),
        before_state_json=canonical_json({"payload": before}),
        before_state_sha256=sha256_text(before),
        after_state_sha256=sha256_text(doc["payload_json"]),
    )
    _set_active(conn, run["target_kind"], run["target_id"], run["id"], log_id, promoted_by, reason)
    return {
        "promotion_log_id": log_id,
        "from_run_id": current_run_id,
        "to_run_id": run["id"],
        "diff": diff,
    }


def _promote_charges(
    conn, run, current_run_id, promoted_by, reason, diff, operation, supersede_protected_ids
):
    version_id = run["target_id"]
    before_rows = _tariff_charge_rows(conn, version_id)
    protected = _protected_ids(conn, before_rows)
    supersede = set(int(i) for i in supersede_protected_ids)
    not_protected = supersede - protected
    if not_protected:
        raise ProvenanceError(
            f"supersede_protected_ids {sorted(not_protected)} are not protected rows of version {version_id}"
        )
    kept = [r for r in before_rows if r["id"] in protected and r["id"] not in supersede]
    deleted = [r for r in before_rows if r not in kept]
    for r in deleted:
        conn.execute("DELETE FROM tariff_charges WHERE id = ?", (r["id"],))

    kept_keys = {natural_key(r) for r in kept}
    overrides = _active_overrides(conn, version_id)
    suppress = {o["natural_key"]: o for o in overrides if o["action"] == "suppress"}
    replace = {o["natural_key"]: o for o in overrides if o["action"] == "replace"}
    adds = [o for o in overrides if o["action"] == "add"]

    now = _now()
    inserted: list[int] = []
    skipped_for_protected: list[int] = []
    applied_overrides: list[int] = []
    final_keys = set(kept_keys)
    candidates = conn.execute(
        "SELECT * FROM extracted_charges WHERE run_id = ? ORDER BY ordinal", (run["id"],)
    ).fetchall()
    for ec in candidates:
        key = ec["natural_key"]
        if key in kept_keys:
            skipped_for_protected.append(ec["id"])
            continue
        if key in suppress:
            applied_overrides.append(suppress[key]["id"])
            continue
        override = replace.get(key)
        if override is not None:
            payload = json.loads(override["payload_json"])
            row = {
                "version_id": version_id,
                "family_key": run["family_key"] or ec["family_key"],
                **{k: payload.get(k) for k in _SUMMARY_FIELDS},
                "source_snippet": override["evidence_quote"],
                "confidence_score": payload.get("confidence_score", 1.0),
                "notes": f"override:{override['source_ref']}",
                "created_at": now,
            }
            tc_id = _insert_tariff_charge(conn, row, keep_id=False)
            conn.execute(
                "INSERT INTO materialized_charges(tariff_charge_id, extracted_charge_id, "
                "charge_override_id, promotion_log_id) VALUES (?, ?, ?, -1)",
                (tc_id, ec["id"], override["id"]),
            )
            applied_overrides.append(override["id"])
        else:
            row = {
                "version_id": version_id,
                "family_key": ec["family_key"],
                **{k: ec[k] for k in _SUMMARY_FIELDS},
                "source_snippet": ec["source_quote"]
                if ec["quote_status"] == "available"
                else ec["extractor_snippet"],
                "confidence_score": ec["confidence_score"],
                "notes": None,
                "created_at": now,
            }
            tc_id = _insert_tariff_charge(conn, row, keep_id=False)
            conn.execute(
                "INSERT INTO materialized_charges(tariff_charge_id, extracted_charge_id, "
                "promotion_log_id) VALUES (?, ?, -1)",
                (tc_id, ec["id"]),
            )
        inserted.append(tc_id)
        final_keys.add(key)
    for override in adds:
        key = override["natural_key"]
        if key in final_keys:
            continue
        payload = json.loads(override["payload_json"])
        row = {
            "version_id": version_id,
            "family_key": run["family_key"],
            **{k: payload.get(k) for k in _SUMMARY_FIELDS},
            "source_snippet": override["evidence_quote"],
            "confidence_score": payload.get("confidence_score", 1.0),
            "notes": f"override:{override['source_ref']}",
            "created_at": now,
        }
        tc_id = _insert_tariff_charge(conn, row, keep_id=False)
        conn.execute(
            "INSERT INTO materialized_charges(tariff_charge_id, charge_override_id, promotion_log_id) "
            "VALUES (?, ?, -1)",
            (tc_id, override["id"]),
        )
        inserted.append(tc_id)
        applied_overrides.append(override["id"])
        final_keys.add(key)

    after_rows = _tariff_charge_rows(conn, version_id)
    log_id = _log_promotion(
        conn,
        operation_uuid=operation,
        action="promote",
        target_kind="tariff_version",
        target_id=version_id,
        from_run_id=current_run_id,
        to_run_id=run["id"],
        at=now,
        by=promoted_by,
        reason=reason,
        diff_json=canonical_json(
            {
                **diff,
                "outcome": {
                    "skipped_candidate_ids_for_protected_rows": skipped_for_protected,
                    "applied_override_ids": applied_overrides,
                },
            }
        ),
        before_state_sha256=_state_sha(before_rows),
        after_state_sha256=_state_sha(after_rows),
        inserted_row_ids_json=canonical_json(inserted),
        deleted_rows_json=canonical_json(deleted),
        preserved_row_ids_json=canonical_json([r["id"] for r in kept]),
        superseded_row_ids_json=canonical_json(sorted(supersede)),
    )
    # materialized_charges rows were written before the log id existed.
    conn.execute(
        "UPDATE materialized_charges SET promotion_log_id = ? WHERE promotion_log_id = -1",
        (log_id,),
    )
    _set_active(conn, "tariff_version", version_id, run["id"], log_id, promoted_by, reason)
    return {
        "promotion_log_id": log_id,
        "from_run_id": current_run_id,
        "to_run_id": run["id"],
        "deleted": len(deleted),
        "inserted": len(inserted),
        "preserved_protected": [r["id"] for r in kept],
        "skipped_candidates_for_protected": skipped_for_protected,
        "applied_overrides": applied_overrides,
        "diff": diff,
    }


def _last_unreverted_promotion(conn: sqlite3.Connection, target_kind: str, target_id: int):
    entries = conn.execute(
        "SELECT * FROM promotion_log WHERE target_kind = ? AND target_id = ? "
        "AND action IN ('promote', 'rollback') ORDER BY id DESC",
        (target_kind, target_id),
    ).fetchall()
    reverted = {e["reverts_log_id"] for e in entries if e["action"] == "rollback"}
    for entry in entries:
        if entry["action"] == "promote" and entry["id"] not in reverted:
            return entry
    return None


def rollback(
    conn: sqlite3.Connection, target_kind: str, target_id: int, *, by: str, reason: str
) -> dict[str, Any]:
    """Undo the most recent un-reverted promotion of a target exactly.

    Restores the rows (with their original ids) or payload the promotion
    replaced. Refuses if the live data changed after that promotion.
    """
    _require_schema(conn)
    if not by.strip() or not reason.strip():
        raise ProvenanceError("rollback needs by and a reason")
    entry = _last_unreverted_promotion(conn, target_kind, target_id)
    if entry is None:
        raise ProvenanceError(f"no promotion to roll back for {target_kind} {target_id}")
    operation = uuid.uuid4().hex
    with (
        atomic(conn),
        write_context(conn, f"provenance.rollback:{by}", operation_id=operation, evidence=reason),
    ):
        if target_kind == RIDER_TARGET:
            from duke_rates.db import rider_summary_provenance as rsp

            restored_sha = rsp.rollback_rider(conn, entry)
        elif target_kind in PAYLOAD_TARGETS:
            table, column, _ = PAYLOAD_TARGETS[target_kind]
            live = _payload_value(conn, target_kind, target_id)
            if sha256_text(live) != entry["after_state_sha256"]:
                raise PromotionConflictError(
                    f"{table}.{column} for {target_id} changed after promotion {entry['id']}"
                )
            before = json.loads(entry["before_state_json"])["payload"]
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (before, target_id))
            restored_sha = sha256_text(before)
        else:
            live_rows = _tariff_charge_rows(conn, target_id)
            if _state_sha(live_rows) != entry["after_state_sha256"]:
                raise PromotionConflictError(
                    f"tariff_charges for version {target_id} changed after promotion {entry['id']}"
                )
            for tc_id in json.loads(entry["inserted_row_ids_json"]):
                conn.execute("DELETE FROM tariff_charges WHERE id = ?", (tc_id,))
            for row in json.loads(entry["deleted_rows_json"]):
                _insert_tariff_charge(conn, row, keep_id=True)
            restored_sha = _state_sha(_tariff_charge_rows(conn, target_id))
        if restored_sha != entry["before_state_sha256"]:
            raise PromotionConflictError(
                f"rollback of promotion {entry['id']} did not reproduce the prior state"
            )
        log_id = _log_promotion(
            conn,
            operation_uuid=operation,
            action="rollback",
            target_kind=target_kind,
            target_id=target_id,
            from_run_id=entry["to_run_id"],
            to_run_id=entry["from_run_id"],
            reverts_log_id=entry["id"],
            at=_now(),
            by=by,
            reason=reason,
            before_state_sha256=entry["after_state_sha256"],
            after_state_sha256=restored_sha,
        )
        _set_active(conn, target_kind, target_id, entry["from_run_id"], log_id, by, reason)
    return {
        "rollback_log_id": log_id,
        "reverted_promotion_id": entry["id"],
        "active_run_id": entry["from_run_id"],
    }


def reject_run(conn: sqlite3.Connection, run_id: int, *, by: str, reason: str) -> int:
    """Record a review decision not to promote a candidate. The run is kept."""
    run = _run(conn, run_id)
    if run_review_status(conn, run_id) == "active":
        raise ProvenanceError(f"run {run_id} is active; roll it back instead")
    with atomic(conn):
        return _log_promotion(
            conn,
            operation_uuid=uuid.uuid4().hex,
            action="reject",
            target_kind=run["target_kind"],
            target_id=run["target_id"],
            to_run_id=run_id,
            at=_now(),
            by=by,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Metadata revisions
# ---------------------------------------------------------------------------


def _column_affinity(conn: sqlite3.Connection, table: str, column: str) -> str:
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row[1] == column:
            return (row[2] or "").upper()
    raise ProvenanceError(f"{table}.{column} does not exist")


def _coerce(value: str | None, affinity: str) -> Any:
    if value is None:
        return None
    if "INT" in affinity:
        return int(value)
    if "REAL" in affinity:
        return float(value)
    return value


def revise_metadata(
    conn: sqlite3.Connection,
    table: str,
    row_id: int,
    column: str,
    new_value: Any,
    *,
    changed_by: str,
    evidence: str | None = None,
    expected_old_value: Any = _UNSET,
) -> int | None:
    """Change one audited metadata column, attributed and in one transaction.

    Effective dates must parse. Returns the ``metadata_revisions`` id, or
    None when the value was already ``new_value``.
    """
    _require_schema(conn)
    if column not in AUDITED_METADATA_COLUMNS.get(table, ()):
        raise ProvenanceError(f"{table}.{column} is not an audited metadata column")
    if not changed_by.strip():
        raise ProvenanceError("changed_by is required")
    if column in ("effective_start", "effective_end") and new_value is not None:
        if not _parses_as_date(str(new_value)):
            raise ProvenanceError(f"{column}={new_value!r} does not parse as a date")
    with atomic(conn), write_context(conn, changed_by, evidence=evidence) as op:
        row = conn.execute(f"SELECT {column} FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise ProvenanceError(f"{table} row {row_id} does not exist")
        if expected_old_value is not _UNSET and row[0] != expected_old_value:
            raise PromotionConflictError(
                f"{table}.{column} for {row_id} is {row[0]!r}, expected {expected_old_value!r}"
            )
        if row[0] == new_value:
            return None
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (new_value, row_id))
        revision = conn.execute(
            "SELECT id FROM metadata_revisions WHERE operation_id = ? ORDER BY id DESC LIMIT 1",
            (op,),
        ).fetchone()
        return int(revision[0]) if revision else None


def revert_metadata(
    conn: sqlite3.Connection, revision_id: int, *, by: str, evidence: str | None = None
) -> int:
    """Restore the value a revision replaced, if nothing changed it since."""
    _require_schema(conn)
    rev = conn.execute("SELECT * FROM metadata_revisions WHERE id = ?", (revision_id,)).fetchone()
    if rev is None:
        raise ProvenanceError(f"revision {revision_id} does not exist")
    if rev["operation"] == "delete" or rev["column_name"] == "*":
        raise ProvenanceError("deleted rows cannot be reverted through revert_metadata")
    if rev["reverted_by"] is not None:
        raise ProvenanceError(f"revision {revision_id} was already reverted")
    table, column = rev["table_name"], rev["column_name"]
    affinity = _column_affinity(conn, table, column)
    with (
        atomic(conn),
        write_context(
            conn,
            by,
            evidence=evidence or f"revert of revision {revision_id}",
            revision_operation="revert",
        ) as op,
    ):
        current = conn.execute(
            f"SELECT CAST({column} AS TEXT) FROM {table} WHERE id = ?", (rev["row_id"],)
        ).fetchone()
        if current is None or current[0] != rev["new_value"]:
            raise PromotionConflictError(
                f"{table}.{column} for {rev['row_id']} changed after revision {revision_id}"
            )
        conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ?",
            (_coerce(rev["old_value"], affinity), rev["row_id"]),
        )
        new_rev = conn.execute(
            "SELECT id FROM metadata_revisions WHERE operation_id = ? ORDER BY id DESC LIMIT 1",
            (op,),
        ).fetchone()
        conn.execute(
            "UPDATE metadata_revisions SET reverted_by = ? WHERE id = ?", (new_rev[0], revision_id)
        )
        return int(new_rev[0])


def metadata_history(conn: sqlite3.Connection, table: str, row_id: int) -> list[dict[str, Any]]:
    _require_schema(conn)
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM metadata_revisions WHERE table_name = ? AND row_id = ? ORDER BY id",
            (table, row_id),
        )
    ]


def payload_history(
    conn: sqlite3.Connection, target_kind: str, target_id: int
) -> list[dict[str, Any]]:
    """Every stored representation of a parsed/statement payload, oldest first.

    Combines runs (with their promotion status) and trigger-captured
    overwrites made outside the run API.
    """
    _require_schema(conn)
    table, column, _ = PAYLOAD_TARGETS[target_kind]
    history: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT er.id, er.parser_name, er.parser_version, er.started_at, ed.payload_sha256 "
        "FROM extraction_runs er JOIN extracted_documents ed ON ed.run_id = er.id "
        "WHERE er.target_kind = ? AND er.target_id = ? ORDER BY er.id",
        (target_kind, target_id),
    ):
        history.append(
            {
                "kind": "run",
                "run_id": r["id"],
                "parser": r["parser_name"],
                "parser_version": r["parser_version"],
                "at": r["started_at"],
                "payload_sha256": r["payload_sha256"],
                "review_status": run_review_status(conn, r["id"]),
            }
        )
    for r in conn.execute(
        "SELECT id, old_payload, changed_at, changed_by, operation FROM document_payload_revisions "
        "WHERE table_name = ? AND row_id = ? AND column_name = ? ORDER BY id",
        (table, target_id, column),
    ):
        history.append(
            {
                "kind": "overwritten_value",
                "revision_id": r["id"],
                "at": r["changed_at"],
                "changed_by": r["changed_by"],
                "operation": r["operation"],
                "payload_sha256": sha256_text(r["old_payload"]),
            }
        )
    live = _payload_value(conn, target_kind, target_id)
    current = active_run(conn, target_kind, target_id)
    active_sha = None
    if current is not None:
        row = conn.execute(
            "SELECT payload_sha256 FROM extracted_documents WHERE run_id = ?", (current["run_id"],)
        ).fetchone()
        active_sha = row[0] if row else None
    history.append(
        {
            "kind": "live",
            "payload_sha256": sha256_text(live),
            "active_run_id": current["run_id"] if current else None,
            "matches_active_run": active_sha == sha256_text(live) if active_sha else None,
        }
    )
    return history


# ---------------------------------------------------------------------------
# Legacy backfill (migration M2) and materialisation check
# ---------------------------------------------------------------------------


def _legacy_charge(row: dict[str, Any]) -> CandidateCharge:
    return CandidateCharge(
        charge_type=row["charge_type"],
        charge_label=row["charge_label"],
        rate_value=row["rate_value"],
        rate_unit=row["rate_unit"],
        tier_min=row["tier_min"],
        tier_max=row["tier_max"],
        tou_period=row["tou_period"],
        season=row["season"],
        customer_class=row["customer_class"],
        source_quote=row["source_snippet"] or None,
        legacy_fields=frozenset({"quote", "source_pdf"}),
        extraction_method="legacy_backfill",
        confidence_score=row["confidence_score"] or 0.0,
        legacy_tariff_charge_id=row["id"],
        legacy_row=row,
    )


def backfill_legacy(
    conn: sqlite3.Connection,
    *,
    created_by: str,
    version_ids: Sequence[int] | None = None,
    include_payloads: bool = True,
) -> dict[str, int]:
    """Record today's legacy data as one active ``legacy`` run per target.

    Reads only; the legacy tables are not modified. Targets that already have
    an active run are skipped, so the backfill can be resumed.
    """
    _require_schema(conn)
    conn.row_factory = sqlite3.Row
    stats = defaultdict(int)
    version_sql = (
        "SELECT tv.id, tv.family_key, tv.effective_start, tv.effective_end, "
        "tv.historical_document_id, tv.document_id, hd.local_path AS hd_path, "
        "hd.content_hash AS hd_hash, d.local_path AS d_path, d.content_hash AS d_hash "
        "FROM tariff_versions tv "
        "LEFT JOIN historical_documents hd ON hd.id = tv.historical_document_id "
        "LEFT JOIN documents d ON d.id = tv.document_id"
    )
    params: list[Any] = []
    if version_ids is not None:
        version_sql += f" WHERE tv.id IN ({','.join('?' * len(version_ids))})"
        params = list(version_ids)
    versions = [dict(r) for r in conn.execute(version_sql + " ORDER BY tv.id", params)]
    if version_ids is None:
        # Charges whose tariff_versions row was deleted. Billing cannot reach
        # them, but they are still recorded so nothing in tariff_charges is
        # untracked.
        for r in conn.execute(
            "SELECT version_id, MIN(family_key) AS family_key FROM tariff_charges "
            "WHERE version_id NOT IN (SELECT id FROM tariff_versions) GROUP BY version_id"
        ):
            versions.append(
                {
                    "id": r["version_id"],
                    "family_key": r["family_key"],
                    "effective_start": None,
                    "effective_end": None,
                    "historical_document_id": None,
                    "document_id": None,
                    "hd_path": None,
                    "hd_hash": None,
                    "d_path": None,
                    "d_hash": None,
                    "orphan": True,
                }
            )
    started = _now()
    for v in versions:
        if active_run(conn, "tariff_version", v["id"]) is not None:
            stats["versions_skipped_active"] += 1
            continue
        if v.get("orphan"):
            stats["orphan_versions_backfilled"] += 1
        rows = _tariff_charge_rows(conn, v["id"])
        if v["historical_document_id"]:
            kind, doc_id, path, digest = (
                "historical_document",
                v["historical_document_id"],
                v["hd_path"],
                v["hd_hash"],
            )
        elif v["document_id"]:
            kind, doc_id, path, digest = "document", v["document_id"], v["d_path"], v["d_hash"]
        else:
            kind = doc_id = path = digest = None
        with atomic(conn):
            run_id = start_run(
                conn,
                target_kind="tariff_version",
                target_id=v["id"],
                parser_name=LEGACY_PARSER,
                parser_version=LEGACY_PARSER,
                extraction_method="legacy_backfill",
                created_by=created_by,
                family_key=v["family_key"],
                version_id=v["id"],
                source_document_kind=kind,
                source_document_id=doc_id,
                source_path=path,
                document_sha256=digest,
                document_sha256_status="stored_unverified" if digest else "unavailable",
                interpreted_effective_start=v["effective_start"],
                interpreted_effective_end=v["effective_end"],
                started_at=started,
                notes=(
                    "Phase 2A legacy backfill of tariff_charges as they stood"
                    + (
                        "; tariff_versions row missing (orphaned charges)"
                        if v.get("orphan")
                        else ""
                    )
                ),
            )
            charges = [_legacy_charge(r) for r in rows]
            for charge in charges:
                if path:
                    charge.source_pdf_path = path
                    charge.source_pdf_sha256 = digest
            record_charges(conn, run_id, charges)
            _seal_legacy(conn, run_id, len(rows))
            _activate_legacy(conn, "tariff_version", v["id"], run_id, created_by, rows)
        stats["versions_backfilled"] += 1
        stats["charges_backfilled"] += len(rows)

    if include_payloads:
        for target_kind, (table, column, _) in PAYLOAD_TARGETS.items():
            source_cols = (
                "local_path, content_hash, effective_start, effective_end, family_key"
                if table == "historical_documents"
                else "source_path AS local_path, content_hash, NULL AS effective_start, "
                "NULL AS effective_end, NULL AS family_key"
            )
            targets = conn.execute(
                f"SELECT id, {column} AS payload, {source_cols} FROM {table} "
                f"WHERE {column} IS NOT NULL ORDER BY id"
            ).fetchall()
            for t in targets:
                if active_run(conn, target_kind, t["id"]) is not None:
                    stats[f"{target_kind}_skipped_active"] += 1
                    continue
                with atomic(conn):
                    run_id = start_run(
                        conn,
                        target_kind=target_kind,
                        target_id=t["id"],
                        parser_name=LEGACY_PARSER,
                        parser_version=LEGACY_PARSER,
                        extraction_method="legacy_backfill",
                        created_by=created_by,
                        family_key=t["family_key"],
                        source_document_kind=table,
                        source_document_id=t["id"],
                        source_path=t["local_path"],
                        document_sha256=t["content_hash"],
                        document_sha256_status="stored_unverified"
                        if t["content_hash"]
                        else "unavailable",
                        interpreted_effective_start=t["effective_start"],
                        interpreted_effective_end=t["effective_end"],
                        started_at=started,
                        notes=f"Phase 2A legacy backfill of {table}.{column}",
                    )
                    record_document(conn, run_id, t["payload"])
                    _seal_legacy(conn, run_id, 1)
                    _activate_legacy(
                        conn, target_kind, t["id"], run_id, created_by, None, payload=t["payload"]
                    )
                stats[f"{target_kind}_backfilled"] += 1
    return dict(stats)


def _seal_legacy(conn: sqlite3.Connection, run_id: int, count: int) -> None:
    # Legacy runs record what is live; they are not validated as candidates.
    now = _now()
    conn.execute(
        "UPDATE extraction_runs SET status = ?, finished_at = ?, sealed_at = ?, "
        "validation_status = 'pending', metrics_json = ? WHERE id = ?",
        ("ok" if count else "empty", now, now, canonical_json({"output_count": count}), run_id),
    )


def _activate_legacy(conn, target_kind, target_id, run_id, by, rows, payload=None) -> None:
    sha = _state_sha(rows) if rows is not None else sha256_text(payload)
    log_id = _log_promotion(
        conn,
        operation_uuid=uuid.uuid4().hex,
        action="activate_legacy",
        target_kind=target_kind,
        target_id=target_id,
        to_run_id=run_id,
        at=_now(),
        by=by,
        reason="Phase 2A backfill: live legacy data recorded as the active result",
        before_state_sha256=sha,
        after_state_sha256=sha,
    )
    _set_active(
        conn, target_kind, target_id, run_id, log_id, by, "Phase 2A backfill of live legacy data"
    )


def verify_materialization(conn: sqlite3.Connection) -> dict[str, Any]:
    """Check that the read model still matches each target's active run.

    * legacy runs: every live row must equal its backfilled copy byte for byte
      (same ids, same column values); payloads must have the same SHA-256.
    * promoted runs: the live state must still hash to the promotion's
      ``after_state_sha256``.
    Any mismatch means someone wrote the legacy table outside this API.
    """
    _require_schema(conn)
    report: dict[str, Any] = {"checked": defaultdict(int), "mismatches": []}
    actives = conn.execute(
        "SELECT ar.*, er.parser_name, pl.after_state_sha256 FROM active_results ar "
        "JOIN extraction_runs er ON er.id = ar.run_id "
        "LEFT JOIN promotion_log pl ON pl.id = ar.promotion_log_id"
    ).fetchall()
    for a in actives:
        kind, tid = a["target_kind"], a["target_id"]
        report["checked"][kind] += 1
        if kind == "tariff_version":
            live = _tariff_charge_rows(conn, tid)
            if a["parser_name"] == LEGACY_PARSER:
                expected = [
                    json.loads(r[0])
                    for r in conn.execute(
                        "SELECT legacy_row_json FROM extracted_charges WHERE run_id = ? ORDER BY ordinal",
                        (a["run_id"],),
                    )
                ]
                if canonical_json(live) != canonical_json(expected):
                    report["mismatches"].append(
                        {
                            "target_kind": kind,
                            "target_id": tid,
                            "run_id": a["run_id"],
                            "live_rows": len(live),
                            "expected_rows": len(expected),
                        }
                    )
            elif _state_sha(live) != a["after_state_sha256"]:
                report["mismatches"].append(
                    {
                        "target_kind": kind,
                        "target_id": tid,
                        "run_id": a["run_id"],
                        "detail": "live rows changed since promotion",
                    }
                )
        elif kind == RIDER_TARGET:
            from duke_rates.db import rider_summary_provenance as rsp

            if rsp.state_sha(conn, tid) != a["after_state_sha256"]:
                report["mismatches"].append(
                    {
                        "target_kind": kind,
                        "target_id": tid,
                        "run_id": a["run_id"],
                        "detail": "live rider summary changed since promotion",
                    }
                )
        else:
            live_sha = sha256_text(_payload_value(conn, kind, tid))
            doc = conn.execute(
                "SELECT payload_sha256 FROM extracted_documents WHERE run_id = ?", (a["run_id"],)
            ).fetchone()
            if doc is None or doc[0] != live_sha:
                report["mismatches"].append(
                    {"target_kind": kind, "target_id": tid, "run_id": a["run_id"]}
                )
    untracked = conn.execute(
        "SELECT COUNT(DISTINCT version_id) FROM tariff_charges WHERE version_id NOT IN "
        "(SELECT target_id FROM active_results WHERE target_kind = 'tariff_version')"
    ).fetchone()[0]
    report["versions_with_charges_but_no_active_run"] = untracked
    report["checked"] = dict(report["checked"])
    report["ok"] = not report["mismatches"] and untracked == 0
    return report


def provenance_completeness(
    conn: sqlite3.Connection, run_ids: Sequence[int] | None = None
) -> dict[str, Any]:
    """Share of charges with each provenance field available, per parser."""
    _require_schema(conn)
    where, params = "", []
    if run_ids:
        where = f"WHERE ec.run_id IN ({','.join('?' * len(run_ids))})"
        params = list(run_ids)
    out: dict[str, Any] = {}
    for row in conn.execute(
        f"""
        SELECT er.parser_name, COUNT(*) AS n,
               SUM(ec.source_pdf_status = 'available') AS pdf_available,
               SUM(ec.source_pdf_status = 'legacy_unverified') AS pdf_legacy,
               SUM(ec.page_status = 'available') AS page_available,
               SUM(ec.bbox_status = 'available') AS bbox_available,
               SUM(ec.quote_status = 'available') AS quote_available,
               SUM(ec.quote_status = 'legacy_unverified') AS quote_legacy,
               SUM(ec.canonical_unit IS NOT NULL) AS canonical_unit,
               SUM(ec.effective_start IS NOT NULL) AS effective_start
        FROM extracted_charges ec JOIN extraction_runs er ON er.id = ec.run_id
        {where} GROUP BY er.parser_name
        """,
        params,
    ):
        out[row["parser_name"]] = {k: row[k] for k in row.keys() if k != "parser_name"}
    return out
