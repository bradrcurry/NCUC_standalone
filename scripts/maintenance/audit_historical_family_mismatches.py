from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pdfplumber

from duke_rates.config import get_settings
from duke_rates.historical.family_mismatch_audit import (
    detect_historical_family_mismatch,
)


def _extract_bounded_text(local_path: str, start_page: int | None, end_page: int | None) -> str:
    path = Path(local_path)
    if not path.exists():
        return ""
    with pdfplumber.open(path) as pdf:
        pages = pdf.pages
        if start_page is not None and end_page is not None:
            pages = pages[max(0, start_page - 1): min(len(pages), end_page)]
        elif start_page is not None:
            pages = pages[max(0, start_page - 1): min(len(pages), start_page + 1)]
        text_parts: list[str] = []
        for page in pages:
            text = page.extract_text() or ""
            if text:
                text_parts.append(text)
        return "\n".join(text_parts)


def _load_candidates(db_path: Path, *, family_key: str | None = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = """
        SELECT
            hd.id,
            hd.family_key,
            hd.title,
            hd.state,
            hd.company,
            hd.local_path,
            hd.start_page,
            hd.end_page,
            tf.schedule_code AS family_schedule_code
        FROM historical_documents hd
        LEFT JOIN tariff_families tf ON tf.family_key = hd.family_key
        WHERE hd.local_path IS NOT NULL
        """
        params: list[object] = []
        if family_key:
            query += " AND hd.family_key = ?"
            params.append(family_key)
        query += " ORDER BY hd.id"
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _purge_bad_docs(db_path: Path, historical_ids: list[int]) -> int:
    if not historical_ids:
        return 0
    placeholders = ",".join("?" for _ in historical_ids)
    conn = sqlite3.connect(db_path)
    try:
        version_ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM tariff_versions WHERE historical_document_id IN ({placeholders})",
                historical_ids,
            ).fetchall()
        ]
        if version_ids:
            vph = ",".join("?" for _ in version_ids)
            conn.execute(f"DELETE FROM tariff_charges WHERE version_id IN ({vph})", version_ids)
            conn.execute(f"DELETE FROM tariff_versions WHERE id IN ({vph})", version_ids)
        cur = conn.execute(
            f"DELETE FROM historical_documents WHERE id IN ({placeholders})",
            historical_ids,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit historical_documents rows whose bounded PDF text conflicts with the assigned family."
    )
    parser.add_argument("--family-key", help="Optional family key filter.")
    parser.add_argument("--apply-purge", action="store_true", help="Delete suspicious historical rows and dependent tariff_versions/tariff_charges.")
    args = parser.parse_args()

    settings = get_settings()
    suspicious: list[dict[str, object]] = []
    for row in _load_candidates(settings.database_path, family_key=args.family_key):
        text = _extract_bounded_text(row["local_path"], row["start_page"], row["end_page"])
        if not text:
            continue
        reasons = detect_historical_family_mismatch(
            family_key=row["family_key"],
            family_schedule_code=row["family_schedule_code"],
            text=text,
            state=row["state"] or "NC",
        )
        if reasons:
            suspicious.append(
                {
                    "id": int(row["id"]),
                    "family_key": row["family_key"],
                    "title": row["title"],
                    "company": row["company"],
                    "local_path": row["local_path"],
                    "reasons": reasons,
                }
            )

    print(f"Found {len(suspicious)} suspicious historical family mappings")
    for item in suspicious:
        print(
            f"- historical_id={item['id']} family={item['family_key']} "
            f"reasons={','.join(item['reasons'])} path={item['local_path']}"
        )

    if args.apply_purge and suspicious:
        purged = _purge_bad_docs(settings.database_path, [int(item["id"]) for item in suspicious])
        print(f"\nPurged {purged} suspicious historical_documents rows.")
    elif suspicious:
        print("\nDry run only. Re-run with --apply-purge to delete the suspicious rows.")


if __name__ == "__main__":
    main()
