"""Phase 2D: cut each benchmark document's span into one excerpt PDF.

Every Docling environment under test converts the *same* excerpt bytes, so a
difference between environments cannot come from a different PDF cutter
(PyMuPDF 1.27 vs 2.0). The span is the union of the transcribed pages and the
DB ``hd_bounds``; documents whose span is the whole file are copied unchanged.
Run once, with the validated environment::

    .venv312/Scripts/python.exe bench/phase2d/make_excerpts.py --out <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import fitz
import yaml

BENCH = Path(__file__).resolve().parents[1]
ROOT = BENCH.parent


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pdf_for(entry: dict) -> Path | None:
    for key in ("bench_pdf", "source_path"):
        if entry.get(key) and (ROOT / entry[key]).exists() and _sha(ROOT / entry[key]) == entry["sha256"]:
            return ROOT / entry[key]
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for entry in yaml.safe_load((BENCH / "corpus.yaml").read_text(encoding="utf-8"))["docs"]:
        src = _pdf_for(entry)
        bounds = [b for b in (entry.get("pages"), entry.get("hd_bounds")) if b]
        start, end = min(b[0] for b in bounds), max(b[1] for b in bounds)
        with fitz.open(src) as doc:
            total = doc.page_count
            target = out / f"{entry['doc_id']}.pdf"
            if (start, end) == (1, total):
                shutil.copy2(src, target)
            else:
                part = fitz.open()
                part.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
                part.save(target, garbage=0, deflate=False)
                part.close()
        manifest.append({
            "doc_id": entry["doc_id"], "source_pdf": str(src.relative_to(ROOT)), "source_sha256": entry["sha256"],
            "source_pages": total, "span": [start, end], "page_offset": start - 1,
            "excerpt": target.name, "excerpt_sha256": _sha(target), "excerpt_pages": end - start + 1,
        })
        print(f"{entry['doc_id']:28s} {start:>3}-{end:<3} of {total}")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
