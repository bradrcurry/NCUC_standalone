"""
Version comparison utilities for tariff charges.

Compares two tariff_versions of the same family by their extracted
tariff_charges, surfaces rate deltas, added/removed charges, and
flags redline-to-clean transitions.

Designed to be called from the compare-version-rates CLI command or
used programmatically when processing a new document to check how it
differs from the previous version.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional


@dataclass
class RateDelta:
    charge_label: str
    charge_type: str
    old_value: Optional[float]
    new_value: Optional[float]
    old_unit: Optional[str]
    new_unit: Optional[str]
    delta_abs: Optional[float]
    delta_pct: Optional[float]
    change_type: str   # "added" | "removed" | "changed" | "unchanged"


@dataclass
class VersionCompareResult:
    family_key: str
    leaf_no: Optional[str]
    version_a_id: int
    version_b_id: int
    effective_date_a: Optional[str]
    effective_date_b: Optional[str]
    revision_label_a: Optional[str]
    revision_label_b: Optional[str]
    redline_flag_a: bool
    redline_flag_b: bool
    doc_tier_a: Optional[str]
    doc_tier_b: Optional[str]
    rate_deltas: list[RateDelta]
    added_charges: list[str]    # charge_labels only present in version B
    removed_charges: list[str]  # charge_labels only present in version A
    changed_charges: list[str]  # charge_labels present in both but different value

    @property
    def summary(self) -> str:
        parts = []
        if self.changed_charges:
            parts.append(f"{len(self.changed_charges)} changed")
        if self.added_charges:
            parts.append(f"{len(self.added_charges)} added")
        if self.removed_charges:
            parts.append(f"{len(self.removed_charges)} removed")
        if not parts:
            parts.append("no rate changes")
        tier_note = ""
        if self.doc_tier_a and self.doc_tier_b and self.doc_tier_a != self.doc_tier_b:
            tier_note = f" [tier {self.doc_tier_a}=>{self.doc_tier_b}]"
        redline_note = ""
        if self.redline_flag_a and not self.redline_flag_b:
            redline_note = " [redline=>clean]"
        elif not self.redline_flag_a and self.redline_flag_b:
            redline_note = " [clean=>redline?]"
        return (
            f"{self.family_key} v{self.version_a_id}({self.effective_date_a}) "
            f"=> v{self.version_b_id}({self.effective_date_b}): "
            + ", ".join(parts) + tier_note + redline_note
        )


def _fetch_version_row(conn: sqlite3.Connection, version_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT tv.id, tv.family_key, tv.effective_start, tv.effective_end,
               tv.revision_label, tv.supersedes_label,
               tv.historical_document_id
        FROM tariff_versions tv
        WHERE tv.id = ?
        """,
        (version_id,),
    ).fetchone()
    return dict(row) if row else None


def _fetch_charges(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT charge_label, charge_type, rate_value, rate_unit
        FROM tariff_charges
        WHERE version_id = ?
        ORDER BY charge_label
        """,
        (version_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_fingerprint_flags(
    conn: sqlite3.Connection,
    historical_document_id: int | None,
) -> tuple[bool, Optional[str]]:
    """Return (is_redline_candidate, doc_quality_tier) for a historical doc."""
    if not historical_document_id:
        return False, None
    # Get local_path from historical_documents, then match fingerprint
    row = conn.execute(
        """
        SELECT df.is_redline_candidate, df.doc_quality_tier
        FROM historical_documents hd
        JOIN document_fingerprints df ON df.source_pdf = hd.local_path
        WHERE hd.id = ?
        LIMIT 1
        """,
        (historical_document_id,),
    ).fetchone()
    if row:
        return bool(row["is_redline_candidate"]), row["doc_quality_tier"]
    return False, None


def _build_charge_map(charges: list[dict]) -> dict[str, dict]:
    """
    Build a label→charge dict.  When the same label appears multiple times
    (e.g. different customer classes), append charge_type to disambiguate.
    """
    seen: dict[str, int] = {}
    out: dict[str, dict] = {}
    for ch in charges:
        raw_label = (ch["charge_label"] or "").strip()
        charge_type = (ch["charge_type"] or "").strip()
        key = f"{raw_label}|{charge_type}" if charge_type else raw_label
        if key in seen:
            seen[key] += 1
            key = f"{key}#{seen[key]}"
        else:
            seen[key] = 0
        out[key] = ch
    return out


def compare_versions(
    conn: sqlite3.Connection,
    version_id_a: int,
    version_id_b: int,
) -> VersionCompareResult:
    """
    Compare two tariff_versions by their tariff_charges.

    version_a is treated as the "before" (older) version.
    version_b is treated as the "after" (newer) version.
    """
    ver_a = _fetch_version_row(conn, version_id_a)
    ver_b = _fetch_version_row(conn, version_id_b)
    if not ver_a:
        raise ValueError(f"tariff_version {version_id_a} not found")
    if not ver_b:
        raise ValueError(f"tariff_version {version_id_b} not found")

    family_key = ver_a["family_key"]
    charges_a = _fetch_charges(conn, version_id_a)
    charges_b = _fetch_charges(conn, version_id_b)
    map_a = _build_charge_map(charges_a)
    map_b = _build_charge_map(charges_b)

    redline_a, tier_a = _fetch_fingerprint_flags(conn, ver_a["historical_document_id"])
    redline_b, tier_b = _fetch_fingerprint_flags(conn, ver_b["historical_document_id"])

    all_labels = sorted(set(map_a) | set(map_b))
    deltas: list[RateDelta] = []
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []

    for label in all_labels:
        if label in map_a and label not in map_b:
            ch = map_a[label]
            removed.append(label)
            deltas.append(RateDelta(
                charge_label=label,
                charge_type=ch.get("charge_type") or "",
                old_value=ch.get("rate_value"),
                new_value=None,
                old_unit=ch.get("rate_unit"),
                new_unit=None,
                delta_abs=None,
                delta_pct=None,
                change_type="removed",
            ))
        elif label not in map_a and label in map_b:
            ch = map_b[label]
            added.append(label)
            deltas.append(RateDelta(
                charge_label=label,
                charge_type=ch.get("charge_type") or "",
                old_value=None,
                new_value=ch.get("rate_value"),
                old_unit=None,
                new_unit=ch.get("rate_unit"),
                delta_abs=None,
                delta_pct=None,
                change_type="added",
            ))
        else:
            ch_a = map_a[label]
            ch_b = map_b[label]
            old_v = ch_a.get("rate_value")
            new_v = ch_b.get("rate_value")
            if old_v is None and new_v is None:
                change_type = "unchanged"
            elif old_v != new_v:
                change_type = "changed"
                changed.append(label)
            else:
                change_type = "unchanged"

            delta_abs: Optional[float] = None
            delta_pct: Optional[float] = None
            if old_v is not None and new_v is not None and change_type == "changed":
                try:
                    delta_abs = round(float(new_v) - float(old_v), 6)
                    if float(old_v) != 0:
                        delta_pct = round(delta_abs / float(old_v) * 100, 2)
                except (TypeError, ValueError):
                    pass

            deltas.append(RateDelta(
                charge_label=label,
                charge_type=ch_a.get("charge_type") or "",
                old_value=old_v,
                new_value=new_v,
                old_unit=ch_a.get("rate_unit"),
                new_unit=ch_b.get("rate_unit"),
                delta_abs=delta_abs,
                delta_pct=delta_pct,
                change_type=change_type,
            ))

    # Extract leaf_no from family_key if possible
    import re
    leaf_m = re.search(r'leaf-(\d+)', family_key)
    leaf_no = leaf_m.group(1) if leaf_m else None

    return VersionCompareResult(
        family_key=family_key,
        leaf_no=leaf_no,
        version_a_id=version_id_a,
        version_b_id=version_id_b,
        effective_date_a=ver_a["effective_start"],
        effective_date_b=ver_b["effective_start"],
        revision_label_a=ver_a["revision_label"],
        revision_label_b=ver_b["revision_label"],
        redline_flag_a=redline_a,
        redline_flag_b=redline_b,
        doc_tier_a=tier_a,
        doc_tier_b=tier_b,
        rate_deltas=deltas,
        added_charges=added,
        removed_charges=removed,
        changed_charges=changed,
    )


def compare_family_latest_two(
    conn: sqlite3.Connection,
    family_key: str,
) -> Optional[VersionCompareResult]:
    """
    Find the two most recent tariff_versions for *family_key* and compare them.

    Returns None if fewer than two versions exist with tariff_charges.
    """
    rows = conn.execute(
        """
        SELECT tv.id, tv.effective_start
        FROM tariff_versions tv
        WHERE tv.family_key = ?
          AND EXISTS (SELECT 1 FROM tariff_charges tc WHERE tc.version_id = tv.id)
        ORDER BY tv.effective_start DESC
        LIMIT 2
        """,
        (family_key,),
    ).fetchall()
    if len(rows) < 2:
        return None
    # rows[0] = newer, rows[1] = older
    return compare_versions(conn, int(rows[1]["id"]), int(rows[0]["id"]))
