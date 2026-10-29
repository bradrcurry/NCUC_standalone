"""No-network, no-database native redline benchmark against attributed page reviews.

Run: .venv312/Scripts/python.exe bench/redline_native.py --out PATH
Labels are assistant source reviews, not independently human-verified ground truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

import fitz

from duke_rates.parse.redline_detector import PAGE_EVIDENCE_VERSION, evaluate_redline_page

ROOT = Path(__file__).resolve().parents[1]


def _score(rows: list[dict], verdict_key: str) -> dict:
    tp = fp = fn = tn = abstained = 0
    for row in rows:
        verdict = row[verdict_key]
        if verdict == "no_text_layer":
            abstained += 1
            continue
        positive = verdict == "redline"
        truth = row["observed_edit_markup"]
        tp += int(positive and truth)
        fp += int(positive and not truth)
        fn += int(not positive and truth)
        tn += int(not positive and not truth)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "abstained": abstained,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall_on_assessed_pages": tp / (tp + fn) if tp + fn else None,
        "positive_label_coverage": (tp + fn) / max(1, sum(r["observed_edit_markup"] for r in rows)),
        "assessment_coverage": (len(rows) - abstained) / max(1, len(rows)),
    }


def run(manifest_path: Path, repeats: int = 5) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    for item in manifest["pages"]:
        path = ROOT / item["fixture"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["fixture_sha256"]:
            raise ValueError(f"Fixture hash mismatch: {item['id']}")
    timings, rows = [], []
    for iteration in range(repeats):
        current = []
        started = time.perf_counter()
        for item in manifest["pages"]:
            with fitz.open(ROOT / item["fixture"]) as doc:
                evidence = evaluate_redline_page(doc[0], item["original_page"])
            current.append(
                {
                    **item,
                    "verdict": evidence.verdict,
                    "classification": evidence.classification,
                    "page_attribution_correct": evidence.page == item["original_page"],
                    "insufficient_attribution_correct": (evidence.verdict == "no_text_layer")
                    == item["native_assessment_insufficient"],
                    "evidence": evidence.signals(),
                }
            )
        timings.append(time.perf_counter() - started)
        if iteration and [(r["verdict"], r["classification"]) for r in rows] != [
            (r["verdict"], r["classification"]) for r in current
        ]:
            raise AssertionError(
                "Native classifier output changed across repeated identical inputs"
            )
        rows = current
    return {
        "detector": PAGE_EVIDENCE_VERSION,
        "label_source": manifest["label_source"],
        "human_verified": manifest["human_verified"],
        "page_count": len(rows),
        "rendered_pages_for_detection": 0,
        "ocr_calls_for_detection": 0,
        "review_method": (
            "assistant inspection of rendered original pages plus source text; "
            "labels frozen before detector changes"
        ),
        "baseline_metrics": _score(rows, "baseline_verdict"),
        "metrics": _score(rows, "verdict"),
        "page_attribution_correct": sum(r["page_attribution_correct"] for r in rows),
        "insufficient_attribution_correct": sum(
            r["insufficient_attribution_correct"] for r in rows
        ),
        "elapsed_seconds": timings,
        "median_seconds": statistics.median(timings),
        "pages": rows,
        "limitations": [
            "Small curated regression corpus reused during development; not a held-out evaluation.",
            "Assistant labels require independent human verification.",
            "No detected edit markup does not establish regulatory approval.",
            "Image-dominant pages abstain even with an OCR text layer.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "tests/fixtures/d5_redline/manifest.json"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    report = run(args.manifest, args.repeats)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(json.dumps({k: v for k, v in report.items() if k != "pages"}, indent=2))


if __name__ == "__main__":
    main()
