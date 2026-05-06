#!/usr/bin/env python
"""
Post-Docling fingerprinting: Quality assessment and redline detection for artifacts.

Run after process-docling-batch completes. Classifies documents and returns
HQ-only subset for ingestion.

Efficiency strategy:
  - Single pass through docling_artifacts
  - Regex-based redline detection on plain_text
  - OCR confidence thresholds
  - Stores classification in database
  - Returns filtering recommendations
"""

import sqlite3
import re
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

# Redline markers from previous fingerprinting work
REDLINE_MARKERS = {
    "DRAFT": re.compile(r"\bDRAFT\b", re.IGNORECASE),
    "PROPOSED": re.compile(r"\bPROPOSED\b", re.IGNORECASE),
    "NEW": re.compile(r"\b(NEW|REVISE[D]?)\b", re.IGNORECASE),
    "OLD": re.compile(r"\bOLD\b", re.IGNORECASE),
    "REDLINED": re.compile(r"\b(REDLINE|REDLINED|REDLINE[D])\b", re.IGNORECASE),
}

# Dual-rate pattern (most reliable redline signal in text layer)
DUAL_RATE_PATTERN = re.compile(r"\b(\d+\.\d+)\s*/\s*(\d+\.\d+)\b")

# OCR indicators (poor quality signal)
OCR_INDICATORS = re.compile(
    r"(OCR|optical.character|garbled|unrecognized|[?]{3,}|[-]{5,})",
    re.IGNORECASE,
)

@dataclass
class ArtifactFingerprint:
    """Quality assessment for a Docling artifact."""
    artifact_id: int
    discovery_id: Optional[int]
    file_path: str

    # Redline signals
    has_redline_markers: bool = False
    redline_marker_types: list = None
    has_dual_rates: bool = False
    dual_rate_count: int = 0
    redline_confidence: float = 0.0

    # Quality signals
    page_count: int = 0
    conversion_confidence: float = 1.0
    has_ocr: bool = False
    ocr_quality_issue: bool = False

    # Classification
    quality_tier: str = "UNKNOWN"  # HQ, UNCERTAIN, REDLINE, SCANNED
    recommendation: str = "INGEST"  # INGEST, REVIEW, SKIP

    def __post_init__(self):
        if self.redline_marker_types is None:
            self.redline_marker_types = []

def fingerprint_artifact(artifact_data: dict) -> ArtifactFingerprint:
    """Analyze a single Docling artifact for quality and redline signals."""
    artifact_id = artifact_data["id"]
    discovery_id = artifact_data.get("discovery_record_id")
    file_path = artifact_data.get("source_file_path", "unknown")

    fp = ArtifactFingerprint(
        artifact_id=artifact_id,
        discovery_id=discovery_id,
        file_path=file_path,
    )

    # Parse Docling output
    doc_json = artifact_data.get("doc_json_content")
    plain_text = artifact_data.get("plain_text_content", "")

    if doc_json:
        try:
            doc = json.loads(doc_json) if isinstance(doc_json, str) else doc_json
            fp.page_count = doc.get("pages", {}).get("count", 0) or len(
                doc.get("pages", [])
            )
            fp.conversion_confidence = doc.get("conversion_confidence", 1.0)
        except (json.JSONDecodeError, TypeError):
            pass

    # Check for OCR usage (indicates scanned document)
    if "Tesseract" in str(artifact_data.get("conversion_status", "")):
        fp.has_ocr = True

    # Scan text for redline markers
    if plain_text:
        for marker_name, pattern in REDLINE_MARKERS.items():
            if pattern.search(plain_text):
                fp.has_redline_markers = True
                fp.redline_marker_types.append(marker_name)

        # Check for dual-rate patterns (most reliable redline signal)
        dual_rates = DUAL_RATE_PATTERN.findall(plain_text)
        if dual_rates:
            fp.has_dual_rates = True
            fp.dual_rate_count = len(dual_rates)

        # Check for OCR quality issues
        if fp.has_ocr and OCR_INDICATORS.search(plain_text):
            fp.ocr_quality_issue = True

    # Calculate redline confidence
    if fp.has_dual_rates:
        fp.redline_confidence += 0.6  # Strongest signal
    if fp.has_redline_markers:
        fp.redline_confidence += 0.3
    if fp.ocr_quality_issue:
        fp.redline_confidence += 0.1

    fp.redline_confidence = min(fp.redline_confidence, 1.0)

    # Classify and recommend
    if fp.dual_rate_count >= 2 or (fp.redline_confidence > 0.5):
        fp.quality_tier = "REDLINE"
        fp.recommendation = "REVIEW"
    elif fp.ocr_quality_issue:
        fp.quality_tier = "SCANNED"
        fp.recommendation = "REVIEW"
    elif fp.conversion_confidence < 0.7:
        fp.quality_tier = "UNCERTAIN"
        fp.recommendation = "REVIEW"
    else:
        fp.quality_tier = "HQ"
        fp.recommendation = "INGEST"

    return fp

def run_fingerprinting(limit: Optional[int] = None, verbose: bool = True):
    """Run fingerprinting on all Docling artifacts."""
    conn = sqlite3.connect("data/db/duke_rates.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Check if docling_artifacts table exists
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='docling_artifacts'"
    )
    if not cursor.fetchone():
        print("ERROR: docling_artifacts table not found. Has Docling processing completed?")
        return None

    # Fetch all artifacts (or limited set)
    query = "SELECT * FROM docling_artifacts"
    if limit:
        query += f" LIMIT {limit}"

    cursor.execute(query)
    artifacts = cursor.fetchall()

    if not artifacts:
        print("No Docling artifacts found in database.")
        return None

    if verbose:
        print(f"\n{'='*70}")
        print(f"FINGERPRINTING {len(artifacts)} DOCLING ARTIFACTS")
        print(f"{'='*70}\n")

    # Fingerprint each artifact
    results = []
    stats = defaultdict(int)

    for i, artifact_row in enumerate(artifacts, 1):
        artifact_dict = dict(artifact_row)
        fp = fingerprint_artifact(artifact_dict)
        results.append(fp)
        stats[fp.quality_tier] += 1
        stats[f"{fp.recommendation}_count"] += 1

        if verbose and (i == 1 or i % 100 == 0 or i == len(artifacts)):
            print(f"  [{i}/{len(artifacts)}] {fp.file_path.split(chr(92))[-1]}: {fp.quality_tier} > {fp.recommendation}")

    if verbose:
        print(f"\n{'='*70}")
        print(f"FINGERPRINTING RESULTS")
        print(f"{'='*70}\n")
        print(f"Total processed: {len(results)}")
        print(f"  HQ (ingest): {stats['HQ']}")
        print(f"  Uncertain (review): {stats['UNCERTAIN']}")
        print(f"  Redline (review): {stats['REDLINE']}")
        print(f"  Scanned (review): {stats['SCANNED']}")
        print()
        print(f"Recommendation breakdown:")
        print(f"  INGEST: {stats['INGEST_count']} documents")
        print(f"  REVIEW: {stats['REVIEW_count']} documents")
        print(f"  SKIP: {stats['SKIP_count']} documents")
        print()

        # Show redline detections
        redlines = [r for r in results if r.quality_tier == "REDLINE"]
        if redlines:
            print(f"Redline detections ({len(redlines)}):")
            for r in redlines[:5]:
                markers = ", ".join(r.redline_marker_types) if r.redline_marker_types else "none"
                print(f"  - {r.file_path.split(chr(92))[-1]}")
                print(f"    Markers: {markers}, Dual rates: {r.dual_rate_count}, Confidence: {r.redline_confidence:.2f}")
            if len(redlines) > 5:
                print(f"  ... and {len(redlines) - 5} more")
            print()

    conn.close()

    return {
        "results": results,
        "stats": dict(stats),
        "hq_count": stats["HQ"],
        "review_count": stats["REVIEW_count"],
        "skip_count": stats["SKIP_count"],
    }

def get_ingest_recommendations(fingerprinting_results: dict) -> dict:
    """Convert fingerprinting results to ingest filter."""
    results = fingerprinting_results["results"]

    hq_artifacts = [r.artifact_id for r in results if r.recommendation == "INGEST"]
    review_artifacts = [r.artifact_id for r in results if r.recommendation == "REVIEW"]
    skip_artifacts = [r.artifact_id for r in results if r.recommendation == "SKIP"]

    redline_artifacts = [r.artifact_id for r in results if r.quality_tier == "REDLINE"]
    scanned_artifacts = [r.artifact_id for r in results if r.quality_tier == "SCANNED"]

    return {
        "total_artifacts": len(results),
        "hq_artifacts": hq_artifacts,
        "review_artifacts": review_artifacts,
        "skip_artifacts": skip_artifacts,
        "redline_artifacts": redline_artifacts,
        "scanned_artifacts": scanned_artifacts,
        "hq_count": len(hq_artifacts),
        "review_count": len(review_artifacts),
        "skip_count": len(skip_artifacts),
        "hq_percentage": round(100 * len(hq_artifacts) / len(results), 1) if results else 0,
    }

if __name__ == "__main__":
    results = run_fingerprinting(verbose=True)

    if results:
        recommendations = get_ingest_recommendations(results)

        print(f"\nPHASE 2 INGESTION RECOMMENDATIONS")
        print(f"{'='*70}\n")
        print(f"HQ documents to ingest: {recommendations['hq_count']} ({recommendations['hq_percentage']}%)")
        print(f"Documents to review: {recommendations['review_count']}")
        print(f"Documents to skip: {recommendations['skip_count']}")
        print()
        print(f"Next command (ingest HQ only):")
        print(f"  python -m duke_rates ingest-ncuc --accelerator cuda --limit {recommendations['hq_count']}")
        print()
        print(f"Or ingest all with tracking:")
        print(f"  python -m duke_rates ingest-ncuc --persist --replace")
