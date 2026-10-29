"""Shared source-evidence validation for every charge and rider promotion (Phase 2E D3).

D2 put redline and effective-date guards on the reviewer CLI (``provenance
promote``). The same promotion is reached without that CLI by
``BulkExtractor._legacy_promote`` (``DUKE_RATES_LEGACY_REPLACE``) and
``ncuc_loader._replace_through_runs`` (rider summaries), and by any future
caller of :func:`duke_rates.db.provenance.promote`. This module is the one
place those paths are checked: ``provenance.promote`` calls :func:`enforce`
for ``tariff_version`` and ``rider_summary_source`` runs, so a caller cannot
skip it.

Checks (each stored ok / blocking / detail):

* ``source_present``: the run's source is a readable file (or, for a version
  sourced from a utility web document with no historical document, the check
  is not applicable).
* ``source_hash_matches``: the file's bytes are the ones the run extracted from
  (``extraction_runs.document_sha256``) and the ones the archive recorded
  (``historical_documents.content_hash``).
* ``bound_pages_in_file``: the version's page bounds exist in the file.
* ``no_redline_duplicate_assertion``: no reviewed assertion says the span is a
  redline duplicate of another document.
* ``bound_pages_not_redline``: page evidence (coloured strike/underline bars,
  strikeout flags) on the pages the run read, plus the stored redline-detector
  fingerprint, which also covers scanned pages without a text layer.
* ``effective_date_printed``: the run's effective-date reading is printed on
  those pages (when they print any date).

Policy:

* Unattended pathways (``automated:*``, the default) fail closed: a failed
  check, or evidence that cannot be established, refuses the promotion, and no
  override is accepted.
* The reviewer pathway (``reviewed``) may override named checks, each recorded
  with the reviewer and reason. Missing evidence is reported to the reviewer
  but does not block.

Every call writes one ``promotion_validations`` row (schema v5) when the table
exists. A clean source passes and the promotion proceeds unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence

from duke_rates.db.provenance import ProvenanceError, atomic

VALIDATOR_VERSION = "ingestion_validation@1.0.0"
REVIEWED = "reviewed"
#: Checks a named reviewer may override on the ``reviewed`` pathway.
OVERRIDABLE = frozenset({
    "source_present", "source_hash_matches", "no_redline_duplicate_assertion",
    "bound_pages_not_redline", "effective_date_printed",
})
#: Checks that are blocking only where nobody reviews the result.
_UNATTENDED_ONLY = frozenset({"source_present"})


class IngestionValidationError(ProvenanceError):
    """The run's source evidence does not support promoting it."""

    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _check(ok: bool | None, detail: Any, *, blocking: bool = True) -> dict:
    return {"ok": ok, "blocking": blocking, "detail": detail}


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def _page_checks(conn: sqlite3.Connection, path: str, pages: list[int], hd_id: int | None) -> dict:
    from duke_rates.historical.ncuc.local_gap_resolution import _redline_fingerprint
    from duke_rates.parse.redline_detector import PageRedlineReader, summarize_page_evidence

    checks: dict[str, dict] = {}
    if hd_id is not None and _table(conn, "document_assertions"):
        asserted = conn.execute(
            "SELECT id, target_historical_document_id FROM document_assertions WHERE historical_document_id = ? "
            "AND assertion = 'redline_duplicate_of' AND retracted_at IS NULL", (hd_id,)).fetchall()
        checks["no_redline_duplicate_assertion"] = _check(
            not asserted, {"assertions": [{"id": a[0], "duplicate_of": a[1]} for a in asserted]})
    reader = PageRedlineReader()
    try:
        evidence = summarize_page_evidence(
            [p for start, end in _runs(pages) for p in reader.pages(path, start, end)])
    finally:
        reader.close()
    fingerprint = _redline_fingerprint(conn, path, min(pages), max(pages)) if pages else None
    checks["bound_pages_not_redline"] = _check(
        evidence["verdict"] != "redline" and fingerprint is None,
        {"page_evidence": {k: evidence.get(k) for k in ("verdict", "redline_pages", "suspect_pages")},
         "redline_fingerprint": fingerprint,
         "note": "suspect / no_text_layer pages are reported, not blocking; the fingerprint covers scanned pages"})
    return checks


def _runs(pages: Iterable[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for p in sorted(set(pages)):
        if out and p == out[-1][1] + 1:
            out[-1] = (out[-1][0], p)
        else:
            out.append((p, p))
    return out


_HEX_HASH = re.compile(r"[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64}")


def _archive_hash_matches(stored: str, path: str, ident) -> bool | None:
    """Does ``historical_documents.content_hash`` name this file's bytes? ``None``: it is not a file hash.

    Legacy rows store SHA-256, MD5 or SHA-1 (66 of the 118 non-SHA-256 values on the D3 scratch copy are
    the file's SHA-1); others store UUIDs, ``local://`` ids or short codes, which identify nothing.
    """
    if not _HEX_HASH.fullmatch(stored or ""):
        return None
    if len(stored) == 40:
        h = hashlib.sha1()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == stored
    return stored in (ident.sha256, ident.md5)


def _identity(path: str | None):
    from duke_rates.historical.ncuc.clue_ledger import file_identity

    return file_identity(path) if path else None


def _version_checks(conn: sqlite3.Connection, run: sqlite3.Row) -> tuple[str, dict]:
    from duke_rates.historical.ncuc.document_promotion import effective_date_promotion_conflict

    v = conn.execute("SELECT id, historical_document_id, document_id FROM tariff_versions WHERE id = ?",
                     (run["target_id"],)).fetchone()
    if v is None:
        return "blocked", {"source_present": _check(False, {"error": f"tariff version {run['target_id']} missing"})}
    if v["historical_document_id"] is None:
        if v["document_id"] is not None:
            return "not_applicable", {"source_present": _check(None, {
                "source": "utility web document", "document_id": v["document_id"],
                "note": "no historical document: page-level checks do not apply"}, blocking=False)}
        return "checked", {"source_present": _check(False, {"error": "version has no source document"})}
    hd = conn.execute("SELECT id, local_path, content_hash, start_page, end_page FROM historical_documents "
                      "WHERE id = ?", (v["historical_document_id"],)).fetchone()
    ident = _identity(hd["local_path"])
    checks = {"source_present": _check(bool(ident and ident.sha256),
                                       {"historical_document_id": hd["id"], "path": hd["local_path"]})}
    if not (ident and ident.sha256):
        return "checked", checks
    stored = (hd["content_hash"] or "").lower()
    run_sha = (run["document_sha256"] or "").lower() if run["document_sha256_status"] == "computed" else ""
    archive_ok = _archive_hash_matches(stored, hd["local_path"], ident)
    checks["source_hash_matches"] = _check(
        archive_ok is not False and (not run_sha or run_sha == ident.sha256),
        {"file_sha256": ident.sha256, "archive_content_hash": stored or None, "run_document_sha256": run_sha or None,
         "archive_hash_comparable": archive_ok is not None})
    start, end = hd["start_page"] or 1, hd["end_page"] or hd["start_page"] or 1
    checks["bound_pages_in_file"] = _check(1 <= start <= end <= (ident.page_count or 0),
                                           {"pages": [start, end], "page_count": ident.page_count})
    if not checks["bound_pages_in_file"]["ok"]:
        return "checked", checks
    checks |= _page_checks(conn, hd["local_path"], list(range(start, end + 1)), hd["id"])
    conflict = effective_date_promotion_conflict(conn, run["id"])
    checks["effective_date_printed"] = _check(conflict is None, conflict or {
        "run_interprets": run["interpreted_effective_start"]})
    return "checked", checks


def _evidence_pages(value: Any) -> set[int]:
    if isinstance(value, dict):
        out = {value["page"]} if isinstance(value.get("page"), int) else set()
        for v in value.values():
            out |= _evidence_pages(v)
        return out
    if isinstance(value, list):
        return set().union(*(_evidence_pages(v) for v in value)) if value else set()
    return set()


def _rider_checks(conn: sqlite3.Connection, run: sqlite3.Row) -> tuple[str, dict]:
    path = run["source_path"]
    ident = _identity(path)
    checks = {"source_present": _check(bool(ident and ident.sha256), {"path": path})}
    if not (ident and ident.sha256):
        return "checked", checks
    run_sha = (run["document_sha256"] or "").lower() if run["document_sha256_status"] == "computed" else ""
    checks["source_hash_matches"] = _check(not run_sha or run_sha == ident.sha256,
                                           {"file_sha256": ident.sha256, "run_document_sha256": run_sha or None})
    pages: set[int] = set()
    for (ev,) in conn.execute("SELECT evidence_json FROM extracted_rider_blocks WHERE run_id = ?", (run["id"],)):
        pages |= _evidence_pages(json.loads(ev)) if ev else set()
    for (ev,) in conn.execute(
            "SELECT li.evidence_json FROM extracted_rider_line_items li JOIN extracted_rider_blocks b "
            "ON b.id = li.block_id WHERE b.run_id = ?", (run["id"],)) if _has_column(
            conn, "extracted_rider_line_items", "evidence_json") else []:
        pages |= _evidence_pages(json.loads(ev)) if ev else set()
    pages = {p for p in pages if 1 <= p <= (ident.page_count or 0)}
    checks["evidence_pages_known"] = _check(bool(pages), {"pages": sorted(pages)})
    if pages:
        checks |= _page_checks(conn, path, sorted(pages), None)
    return "checked", checks


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def validate_run(conn: sqlite3.Connection, run_id: int, *, pathway: str) -> dict:
    """Run every check for this run's source; nothing is written."""
    from duke_rates.db.provenance import RIDER_TARGET

    conn.row_factory = sqlite3.Row
    run = conn.execute("SELECT * FROM extraction_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ProvenanceError(f"no extraction run {run_id}")
    if run["target_kind"] == "tariff_version":
        status, checks = _version_checks(conn, run)
    elif run["target_kind"] == RIDER_TARGET:
        status, checks = _rider_checks(conn, run)
    else:
        status, checks = "not_applicable", {}
    unattended = pathway != REVIEWED
    for name, c in checks.items():
        if name in _UNATTENDED_ONLY or name == "evidence_pages_known":
            c["blocking"] = c["blocking"] and unattended
    failed = sorted(n for n, c in checks.items() if c["blocking"] and c["ok"] is not True)
    if status == "blocked":
        failed = failed or ["source_present"]
    verdict = "not_applicable" if status == "not_applicable" else ("blocked" if failed else "pass")
    return {"run_id": run_id, "target_kind": run["target_kind"], "target_id": run["target_id"],
            "pathway": pathway, "validator": VALIDATOR_VERSION, "verdict": verdict, "failed": failed,
            "checks": checks}


def enforce(conn: sqlite3.Connection, run_id: int, *, pathway: str, requested_by: str, reason: str,
            overrides: Sequence[str] = (), dry_run: bool = False) -> dict:
    """Validate, apply named overrides, record the outcome; raise unless the promotion may proceed."""
    overrides = sorted(set(overrides))
    if overrides and pathway != REVIEWED:
        raise ProvenanceError(f"validation overrides {overrides} are accepted only on the reviewed pathway, "
                              f"not {pathway!r}")
    unknown = [o for o in overrides if o not in OVERRIDABLE]
    if unknown:
        raise ProvenanceError(f"unknown or non-overridable validation checks: {unknown}")
    result = validate_run(conn, run_id, pathway=pathway)
    remaining = [f for f in result["failed"] if f not in overrides]
    if result["verdict"] == "blocked" and not remaining:
        result["verdict"] = "overridden"
    result["overrides"] = [o for o in overrides if o in result["failed"]]
    if dry_run:
        return result
    if _table(conn, "promotion_validations"):
        with atomic(conn):
            cur = conn.execute(
                "INSERT INTO promotion_validations (run_id, target_kind, target_id, pathway, validator_version, "
                "verdict, checks_json, failed_json, overrides_json, requested_by, reason, validated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, result["target_kind"], result["target_id"], pathway, VALIDATOR_VERSION,
                 result["verdict"], json.dumps(result["checks"], default=str), json.dumps(result["failed"]),
                 json.dumps(result["overrides"]), requested_by, reason, _now()))
            result["validation_id"] = cur.lastrowid
    if remaining:
        raise IngestionValidationError(
            f"run {run_id} ({result['target_kind']} {result['target_id']}) failed source validation on the "
            f"{pathway} pathway: {', '.join(remaining)}"
            + ("" if pathway == REVIEWED else " (unattended promotion fails closed; review it with "
               "`duke-rates provenance promote`)"), result)
    return result


def audit_version_sources(conn: sqlite3.Connection, version_ids: Sequence[int] | None = None) -> list[dict]:
    """Shadow-run the version checks over versions that already hold charges (read-only).

    The checks that need a run (its source bytes, its effective-date reading) are left out; what remains
    says whether the charges billing reads today rest on a readable, unchanged, non-redline source.
    """
    conn.row_factory = sqlite3.Row
    sql = ("SELECT tv.id, tv.family_key, COUNT(tc.id) AS charges FROM tariff_versions tv "
           "JOIN tariff_charges tc ON tc.version_id = tv.id")
    params: list = []
    if version_ids:
        sql += f" WHERE tv.id IN ({','.join('?' * len(version_ids))})"
        params = list(version_ids)
    out = []
    for v in conn.execute(sql + " GROUP BY tv.id ORDER BY tv.id", params).fetchall():
        run = {"id": None, "target_id": v["id"], "document_sha256": None, "document_sha256_status": None,
               "interpreted_effective_start": None}
        status, checks = _version_checks(conn, run)
        checks.pop("effective_date_printed", None)
        failed = sorted(n for n, c in checks.items() if c["blocking"] and c["ok"] is not True)
        out.append({"tariff_version_id": v["id"], "family_key": v["family_key"], "charges": v["charges"],
                    "status": status, "failed": failed, "checks": checks})
    return out


def version_source_redline(conn: sqlite3.Connection, version_id: int | None) -> dict | None:
    """Redline findings on a version's bound pages, for writers that add charges without a run.

    Returns the failed redline checks (page markup, stored fingerprint, redline-duplicate assertion) or
    ``None``. A version without a readable source returns ``None``: those writers keep their own
    validation (for example the LLM row validation chain), and missing files are reported elsewhere.
    """
    if version_id is None or not _table(conn, "historical_documents"):
        return None
    hd = conn.execute("SELECT hd.id, hd.local_path, hd.start_page, hd.end_page FROM tariff_versions tv "
                      "JOIN historical_documents hd ON hd.id = tv.historical_document_id WHERE tv.id = ?",
                      (version_id,)).fetchone()
    if hd is None or not hd[1] or not (_identity(hd[1]) and _identity(hd[1]).sha256):
        return None
    start, end = hd[2] or 1, hd[3] or hd[2] or 1
    checks = _page_checks(conn, hd[1], list(range(start, end + 1)), hd[0])
    failed = {n: c["detail"] for n, c in checks.items() if c["ok"] is not True}
    return {"version_id": version_id, "failed": failed, "validator": VALIDATOR_VERSION} if failed else None
