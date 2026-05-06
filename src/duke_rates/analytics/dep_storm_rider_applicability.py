from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.models.tariff import RiderApplicabilityRecord


@dataclass(frozen=True)
class StormRiderSeedSpec:
    rider_family_key: str
    applies_to_family_keys: tuple[str, ...]
    applicability_notes: str


_STORM_RIDERS: tuple[StormRiderSeedSpec, ...] = (
    StormRiderSeedSpec(
        rider_family_key="nc-progress-leaf-607",
        applies_to_family_keys=(
            "nc-progress-leaf-500",
            "nc-progress-leaf-501",
            "nc-progress-leaf-502",
            "nc-progress-leaf-503",
            "nc-progress-leaf-504",
        ),
        applicability_notes=(
            "Seeded from DEP Storm Securitization Rider STS applicability text. "
            "Leaf 607 currently lists RES, R-TOUD, R-TOU, R-TOU-CPP, and R-TOU-EV."
        ),
    ),
    StormRiderSeedSpec(
        rider_family_key="nc-progress-leaf-613",
        applies_to_family_keys=(
            "nc-progress-leaf-500",
            "nc-progress-leaf-501",
            "nc-progress-leaf-502",
            "nc-progress-leaf-503",
        ),
        applicability_notes=(
            "Seeded from DEP Storm Securitization Rider STS-2 applicability text. "
            "Leaf 613 currently lists RES, R-TOUD, R-TOU, and R-TOU-CPP."
        ),
    ),
)


def seed_dep_storm_rider_applicability(
    database_path: Path | None = None,
) -> dict[str, Any]:
    repo = Repository(str(database_path or get_settings().database_path))
    existing_pairs: set[tuple[str, str, str | None]] = set()
    for spec in _STORM_RIDERS:
        for schedule_key in spec.applies_to_family_keys:
            for link in repo.list_rider_applicability(applies_to_family_key=schedule_key):
                existing_pairs.add((link.rider_family_key, link.applies_to_family_key, link.effective_start))

    inserted = 0
    skipped = 0
    seeded_rows: list[dict[str, Any]] = []
    for spec in _STORM_RIDERS:
        for schedule_key in spec.applies_to_family_keys:
            pair = (spec.rider_family_key, schedule_key, None)
            if pair in existing_pairs:
                skipped += 1
                continue
            repo.upsert_rider_applicability(
                RiderApplicabilityRecord(
                    rider_family_key=spec.rider_family_key,
                    applies_to_family_key=schedule_key,
                    mandatory=True,
                    enrollment_type="mandatory",
                    in_rider_summary=True,
                    applicability_notes=spec.applicability_notes,
                    source_type="manual",
                    confidence_score=0.95,
                )
            )
            existing_pairs.add(pair)
            inserted += 1
            seeded_rows.append(
                {
                    "rider_family_key": spec.rider_family_key,
                    "applies_to_family_key": schedule_key,
                    "mandatory": True,
                    "enrollment_type": "mandatory",
                    "in_rider_summary": True,
                }
            )

    return {
        "inserted": inserted,
        "skipped": skipped,
        "seeded_rows": seeded_rows,
        "storm_rider_count": len(_STORM_RIDERS),
    }


__all__ = [
    "seed_dep_storm_rider_applicability",
]
