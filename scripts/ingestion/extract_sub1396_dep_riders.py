"""
Targeted extraction for DEP E-2 Sub 1396 rider updates, effective 2026-04-01.
Processes hd IDs 2591-2595: leaf-600/608/609/670/669 (+ leaf-610 already at tv=5714).
"""
import sys
import sqlite3
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

DB_PATH = str(ROOT / "data" / "db" / "duke_rates.db")


def main():
    extractor = BulkExtractor(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

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
        WHERE hd.id >= 2591 AND hd.id <= 2595
        ORDER BY hd.id
    """
    docs = [dict(r) for r in conn.execute(query).fetchall()]

    # Also include leaf-610 (PIM) tv=5714 which was already registered
    extra_query = """
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
        FROM tariff_versions tv
        JOIN historical_documents hd ON hd.id = tv.historical_document_id
        WHERE tv.id = 5714
    """
    extra_docs = [dict(r) for r in conn.execute(extra_query).fetchall()]
    docs = extra_docs + docs  # PIM first

    conn.close()

    print(f"Processing {len(docs)} DEP Sub 1396 rider documents (eff 2026-04-01)")
    print()

    total_charges = 0
    for doc in docs:
        hd_id = doc["id"]
        fk = doc["family_key"]
        eff = doc.get("effective_start")
        print(f"  [{hd_id}] {fk} eff={eff}... ", end="", flush=True)
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
