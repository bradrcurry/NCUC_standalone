"""
Register and extract DEP leaf-533 (LGS-TOU) from E-2 Sub 1023.

Confirmed source:
- data/historical/ncuc/e-2-sub-1023/02e8dadc-1cc4-4ea0-9a17-e0762c2aa842.pdf
- pages 41-44
- "Supersedes Schedule LGS-TOU-26"
- "Effective for service rendered on and after June 1, 2014"

This fills the remaining DEP LGS-TOU 2015 matrix gap by providing the predecessor
version in force before the existing 2015-12-01 registration.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from duke_rates.db.repository import Repository
from duke_rates.download.hashing import sha256_bytes
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.tariff import TariffVersionRecord

DB_PATH = ROOT / "data" / "db" / "duke_rates.db"
PDF_PATH = (
    ROOT
    / "data"
    / "historical"
    / "ncuc"
    / "e-2-sub-1023"
    / "02e8dadc-1cc4-4ea0-9a17-e0762c2aa842.pdf"
)
CANONICAL_URL = "https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=02e8dadc-1cc4-4ea0-9a17-e0762c2aa842"
FAMILY_KEY = "nc-progress-leaf-533"
EFFECTIVE_START = "2014-06-01"
START_PAGE = 41
END_PAGE = 44
LEAF_NO = "533"
DOCKET_NUMBER = "E-2 Sub 1023"


def _existing_version(conn: sqlite3.Connection) -> tuple[int, int] | None:
    row = conn.execute(
        """
        SELECT tv.id AS version_id, hd.id AS historical_document_id
        FROM tariff_versions tv
        JOIN historical_documents hd ON hd.id = tv.historical_document_id
        WHERE tv.family_key = ? AND tv.effective_start = ?
        ORDER BY tv.id DESC
        LIMIT 1
        """,
        (FAMILY_KEY, EFFECTIVE_START),
    ).fetchone()
    if not row:
        return None
    return int(row["version_id"]), int(row["historical_document_id"])


def register_document() -> int:
    repo = Repository(str(DB_PATH))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    now = datetime.now(UTC)
    content_hash = sha256_bytes(PDF_PATH.read_bytes())

    try:
        existing = _existing_version(conn)
        if existing:
            version_id, historical_document_id = existing
            print(
                f"EXISTS {FAMILY_KEY} eff={EFFECTIVE_START} "
                f"hd={historical_document_id} tv={version_id}"
            )
            return historical_document_id

        archived_url = f"{CANONICAL_URL}#page={START_PAGE}"
        hd_record = HistoricalDocumentRecord(
            family_key=FAMILY_KEY,
            title="LGS-TOU-27 (Sub 1023, pages 41-44)",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url=CANONICAL_URL,
            archived_url=archived_url,
            snapshot_timestamp=now,
            local_path=PDF_PATH,
            raw_text_path=None,
            content_hash=content_hash,
            content_type="application/pdf",
            direct_status_code=None,
            direct_downloadable=False,
            revision_label="LGS-TOU-27",
            supersedes_label="LGS-TOU-26",
            leaf_no=LEAF_NO,
            start_page=START_PAGE,
            end_page=END_PAGE,
            evidence_json=None,
            effective_start=EFFECTIVE_START,
            effective_end=None,
            retrieved_at=now,
            metadata_json=None,
            notes=["source=ncuc", "jurisdiction=NC-progress"],
        )
        historical_document_id = repo.upsert_historical_document(hd_record)
        tv_record = TariffVersionRecord(
            family_key=FAMILY_KEY,
            historical_document_id=historical_document_id,
            effective_start=EFFECTIVE_START,
            source_type="regulator",
            confidence_score=0.98,
            notes="Manually registered from E-2 Sub 1023 LGS-TOU-27 pages 41-44.",
            docket_number=DOCKET_NUMBER,
            leaf_no=LEAF_NO,
            source_pdf=str(PDF_PATH),
            docket_dir=str(PDF_PATH.parent),
        )
        version_id = repo.upsert_tariff_version(tv_record)
        print(
            f"CREATED {FAMILY_KEY} eff={EFFECTIVE_START} "
            f"hd={historical_document_id} tv={version_id} pages={START_PAGE}-{END_PAGE}"
        )
        return historical_document_id
    finally:
        conn.close()


def extract_document(historical_document_id: int) -> None:
    extractor = BulkExtractor(str(DB_PATH))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
        """,
        (historical_document_id,),
    ).fetchone()
    conn.close()
    if not doc:
        raise RuntimeError(f"historical_document {historical_document_id} not found")

    print(
        f"Extracting [{doc['id']}] {doc['family_key']} "
        f"p{doc['start_page']}-{doc['end_page']} eff={doc['effective_start']} ... ",
        end="",
        flush=True,
    )
    _doc_id, _family_key, inserted = extractor.process_document(dict(doc))
    print(f"{inserted} charges")


def main() -> None:
    historical_document_id = register_document()
    extract_document(historical_document_id)


if __name__ == "__main__":
    main()
