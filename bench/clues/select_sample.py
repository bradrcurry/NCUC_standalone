"""Draw the D1 clue benchmark sample and render each page's header/footer bands for hand transcription.

Usage:
    python bench/clues/select_sample.py --db <scratch.db with clue ledger> --out bench/clues/_render

Strata (seeded, deterministic): DEP/DEC, residential / non-residential / rider summary / other rider,
historical (effective before 2015), bundled filings, OCR pages, redline candidates, and documents
whose bound pages print no regulatory clue. Rendering reads the PDF image, never the page-artifact
text, so the transcription is independent of the extractor being scored.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from pathlib import Path

SEED = 20261028
TARGET = 50
QUOTAS = {
    "dep_rider_summary": 4, "dec_rider_summary": 3, "dep_residential": 5, "dec_residential": 4,
    "dep_nonresidential": 5, "dec_nonresidential": 5, "dep_rider": 4, "dec_rider": 3,
    "historical": 5, "bundled": 3, "ocr": 4, "redline": 3, "no_clues": 2,
}
_RES = ("RES", "R-TOU", "R_TOU", "RT", "RS", "RE", "WC", "ES")


def _strata(row: dict) -> list[str]:
    fam = row["family_key"]
    util = "dep" if "progress" in fam else "dec"
    tags = []
    code = (row["schedule_code"] or "").upper()
    if "SUMMARY" in code or fam.endswith("leaf-600"):
        tags.append(f"{util}_rider_summary")
    elif row["family_type"] == "rider":
        tags.append(f"{util}_rider")
    elif code.startswith(_RES) or code in ("RS", "RE", "RT"):
        tags.append(f"{util}_residential")
    else:
        tags.append(f"{util}_nonresidential")
    if (row["effective_start"] or "9999") < "2015":
        tags.append("historical")
    if row["pdf_pages"] and row["pdf_pages"] > 20 and row["span_pages"] < row["pdf_pages"]:
        tags.append("bundled")
    if row["ocr_pages"]:
        tags.append("ocr")
    if row["redline"]:
        tags.append("redline")
    if row["footer_clues"] == 0:
        tags.append("no_clues")
    return tags


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """
        SELECT hd.id AS hd_id, hd.family_key, hd.local_path, hd.start_page, hd.end_page,
               tf.schedule_code, tf.family_type, MIN(tv.effective_start) AS effective_start,
               (SELECT COUNT(*) FROM clue_scan_pages s WHERE s.source_pdf = hd.local_path
                  AND s.page BETWEEN COALESCE(hd.start_page, 1) AND COALESCE(hd.end_page, hd.start_page, 100000))
                  AS span_pages,
               (SELECT COUNT(*) FROM clue_scan_pages s WHERE s.source_pdf = hd.local_path AND s.text_layer = 'ocr'
                  AND s.page BETWEEN COALESCE(hd.start_page, 1) AND COALESCE(hd.end_page, hd.start_page, 100000))
                  AS ocr_pages,
               (SELECT COUNT(*) FROM clue_associations a JOIN regulatory_clues c ON c.id = a.clue_id
                  WHERE a.historical_document_id = hd.id
                  AND c.clue_kind IN ('revision', 'supersedes', 'effective_date', 'order_date', 'docket')
                  AND COALESCE(c.role, '') <> 'body') AS footer_clues,
               (SELECT MAX(is_redline_candidate) FROM document_fingerprints f WHERE f.source_pdf = hd.local_path)
                  AS redline
        FROM historical_documents hd
        JOIN tariff_versions tv ON tv.historical_document_id = hd.id
        LEFT JOIN tariff_families tf ON tf.family_key = hd.family_key
        WHERE hd.family_key LIKE 'nc-%'
        GROUP BY hd.id
        """
    )]
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("PyMuPDF is required to render pages") from exc
    eligible = []
    for r in rows:
        if r["span_pages"] == 0 or not Path(r["local_path"]).is_file():
            continue
        with fitz.open(r["local_path"]) as doc:
            r["pdf_pages"] = doc.page_count
        r["strata"] = _strata(r)
        eligible.append(r)
    rng = random.Random(SEED)
    rng.shuffle(eligible)
    chosen: list[dict] = []
    filled = {k: 0 for k in QUOTAS}
    for stratum in sorted(QUOTAS, key=lambda k: (QUOTAS[k], k)):
        for r in eligible:
            if filled[stratum] >= QUOTAS[stratum] or len(chosen) >= TARGET:
                break
            if stratum in r["strata"] and r not in chosen:
                chosen.append(r)
                for tag in r["strata"]:
                    if tag in filled:
                        filled[tag] += 1
    for r in eligible:
        if len(chosen) >= TARGET:
            break
        if r not in chosen:
            chosen.append(r)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, r in enumerate(chosen, start=1):
        # The page to transcribe: the first span page that is not an index page.
        page = conn.execute(
            "SELECT MIN(page) FROM clue_scan_pages WHERE source_pdf = ? AND page BETWEEN ? AND ? AND is_index_page = 0",
            (r["local_path"], r["start_page"] or 1, r["end_page"] or r["start_page"] or r["pdf_pages"]),
        ).fetchone()[0] or (r["start_page"] or 1)
        with fitz.open(r["local_path"]) as doc:
            pg = doc[page - 1]
            w, h = pg.rect.width, pg.rect.height
            bands = [fitz.Rect(0, 0, w, h * 0.24), fitz.Rect(0, h * 0.78, w, h)]
            pix = [pg.get_pixmap(clip=b, dpi=100) for b in bands]
            canvas = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, max(p.width for p in pix), sum(p.height for p in pix) + 6), False)
            canvas.clear_with(200)
            y = 0
            for p in pix:
                p2 = fitz.Pixmap(fitz.csRGB, p) if p.n != 3 or p.alpha else p
                p2.set_origin(0, y)
                canvas.copy(p2, p2.irect)
                y += p.height + 6
            canvas.save(out / f"s{i:02d}_hd{r['hd_id']}_p{page}.png")
        manifest.append({"sample": i, "hd_id": r["hd_id"], "family_key": r["family_key"], "source_pdf": r["local_path"],
                         "page": page, "strata": r["strata"], "span": [r["start_page"], r["end_page"]]})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(json.dumps({"chosen": len(chosen), "filled": filled}, indent=1))


if __name__ == "__main__":
    main()
