from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.download.hashing import sha256_bytes
from duke_rates.historical.family_targets import ProgressNCFamilyTarget, find_target_by_query
from duke_rates.historical.lead_scoring import score_url_variant
from duke_rates.historical.metadata import extract_historical_metadata
from duke_rates.historical.wayback import WaybackClient, WaybackSnapshot
from duke_rates.models.document import DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.url_variant import CandidateUrlVariantRecord
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.rider_parser import parse_rider_text
from duke_rates.parse.schedule_parser import parse_schedule_text
from duke_rates.utils.files import ensure_parent
from duke_rates.utils.retry import retry_call
from duke_rates.utils.text import slugify

KNOWN_HOSTS = (
    "www.duke-energy.com",
    "duke-energy.com",
    "www.progress-energy.com",
    "progress-energy.com",
)
KNOWN_PATH_FAMILIES = (
    "/-/media/pdfs/for-your-home/rates/dep-nc/",
    "/-/media/pdfs/for-your-home/rates/electric-nc/",
    "/pdfs/",
    "/aboutenergy/rates/",
    "/assets/www/docs/home/",
    "/assets/www/docs/company/",
)

logger = logging.getLogger(__name__)


class ProgressNCUrlArchaeologyService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        *,
        state: str = "NC",
        company: str = "progress",
    ):
        self.settings = settings
        self.repository = repository
        self.state = state
        self.company = company
        self.wayback = WaybackClient(
            timeout=settings.request_timeout,
            user_agent=settings.user_agent,
        )
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
        )

    def close(self) -> None:
        self.wayback.close()
        self.client.close()

    def generate_variants_for_family(
        self,
        family_query: str,
        *,
        max_variants: int = 40,
    ) -> list[CandidateUrlVariantRecord]:
        target = find_target_by_query(self.repository, family_query, missing_only=False)
        if not target:
            raise ValueError(f"No Progress NC family matched query={family_query!r}")
        leads = self.repository.list_historical_leads(family_key=target.family_key)
        existing_variants = {
            item.variant_url: item
            for item in self.repository.list_candidate_url_variants(family_key=target.family_key)
        }
        variant_specs = _variant_specs(target, leads)
        variants: list[CandidateUrlVariantRecord] = []
        seen_urls: set[str] = set()
        evaluated = 0
        for variant_url, filename, heuristic, lead_id in variant_specs:
            if variant_url in seen_urls:
                continue
            seen_urls.add(variant_url)
            parsed = urlparse(variant_url)
            variant = existing_variants.get(variant_url) or CandidateUrlVariantRecord(
                family_key=target.family_key,
                lead_id=lead_id,
                variant_url=variant_url,
                hostname=parsed.netloc.lower(),
                path_family=_path_family_from_variant_url(parsed.path),
                filename=filename,
                heuristic=heuristic,
                notes=[f"target={target.title}", f"family_type={target.family_type}"],
                metadata_json=json.dumps(
                    {
                        "target_leaf_no": target.leaf_no,
                        "target_code": target.code,
                        "current_url": target.current_url,
                    },
                    sort_keys=True,
                ),
            )
            if variant_url not in existing_variants:
                variant = self._evaluate_variant(variant)
            self.repository.upsert_candidate_url_variant(variant)
            variants.append(variant)
            evaluated += 1
            if evaluated >= max_variants:
                break
        return sorted(
            self.repository.list_candidate_url_variants(family_key=target.family_key),
            key=lambda item: (-item.score, item.variant_url),
        )[:max_variants]

    def recover_family(
        self,
        family_query: str,
        *,
        from_year: int = 2010,
        max_variants: int = 40,
    ) -> list[HistoricalDocumentRecord]:
        target = find_target_by_query(self.repository, family_query, missing_only=False)
        if not target:
            raise ValueError(f"No Progress NC family matched query={family_query!r}")
        variants = self.generate_variants_for_family(
            family_query,
            max_variants=max_variants,
        )
        recovered: list[HistoricalDocumentRecord] = []
        for variant in variants:
            try:
                record = self._recover_variant(target, variant, from_year=from_year)
            except Exception as exc:
                logger.warning(
                    "Historical recovery failed for family=%s variant=%s: %s",
                    target.family_key,
                    variant.variant_url,
                    exc,
                )
                continue
            if record:
                recovered.append(record)
        unique: dict[int, HistoricalDocumentRecord] = {
            item.id or idx: item for idx, item in enumerate(recovered)
        }
        return list(unique.values())

    def _evaluate_variant(self, variant: CandidateUrlVariantRecord) -> CandidateUrlVariantRecord:
        direct_status = None
        direct_downloadable = False
        wayback_snapshots: list[WaybackSnapshot] = []
        try:
            response = retry_call(
                lambda: self.client.get(variant.variant_url),
                retries=max(self.settings.max_retries - 1, 0),
                delay_seconds=self.settings.rate_limit_seconds,
                retry_on=(httpx.HTTPError,),
            )
            direct_status = response.status_code
            direct_downloadable = (
                response.status_code == 200
                and (
                    response.content.startswith(b"%PDF")
                    or "pdf" in (response.headers.get("content-type") or "").lower()
                )
            )
        except httpx.HTTPError:
            direct_status = None
        try:
            wayback_snapshots = self.wayback.lookup_capture_history(
                variant.variant_url,
                from_year=2010,
                limit=6,
            )
        except httpx.HTTPError:
            wayback_snapshots = []
        if not wayback_snapshots:
            try:
                wayback_snapshots = self.wayback.lookup_snapshots(
                    variant.variant_url,
                    from_year=2010,
                    limit=6,
                    wildcard=True,
                    dedupe_originals=False,
                )
                if wayback_snapshots:
                    variant.notes.append("wayback wildcard fallback")
            except httpx.HTTPError:
                wayback_snapshots = []
        variant.direct_status_code = direct_status
        variant.direct_downloadable = direct_downloadable
        variant.wayback_snapshot_count = len(wayback_snapshots)
        variant.wayback_first_timestamp = (
            wayback_snapshots[0].timestamp if wayback_snapshots else None
        )
        score, notes = score_url_variant(variant)
        variant.score = score
        variant.notes = list(dict.fromkeys([*variant.notes, *notes]))
        return variant

    def _recover_variant(
        self,
        target: ProgressNCFamilyTarget,
        variant: CandidateUrlVariantRecord,
        *,
        from_year: int,
    ) -> HistoricalDocumentRecord | None:
        content: bytes | None = None
        content_type: str | None = None
        canonical_url = variant.variant_url
        archived_url = variant.variant_url
        snapshot_timestamp = datetime.now(UTC)
        direct_downloadable = False
        direct_status_code = None

        if variant.direct_downloadable:
            response = self.client.get(variant.variant_url)
            if response.status_code == 200 and (
                response.content.startswith(b"%PDF")
                or "pdf" in (response.headers.get("content-type") or "").lower()
            ):
                content = response.content
                content_type = response.headers.get("content-type")
                direct_downloadable = True
                direct_status_code = 200
        if content is None:
            snapshots = self.wayback.lookup_capture_history(
                variant.variant_url,
                from_year=from_year,
                limit=6,
            )
            for snapshot in snapshots:
                archived = self.client.get(snapshot.archive_url)
                if archived.status_code != 200:
                    continue
                if not (
                    archived.content.startswith(b"%PDF")
                    or "pdf" in (archived.headers.get("content-type") or "").lower()
                ):
                    continue
                content = archived.content
                content_type = archived.headers.get("content-type")
                archived_url = snapshot.archive_url
                snapshot_timestamp = datetime.strptime(snapshot.timestamp, "%Y%m%d%H%M%S").replace(
                    tzinfo=UTC
                )
                break
        if content is None:
            return None

        archive_stem = slugify(
            f"{target.title}-{variant.filename or 'candidate'}-{snapshot_timestamp.date()}"
        )
        archive_path = ensure_parent(
            self.settings.historical_dir
            / "raw"
            / self.state.lower()
            / self.company.lower()
            / target.category
            / f"{archive_stem}.pdf"
        )
        archive_path.write_bytes(content)
        raw_text = extract_pdf_text(archive_path)
        raw_text_path = archive_path.with_suffix(".pdf.txt")
        raw_text_path.write_text(raw_text, encoding="utf-8")
        metadata = extract_historical_metadata(raw_text)
        record = HistoricalDocumentRecord(
            current_document_id=target.current_document_id,
            family_key=target.family_key,
            title=target.title,
            state=self.state,
            company=self.company,
            category=target.category,
            kind=DocumentKind.PDF.value,
            canonical_url=canonical_url,
            archived_url=archived_url,
            snapshot_timestamp=snapshot_timestamp,
            local_path=archive_path,
            raw_text_path=raw_text_path,
            content_hash=sha256_bytes(content),
            content_type=content_type,
            direct_status_code=direct_status_code,
            direct_downloadable=direct_downloadable,
            revision_label=metadata.get("revision_label"),
            supersedes_label=metadata.get("supersedes_label"),
            leaf_no=metadata.get("leaf_no"),
            effective_start=metadata.get("effective_start"),
            effective_end=metadata.get("effective_end"),
            retrieved_at=datetime.now(UTC),
            metadata_json=json.dumps(
                {
                    "source_authority": (
                        "archive" if "web.archive.org" in archived_url else "utility"
                    ),
                    "source_type": "url_archaeology",
                    "heuristic": variant.heuristic,
                    "variant_url": variant.variant_url,
                },
                sort_keys=True,
            ),
            notes=[f"heuristic={variant.heuristic}", "source=url_archaeology"],
        )
        historical_id = self.repository.upsert_historical_document(record)
        if target.category == DocumentCategory.RIDER.value:
            parse_result = parse_rider_text(
                document_id=historical_id,
                title=target.title,
                state=self.state,
                company=self.company,
                text=raw_text,
                raw_text_path=raw_text_path,
            )
        else:
            parse_result = parse_schedule_text(
                document_id=historical_id,
                title=target.title,
                state=self.state,
                company=self.company,
                text=raw_text,
                raw_text_path=raw_text_path,
            )
        self.repository.save_historical_parse_result(historical_id, parse_result)
        return self.repository.get_historical_document(historical_id)


def _matching_lead_id(leads, filename: str) -> int | None:
    for lead in leads:
        if lead.filename and lead.filename.lower() == filename.lower():
            return lead.id
    return None


def _path_family_candidates(target: ProgressNCFamilyTarget) -> list[str]:
    candidates = [target.current_path.rsplit("/", 1)[0] + "/"]
    candidates.extend(KNOWN_PATH_FAMILIES)
    return list(dict.fromkeys(candidates))


def _filename_candidates(target: ProgressNCFamilyTarget, leads) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for lead in leads:
        if lead.filename:
            results.append((lead.filename, "lead_filename"))
        if lead.extracted_url:
            results.append((Path(urlparse(lead.extracted_url).path).name, "lead_url"))
    results.append((target.current_filename, "current_filename"))
    for candidate in _legacy_filename_candidates(target):
        results.append(candidate)
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for filename, heuristic in results:
        if not filename or filename in seen:
            continue
        seen.add(filename)
        deduped.append((filename, heuristic))
    return deduped


def _variant_specs(
    target: ProgressNCFamilyTarget,
    leads,
) -> list[tuple[str, str | None, str, int | None]]:
    specs: list[tuple[str, str | None, str, int | None]] = []
    seen: set[str] = set()

    # Exact lead URLs first: these are the highest-signal imported candidates.
    for lead in leads:
        if not lead.extracted_url:
            continue
        parsed = urlparse(lead.extracted_url)
        if not parsed.netloc or not parsed.path.lower().endswith(".pdf"):
            continue
        variant_url = lead.extracted_url
        if variant_url in seen:
            continue
        seen.add(variant_url)
        specs.append(
            (
                variant_url,
                Path(parsed.path).name or None,
                "lead_exact_url",
                lead.id,
            )
        )

    for filename, heuristic in _filename_candidates(target, leads):
        for host in KNOWN_HOSTS:
            for path_family in _path_family_candidates(target):
                variant_url = f"https://{host}{path_family}{filename}"
                if variant_url in seen:
                    continue
                seen.add(variant_url)
                specs.append(
                    (
                        variant_url,
                        filename,
                        heuristic,
                        _matching_lead_id(leads, filename),
                    )
                )
    return specs


def _path_family_from_variant_url(path: str) -> str:
    if "/" not in path.strip("/"):
        return "/"
    return path.rsplit("/", 1)[0] + "/"


def _legacy_filename_candidates(target: ProgressNCFamilyTarget) -> list[tuple[str, str]]:
    code = (target.code or "").upper()
    leaf = target.leaf_no or ""
    candidates: list[tuple[str, str]] = []
    if leaf and code:
        if target.category == "rider":
            candidates.append((f"leaf-no-{leaf}-rider-{code.lower()}.pdf", "leaf_rider"))
            candidates.append(
                (
                    f"leaf-no-{leaf}-rider-{code.lower()}-ry1.pdf",
                    "leaf_rider_revision",
                )
            )
        else:
            candidates.append((f"leaf-no-{leaf}-schedule-{code.lower()}.pdf", "leaf_schedule"))
            candidates.append(
                (
                    f"leaf-no-{leaf}-schedule-{code.lower()}-ry1.pdf",
                    "leaf_schedule_revision",
                )
            )
    legacy_map = {
        "RES": ["R1-NC-Schedule-RES-dep.pdf", "pe-NCScheduleRES.pdf"],
        "R-TOUD": [
            "R2-NC-Schedule-R-TOUD-dep.pdf",
            "pe-NCScheduleR-TOUD.pdf",
            "r2ncschedulertouddep.pdf",
        ],
        "R-TOU": [
            "R3-NC-Schedule-R-TOU-dep.pdf",
            "R3-NC-Schedule-R-TOUE-dep.pdf",
            "pe-NCScheduleR-TOUE.pdf",
            "r3ncschedulertoudep.pdf",
        ],
        "BA": ["RR1-NC-Rider-BA-dep.pdf"],
        "SLS": [
            "street-lighting-service-sls.pdf",
            "S1-NC-Schedule-SLS-dep.pdf",
            "pe-NCScheduleSLS.pdf",
        ],
        "SLR": [
            "street-lighting-service-residential-subdivisions-slr.pdf",
            "S2-NC-Schedule-SLR-dep.pdf",
            "pe-NCScheduleSLR.pdf",
        ],
        # Fuel Charge Adjustment (Leaf 501) — published annually on duke-energy.com
        "BA-FUEL": [
            "R4-NC-Schedule-Fuel-dep.pdf",
            "fuel-charge-adjustment.pdf",
            "pe-NCScheduleFuel.pdf",
        ],
        "501": [
            "leaf-no-501-schedule-fuel.pdf",
            "R4-NC-Schedule-Fuel-dep.pdf",
            "fuel-charge-adjustment.pdf",
            "pe-NCScheduleFuel.pdf",
            "fuel-charge-adjustment-rider.pdf",
        ],
        # Residential Time-of-Use Demand (Leaf 503)
        "503": [
            "R2-NC-Schedule-R-TOUD-dep.pdf",
            "pe-NCScheduleR-TOUD.pdf",
            "r2ncschedulertouddep.pdf",
        ],
        # Residential Time-of-Use Energy (Leaf 504)
        "504": [
            "R3-NC-Schedule-R-TOU-dep.pdf",
            "R3-NC-Schedule-R-TOUE-dep.pdf",
            "pe-NCScheduleR-TOUE.pdf",
            "r3ncschedulertoudep.pdf",
        ],
        # Street Lighting Service (Leaf 571)
        "571": [
            "street-lighting-service-sls.pdf",
            "S1-NC-Schedule-SLS-dep.pdf",
            "pe-NCScheduleSLS.pdf",
        ],
        # Street Lighting Residential Subdivisions (Leaf 572)
        "572": [
            "street-lighting-service-residential-subdivisions-slr.pdf",
            "S2-NC-Schedule-SLR-dep.pdf",
            "pe-NCScheduleSLR.pdf",
        ],
        # Joint Agency Asset Rider (Leaf 602 / 609)
        "JAA": [
            "RR2-NC-Rider-JAA-dep.pdf",
            "rider-jaa.pdf",
            "joint-agency-asset-rider.pdf",
            "pe-NCRiderJAA.pdf",
        ],
        "602": [
            "RR2-NC-Rider-JAA-dep.pdf",
            "rider-jaa.pdf",
            "joint-agency-asset-rider.pdf",
            "pe-NCRiderJAA.pdf",
        ],
        "609": [
            "RR2-NC-Rider-JAA-dep.pdf",
            "rider-jaa.pdf",
            "joint-agency-asset-rider.pdf",
            "pe-NCRiderJAA.pdf",
        ],
        # REPS Rider (Leaf 604)
        "REPS": [
            "RR3-NC-Rider-REPS-dep.pdf",
            "rider-reps.pdf",
            "renewable-energy-portfolio-standard-rider.pdf",
            "pe-NCRiderREPS.pdf",
        ],
        "604": [
            "RR3-NC-Rider-REPS-dep.pdf",
            "rider-reps.pdf",
            "pe-NCRiderREPS.pdf",
        ],
        # REPS EMF Rider (Leaf 605)
        "REPS-EMF": [
            "RR4-NC-Rider-REPS-EMF-dep.pdf",
            "rider-reps-emf.pdf",
            "reps-emf-rider.pdf",
            "pe-NCRiderREPS-EMF.pdf",
        ],
        "605": [
            "RR4-NC-Rider-REPS-EMF-dep.pdf",
            "rider-reps-emf.pdf",
            "pe-NCRiderREPS-EMF.pdf",
        ],
        # Storm Cost Recovery / Storm Securitization (Leaf 607 / 613)
        "STS": [
            "RR5-NC-Rider-STS-dep.pdf",
            "storm-securitization-rider.pdf",
            "storm-cost-recovery-rider.pdf",
            "rider-sts.pdf",
            "pe-NCRiderSTS.pdf",
        ],
        "607": [
            "RR5-NC-Rider-STS-dep.pdf",
            "storm-cost-recovery-rider.pdf",
            "storm-securitization-rider.pdf",
            "rider-sts.pdf",
            "pe-NCRiderSTS.pdf",
        ],
        "613": [
            "RR5-NC-Rider-STS-dep.pdf",
            "storm-securitization-rider.pdf",
            "rider-sts.pdf",
            "pe-NCRiderSTS.pdf",
        ],
        # DSM Rider (Leaf 611)
        "DSM": [
            "RR6-NC-Rider-DSM-dep.pdf",
            "demand-side-management-rider.pdf",
            "rider-dsm.pdf",
            "pe-NCRiderDSM.pdf",
        ],
        "611": [
            "RR6-NC-Rider-DSM-dep.pdf",
            "demand-side-management-rider.pdf",
            "rider-dsm.pdf",
            "pe-NCRiderDSM.pdf",
        ],
        # Energy Efficiency Rider (Leaf 610)
        "EE": [
            "RR7-NC-Rider-EE-dep.pdf",
            "energy-efficiency-rider.pdf",
            "rider-ee.pdf",
            "pe-NCRiderEE.pdf",
        ],
        "610": [
            "RR7-NC-Rider-EE-dep.pdf",
            "energy-efficiency-rider.pdf",
            "rider-ee.pdf",
            "pe-NCRiderEE.pdf",
        ],
        # CPRE Rider (Leaf 640)
        "CPRE": [
            "RR8-NC-Rider-CPRE-dep.pdf",
            "clean-power-rate-enhancement-rider.pdf",
            "rider-cpre.pdf",
            "pe-NCRiderCPRE.pdf",
        ],
        "640": [
            "RR8-NC-Rider-CPRE-dep.pdf",
            "clean-power-rate-enhancement-rider.pdf",
            "rider-cpre.pdf",
            "pe-NCRiderCPRE.pdf",
        ],
        # Prepay Service Rider (Leaf 662)
        "PREPAY": [
            "prepay-service-rider.pdf",
            "rider-prepay.pdf",
            "prepay-rider.pdf",
            "pe-NCRiderPrepay.pdf",
        ],
        "662": [
            "prepay-service-rider.pdf",
            "rider-prepay.pdf",
            "prepay-rider.pdf",
            "pe-NCRiderPrepay.pdf",
        ],
        # Solar Choice Rider (Leaf 670)
        "SOLAR": [
            "residential-solar-choice-rider.pdf",
            "solar-choice-rider.pdf",
            "rider-solar-choice.pdf",
            "rider-solar.pdf",
            "pe-NCRiderSolarChoice.pdf",
        ],
        "670": [
            "residential-solar-choice-rider.pdf",
            "solar-choice-rider.pdf",
            "rider-solar-choice.pdf",
            "rider-solar.pdf",
            "pe-NCRiderSolarChoice.pdf",
        ],
        # Clean Energy Impact Rider (Leaf 672)
        "CEI": [
            "clean-energy-impact-rider.pdf",
            "rider-cei.pdf",
            "pe-NCRiderCEI.pdf",
        ],
        "672": [
            "clean-energy-impact-rider.pdf",
            "rider-cei.pdf",
            "pe-NCRiderCEI.pdf",
        ],
    }
    for key in (code, leaf):
        for filename in legacy_map.get(key, []):
            candidates.append((filename, "legacy_code_map"))
    if code and target.category == "rider":
        candidates.append((f"rider-{code.lower()}.pdf", "generic_rider_code"))
    return candidates
