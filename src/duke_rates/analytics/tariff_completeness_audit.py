"""Tariff completeness audit service.

Provides systematic coverage checks for tariff families and rider applicability:

- ``build_temporal_map(family_key)`` — full version timeline for one family,
  including date gaps and supersession chain reconstruction.

- ``build_coverage_map(schedule_key, as_of_date)`` — point-in-time rider coverage
  for one rate schedule: which riders are expected, which have charges, which are
  missing, and how the engine total compares to the leaf-600 authoritative summary.

- ``build_null_audit(state, company, as_of_date)`` — batch form of
  ``build_coverage_map`` over all rate schedules for a state/company.

- ``build_rider_map(state, company)`` — static (undated) map of which riders
  apply to which schedules, with in_rider_summary and enrollment_type metadata.
"""

from __future__ import annotations

import datetime
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from duke_rates.billing.tariff_engine import (
    _RIDER_SUMMARY_FAMILY,
    _RIDER_TOTAL_TOLERANCE,
    _get_rider_summary_total,
    _select_version,
)
from duke_rates.models.audit import (
    FamilyTemporalMap,
    RiderCoverageEntry,
    TariffCoverageMap,
    TariffSearchWorkItem,
    VersionGap,
    VersionTimelineEntry,
)

if TYPE_CHECKING:
    from duke_rates.db.repository import Repository

log = logging.getLogger(__name__)


class TariffCompletenessAuditService:
    """Completeness and coverage auditor for tariff families."""

    def __init__(self, repo: "Repository") -> None:
        self._repo = repo

    # ------------------------------------------------------------------
    # Temporal map — timeline, gaps, supersession chain
    # ------------------------------------------------------------------

    def build_temporal_map(self, family_key: str) -> FamilyTemporalMap:
        """Build a full timeline analysis for one tariff family.

        Returns version entries sorted by effective_start, detected date gaps,
        the supersession chain (if reconstruction is possible), and a status
        verdict: "complete" | "gaps_exist" | "undated" | "empty".
        """
        family = self._repo.get_tariff_family(family_key)
        if family is None:
            return FamilyTemporalMap(
                family_key=family_key,
                family_type="unknown",
                title=None,
                timeline_status="empty",
            )

        raw_versions = self._repo.list_tariff_versions(family_key)
        if not raw_versions:
            return FamilyTemporalMap(
                family_key=family_key,
                family_type=family.family_type,
                title=family.title,
                timeline_status="empty",
            )

        # Enrich each version with charge statistics from the DB view
        entries = self._enrich_versions(raw_versions)

        # Split into dated and undated buckets
        dated = sorted(
            [e for e in entries if e.effective_start],
            key=lambda e: e.effective_start,  # type: ignore[arg-type]
        )
        undated = [e for e in entries if not e.effective_start]

        gaps: list[VersionGap] = []

        # Flag undated versions
        for e in undated:
            gaps.append(
                VersionGap(
                    family_key=family_key,
                    gap_start=None,
                    gap_end=None,
                    gap_days=None,
                    predecessor_version_id=None,
                    successor_version_id=e.version_id,
                    gap_type="undated_version",
                )
            )

        # Detect gaps between consecutive dated versions
        for i in range(len(dated) - 1):
            a, b = dated[i], dated[i + 1]
            if a.effective_end is None:
                # Open-ended predecessor — assume contiguous (common for current versions)
                pass
            elif a.effective_end < b.effective_start:  # type: ignore[operator]
                try:
                    end_dt = datetime.date.fromisoformat(a.effective_end)
                    start_dt = datetime.date.fromisoformat(b.effective_start)  # type: ignore[arg-type]
                    # gap_days = uncovered days between end (inclusive) and start (inclusive).
                    # e.g. end=Jun-30, start=Jul-01 → (Jul-01 - Jun-30).days = 1 → gap_days=0 (contiguous)
                    gap_days = (start_dt - end_dt).days - 1
                except ValueError:
                    gap_days = None
                # A gap_days of 0 means the dates are contiguous (end+1==start); not a true gap.
                if gap_days is not None and gap_days <= 0:
                    pass
                else:
                    gaps.append(
                        VersionGap(
                            family_key=family_key,
                            gap_start=a.effective_end,
                            gap_end=b.effective_start,
                            gap_days=gap_days,
                            predecessor_version_id=a.version_id,
                            successor_version_id=b.version_id,
                            gap_type="between_versions",
                        )
                    )

        # Flag if earliest dated version has no known start (shouldn't happen but guard)
        if dated and dated[0].effective_start is None:
            gaps.append(
                VersionGap(
                    family_key=family_key,
                    gap_start=None,
                    gap_end=dated[0].effective_start,
                    gap_days=None,
                    predecessor_version_id=None,
                    successor_version_id=dated[0].version_id,
                    gap_type="open_start",
                )
            )

        # Reconstruct supersession chain
        chain, orphans = _build_supersession_chain(dated + undated)

        # Timeline status
        if not dated and undated:
            status = "undated"
        elif gaps and any(g.gap_type != "undated_version" for g in gaps):
            status = "gaps_exist"
        elif not gaps:
            status = "complete"
        else:
            status = "undated"

        return FamilyTemporalMap(
            family_key=family_key,
            family_type=family.family_type,
            title=family.title,
            versions=dated + undated,
            gaps=gaps,
            supersession_chain=chain,
            orphaned_revisions=orphans,
            timeline_status=status,
        )

    # ------------------------------------------------------------------
    # Coverage map — point-in-time rider completeness for one schedule
    # ------------------------------------------------------------------

    def build_coverage_map(
        self,
        schedule_family_key: str,
        as_of_date: datetime.date,
        customer_class: str = "residential",
    ) -> TariffCoverageMap:
        """Audit coverage for one rate schedule at a given date.

        Resolves the schedule version, then for every rider in
        rider_applicability checks whether the rider has a version with
        non-null charges for that date.  Also cross-checks the engine's
        per-kWh summary total against the leaf-600 authoritative total.
        """
        family = self._repo.get_tariff_family(schedule_family_key)
        date_str = str(as_of_date)

        # --- Schedule version ---
        sched_versions = self._repo.list_tariff_versions(schedule_family_key)
        sched_version = _select_version(sched_versions, as_of_date)

        if sched_version is None:
            return TariffCoverageMap(
                as_of_date=date_str,
                schedule_family_key=schedule_family_key,
                schedule_title=family.title if family else None,
                schedule_version_id=None,
                schedule_revision_label=None,
                schedule_charge_status="no_version",
                audit_verdict="no_data",
                warnings=[f"No tariff_version found for {schedule_family_key} as of {date_str}"],
            )

        sched_entries = self._enrich_versions([sched_version])
        sched_entry = sched_entries[0]

        # --- Rider links ---
        rider_links = self._repo.list_rider_applicability(
            applies_to_family_key=schedule_family_key
        )

        # Filter to active links for the date
        active_links = [
            lnk for lnk in rider_links
            if (lnk.effective_start is None or lnk.effective_start <= date_str)
            and (lnk.effective_end is None or lnk.effective_end >= date_str)
        ]

        rider_entries: list[RiderCoverageEntry] = []
        engine_summary_total = 0.0

        for link in active_links:
            rider_fam = self._repo.get_tariff_family(link.rider_family_key)
            rider_versions = self._repo.list_tariff_versions(link.rider_family_key)
            rider_ver = _select_version(rider_versions, as_of_date)

            if rider_ver is None:
                rider_entries.append(
                    RiderCoverageEntry(
                        rider_family_key=link.rider_family_key,
                        rider_title=rider_fam.title if rider_fam else None,
                        applies_to_family_key=schedule_family_key,
                        mandatory=link.mandatory,
                        in_rider_summary=link.in_rider_summary,
                        enrollment_type=link.enrollment_type,
                        coverage_status="no_version",
                        notes=f"No version for {link.rider_family_key} as of {date_str}",
                    )
                )
                continue

            enriched = self._enrich_versions([rider_ver])[0]

            # Sum $/kWh adjustment charges for the customer class
            rate_total = self._sum_per_kwh_rate(rider_ver.id, customer_class)

            if link.in_rider_summary and rate_total is not None:
                engine_summary_total += rate_total

            rider_entries.append(
                RiderCoverageEntry(
                    rider_family_key=link.rider_family_key,
                    rider_title=rider_fam.title if rider_fam else None,
                    applies_to_family_key=schedule_family_key,
                    mandatory=link.mandatory,
                    in_rider_summary=link.in_rider_summary,
                    enrollment_type=link.enrollment_type,
                    rider_version_id=rider_ver.id,
                    rider_effective_start=rider_ver.effective_start,
                    rider_effective_end=rider_ver.effective_end,
                    rider_charge_count=enriched.charge_count,
                    rider_null_rate_count=enriched.null_rate_count,
                    rate_cents_per_kwh=round(rate_total * 100, 6) if rate_total is not None else None,
                    coverage_status=enriched.charge_status,
                )
            )

        # --- Leaf-600 cross-check ---
        # The leaf-600 Summary of Rider Adjustments is only meaningful for schedules that
        # share the same rider set as the reference class used in the leaf-600 PDF.
        # We determine eligibility by checking whether the schedule includes RDM (leaf-608),
        # which is residential-only and appears in the leaf-600 summary.  Schedules without
        # RDM (commercial, industrial, lighting) use different per-class rider rates and
        # should not be cross-checked against the residential leaf-600 total.
        state = family.state.lower() if family else ""
        company = family.company.lower() if family else ""
        has_rdm = any(
            lnk.rider_family_key.endswith("-leaf-608") for lnk in active_links
        )
        if customer_class == "residential" and has_rdm:
            leaf600_total_raw = _get_rider_summary_total(
                self._repo, state, company, as_of_date, customer_class
            )
        else:
            leaf600_total_raw = None
        leaf600_cents = round(leaf600_total_raw * 100, 6) if leaf600_total_raw is not None else None
        engine_cents = round(engine_summary_total * 100, 6)

        delta = None
        within_tol = None
        if leaf600_cents is not None:
            delta = round(abs(engine_cents - leaf600_cents), 6)
            within_tol = delta <= (_RIDER_TOTAL_TOLERANCE * 100)

        # --- Verdict ---
        warnings: list[str] = []
        # Only flag riders that are expected to have charges (in_rider_summary or mandatory).
        # Enrollment programs (opt_in/conditional, not in summary) may legitimately have no charges.
        missing = [
            r for r in rider_entries
            if r.coverage_status != "ok" and (r.in_rider_summary or r.mandatory)
        ]
        informational = [
            r for r in rider_entries
            if r.coverage_status != "ok" and not r.in_rider_summary and not r.mandatory
        ]
        if missing:
            for r in missing:
                warnings.append(
                    f"Rider {r.rider_family_key} ({r.rider_title}): {r.coverage_status}"
                )
        if informational:
            for r in informational:
                warnings.append(
                    f"[info] Enrollment rider {r.rider_family_key} ({r.rider_title}): {r.coverage_status}"
                )
        if delta is not None and not within_tol:
            warnings.append(
                f"Leaf-600 mismatch: engine={engine_cents:.4f} ¢/kWh, "
                f"leaf-600={leaf600_cents:.4f} ¢/kWh, delta={delta:.4f} ¢/kWh"
            )

        if not rider_entries and sched_entry.charge_status == "no_charges":
            verdict = "no_data"
        elif missing:
            verdict = "missing_riders"
        elif delta is not None and not within_tol:
            verdict = "partial"
        elif informational:
            verdict = "complete"  # enrollment riders without charges are expected
        else:
            verdict = "complete"

        return TariffCoverageMap(
            as_of_date=date_str,
            schedule_family_key=schedule_family_key,
            schedule_title=family.title if family else None,
            schedule_version_id=sched_version.id,
            schedule_revision_label=sched_version.revision_label,
            schedule_charge_status=sched_entry.charge_status,
            riders=rider_entries,
            leaf600_total_cents_per_kwh=leaf600_cents,
            engine_summary_total_cents_per_kwh=engine_cents,
            delta_cents_per_kwh=delta,
            delta_within_tolerance=within_tol,
            audit_verdict=verdict,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Batch null audit
    # ------------------------------------------------------------------

    def build_null_audit(
        self,
        state: str,
        company: str,
        as_of_date: datetime.date,
        family_type: str = "rate_schedule",
        customer_class: str = "residential",
    ) -> list[TariffCoverageMap]:
        """Run coverage audit over all rate schedules for a state/company.

        Returns one TariffCoverageMap per schedule, sorted by family_key.
        """
        families = self._repo.list_tariff_families(
            state=state,
            company=company,
            family_type=family_type,
        )
        results = []
        for fam in families:
            try:
                cmap = self.build_coverage_map(fam.family_key, as_of_date, customer_class)
            except Exception as exc:
                log.warning("Audit failed for %s: %s", fam.family_key, exc)
                cmap = TariffCoverageMap(
                    as_of_date=str(as_of_date),
                    schedule_family_key=fam.family_key,
                    schedule_title=fam.title,
                    schedule_version_id=None,
                    schedule_revision_label=None,
                    schedule_charge_status="error",
                    audit_verdict="no_data",
                    warnings=[f"Audit error: {exc}"],
                )
            results.append(cmap)
        return results

    # ------------------------------------------------------------------
    # Static rider map (no date filtering)
    # ------------------------------------------------------------------

    def build_rider_map(
        self,
        state: str,
        company: str,
    ) -> dict[str, list[dict]]:
        """Return a static map of schedule → riders (without date filtering).

        Each entry in the list contains:
        ``rider_family_key``, ``rider_title``, ``in_rider_summary``,
        ``enrollment_type``, ``mandatory``.
        """
        families = self._repo.list_tariff_families(state=state, company=company)
        schedule_keys = {f.family_key for f in families if f.family_type == "rate_schedule"}

        result: dict[str, list[dict]] = defaultdict(list)
        all_links = self._repo.list_rider_applicability()
        rider_titles: dict[str, str | None] = {
            f.family_key: f.title for f in families
        }

        for link in all_links:
            if link.applies_to_family_key not in schedule_keys:
                continue
            result[link.applies_to_family_key].append(
                {
                    "rider_family_key": link.rider_family_key,
                    "rider_title": rider_titles.get(link.rider_family_key),
                    "in_rider_summary": link.in_rider_summary,
                    "enrollment_type": link.enrollment_type,
                    "mandatory": link.mandatory,
                }
            )

        # Sort riders within each schedule for stable output
        for key in result:
            result[key].sort(key=lambda r: r["rider_family_key"])

        return dict(sorted(result.items()))

    # ------------------------------------------------------------------
    # Search work list — NCUC-targeted gap list
    # ------------------------------------------------------------------

    def build_search_worklist(
        self,
        state: str,
        company: str,
        family_types: list[str] | None = None,
        include_enrollment_riders: bool = False,
    ) -> list["TariffSearchWorkItem"]:
        """Build a prioritized list of families that need NCUC searches.

        For each tariff family that has no charges (or no versions), returns a
        ``TariffSearchWorkItem`` with:

        - The leaf number and current revision label (the primary NCUC search term)
        - Known docket numbers from ``regulatory_docket_leads`` that reference
          this leaf's code
        - Pre-formed NCUC search queries ranked best-first
        - Count of already-downloaded PDFs in ``ncuc_discovery_records`` that may
          contain this leaf

        *Priority* is assigned as:
        - ``high``: rate_schedule families — directly impacts bill calculation
        - ``medium``: rider families that are in the leaf-600 summary or mandatory
        - ``low``: enrollment/optional rider families
        """
        from duke_rates.models.audit import TariffSearchWorkItem

        if family_types is None:
            family_types = ["rate_schedule", "rider"]

        all_families = self._repo.list_tariff_families(state=state, company=company)
        target_families = [
            f for f in all_families if f.family_type in family_types
        ]

        # Pre-load rider_applicability to determine rider priority
        all_links = self._repo.list_rider_applicability()
        # in_summary or mandatory for any schedule → higher priority
        rider_summary_keys: set[str] = set()
        for lnk in all_links:
            if lnk.in_rider_summary or lnk.mandatory:
                rider_summary_keys.add(lnk.rider_family_key)

        # Pre-load docket cross-reference from regulatory_docket_leads
        docket_map = self._build_docket_map(state, company)

        # Pre-load local PDF counts from ncuc_discovery_records per leaf number
        pdf_count_map = self._build_pdf_count_map(state, company)

        items: list[TariffSearchWorkItem] = []

        for fam in target_families:
            versions = self._repo.list_tariff_versions(fam.family_key)
            if not versions:
                gap_reason = "no_versions"
            else:
                # Check if any version has charges
                has_charges = False
                for v in versions:
                    charges = self._repo.list_tariff_charges(v.id)
                    if charges:
                        has_charges = True
                        break
                if has_charges:
                    continue  # family is populated — skip
                gap_reason = "no_charges"

            # Skip enrollment riders unless requested
            if fam.family_type == "rider" and not include_enrollment_riders:
                if fam.family_key not in rider_summary_keys:
                    continue

            # Extract leaf number from family_key: nc-progress-leaf-532 → "532"
            leaf_no = _extract_leaf_no(fam.family_key)

            # Most recent version info
            current_label: str | None = None
            current_start: str | None = None
            if versions:
                dated = [v for v in versions if v.effective_start]
                latest = max(dated, key=lambda v: v.effective_start, default=None) if dated else versions[-1]
                current_label = latest.revision_label
                current_start = latest.effective_start

            # Priority — rate_schedule priority is refined by leaf number range:
            #   high:   traditional billing schedules (residential, commercial, lighting, purchased power)
            #   medium: EV charging program schedules (per-kWh billing relevant)
            #   low:    EE/DSM programs, regulations (no per-kWh charges expected)
            if fam.family_type == "rate_schedule":
                priority = _schedule_priority(leaf_no)
            elif fam.family_key in rider_summary_keys:
                priority = "medium"
            else:
                priority = "low"

            # Known dockets
            known_dockets = docket_map.get(leaf_no or "", [])

            # PDF count
            local_pdfs = pdf_count_map.get(leaf_no or "", 0)

            # Suggested search queries — ranked best-first
            queries = _build_search_queries(
                leaf_no=leaf_no,
                revision_label=current_label,
                title=fam.title,
                state=state,
                company=company,
            )

            category = _schedule_category(fam.family_type, leaf_no)

            items.append(
                TariffSearchWorkItem(
                    family_key=fam.family_key,
                    family_type=fam.family_type,
                    title=fam.title,
                    leaf_no=leaf_no,
                    current_revision_label=current_label,
                    current_effective_start=current_start,
                    gap_reason=gap_reason,
                    priority=priority,
                    category=category,
                    known_dockets=known_dockets,
                    suggested_queries=queries,
                    local_pdf_count=local_pdfs,
                )
            )

        # Sort: high priority first, then by family_key
        priority_order = {"high": 0, "medium": 1, "low": 2}
        items.sort(key=lambda x: (priority_order[x.priority], x.family_key))
        return items

    def _build_docket_map(self, state: str, company: str) -> dict[str, list[str]]:
        """Return {leaf_no: [docket_number, ...]} from regulatory_docket_leads."""
        import json

        with self._repo._connect() as conn:
            rows = conn.execute(
                """
                SELECT docket_number, referenced_codes_json
                FROM regulatory_docket_leads
                WHERE referenced_codes_json IS NOT NULL
                  AND referenced_codes_json != '[]'
                  AND referenced_codes_json != 'null'
                """
            ).fetchall()

        result: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            try:
                codes = json.loads(row["referenced_codes_json"])
            except (TypeError, ValueError):
                continue
            docket = row["docket_number"]
            if not docket:
                continue
            for code in codes:
                code_str = str(code).strip()
                # Only include purely numeric codes (leaf numbers)
                if code_str.isdigit():
                    if docket not in result[code_str]:
                        result[code_str].append(docket)

        return dict(result)

    def _build_pdf_count_map(self, state: str, company: str) -> dict[str, int]:
        """Return {leaf_no: count_of_local_pdfs} from ncuc_discovery_records."""
        with self._repo._connect() as conn:
            rows = conn.execute(
                """
                SELECT search_query, COUNT(*) as cnt
                FROM ncuc_discovery_records
                WHERE local_path IS NOT NULL
                  AND fetch_status = 'success'
                GROUP BY search_query
                """
            ).fetchall()

        # We can also check filing_title for leaf mentions
        # Build a simpler map by scanning for leaf number patterns in titles
        with self._repo._connect() as conn:
            title_rows = conn.execute(
                """
                SELECT COALESCE(filing_title, '') as title,
                       COALESCE(search_query, '') as query,
                       CASE WHEN local_path IS NOT NULL THEN 1 ELSE 0 END as has_local
                FROM ncuc_discovery_records
                WHERE fetch_status = 'success'
                """
            ).fetchall()

        import re
        result: dict[str, int] = defaultdict(int)
        leaf_pat = re.compile(r'\bLeaf\s+No\.?\s+(\d+)\b', re.IGNORECASE)
        for row in title_rows:
            if not row["has_local"]:
                continue
            text = row["title"] + " " + row["query"]
            for m in leaf_pat.finditer(text):
                result[m.group(1)] += 1

        return dict(result)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _enrich_versions(self, versions) -> list[VersionTimelineEntry]:
        """Fetch charge stats from v_version_charge_summary for each version."""
        if not versions:
            return []
        ids = tuple(v.id for v in versions)
        placeholders = ",".join("?" * len(ids))
        with self._repo._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM v_version_charge_summary WHERE version_id IN ({placeholders})",
                ids,
            ).fetchall()
        stats: dict[int, dict] = {r["version_id"]: dict(r) for r in rows}

        entries = []
        for v in versions:
            s = stats.get(v.id, {})
            entries.append(
                VersionTimelineEntry(
                    version_id=v.id,
                    family_key=v.family_key,
                    effective_start=v.effective_start,
                    effective_end=v.effective_end,
                    revision_label=v.revision_label,
                    supersedes_label=v.supersedes_label,
                    source_type=v.source_type,
                    confidence_score=v.confidence_score,
                    charge_count=s.get("charge_count", 0) or 0,
                    null_rate_count=s.get("null_rate_count", 0) or 0,
                )
            )
        return entries

    def _sum_per_kwh_rate(
        self, version_id: int, customer_class: str = "residential"
    ) -> float | None:
        """Sum all adjustment $/kWh charges for a version/customer_class.

        Returns the total in $/kWh (not cents), or None if no matching charges.
        """
        charges = self._repo.list_tariff_charges(version_id)
        total = 0.0
        found = False
        for c in charges:
            if c.charge_type != "adjustment":
                continue
            unit = (c.rate_unit or "").lower()
            if "kwh" not in unit:
                continue
            # Class matching: None or 'all' means applies to all
            if c.customer_class and c.customer_class not in ("all", customer_class):
                continue
            val = c.rate_value or 0.0
            # Convert cents/kWh → $/kWh if needed
            if unit.startswith("cents") or "¢" in unit:
                val /= 100.0
            total += val
            found = True
        return total if found else None


# ---------------------------------------------------------------------------
# Search work list helpers
# ---------------------------------------------------------------------------


def _schedule_category(family_type: str, leaf_no: str | None) -> str:
    """Return a human-readable category for a tariff family."""
    if family_type == "rider":
        return "rider"
    if leaf_no is None or not leaf_no.isdigit():
        return "other"
    n = int(leaf_no)
    if 500 <= n <= 519:
        return "residential"
    if 520 <= n <= 539:
        return "commercial_small"
    if 540 <= n <= 559:
        return "commercial_large"
    if 560 <= n <= 569:
        return "industrial"
    if 570 <= n <= 579:
        return "lighting"
    if 580 <= n <= 589:
        return "agricultural"
    if 590 <= n <= 599:
        return "purchased_power"
    if 700 <= n <= 739:
        return "ee_program"
    if 740 <= n <= 799:
        return "ev_program"
    if 800 <= n <= 899:
        return "regulation"
    return "other"


def _schedule_priority(leaf_no: str | None) -> str:
    """Return priority for an unseeded rate_schedule based on its leaf number range.

    DEP NC tariff leaf number conventions:
    - 500-599: Core billing schedules (residential, commercial, lighting, PP) → high
    - 700-769: EE/DSM program schedules (incentive programs, no per-kWh rates) → low
    - 740-799: EV charging programs (per-kWh billing relevant) → medium
    - 800-809: Service regulations (no rates) → low
    """
    if leaf_no is None or not leaf_no.isdigit():
        return "medium"
    n = int(leaf_no)
    if 500 <= n <= 599:
        return "high"
    if 740 <= n <= 799:
        return "medium"   # EV program schedules — per-kWh billing relevant
    if 700 <= n <= 739:
        return "low"      # EE/DSM programs — incentive/rebate, no standard rate
    if 800 <= n <= 899:
        return "low"      # Service regulations — no rate charges
    return "medium"       # Unknown range — default to medium


def _extract_leaf_no(family_key: str) -> str | None:
    """Extract the leaf number from a family_key like 'nc-progress-leaf-532'."""
    import re
    m = re.search(r"-leaf-(\d+)$", family_key)
    return m.group(1) if m else None


def _build_search_queries(
    leaf_no: str | None,
    revision_label: str | None,
    title: str | None,
    state: str,
    company: str,
) -> list[str]:
    """Build NCUC search query strings, ranked best-first.

    Query strategy (in order of specificity):
    1. Full revision label — most specific, matches exact filing title
    2. "Leaf No. <N>" — finds all filings for this leaf regardless of revision
    3. Schedule/rider short code from title (e.g. "SGS", "RDM")
    4. DEP utility + schedule code combo
    """
    queries: list[str] = []

    # 1. Full revision label (e.g. "NC Second Revised Leaf No. 532")
    if revision_label:
        queries.append(revision_label)

    # 2. Leaf number search
    if leaf_no:
        queries.append(f"Leaf No. {leaf_no}")
        queries.append(f"Leaf {leaf_no}")

    # 3. Short code from title — extract parenthetical like "SGS", "RDM", "CPRE"
    if title:
        import re
        # Extract parenthetical abbreviation: "Small General Service Schedule SGS" → "SGS"
        m = re.search(r'\b([A-Z][A-Z0-9\-]{1,8})\s*(?:\(.*\))?\s*$', title)
        if m:
            code = m.group(1)
            if len(code) >= 2 and code not in ("NC", "SC", "DEP", "DEC"):
                queries.append(f"Duke Energy Progress {code}")
                queries.append(code)

    return queries


# ---------------------------------------------------------------------------
# Supersession chain reconstruction
# ---------------------------------------------------------------------------


def _build_supersession_chain(
    entries: list[VersionTimelineEntry],
) -> tuple[list[str], list[str]]:
    """Reconstruct the linear supersession chain from revision/supersedes labels.

    Returns ``(chain, orphans)`` where ``chain`` is a list of revision_labels
    in chronological supersession order and ``orphans`` is any revision_label
    not linked into the main chain.
    """
    # Build maps
    label_to_entry: dict[str, VersionTimelineEntry] = {}
    for e in entries:
        if e.revision_label:
            label_to_entry[e.revision_label] = e

    # supersedes_label → revision_label (child → parent link)
    # We want: given a revision_label, find what supersedes it
    superseded_by: dict[str, str] = {}  # parent_label → child_label
    for e in entries:
        if e.supersedes_label and e.revision_label:
            superseded_by[e.supersedes_label] = e.revision_label

    if not label_to_entry:
        return [], []

    # Find roots: revision_labels that are not themselves superseded by anything
    all_labels = set(label_to_entry.keys())
    non_roots = set(superseded_by.values())
    roots = all_labels - non_roots

    best_root = None
    if roots:
        # If multiple roots (e.g. original + orphan), prefer the one with earliest start
        dated_roots = sorted(
            [label_to_entry[r] for r in roots if label_to_entry[r].effective_start],
            key=lambda e: e.effective_start,  # type: ignore[arg-type]
        )
        if dated_roots:
            best_root = dated_roots[0].revision_label
        else:
            best_root = next(iter(roots))

    if best_root is None:
        # Circular or all have supersedes — fall back to date order
        chain = [
            e.revision_label
            for e in sorted(
                [e for e in entries if e.revision_label and e.effective_start],
                key=lambda e: e.effective_start,  # type: ignore[arg-type]
            )
        ]
        return chain, []

    # Walk forward from root through superseded_by
    chain: list[str] = []
    current = best_root
    visited: set[str] = set()
    while current and current not in visited:
        chain.append(current)
        visited.add(current)
        current = superseded_by.get(current)  # type: ignore[assignment]

    orphans = sorted(all_labels - set(chain))
    return chain, orphans
