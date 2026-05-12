from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from duke_rates.document_intelligence.llm_charge_promotion import (
    promote_llm_charge_proposals,
    propose_llm_charge_promotions,
)
from duke_rates.document_intelligence.llm_extraction_validation import (
    validate_candidate_extractions,
)
from duke_rates.document_intelligence.llm_row_evidence_locator import (
    LLMRowEvidenceLocator,
)


def run_llm_promotion_overnight(
    db_path: Path | str,
    *,
    validation_limit: int = 500,
    repair_limit: int = 1000,
    proposal_limit: int = 5000,
    promotion_limit: int = 100,
    execute_safe: bool = False,
    output_dir: Path | str = Path("docs/reports/llm_promotion_overnight"),
) -> dict[str, Any]:
    """Run the safe overnight LLM-promotion maintenance loop.

    The runner intentionally avoids lineage mutation, target-version bootstrap,
    and any LLM calls. It refreshes deterministic validation/proposal state,
    dry-runs promotion, and only inserts charges when ``execute_safe`` is set.
    """
    db = Path(db_path)
    started_at = _utc_now()
    before = _snapshot(db)

    validation_reports = {
        status: validate_candidate_extractions(
            db,
            limit=validation_limit,
            status=status,
            execute=True,
        )["summary"]
        for status in ("candidate", "review_candidate")
    }

    locator = LLMRowEvidenceLocator(None, db)
    repair_report = locator.apply_deterministic_repairs(
        limit=repair_limit,
        execute=True,
    )
    effective_status = locator.effective_status_report()

    proposal_create_report = propose_llm_charge_promotions(
        db,
        limit=proposal_limit,
        include_repaired=True,
        refresh_existing=False,
        execute=True,
    )
    proposal_refresh_report = propose_llm_charge_promotions(
        db,
        limit=proposal_limit,
        include_repaired=True,
        refresh_existing=True,
        execute=True,
    )
    promotion_dry_run = promote_llm_charge_proposals(
        db,
        limit=promotion_limit,
        execute=False,
    )

    promotion_execute: dict[str, Any] | None = None
    dry_summary = promotion_dry_run["summary"]
    if (
        execute_safe
        and dry_summary["evaluated"] > 0
        and dry_summary["skipped"] == 0
    ):
        promotion_execute = promote_llm_charge_proposals(
            db,
            limit=promotion_limit,
            execute=True,
        )

    after = _snapshot(db)
    finished_at = _utc_now()
    report = {
        "started_at": started_at,
        "finished_at": finished_at,
        "execute_safe": execute_safe,
        "limits": {
            "validation_limit": validation_limit,
            "repair_limit": repair_limit,
            "proposal_limit": proposal_limit,
            "promotion_limit": promotion_limit,
        },
        "before": before,
        "after": after,
        "delta": _delta(before, after),
        "validation": validation_reports,
        "deterministic_repairs": repair_report["summary"],
        "effective_status": effective_status,
        "proposal_create": proposal_create_report["summary"],
        "proposal_refresh": proposal_refresh_report["summary"],
        "promotion_dry_run": promotion_dry_run["summary"],
        "promotion_execute": promotion_execute["summary"] if promotion_execute else None,
        "report_path": "",
    }
    report["report_path"] = str(_write_report(report, Path(output_dir)))
    return report


def _snapshot(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return {
            "tariff_charges": _scalar(conn, "SELECT COUNT(*) FROM tariff_charges"),
            "promoted_audit": _scalar(
                conn,
                "SELECT COUNT(*) FROM llm_promoted_charge_audit",
                default=0,
            ),
            "pending_promotable": _scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM llm_rate_charge_promotion_proposals
                WHERE promotion_status = 'pending'
                  AND eligibility_status = 'eligible'
                  AND duplicate_status = 'novel'
                  AND conflict_status = 'none'
                """,
                default=0,
            ),
            "pending_blockers": _rows(
                conn,
                """
                SELECT eligibility_issues_json AS issues, COUNT(*) AS count
                FROM llm_rate_charge_promotion_proposals
                WHERE promotion_status = 'pending'
                  AND eligibility_status = 'blocked'
                GROUP BY eligibility_issues_json
                ORDER BY count DESC
                """,
            ),
            "proposal_status": _rows(
                conn,
                """
                SELECT eligibility_status, duplicate_status, conflict_status,
                       promotion_status, COUNT(*) AS count
                FROM llm_rate_charge_promotion_proposals
                GROUP BY eligibility_status, duplicate_status, conflict_status,
                         promotion_status
                ORDER BY count DESC
                """,
            ),
        }
    finally:
        conn.close()


def _scalar(conn: sqlite3.Connection, sql: str, default: int = 0) -> int:
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return default
    return int(row[0]) if row and row[0] is not None else default


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    except sqlite3.Error:
        return []


def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    keys = ("tariff_charges", "promoted_audit", "pending_promotable")
    return {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in keys}


def _write_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"llm_promotion_overnight_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
