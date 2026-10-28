"""Phase 2E D1 schema: regulatory clue ledger, lineage links, typed gaps, search ledger.

Installed only through :func:`duke_rates.db.provenance_schema.ensure_provenance_schema`
(schema version 3), so it follows the same opt-in, scratch-first migration and
rollback as the rest of the provenance schema. No legacy table is altered and
nothing here is read by extraction or billing.

Three kinds of statement are kept apart:

* **Source-observed**: ``regulatory_clues.printed_text`` with
  ``label_source = 'source_observed'``. The text is a literal slice
  ``[char_start:char_end]`` of the cited page's artifact text, whose SHA-256 is
  stored with it.
* **Machine interpretation**: every ``normalized_value`` / ``normalized_json``,
  every ``lineage_links`` / ``lineage_gaps_typed`` / ``gap_candidates`` row. They
  record the rule that produced them and the clue ids they rest on.
* **Independently verified**: ``verification_status = 'verified'`` (clues),
  ``human_review = 'confirmed'`` (links) and ``verification_status = 'verified'``
  (candidates). Only a named reviewer sets these; no builder does.

Every derived table is keyed by a content-addressed ``*_uid`` so rebuilding is
idempotent, and a rebuild never overwrites review columns.
"""

from __future__ import annotations

CLUE_KINDS: tuple[str, ...] = (
    "docket",
    "order_date",
    "effective_date",
    "leaf",
    "schedule_code",
    "revision",
    "supersedes",
    "approval_reference",
    "utility",
)

GAP_TYPES: tuple[str, ...] = (
    "missing_predecessor_revision",
    "missing_intermediate_revision",
    "missing_successor_document",
    "missing_official_clean_tariff",
    "missing_regulatory_order",
    "missing_effective_date_evidence",
    "unresolved_version_identity",
    "conflicting_supersession",
)

GAP_STATUSES: tuple[str, ...] = (
    "open",
    "candidate_found",
    "verification_required",
    "verified",
    "resolved",
    "searched_not_found",
)

SEARCH_CHANNELS: tuple[str, ...] = ("local", "zoom", "portal_params", "portal_orders", "wayback")
SEARCH_OUTCOMES: tuple[str, ...] = ("running", "hits", "no_results", "error", "blocked", "incomplete")

#: Parents before children: ``drop_provenance_schema`` drops in reverse order.
CLUE_LEDGER_TABLES: tuple[str, ...] = (
    "regulatory_clues",
    "clue_scan_pages",
    "clue_associations",
    "lineage_links",
    "lineage_gaps_typed",
    "search_attempts",
    "gap_candidates",
)


def clue_ledger_usage(conn) -> dict[str, int]:
    """Rows a rebuild cannot regenerate: human reviews and search history."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='search_attempts'").fetchone():
        return {}
    q = conn.execute
    return {
        "reviewed_clues": q(
            "SELECT COUNT(*) FROM regulatory_clues WHERE verification_status <> 'unverified'"
        ).fetchone()[0],
        "reviewed_links": q(
            "SELECT COUNT(*) FROM lineage_links WHERE human_review <> 'unreviewed'"
        ).fetchone()[0],
        "reviewed_candidates": q(
            "SELECT COUNT(*) FROM gap_candidates WHERE verification_status <> 'unreviewed'"
        ).fetchone()[0],
        "search_attempts": q("SELECT COUNT(*) FROM search_attempts").fetchone()[0],
    }


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


CLUE_LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS regulatory_clues (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    clue_uid            TEXT NOT NULL UNIQUE,
    source_pdf          TEXT NOT NULL,
    file_sha256         TEXT NOT NULL,
    file_sha256_status  TEXT NOT NULL CHECK (file_sha256_status IN ('computed', 'stored_unverified')),
    page                INTEGER NOT NULL,
    page_artifact_id    INTEGER,
    artifact_version    TEXT NOT NULL,
    text_layer          TEXT NOT NULL CHECK (text_layer IN ('native_text', 'ocr', 'unknown')),
    page_text_sha256    TEXT NOT NULL,
    char_start          INTEGER,
    char_end            INTEGER,
    bbox_json           TEXT,
    bbox_status         TEXT NOT NULL
        CHECK (bbox_status IN ('located', 'not_found', 'ambiguous', 'no_text_layer',
                               'not_attempted', 'pdf_unavailable')),
    clue_kind           TEXT NOT NULL CHECK (clue_kind IN ({_in(CLUE_KINDS)})),
    role                TEXT,
    printed_text        TEXT NOT NULL,
    normalized_value    TEXT,
    normalized_json     TEXT NOT NULL DEFAULT '{{}}',
    normalization       TEXT NOT NULL
        CHECK (normalization IN ('exact', 'normalized', 'ocr_repaired', 'redline_ambiguous',
                                 'blank', 'unparsed')),
    label_source        TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN ('unverified', 'verified', 'rejected')),
    verified_by         TEXT,
    verified_at         TEXT,
    verification_ref    TEXT,
    extractor           TEXT NOT NULL,
    extractor_version   TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regulatory_clues_page
ON regulatory_clues(file_sha256, page, clue_kind);
CREATE INDEX IF NOT EXISTS idx_regulatory_clues_kind_value
ON regulatory_clues(clue_kind, normalized_value);

-- Which pages were scanned, so absence of a clue is distinguishable from "not looked at".
CREATE TABLE IF NOT EXISTS clue_scan_pages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_sha256       TEXT NOT NULL,
    page              INTEGER NOT NULL,
    source_pdf        TEXT NOT NULL,
    page_artifact_id  INTEGER,
    artifact_version  TEXT NOT NULL,
    text_layer        TEXT NOT NULL,
    page_text_sha256  TEXT NOT NULL,
    clue_count        INTEGER NOT NULL,
    is_index_page     INTEGER NOT NULL DEFAULT 0,
    extractor_version TEXT NOT NULL,
    scan_reason       TEXT NOT NULL,
    scanned_at        TEXT NOT NULL,
    UNIQUE(file_sha256, page, page_text_sha256, extractor_version)
);

-- A clue is attributed to a document/version only when its page lies inside the bound span.
CREATE TABLE IF NOT EXISTS clue_associations (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    clue_id                INTEGER NOT NULL REFERENCES regulatory_clues(id),
    historical_document_id INTEGER NOT NULL,
    tariff_version_id      INTEGER,
    family_key             TEXT,
    basis                  TEXT NOT NULL,
    document_context_json  TEXT NOT NULL DEFAULT '{{}}',
    created_at             TEXT NOT NULL,
    UNIQUE(clue_id, historical_document_id, tariff_version_id)
);
CREATE INDEX IF NOT EXISTS idx_clue_associations_version
ON clue_associations(tariff_version_id);
CREATE INDEX IF NOT EXISTS idx_clue_associations_hd
ON clue_associations(historical_document_id);

CREATE TABLE IF NOT EXISTS lineage_links (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    link_uid             TEXT NOT NULL UNIQUE,
    link_kind            TEXT NOT NULL CHECK (link_kind IN ('supersedes')),
    family_key           TEXT NOT NULL,
    from_version_id      INTEGER NOT NULL,
    to_version_id        INTEGER,
    clue_id              INTEGER NOT NULL REFERENCES regulatory_clues(id),
    predecessor_clue_id  INTEGER REFERENCES regulatory_clues(id),
    expected_json        TEXT NOT NULL,
    status               TEXT NOT NULL
        CHECK (status IN ('explicit_unresolved', 'candidate', 'verified', 'conflict')),
    verification_rule    TEXT NOT NULL,
    rule_checks_json     TEXT NOT NULL DEFAULT '{{}}',
    human_review         TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (human_review IN ('unreviewed', 'confirmed', 'rejected')),
    reviewed_by          TEXT,
    reviewed_at          TEXT,
    review_note          TEXT,
    builder_version      TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_links_from ON lineage_links(from_version_id);
CREATE INDEX IF NOT EXISTS idx_lineage_links_to ON lineage_links(to_version_id);

CREATE TABLE IF NOT EXISTS lineage_gaps_typed (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_uid                      TEXT NOT NULL UNIQUE,
    gap_type                     TEXT NOT NULL CHECK (gap_type IN ({_in(GAP_TYPES)})),
    evidence_basis               TEXT NOT NULL CHECK (evidence_basis IN ('explicit', 'inferred')),
    family_key                   TEXT NOT NULL,
    utility                      TEXT,
    anchor_version_id            INTEGER,
    known_predecessor_version_id INTEGER,
    known_successor_version_id   INTEGER,
    expected_revision            TEXT,
    expected_json                TEXT NOT NULL DEFAULT '{{}}',
    docket_candidates_json       TEXT NOT NULL DEFAULT '[]',
    window_start                 TEXT,
    window_end                   TEXT,
    supporting_clue_ids_json     TEXT NOT NULL DEFAULT '[]',
    link_id                      INTEGER REFERENCES lineage_links(id),
    status                       TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ({_in(GAP_STATUSES)})),
    status_reason                TEXT,
    searched_channels_json       TEXT NOT NULL DEFAULT '[]',
    active                       INTEGER NOT NULL DEFAULT 1,
    builder_version              TEXT NOT NULL,
    created_at                   TEXT NOT NULL,
    updated_at                   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_gaps_family ON lineage_gaps_typed(family_key, gap_type);

CREATE TABLE IF NOT EXISTS gap_candidates (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_uid               TEXT NOT NULL UNIQUE,
    gap_id                      INTEGER NOT NULL REFERENCES lineage_gaps_typed(id),
    search_attempt_id           INTEGER REFERENCES search_attempts(id),
    source_pdf                  TEXT NOT NULL,
    file_sha256                 TEXT NOT NULL,
    start_page                  INTEGER NOT NULL,
    end_page                    INTEGER NOT NULL,
    family_key_guess            TEXT,
    bound_historical_document_ids_json TEXT NOT NULL DEFAULT '[]',
    matched_clue_ids_json       TEXT NOT NULL DEFAULT '[]',
    match_json                  TEXT NOT NULL DEFAULT '{{}}',
    match_strength              TEXT NOT NULL CHECK (match_strength IN ('full', 'partial')),
    rationale                   TEXT NOT NULL,
    verification_status         TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (verification_status IN ('unreviewed', 'verified', 'rejected')),
    reviewed_by                 TEXT,
    reviewed_at                 TEXT,
    review_note                 TEXT,
    created_at                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gap_candidates_gap ON gap_candidates(gap_id);

CREATE TABLE IF NOT EXISTS search_attempts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_id            INTEGER REFERENCES lineage_gaps_typed(id),
    channel           TEXT NOT NULL CHECK (channel IN ({_in(SEARCH_CHANNELS)})),
    caller            TEXT NOT NULL,
    params_json       TEXT NOT NULL,
    params_hash       TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    result_count      INTEGER,
    pages_reached     INTEGER,
    outcome           TEXT NOT NULL DEFAULT 'running' CHECK (outcome IN ({_in(SEARCH_OUTCOMES)})),
    error_detail      TEXT,
    result_refs_json  TEXT NOT NULL DEFAULT '[]',
    created_by        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_attempts_hash ON search_attempts(params_hash, outcome);
CREATE INDEX IF NOT EXISTS idx_search_attempts_gap ON search_attempts(gap_id);
"""
