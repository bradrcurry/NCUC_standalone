"""Backfill historical_documents.raw_text_path from existing .txt / .ocr.txt sidecars.

For NC historical_documents where raw_text_path IS NULL but a sidecar text file
already exists on disk at:
    <local_path>.txt          (native-text extraction)
    <local_path>.ocr.txt      (Tesseract OCR fallback)

We pick whichever sidecar is larger (handles the common case where a scanned
PDF has a tiny .txt of just header noise plus a real .ocr.txt with content).

Dry-run by default; pass --apply to write. Prints per-family counts and a
sample of the chosen paths.

Companion to the 2026-05-14 fix (which only checked .txt). Most of the 311
docs still missing raw_text_path are scanned PDFs with .ocr.txt sidecars that
the earlier pass missed.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from collections import Counter
from pathlib import Path

DB_PATH = Path("data/db/duke_rates.db")

# Minimum content size to be considered "real text". A 200-byte .txt on a
# scanned PDF is usually just the PDF metadata, not content.
MIN_BYTES = 200


def pick_sidecar(local_path: str) -> tuple[str | None, int]:
    """Return (chosen_sidecar_path, size_bytes) or (None, 0) if none usable."""
    candidates: list[tuple[str, int]] = []
    for suffix in (".txt", ".ocr.txt"):
        p = local_path + suffix
        if os.path.exists(p):
            sz = os.path.getsize(p)
            if sz >= MIN_BYTES:
                candidates.append((p, sz))
    if not candidates:
        return None, 0
    # Prefer larger sidecar
    candidates.sort(key=lambda x: -x[1])
    return candidates[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Actually write changes")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N rows (0=all)")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, family_key, local_path
        FROM historical_documents
        WHERE family_key LIKE 'nc-%'
          AND (raw_text_path IS NULL OR raw_text_path = '')
          AND local_path IS NOT NULL
        ORDER BY family_key, id
        """
    ).fetchall()
    print(f"NC docs missing raw_text_path: {len(rows)}")

    updates: list[tuple[int, str, str, int]] = []  # (id, family, sidecar, size)
    no_sidecar: Counter[str] = Counter()
    pdf_missing = 0
    for r in rows:
        rid, fam, lp = r["id"], r["family_key"], r["local_path"]
        if not os.path.exists(lp):
            pdf_missing += 1
            continue
        sidecar, sz = pick_sidecar(lp)
        if sidecar:
            updates.append((rid, fam, sidecar, sz))
        else:
            no_sidecar[fam] += 1

    print(f"  fixable (has sidecar): {len(updates)}")
    print(f"  needs OCR (no sidecar): {sum(no_sidecar.values())}")
    print(f"  PDF missing from disk: {pdf_missing}")

    by_fam: Counter[str] = Counter()
    for _, fam, _, _ in updates:
        by_fam[fam] += 1
    print()
    print("Top fixable families:")
    for fam, n in by_fam.most_common(20):
        print(f"  {n:>4}  {fam}")

    if args.limit and len(updates) > args.limit:
        updates = updates[: args.limit]
        print(f"\n(limited to first {args.limit} rows)")

    print()
    print("Sample (id, family, sidecar, bytes):")
    for u in updates[:5]:
        print(f"  id={u[0]:>6}  {u[1]:<40}  {u[2]} ({u[3]:,} bytes)")

    if not args.apply:
        print()
        print("DRY RUN — pass --apply to write")
        return

    print()
    print(f"Applying {len(updates)} updates...")
    cur = conn.cursor()
    for rid, _, sidecar, _ in updates:
        cur.execute(
            "UPDATE historical_documents SET raw_text_path = ? WHERE id = ?",
            (sidecar, rid),
        )
    conn.commit()
    conn.close()
    print(f"Done. Updated {len(updates)} rows.")


if __name__ == "__main__":
    main()
