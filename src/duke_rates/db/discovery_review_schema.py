"""Phase 2E D2/D3 schema: remote candidates, effective-date assessments, reviewed promotion.

Installed with the rest of the provenance schema (version 4; D3 tables: version 5) by
:func:`duke_rates.db.provenance_schema.ensure_provenance_schema`. No legacy table
is altered by the DDL. The only legacy writes are made by an *applied*,
*approved* promotion proposal, inside one transaction, after its snapshot.

Lifecycles keep machine findings and human decisions apart:

* ``remote_document_candidates.status``: ``listed`` (a search returned it) →
  ``retrieved`` (file downloaded, SHA-256 known) → ``identity_checked`` (its own
  pages were read; the result is in ``identity_json``) → ``needs_review``.
  ``linked`` and ``rejected`` are set only by a named reviewer. A docket match
  never moves a candidate past ``listed``.
* ``effective_date_assessments.status`` is a rule result. ``reviewed_value``
  plus ``reviewed_by`` is the only authoritative correction, and nothing writes it
  back to ``tariff_versions`` in D2.
* ``promotion_proposals.state``: ``proposed`` → ``approved`` (named reviewer) →
  ``applied`` → ``rolled_back``, or ``rejected`` / ``blocked`` (checks failed).
* ``promotion_validations`` (D3): one row per ``provenance.promote`` call on a
  tariff-version or rider-summary run: the shared source-evidence checks, the
  pathway that called it, and any named override. Append-only.
* ``evidence_review_items`` (D3): the prioritized review queue. ``review_status``
  changes only with a named reviewer; nothing here writes a legacy table.
"""

from __future__ import annotations

REMOTE_CANDIDATE_KINDS: tuple[str, ...] = ("order", "tariff_document")
REMOTE_CANDIDATE_STATUSES: tuple[str, ...] = (
    "listed", "retrieved", "identity_checked", "needs_review", "linked", "rejected",
)
DATE_ASSESSMENT_STATUSES: tuple[str, ...] = (
    "agree", "conflict", "ambiguous", "stored_only", "observed_only", "none", "reviewed",
)
PROPOSAL_KINDS: tuple[str, ...] = ("rebind_page_range", "mark_redline_duplicate", "bind_unbound_document")
PROPOSAL_STATES: tuple[str, ...] = (
    "proposed", "blocked", "approved", "applied", "rolled_back", "rejected",
)
VALIDATION_VERDICTS: tuple[str, ...] = ("pass", "blocked", "overridden", "not_applicable")
REVIEW_ITEM_STATUSES: tuple[str, ...] = ("open", "accepted", "rejected", "deferred")

#: ``search_attempts`` columns added in schema v4: (table, column, type).
SEARCH_ATTEMPT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("search_attempts", "stop_reason", "TEXT"),
    ("search_attempts", "pages_total", "INTEGER"),
    ("search_attempts", "items_reported", "INTEGER"),
    ("search_attempts", "filter_verified", "INTEGER"),
)

#: Parents before children: dropped in reverse order.
DISCOVERY_REVIEW_TABLES: tuple[str, ...] = (
    "remote_document_candidates",
    "effective_date_assessments",
    "promotion_proposals",
    "promotion_snapshots",
    "document_assertions",
    "promotion_validations",
    "evidence_review_items",
)


def discovery_review_usage(conn) -> dict[str, int]:
    """Rows a rebuild cannot regenerate: reviews, proposals, snapshots, assertions."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='promotion_proposals'").fetchone():
        return {}
    q = conn.execute
    return {
        "remote_candidates": q("SELECT COUNT(*) FROM remote_document_candidates").fetchone()[0],
        "reviewed_date_assessments": q(
            "SELECT COUNT(*) FROM effective_date_assessments WHERE reviewed_by IS NOT NULL"
        ).fetchone()[0],
        "promotion_proposals": q("SELECT COUNT(*) FROM promotion_proposals").fetchone()[0],
        "document_assertions": q("SELECT COUNT(*) FROM document_assertions").fetchone()[0],
        **({"promotion_validations": q("SELECT COUNT(*) FROM promotion_validations").fetchone()[0],
            "reviewed_review_items": q("SELECT COUNT(*) FROM evidence_review_items "
                                       "WHERE reviewed_by IS NOT NULL").fetchone()[0]}
           if q("SELECT 1 FROM sqlite_master WHERE name='evidence_review_items'").fetchone() else {}),
    }


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


DISCOVERY_REVIEW_DDL = f"""
-- A document a network search listed. Never authoritative by itself.
CREATE TABLE IF NOT EXISTS remote_document_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_uid       TEXT NOT NULL UNIQUE,
    kind                TEXT NOT NULL CHECK (kind IN ({_in(REMOTE_CANDIDATE_KINDS)})),
    gap_id              INTEGER REFERENCES lineage_gaps_typed(id),
    target_json         TEXT NOT NULL DEFAULT '{{}}',   -- what was being looked for (docket, order date, revision)
    search_attempt_id   INTEGER NOT NULL REFERENCES search_attempts(id),
    channel             TEXT NOT NULL,
    params_hash         TEXT NOT NULL,
    portal_document_id  TEXT,
    detail_url          TEXT,
    view_file_urls_json TEXT NOT NULL DEFAULT '[]',
    description         TEXT,
    filing_type         TEXT,
    docket_number       TEXT,
    date_filed          TEXT,
    listing_json        TEXT NOT NULL DEFAULT '{{}}',    -- the search row exactly as parsed
    score_json          TEXT NOT NULL DEFAULT '{{}}',    -- per-criterion ranking (machine)
    local_path          TEXT,
    file_sha256         TEXT,
    retrieved_from      TEXT,
    retrieved_at        TEXT,
    retrieval_json      TEXT NOT NULL DEFAULT '{{}}',    -- HTTP status, bytes, dedupe decision
    identity_json       TEXT NOT NULL DEFAULT '{{}}',    -- clue ids / page evidence once read
    status              TEXT NOT NULL DEFAULT 'listed'
        CHECK (status IN ({_in(REMOTE_CANDIDATE_STATUSES)})),
    linked_version_id   INTEGER,
    reviewed_by         TEXT,
    reviewed_at         TEXT,
    review_note         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_remote_candidates_gap ON remote_document_candidates(gap_id, kind);
CREATE INDEX IF NOT EXISTS idx_remote_candidates_doc ON remote_document_candidates(portal_document_id);

-- Stored vs printed vs inferred effective date for one version. Rule output, rebuildable;
-- the reviewed_* columns survive rebuilds.
CREATE TABLE IF NOT EXISTS effective_date_assessments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tariff_version_id   INTEGER NOT NULL UNIQUE,
    family_key          TEXT NOT NULL,
    stored_value        TEXT,                    -- tariff_versions.effective_start (as stored)
    observed_json       TEXT NOT NULL DEFAULT '[]',  -- [{{date, clue_id, page, file_sha256, role}}]
    inferred_json       TEXT NOT NULL DEFAULT '[]',  -- [{{date, basis}}]: never treated as observed
    status              TEXT NOT NULL CHECK (status IN ({_in(DATE_ASSESSMENT_STATUSES)})),
    evaluation_window_json TEXT NOT NULL DEFAULT '[]',  -- [earliest, latest] candidate dates for gap windows
    rule                TEXT NOT NULL,
    reviewed_value      TEXT,
    reviewed_by         TEXT,
    reviewed_at         TEXT,
    review_note         TEXT,
    builder_version     TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promotion_proposals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                TEXT NOT NULL CHECK (kind IN ({_in(PROPOSAL_KINDS)})),
    historical_document_id INTEGER,          -- bind_unbound_document: set when applied
    tariff_version_id   INTEGER,
    params_json         TEXT NOT NULL,           -- e.g. {{start_page, end_page}} or {{duplicate_of_hd}}
    evidence_json       TEXT NOT NULL DEFAULT '{{}}',  -- gap/candidate/clue ids the proposal rests on
    checks_json         TEXT NOT NULL DEFAULT '{{}}',  -- every automatic check, pass/fail + detail
    state               TEXT NOT NULL CHECK (state IN ({_in(PROPOSAL_STATES)})),
    state_reason        TEXT,
    proposed_by         TEXT NOT NULL,
    proposed_at         TEXT NOT NULL,
    approved_by         TEXT,
    approved_at         TEXT,
    approval_note       TEXT,
    applied_by          TEXT,
    applied_at          TEXT,
    apply_result_json   TEXT,
    rolled_back_by      TEXT,
    rolled_back_at      TEXT
);

-- Exact rows before (and after) an applied proposal, for audit and restoration.
CREATE TABLE IF NOT EXISTS promotion_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id         INTEGER NOT NULL REFERENCES promotion_proposals(id),
    phase               TEXT NOT NULL CHECK (phase IN ('before', 'after')),
    table_name          TEXT NOT NULL,
    row_key             TEXT NOT NULL,
    row_json            TEXT,                    -- NULL: the row did not exist in this phase
    taken_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_promotion_snapshots_proposal ON promotion_snapshots(proposal_id, phase);

-- Reviewed statements about a document that the legacy schema has no column for
-- (for example, "this span is the redline duplicate of hd 7265").
CREATE TABLE IF NOT EXISTS document_assertions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    historical_document_id INTEGER NOT NULL,
    assertion           TEXT NOT NULL CHECK (assertion IN ('redline_duplicate_of')),
    target_historical_document_id INTEGER,
    evidence_json       TEXT NOT NULL DEFAULT '{{}}',
    proposal_id         INTEGER REFERENCES promotion_proposals(id),
    asserted_by         TEXT NOT NULL,
    asserted_at         TEXT NOT NULL,
    retracted_at        TEXT,
    UNIQUE(historical_document_id, assertion, target_historical_document_id)
);

-- D3: what the shared ingestion validation found for each charge/rider promotion.
CREATE TABLE IF NOT EXISTS promotion_validations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL,
    target_kind         TEXT NOT NULL,
    target_id           INTEGER NOT NULL,
    pathway             TEXT NOT NULL,           -- reviewed | automated:<caller>
    validator_version   TEXT NOT NULL,
    verdict             TEXT NOT NULL CHECK (verdict IN ({_in(VALIDATION_VERDICTS)})),
    checks_json         TEXT NOT NULL,           -- every check: ok / blocking / detail
    failed_json         TEXT NOT NULL DEFAULT '[]',
    overrides_json      TEXT NOT NULL DEFAULT '[]',
    requested_by        TEXT NOT NULL,
    reason              TEXT,
    validated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_promotion_validations_run ON promotion_validations(run_id);

-- D3: prioritized evidence review queue (date conflicts, redline candidates, verified bindings, ...).
CREATE TABLE IF NOT EXISTS evidence_review_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_uid            TEXT NOT NULL UNIQUE,
    problem_type        TEXT NOT NULL,
    priority            INTEGER NOT NULL,        -- 1 = may change a billed charge
    family_key          TEXT,
    tariff_version_id   INTEGER,
    historical_document_id INTEGER,
    source_documents_json TEXT NOT NULL DEFAULT '[]',  -- [{{path, sha256, pages}}]
    printed_evidence_json TEXT NOT NULL DEFAULT '{{}}',
    db_values_json      TEXT NOT NULL DEFAULT '{{}}',
    explanation         TEXT NOT NULL,
    cause               TEXT,                    -- classification, when evidence supports one
    proposed_correction_json TEXT,
    evidence_level      TEXT NOT NULL,           -- rule | assistant_page_review | human_review
    downstream_json     TEXT NOT NULL DEFAULT '{{}}',
    review_status       TEXT NOT NULL DEFAULT 'open' CHECK (review_status IN ({_in(REVIEW_ITEM_STATUSES)})),
    reviewed_by         TEXT,
    reviewed_at         TEXT,
    review_note         TEXT,
    builder_version     TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
"""


def widen_proposal_kinds(conn) -> None:
    """Rebuild a v4 ``promotion_proposals`` so its CHECK admits every proposal kind (rows and ids kept)."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='promotion_proposals'").fetchone()
    if row is None or all(f"'{k}'" in row[0] for k in PROPOSAL_KINDS):
        return
    start = DISCOVERY_REVIEW_DDL.index("CREATE TABLE IF NOT EXISTS promotion_proposals (")
    ddl = DISCOVERY_REVIEW_DDL[start: DISCOVERY_REVIEW_DDL.index(");", start) + 2]
    ddl = ddl.replace("IF NOT EXISTS promotion_proposals (", "promotion_proposals__v5 (")
    cols = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(promotion_proposals)"))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")  # snapshots/assertions keep REFERENCES promotion_proposals
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM promotion_proposals").fetchone()
        conn.execute(ddl)
        conn.execute(f"INSERT INTO promotion_proposals__v5 ({cols}) SELECT {cols} FROM promotion_proposals")
        conn.execute("DROP TABLE promotion_proposals")
        conn.execute("ALTER TABLE promotion_proposals__v5 RENAME TO promotion_proposals")
        after = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM promotion_proposals").fetchone()
        if tuple(before) != tuple(after):
            raise RuntimeError(f"promotion_proposals rebuild lost rows: {tuple(before)} -> {tuple(after)}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
