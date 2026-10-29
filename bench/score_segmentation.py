"""Source-backed D7 formal tariff-component benchmark; no database writes.

Reference scope/provenance is part of every result. Fine-grained legal appendix
boundaries are not scored. Native and actual stored PageEvidence paths are
reported separately; missing artifacts never become an empty successful score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

import fitz
import yaml

from duke_rates.db.artifact_cache import load_page_artifacts
from duke_rates.document_intelligence.bounded import peak_rss_mb
from duke_rates.historical.ncuc.pipeline.page_miner import _extract_page_features_from_text
from duke_rates.historical.ncuc.pipeline.segmentation import segment_document
from duke_rates.historical.ncuc.pipeline.stage_versions import PAGE_ARTIFACT_VERSION


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def boundary_score(expected: set[int], observed: set[int], tolerance: int) -> dict:
    remaining = set(expected)
    hits = 0
    for value in sorted(observed):
        candidates = [p for p in remaining if abs(value - p) <= tolerance]
        if candidates:
            selected = min(candidates, key=lambda p: (abs(value - p), p))
            remaining.remove(selected)
            hits += 1
    precision = ratio(hits, len(observed))
    recall = ratio(hits, len(expected))
    return {
        "tp": hits,
        "fp": len(observed) - hits,
        "fn": len(expected) - hits,
        "precision": precision,
        "recall": recall,
        "f1": ratio(2 * hits, len(observed) + len(expected)),
    }


def score_components(reference: dict, spans: list) -> dict:
    gold = [(x["start"], x["end"]) for x in reference["tariff_components"]]
    predicted = [(x.start_page, x.end_page) for x in spans if x.doc_type == "tariff"]
    positive = {p for start, end in gold for p in range(start, end + 1)}
    proposed = {p for start, end in predicted for p in range(start, end + 1)}
    unclaimed = set(range(len(gold)))
    matched = []
    for start, end in predicted:
        options = []
        for i in unclaimed:
            left, right = gold[i]
            overlap = max(0, min(end, right) - max(start, left) + 1)
            union = max(end, right) - min(start, left) + 1
            if overlap / union >= 0.9:
                options.append((overlap / union, i))
        if options:
            _, i = max(options)
            unclaimed.remove(i)
            matched.append(i)
    ownership = Counter(p for span in spans for p in range(span.start_page, span.end_page + 1))
    expected_boundaries = {p for start, end in gold for p in (start, end + 1)}
    observed_boundaries = {p for start, end in predicted for p in (start, end + 1)}
    return {
        "page_tp": len(positive & proposed),
        "page_fp": len(proposed - positive),
        "page_fn": len(positive - proposed),
        "page_precision": ratio(len(positive & proposed), len(proposed)),
        "page_recall": ratio(len(positive & proposed), len(positive)),
        "false_tariff_pages": sorted(proposed - positive),
        "missed_tariff_pages": sorted(positive - proposed),
        "component_iou_90": {
            "tp": len(matched),
            "fp": len(predicted) - len(matched),
            "fn": len(unclaimed),
            "precision": ratio(len(matched), len(predicted)),
            "recall": ratio(len(matched), len(gold)),
        },
        "boundary_exact": boundary_score(expected_boundaries, observed_boundaries, 0),
        "boundary_within_one": boundary_score(expected_boundaries, observed_boundaries, 1),
        "overlapping_owned_pages": sorted(p for p, count in ownership.items() if count > 1),
        "missing_owned_pages": sorted(set(range(1, reference["page_count"] + 1)) - set(ownership)),
        "span_types": dict(Counter(span.doc_type for span in spans)),
        "predicted_tariff_pages": len(proposed),
        "gold_tariff_pages": len(positive),
    }


def cached_pages(connection, reference: dict, md5: str) -> tuple[list, dict]:
    for digest in (reference["sha256"], md5):
        for alias in reference["source_aliases"]:
            pages = load_page_artifacts(connection, source_pdf=alias, file_hash=digest)
            if pages and [p.page_number for p in pages] == list(
                range(1, reference["page_count"] + 1)
            ):
                return pages, {
                    "status": "complete",
                    "source_alias": alias,
                    "matched_file_hash": digest,
                    "artifact_version": PAGE_ARTIFACT_VERSION,
                }
    return [], {"status": "missing_or_incomplete", "artifact_version": PAGE_ARTIFACT_VERSION}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference",
        type=Path,
        default=Path(__file__).parent / "segmentation/d7_source_reference.yaml",
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raw_reference = args.reference.read_bytes()
    reference = yaml.safe_load(raw_reference)
    connection = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    results = []
    try:
        for item in reference["references"]:
            path = Path(item["source_pdf"])
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError(f"source identity changed: {path}")
            md5 = hashlib.md5(raw).hexdigest()
            with fitz.open(path) as document:
                if len(document) != item["page_count"]:
                    raise ValueError(f"source page count changed: {path}")
                started = time.perf_counter()
                native = [
                    _extract_page_features_from_text(page.get_text("text"), i + 1)
                    for i, page in enumerate(document)
                ]
                native_seconds = time.perf_counter() - started
            started = time.perf_counter()
            artifacts, artifact_status = cached_pages(connection, item, md5)
            cache_seconds = time.perf_counter() - started
            for mode, pages, seconds in (
                ("native", native, native_seconds),
                ("artifact", artifacts, cache_seconds),
            ):
                result = {
                    "source": item["label"],
                    "mode": mode,
                    "input_seconds": seconds,
                    "source_sha256": item["sha256"],
                    "page_count": item["page_count"],
                    "label_source": item["label_source"],
                    "human_verified": item["human_verified"],
                    "reference_scope": item["boundary_scoring_scope"],
                }
                if mode == "artifact":
                    result["artifact_status"] = artifact_status
                if not pages:
                    result["status"] = "not_scored_missing_artifacts"
                    results.append(result)
                    continue
                started = time.perf_counter()
                spans = segment_document(pages)
                result.update(
                    status="scored",
                    segmentation_seconds=time.perf_counter() - started,
                    metrics=score_components(item, spans),
                    spans=[s.model_dump(mode="json") for s in spans],
                )
                results.append(result)
    finally:
        connection.close()
    report = {
        "reference_sha256": hashlib.sha256(raw_reference).hexdigest(),
        "results": results,
        "peak_process_rss_mb": peak_rss_mb(),
        "gpu_vram": "not_measured_no_gpu_conversion",
        "pdf_rendering_or_ocr_in_benchmark": False,
        "database_read_only": True,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps([{k: v for k, v in r.items() if k != "spans"} for r in results], indent=2))


if __name__ == "__main__":
    main()
