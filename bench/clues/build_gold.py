"""Assemble bench/clues/sample_50.yaml from the hand transcription and the sample manifest.

    python bench/clues/build_gold.py --manifest <render dir>/manifest.json

Adds each document's file SHA-256 (computed from the PDF on disk) so the scorer can refuse
to score when the cited source is missing or changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

HERE = Path(__file__).parent

#: Rule-verified links checked by hand against the successor and predecessor page images.
LINK_REVIEWS = [
    {"link_id": 38, "from_version_id": 5357, "to_version_id": 6548, "verdict": "confirmed",
     "basis": "successor prints 'Superseding NC Fifth Revised Leaf No. 600' (eff 2025-04-01); predecessor prints 'NC Fifth Revised Leaf No. 600' (eff 2025-01-01)"},
    {"link_id": 121, "from_version_id": 6057, "to_version_id": 6548, "verdict": "confirmed",
     "basis": "same Sixth Revised Leaf 600 sheet as link 38 (second copy, f76e6a9b p.8)"},
    {"link_id": 146, "from_version_id": 6737, "to_version_id": 5997, "verdict": "confirmed",
     "basis": "DEC Sixty-Second Revised Leaf 99 (eff 2024-09-01) supersedes Sixty-First; predecessor prints Sixty-First (eff 2024-07-01)"},
    {"link_id": 7, "from_version_id": 5300, "to_version_id": 5299, "verdict": "confirmed",
     "basis": "Leaf 500 Second Revised (2025-10-01) supersedes First Revised; predecessor First Revised (2024-10-01)"},
    {"link_id": 20, "from_version_id": 5329, "to_version_id": 6825, "verdict": "confirmed",
     "basis": "Leaf 528 Second Revised supersedes First Revised; predecessor First Revised (2024-10-01)"},
    {"link_id": 128, "from_version_id": 6181, "to_version_id": 6176, "verdict": "confirmed",
     "basis": "R-TOUD-12 sheet 3 prints 'Supersedes Schedule R-TOUD-10B'; predecessor is SCHEDULE R-TOUD-10B (CP&L)"},
    {"link_id": 91, "from_version_id": 5608, "to_version_id": 5596, "verdict": "confirmed",
     "basis": "TSS sheet p.51 footer 'Supersedes Schedule TSS-47' (eff 2018-06-01); predecessor SCHEDULE TSS-47"},
    {"link_id": 98, "from_version_id": 5716, "to_version_id": 7274, "verdict": "confirmed",
     "basis": "Leaf 605 First Revised (2023-12-01) supersedes Original; predecessor Original Leaf 605 (2022-12-01)"},
    {"link_id": 88, "from_version_id": 5565, "to_version_id": 6924, "verdict": "confirmed",
     "basis": "Original Leaf 663 prints 'Superseding NC Rider SRR-7'; predecessor RIDER SRR-7"},
    {"link_id": 170, "from_version_id": 7189, "to_version_id": 7272, "verdict": "confirmed",
     "basis": "Rider CPRE-2 prints 'Supersedes Schedule CPRE-1'; predecessor Rider CPRE-1 (eff 2020-12-07)"},
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, default=HERE / "sample_manifest.json")
    a = ap.parse_args()
    manifest = json.loads(a.manifest.read_text(encoding="utf-8"))
    transcription = yaml.safe_load((HERE / "transcription.yaml").read_text(encoding="utf-8"))
    documents = []
    for m in manifest:
        t = transcription[m["sample"]]
        data = Path(m["source_pdf"]).read_bytes()
        doc = {
            "sample": m["sample"],
            "hd_id": m["hd_id"],
            "family_key": m["family_key"],
            "source_pdf": str(Path(m["source_pdf"])),
            "sha256": hashlib.sha256(data).hexdigest(),
            "page": m["page"],
            "strata": m["strata"],
        }
        if "sheet_of_family" in t:
            doc["sheet_of_family"] = t["sheet_of_family"]
        if t.get("notes"):
            doc["notes"] = " ".join(str(t["notes"]).split())
        doc["clues"] = t.get("clues") or []
        documents.append(doc)
    gold = {
        "version": 1,
        "transcribed_by": "Claude (assistant), 2026-10-28, from rendered PDF page images",
        "verified_from": "page image: top 24 % and bottom 22 % bands at 100 dpi (render via bench/clues/render_pages.py --bands)",
        "scope": ["revision", "supersedes", "effective_date", "docket (rate-sheet footer)", "order_date (rate-sheet footer)"],
        "sampling": "bench/clues/select_sample.py, seed 20261028, stratified",
        "documents": documents,
        "link_reviews": LINK_REVIEWS,
    }
    out = HERE / "sample_50.yaml"
    out.write_text(yaml.safe_dump(gold, sort_keys=False, allow_unicode=True, width=140), encoding="utf-8")
    print(f"wrote {out} ({len(documents)} documents, {sum(len(d['clues']) for d in documents)} clues)")


if __name__ == "__main__":
    main()
