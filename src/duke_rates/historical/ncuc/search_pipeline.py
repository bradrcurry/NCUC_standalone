"""
Main search pipeline orchestrator.

Wires together all 10 stages:

  Stage 1: SearchCompatibilityHarness (search_compat.py)
  Stage 2: QueryBuilder (query_builder.py)
  Stage 3: SearchResultHarvester (result_harvester.py)
  Stage 4: ResultScorer — local scoring (result_scorer.py)
  Stage 5: QueryOptimizer — feedback loop (query_optimizer.py)
  Stage 6: Iterative refinement (via QueryBuilder.build_refinement_queries)
  Stage 7: DocumentFamilyGrouper (family_grouper.py)
  Stage 8: Ideality classification (embedded in ResultScorer)
  Stage 9: Selective download (uses existing NcucDownloader)
  Stage 10: Optional LLMClassifier (llm_classifier.py)

Persistence:
  - SearchCompatibilityHarness → data/manifests/search_compat.json
  - QueryOptimizer            → data/manifests/query_optimizer.json
  - HarvestSession            → data/manifests/search_pipeline/harvest_*.jsonl
  - ScoredResults             → data/manifests/search_pipeline/scored_*.jsonl
  - Families                  → data/manifests/search_pipeline/families_*.json
  - LLM classifications       → data/manifests/search_pipeline/llm_classifications_*.jsonl

Usage:
    pipeline = SearchPipeline(settings)
    result = pipeline.run(
        utility="Duke Energy Progress",
        schedule_codes=["501", "602"],
        max_queries=30,
        use_llm=False,
    )
    result.print_summary()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from duke_rates.config import Settings
from duke_rates.historical.ncuc.search_compat import SearchCompatibilityHarness
from duke_rates.historical.ncuc.query_builder import QueryBuilder, QuerySpec
from duke_rates.historical.ncuc.query_optimizer import QueryOptimizer
from duke_rates.historical.ncuc.result_harvester import SearchResultHarvester, HarvestSession
from duke_rates.historical.ncuc.result_scorer import ResultScorer, ScoredResult
from duke_rates.historical.ncuc.family_grouper import DocumentFamilyGrouper, DocumentFamily
from duke_rates.historical.ncuc.portal_harvester import PortalSearchHarvester
from duke_rates.historical.ncuc import search_persistence as persist

if TYPE_CHECKING:
    from duke_rates.historical.ncuc.llm_classifier import LLMClassification

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline result container
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """All outputs from a single pipeline run."""
    run_tag: str
    all_scored: list[ScoredResult] = field(default_factory=list)
    families: list[DocumentFamily] = field(default_factory=list)
    top_ideal: list[ScoredResult] = field(default_factory=list)
    llm_pairs: list[tuple[ScoredResult, "LLMClassification"]] = field(default_factory=list)

    harvest_path: Path | None = None
    scored_path: Path | None = None
    families_path: Path | None = None
    llm_path: Path | None = None

    def print_summary(self, top_n: int = 20) -> None:
        print(f"\n{'=' * 70}")
        print(f"Search Pipeline Result: {self.run_tag}")
        print(f"{'=' * 70}")
        print(f"Total scored results:   {len(self.all_scored)}")
        print(f"Document families:      {len(self.families)}")
        print(f"Ideal candidates:       {len(self.top_ideal)}")
        if self.llm_pairs:
            print(f"LLM-classified:         {len(self.llm_pairs)}")
        print()

        if not self.top_ideal:
            print("No ideal candidates identified.")
            return

        print(f"Top {min(top_n, len(self.top_ideal))} ideal candidates:")
        print("-" * 70)
        for i, sr in enumerate(self.top_ideal[:top_n], 1):
            title = (sr.result.title or "(no title)")[:55]
            date = sr.result.filing_date or ""
            docket = sr.result.docket_number or ""
            print(
                f"{i:3d}. [{sr.ideality.doc_type_guess:<16}] [{sr.ideality.likely_finality:<15}] "
                f"score={sr.combined_score:6.2f}"
            )
            print(f"     {title}")
            if docket or date:
                print(f"     docket={docket}  date={date}")
            print(f"     url={sr.result.url[:80]}")
            print(f"     -> {sr.explain()[:100]}")
            print()

    def export_csv(self, output_path: Path, top_n: int | None = None) -> None:
        from duke_rates.historical.ncuc.search_persistence import export_ranked_candidates_csv
        candidates = self.top_ideal or self.all_scored
        export_ranked_candidates_csv(candidates, output_path, top_n=top_n)
        print(f"Exported → {output_path}")

    def export_json(self, output_path: Path, top_n: int | None = None) -> None:
        from duke_rates.historical.ncuc.search_persistence import export_ranked_candidates_json
        candidates = self.top_ideal or self.all_scored
        export_ranked_candidates_json(candidates, output_path, top_n=top_n)
        print(f"Exported → {output_path}")


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

class SearchPipeline:
    """
    Orchestrates the 10-stage NCUC tariff document search pipeline.

    Stages:
    1. Load compat report (or probe if missing)
    2. Generate safe queries via QueryBuilder
    3. Prioritize queries via QueryOptimizer
    4. Harvest results via SearchResultHarvester
    5. Score results locally via ResultScorer
    6. Optionally run iterative refinement
    7. Group into document families
    8. Surface top ideal candidates
    9. Optionally invoke LLMClassifier on top candidates
    10. Persist all outputs
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.compat = SearchCompatibilityHarness(settings)
        self.builder = QueryBuilder(settings, compat_harness=self.compat)
        self.optimizer = QueryOptimizer(settings)
        self.harvester = SearchResultHarvester(settings)
        self.portal_harvester = PortalSearchHarvester(settings)
        self.harvester.set_safe_pattern_types(self.compat.load_safe_pattern_types())
        self.scorer = ResultScorer()
        self.grouper = DocumentFamilyGrouper()
        self._optimizer_loaded = False

    def _ensure_optimizer_loaded(self) -> None:
        if not self._optimizer_loaded:
            self.optimizer.load()
            self._optimizer_loaded = True

    def run(
        self,
        *,
        utility: str | None = None,
        schedule_codes: list[str] | None = None,
        rider_names: list[str] | None = None,
        doc_types: list[str] | None = None,
        max_queries: int = 40,
        max_results_per_query: int = 20,
        max_search_pages: int = 2,
        refinement_rounds: int = 1,
        top_n_ideal: int = 30,
        use_llm: bool = False,
        llm_max_candidates: int = 15,
        use_portal: bool = True,
        portal_only: bool = False,
        portal_max_results: int = 250,
        save: bool = True,
        run_tag: str = "",
    ) -> PipelineResult:
        """
        Run the full pipeline.

        Args:
            utility: Target utility name (default: both DEP and PEC)
            schedule_codes: Target schedule codes (default: all DEP codes)
            rider_names: Target rider names (default: all DEP riders)
            doc_types: Target document types (default: tariff, rider, schedule)
            max_queries: Maximum number of queries to execute
            max_results_per_query: Results per search page
            max_search_pages: Number of pages to fetch per query
            refinement_rounds: How many iterative refinement rounds to run
            top_n_ideal: Number of top ideal candidates to surface
            use_llm: Whether to run LLM classification on top candidates
            llm_max_candidates: Max candidates to send to LLM
            save: Whether to persist outputs to disk
            run_tag: Optional string tag for output filenames
        """
        import datetime
        if not run_tag:
            run_tag = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")

        self._ensure_optimizer_loaded()

        # ---- Stage 2: Generate queries ----
        # broad_run = no specific targets provided; portal should return all results.
        broad_run = not any([utility, schedule_codes, rider_names, doc_types])
        if not broad_run:
            query_specs = self.builder.build_targeted_queries(
                utility=utility,
                schedule_codes=schedule_codes,
                rider_names=rider_names,
                doc_types=doc_types,
            )
        else:
            query_specs = self.builder.build_dep_queries()

        # ---- Stage 3 (pre): Prioritize queries ----
        query_specs = self.optimizer.get_prioritized_queries(
            query_specs, max_results=max_queries
        )
        logger.info("Pipeline: executing %d queries", len(query_specs))

        # ---- Stage 3: Harvest ----
        session = HarvestSession()
        if use_portal:
            portal_session = self.portal_harvester.harvest(
                utility=utility,
                query_specs=query_specs,
                doc_types=doc_types,
                max_results=portal_max_results,
                broad_run=broad_run,
            )
            session.merge(portal_session)
            logger.info("Pipeline: harvested %d portal results", portal_session.total_unique)

        if not portal_only:
            self.harvester.reset_session()
            public_session = self.harvester.harvest_all(
                query_specs,
                per_page=max_results_per_query,
                max_pages=max_search_pages,
            )
            session.merge(public_session)
            logger.info("Pipeline: harvested %d public unique results", public_session.total_unique)
        logger.info("Pipeline: harvested %d unique results", session.total_unique)

        # ---- Stage 5: Update optimizer ----
        self._update_optimizer(session)

        # ---- Stage 4 & 8: Score and classify ----
        raw_results = session.all_results
        query_usefulness = {
            s.query_text: s.usefulness_score
            for s in self.optimizer._stats.values()
        }
        scored = self.scorer.score_all(raw_results, query_usefulness=query_usefulness)
        logger.info("Pipeline: scored %d results", len(scored))

        # ---- Stage 6: Iterative refinement ----
        for round_num in range(refinement_rounds):
            refinement_terms = self.optimizer.suggest_refinement_terms(top_n=10)
            if not refinement_terms:
                break
            logger.info(
                "Refinement round %d: terms=%s", round_num + 1, refinement_terms[:5]
            )
            refinement_specs = self.builder.build_refinement_queries(
                seed_terms=refinement_terms[:8],
                utility_hint=utility,
            )
            refinement_specs = self.optimizer.get_prioritized_queries(
                refinement_specs, max_results=10
            )
            if not refinement_specs:
                break

            ref_session = self.harvester.harvest_all(
                refinement_specs,
                per_page=max_results_per_query,
                max_pages=max_search_pages,
            )
            self._update_optimizer(ref_session)
            if ref_session.all_results:
                new_scored = self.scorer.score_all(
                    ref_session.all_results,
                    query_usefulness=query_usefulness,
                )
                scored.extend(new_scored)
                # Re-sort
                scored.sort(key=lambda s: -s.combined_score)
                logger.info(
                    "Refinement round %d: +%d results, total=%d",
                    round_num + 1, len(new_scored), len(scored),
                )

        # ---- Stage 7: Family grouping ----
        families = self.grouper.group(scored)
        logger.info("Pipeline: %d document families", len(families))

        # ---- Surface top ideal candidates ----
        top_ideal = [
            sr for sr in scored
            if sr.ideality.is_ideal_candidate
        ][:top_n_ideal]
        if not top_ideal:
            # Fall back to top scored even if not classified as ideal
            top_ideal = scored[:top_n_ideal]

        # ---- Stage 9 (optional): LLM classification ----
        llm_pairs: list[tuple[ScoredResult, "LLMClassification"]] = []
        if use_llm:
            try:
                from duke_rates.historical.ncuc.llm_classifier import LLMClassifier
                classifier = LLMClassifier(self.settings)
                if classifier.is_available:
                    llm_pairs = classifier.classify_batch(
                        top_ideal, max_candidates=llm_max_candidates
                    )
                    logger.info("LLM classified %d candidates", len(llm_pairs))
                else:
                    logger.info("LLM classifier not available (no API key)")
            except ImportError:
                logger.warning("LLM classifier import failed")

        # ---- Stage G: Persist ----
        result = PipelineResult(
            run_tag=run_tag,
            all_scored=scored,
            families=families,
            top_ideal=top_ideal,
            llm_pairs=llm_pairs,
        )

        if save:
            result.harvest_path = persist.save_harvest_session(session, self.settings)
            result.scored_path = persist.save_scored_results(scored, self.settings, tag=run_tag)
            result.families_path = persist.save_family_groupings(families, self.settings, tag=run_tag)
            if llm_pairs:
                result.llm_path = persist.save_llm_classifications(llm_pairs, self.settings)
            self.optimizer.save()

        return result

    def _update_optimizer(self, session: HarvestSession) -> None:
        """Feed harvest session outcomes back to the QueryOptimizer."""
        # Build a score map for quick lookup
        from duke_rates.historical.ncuc.result_scorer import ResultScorer

        raw_results = session.all_results
        if raw_results:
            temp_scored = self.scorer.score_all(raw_results)
            score_map: dict[str, ScoredResult] = {
                r.result.url: r for r in temp_scored
            }
        else:
            score_map = {}

        HIGH_THRESHOLD = 0.55

        for qr in session.query_records:
            if qr.template_name.startswith("portal_"):
                continue
            # Determine which results came from this query
            query_results = [
                score_map[r.url]
                for r in raw_results
                if r.source_query == qr.query_text and r.url in score_map
            ]
            # Also include results where this was one of multiple queries
            found_by_this = [
                score_map[r.url]
                for r in raw_results
                if qr.query_text in r.found_by_queries and r.url in score_map
            ]
            all_query_results = list({sr.result.url: sr for sr in query_results + found_by_this}.values())

            high_score = sum(1 for sr in all_query_results if sr.local_score >= HIGH_THRESHOLD)
            noisy = sum(1 for sr in all_query_results if not sr.ideality.is_ideal_candidate and sr.local_score < 3.0)
            ideal = sum(1 for sr in all_query_results if sr.ideality.is_ideal_candidate)

            self.optimizer.record_query_result(
                query_text=qr.query_text,
                template_name=qr.template_name,
                result_count=qr.new_result_count,
                high_score_count=high_score,
                noisy_count=noisy,
                new_families=ideal,
                syntax_error=qr.had_error,
            )

    def run_compat_probe(
        self,
        *,
        save: bool = True,
        delay: float = 1.5,
    ) -> None:
        """
        Run (or re-run) the search compatibility probe and save results.
        This resets the safe pattern cache so the next run will use fresh data.
        """
        logger.info("Running NCUC search compatibility probe...")
        report = self.compat.run_full_probe(save=save, delay=delay)
        self.builder._loaded = False  # force reload
        self.compat.print_summary(report)

    def close(self) -> None:
        self.harvester.close()
        self.compat.close()
