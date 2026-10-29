"""Read-only plans comparing isolated proposed rates with accepted lineage.

Proposal blocks remain valuable research even when their source is redlined.
Their presence, a proposed date, and a CLI confirmation do not establish
regulatory approval. The former direct-to-approved insertion shortcut is
retired: use the existing reviewed source-binding and candidate-promotion
workflow for accepted records, retaining its validation and rollback history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
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


def apply_promotion(conn: sqlite3.Connection, plan: PromotionPlan) -> list[PromotionAction]:
    """Refuse the retired approval shortcut without changing proposal data.

    Retained as a fail-closed compatibility entry point for existing callers.
    A source-reviewed binding and validated extraction run must precede any
    accepted charge promotion; a raw proposal plan cannot supply that proof.
    """
    if not plan.actionable:
        return []
    raise UnsafeProposalPromotionError(
        "Direct proposal-to-approved promotion is disabled: a proposal plan does not establish "
        "source integrity or regulatory approval. Keep proposed_tariff_* for research; use "
        "provenance propose-binding / approve-proposal / apply-proposal, then extract-candidate "
        "and reviewed provenance promote. "
        "Review regulatory approval separately before billing selection."
    )


class UnsafeProposalPromotionError(RuntimeError):
    """A raw proposal plan lacks the existing reviewed promotion evidence."""


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
