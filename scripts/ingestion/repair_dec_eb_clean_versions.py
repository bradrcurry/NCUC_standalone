from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

DB_PATH = ROOT / "data" / "db" / "duke_rates.db"

REPAIRS = [
    {
        "historical_document_id": 2601,
        "version_id": 5901,
        "family_key": "nc-carolinas-rider-EB",
        "effective_start": "2015-10-27",
        "title": "DEC Rider EB clean tariff (Sub 1093, pages 2-3)",
        "local_path": ROOT
        / "data"
        / "historical"
        / "ncuc"
        / "dec"
        / "E-7_Sub_1093"
        / "11-4-2015_0c8a6382_E-7_Sub_1093_DEC_EB_tariff_110415.pdf",
        "start_page": 2,
        "end_page": 3,
        "leaf_no": "226",
        "revision_label": "North Carolina Original Leaf No. 226",
        "supersedes_label": None,
        "docket_number": "E-7 Sub 1093",
    },
    {
        "historical_document_id": 2602,
        "version_id": 5902,
        "family_key": "nc-carolinas-rider-EB",
        "effective_start": "2022-09-29",
        "title": "DEC Rider EB clean tariff (Sub 1274, pages 3-6)",
        "local_path": ROOT
        / "data"
        / "historical"
        / "ncuc"
        / "dec"
        / "E-7_Sub_1093"
        / "10-7-2022_94724be3_E-7_Sub_1274_DEC_EnergyWise_for_Business_Program_Tariff.pdf",
        "start_page": 3,
        "end_page": 6,
        "leaf_no": "226",
        "revision_label": "North Carolina First Revised Leaf No. 226",
        "supersedes_label": "North Carolina Original Leaf No. 226",
        "docket_number": "E-7 Sub 1274",
    },
]


def apply_repairs() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        for spec in REPAIRS:
            row = conn.execute(
                """
                SELECT id, family_key, local_path, effective_start, start_page, end_page
                FROM historical_documents
                WHERE id = ?
                """,
                (spec["historical_document_id"],),
            ).fetchone()
            if not row:
                raise RuntimeError(f"historical_document {spec['historical_document_id']} not found")
            conn.execute(
                """
                UPDATE historical_documents
                SET title = ?,
                    local_path = ?,
                    start_page = ?,
                    end_page = ?,
                    leaf_no = ?,
                    revision_label = ?,
                    supersedes_label = ?
                WHERE id = ?
                """,
                (
                    spec["title"],
                    str(spec["local_path"]),
                    spec["start_page"],
                    spec["end_page"],
                    spec["leaf_no"],
                    spec["revision_label"],
                    spec["supersedes_label"],
                    spec["historical_document_id"],
                ),
            )
            conn.execute(
                """
                UPDATE tariff_versions
                SET leaf_no = ?, docket_number = ?, source_pdf = ?, docket_dir = ?
                WHERE id = ?
                """,
                (
                    spec["leaf_no"],
                    spec["docket_number"],
                    str(spec["local_path"]),
                    str(Path(spec["local_path"]).parent),
                    spec["version_id"],
                ),
            )
            print(
                f"UPDATED hd={spec['historical_document_id']} tv={spec['version_id']} "
                f"pages={spec['start_page']}-{spec['end_page']} path={Path(spec['local_path']).name}"
            )
        conn.commit()
    finally:
        conn.close()


def extract_repairs() -> None:
    extractor = BulkExtractor(str(DB_PATH))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        for spec in REPAIRS:
            doc = conn.execute(
                """
                SELECT
                    hd.id,
                    hd.family_key,
                    hd.title,
                    hd.company,
                    hd.state,
                    hd.local_path,
                    hd.content_hash,
                    hd.effective_start,
                    hd.revision_label,
                    hd.supersedes_label,
                    hd.leaf_no,
                    hd.start_page,
                    hd.end_page,
                    NULL AS discovery_record_id,
                    tv.docket_number AS docket_number,
                    'manual_registration' AS acquisition_method,
                    'T1' AS discovery_doc_quality_tier,
                    tv.id AS version_id
                FROM historical_documents hd
                JOIN tariff_versions tv ON tv.historical_document_id = hd.id
                WHERE hd.id = ?
                ORDER BY tv.id DESC
                LIMIT 1
                """,
                (spec["historical_document_id"],),
            ).fetchone()
            if not doc:
                raise RuntimeError(f"joined historical_document {spec['historical_document_id']} not found")
            print(
                f"Extracting [{doc['id']}] {doc['family_key']} "
                f"p{doc['start_page']}-{doc['end_page']} eff={doc['effective_start']} ... ",
                end="",
                flush=True,
            )
            _doc_id, _family_key, inserted = extractor.process_document(dict(doc))
            print(f"{inserted} charges")
    finally:
        conn.close()


def main() -> None:
    apply_repairs()
    extract_repairs()


if __name__ == "__main__":
    main()
