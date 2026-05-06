"""
Stage G: Persistence and caching layer for the search pipeline.

Stores:
- query attempt records
- query performance stats (delegated to QueryOptimizer)
- result metadata (SearchResult objects)
- scoring outcomes (ScoredResult → ranked_results.jsonl)
- family groupings (DocumentFamily objects)
- optional LLM classifications

All outputs live under data/manifests/search_pipeline/ for easy inspection
and replay.

Also provides export to JSON and CSV for downstream tooling.
"""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duke_rates.historical.ncuc.result_harvester import SearchResult, HarvestSession
    from duke_rates.historical.ncuc.result_scorer import ScoredResult
    from duke_rates.historical.ncuc.family_grouper import DocumentFamily
    from duke_rates.historical.ncuc.llm_classifier import LLMClassification
    from duke_rates.historical.ncuc.query_builder import QuerySpec

from duke_rates.config import Settings

logger = logging.getLogger(__name__)


def _pipeline_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "manifests" / "search_pipeline"


# ---------------------------------------------------------------------------
# Individual save functions
# ---------------------------------------------------------------------------

def save_harvest_session(session: "HarvestSession", settings: Settings) -> Path:
    """Persist all SearchResult objects from a harvest session to JSONL."""
    out_dir = _pipeline_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"harvest_{ts}.jsonl"

    with path.open("w", encoding="utf-8") as f:
        for result in session.all_results:
            row = _result_to_dict(result)
            f.write(json.dumps(row) + "\n")

    # Also write query session summary
    summary_path = out_dir / f"harvest_summary_{ts}.json"
    summary = {
        "ts": ts,
        "total_unique": session.total_unique,
        "queries": [
            {
                "query_text": qr.query_text,
                "template_name": qr.template_name,
                "new_result_count": qr.new_result_count,
                "had_error": qr.had_error,
                "error_snippet": qr.error_snippet[:100] if qr.error_snippet else "",
            }
            for qr in session.query_records
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info("Saved harvest session: %d results → %s", session.total_unique, path)
    return path


def save_scored_results(
    scored: list["ScoredResult"],
    settings: Settings,
    *,
    tag: str = "",
) -> Path:
    """Persist scored results to JSONL."""
    out_dir = _pipeline_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    fname = f"scored_{ts}{'_' + tag if tag else ''}.jsonl"
    path = out_dir / fname

    with path.open("w", encoding="utf-8") as f:
        for sr in scored:
            row = _scored_to_dict(sr)
            f.write(json.dumps(row) + "\n")

    logger.info("Saved %d scored results → %s", len(scored), path)
    return path


def save_family_groupings(
    families: list["DocumentFamily"],
    settings: Settings,
    *,
    tag: str = "",
) -> Path:
    """Persist document family groupings to JSON."""
    out_dir = _pipeline_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    fname = f"families_{ts}{'_' + tag if tag else ''}.json"
    path = out_dir / fname

    data = []
    for fam in families:
        best = fam.best
        data.append({
            "family_id": fam.family_id,
            "size": fam.size(),
            "canonical_title": fam.canonical_title,
            "utility": fam.utility,
            "schedule_codes": fam.schedule_codes,
            "rider_codes": fam.rider_codes,
            "docket_numbers": fam.docket_numbers,
            "has_redline": fam.has_redline,
            "has_clean": fam.has_clean,
            "best": _scored_to_dict(best) if best else None,
            "members": [_scored_to_dict(m) for m in fam.members],
        })

    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Saved %d families → %s", len(families), path)
    return path


def save_llm_classifications(
    pairs: list[tuple["ScoredResult", "LLMClassification"]],
    settings: Settings,
) -> Path:
    """Persist LLM classifications to JSONL."""
    out_dir = _pipeline_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"llm_classifications_{ts}.jsonl"

    with path.open("w", encoding="utf-8") as f:
        for scored, cls in pairs:
            row = {
                "url": scored.result.url,
                "title": scored.result.title,
                "local_score": scored.local_score,
                "combined_score": scored.combined_score,
                "llm_doc_type": cls.doc_type,
                "llm_finality": cls.likely_finality,
                "llm_utility": cls.utility,
                "llm_rider_name": cls.rider_name,
                "llm_schedule_name": cls.schedule_name,
                "llm_effective_date": cls.effective_date,
                "llm_revision_status": cls.revision_status,
                "llm_confidence": cls.confidence,
                "llm_rationale": cls.rationale,
                "llm_model": cls.model_used,
                "llm_error": cls.error,
            }
            f.write(json.dumps(row) + "\n")

    logger.info("Saved %d LLM classifications → %s", len(pairs), path)
    return path


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def export_ranked_candidates_json(
    scored: list["ScoredResult"],
    output_path: Path,
    *,
    top_n: int | None = None,
) -> None:
    """Export ranked candidates to a JSON file for external consumption."""
    candidates = scored[:top_n] if top_n else scored
    data = [_scored_to_dict(sr) for sr in candidates]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Exported %d candidates → %s", len(data), output_path)


def export_ranked_candidates_csv(
    scored: list["ScoredResult"],
    output_path: Path,
    *,
    top_n: int | None = None,
) -> None:
    """Export ranked candidates to CSV."""
    candidates = scored[:top_n] if top_n else scored
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "rank", "local_score", "combined_score",
        "is_ideal", "doc_type", "finality", "confidence",
        "title", "url", "docket_number", "filing_date",
        "schedule_codes", "rider_codes",
        "source_query", "found_by_count",
        "ideal_reason", "nonideal_reason", "explanation",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for rank, sr in enumerate(candidates, 1):
            writer.writerow({
                "rank": rank,
                "local_score": f"{sr.local_score:.3f}",
                "combined_score": f"{sr.combined_score:.3f}",
                "is_ideal": sr.ideality.is_ideal_candidate,
                "doc_type": sr.ideality.doc_type_guess,
                "finality": sr.ideality.likely_finality,
                "confidence": f"{sr.ideality.confidence:.3f}",
                "title": sr.result.title or "",
                "url": sr.result.url,
                "docket_number": sr.result.docket_number or "",
                "filing_date": sr.result.filing_date or "",
                "schedule_codes": ", ".join(sr.result.extracted_schedule_codes),
                "rider_codes": ", ".join(sr.result.extracted_rider_codes),
                "source_query": sr.result.source_query,
                "found_by_count": len(sr.result.found_by_queries),
                "ideal_reason": sr.ideality.ideal_reason,
                "nonideal_reason": sr.ideality.nonideal_reason,
                "explanation": sr.explain()[:200],
            })

    logger.info("Exported %d candidates to CSV → %s", len(candidates), output_path)


# ---------------------------------------------------------------------------
# Load previously saved results
# ---------------------------------------------------------------------------

def load_latest_scored_results(settings: Settings) -> list[dict]:
    """Load the most recently saved scored results JSONL file."""
    out_dir = _pipeline_dir(settings)
    files = sorted(out_dir.glob("scored_*.jsonl"), reverse=True)
    if not files:
        return []
    return _load_jsonl(files[0])


def load_latest_harvest(settings: Settings) -> list[dict]:
    """Load the most recently saved harvest JSONL file."""
    out_dir = _pipeline_dir(settings)
    files = sorted(out_dir.glob("harvest_*.jsonl"), reverse=True)
    if not files:
        return []
    return _load_jsonl(files[0])


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _result_to_dict(result: "SearchResult") -> dict:
    return {
        "url": result.url,
        "url_hash": result.url_hash,
        "title": result.title,
        "snippet": result.snippet,
        "filing_date": result.filing_date,
        "docket_number": result.docket_number,
        "sub_number": result.sub_number,
        "source_query": result.source_query,
        "source_template": result.source_template,
        "utility_hint": result.utility_hint,
        "doc_type_hint": result.doc_type_hint,
        "schedule_code_hint": result.schedule_code_hint,
        "rider_code_hint": result.rider_code_hint,
        "extracted_schedule_codes": result.extracted_schedule_codes,
        "extracted_rider_codes": result.extracted_rider_codes,
        "extracted_leaf_nos": result.extracted_leaf_nos,
        "filing_classification": result.filing_classification,
        "found_by_queries": result.found_by_queries,
        "discovered_at": result.discovered_at,
    }


def _scored_to_dict(sr: "ScoredResult") -> dict:
    d = _result_to_dict(sr.result)
    d.update({
        "local_score": sr.local_score,
        "content_bonus": sr.content_bonus,
        "combined_score": sr.combined_score,
        "is_ideal_candidate": sr.ideality.is_ideal_candidate,
        "doc_type_guess": sr.ideality.doc_type_guess,
        "likely_finality": sr.ideality.likely_finality,
        "confidence": sr.ideality.confidence,
        "ideal_reason": sr.ideality.ideal_reason,
        "nonideal_reason": sr.ideality.nonideal_reason,
        "explanation": sr.explain(),
    })
    return d
