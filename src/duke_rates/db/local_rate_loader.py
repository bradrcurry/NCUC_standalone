from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.ncuc_loader import _normalize_date
from duke_rates.parse.nc_carolinas import parse_nc_carolinas_leaf_file
from duke_rates.parse.nc_progress import parse_nc_progress_leaf_file

_NOW = datetime.now(UTC).isoformat()


def load_local_nc_residential_rates(
    conn: sqlite3.Connection,
    *,
    company: str,
    replace: bool = True,
) -> dict[str, object]:
    company_key = company.lower()
    if company_key == "progress":
        selected = _select_rate_paths(company_key, leaf_no="500")
        schedule_code = "RES"
        family_key = "/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf"
        parser = parse_nc_progress_leaf_file
    elif company_key == "carolinas":
        selected = _select_rate_paths(company_key, leaf_no="11")
        schedule_code = "RS"
        family_key = "nc-carolinas-leaf-11"
        parser = parse_nc_carolinas_leaf_file
    else:
        raise ValueError("company must be 'progress' or 'carolinas'")

    inserted = 0
    skipped = 0
    for path in selected:
        version, charges, _ = parser(path, version_id=0, family_key=family_key, document_id=None)
        effective_date = _normalize_date(version.effective_start or "") or version.effective_start
        if not effective_date:
            skipped += 1
            continue

        energy = [_charge_to_energy_json(charge, company_key) for charge in charges if charge.charge_type in {"energy_block", "tou_energy"}]
        energy = [item for item in energy if item is not None]
        fixed = [_charge_to_fixed_json(charge) for charge in charges if charge.charge_type == "fixed"]
        fixed = [item for item in fixed if item is not None]
        demand = [_charge_to_demand_json(charge) for charge in charges if charge.charge_type == "demand"]
        demand = [item for item in demand if item is not None]
        if not energy:
            skipped += 1
            continue

        docket_dir = f"local_raw_nc_{company_key}"
        existing = conn.execute(
            """
            SELECT id FROM ncuc_ingest_segments
            WHERE source_pdf = ? AND schedule_code = ? AND effective_date IS ?
            """,
            (str(path), schedule_code, effective_date),
        ).fetchone()
        params = (
            docket_dir,
            str(path),
            "500" if company_key == "progress" else "11",
            schedule_code,
            effective_date,
            version.revision_label,
            version.supersedes_label,
            "LOCAL-RAW",
            None,
            1,
            0.97,
            "parsed",
            None,
            None,
            json.dumps(energy),
            json.dumps(fixed),
            json.dumps(demand),
            json.dumps(
                {
                    "source": "local_raw",
                    "company": company_key,
                    "family_key": family_key,
                    "version": version.model_dump(mode="json"),
                },
                sort_keys=True,
            ),
            _NOW,
        )

        if existing and replace:
            conn.execute(
                """
                UPDATE ncuc_ingest_segments SET
                    docket_dir=?, source_pdf=?, leaf_no=?, schedule_code=?, effective_date=?,
                    revision_label=?, supersedes=?, docket_number=?, order_date=?,
                    tier=?, confidence=?, status=?, page_start=?, page_end=?,
                    energy_charges_json=?, fixed_charges_json=?, demand_charges_json=?,
                    raw_segment_json=?, created_at=?
                WHERE id=?
                """,
                params + (existing["id"],),
            )
            inserted += 1
        elif existing:
            skipped += 1
        else:
            conn.execute(
                """
                INSERT INTO ncuc_ingest_segments (
                    docket_dir, source_pdf, leaf_no, schedule_code, effective_date,
                    revision_label, supersedes, docket_number, order_date,
                    tier, confidence, status, page_start, page_end,
                    energy_charges_json, fixed_charges_json, demand_charges_json,
                    raw_segment_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                params,
            )
            inserted += 1

    conn.commit()
    return {
        "company": company_key,
        "source_count": len(selected),
        "sources": [str(path) for path in selected],
        "rows_written": inserted,
        "rows_skipped": skipped,
    }


def _select_rate_paths(company: str, *, leaf_no: str) -> list[Path]:
    current_root = Path("data") / "raw" / "nc" / company / "rate"
    historical_root = Path("data") / "historical" / "raw" / "nc" / company / "rate"
    candidates: list[tuple[int, str, Path]] = []
    for root_label, root in (("historical", historical_root), ("current", current_root)):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.pdf")):
            lowered = path.name.lower()
            if company == "carolinas":
                if not (
                    f"leaf-no-{leaf_no}" in lowered
                    or "ncschedulers" in lowered
                    or lowered.startswith("rs-")
                    or "residential-service" in lowered
                ):
                    continue
            else:
                if f"leaf-no-{leaf_no}" not in lowered:
                    continue
            priority = 0 if root_label == "historical" else 1
            candidates.append((priority, lowered, path))

    chosen: dict[str, Path] = {}
    for _, lowered, path in candidates:
        effect_key = _effect_key_from_name(lowered)
        if effect_key not in chosen:
            chosen[effect_key] = path
    return list(chosen.values())


def _effect_key_from_name(name: str) -> str:
    marker = "-eff-"
    if marker in name:
        return name.split(marker, maxsplit=1)[1][:10]
    return "current"


def _charge_to_energy_json(charge, company: str) -> dict | None:
    if charge.rate_value is None:
        return None
    season = charge.season
    if company == "progress":
        if season == "summer":
            season = "May - September"
        elif season == "winter":
            season = "October - April"
        elif season == "all_year":
            season = None
    elif season == "all_year":
        season = None
    return {
        "label": charge.charge_label or "Energy Charge",
        "rate": charge.rate_value,
        "unit": charge.rate_unit or "$/kWh",
        "season": season,
        "period": charge.tou_period,
        "block_from": charge.tier_min,
        "block_to": charge.tier_max,
    }


def _charge_to_fixed_json(charge) -> dict | None:
    if charge.rate_value is None:
        return None
    return {
        "label": charge.charge_label or "Fixed Charge",
        "amount": charge.rate_value,
        "unit": charge.rate_unit or "$/month",
    }


def _charge_to_demand_json(charge) -> dict | None:
    if charge.rate_value is None:
        return None
    return {
        "label": charge.charge_label or "Demand Charge",
        "rate": charge.rate_value,
        "unit": charge.rate_unit or "$/kW",
    }
