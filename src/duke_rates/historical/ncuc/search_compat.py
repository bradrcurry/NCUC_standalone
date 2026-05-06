"""
Stage 1: NCUC full-text search compatibility harness.

Tests which query syntax patterns are accepted by the NCUC Zoom search UI
without SQL syntax errors or zero-result pages, and persists the results
so the query builder can emit only empirically safe patterns.

The NCUC public search is at: https://www.ncuc.gov/search/search.php
It uses a Zoom-based full-text search which has an undocumented SQL engine
underneath — some Boolean operator combinations and quoted-phrase + wildcard
combinations trigger SQL parse errors.

Usage:
    harness = SearchCompatibilityHarness(settings)
    report = harness.run_full_probe(save=True)
    safe = harness.load_safe_patterns()
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import httpx

from duke_rates.config import Settings
from duke_rates.historical.ncuc.query_syntax import classify_pattern_type, sanitize_ncuc_query

logger = logging.getLogger(__name__)

NCUC_ZOOM_SEARCH = "https://www.ncuc.gov/search/search.php"

# ---------------------------------------------------------------------------
# Candidate query template library — one per testable syntactic pattern
# ---------------------------------------------------------------------------

@dataclass
class QueryTemplate:
    """A candidate query to probe for parser compatibility."""
    name: str          # Unique identifier for this pattern family
    query: str         # Literal query string to submit
    pattern_type: str  # single_term | two_term | boolean_and | boolean_or |
                       # near | quoted_phrase | suffix_wildcard | complex


# All patterns we want to probe. Designed to cover the space from trivially
# safe to increasingly exotic.
CANDIDATE_TEMPLATES: list[QueryTemplate] = [
    # -- Single terms (always expected to work) --
    QueryTemplate("single_tariff", "tariff", "single_term"),
    QueryTemplate("single_rider", "rider", "single_term"),
    QueryTemplate("single_schedule", "schedule", "single_term"),
    QueryTemplate("single_duke", "Duke", "single_term"),
    QueryTemplate("single_progress", "Progress", "single_term"),
    QueryTemplate("single_rate", "rate", "single_term"),
    QueryTemplate("single_sheet", "sheet", "single_term"),
    QueryTemplate("single_residential", "residential", "single_term"),
    QueryTemplate("single_approved", "approved", "single_term"),
    QueryTemplate("single_superseding", "superseding", "single_term"),
    QueryTemplate("single_canceling", "canceling", "single_term"),
    QueryTemplate("single_effective", "effective", "single_term"),

    # -- Simple two-term implicit AND (space-separated, no operators) --
    QueryTemplate("two_duke_tariff", "Duke tariff", "two_term"),
    QueryTemplate("two_duke_rider", "Duke rider", "two_term"),
    QueryTemplate("two_duke_schedule", "Duke schedule", "two_term"),
    QueryTemplate("two_duke_rate", "Duke rate", "two_term"),
    QueryTemplate("two_progress_tariff", "Progress tariff", "two_term"),
    QueryTemplate("two_progress_rider", "Progress rider", "two_term"),
    QueryTemplate("two_tariff_sheet", "tariff sheet", "two_term"),
    QueryTemplate("two_rate_schedule", "rate schedule", "two_term"),
    QueryTemplate("two_residential_service", "residential service", "two_term"),

    # -- Three-term implicit AND --
    QueryTemplate("three_duke_progress_tariff", "Duke Progress tariff", "two_term"),
    QueryTemplate("three_duke_progress_rider", "Duke Progress rider", "two_term"),
    QueryTemplate("three_duke_tariff_sheet", "Duke tariff sheet", "two_term"),
    QueryTemplate("three_progress_rate_schedule", "Progress rate schedule", "two_term"),
    QueryTemplate("three_duke_residential_service", "Duke residential service", "two_term"),

    # -- Quoted phrases (may or may not be supported) --
    QueryTemplate("quoted_duke_progress", '"Duke Energy Progress"', "quoted_phrase"),
    QueryTemplate("quoted_progress_carolinas", '"Progress Energy Carolinas"', "quoted_phrase"),
    QueryTemplate("quoted_tariff_sheet", '"tariff sheet"', "quoted_phrase"),
    QueryTemplate("quoted_rate_schedule", '"rate schedule"', "quoted_phrase"),
    QueryTemplate("quoted_residential_service", '"residential service"', "quoted_phrase"),
    QueryTemplate("quoted_superseding_sheet", '"superseding sheet"', "quoted_phrase"),
    QueryTemplate("quoted_canceling_sheet", '"canceling sheet"', "quoted_phrase"),
    QueryTemplate("quoted_effective_for", '"effective for service"', "quoted_phrase"),
    QueryTemplate("quoted_issued_by", '"issued by authority"', "quoted_phrase"),

    # -- Quoted phrase + bare term --
    QueryTemplate("quoted_dep_tariff", '"Duke Energy Progress" tariff', "quoted_phrase"),
    QueryTemplate("quoted_dep_rider", '"Duke Energy Progress" rider', "quoted_phrase"),
    QueryTemplate("quoted_dep_schedule", '"Duke Energy Progress" schedule', "quoted_phrase"),
    QueryTemplate("quoted_dep_rate", '"Duke Energy Progress" rate', "quoted_phrase"),
    QueryTemplate("quoted_dep_sheet", '"Duke Energy Progress" sheet', "quoted_phrase"),
    QueryTemplate("quoted_pec_tariff", '"Progress Energy Carolinas" tariff', "quoted_phrase"),
    QueryTemplate("quoted_pec_rider", '"Progress Energy Carolinas" rider', "quoted_phrase"),

    # -- Suffix wildcards --
    QueryTemplate("wildcard_tariff_star", "tariff*", "suffix_wildcard"),
    QueryTemplate("wildcard_rider_star", "rider*", "suffix_wildcard"),
    QueryTemplate("wildcard_schedule_star", "schedule*", "suffix_wildcard"),
    QueryTemplate("wildcard_superced_star", "superced*", "suffix_wildcard"),
    QueryTemplate("wildcard_cancel_star", "cancel*", "suffix_wildcard"),

    # -- Explicit Boolean AND (uppercase) --
    QueryTemplate("bool_and_duke_tariff", "Duke AND tariff", "boolean_and"),
    QueryTemplate("bool_and_duke_rider", "Duke AND rider", "boolean_and"),
    QueryTemplate("bool_and_progress_tariff", "Progress AND tariff", "boolean_and"),
    QueryTemplate("bool_and_tariff_sheet", "tariff AND sheet", "boolean_and"),
    QueryTemplate("bool_and_rate_schedule", "rate AND schedule", "boolean_and"),
    QueryTemplate("bool_and_residential_service", "residential AND service", "boolean_and"),

    # -- Explicit Boolean OR (uppercase) --
    QueryTemplate("bool_or_tariff_rider", "tariff OR rider", "boolean_or"),
    QueryTemplate("bool_or_duke_progress", "Duke OR Progress", "boolean_or"),
    QueryTemplate("bool_or_superseding_canceling", "superseding OR canceling", "boolean_or"),

    # -- NEAR operator --
    QueryTemplate("near_duke_tariff", "Duke NEAR tariff", "near"),
    QueryTemplate("near_tariff_sheet", "tariff NEAR sheet", "near"),
    QueryTemplate("near_rider_schedule", "rider NEAR schedule", "near"),

    # -- Parenthesized groups --
    QueryTemplate("paren_or_simple", "(tariff OR rider)", "complex"),
    QueryTemplate("paren_and_or", "Duke (tariff OR rider)", "complex"),
    QueryTemplate("paren_progress_and_or", "Progress (tariff OR rider)", "complex"),

    # -- Complex multi-term --
    QueryTemplate("complex_dep_tariff_sheet", '"Duke Energy Progress" tariff sheet', "complex"),
    QueryTemplate("complex_dep_rider_schedule", '"Duke Energy Progress" rider schedule', "complex"),
    QueryTemplate("complex_progress_rate_schedule", '"Progress Energy Carolinas" rate schedule', "complex"),
]


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """Outcome of testing a single query template against the NCUC search UI."""
    template_name: str
    query: str
    pattern_type: str
    tested_at: str
    success: bool              # Returned HTTP 200 with no error signal
    error_detected: bool       # SQL/parse error in response body
    zero_results: bool         # No results returned
    result_count: int          # Estimated count from page
    error_snippet: str         # First 200 chars of error text if detected
    response_ms: int           # Round-trip time in milliseconds
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProbeResult":
        d2 = dict(d)
        if "notes" not in d2:
            d2["notes"] = []
        return cls(**d2)


@dataclass
class CompatibilityReport:
    """Aggregated results from a full compatibility probe run."""
    run_at: str
    total_probed: int
    safe_count: int
    error_count: int
    zero_result_count: int
    results: list[ProbeResult] = field(default_factory=list)

    @property
    def safe_patterns(self) -> list[ProbeResult]:
        return [r for r in self.results if r.success and not r.error_detected and not r.zero_results]

    @property
    def error_patterns(self) -> list[ProbeResult]:
        return [r for r in self.results if r.error_detected]

    def to_dict(self) -> dict:
        return {
            "run_at": self.run_at,
            "total_probed": self.total_probed,
            "safe_count": self.safe_count,
            "error_count": self.error_count,
            "zero_result_count": self.zero_result_count,
            "results": [r.to_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CompatibilityReport":
        results = [ProbeResult.from_dict(r) for r in d.get("results", [])]
        return cls(
            run_at=d["run_at"],
            total_probed=d["total_probed"],
            safe_count=d["safe_count"],
            error_count=d["error_count"],
            zero_result_count=d["zero_result_count"],
            results=results,
        )


# ---------------------------------------------------------------------------
# Error / zero-result detection patterns
# ---------------------------------------------------------------------------

_SQL_ERROR_PATTERNS: list[re.Pattern] = [
    re.compile(r"sql\s+(?:syntax\s+)?error", re.IGNORECASE),
    re.compile(r"you have an error in your sql syntax", re.IGNORECASE),
    re.compile(r"mysql\s+error", re.IGNORECASE),
    re.compile(r"parse\s+error", re.IGNORECASE),
    re.compile(r"query\s+failed", re.IGNORECASE),
    re.compile(r"internal\s+server\s+error", re.IGNORECASE),
    re.compile(r"error\s+executing\s+query", re.IGNORECASE),
    re.compile(r"database\s+error", re.IGNORECASE),
    re.compile(r"unexpected\s+token", re.IGNORECASE),
    re.compile(r"zoom_results_count.*?0", re.IGNORECASE),  # Zoom-specific
]

_ZERO_RESULT_PATTERNS: list[re.Pattern] = [
    re.compile(r"no\s+(?:documents?|results?|pages?)\s+(?:found|match)", re.IGNORECASE),
    re.compile(r"0\s+(?:results?|documents?)\s+found", re.IGNORECASE),
    re.compile(r"your\s+search\s+(?:did\s+not\s+match|returned\s+no)", re.IGNORECASE),
    re.compile(r"zoom_results_count.*?>\s*0\s*<", re.IGNORECASE),
]

_RESULT_COUNT_PAT = re.compile(
    r"(?:results?\s+\d+\s*[-–]\s*\d+\s+of\s+|"
    r"(?:about\s+)?(\d+)\s+(?:results?|documents?|pages?)\s+found|"
    r"zoom_results_count[^>]*>\s*(\d+)\s*<)",
    re.IGNORECASE,
)


def _detect_errors(html: str) -> tuple[bool, str]:
    """Return (error_found, snippet) from response HTML."""
    for pat in _SQL_ERROR_PATTERNS:
        m = pat.search(html)
        if m:
            start = max(0, m.start() - 50)
            end = min(len(html), m.end() + 150)
            return True, html[start:end].strip()
    return False, ""


def _detect_zero_results(html: str) -> bool:
    for pat in _ZERO_RESULT_PATTERNS:
        if pat.search(html):
            return True
    return False


def _estimate_result_count(html: str) -> int:
    m = _RESULT_COUNT_PAT.search(html)
    if m:
        for grp in m.groups():
            if grp:
                try:
                    return int(grp)
                except ValueError:
                    pass
    # Count result links as a heuristic
    link_count = len(re.findall(r'class=["\'](?:result|zoom_result)', html, re.IGNORECASE))
    return link_count


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------

class SearchCompatibilityHarness:
    """
    Tests NCUC full-text search query syntax empirically.
    Persists results so the query builder can use only safe patterns.
    """

    def __init__(
        self,
        settings: Settings,
        persist_path: Path | None = None,
        delay_seconds: float = 1.5,
    ):
        self.settings = settings
        self.persist_path = persist_path or (
            Path(settings.data_dir) / "manifests" / "search_compat.json"
        )
        self.delay_seconds = delay_seconds
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Referer": "https://www.ncuc.gov/search/",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _probe_one(self, template: QueryTemplate) -> ProbeResult:
        """Submit a single query and record the outcome."""
        safe_types = self.load_safe_pattern_types() or {"single_term", "two_term"}
        sanitized_query = sanitize_ncuc_query(template.query, safe_pattern_types=safe_types)
        pattern_type = classify_pattern_type(sanitized_query) if sanitized_query else template.pattern_type
        params = {
            "zoom_query": sanitized_query or template.query,
            "zoom_sort": "0",
            "zoom_cat[]": "-1",
            "zoom_per_page": "20",
        }
        tested_at = datetime.utcnow().isoformat()
        t0 = time.monotonic()
        try:
            resp = self._client.get(NCUC_ZOOM_SEARCH, params=params)
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            if resp.status_code != 200:
                return ProbeResult(
                    template_name=template.name,
                    query=sanitized_query or template.query,
                    pattern_type=pattern_type,
                    tested_at=tested_at,
                    success=False,
                    error_detected=False,
                    zero_results=True,
                    result_count=0,
                    error_snippet=f"HTTP {resp.status_code}",
                    response_ms=elapsed_ms,
                    notes=[f"http_status={resp.status_code}"],
                )

            html = resp.text
            error_found, error_snippet = _detect_errors(html)
            zero_results = _detect_zero_results(html) if not error_found else True
            count = _estimate_result_count(html) if not error_found else 0

            return ProbeResult(
                template_name=template.name,
                query=sanitized_query or template.query,
                pattern_type=pattern_type,
                tested_at=tested_at,
                success=not error_found,
                error_detected=error_found,
                zero_results=zero_results and count == 0,
                result_count=count,
                error_snippet=error_snippet[:200],
                response_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return ProbeResult(
                template_name=template.name,
                query=sanitized_query or template.query,
                pattern_type=pattern_type,
                tested_at=tested_at,
                success=False,
                error_detected=False,
                zero_results=True,
                result_count=0,
                error_snippet=str(exc)[:200],
                response_ms=elapsed_ms,
                notes=["exception"],
            )

    def run_full_probe(
        self,
        templates: list[QueryTemplate] | None = None,
        *,
        save: bool = True,
        delay: float | None = None,
    ) -> CompatibilityReport:
        """
        Test all candidate templates (or a supplied subset) against the NCUC
        search UI. Records and persists results.

        Args:
            templates: Override the default CANDIDATE_TEMPLATES list.
            save: Persist results to self.persist_path.
            delay: Override per-request delay (seconds).
        """
        templates = templates or CANDIDATE_TEMPLATES
        delay_s = delay if delay is not None else self.delay_seconds
        results: list[ProbeResult] = []
        run_at = datetime.utcnow().isoformat()

        logger.info("Starting NCUC search compatibility probe: %d templates", len(templates))
        for i, tmpl in enumerate(templates, 1):
            logger.info("[%d/%d] Probing pattern %r: %r", i, len(templates), tmpl.name, tmpl.query)
            result = self._probe_one(tmpl)
            results.append(result)
            status = "OK" if result.success else ("ERROR" if result.error_detected else "ZERO")
            logger.info(
                "  → %s  count=%d  ms=%d%s",
                status,
                result.result_count,
                result.response_ms,
                f"  [{result.error_snippet[:80]}]" if result.error_detected else "",
            )
            if i < len(templates):
                time.sleep(delay_s)

        safe = [r for r in results if r.success and not r.error_detected and not r.zero_results]
        errors = [r for r in results if r.error_detected]
        zeros = [r for r in results if not r.error_detected and r.zero_results]

        report = CompatibilityReport(
            run_at=run_at,
            total_probed=len(results),
            safe_count=len(safe),
            error_count=len(errors),
            zero_result_count=len(zeros),
            results=results,
        )

        if save:
            self._save_report(report)

        logger.info(
            "Probe complete: %d safe / %d errors / %d zeros out of %d",
            len(safe), len(errors), len(zeros), len(results),
        )
        return report

    def probe_single(self, query: str, pattern_type: str = "custom") -> ProbeResult:
        """Test a single ad-hoc query string."""
        tmpl = QueryTemplate(name="adhoc", query=query, pattern_type=pattern_type)
        return self._probe_one(tmpl)

    def _save_report(self, report: CompatibilityReport) -> None:
        path = self.persist_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        logger.info("Compatibility report saved to %s", path)

    def load_report(self) -> CompatibilityReport | None:
        """Load a previously saved compatibility report."""
        if not self.persist_path.exists():
            return None
        try:
            data = json.loads(self.persist_path.read_text(encoding="utf-8"))
            return CompatibilityReport.from_dict(data)
        except Exception as exc:
            logger.warning("Failed to load compat report: %s", exc)
            return None

    def load_safe_patterns(self) -> list[ProbeResult]:
        """
        Return safe (non-error, non-zero) patterns from the persisted report.
        Falls back to an empty list if no report exists.
        """
        report = self.load_report()
        if report is None:
            logger.warning("No compatibility report found; run probe first")
            return []
        return report.safe_patterns

    def load_safe_pattern_types(self) -> set[str]:
        """Return the set of pattern_type values that have at least one safe result."""
        safe = self.load_safe_patterns()
        return {r.pattern_type for r in safe}

    def print_summary(self, report: CompatibilityReport | None = None) -> None:
        """Print a human-readable summary of a compatibility report."""
        if report is None:
            report = self.load_report()
        if report is None:
            print("No compatibility report available.")
            return

        print(f"\n=== NCUC Search Compatibility Report ({report.run_at}) ===")
        print(f"Total probed: {report.total_probed}")
        print(f"Safe:         {report.safe_count}")
        print(f"Errors:       {report.error_count}")
        print(f"Zero results: {report.zero_result_count}")
        print()

        # Group by pattern_type
        by_type: dict[str, list[ProbeResult]] = {}
        for r in report.results:
            by_type.setdefault(r.pattern_type, []).append(r)

        for pt, rlist in sorted(by_type.items()):
            safe_c = sum(1 for r in rlist if r.success and not r.error_detected and not r.zero_results)
            err_c = sum(1 for r in rlist if r.error_detected)
            print(f"  {pt:<25} {len(rlist):3d} tested   {safe_c:3d} safe   {err_c:3d} errors")

        if report.error_patterns:
            print("\nPatterns with errors:")
            for r in report.error_patterns:
                print(f"  [{r.template_name}] {r.query!r}")
                if r.error_snippet:
                    print(f"    → {r.error_snippet[:120]}")
