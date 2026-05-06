from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.models.tariff import RiderApplicabilityRecord

_RESIDENTIAL_SCHEDULES = (
    "nc-progress-leaf-500",
    "nc-progress-leaf-501",
    "nc-progress-leaf-502",
    "nc-progress-leaf-503",
    "nc-progress-leaf-504",
)


@dataclass(frozen=True)
class RiderSeedSpec:
    rider_family_key: str
    mandatory: bool
    enrollment_type: str
    in_rider_summary: bool
    applicability_notes: str


_MANDATORY_RIDERS: tuple[RiderSeedSpec, ...] = (
    RiderSeedSpec(
        "nc-progress-leaf-601",
        mandatory=True,
        enrollment_type="mandatory",
        in_rider_summary=True,
        applicability_notes=(
            "Seeded from DEP residential rider model. Leaf 601 represents annual billing "
            "adjustment factors used in the residential rider summary (BA-DSM / BA-EMF / "
            "BA-Fuel / legacy RAL-2 components)."
        ),
    ),
    RiderSeedSpec(
        "nc-progress-leaf-602",
        mandatory=True,
        enrollment_type="mandatory",
        in_rider_summary=True,
        applicability_notes="Seeded from DEP residential rider model (JAA).",
    ),
    RiderSeedSpec(
        "nc-progress-leaf-604",
        mandatory=True,
        enrollment_type="mandatory",
        in_rider_summary=True,
        applicability_notes="Seeded from DEP residential rider model (EDIT-4).",
    ),
    RiderSeedSpec(
        "nc-progress-leaf-605",
        mandatory=True,
        enrollment_type="mandatory",
        in_rider_summary=True,
        applicability_notes="Seeded from DEP residential rider model (CPRE).",
    ),
    RiderSeedSpec(
        "nc-progress-leaf-608",
        mandatory=True,
        enrollment_type="mandatory",
        in_rider_summary=True,
        applicability_notes="Seeded from DEP residential rider model (RDM).",
    ),
    RiderSeedSpec(
        "nc-progress-leaf-609",
        mandatory=True,
        enrollment_type="mandatory",
        in_rider_summary=True,
        applicability_notes="Seeded from DEP residential rider model (ESM).",
    ),
    RiderSeedSpec(
        "nc-progress-leaf-610",
        mandatory=True,
        enrollment_type="mandatory",
        in_rider_summary=True,
        applicability_notes="Seeded from DEP residential rider model (PIM).",
    ),
    RiderSeedSpec(
        "nc-progress-leaf-611",
        mandatory=True,
        enrollment_type="mandatory",
        in_rider_summary=True,
        applicability_notes="Seeded from DEP residential rider model (CAR).",
    ),
)


def seed_dep_residential_rider_applicability(
    database_path: Path | None = None,
) -> dict[str, Any]:
    repo = Repository(str(database_path or get_settings().database_path))
    existing_pairs: set[tuple[str, str, str | None]] = set()
    for schedule_key in _RESIDENTIAL_SCHEDULES:
        for link in repo.list_rider_applicability(applies_to_family_key=schedule_key):
            existing_pairs.add((link.rider_family_key, link.applies_to_family_key, link.effective_start))

    inserted = 0
    skipped = 0
    seeded_rows: list[dict[str, Any]] = []
    for schedule_key in _RESIDENTIAL_SCHEDULES:
        for rider in _MANDATORY_RIDERS:
            pair = (rider.rider_family_key, schedule_key, None)
            if pair in existing_pairs:
                skipped += 1
                continue
            repo.upsert_rider_applicability(
                RiderApplicabilityRecord(
                    rider_family_key=rider.rider_family_key,
                    applies_to_family_key=schedule_key,
                    mandatory=rider.mandatory,
                    enrollment_type=rider.enrollment_type,
                    in_rider_summary=rider.in_rider_summary,
                    applicability_notes=rider.applicability_notes,
                    source_type="manual",
                    confidence_score=0.95,
                )
            )
            inserted += 1
            existing_pairs.add(pair)
            seeded_rows.append(
                {
                    "rider_family_key": rider.rider_family_key,
                    "applies_to_family_key": schedule_key,
                    "mandatory": rider.mandatory,
                    "enrollment_type": rider.enrollment_type,
                    "in_rider_summary": rider.in_rider_summary,
                }
            )

    return {
        "inserted": inserted,
        "skipped": skipped,
        "seeded_rows": seeded_rows,
        "schedule_count": len(_RESIDENTIAL_SCHEDULES),
        "rider_count": len(_MANDATORY_RIDERS),
    }


__all__ = [
    "seed_dep_residential_rider_applicability",
]
