"""Read-only comparison: proposed tariff candidates vs currently-approved rates.

This module is deliberately one-way: it reads ``proposed_tariff_*`` and the
accepted ``tariff_families`` / ``tariff_versions`` / ``tariff_charges`` tables
side by side. It never writes to either lane. Promotion of a proposed
candidate into accepted lineage stays an explicit, separate workflow.

The matching from ``proposed_tariff_blocks`` to an accepted ``tariff_families``
row is intentionally fuzzy because the production lineage uses several
naming conventions (leaf-keyed families like ``nc-progress-leaf-653``,
auto-generated long codes derived from titles, ``_RY1`` suffixed codes
imported from rate-year exhibits, etc.). We try a small ordered set of
strategies and report which strategy actually matched so the result is
auditable.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable


_UTILITY_TO_COMPANY = {
    "duke energy progress": "progress",
    "duke energy carolinas": "carolinas",
    "progress": "progress",
    "carolinas": "carolinas",
    "dep": "progress",
    "dec": "carolinas",
}


@dataclass(frozen=True)
class ProposedCharge:
    charge_type: str
    charge_label: str
    rate_value: float | None
    rate_unit: str | None
    raw_line: str


@dataclass(frozen=True)
class ApprovedCharge:
    charge_type: str
    charge_label: str
    rate_value: float | None
    rate_unit: str | None


@dataclass(frozen=True)
class FamilyMatch:
    family_key: str
    family_title: str
    family_schedule_code: str
    match_strategy: str


@dataclass
class TariffComparison:
    """Side-by-side comparison for one proposed tariff (schedule or rider)."""

    docket_number: str
    exhibit_key: str
    tariff_kind: str
    schedule_code: str
    tariff_name: str
    pages: list[int]
    proposed_charges: list[ProposedCharge]
    family_match: FamilyMatch | None = None
    approved_version_id: int | None = None
    approved_effective_start: str | None = None
    approved_charges: list[ApprovedCharge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "docket_number": self.docket_number,
            "exhibit_key": self.exhibit_key,
            "tariff_kind": self.tariff_kind,
            "schedule_code": self.schedule_code,
            "tariff_name": self.tariff_name,
            "pages": self.pages,
            "proposed_charges": [c.__dict__ for c in self.proposed_charges],
            "family_match": (
                self.family_match.__dict__ if self.family_match else None
            ),
            "approved_version_id": self.approved_version_id,
            "approved_effective_start": self.approved_effective_start,
            "approved_charges": [c.__dict__ for c in self.approved_charges],
        }


def utility_to_company(utility: str | None) -> str | None:
    if not utility:
        return None
    return _UTILITY_TO_COMPANY.get(utility.strip().lower())


def build_comparisons(
    conn: sqlite3.Connection,
    *,
    docket_number: str,
    utility: str | None = None,
    exhibit_filter: str | None = None,
    code_filter: str | None = None,
) -> list[TariffComparison]:
    """Build per-tariff proposed-vs-approved comparisons for a docket.

    Blocks for the same ``(exhibit_key, tariff_kind, schedule_code,
    tariff_name)`` tuple are collapsed into one comparison row, with their
    pages and proposed charge candidates merged.
    """
    conn.row_factory = sqlite3.Row
    where = ["docket_number = ?"]
    params: list[Any] = [docket_number]
    if exhibit_filter:
        where.append("exhibit_key = ?")
        params.append(exhibit_filter)
    if code_filter:
        where.append("schedule_code = ?")
        params.append(code_filter.upper())

    sql = f"""
        SELECT proposed_tariff_blocks.id AS block_id,
               proposed_tariff_blocks.exhibit_key AS exhibit_key,
               proposed_tariff_blocks.tariff_kind AS tariff_kind,
               proposed_tariff_blocks.schedule_code AS schedule_code,
               proposed_tariff_blocks.tariff_name AS tariff_name,
               proposed_tariff_blocks.start_page AS start_page
        FROM proposed_tariff_blocks
        JOIN proposed_tariff_documents
            ON proposed_tariff_blocks.proposed_document_id
               = proposed_tariff_documents.id
        WHERE {" AND ".join(where)}
        ORDER BY exhibit_key, tariff_kind, schedule_code, tariff_name, start_page
    """
    rows = conn.execute(sql, params).fetchall()
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            row["exhibit_key"],
            row["tariff_kind"],
            row["schedule_code"] or "",
            row["tariff_name"],
        )
        bucket = grouped.setdefault(
            key,
            {"block_ids": [], "pages": set()},
        )
        bucket["block_ids"].append(row["block_id"])
        bucket["pages"].add(row["start_page"])

    comparisons: list[TariffComparison] = []
    company = utility_to_company(utility)
    for (exhibit_key, tariff_kind, schedule_code, tariff_name), data in grouped.items():
        proposed = _load_proposed_charges(conn, data["block_ids"])
        comparison = TariffComparison(
            docket_number=docket_number,
            exhibit_key=exhibit_key,
            tariff_kind=tariff_kind,
            schedule_code=schedule_code,
            tariff_name=tariff_name,
            pages=sorted(data["pages"]),
            proposed_charges=proposed,
        )
        match = match_family(
            conn,
            tariff_kind=tariff_kind,
            schedule_code=schedule_code,
            tariff_name=tariff_name,
            company=company,
        )
        if match is not None:
            comparison.family_match = match
            version = _latest_approved_version(conn, match.family_key)
            if version is not None:
                comparison.approved_version_id = int(version["id"])
                comparison.approved_effective_start = version["effective_start"]
                comparison.approved_charges = _load_approved_charges(
                    conn, int(version["id"])
                )
        comparisons.append(comparison)
    return comparisons


def match_family(
    conn: sqlite3.Connection,
    *,
    tariff_kind: str,
    schedule_code: str,
    tariff_name: str,
    company: str | None,
) -> FamilyMatch | None:
    """Try to match a proposed tariff name/code to a ``tariff_families`` row.

    Strategies are tried in order; the first one that returns exactly one
    family wins. Each match strategy is recorded on the returned object so
    consumers can audit how each comparison was built.
    """
    conn.row_factory = sqlite3.Row
    base_filters = ["state = ?"]
    base_params: list[Any] = ["NC"]
    if company:
        base_filters.append("company = ?")
        base_params.append(company)
    if tariff_kind == "rider":
        base_filters.append("family_type = ?")
        base_params.append("rider")
    elif tariff_kind == "schedule":
        base_filters.append("family_type = ?")
        base_params.append("schedule")

    code_token = _extract_code_token(tariff_name) or schedule_code
    title_tail = _strip_rider_prefix(tariff_name)

    strategies: list[tuple[str, str, list[Any]]] = []

    if code_token:
        strategies.append(
            (
                "schedule_code_suffix",
                "schedule_code LIKE ?",
                [f"%_{code_token}"],
            )
        )
        strategies.append(
            (
                "schedule_code_suffix_with_ry1",
                "schedule_code LIKE ?",
                [f"%_{code_token}_RY1"],
            )
        )
        strategies.append(
            (
                "schedule_code_equals",
                "schedule_code = ?",
                [code_token],
            )
        )
        strategies.append(
            (
                "title_ends_with_code",
                "UPPER(title) LIKE ?",
                [f"% {code_token}"],
            )
        )
        strategies.append(
            (
                "title_contains_rider_code",
                "UPPER(title) LIKE ?",
                [f"%RIDER {code_token}%"],
            )
        )

    if title_tail:
        strategies.append(
            (
                "title_substring",
                "UPPER(title) LIKE ?",
                [f"%{title_tail.upper()}%"],
            )
        )

    for strategy, clause, extra_params in strategies:
        sql = (
            "SELECT family_key, title, schedule_code FROM tariff_families "
            "WHERE " + " AND ".join(base_filters + [clause])
        )
        rows = conn.execute(sql, base_params + extra_params).fetchall()
        if len(rows) == 1:
            row = rows[0]
            return FamilyMatch(
                family_key=row["family_key"],
                family_title=row["title"] or "",
                family_schedule_code=row["schedule_code"] or "",
                match_strategy=strategy,
            )
    return None


def _extract_code_token(tariff_name: str) -> str | None:
    """Return the short tariff code embedded in a proposed name.

    ``RIDER PC PENSIONS COSTS`` -> ``PC``
    ``SCHEDULE LGS-TOU`` -> ``LGS-TOU``
    ``RIDER BPM-P BPM PROSPECTIVE`` -> ``BPM-P``
    """
    match = re.match(
        r"^\s*(?:RIDER|SCHEDULE)\s+(?P<code>[A-Z][A-Z0-9-]{0,15})\b",
        tariff_name or "",
    )
    if match:
        return match.group("code")
    return None


_RIDER_PREFIX_RE = re.compile(
    r"^\s*(?:RIDER|SCHEDULE)\s+[A-Z][A-Z0-9-]{0,15}(?:\s+|$)",
    re.IGNORECASE,
)


def _strip_rider_prefix(tariff_name: str) -> str:
    """Return the descriptive tail of a tariff name after ``RIDER <CODE>``."""
    stripped = _RIDER_PREFIX_RE.sub("", tariff_name or "").strip()
    return stripped


def _load_proposed_charges(
    conn: sqlite3.Connection, block_ids: Iterable[int]
) -> list[ProposedCharge]:
    ids = list(block_ids)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT charge_type, charge_label, rate_value, rate_unit, raw_line
        FROM proposed_tariff_charge_candidates
        WHERE proposed_block_id IN ({placeholders})
        ORDER BY charge_type, charge_label
        """,
        ids,
    ).fetchall()
    return [
        ProposedCharge(
            charge_type=row["charge_type"],
            charge_label=row["charge_label"],
            rate_value=row["rate_value"],
            rate_unit=row["rate_unit"],
            raw_line=row["raw_line"],
        )
        for row in rows
    ]


def _latest_approved_version(
    conn: sqlite3.Connection, family_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, effective_start
        FROM tariff_versions
        WHERE family_key = ?
          AND COALESCE(status, '') IN ('', 'approved', 'effective')
        ORDER BY
            CASE WHEN effective_start IS NULL THEN 1 ELSE 0 END,
            effective_start DESC,
            id DESC
        LIMIT 1
        """,
        (family_key,),
    ).fetchone()


def _load_approved_charges(
    conn: sqlite3.Connection, version_id: int
) -> list[ApprovedCharge]:
    rows = conn.execute(
        """
        SELECT charge_type, charge_label, rate_value, rate_unit
        FROM tariff_charges
        WHERE version_id = ?
        ORDER BY charge_type, charge_label
        """,
        (version_id,),
    ).fetchall()
    return [
        ApprovedCharge(
            charge_type=row["charge_type"],
            charge_label=row["charge_label"],
            rate_value=row["rate_value"],
            rate_unit=row["rate_unit"],
        )
        for row in rows
    ]
