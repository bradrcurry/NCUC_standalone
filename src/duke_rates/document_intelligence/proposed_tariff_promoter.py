"""Promote approved proposed-tariff candidates into the accepted lineage.

This module is the deliberate, opt-in bridge from the ``proposed_tariff_*``
tables into the accepted ``tariff_families`` / ``tariff_versions`` /
``tariff_charges`` lineage. It is the *only* place in the codebase that
writes to accepted lineage on behalf of a proposed application, and it does
so with three guardrails:

1. Planning is separate from execution. ``plan_promotion`` builds an
   ordered list of ``PromotionAction`` objects describing exactly what
   would be written. ``apply_promotion`` is the only function that issues
   any INSERT statements.
2. The CLI defaults to dry-run; ``--confirm`` must be passed explicitly to
   call ``apply_promotion``.
3. Idempotency: ``plan_promotion`` skips families that already have an
   accepted ``tariff_versions`` row for the same ``(family_key,
   effective_start)`` pair, so re-running after a successful promotion is a
   no-op rather than a duplicate.

Operators are responsible for only running this on dockets that have an
actual final order from the commission — there is no automation that
verifies a docket has been approved.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from duke_rates.document_intelligence.proposed_vs_approved import (
    ProposedCharge,
    build_comparisons,
)


@dataclass
class ChargeToCreate:
    charge_type: str
    charge_label: str
    rate_value: float | None
    rate_unit: str | None
    notes: str | None = None


@dataclass
class FamilyToCreate:
    family_key: str
    schedule_code: str
    family_type: str
    title: str
    state: str
    company: str | None


@dataclass
class PromotionAction:
    """One promotion: a planned version + charges (and optional new family)."""

    docket_number: str
    exhibit_key: str
    tariff_kind: str
    schedule_code: str
    tariff_name: str
    proposed_block_ids: list[int]
    effective_start: str | None
    family_key: str | None
    family_to_create: FamilyToCreate | None
    matched_existing_family: bool
    charges: list[ChargeToCreate]
    source_pdf: str
    historical_document_id: int | None
    leaf_no: int | None
    pages: list[int]
    skip_reason: str | None = None
    created_version_id: int | None = None
    created_charge_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "docket_number": self.docket_number,
            "exhibit_key": self.exhibit_key,
            "tariff_kind": self.tariff_kind,
            "schedule_code": self.schedule_code,
            "tariff_name": self.tariff_name,
            "proposed_block_ids": self.proposed_block_ids,
            "effective_start": self.effective_start,
            "family_key": self.family_key,
            "family_to_create": (
                self.family_to_create.__dict__ if self.family_to_create else None
            ),
            "matched_existing_family": self.matched_existing_family,
            "charges": [c.__dict__ for c in self.charges],
            "source_pdf": self.source_pdf,
            "historical_document_id": self.historical_document_id,
            "leaf_no": self.leaf_no,
            "pages": self.pages,
            "skip_reason": self.skip_reason,
            "created_version_id": self.created_version_id,
            "created_charge_ids": self.created_charge_ids,
        }


@dataclass
class PromotionPlan:
    docket_number: str
    actions: list[PromotionAction]

    @property
    def actionable(self) -> list[PromotionAction]:
        return [a for a in self.actions if a.skip_reason is None]


def plan_promotion(
    conn: sqlite3.Connection,
    *,
    docket_number: str,
    utility: str | None = None,
    exhibit_filter: str | None = None,
    code_filter: str | None = None,
    create_new_families: bool = False,
) -> PromotionPlan:
    """Build the per-tariff promotion plan without writing anything.

    Each ``TariffComparison`` for the docket becomes one ``PromotionAction``.
    The action is marked skipped (and ``skip_reason`` filled) when it cannot
    be safely promoted — for example, no proposed charges were captured, no
    accepted family matched and ``create_new_families`` was not requested,
    or an accepted version with the same ``(family_key, effective_start)``
    already exists.
    """
    conn.row_factory = sqlite3.Row
    comparisons = build_comparisons(
        conn,
        docket_number=docket_number,
        utility=utility,
        exhibit_filter=exhibit_filter,
        code_filter=code_filter,
    )
    actions: list[PromotionAction] = []
    source_pdf, historical_document_id = _lookup_source_pdf(conn, docket_number)
    company = _detect_company_for_docket(conn, docket_number)

    for comparison in comparisons:
        block_ids = _lookup_block_ids(
            conn,
            docket_number=docket_number,
            exhibit_key=comparison.exhibit_key,
            tariff_kind=comparison.tariff_kind,
            schedule_code=comparison.schedule_code,
            tariff_name=comparison.tariff_name,
        )
        charges = [_to_create(pc) for pc in comparison.proposed_charges]
        family_key: str | None = None
        family_to_create: FamilyToCreate | None = None
        matched_existing = False

        if comparison.family_match is not None:
            family_key = comparison.family_match.family_key
            matched_existing = True
        elif create_new_families and comparison.tariff_kind in {"rider", "schedule"}:
            family_to_create = _draft_new_family(
                tariff_name=comparison.tariff_name,
                tariff_kind=comparison.tariff_kind,
                schedule_code=comparison.schedule_code,
                leaf_no=comparison.proposed_leaf_no,
                company=company,
            )
            family_key = family_to_create.family_key

        action = PromotionAction(
            docket_number=docket_number,
            exhibit_key=comparison.exhibit_key,
            tariff_kind=comparison.tariff_kind,
            schedule_code=comparison.schedule_code,
            tariff_name=comparison.tariff_name,
            proposed_block_ids=block_ids,
            effective_start=comparison.proposed_effective_start,
            family_key=family_key,
            family_to_create=family_to_create,
            matched_existing_family=matched_existing,
            charges=charges,
            source_pdf=source_pdf or "",
            historical_document_id=historical_document_id,
            leaf_no=comparison.proposed_leaf_no,
            pages=comparison.pages,
        )

        skip = _decide_skip(conn, action, create_new_families=create_new_families)
        action.skip_reason = skip
        actions.append(action)

    return PromotionPlan(docket_number=docket_number, actions=actions)


def apply_promotion(
    conn: sqlite3.Connection, plan: PromotionPlan
) -> list[PromotionAction]:
    """Execute an already-planned promotion.

    Each actionable plan entry is written inside a single transaction —
    if any insert fails the whole promotion rolls back. Created
    ``tariff_versions.id`` and ``tariff_charges.id`` values are written
    back onto the action so callers can audit what happened.
    """
    actionable = plan.actionable
    if not actionable:
        return []
    now = datetime.now(UTC).isoformat()
    with conn:
        for action in actionable:
            assert action.family_key is not None
            if action.family_to_create is not None:
                _insert_family(conn, action.family_to_create, now)
            version_id = _insert_version(conn, action, now)
            action.created_version_id = version_id
            for charge in action.charges:
                charge_id = _insert_charge(conn, version_id, charge)
                if charge_id is not None:
                    action.created_charge_ids.append(charge_id)
    return actionable


def _decide_skip(
    conn: sqlite3.Connection,
    action: PromotionAction,
    *,
    create_new_families: bool,
) -> str | None:
    if not action.charges:
        return "no proposed charges captured"
    if action.tariff_kind == "rider_catalog":
        return "rider_catalog entries are index pointers, not charge records"
    if action.family_key is None and not create_new_families:
        return "no matching accepted family (pass --create-new-families to opt in)"
    if action.family_key is None:
        return "family_key could not be inferred"
    if action.effective_start is None:
        return "no proposed effective_start (cannot place on the timeline)"
    existing = conn.execute(
        """
        SELECT id FROM tariff_versions
        WHERE family_key = ?
          AND COALESCE(effective_start, '') = COALESCE(?, '')
        LIMIT 1
        """,
        (action.family_key, action.effective_start),
    ).fetchone()
    if existing is not None:
        return (
            f"already promoted (tariff_versions.id={existing[0]} for "
            f"family_key={action.family_key} effective_start={action.effective_start})"
        )
    return None


def _to_create(pc: ProposedCharge) -> ChargeToCreate:
    return ChargeToCreate(
        charge_type=pc.charge_type,
        charge_label=pc.charge_label,
        rate_value=pc.rate_value,
        rate_unit=pc.rate_unit,
        notes=f"promoted_from_proposed_raw_line: {pc.raw_line[:200]}",
    )


def _draft_new_family(
    *,
    tariff_name: str,
    tariff_kind: str,
    schedule_code: str,
    leaf_no: int | None,
    company: str | None,
) -> FamilyToCreate:
    if leaf_no is not None and company:
        family_key = f"nc-{company}-leaf-{leaf_no}"
    elif company:
        slug = (schedule_code or tariff_name).lower().replace(" ", "-")
        family_key = f"nc-{company}-{tariff_kind}-{slug}"
    else:
        slug = (schedule_code or tariff_name).lower().replace(" ", "-")
        family_key = f"nc-unknown-{tariff_kind}-{slug}"
    return FamilyToCreate(
        family_key=family_key,
        schedule_code=schedule_code or tariff_name.split()[1] if tariff_name else "",
        family_type=tariff_kind,
        title=tariff_name,
        state="NC",
        company=company,
    )


def _lookup_source_pdf(
    conn: sqlite3.Connection, docket_number: str
) -> tuple[str | None, int | None]:
    row = conn.execute(
        "SELECT source_pdf, source_record_id FROM proposed_tariff_documents "
        "WHERE docket_number = ? LIMIT 1",
        (docket_number,),
    ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _detect_company_for_docket(
    conn: sqlite3.Connection, docket_number: str
) -> str | None:
    row = conn.execute(
        "SELECT utility FROM proposed_tariff_documents "
        "WHERE docket_number = ? LIMIT 1",
        (docket_number,),
    ).fetchone()
    if not row or not row[0]:
        return None
    utility = str(row[0]).lower()
    if "carolinas" in utility:
        return "carolinas"
    if "progress" in utility:
        return "progress"
    return None


def _lookup_block_ids(
    conn: sqlite3.Connection,
    *,
    docket_number: str,
    exhibit_key: str,
    tariff_kind: str,
    schedule_code: str,
    tariff_name: str,
) -> list[int]:
    rows = conn.execute(
        """
        SELECT proposed_tariff_blocks.id AS block_id
        FROM proposed_tariff_blocks
        JOIN proposed_tariff_documents
            ON proposed_tariff_blocks.proposed_document_id
               = proposed_tariff_documents.id
        WHERE proposed_tariff_documents.docket_number = ?
          AND proposed_tariff_blocks.exhibit_key = ?
          AND proposed_tariff_blocks.tariff_kind = ?
          AND COALESCE(proposed_tariff_blocks.schedule_code, '') = ?
          AND proposed_tariff_blocks.tariff_name = ?
        """,
        (
            docket_number,
            exhibit_key,
            tariff_kind,
            schedule_code or "",
            tariff_name,
        ),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _insert_family(
    conn: sqlite3.Connection, family: FamilyToCreate, now: str
) -> None:
    info = conn.execute("PRAGMA table_info(tariff_families)").fetchall()
    if not info:
        raise RuntimeError("tariff_families table is missing from this DB")
    columns = {row[1] for row in info}
    payload: dict[str, Any] = {
        "family_key": family.family_key,
        "state": family.state,
        "company": family.company,
        "schedule_code": family.schedule_code,
        "family_type": family.family_type,
        "title": family.title,
        "created_at": now,
        "updated_at": now,
        "aliases_json": "[]",
        "notes": "created via proposed_tariff_promoter",
    }
    payload = {k: v for k, v in payload.items() if k in columns}
    cols = ", ".join(payload.keys())
    placeholders = ", ".join("?" for _ in payload)
    conn.execute(
        f"INSERT OR IGNORE INTO tariff_families ({cols}) VALUES ({placeholders})",
        list(payload.values()),
    )


def _insert_version(
    conn: sqlite3.Connection, action: PromotionAction, now: str
) -> int:
    info = conn.execute("PRAGMA table_info(tariff_versions)").fetchall()
    if not info:
        raise RuntimeError("tariff_versions table is missing from this DB")
    columns = {row[1] for row in info}
    notes = json.dumps(
        {
            "source": "proposed_tariff_promoter",
            "promoted_from_proposed_block_ids": action.proposed_block_ids,
            "docket_number": action.docket_number,
            "exhibit_key": action.exhibit_key,
            "pages": action.pages,
        }
    )
    payload: dict[str, Any] = {
        "family_key": action.family_key,
        "effective_start": action.effective_start,
        "docket_number": action.docket_number,
        "source_pdf": action.source_pdf or None,
        "leaf_no": str(action.leaf_no) if action.leaf_no is not None else None,
        "status": "approved",
        "source_type": "promoted_from_proposal",
        "historical_document_id": action.historical_document_id,
        "created_at": now,
        "notes": notes,
    }
    payload = {k: v for k, v in payload.items() if k in columns}
    cols = ", ".join(payload.keys())
    placeholders = ", ".join("?" for _ in payload)
    cur = conn.execute(
        f"INSERT INTO tariff_versions ({cols}) VALUES ({placeholders})",
        list(payload.values()),
    )
    return int(cur.lastrowid)


def _insert_charge(
    conn: sqlite3.Connection, version_id: int, charge: ChargeToCreate
) -> int | None:
    info = conn.execute("PRAGMA table_info(tariff_charges)").fetchall()
    columns = {row[1] for row in info}
    payload: dict[str, Any] = {
        "version_id": version_id,
        "charge_type": charge.charge_type,
        "charge_label": charge.charge_label,
        "rate_value": charge.rate_value,
        "rate_unit": charge.rate_unit,
        "notes": charge.notes,
        "source_snippet": charge.notes,
    }
    payload = {k: v for k, v in payload.items() if k in columns}
    cols = ", ".join(payload.keys())
    placeholders = ", ".join("?" for _ in payload)
    cur = conn.execute(
        f"INSERT INTO tariff_charges ({cols}) VALUES ({placeholders})",
        list(payload.values()),
    )
    return int(cur.lastrowid) if cur.lastrowid else None
