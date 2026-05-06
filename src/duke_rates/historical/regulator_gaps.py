from __future__ import annotations

from duke_rates.db.repository import Repository
from duke_rates.historical.lineage import ProgressNCLineageService
from duke_rates.historical.notice_links import ProgressNCNoticeLinkService
from duke_rates.historical.provenance import ProgressNCProvenanceService
from duke_rates.models.document import DocumentCategory
from duke_rates.models.notice_link import NoticeLinkRecord
from duke_rates.models.regulator_gap import RegulatorGapHint, RegulatorGapRecord


class ProgressNCRegulatorGapService:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.lineage = ProgressNCLineageService(repository)
        self.provenance = ProgressNCProvenanceService(repository)
        self.notice_links = ProgressNCNoticeLinkService(repository)

    def build_gaps(self, *, query: str | None = None) -> list[RegulatorGapRecord]:
        coverage_by_family = {
            item.family_key: item for item in self.provenance.build_chain_coverage(query=query)
        }
        linked_notices = self.notice_links.build_links()

        gaps: list[RegulatorGapRecord] = []
        for chain in self.lineage.build_chains(query=query, recovered_only=False):
            if chain.category not in {DocumentCategory.RATE.value, DocumentCategory.RIDER.value}:
                continue
            if "/rates/dep-nc/leaf-no-" not in chain.family_key:
                continue

            coverage = coverage_by_family.get(chain.family_key)
            authorities = coverage.authorities if coverage else []
            source_types = coverage.source_types if coverage else []
            if "regulator" in authorities:
                continue

            hints = _build_hints(chain.family_key, linked_notices)
            suggested_dockets = sorted(
                {docket for hint in hints for docket in hint.docket_numbers if docket}
            )
            priority, reason = _gap_priority(
                len(chain.versions),
                authorities,
                suggested_dockets,
            )

            gaps.append(
                RegulatorGapRecord(
                    family_key=chain.family_key,
                    title=chain.title,
                    leaf_no=chain.leaf_no,
                    category=chain.category,
                    version_count=len(chain.versions),
                    evidence_authorities=authorities,
                    evidence_source_types=source_types,
                    gap_priority=priority,
                    reason=reason,
                    suggested_dockets=suggested_dockets,
                    hints=hints,
                )
            )

        gaps.sort(
            key=lambda item: (
                -item.gap_priority,
                item.title.lower(),
                item.leaf_no or "",
            )
        )
        return gaps


def _build_hints(family_key: str, linked_notices: list[NoticeLinkRecord]) -> list[RegulatorGapHint]:
    hints: list[RegulatorGapHint] = []
    for notice in linked_notices:
        matches = [match for match in notice.matches if match.family_key == family_key]
        if not matches:
            continue
        hints.append(
            RegulatorGapHint(
                title=notice.title,
                docket_numbers=notice.docket_numbers,
                basis=sorted({match.basis for match in matches}),
            )
        )
    hints.sort(key=lambda item: (item.title.lower(), ",".join(item.docket_numbers)))
    return hints


def _gap_priority(
    version_count: int,
    authorities: list[str],
    suggested_dockets: list[str],
) -> tuple[int, str]:
    if not authorities:
        return (
            3,
            "No historical source evidence is cataloged for this chain.",
        )
    if authorities == ["archive"]:
        return (
            3 if suggested_dockets else 2,
            (
                "Only archive evidence is available; regulator evidence would "
                "materially strengthen provenance."
            ),
        )
    if "utility" in authorities and not suggested_dockets:
        return (
            1,
            "Only utility-hosted evidence is available and no regulator docket hint is linked yet.",
        )
    if "utility" in authorities:
        return (
            2,
            (
                "Only utility-hosted evidence is available; linked dockets "
                "suggest regulator materials may exist."
            ),
        )
    return (
        1,
        "Regulator evidence is missing for this chain.",
    )
