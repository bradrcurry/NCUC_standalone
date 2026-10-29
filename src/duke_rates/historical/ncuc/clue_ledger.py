"""Build the regulatory clue ledger from existing page artifacts (Phase 2E D1).

Reads ``ncuc_page_artifacts`` text (no PDF conversion), runs
:func:`regulatory_clues.extract_page_clues` per page, and writes
``regulatory_clues`` / ``clue_scan_pages`` / ``clue_associations``. It writes
nothing else: legacy tables, versions and charges are only read.

Page text is accepted only when it can be tied to the file on disk:

* ``docling_page_miner_v1`` rows are never used (M1: chunk-relative pages);
* an artifact row must carry the file's SHA-256 or MD5, otherwise it is stale;
* the artifact may not cite a page beyond the PDF's page count.

A clue is attributed to a historical document (and its versions) only when its
page lies inside that document's bound span. Clues on pages outside every span
are still recorded (for local gap resolution) but carry no association.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from duke_rates.historical.ncuc.regulatory_clues import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    PageClue,
    extract_page_clues,
)

LEDGER_EXTRACTOR = f"{EXTRACTOR_NAME}@{EXTRACTOR_VERSION}"
#: Page-miner versions whose page numbers are absolute PDF pages.
_NATIVE_ARTIFACT_RE = re.compile(r"^page_miner_v(\d+)$")
# Not "Leaf Indexing is effective as of ...": the Sept-2023 compliance book prints that footer on every sheet.
_INDEX_PAGE_RE = re.compile(r"Index\s+of\s+Tariffs|Schedules,\s+Riders,\s+and\s+Other\s+Tariffs", re.I)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clue_uid(file_sha256: str, page: int, page_text_sha256: str, clue: PageClue) -> str:
    key = json.dumps(
        [file_sha256, page, page_text_sha256, clue.kind, clue.char_start, clue.char_end,
         clue.normalized_value, EXTRACTOR_VERSION]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def is_index_page(text: str) -> bool:
    return bool(_INDEX_PAGE_RE.search(text or ""))


# ---------------------------------------------------------------------------
# File identity and page text
# ---------------------------------------------------------------------------


@dataclass
class FileIdentity:
    path: str
    sha256: str | None
    md5: str | None
    status: str  # computed | stored_unverified | unavailable
    page_count: int | None


_IDENTITY_CACHE: dict[tuple, FileIdentity] = {}


def file_identity(path: str, stored_sha256: str | None = None) -> FileIdentity:
    p = Path(path)
    try:
        st = p.stat()
        key: tuple = (str(path), st.st_size, st.st_mtime_ns)  # a file rewritten in place is hashed again
    except OSError:
        key = (str(path), None, None)
    cached = _IDENTITY_CACHE.get(key)
    if cached is not None:
        return cached
    if p.is_file():
        sha, md5 = hashlib.sha256(), hashlib.md5()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                sha.update(chunk)
                md5.update(chunk)
        identity = FileIdentity(path, sha.hexdigest(), md5.hexdigest(), "computed", _page_count(p))
    elif stored_sha256 and re.fullmatch(r"[0-9a-f]{64}", stored_sha256):
        identity = FileIdentity(path, stored_sha256, None, "stored_unverified", None)
    else:
        identity = FileIdentity(path, None, None, "unavailable", None)
    _IDENTITY_CACHE[key] = identity
    return identity


def _page_count(path: Path) -> int | None:
    try:
        import fitz

        with fitz.open(path) as doc:
            return doc.page_count
    except Exception:
        return None


@dataclass
class PageText:
    page: int
    text: str
    artifact_id: int
    artifact_version: str
    text_layer: str


@dataclass
class PdfPages:
    identity: FileIdentity
    pages: dict[int, PageText] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    integrity_error: str | None = None


def load_pdf_pages(conn: sqlite3.Connection, source_pdf: str, identity: FileIdentity) -> PdfPages:
    """Best native page-artifact text per page that provably belongs to this file."""
    result = PdfPages(identity)
    accepted_hashes = {h for h in (identity.sha256, identity.md5) if h}
    best: dict[int, tuple[int, sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT id, artifact_version, page_number, text_content, file_hash, metadata_json "
        "FROM ncuc_page_artifacts WHERE source_pdf = ?",
        (source_pdf,),
    ):
        m = _NATIVE_ARTIFACT_RE.match(row["artifact_version"] or "")
        if not m:
            result.rejected["non_native_artifact"] = result.rejected.get("non_native_artifact", 0) + 1
            continue
        if accepted_hashes and row["file_hash"] not in accepted_hashes:
            result.rejected["hash_mismatch"] = result.rejected.get("hash_mismatch", 0) + 1
            continue
        rank = int(m.group(1)) * 2 + (1 if row["file_hash"] == identity.sha256 else 0)
        current = best.get(row["page_number"])
        if current is None or rank > current[0]:
            best[row["page_number"]] = (rank, row)
    if identity.page_count is not None and best and max(best) > identity.page_count:
        result.integrity_error = (
            f"artifact cites page {max(best)} but the PDF has {identity.page_count} pages"
        )
        return result
    for page, (_, row) in best.items():
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except ValueError:
            meta = {}
        source = meta.get("artifact_source")
        layer = "native_text" if source == "native_text" else ("ocr" if source == "ocr" else "unknown")
        result.pages[page] = PageText(page, row["text_content"] or "", row["id"],
                                      row["artifact_version"], layer)
    return result


# ---------------------------------------------------------------------------
# Bounding boxes (text layer only; first printed line of the clue)
# ---------------------------------------------------------------------------


class BboxLocator:
    def __init__(self) -> None:
        self._docs: dict[str, object] = {}

    def locate(self, path: str, page: int, page_text: str, clue: PageClue, layer: str):
        if layer != "native_text":
            return None, "no_text_layer"
        doc = self._open(path)
        if doc is None:
            return None, "pdf_unavailable"
        first_line = clue.printed_text.split("\n", 1)[0].strip()
        needle = " ".join(first_line.split())
        if len(needle) < 4:
            return None, "not_found"
        occurrence = page_text.count(first_line, 0, clue.char_start + len(first_line) - 1) - 1
        occurrence = max(occurrence, 0)
        try:
            rects = doc[page - 1].search_for(needle)
        except Exception:
            return None, "not_found"
        if not rects:
            return None, "not_found"
        expected = page_text.count(first_line)
        if len(rects) != expected:
            return None, "ambiguous"
        r = rects[min(occurrence, len(rects) - 1)]
        return json.dumps({"page": page, "x0": round(r.x0, 2), "y0": round(r.y0, 2),
                           "x1": round(r.x1, 2), "y1": round(r.y1, 2),
                           "unit": "pt", "origin": "top-left", "covers": "first_printed_line",
                           "engine": "pymupdf.search_for"}), "located"

    def _open(self, path: str):
        if path not in self._docs:
            try:
                import fitz

                self._docs[path] = fitz.open(path) if Path(path).is_file() else None
            except Exception:
                self._docs[path] = None
        return self._docs[path]

    def close(self) -> None:
        for doc in self._docs.values():
            if doc is not None:
                doc.close()
        self._docs.clear()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write_page_clues(
    conn: sqlite3.Connection,
    *,
    source_pdf: str,
    identity: FileIdentity,
    page: PageText,
    scan_reason: str,
    locator: BboxLocator | None,
) -> list[int]:
    """Extract and persist one page's clues (idempotent). Returns clue ids."""
    text = page.text
    text_sha = _sha256_text(text)
    clues = extract_page_clues(text)
    index_page = is_index_page(text)
    if index_page:
        # A tariff index lists other sheets' names; nothing on it identifies this page.
        for clue in clues:
            clue.role = "index"
    now = _now()
    ids: list[int] = []
    for clue in clues:
        if text[clue.char_start:clue.char_end] == clue.printed_text:
            label = "source_observed" if page.text_layer == "native_text" else "ocr_observed"
        else:  # pragma: no cover - extract_page_clues always slices the page text
            label = f"machine:{LEDGER_EXTRACTOR}"
        uid = clue_uid(identity.sha256, page.page, text_sha, clue)
        existing = conn.execute("SELECT id FROM regulatory_clues WHERE clue_uid = ?", (uid,)).fetchone()
        if existing:
            ids.append(existing[0])
            continue
        bbox, bbox_status = (None, "not_attempted")
        if locator is not None:
            bbox, bbox_status = locator.locate(source_pdf, page.page, text, clue, page.text_layer)
        cur = conn.execute(
            """
            INSERT INTO regulatory_clues (
                clue_uid, source_pdf, file_sha256, file_sha256_status, page, page_artifact_id,
                artifact_version, text_layer, page_text_sha256, char_start, char_end, bbox_json,
                bbox_status, clue_kind, role, printed_text, normalized_value, normalized_json,
                normalization, label_source, extractor, extractor_version, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (uid, source_pdf, identity.sha256, identity.status, page.page, page.artifact_id,
             page.artifact_version, page.text_layer, text_sha, clue.char_start, clue.char_end, bbox,
             bbox_status, clue.kind, clue.role, clue.printed_text, clue.normalized_value,
             clue.normalized_json(), clue.normalization, label, EXTRACTOR_NAME, EXTRACTOR_VERSION, now),
        )
        ids.append(cur.lastrowid)
    conn.execute(
        """
        INSERT OR IGNORE INTO clue_scan_pages (
            file_sha256, page, source_pdf, page_artifact_id, artifact_version, text_layer,
            page_text_sha256, clue_count, is_index_page, extractor_version, scan_reason, scanned_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (identity.sha256, page.page, source_pdf, page.artifact_id, page.artifact_version,
         page.text_layer, text_sha, len(clues), int(index_page), EXTRACTOR_VERSION, scan_reason, now),
    )
    return ids


@dataclass
class BoundDocument:
    id: int
    family_key: str | None
    local_path: str
    start_page: int | None
    end_page: int | None
    content_hash: str | None
    status: str | None
    version_ids: list[int]

    def covers(self, page: int, page_count: int) -> bool:
        start = self.start_page or 1
        end = self.end_page or self.start_page or page_count
        return start <= page <= end


def _bound_documents(conn: sqlite3.Connection, hd_ids: list[int] | None) -> list[BoundDocument]:
    versions: dict[int, list[int]] = defaultdict(list)
    for vid, hid in conn.execute(
        "SELECT id, historical_document_id FROM tariff_versions WHERE historical_document_id IS NOT NULL"
    ):
        versions[hid].append(vid)
    sql = ("SELECT id, family_key, local_path, start_page, end_page, content_hash, status "
           "FROM historical_documents WHERE local_path IS NOT NULL")
    params: list = []
    if hd_ids:
        sql += f" AND id IN ({','.join('?' * len(hd_ids))})"
        params = list(hd_ids)
    return [
        BoundDocument(r[0], r[1], r[2], r[3], r[4], r[5], r[6], sorted(versions.get(r[0], [])))
        for r in conn.execute(sql + " ORDER BY id", params)
    ]


def _redline_context(conn: sqlite3.Connection, doc: BoundDocument) -> dict | None:
    rows = conn.execute(
        """
        SELECT is_redline_candidate, redline_confidence, redline_detector_version, page_start, page_end,
               red_is_index_only
        FROM document_fingerprints
        WHERE source_pdf = ? AND COALESCE(page_start, 1) <= ? AND COALESCE(page_end, 1000000) >= ?
        """,
        (doc.local_path, doc.end_page or doc.start_page or 1000000, doc.start_page or 1),
    ).fetchall()
    if not rows:
        return None
    flagged = [r for r in rows if r[0]]
    best = max(rows, key=lambda r: (r[0] or 0, r[1] or 0))
    return {
        "is_redline_candidate": bool(flagged),
        "redline_confidence": best[1],
        "detector_version": best[2],
        "red_is_index_only": bool(best[5]) if best[5] is not None else None,
        "label_source": f"machine:redline_detector@{best[2] or 'unknown'}",
    }


def build_clue_ledger(
    conn: sqlite3.Connection,
    *,
    historical_document_ids: list[int] | None = None,
    scope: str = "bound",
    locate_bbox: bool = True,
) -> dict:
    """Populate the ledger for bound spans (``scope='bound'``) or whole bound PDFs (``'pdf'``)."""
    if scope not in ("bound", "pdf"):
        raise ValueError("scope must be 'bound' or 'pdf'")
    docs = _bound_documents(conn, historical_document_ids)
    by_pdf: dict[str, list[BoundDocument]] = defaultdict(list)
    for doc in docs:
        by_pdf[doc.local_path].append(doc)

    locator = BboxLocator() if locate_bbox else None
    stats: dict = defaultdict(int)
    skipped: list[dict] = []
    now = _now()
    try:
        for source_pdf, pdf_docs in by_pdf.items():
            identity = file_identity(source_pdf, pdf_docs[0].content_hash)
            if identity.sha256 is None:
                skipped.append({"source_pdf": source_pdf, "reason": "file_unavailable_no_sha256",
                                "historical_document_ids": [d.id for d in pdf_docs]})
                continue
            pages = load_pdf_pages(conn, source_pdf, identity)
            for reason, n in pages.rejected.items():
                stats[f"artifact_rows_rejected_{reason}"] += n
            if pages.integrity_error:
                skipped.append({"source_pdf": source_pdf, "reason": pages.integrity_error,
                                "historical_document_ids": [d.id for d in pdf_docs]})
                continue
            if not pages.pages:
                skipped.append({"source_pdf": source_pdf, "reason": "no_native_page_artifacts",
                                "historical_document_ids": [d.id for d in pdf_docs]})
                continue
            page_count = identity.page_count or max(pages.pages)
            contexts = {d.id: _document_context(conn, d) for d in pdf_docs}
            wanted = set(pages.pages) if scope == "pdf" else {
                p for p in pages.pages if any(d.covers(p, page_count) for d in pdf_docs)
            }
            for page_no in sorted(wanted):
                page = pages.pages[page_no]
                clue_ids = write_page_clues(conn, source_pdf=source_pdf, identity=identity, page=page,
                                            scan_reason=f"ledger_{scope}", locator=locator)
                stats["pages_scanned"] += 1
                stats["clue_rows_seen"] += len(clue_ids)
                for doc in pdf_docs:
                    if not doc.covers(page_no, page_count):
                        continue
                    for clue_id in clue_ids:
                        for vid in doc.version_ids or [None]:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO clue_associations (
                                    clue_id, historical_document_id, tariff_version_id, family_key,
                                    basis, document_context_json, created_at
                                ) VALUES (?,?,?,?,?,?,?)
                                """,
                                (clue_id, doc.id, vid, doc.family_key, "page_in_bound_span",
                                 json.dumps(contexts[doc.id], sort_keys=True), now),
                            )
            stats["pdfs_scanned"] += 1
            conn.commit()
    finally:
        if locator is not None:
            locator.close()
        conn.commit()
    stats["historical_documents_considered"] = len(docs)
    return {"stats": dict(stats), "skipped": skipped}


def _document_context(conn: sqlite3.Connection, doc: BoundDocument) -> dict:
    return {
        "historical_document_status": doc.status,
        # The column defaults to 'approved'; nothing records who set it, so it is not evidence.
        "historical_document_status_evidence": "column_value_unverified",
        "bound_span": [doc.start_page, doc.end_page],
        "redline": _redline_context(conn, doc),
    }


# ---------------------------------------------------------------------------
# Read-side helpers and the A3 check
# ---------------------------------------------------------------------------


def verify_source_grounding(conn: sqlite3.Connection, *, limit: int | None = None) -> dict:
    """A3: every observed clue's printed text is the cited slice of the cited page's text."""
    sql = (
        "SELECT c.id, c.page_artifact_id, c.page_text_sha256, c.char_start, c.char_end, c.printed_text, "
        "c.label_source, a.text_content FROM regulatory_clues c "
        "LEFT JOIN ncuc_page_artifacts a ON a.id = c.page_artifact_id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    checked = failures = 0
    examples = []
    for row in conn.execute(sql):
        if row[6] == f"machine:{LEDGER_EXTRACTOR}":
            continue
        checked += 1
        text = row[7]
        ok = (
            text is not None
            and _sha256_text(text) == row[2]
            and text[row[3]:row[4]] == row[5]
        )
        if not ok:
            failures += 1
            if len(examples) < 10:
                examples.append({"clue_id": row[0], "page_artifact_id": row[1]})
    return {"checked": checked, "failures": failures, "examples": examples, "ok": failures == 0}


def ledger_summary(conn: sqlite3.Connection) -> dict:
    q = conn.execute
    return {
        "clues_by_kind": dict(q("SELECT clue_kind, COUNT(*) FROM regulatory_clues GROUP BY 1").fetchall()),
        "clues_by_label_source": dict(q("SELECT label_source, COUNT(*) FROM regulatory_clues GROUP BY 1").fetchall()),
        "clues_by_normalization": dict(q("SELECT normalization, COUNT(*) FROM regulatory_clues GROUP BY 1").fetchall()),
        "bbox_status": dict(q("SELECT bbox_status, COUNT(*) FROM regulatory_clues GROUP BY 1").fetchall()),
        "pages_scanned": q("SELECT COUNT(*) FROM clue_scan_pages").fetchone()[0],
        "pdfs_scanned": q("SELECT COUNT(DISTINCT file_sha256) FROM clue_scan_pages").fetchone()[0],
        "historical_documents_with_clues": q(
            "SELECT COUNT(DISTINCT historical_document_id) FROM clue_associations").fetchone()[0],
        "versions_with_clues": q(
            "SELECT COUNT(DISTINCT tariff_version_id) FROM clue_associations "
            "WHERE tariff_version_id IS NOT NULL").fetchone()[0],
        "versions_with_docket_clue": q(
            "SELECT COUNT(DISTINCT a.tariff_version_id) FROM clue_associations a "
            "JOIN regulatory_clues c ON c.id = a.clue_id "
            "WHERE c.clue_kind = 'docket' AND a.tariff_version_id IS NOT NULL").fetchone()[0],
    }


# ---------------------------------------------------------------------------
# Bound-span check (read-only on bindings)
# ---------------------------------------------------------------------------


def check_bound_spans(conn: sqlite3.Connection, *, family_leaves: dict[str, str]) -> list[dict]:
    """Bound spans whose every page is an index page or prints only other leaves.

    For each, propose the page runs in the same PDF whose revision header names
    the family's leaf. The proposal is read from page clues (the PDF's remaining
    pages are scanned into the ledger with ``scan_reason='binding_check'``);
    nothing is rebound here.
    """
    findings = []
    for doc in _bound_documents(conn, None):
        leaf = family_leaves.get(doc.family_key or "")
        if not leaf:
            continue
        identity = file_identity(doc.local_path, doc.content_hash)
        if identity.sha256 is None:
            continue
        pages = load_pdf_pages(conn, doc.local_path, identity)
        if pages.integrity_error or not pages.pages:
            continue
        page_count = identity.page_count or max(pages.pages)
        span = [p for p in sorted(pages.pages) if doc.covers(p, page_count)]
        if not span:
            continue
        verdicts = {}
        for p in span:
            text = pages.pages[p].text
            leaves = {c.normalized["leaf"] for c in extract_page_clues(text) if c.kind == "revision"}
            verdicts[p] = ("index" if is_index_page(text) else
                           "own_leaf" if leaf in leaves else
                           "other_leaf" if leaves else "no_leaf_printed")
        if any(v in ("own_leaf", "no_leaf_printed") for v in verdicts.values()):
            continue
        own_pages = []
        for p in sorted(pages.pages):
            ids = write_page_clues(conn, source_pdf=doc.local_path, identity=identity, page=pages.pages[p],
                                   scan_reason="binding_check", locator=None)
            if any(
                row[0] == "revision" and json.loads(row[1]).get("leaf") == leaf and row[2] != "index"
                for row in conn.execute(
                    f"SELECT clue_kind, normalized_json, role FROM regulatory_clues WHERE id IN "
                    f"({','.join('?' * len(ids))})", ids)
            ) if ids else False:
                own_pages.append(p)
        runs: list[list[int]] = []
        for p in own_pages:
            if runs and p == runs[-1][1] + 1:
                runs[-1][1] = p
            else:
                runs.append([p, p])
        findings.append({
            "historical_document_id": doc.id,
            "family_key": doc.family_key,
            "leaf": leaf,
            "source_pdf": doc.local_path,
            "file_sha256": identity.sha256,
            "bound_span": [doc.start_page, doc.end_page],
            "span_page_verdicts": verdicts,
            "proposed_runs": runs,
            "version_ids": doc.version_ids,
        })
    conn.commit()
    return findings
