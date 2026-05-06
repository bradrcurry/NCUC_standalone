"""
Targeted extraction for newly registered Sub 1152 and Sub 1214 schedule slices.
Processes hd IDs 2521-2542 directly via BulkExtractor.process_document.
"""
import sys
import sqlite3
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# Add project src to path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

DB_PATH = str(ROOT / "data" / "db" / "duke_rates.db")
NEW_HD_RANGE = range(2521, 2543)  # hd IDs 2521-2542


def main():
    extractor = BulkExtractor(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Build doc dicts matching what get_documents_needing_extraction returns
    query = """
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
        WHERE hd.id >= 2521 AND hd.id <= 2542
        ORDER BY hd.family_key, hd.effective_start
    """
    docs = [dict(r) for r in conn.execute(query).fetchall()]
    conn.close()

    print(f"Processing {len(docs)} new schedule slice documents")
    print()

    total_charges = 0
    for doc in docs:
        hd_id = doc["id"]
        fk = doc["family_key"]
        pages = f"p{doc.get('start_page')}-{doc.get('end_page')}"
        eff = doc.get("effective_start")
        print(f"  [{hd_id}] {fk} {pages} eff={eff}... ", end="", flush=True)
        try:
            doc_id, family_key, num_inserted = extractor.process_document(doc)
            total_charges += num_inserted
            print(f"{num_inserted} charges")
        except Exception as e:
            print(f"ERROR: {e}")

    print()
    print(f"Done. Total charges inserted: {total_charges}")


if __name__ == "__main__":
    main()
