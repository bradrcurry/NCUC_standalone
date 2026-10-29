"""Render PDF pages (or header/footer bands) to PNG for hand verification.

    python bench/clues/render_pages.py OUT_DIR PDF PAGE[,PAGE...] [--bands] [--dpi 100]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import fitz


def render(pdf: str, pages: list[int], out_dir: Path, *, bands: bool, dpi: int, tag: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with fitz.open(pdf) as doc:
        for page_no in pages:
            pg = doc[page_no - 1]
            w, h = pg.rect.width, pg.rect.height
            clips = [fitz.Rect(0, 0, w, h * 0.24), fitz.Rect(0, h * 0.78, w, h)] if bands else [pg.rect]
            pix = [pg.get_pixmap(clip=c, dpi=dpi) for c in clips]
            width = max(p.width for p in pix)
            canvas = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, sum(p.height for p in pix) + 6 * len(pix)), False)
            canvas.clear_with(200)
            y = 0
            for p in pix:
                p = fitz.Pixmap(fitz.csRGB, p) if p.alpha or p.n != 3 else p
                p.set_origin(0, y)
                canvas.copy(p, p.irect)
                y += p.height + 6
            path = out_dir / f"{tag}_p{page_no}.png"
            canvas.save(path)
            written.append(path)
    return written


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("pdf")
    ap.add_argument("pages")
    ap.add_argument("--bands", action="store_true")
    ap.add_argument("--dpi", type=int, default=100)
    ap.add_argument("--tag", default="page")
    a = ap.parse_args()
    for p in render(a.pdf, [int(x) for x in a.pages.split(",")], Path(a.out_dir), bands=a.bands, dpi=a.dpi, tag=a.tag):
        print(p)
