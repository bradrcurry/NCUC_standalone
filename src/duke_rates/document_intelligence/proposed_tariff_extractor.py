"""Persist proposed tariff/rider charge candidates outside accepted rates.

The production tariff tables represent approved/current or historical rates.
Forward-looking NCUC application exhibits belong in a separate lane until an
operator explicitly decides they have been approved and should be promoted.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from duke_rates.document_intelligence.proposed_tariff_detector import (
    ProposedTariffBlock,
    detect_proposed_tariff_blocks_from_pdf,
)
from duke_rates.document_intelligence.proposed_tariff_dec_strategy import (
    DecSplitCharge,
    detect_dec_proposed_blocks_from_pdf,
    extract_dec_split_line_charges,
    is_dec_filing,
)


_DDL = """
CREATE TABLE IF NOT EXISTS proposed_tariff_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pdf TEXT NOT NULL UNIQUE,
    docket_number TEXT,
    utility TEXT,
    proposal_stage TEXT NOT NULL DEFAULT 'proposed',
    source_record_id INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS proposed_tariff_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_document_id INTEGER NOT NULL,
    source_pdf TEXT NOT NULL,
    start_page INTEGER NOT NULL,
    end_page INTEGER NOT NULL,
    exhibit_key TEXT NOT NULL,
    rate_year_context TEXT NOT NULL,
    tariff_name TEXT NOT NULL,
    tariff_kind TEXT NOT NULL,
    schedule_code TEXT,
    leaf_no INTEGER,
    effective_start TEXT,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    block_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(proposed_document_id) REFERENCES proposed_tariff_documents(id),
    UNIQUE(source_pdf, start_page, end_page, exhibit_key, tariff_name)
);

CREATE TABLE IF NOT EXISTS proposed_tariff_charge_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_block_id INTEGER NOT NULL,
    source_pdf TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    exhibit_key TEXT NOT NULL,
    rate_year_context TEXT NOT NULL,
    tariff_name TEXT NOT NULL,
    tariff_kind TEXT NOT NULL,
    charge_type TEXT NOT NULL,
    charge_label TEXT NOT NULL,
    rate_value REAL,
    rate_unit TEXT,
    raw_line TEXT NOT NULL,
    parser_version TEXT NOT NULL DEFAULT 'proposed_regex_v1',
    confidence REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(proposed_block_id) REFERENCES proposed_tariff_blocks(id),
    UNIQUE(proposed_block_id, charge_type, charge_label, raw_line)
);

CREATE INDEX IF NOT EXISTS idx_proposed_tariff_blocks_source
ON proposed_tariff_blocks(source_pdf, exhibit_key, start_page);

CREATE INDEX IF NOT EXISTS idx_proposed_charge_candidates_tariff
ON proposed_tariff_charge_candidates(tariff_name, exhibit_key);
"""

_BASIC_RE = re.compile(
    r"\b(Basic Customer Charge[^\n$¢]*?)\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\b",
    re.IGNORECASE,
)
_MONEY_UNIT_RE = re.compile(
    r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:dollars?)?\s+per\s+"
    r"(month|kW|kWh|bill|day|fixture|lamp|pole|block)\b",
    re.IGNORECASE,
)
_CENTS_UNIT_RE = re.compile(
    r"\(?\s*"
    r"(?P<neg>-)?\s*(?P<value>[0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:¢|cents?)\s*\)?"
    r"\s*(?:\s+per\s+|\s*/\s*)"
    r"(?:on[-\s]?peak\s+|off[-\s]?peak\s+|discount\s+|super[-\s]?off[-\s]?peak\s+)?"
    r"(?:kWh|kilowatt[-\s]?hour)\b",
    re.IGNORECASE,
)
_RATE_WORD_RE = re.compile(
    r"(basic customer charge|energy|kilowatt[-\s]?hour|kwh|demand|on-peak|off-peak|"
    r"discount|rider|credit|charge|fee|adjustment|increment|decrement)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProposedChargeCandidate:
    charge_type: str
    charge_label: str
    rate_value: float | None
    rate_unit: str | None
    raw_line: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProposedExtractionSummary:
    document_id: int
    blocks_detected: int
    blocks_persisted: int
    charges_persisted: int
    report_path: str | None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    _add_missing_block_columns(conn)
    conn.commit()


def _add_missing_block_columns(conn: sqlite3.Connection) -> None:
    """Lightweight migration for DBs created before leaf_no/effective_start
    were added to proposed_tariff_blocks."""
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(proposed_tariff_blocks)")
    }
    if "leaf_no" not in existing:
        conn.execute("ALTER TABLE proposed_tariff_blocks ADD COLUMN leaf_no INTEGER")
    if "effective_start" not in existing:
        conn.execute(
            "ALTER TABLE proposed_tariff_blocks ADD COLUMN effective_start TEXT"
        )


def extract_charge_candidates(text: str) -> list[ProposedChargeCandidate]:
    """Extract conservative charge candidates from one page/block of text."""
    candidates: list[ProposedChargeCandidate] = []
    lines = [_clean_line(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]

    for line in lines:
        if not _RATE_WORD_RE.search(line):
            continue
        basic = _BASIC_RE.search(line)
        if basic:
            candidates.append(
                ProposedChargeCandidate(
                    charge_type="fixed",
                    charge_label="Basic Customer Charge",
                    rate_value=_to_float(basic.group(2)),
                    rate_unit="$/month",
                    raw_line=line,
                    confidence=0.9,
                )
            )
            continue

        cents = _CENTS_UNIT_RE.search(line)
        if cents:
            value = (_to_float(cents.group("value")) or 0.0) / 100.0
            if cents.group("neg") or _looks_like_parenthesized_credit(line, cents.start()):
                value = -value
            candidates.append(
                ProposedChargeCandidate(
                    charge_type=_infer_charge_type(line),
                    charge_label=_label_from_line(line),
                    rate_value=round(value, 8),
                    rate_unit="$/kWh",
                    raw_line=line,
                    confidence=0.72,
                )
            )
            continue

        money = _MONEY_UNIT_RE.search(line)
        if money:
            unit = money.group(2).lower()
            candidates.append(
                ProposedChargeCandidate(
                    charge_type=_infer_charge_type(line),
                    charge_label=_label_from_line(line),
                    rate_value=_to_float(money.group(1)),
                    rate_unit=f"$/{unit}",
                    raw_line=line,
                    confidence=0.68,
                )
            )

    return _dedupe_candidates(candidates)


def persist_proposed_pdf_extraction(
    conn: sqlite3.Connection,
    *,
    pdf_path: Path | str,
    docket_number: str | None = None,
    utility: str | None = None,
    source_record_id: int | None = None,
    report_path: Path | str | None = None,
) -> ProposedExtractionSummary:
    """Detect, parse, and persist proposed charge candidates for a PDF.

    Filings from Duke Energy Carolinas (E-7 dockets) use a leaf-index header
    and split-line rate cells; we dispatch those to the DEC strategy. Anything
    else falls through the DEP/PBR exhibit-anchor detector.
    """
    ensure_schema(conn)
    pdf = Path(pdf_path)
    strategy = _detect_strategy(pdf, utility)
    if strategy == "dec":
        blocks, text_by_page = detect_dec_proposed_blocks_from_pdf(pdf)
    else:
        blocks = detect_proposed_tariff_blocks_from_pdf(pdf)
        text_by_page = _load_pdf_text_by_page(pdf, {b.start_page for b in blocks})
    blocks = _propagate_effective_start_within_exhibit(blocks)
    now = datetime.now(UTC).isoformat()
    if source_record_id is None:
        source_record_id = _register_proposed_pdf_in_historical_documents(
            conn,
            pdf=pdf,
            docket_number=docket_number,
            utility=utility,
            blocks=blocks,
            now=now,
        )

    with conn:
        conn.execute(
            """
            INSERT INTO proposed_tariff_documents
                (source_pdf, docket_number, utility, proposal_stage,
                 source_record_id, metadata_json, updated_at)
            VALUES (?, ?, ?, 'proposed', ?, ?, ?)
            ON CONFLICT(source_pdf) DO UPDATE SET
                docket_number=excluded.docket_number,
                utility=excluded.utility,
                source_record_id=excluded.source_record_id,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                str(pdf),
                docket_number,
                utility,
                source_record_id,
                json.dumps({"parser": "proposed_regex_v1", "block_count": len(blocks)}),
                now,
            ),
        )
        doc_id = conn.execute(
            "SELECT id FROM proposed_tariff_documents WHERE source_pdf = ?",
            (str(pdf),),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM proposed_tariff_charge_candidates WHERE source_pdf = ?",
            (str(pdf),),
        )
        conn.execute("DELETE FROM proposed_tariff_blocks WHERE source_pdf = ?", (str(pdf),))

        block_count = charge_count = 0
        report_rows: list[dict[str, Any]] = []
        for block in blocks:
            block_id = _insert_block(conn, doc_id, block)
            block_count += 1
            text = text_by_page.get(block.start_page, "")
            if strategy == "dec":
                split = _candidates_from_dec(text)
                inline = extract_charge_candidates(text)
                charges = _dedupe_candidates(list(split) + list(inline))
            else:
                inline = extract_charge_candidates(text)
                split = _candidates_from_dec(text)
                charges = _dedupe_candidates(list(inline) + list(split))
            for charge in charges:
                _insert_charge(conn, block_id, block, charge)
                charge_count += 1
            row = block.to_dict()
            row["charge_candidates"] = [c.to_dict() for c in charges]
            report_rows.append(row)

    if report_path is not None:
        out = Path(report_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report_rows, indent=2), encoding="utf-8")
        report = str(out)
    else:
        report = None

    return ProposedExtractionSummary(
        document_id=doc_id,
        blocks_detected=len(blocks),
        blocks_persisted=block_count,
        charges_persisted=charge_count,
        report_path=report,
    )


def _insert_block(
    conn: sqlite3.Connection,
    doc_id: int,
    block: ProposedTariffBlock,
) -> int:
    tariff_name = block.schedule_name
    tariff_kind = "rider" if "RIDER" in tariff_name.upper() else "schedule"
    schedule_code = _infer_code(tariff_name)
    conn.execute(
        """
        INSERT INTO proposed_tariff_blocks
            (proposed_document_id, source_pdf, start_page, end_page, exhibit_key,
             rate_year_context, tariff_name, tariff_kind, schedule_code,
             leaf_no, effective_start,
             confidence, evidence_json, block_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            block.source_pdf,
            block.start_page,
            block.end_page,
            block.exhibit_key,
            block.rate_year_context,
            tariff_name,
            tariff_kind,
            schedule_code,
            block.leaf_no,
            block.effective_start,
            block.confidence,
            json.dumps(block.evidence),
            json.dumps(block.to_dict()),
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_charge(
    conn: sqlite3.Connection,
    block_id: int,
    block: ProposedTariffBlock,
    charge: ProposedChargeCandidate,
) -> None:
    tariff_name = block.schedule_name
    tariff_kind = "rider" if "RIDER" in tariff_name.upper() else "schedule"
    conn.execute(
        """
        INSERT OR IGNORE INTO proposed_tariff_charge_candidates
            (proposed_block_id, source_pdf, page_number, exhibit_key,
             rate_year_context, tariff_name, tariff_kind, charge_type,
             charge_label, rate_value, rate_unit, raw_line, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            block_id,
            block.source_pdf,
            block.start_page,
            block.exhibit_key,
            block.rate_year_context,
            tariff_name,
            tariff_kind,
            charge.charge_type,
            charge.charge_label,
            charge.rate_value,
            charge.rate_unit,
            charge.raw_line,
            charge.confidence,
        ),
    )


def _load_pdf_text_by_page(pdf_path: Path, pages: Iterable[int]) -> dict[int, str]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF/fitz is required for proposed PDF extraction") from exc
    wanted = set(pages)
    doc = fitz.open(pdf_path)
    try:
        return {
            page_number: doc.load_page(page_number - 1).get_text("text") or ""
            for page_number in sorted(wanted)
            if 1 <= page_number <= doc.page_count
        }
    finally:
        doc.close()


def _infer_charge_type(line: str) -> str:
    low = line.lower()
    if "demand" in low or "kw" in low and "kwh" not in low:
        return "demand"
    if "on-peak" in low or "off-peak" in low or "discount" in low:
        return "tou_energy"
    if "credit" in low:
        return "credit"
    if "rider" in low:
        return "adjustment"
    return "energy"


def _label_from_line(line: str) -> str:
    cleaned = _clean_line(line)
    before = re.split(
        r"\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:¢|cents?|per|\$)",
        cleaned,
        maxsplit=1,
    )[0].strip(" :-")
    if before:
        return before[:120]
    # Value-first patterns such as "21.859¢ per On-Peak kWh": label is the
    # descriptive tail that follows the value and unit.
    after = re.sub(
        r"^\s*\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:¢|cents?)?\s*(?:per\s+)?",
        "",
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    ).strip(" :-")
    if after:
        return after[:120]
    return "Proposed Charge"


def _infer_code(tariff_name: str) -> str | None:
    upper = tariff_name.upper()
    match = re.search(r"\b(?:SCHEDULE|RIDER)\s+([A-Z0-9][A-Z0-9-]{0,20})\b", upper)
    if match:
        return match.group(1)
    return None


def _clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line or "").strip()


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except Exception:
        return None


def _looks_like_parenthesized_credit(line: str, match_start: int) -> bool:
    """Return True when the matched value is wrapped in parentheses — e.g.
    ``(0.0030¢) per kilowatt hour`` — which is how DEC writes negative/credit
    rider amounts on rider body pages."""
    prefix = line[:match_start]
    if "(" not in prefix.rsplit(")", 1)[-1]:
        return False
    open_idx = prefix.rfind("(")
    return ")" in line[open_idx:]


def _synthetic_application_family_key(
    company: str | None, docket_number: str | None
) -> str:
    """Build a stable, distinctive family_key for a proposed application PDF.

    Real schedule/rider families use keys like ``nc-progress-leaf-614``; we
    use ``nc-<company>-application-<docket-slug>`` so an application doc
    cannot collide with a real tariff family and is easy to filter out.
    """
    company_slug = company or "unknown"
    docket_slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        (docket_number or "unknown").lower(),
    ).strip("-")
    return f"nc-{company_slug}-application-{docket_slug or 'unknown'}"


_UTILITY_TO_COMPANY_HD = {
    "duke energy progress": "progress",
    "duke energy carolinas": "carolinas",
    "progress": "progress",
    "carolinas": "carolinas",
    "dep": "progress",
    "dec": "carolinas",
}


def _register_proposed_pdf_in_historical_documents(
    conn: sqlite3.Connection,
    *,
    pdf: Path,
    docket_number: str | None,
    utility: str | None,
    blocks: list[ProposedTariffBlock],
    now: str,
) -> int | None:
    """Register the proposed application PDF in ``historical_documents`` so the
    proposed lane has a real document_id to point at, and so docket-level
    lineage tooling can find the filing.

    A lightweight upsert keyed on the file's sha256 content hash: if the PDF
    has already been registered (e.g., by the normal NCUC discovery pipeline)
    we reuse that row; otherwise we insert a new row with
    ``status='proposed'`` and ``category='rate_case_application'`` so it does
    not get mistaken for an accepted tariff sheet.
    """
    if not pdf.exists():
        return None
    try:
        info = conn.execute("PRAGMA table_info(historical_documents)").fetchall()
    except sqlite3.OperationalError:
        return None
    columns = {row[1] for row in info}
    if not columns:
        return None

    content_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    existing = conn.execute(
        "SELECT id FROM historical_documents WHERE content_hash = ? LIMIT 1",
        (content_hash,),
    ).fetchone()
    if existing is not None:
        return int(existing[0])

    company = _UTILITY_TO_COMPANY_HD.get((utility or "").strip().lower())
    eff_start = next(
        (b.effective_start for b in blocks if b.effective_start),
        None,
    )
    docket_label = docket_number or "Unknown Docket"
    utility_label = utility or "Unknown Utility"
    family_key = _synthetic_application_family_key(company, docket_number)
    # The real ``historical_documents`` schema makes canonical_url /
    # archived_url / direct_downloadable NOT NULL; these PDFs were dropped in
    # outside the normal discovery flow so we synthesize file:// URLs that
    # round-trip to the on-disk artifact.
    local_url = pdf.resolve().as_uri()
    payload: dict[str, Any] = {
        "title": f"{utility_label} {docket_label} Proposed Tariff Application",
        "state": "NC",
        "company": company,
        "category": "rate_case_application",
        "kind": "pdf",
        "family_key": family_key,
        "canonical_url": local_url,
        "archived_url": local_url,
        "direct_downloadable": 0,
        "local_path": str(pdf),
        "content_hash": content_hash,
        "content_type": "application/pdf",
        "status": "proposed",
        "retrieved_at": now,
        "snapshot_timestamp": now,
        "effective_start": eff_start,
        "requested_effective_date": eff_start,
        "metadata_json": json.dumps(
            {
                "source": "proposed_tariff_extractor",
                "docket_number": docket_number,
                "utility": utility,
                "proposal_stage": "proposed",
                "block_count": len(blocks),
            }
        ),
    }
    payload = {k: v for k, v in payload.items() if k in columns}
    if not payload:
        return None
    cols = ", ".join(payload.keys())
    placeholders = ", ".join("?" for _ in payload)
    cur = conn.execute(
        f"INSERT INTO historical_documents ({cols}) VALUES ({placeholders})",
        list(payload.values()),
    )
    return int(cur.lastrowid) if cur.lastrowid else None


def _propagate_effective_start_within_exhibit(
    blocks: list[ProposedTariffBlock],
) -> list[ProposedTariffBlock]:
    """Backfill ``effective_start`` on blocks that share an exhibit with at
    least one dated peer. Useful for DEC schedule body pages, which omit the
    explicit Effective line — neighboring rider body pages in the same
    Exhibit B/B_1/B_2 carry it explicitly."""
    from collections import Counter

    counts: dict[tuple[str, str], Counter[str]] = {}
    for block in blocks:
        if not block.effective_start:
            continue
        key = (block.source_pdf, block.exhibit_key)
        counts.setdefault(key, Counter())[block.effective_start] += 1

    mode_by_exhibit: dict[tuple[str, str], str] = {}
    for key, counter in counts.items():
        if counter:
            mode_by_exhibit[key] = counter.most_common(1)[0][0]

    if not mode_by_exhibit:
        return blocks

    filled: list[ProposedTariffBlock] = []
    for block in blocks:
        if block.effective_start:
            filled.append(block)
            continue
        key = (block.source_pdf, block.exhibit_key)
        mode = mode_by_exhibit.get(key)
        if mode is None:
            filled.append(block)
            continue
        data = block.to_dict()
        data["effective_start"] = mode
        filled.append(ProposedTariffBlock(**data))
    return filled


def _detect_strategy(pdf_path: Path, utility: str | None) -> str:
    """Return ``"dec"`` for Duke Energy Carolinas filings, else ``"dep"``.

    The utility hint from the CLI is respected when provided; otherwise we
    sniff the first few pages of the PDF for the DEC company name."""
    if utility:
        if "carolinas" in utility.lower():
            return "dec"
        if "progress" in utility.lower():
            return "dep"
    try:
        import fitz
    except ImportError:
        return "dep"
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return "dep"
    try:
        for page_number in range(1, min(doc.page_count, 25) + 1):
            text = doc.load_page(page_number - 1).get_text("text") or ""
            if is_dec_filing(text):
                return "dec"
    finally:
        doc.close()
    return "dep"


def _candidates_from_dec(text: str) -> list[ProposedChargeCandidate]:
    """Adapt DEC split-line charges into the shared candidate dataclass."""
    return [_to_candidate(charge) for charge in extract_dec_split_line_charges(text)]


def _to_candidate(charge: DecSplitCharge) -> ProposedChargeCandidate:
    return ProposedChargeCandidate(
        charge_type=charge.charge_type,
        charge_label=charge.charge_label,
        rate_value=charge.rate_value,
        rate_unit=charge.rate_unit,
        raw_line=charge.raw_line,
        confidence=charge.confidence,
    )


def _dedupe_candidates(
    candidates: Iterable[ProposedChargeCandidate],
) -> list[ProposedChargeCandidate]:
    seen: set[tuple[str, str, str]] = set()
    out: list[ProposedChargeCandidate] = []
    for cand in candidates:
        key = (cand.charge_type, cand.charge_label, cand.raw_line)
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out
