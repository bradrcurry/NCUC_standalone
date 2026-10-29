"""Cut the D2 redline regression pages, unchanged, from their source PDFs.

Each fixture is one page copied with PyMuPDF ``insert_pdf`` (no re-rendering),
so text spans, colours and drawn bars are the originals.

| Fixture | Source (data/historical/ncuc/e-2-sub-1300-sept2023/) | Page | What it is |
|---|---|---|---|
| hd2275_leaf600_redline_p100.pdf | 1b72e8de-c435-43b7-9b88-dfc4c0a8a97b.pdf | 100 | hd 2275 rebind target: NC ~~Original~~ First Revised Leaf 600, struck RAL-2 rows (D1 §10) |
| hd7265_leaf600_clean_p96.pdf    | 7b9171ff-48d4-46dd-82eb-43bd1ca2cd3d.pdf | 96  | hd 7265 rebind target: clean First Revised Leaf 600 (control) |
| leaf660_redline_p221.pdf        | 7f394a37-a20d-4da5-b97d-0035c9d9c0c0.pdf | 221 | D1 spot-check #10: Original Leaf 660, redline scored ``full`` |
| leaf661_redline_p226.pdf        | 7f394a37-a20d-4da5-b97d-0035c9d9c0c0.pdf | 226 | D1 spot-check #19: struck "RIDER MROP-16A" matched as identity |

    python tests/fixtures/d2_redline/make_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "data" / "historical" / "ncuc" / "e-2-sub-1300-sept2023"
HERE = Path(__file__).resolve().parent
PAGES = {
    "hd2275_leaf600_redline_p100.pdf": ("1b72e8de-c435-43b7-9b88-dfc4c0a8a97b.pdf", 100),
    "hd7265_leaf600_clean_p96.pdf": ("7b9171ff-48d4-46dd-82eb-43bd1ca2cd3d.pdf", 96),
    "leaf660_redline_p221.pdf": ("7f394a37-a20d-4da5-b97d-0035c9d9c0c0.pdf", 221),
    "leaf661_redline_p226.pdf": ("7f394a37-a20d-4da5-b97d-0035c9d9c0c0.pdf", 226),
}


def main() -> None:
    for name, (source, page) in PAGES.items():
        src = fitz.open(SRC / source)
        out = fitz.open()
        out.insert_pdf(src, from_page=page - 1, to_page=page - 1)
        out.save(HERE / name, garbage=3, deflate=True)
        out.close()
        src.close()


if __name__ == "__main__":
    main()
