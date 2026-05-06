"""Dork runner for locating missing historical Duke Energy Progress tariff PDFs.

Workflow
--------
1. Build per-family dork queries using the same context as search_packs.py
   (leaf no., schedule code, aliases, related dockets, predecessor utility names).
2. Execute queries via DuckDuckGo (default, free, no API key) or Google CSE
   (optional, set DUKE_RATES_GOOGLE_API_KEY + DUKE_RATES_GOOGLE_CSE_ID).
3. Parse results into HistoricalLeadRecord entries (written to `historical_leads` table).
4. Optionally auto-import any PDF hit that scores above a confidence threshold
   via ProgressNCHistoricalImportService.

Query strategies:
  - ``site:progress-energy.com`` legacy filename patterns
  - ``site:duke-energy.com`` leaf-number URL patterns
  - ``site:starw1.ncuc.gov`` docket-targeted searches
  - ``site:openei.org`` federal tariff database
  - Boilerplate phrase hunts ("Superseding Leaf No.", "Issued by", etc.)
  - Mirror searches (scribd, archive.org, google docs)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.family_targets import (
    ProgressNCFamilyTarget,
    build_progress_nc_family_targets,
)
from duke_rates.models.docket_lead import RegulatoryDocketLeadRecord
from duke_rates.models.historical_lead import HistoricalLeadRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Predecessor utility names (used in query generation)
# ---------------------------------------------------------------------------
_UTILITY_NAMES = [
    "Duke Energy Progress",
    "Progress Energy Carolinas",
    "Carolina Power & Light",
    "CP&L",
]

# ---------------------------------------------------------------------------
# Boilerplate phrases found on the face of North Carolina tariff sheets
# ---------------------------------------------------------------------------
_TARIFF_BOILERPLATE = [
    "Superseding Leaf No.",
    "Original Leaf No.",
    "Issued by",
    "North Carolina Rate Schedule",
    "Effective for service rendered",
    "NCUC Docket No. E-2",
]

# ---------------------------------------------------------------------------
# Legacy Progress Energy filename map  (code/leaf → list[filename])
# Mirrors the patterns in url_archaeology.py
# ---------------------------------------------------------------------------
_LEGACY_FILENAMES: dict[str, list[str]] = {
    "501": ["R4-NC-Schedule-Fuel-dep.pdf", "fuel-charge-adjustment.pdf", "pe-NCScheduleFuel.pdf"],
    "503": ["R2-NC-Schedule-R-TOUD-dep.pdf", "pe-NCScheduleR-TOUD.pdf"],
    "504": ["R3-NC-Schedule-R-TOU-dep.pdf", "R3-NC-Schedule-R-TOUE-dep.pdf", "pe-NCScheduleR-TOUE.pdf"],
    "571": ["S1-NC-Schedule-SLS-dep.pdf", "pe-NCScheduleSLS.pdf"],
    "572": ["S2-NC-Schedule-SLR-dep.pdf", "pe-NCScheduleSLR.pdf"],
    "602": ["RR2-NC-Rider-JAA-dep.pdf", "rider-jaa.pdf", "pe-NCRiderJAA.pdf"],
    "604": ["RR3-NC-Rider-REPS-dep.pdf", "rider-reps.pdf", "pe-NCRiderREPS.pdf"],
    "605": ["RR4-NC-Rider-REPS-EMF-dep.pdf", "rider-reps-emf.pdf", "pe-NCRiderREPS-EMF.pdf"],
    "607": ["RR5-NC-Rider-STS-dep.pdf", "storm-cost-recovery-rider.pdf", "pe-NCRiderSTS.pdf"],
    "609": ["RR2-NC-Rider-JAA-dep.pdf", "rider-jaa.pdf", "pe-NCRiderJAA.pdf"],
    "610": ["RR7-NC-Rider-EE-dep.pdf", "energy-efficiency-rider.pdf", "pe-NCRiderEE.pdf"],
    "611": ["RR6-NC-Rider-DSM-dep.pdf", "demand-side-management-rider.pdf", "pe-NCRiderDSM.pdf"],
    "613": ["RR5-NC-Rider-STS-dep.pdf", "storm-securitization-rider.pdf", "pe-NCRiderSTS.pdf"],
    "640": ["RR8-NC-Rider-CPRE-dep.pdf", "clean-power-rate-enhancement-rider.pdf", "pe-NCRiderCPRE.pdf"],
    "662": ["prepay-service-rider.pdf", "rider-prepay.pdf", "pe-NCRiderPrepay.pdf"],
    "670": ["residential-solar-choice-rider.pdf", "solar-choice-rider.pdf", "pe-NCRiderSolarChoice.pdf"],
    "672": ["clean-energy-impact-rider.pdf", "rider-cei.pdf", "pe-NCRiderCEI.pdf"],
}

# Domains we can search directly
_PROGRESS_DOMAIN = "progress-energy.com"
_DUKE_DOMAIN = "duke-energy.com"
_NCUC_DOCKET_DOMAIN = "starw1.ncuc.gov"
_OPENEI_DOMAIN = "openei.org"


# ---------------------------------------------------------------------------
# Query generation
# ---------------------------------------------------------------------------

@dataclass
class DorkQuery:
    query: str
    strategy: str          # e.g. "legacy_filename", "boilerplate", "docket_targeted"
    family_key: str
    leaf_no: str | None
    code: str | None


def build_queries_for_family(
    target: ProgressNCFamilyTarget,
    dockets: list[RegulatoryDocketLeadRecord],
) -> list[DorkQuery]:
    """Generate the full set of Google Dork queries for one tariff family."""
    queries: list[DorkQuery] = []
    leaf = target.leaf_no
    code = target.code
    title = target.title
    fk = target.family_key

    def add(query: str, strategy: str) -> None:
        if query.strip():
            queries.append(DorkQuery(query=query, strategy=strategy, family_key=fk, leaf_no=leaf, code=code))

    # ---- 1. Legacy filename hunts on progress-energy.com ----
    filenames = _LEGACY_FILENAMES.get(leaf or "", []) + _LEGACY_FILENAMES.get(code or "", [])
    seen_fnames: set[str] = set()
    for fname in filenames:
        if fname in seen_fnames:
            continue
        seen_fnames.add(fname)
        add(f'site:{_PROGRESS_DOMAIN} filetype:pdf "{fname}"', "legacy_filename")

    # Also probe path sub-patterns without quotes (catches paths that embed the stem)
    if filenames:
        stem_group = " OR ".join(f'"{fn}"' for fn in filenames[:3])
        add(f"site:{_PROGRESS_DOMAIN} filetype:pdf {stem_group}", "legacy_filename_group")

    # ---- 2. duke-energy.com leaf-number URL patterns ----
    if leaf:
        add(f'site:{_DUKE_DOMAIN} filetype:pdf "leaf-no-{leaf}"', "duke_leaf_url")
        add(f'site:{_DUKE_DOMAIN} filetype:pdf "leaf no. {leaf}"', "duke_leaf_text")
        # Cover both /electric-nc/ and /dep-nc/ path families
        for path_seg in ("electric-nc", "dep-nc"):
            add(
                f'site:{_DUKE_DOMAIN}/-/media/pdfs/for-your-home/rates/{path_seg} '
                f'filetype:pdf "leaf no. {leaf}"',
                "duke_media_path",
            )

    # ---- 3. NCUC docket-targeted searches ----
    docket_numbers = sorted({d.docket_number for d in dockets})[:5]
    if docket_numbers:
        for dn in docket_numbers:
            term = f'"{code}"' if code else f'"{title}"'
            add(f'site:{_NCUC_DOCKET_DOMAIN} filetype:pdf "{dn}" {term}', "ncuc_docket")
        # Also search ncuc.gov (public site)
        dn_group = " OR ".join(f'"{dn}"' for dn in docket_numbers[:3])
        add(f"site:ncuc.gov filetype:pdf {dn_group} {f'\"leaf no. {leaf}\"' if leaf else f'\"{ title }\"'}", "ncuc_public")

    # ---- 4. openei.org federal tariff DB ----
    for utility in _UTILITY_NAMES[:3]:
        leaf_term = f'"leaf no. {leaf}" ' if leaf else ""
        add(
            f'site:{_OPENEI_DOMAIN} filetype:pdf "{utility}" {leaf_term}"North Carolina"',
            "openei",
        )

    # ---- 5. Boilerplate phrase hunts — fanned out across each indexed domain ----
    # CSE is site-restricted so cross-domain queries without site: only hit indexed domains.
    # We explicitly fan boilerplate queries across the three most productive domains.
    _boilerplate_domains = [
        _PROGRESS_DOMAIN,
        _DUKE_DOMAIN,
        _NCUC_DOCKET_DOMAIN,
        "ncuc.gov",
        _OPENEI_DOMAIN,
        "web.archive.org",
    ]
    for boilerplate in _TARIFF_BOILERPLATE:
        if leaf:
            for domain in _boilerplate_domains:
                add(
                    f'site:{domain} filetype:pdf "{boilerplate}" "Leaf No. {leaf}"',
                    "boilerplate",
                )

    # ---- 6. Utility name + code/leaf combinations — fanned across domains ----
    _utility_domains = [_PROGRESS_DOMAIN, _DUKE_DOMAIN, _NCUC_DOCKET_DOMAIN, _OPENEI_DOMAIN]
    for domain in _utility_domains:
        for utility in _UTILITY_NAMES:
            terms: list[str] = [f'site:{domain}', f'"{utility}"']
            if leaf:
                terms.append(f'"Leaf No. {leaf}"')
            if code:
                terms.append(f'"{code}"')
            add("filetype:pdf " + " ".join(terms), "utility_name_leaf")

    # ---- 7. Title phrase hunts — fanned across domains ----
    for domain in _utility_domains:
        if leaf:
            add(f'site:{domain} filetype:pdf "{title}" "leaf no. {leaf}"', "title_phrase")
        else:
            add(f'site:{domain} filetype:pdf "{title}" "North Carolina"', "title_phrase")

    # ---- 8. Third-party mirror searches (scribd, google docs, archive.org) ----
    # These catch uploaded/mirrored copies outside the utility's own servers.
    _mirror_domains = ["scribd.com", "web.archive.org", "docs.google.com", "drive.google.com"]
    for domain in _mirror_domains:
        if leaf:
            add(
                f'site:{domain} filetype:pdf "Leaf No. {leaf}" "North Carolina"',
                "mirror",
            )
        if code:
            add(
                f'site:{domain} filetype:pdf "{code}" "{title[:40]}" "North Carolina"',
                "mirror",
            )

    # Deduplicate while preserving order
    seen_queries: set[str] = set()
    unique: list[DorkQuery] = []
    for q in queries:
        if q.query not in seen_queries:
            seen_queries.add(q.query)
            unique.append(q)
    return unique


def build_all_queries(
    repository: Repository,
    *,
    family_keys: list[str] | None = None,
    missing_only: bool = True,
) -> list[DorkQuery]:
    """Build queries for all (or a subset of) families."""
    targets = build_progress_nc_family_targets(repository, missing_only=missing_only)
    all_queries: list[DorkQuery] = []
    for leaf_no, target in targets.items():
        if family_keys and target.family_key not in family_keys:
            continue
        dockets = repository.list_regulatory_docket_leads(family_key=target.family_key)
        all_queries.extend(build_queries_for_family(target, dockets))
    return all_queries


# ---------------------------------------------------------------------------
# Result scoring
# ---------------------------------------------------------------------------

_PDF_CONFIDENCE_BASE = 55.0
_NCUC_DOMAIN_BONUS = 15.0
_DUKE_DOMAIN_BONUS = 10.0
_PROGRESS_DOMAIN_BONUS = 10.0
_LEAF_IN_URL_BONUS = 20.0
_LEAF_IN_TITLE_BONUS = 10.0
_CODE_IN_TITLE_BONUS = 8.0
_TITLE_IN_SNIPPET_BONUS = 12.0
_LEGACY_FILENAME_BONUS = 15.0


def score_search_result(
    result,
    *,
    leaf_no: str | None,
    code: str | None,
    title: str,
    strategy: str,
) -> tuple[float, list[str]]:
    score = _PDF_CONFIDENCE_BASE if result.file_format == "PDF" or result.url.lower().endswith(".pdf") else 30.0
    notes: list[str] = [f"strategy={strategy}"]

    url_lower = result.url.lower()
    title_lower = result.title.lower()
    snippet_lower = result.snippet.lower()

    if _NCUC_DOCKET_DOMAIN in result.hostname:
        score += _NCUC_DOMAIN_BONUS
        notes.append("ncuc_domain")
    elif _DUKE_DOMAIN in result.hostname:
        score += _DUKE_DOMAIN_BONUS
        notes.append("duke_domain")
    elif _PROGRESS_DOMAIN in result.hostname:
        score += _PROGRESS_DOMAIN_BONUS
        notes.append("progress_domain")

    if leaf_no and f"leaf-no-{leaf_no}" in url_lower:
        score += _LEAF_IN_URL_BONUS
        notes.append("leaf_in_url")
    if leaf_no and f"leaf no. {leaf_no}" in title_lower:
        score += _LEAF_IN_TITLE_BONUS
        notes.append("leaf_in_title")
    if code and code.lower() in title_lower:
        score += _CODE_IN_TITLE_BONUS
        notes.append("code_in_title")
    if title.lower() in snippet_lower:
        score += _TITLE_IN_SNIPPET_BONUS
        notes.append("title_in_snippet")

    # Bonus if the filename is one of the known legacy names
    fname_lower = result.filename.lower()
    for known in _LEGACY_FILENAMES.get(leaf_no or "", []) + _LEGACY_FILENAMES.get(code or "", []):
        if fname_lower == known.lower():
            score += _LEGACY_FILENAME_BONUS
            notes.append(f"legacy_filename_match:{known}")
            break

    return min(score, 100.0), notes


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------

@dataclass
class DorkRunResult:
    queries_run: int = 0
    leads_saved: int = 0
    leads_updated: int = 0
    auto_imported: int = 0
    quota_exhausted: bool = False
    errors: list[str] = field(default_factory=list)


class ProgressNCDorkRunnerService:
    """Execute dork queries via DuckDuckGo (default) or Google CSE and persist leads."""

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

    def _make_client(self):
        """Return a DDGS client, or Google CSE client if credentials are configured."""
        if self.settings.google_api_key and self.settings.google_cse_id and self.settings.google_api_key.startswith("AIza"):
            from duke_rates.external.google_cse import GoogleCseClient
            logger.info("Using Google CSE search backend")
            return GoogleCseClient(
                api_key=self.settings.google_api_key,
                cse_id=self.settings.google_cse_id,
                rate_limit_seconds=self.settings.rate_limit_seconds,
                user_agent=self.settings.user_agent,
            ), "google_cse_api"
        from duke_rates.external.ddgs_search import DdgsSearchClient
        logger.info("Using DuckDuckGo search backend (free)")
        return DdgsSearchClient(rate_limit_seconds=2.0), "ddgs"

    def run(
        self,
        *,
        family_keys: list[str] | None = None,
        missing_only: bool = True,
        strategies: list[str] | None = None,
        max_queries: int | None = None,
        max_results_per_query: int = 10,
        min_confidence_for_import: float | None = None,
        dry_run: bool = False,
    ) -> DorkRunResult:
        """Run dork queries and store leads. Optionally auto-import high-confidence PDFs."""
        client, extraction_method = self._make_client()
        result = DorkRunResult()

        # Build target map once outside the loop
        target_map = build_progress_nc_family_targets(
            self.repository, missing_only=False, state=self.state, company=self.company
        )

        try:
            queries = build_all_queries(
                self.repository,
                family_keys=family_keys,
                missing_only=missing_only,
            )
            if strategies:
                queries = [q for q in queries if q.strategy in strategies]
            if max_queries:
                queries = queries[:max_queries]

            logger.info("Running %d dork queries via %s", len(queries), extraction_method)

            for dork in queries:
                if result.quota_exhausted:
                    break
                try:
                    response = client.search_all_pages(
                        dork.query,
                        max_results=max_results_per_query,
                    )
                    result.queries_run += 1

                    if getattr(response, "quota_exhausted", False):
                        result.quota_exhausted = True
                        logger.warning("Search quota exhausted after %d queries", result.queries_run)
                        break
                    if getattr(response, "rate_limited", False):
                        logger.warning("Rate limited after %d queries — stopping", result.queries_run)
                        result.quota_exhausted = True
                        break

                    target = next(
                        (t for t in target_map.values() if t.family_key == dork.family_key),
                        None,
                    )
                    for item in response.items:
                        confidence, notes = score_search_result(
                            item,
                            leaf_no=dork.leaf_no,
                            code=dork.code,
                            title=target.title if target else dork.family_key,
                            strategy=dork.strategy,
                        )
                        lead = HistoricalLeadRecord(
                            family_key=dork.family_key,
                            target_leaf_no=dork.leaf_no,
                            target_code=dork.code,
                            target_title=target.title if target else dork.family_key,
                            family_type=target.family_type if target else "unknown",
                            category=target.category if target else "unknown",
                            source_class=extraction_method,
                            provenance_class="search_engine",
                            source_label=f"dork:{dork.strategy}",
                            source_url=item.url,
                            extracted_url=item.url,
                            extracted_title=item.title,
                            hostname=item.hostname,
                            path_fragment=item.path,
                            filename=item.filename,
                            extraction_method=extraction_method,
                            confidence_score=confidence,
                            score_notes=notes,
                            notes=[
                                f"query={dork.query[:120]}",
                                f"snippet={item.snippet[:200]}",
                            ],
                            metadata_json=json.dumps(
                                {
                                    "query": dork.query,
                                    "strategy": dork.strategy,
                                    "result_title": item.title,
                                    "result_snippet": item.snippet,
                                    "file_format": item.file_format,
                                    "backend": extraction_method,
                                },
                                sort_keys=True,
                            ),
                        )
                        if not dry_run:
                            self.repository.upsert_historical_lead(lead)
                            result.leads_saved += 1
                            logger.debug("Saved lead score=%.0f %s", confidence, item.url)
                        else:
                            result.leads_saved += 1
                            logger.info(
                                "DRY-RUN score=%.0f %-22s %s",
                                confidence, dork.strategy, item.url,
                            )

                        # Auto-import high-confidence PDFs
                        if (
                            min_confidence_for_import is not None
                            and confidence >= min_confidence_for_import
                            and (item.file_format == "PDF" or item.url.lower().endswith(".pdf"))
                            and not dry_run
                        ):
                            try:
                                self._auto_import(item, lead, target)
                                result.auto_imported += 1
                            except Exception as exc:
                                msg = f"Auto-import failed for {item.url}: {exc}"
                                logger.warning(msg)
                                result.errors.append(msg)

                except Exception as exc:
                    msg = f"Query failed: {dork.query[:80]} — {exc}"
                    logger.warning(msg)
                    result.errors.append(msg)

        finally:
            client.close()

        return result

    def _auto_import(self, item, lead: HistoricalLeadRecord, target: ProgressNCFamilyTarget | None) -> None:
        from duke_rates.historical.manual_import import ProgressNCHistoricalImportService

        importer = ProgressNCHistoricalImportService(
            self.settings, self.repository, state=self.state, company=self.company
        )
        try:
            importer.import_document(
                title=item.title or (target.title if target else lead.target_title),
                category=target.category if target else "other",
                source_label=f"dork:{lead.source_label}",
                source_authority="dork_runner",
                source_type="search_result",
                source_url=item.url,
                family_key_override=lead.family_key,
            )
        finally:
            importer.close()


# ---------------------------------------------------------------------------
# Preview helpers (no API calls needed)
# ---------------------------------------------------------------------------

def preview_queries(
    repository: Repository,
    *,
    family_keys: list[str] | None = None,
    strategies: list[str] | None = None,
    missing_only: bool = True,
) -> list[DorkQuery]:
    queries = build_all_queries(repository, family_keys=family_keys, missing_only=missing_only)
    if strategies:
        queries = [q for q in queries if q.strategy in strategies]
    return queries


# ---------------------------------------------------------------------------
# Convenience: build a flat JSON-serialisable list for export / manual use
# ---------------------------------------------------------------------------

def export_queries_json(
    repository: Repository,
    *,
    family_keys: list[str] | None = None,
    missing_only: bool = True,
) -> list[str]:
    """Return a plain list[str] of dork query strings (no duplicates)."""
    queries = build_all_queries(repository, family_keys=family_keys, missing_only=missing_only)
    seen: set[str] = set()
    result: list[str] = []
    for q in queries:
        if q.query not in seen:
            seen.add(q.query)
            result.append(q.query)
    return result
