"""
Register and extract DEC intermediate-gap schedule slices from confirmed tariff books.

Targets:
- 2019-12-01: E-7 Sub 1146 / 1152 JRRR Revised Tariffs
- 2020-08-24: E-7 Sub 1214 Temporary Rates Tariffs Compliance Filing

These filings close most of the DEC 2018-12-01 -> 2021-12-01 intermediate gap with
page-bounded historical_documents + tariff_versions backed by real tariff-book pages.
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

FAMILY_TO_LABEL = {
    "nc-carolinas-schedule-RS": "RS",
    "nc-carolinas-schedule-RE": "RE",
    "nc-carolinas-schedule-ES": "ES",
    "nc-carolinas-schedule-RT": "RT",
    "nc-carolinas-schedule-SGS": "SGS",
    "nc-carolinas-schedule-BC": "BC",
    "nc-carolinas-schedule-LGS": "LGS",
    "nc-carolinas-schedule-OL": "OL",
    "nc-carolinas-schedule-PL": "PL",
    "nc-carolinas-schedule-NL": "NL",
    "nc-carolinas-schedule-TS": "TS",
    "nc-carolinas-schedule-I": "I",
    "nc-carolinas-schedule-OPT-E": "OPT-E",
    "nc-carolinas-schedule-OPT-V": "OPT-V",
    "nc-carolinas-schedule-HP": "HP",
    "nc-carolinas-schedule-PG": "PG",
    "nc-carolinas-schedule-S": "S",
}

SLICE_SPECS = [
    {
        "effective_start": "2019-12-01",
        "docket_number": "E-7 Sub 1152",
        "source_label": "e-7-sub-1146-1152-jrrr-revised-tariffs",
        "source_pdf": ROOT / "data" / "historical" / "ncuc" / "e-7-gap-candidates"
        / "a1c2e778-12b9-457c-898c-40c476e5bd47__E-7 Sub 1146_ 1152 DEC JRRR Revised Tariffs_112519.pdf",
        "canonical_url": "https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=a1c2e778-12b9-457c-898c-40c476e5bd47",
        "slices": [
            ("nc-carolinas-schedule-RS", 4, 5),
            ("nc-carolinas-schedule-RE", 6, 7),
            ("nc-carolinas-schedule-ES", 8, 9),
            ("nc-carolinas-schedule-RT", 10, 11),
            ("nc-carolinas-schedule-SGS", 12, 14),
            ("nc-carolinas-schedule-BC", 15, 16),
            ("nc-carolinas-schedule-LGS", 17, 19),
            ("nc-carolinas-schedule-OL", 20, 23),
            ("nc-carolinas-schedule-PL", 24, 28),
            ("nc-carolinas-schedule-NL", 29, 30),
            ("nc-carolinas-schedule-TS", 31, 31),
            ("nc-carolinas-schedule-I", 38, 40),
            ("nc-carolinas-schedule-OPT-E", 48, 49),
            ("nc-carolinas-schedule-OPT-V", 50, 53),
            ("nc-carolinas-schedule-HP", 60, 63),
            ("nc-carolinas-schedule-PG", 64, 66),
            ("nc-carolinas-schedule-S", 76, 76),
        ],
    },
    {
        "effective_start": "2020-08-24",
        "docket_number": "E-7 Sub 1214",
        "source_label": "e-7-sub-1214-temporary-rates-compliance",
        "source_pdf": ROOT / "data" / "historical" / "ncuc" / "e-7-gap-candidates"
        / "2c1b10c4-bd9b-42f7-a075-9ccfe139820e__2020-08-13  Temporary Rates Tariffs Compliance Filing _002_.pdf",
        "canonical_url": "https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=2c1b10c4-bd9b-42f7-a075-9ccfe139820e",
        "slices": [
            ("nc-carolinas-schedule-RS", 2, 3),
            ("nc-carolinas-schedule-RE", 4, 5),
            ("nc-carolinas-schedule-ES", 6, 7),
            ("nc-carolinas-schedule-RT", 8, 9),
            ("nc-carolinas-schedule-SGS", 28, 30),
            ("nc-carolinas-schedule-BC", 31, 32),
            ("nc-carolinas-schedule-LGS", 33, 35),
            ("nc-carolinas-schedule-TS", 36, 36),
            ("nc-carolinas-schedule-I", 37, 39),
            ("nc-carolinas-schedule-OPT-E", 40, 42),
            ("nc-carolinas-schedule-OPT-V", 43, 47),
            ("nc-carolinas-schedule-HP", 48, 51),
            ("nc-carolinas-schedule-PG", 52, 55),
            ("nc-carolinas-schedule-S", 65, 65),
            ("nc-carolinas-schedule-OL", 66, 69),
            ("nc-carolinas-schedule-PL", 70, 74),
            ("nc-carolinas-schedule-NL", 75, 76),
        ],
    },
]


def _existing_version_id(
    conn: sqlite3.Connection,
    family_key: str,
    effective_start: str,
) -> tuple[int, int] | None:
    row = conn.execute(
        """
        SELECT tv.id AS version_id, hd.id AS historical_document_id
        FROM tariff_versions tv
        JOIN historical_documents hd ON hd.id = tv.historical_document_id
        WHERE tv.family_key = ? AND tv.effective_start = ?
        ORDER BY tv.id DESC
        LIMIT 1
        """,
        (family_key, effective_start),
    ).fetchone()
    if not row:
        return None
    return int(row["version_id"]), int(row["historical_document_id"])


def _build_title(family_key: str, effective_start: str, source_label: str, start_page: int, end_page: int) -> str:
    label = FAMILY_TO_LABEL[family_key]
    return f"{label} {effective_start} ({source_label}, pages {start_page}-{end_page})"


def register_slices() -> list[int]:
    repo = Repository(str(DB_PATH))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    created_ids: list[int] = []
    now = datetime.now(UTC)

    try:
        for spec in SLICE_SPECS:
            pdf_path = spec["source_pdf"]
            content_hash = sha256_bytes(pdf_path.read_bytes())
            canonical_url = spec["canonical_url"]
            effective_start = spec["effective_start"]
            docket_number = spec["docket_number"]
            docket_dir = str(pdf_path.parent)

            print(f"\nRegistering {effective_start} from {pdf_path.name}")
            for family_key, start_page, end_page in spec["slices"]:
                existing = _existing_version_id(conn, family_key, effective_start)
                if existing:
                    version_id, historical_document_id = existing
                    print(
                        f"  EXISTS {family_key} eff={effective_start} "
                        f"hd={historical_document_id} tv={version_id}"
                    )
                    created_ids.append(historical_document_id)
                    continue

                archived_url = f"{canonical_url}#page={start_page}"
                title = _build_title(
                    family_key,
                    effective_start,
                    spec["source_label"],
                    start_page,
                    end_page,
                )
                leaf_no = None
                if family_key.startswith("nc-carolinas-schedule-"):
                    leaf_code = family_key.removeprefix("nc-carolinas-schedule-")
                    leaf_no = leaf_code

                hd_record = HistoricalDocumentRecord(
                    family_key=family_key,
                    title=title,
                    state="NC",
                    company="carolinas",
                    category="rate",
                    kind="pdf",
                    canonical_url=canonical_url,
                    archived_url=archived_url,
                    snapshot_timestamp=now,
                    local_path=pdf_path,
                    raw_text_path=None,
                    content_hash=content_hash,
                    content_type="application/pdf",
                    direct_status_code=None,
                    direct_downloadable=False,
                    revision_label=None,
                    supersedes_label=None,
                    leaf_no=leaf_no,
                    start_page=start_page,
                    end_page=end_page,
                    evidence_json=None,
                    effective_start=effective_start,
                    effective_end=None,
                    retrieved_at=now,
                    metadata_json=None,
                    notes=["source=ncuc", "jurisdiction=NC-carolinas"],
                )
                historical_document_id = repo.upsert_historical_document(hd_record)
                tv_record = TariffVersionRecord(
                    family_key=family_key,
                    historical_document_id=historical_document_id,
                    effective_start=effective_start,
                    source_type="regulator",
                    confidence_score=0.95,
                    notes=f"Manually registered from {spec['source_label']}.",
                    docket_number=docket_number,
                    leaf_no=leaf_no,
                    source_pdf=str(pdf_path),
                    docket_dir=docket_dir,
                )
                version_id = repo.upsert_tariff_version(tv_record)
                print(
                    f"  CREATED {family_key} eff={effective_start} "
                    f"hd={historical_document_id} tv={version_id} pages={start_page}-{end_page}"
                )
                created_ids.append(historical_document_id)
    finally:
        conn.close()

    return sorted(set(created_ids))


def extract_registered_documents(historical_document_ids: list[int]) -> None:
    if not historical_document_ids:
        print("\nNo historical documents to extract.")
        return

    extractor = BulkExtractor(str(DB_PATH))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in historical_document_ids)
    docs = [
        dict(r)
        for r in conn.execute(
            f"""
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
            WHERE hd.id IN ({placeholders})
            ORDER BY hd.effective_start, hd.start_page
            """,
            historical_document_ids,
        ).fetchall()
    ]
    conn.close()

    print(f"\nExtracting {len(docs)} registered DEC gap documents")
    total_charges = 0
    for doc in docs:
        print(
            f"  [{doc['id']}] {doc['family_key']} "
            f"p{doc['start_page']}-{doc['end_page']} eff={doc['effective_start']} ... ",
            end="",
            flush=True,
        )
        try:
            _doc_id, _family_key, inserted = extractor.process_document(doc)
            total_charges += inserted
            print(f"{inserted} charges")
        except Exception as exc:
            print(f"ERROR: {exc}")
    print(f"\nDone. Total charges inserted: {total_charges}")


def main() -> None:
    historical_document_ids = register_slices()
    extract_registered_documents(historical_document_ids)


if __name__ == "__main__":
    main()
