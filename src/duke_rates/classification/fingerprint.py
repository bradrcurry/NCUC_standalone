"""Document fingerprinting for unknown-type discovery.

The fingerprinter inspects every PDF the pipeline encounters and records a
flat set of observable signals (page count, vocabulary, structural cues)
plus a coarse ``cluster_signature`` for grouping similar documents.

Critically, this is independent of any classifier. We fingerprint EVERY
PDF we see — even ones we don't yet know how to classify — so that when
new document types appear (RFP responses, IRP filings, etc.) they show up
as distinct clusters in ``document_fingerprints_v2``.

Fingerprint version: bump ``FINGERPRINTER_VERSION`` whenever the feature
set changes meaningfully. Old fingerprint rows stay (UNIQUE constraint
includes the version) so historical clusters remain auditable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FINGERPRINTER_VERSION = "v2"

# Vocabulary signals — high-signal terms whose presence/count helps
# distinguish document types. Deliberately broad: tariff terms, regulatory
# terms, legal/order terms. Future doc types (IRP, RFP, etc.) get their
# vocab added here when we encounter them.
_VOCAB_SIGNALS = {
    "tariff": ("tariff", "rate schedule", "monthly rate", "billing", "kwh", "kw demand"),
    "rider": ("rider ", "rider, ", "applies to schedule"),
    "schedule": ("schedule ", "schedule\n"),
    "leaf": ("leaf no.", "original leaf", "revised leaf"),
    "docket": ("docket no.", "docket no ", "sub "),
    "order": ("order approving", "order denying", "ordering paragraph", "the commission finds"),
    "filing": ("via electronic filing", "chief clerk", "respectfully submitted"),
    "compliance": ("compliance tariff", "compliance filing", "compliance revisions"),
    "irp": ("integrated resource plan", "irp"),
    "settlement": ("settlement agreement", "stipulation"),
    "testimony": ("direct testimony", "rebuttal testimony", "q.", "a."),
    "rfp": ("request for proposal", "rfp"),
    "fuel": ("fuel adjustment", "fuel cost", "fuel rider"),
    "dsm_ee": ("demand-side management", "energy efficiency", "dsm/ee", "dsm ee"),
}

# First-page signature patterns — what does the doc OPEN with?
# These often discriminate document types better than full-doc vocabulary.
_FIRST_PAGE_PATTERNS = [
    ("STATE_OF_NC_UC", re.compile(r"state\s+of\s+north\s+carolina[\s\S]{0,80}utilities\s+commission", re.IGNORECASE)),
    ("VIA_ELECTRONIC_FILING", re.compile(r"via\s+electronic\s+filing", re.IGNORECASE)),
    ("DOCKET_HEADER", re.compile(r"docket\s+no\.\s*[a-z]?-?\d", re.IGNORECASE)),
    ("LEAF_HEADER", re.compile(r"(?:original|revised)\s+leaf\s+no\.\s*\d", re.IGNORECASE)),
    ("SCHEDULE_HEADING", re.compile(r"^\s*(schedule|rider)\s+[A-Z][A-Z0-9\-]*", re.MULTILINE)),
    ("TARIFF_BOOK_HEADER", re.compile(r"(?:electricity\s+no|tariff\s+book|retail\s+classification)", re.IGNORECASE)),
    ("TARIFF_SECTION_DIVIDER", re.compile(r"^\s*(?:residential|general|lighting|street)\s+(?:rate\s+schedules?|service|classification)", re.IGNORECASE | re.MULTILINE)),
    ("ATTORNEY_HEADER", re.compile(r"(?:attorney|counsel|esq\.)", re.IGNORECASE)),
    ("PUBLIC_STAFF", re.compile(r"public\s+staff", re.IGNORECASE)),
    ("COMPLIANCE_TARIFFS_HEADER", re.compile(r"compliance\s+(?:tariffs?|filing|revision)", re.IGNORECASE)),
    ("TRANSCRIPT_HEADER", re.compile(r"(?:information\s+sheet|transcript\s+pages|presiding:)", re.IGNORECASE)),
    ("EXHIBIT_HEADER", re.compile(r"\bexhibit\s+(?:no\.?\s*)?[a-z0-9]", re.IGNORECASE)),
    ("SEC_FILING", re.compile(r"(?:securities\s+and\s+exchange\s+commission|form\s+10-K)", re.IGNORECASE)),
    ("ORDER_HEADER", re.compile(r"(?:^\s*order\s+(?:approving|denying|granting|scheduling)|ordering\s+paragraph)", re.IGNORECASE | re.MULTILINE)),
]

# Patterns to extract structural identifiers from text
_LEAF_NO_RE = re.compile(r"leaf\s+no\.?\s*(\d{1,4})", re.IGNORECASE)
_SCHEDULE_CODE_RE = re.compile(r"schedule\s+([A-Z]{1,4}(?:-\d+)?)\b", re.IGNORECASE)
_RIDER_CODE_RE = re.compile(r"rider\s+([A-Z]{2,5}(?:-\d+)?)\b", re.IGNORECASE)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _detect_first_page_signature(first_page_text: str) -> str:
    """Match the strongest signature pattern. Returns label or 'unknown'.

    Patterns are ordered by specificity — earliest match wins.
    """
    sample = first_page_text[:3000]  # bound the regex work
    for label, pattern in _FIRST_PAGE_PATTERNS:
        if pattern.search(sample):
            return label
    return "unknown"


def _count_vocab_signals(full_text_lower: str) -> dict[str, int]:
    """Count occurrences of each vocab category's terms."""
    counts: dict[str, int] = {}
    for category, terms in _VOCAB_SIGNALS.items():
        n = sum(full_text_lower.count(t) for t in terms)
        if n:
            counts[category] = n
    return counts


def _extract_codes(text: str, pattern: re.Pattern) -> list[str]:
    return sorted({m.upper() for m in pattern.findall(text)})


def _build_cluster_signature(
    *,
    first_page_signature: str,
    vocab: dict[str, int],
    page_count: int,
    has_tables: bool,
) -> str:
    """Coarse human-readable signature for SQL grouping.

    Format: ``<first_page_sig>|pages=<bucket>|vocab=<top3>|tables=<0|1>``

    Page count is bucketed (1, 2-5, 6-15, 16-50, 51-150, 151+) so small
    differences don't fragment clusters. Vocab uses the top 3 categories
    with non-zero counts, sorted descending — captures the document's
    dominant content type.
    """
    if page_count <= 1:
        page_bucket = "1"
    elif page_count <= 5:
        page_bucket = "2-5"
    elif page_count <= 15:
        page_bucket = "6-15"
    elif page_count <= 50:
        page_bucket = "16-50"
    elif page_count <= 150:
        page_bucket = "51-150"
    else:
        page_bucket = "151+"

    top_vocab = sorted(vocab.items(), key=lambda kv: kv[1], reverse=True)[:3]
    vocab_part = ",".join(k for k, _ in top_vocab) if top_vocab else "none"

    return f"{first_page_signature}|pages={page_bucket}|vocab={vocab_part}|tables={int(bool(has_tables))}"


def fingerprint_pdf(pdf_path: str | Path) -> dict[str, Any] | None:
    """Compute a fingerprint for one PDF. Returns a dict or None on failure.

    The dict shape mirrors the ``document_fingerprints_v2`` columns so
    callers can pass it straight to :func:`save_fingerprint`.
    """
    p = Path(pdf_path)
    if not p.exists():
        return None
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed — fingerprinter cannot run")
        return None

    file_hash = _hash_file(p)

    page_count = 0
    text_chars = 0
    has_tables = False
    has_scanned_pages = False
    first_page_text = ""
    full_text_parts: list[str] = []
    titles: list[str] = []

    try:
        with pdfplumber.open(p) as pdf:
            page_count = len(pdf.pages)
            # Sample up to first 8 pages for vocab signals; large bundles
            # otherwise dominate fingerprint cost without changing the
            # signature meaningfully.
            for i, page in enumerate(pdf.pages[:8]):
                text = page.extract_text() or ""
                full_text_parts.append(text)
                text_chars += len(text)
                if i == 0:
                    first_page_text = text
                # tables
                try:
                    tables = page.find_tables()
                    if tables:
                        has_tables = True
                except Exception:
                    pass
                # scanned-page heuristic: page has imagery but very little text
                try:
                    if len(text) < 80 and getattr(page, "images", None):
                        has_scanned_pages = True
                except Exception:
                    pass
                # title candidates: short bold-ish lines
                for line in (text.split("\n")[:6] if text else []):
                    line = line.strip()
                    if 4 < len(line) < 80 and not any(c.isdigit() for c in line[:3]):
                        if line.upper() == line or line.istitle():
                            titles.append(line)
    except Exception as exc:
        logger.debug("Fingerprinter failed on %s: %s", p, exc)

    full_text = "\n".join(full_text_parts)
    full_text_lower = full_text.lower()

    vocab = _count_vocab_signals(full_text_lower)
    first_page_sig = _detect_first_page_signature(first_page_text)
    leaf_nos = _extract_codes(full_text, _LEAF_NO_RE)
    schedule_codes = _extract_codes(full_text, _SCHEDULE_CODE_RE)
    rider_codes = _extract_codes(full_text, _RIDER_CODE_RE)

    avg_chars = text_chars / max(min(page_count, 8), 1)

    cluster_sig = _build_cluster_signature(
        first_page_signature=first_page_sig,
        vocab=vocab,
        page_count=page_count,
        has_tables=has_tables,
    )

    return {
        "source_pdf": str(p),
        "file_hash": file_hash or None,
        "page_count": page_count,
        "text_chars": text_chars,
        "has_tables": int(has_tables),
        "has_scanned_pages": int(has_scanned_pages),
        "avg_chars_per_page": round(avg_chars, 2),
        "token_signals": vocab,
        "first_page_signature": first_page_sig,
        "title_candidates": titles[:20],
        "leaf_numbers": leaf_nos,
        "schedule_codes": schedule_codes,
        "rider_codes": rider_codes,
        "cluster_signature_v1": cluster_sig,
        "fingerprinter_version": FINGERPRINTER_VERSION,
    }


def save_fingerprint(
    conn: sqlite3.Connection,
    fp: dict[str, Any],
) -> int:
    """Upsert a fingerprint row. Returns the row id.

    Idempotent on (source_pdf, file_hash, fingerprinter_version).
    """
    now = datetime.now(UTC).isoformat()
    existing = conn.execute(
        """
        SELECT id FROM document_fingerprints_v2
        WHERE source_pdf = ? AND file_hash IS ? AND fingerprinter_version = ?
        """,
        (fp["source_pdf"], fp.get("file_hash"), fp["fingerprinter_version"]),
    ).fetchone()

    payload = (
        fp["source_pdf"],
        fp.get("file_hash"),
        fp.get("page_count", 0),
        fp.get("text_chars", 0),
        fp.get("has_tables", 0),
        fp.get("has_scanned_pages", 0),
        fp.get("avg_chars_per_page", 0.0),
        json.dumps(fp.get("token_signals") or {}, sort_keys=True),
        fp.get("first_page_signature", "unknown"),
        json.dumps(fp.get("title_candidates") or []),
        json.dumps(fp.get("leaf_numbers") or []),
        json.dumps(fp.get("schedule_codes") or []),
        json.dumps(fp.get("rider_codes") or []),
        fp.get("cluster_signature_v1"),
        fp["fingerprinter_version"],
    )

    if existing:
        conn.execute(
            """
            UPDATE document_fingerprints_v2
            SET source_pdf=?, file_hash=?, page_count=?, text_chars=?,
                has_tables=?, has_scanned_pages=?, avg_chars_per_page=?,
                token_signals_json=?, first_page_signature=?,
                title_candidates_json=?, leaf_numbers_json=?,
                schedule_codes_json=?, rider_codes_json=?,
                cluster_signature_v1=?, fingerprinter_version=?,
                updated_at=?
            WHERE id=?
            """,
            payload + (now, existing["id"]),
        )
        return int(existing["id"])

    cur = conn.execute(
        """
        INSERT INTO document_fingerprints_v2 (
            source_pdf, file_hash, page_count, text_chars,
            has_tables, has_scanned_pages, avg_chars_per_page,
            token_signals_json, first_page_signature,
            title_candidates_json, leaf_numbers_json,
            schedule_codes_json, rider_codes_json,
            cluster_signature_v1, fingerprinter_version,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        payload + (now, now),
    )
    return int(cur.lastrowid)
