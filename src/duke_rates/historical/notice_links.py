from __future__ import annotations

from duke_rates.db.repository import Repository
from duke_rates.historical.lineage import ProgressNCLineageService
from duke_rates.models.document import DocumentCategory
from duke_rates.models.notice_link import NoticeLinkMatch, NoticeLinkRecord
from duke_rates.models.parse_result import DocumentParseResult


class ProgressNCNoticeLinkService:
    def __init__(
        self,
        repository: Repository,
        *,
        state: str = "NC",
        company: str = "progress",
    ):
        self.repository = repository
        self.state = state
        self.company = company

    def build_links(self) -> list[NoticeLinkRecord]:
        chains = [
            chain
            for chain in ProgressNCLineageService(
                self.repository, state=self.state, company=self.company
            ).build_chains(recovered_only=False)
            if chain.category in {DocumentCategory.RATE.value, DocumentCategory.RIDER.value}
            and "/rates/dep-nc/leaf-no-" in chain.family_key
        ]
        links: list[NoticeLinkRecord] = []
        for row in self.repository.list_historical_documents(state=self.state, company=self.company):
            if row.category not in {
                DocumentCategory.PUBLIC_NOTICE.value,
                DocumentCategory.OTHER.value,
                DocumentCategory.RIDER.value,
            }:
                continue
            if not row.parsed_result_json:
                continue
            parse_result = DocumentParseResult.model_validate_json(row.parsed_result_json)
            if not parse_result.notice:
                continue
            matches = _match_notice(parse_result.notice, chains)
            if not matches:
                continue
            links.append(
                NoticeLinkRecord(
                    historical_id=row.id or 0,
                    title=row.title,
                    docket_numbers=parse_result.notice.docket_numbers,
                    related_rider_codes=parse_result.notice.related_rider_codes,
                    related_schedule_codes=parse_result.notice.related_schedule_codes,
                    matches=matches,
                )
            )
        links.sort(key=lambda item: (item.title.lower(), item.historical_id))
        return links


def _match_notice(notice, chains) -> list[NoticeLinkMatch]:
    matches: list[NoticeLinkMatch] = []
    seen: set[tuple[str, str]] = set()
    rider_codes = {code.upper() for code in notice.related_rider_codes}
    schedule_codes = {code.upper() for code in notice.related_schedule_codes}
    for chain in chains:
        chain_schedule_codes = {
            (version.schedule_code or "").upper()
            for version in chain.versions
            if version.schedule_code
        }
        chain_rider_codes = set()
        for version in chain.versions:
            if version.rider_id:
                chain_rider_codes.add(version.rider_id.upper())
            if "rider-" in version.family_key:
                token = version.family_key.split("rider-", maxsplit=1)[1].split(".", maxsplit=1)[0]
                chain_rider_codes.add(token.replace("-ry1", "").replace("-ry", "").upper())

        basis: str | None = None
        if rider_codes & chain_rider_codes:
            basis = f"rider:{','.join(sorted(rider_codes & chain_rider_codes))}"
        elif schedule_codes & chain_schedule_codes:
            basis = f"schedule:{','.join(sorted(schedule_codes & chain_schedule_codes))}"
        if basis and (chain.family_key, basis) not in seen:
            seen.add((chain.family_key, basis))
            matches.append(
                NoticeLinkMatch(
                    family_key=chain.family_key,
                    title=chain.title,
                    category=chain.category,
                    basis=basis,
                )
            )
    return matches
