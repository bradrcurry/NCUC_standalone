"""Phase 2A provenance schema: immutable extraction runs, promotion, audit.

This schema is **opt-in**. ``duke_rates.db.sqlite.connect`` does not install
it; callers apply it explicitly with :func:`ensure_provenance_schema` (the
``duke-rates provenance migrate`` command). That keeps every existing
database byte-identical until someone migrates it on purpose, and lets the
migration be tested and rolled back on scratch copies first.

Everything here is additive. No existing table or column is altered. The
legacy tables (``tariff_charges``, ``historical_documents.parsed_result_json``,
``bill_statements.statement_json``) stay the read model; promotion
materialises the active run into them (see ``duke_rates.db.provenance``).

Objects created:

* ``extraction_runs`` / ``extracted_charges`` / ``extracted_documents`` --
  one run per extraction attempt and its output. Rows are append-only; a run
  becomes fully immutable once ``sealed_at`` is set.
* ``active_results`` / ``promotion_log`` / ``materialized_charges`` -- which
  run is live per target, the full promote/rollback/reject history, and which
  ``tariff_charges`` rows a promotion created.
* ``charge_overrides`` -- curated adds/replaces/suppressions applied on
  promotion.
* ``metadata_revisions`` -- column-level history of material tariff metadata,
  filled by triggers so that *every* writer (including the ad-hoc scripts that
  bypass ``Repository``) leaves a receipt.
* ``document_payload_revisions`` -- the previous value of every overwritten
  ``parsed_result_json`` / ``statement_json``.
* ``tariff_charge_revisions`` -- every insert/update/delete on
  ``tariff_charges``, with the full old row, so a destructive re-extraction
  can always be reconstructed.
* ``regulatory_clues`` ... ``search_attempts`` (schema v3) -- the Phase 2E D1
  clue ledger; see ``duke_rates.db.clue_ledger_schema``.
* ``provenance_write_context`` -- a single-row table that explicit write APIs
  fill for the duration of a transaction, so trigger-written receipts carry
  the originating process. Writers that do not set it are recorded as
  ``unattributed``.
"""

from __future__ import annotations

import sqlite3

from duke_rates.db.clue_ledger_schema import CLUE_LEDGER_DDL, CLUE_LEDGER_TABLES, clue_ledger_usage

PROVENANCE_SCHEMA_VERSION = 3  # 2: rider-summary runs (Phase 2C); 3: clue ledger (Phase 2E D1)

# Columns whose changes are audited, per legacy table.
AUDITED_METADATA_COLUMNS: dict[str, tuple[str, ...]] = {
    "historical_documents": (
        "effective_start",
        "effective_end",
        "family_key",
        "revision_label",
        "supersedes_label",
        "leaf_no",
        "status",
        "current_document_id",
        "approved_document_id",
        "content_hash",
        "local_path",
        "start_page",
        "end_page",
    ),
    "tariff_versions": (
        "effective_start",
        "effective_end",
        "family_key",
        "document_id",
        "historical_document_id",
        "revision_label",
        "supersedes_label",
        "status",
        "approved_version_id",
    ),
    "tariff_families": (
        "family_key",
        "current_document_id",
    ),
}

# Large JSON payload columns whose previous value is kept on overwrite.
AUDITED_PAYLOAD_COLUMNS: dict[str, str] = {
    "historical_documents": "parsed_result_json",
    "bill_statements": "statement_json",
}

TARIFF_CHARGE_COLUMNS: tuple[str, ...] = (
    "id",
    "version_id",
    "family_key",
    "charge_type",
    "charge_label",
    "rate_value",
    "rate_unit",
    "tier_min",
    "tier_max",
    "tou_period",
    "season",
    "customer_class",
    "source_snippet",
    "confidence_score",
    "notes",
    "created_at",
)

PROVENANCE_TABLES: tuple[str, ...] = (
    "provenance_schema_meta",
    "provenance_write_context",
    "document_blobs",
    "extraction_runs",
    "extracted_charges",
    "extracted_documents",
    "active_results",
    "promotion_log",
    "materialized_charges",
    "rider_summary_sources",
    "extracted_rider_blocks",
    "extracted_rider_line_items",
    "materialized_rider_blocks",
    "rider_summary_revisions",
    "charge_overrides",
    "metadata_revisions",
    "document_payload_revisions",
    "tariff_charge_revisions",
) + CLUE_LEDGER_TABLES

_ACTOR = "COALESCE((SELECT actor FROM provenance_write_context WHERE id = 1), 'unattributed')"
_OPERATION = "(SELECT operation_id FROM provenance_write_context WHERE id = 1)"
_EVIDENCE = "(SELECT evidence FROM provenance_write_context WHERE id = 1)"
_REVISION_OP = (
    "COALESCE((SELECT revision_operation FROM provenance_write_context WHERE id = 1), 'update')"
)
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

_STATUS_CHECK = "IN ('available', 'unavailable', 'legacy_unverified', 'not_applicable')"

#: Every run target. Schema v1 lacked 'rider_summary_source'; see _widen_target_kinds.
RUN_TARGET_KINDS: tuple[str, ...] = (
    "tariff_version",
    "historical_document",
    "bill_statement",
    "rider_summary_source",
)
_TARGET_KIND_LIST = ", ".join(f"'{k}'" for k in RUN_TARGET_KINDS)

RIDER_BLOCK_COLUMNS: tuple[str, ...] = (
    "id",
    "docket_dir",
    "source_pdf",
    "leaf_no",
    "effective_date",
    "docket_number",
    "order_date",
    "supersedes",
    "rate_class",
    "applicable_schedules_json",
    "total_cents_per_kwh",
    "total_dollars_per_kw",
    "utility",
    "created_at",
)
RIDER_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "block_id",
    "label",
    "rider_code",
    "cents_per_kwh",
    "dollars_per_kw",
    "line_effective_date",
    "is_section_header",
    "is_subtotal",
    "is_total",
    "created_at",
)

PROVENANCE_DDL = f"""
CREATE TABLE IF NOT EXISTS provenance_schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provenance_write_context (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    actor        TEXT NOT NULL,
    operation_id TEXT,
    revision_operation TEXT,
    evidence     TEXT,
    set_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_blobs (
    sha256          TEXT PRIMARY KEY,
    size_bytes      INTEGER NOT NULL,
    first_seen_path TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid               TEXT NOT NULL UNIQUE,
    target_kind            TEXT NOT NULL
        CHECK (target_kind IN ({_TARGET_KIND_LIST})),
    target_id              INTEGER NOT NULL,
    family_key             TEXT,
    version_id             INTEGER,
    source_document_kind   TEXT,
    source_document_id     INTEGER,
    source_path            TEXT,
    document_sha256        TEXT,
    document_sha256_status TEXT NOT NULL
        CHECK (document_sha256_status IN ('computed', 'stored_unverified', 'unavailable')),
    parser_name            TEXT NOT NULL,
    parser_version         TEXT NOT NULL,
    code_fingerprint       TEXT,
    model_id               TEXT,
    model_digest           TEXT,
    config_fingerprint     TEXT,
    config_json            TEXT NOT NULL DEFAULT '{{}}',
    text_source            TEXT,
    extraction_method      TEXT NOT NULL,
    interpreted_effective_start TEXT,
    interpreted_effective_end   TEXT,
    started_at             TEXT NOT NULL,
    finished_at            TEXT,
    sealed_at              TEXT,
    status                 TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'ok', 'empty', 'failed')),
    validation_status      TEXT NOT NULL DEFAULT 'pending'
        CHECK (validation_status IN ('pending', 'passed', 'warnings', 'failed')),
    validation_json        TEXT NOT NULL DEFAULT '[]',
    metrics_json           TEXT NOT NULL DEFAULT '{{}}',
    created_by             TEXT NOT NULL,
    notes                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_target
ON extraction_runs(target_kind, target_id, started_at);

CREATE TABLE IF NOT EXISTS extracted_charges (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  INTEGER NOT NULL REFERENCES extraction_runs(id),
    ordinal                 INTEGER NOT NULL,
    version_id              INTEGER NOT NULL,
    family_key              TEXT NOT NULL,
    charge_type             TEXT NOT NULL,
    charge_label            TEXT,
    rate_value              REAL,
    rate_unit               TEXT,
    printed_value_text      TEXT,
    printed_unit            TEXT,
    canonical_value         REAL,
    canonical_unit          TEXT,
    unit_normalization      TEXT NOT NULL,
    tier_min                REAL,
    tier_max                REAL,
    tou_period              TEXT,
    season                  TEXT,
    customer_class          TEXT,
    applicability_json      TEXT,
    effective_start         TEXT,
    effective_end           TEXT,
    source_pdf_path         TEXT,
    source_pdf_sha256       TEXT,
    source_pdf_status       TEXT NOT NULL CHECK (source_pdf_status {_STATUS_CHECK}),
    page_number             INTEGER,
    page_status             TEXT NOT NULL CHECK (page_status {_STATUS_CHECK}),
    bbox_json               TEXT,
    bbox_status             TEXT NOT NULL CHECK (bbox_status {_STATUS_CHECK}),
    source_quote            TEXT,
    quote_status            TEXT NOT NULL CHECK (quote_status {_STATUS_CHECK}),
    extractor_snippet       TEXT,
    table_ref_json          TEXT,
    extraction_method       TEXT NOT NULL,
    validation_status       TEXT NOT NULL DEFAULT 'pending'
        CHECK (validation_status IN ('pending', 'passed', 'warnings', 'failed')),
    validation_json         TEXT NOT NULL DEFAULT '[]',
    confidence_score        REAL NOT NULL DEFAULT 0.0,
    natural_key             TEXT NOT NULL,
    legacy_tariff_charge_id INTEGER,
    legacy_row_json         TEXT,
    UNIQUE (run_id, ordinal),
    -- Never store a page, bbox, PDF or quote without saying it is available,
    -- and never claim availability without the value.
    -- 'legacy_unverified' marks a value copied from pre-provenance data that
    -- nobody has checked against the source.
    CHECK ((page_status IN ('available', 'legacy_unverified')) = (page_number IS NOT NULL)),
    CHECK ((bbox_status IN ('available', 'legacy_unverified')) = (bbox_json IS NOT NULL)),
    CHECK ((source_pdf_status IN ('available', 'legacy_unverified')) = (source_pdf_path IS NOT NULL)),
    CHECK ((quote_status IN ('available', 'legacy_unverified')) = (source_quote IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_extracted_charges_run ON extracted_charges(run_id);
CREATE INDEX IF NOT EXISTS idx_extracted_charges_version ON extracted_charges(version_id);
CREATE INDEX IF NOT EXISTS idx_extracted_charges_legacy
ON extracted_charges(legacy_tariff_charge_id);

CREATE TABLE IF NOT EXISTS extracted_documents (
    run_id         INTEGER PRIMARY KEY REFERENCES extraction_runs(id),
    payload_kind   TEXT NOT NULL CHECK (payload_kind IN ('parsed_result_json', 'statement_json')),
    payload_json   TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS active_results (
    target_kind TEXT NOT NULL,
    target_id   INTEGER NOT NULL,
    run_id      INTEGER NOT NULL REFERENCES extraction_runs(id),
    promotion_log_id INTEGER,
    promoted_at TEXT NOT NULL,
    promoted_by TEXT NOT NULL,
    reason      TEXT,
    PRIMARY KEY (target_kind, target_id)
);

CREATE TABLE IF NOT EXISTS promotion_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_uuid       TEXT NOT NULL UNIQUE,
    action               TEXT NOT NULL CHECK (action IN ('activate_legacy', 'promote', 'rollback', 'reject')),
    target_kind          TEXT NOT NULL,
    target_id            INTEGER NOT NULL,
    from_run_id          INTEGER,
    to_run_id            INTEGER,
    reverts_log_id       INTEGER REFERENCES promotion_log(id),
    at                   TEXT NOT NULL,
    by                   TEXT NOT NULL,
    reason               TEXT NOT NULL,
    diff_json            TEXT NOT NULL DEFAULT '{{}}',
    before_state_json    TEXT,
    before_state_sha256  TEXT,
    after_state_sha256   TEXT,
    inserted_row_ids_json TEXT NOT NULL DEFAULT '[]',
    deleted_rows_json    TEXT NOT NULL DEFAULT '[]',
    preserved_row_ids_json TEXT NOT NULL DEFAULT '[]',
    superseded_row_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_promotion_log_target ON promotion_log(target_kind, target_id, id);

CREATE TABLE IF NOT EXISTS materialized_charges (
    tariff_charge_id    INTEGER PRIMARY KEY,
    extracted_charge_id INTEGER REFERENCES extracted_charges(id),
    charge_override_id  INTEGER REFERENCES charge_overrides(id),
    promotion_log_id    INTEGER NOT NULL REFERENCES promotion_log(id)
);

-- Rider summaries (Phase 2C). A run's target is one (docket_dir, source_pdf):
-- every rider_summary_blocks row of that source, with its line items.
CREATE TABLE IF NOT EXISTS rider_summary_sources (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    docket_dir TEXT NOT NULL,
    source_pdf TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (docket_dir, source_pdf)
);

CREATE TABLE IF NOT EXISTS extracted_rider_blocks (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                    INTEGER NOT NULL REFERENCES extraction_runs(id),
    ordinal                   INTEGER NOT NULL,
    docket_dir                TEXT NOT NULL,
    source_pdf                TEXT NOT NULL,
    leaf_no                   TEXT,
    effective_date            TEXT,
    docket_number             TEXT,
    order_date                TEXT,
    supersedes                TEXT,
    rate_class                TEXT NOT NULL,
    applicable_schedules_json TEXT NOT NULL DEFAULT '[]',
    total_cents_per_kwh       REAL,
    total_dollars_per_kw      REAL,
    utility                   TEXT,
    checksum_ok               INTEGER,
    evidence_json             TEXT,
    legacy_block_id           INTEGER,
    UNIQUE (run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_extracted_rider_blocks_run ON extracted_rider_blocks(run_id);

CREATE TABLE IF NOT EXISTS extracted_rider_line_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES extraction_runs(id),
    block_id            INTEGER NOT NULL REFERENCES extracted_rider_blocks(id),
    ordinal             INTEGER NOT NULL,
    label               TEXT NOT NULL,
    rider_code          TEXT,
    cents_per_kwh       REAL,
    dollars_per_kw      REAL,
    line_effective_date TEXT,
    is_section_header   INTEGER NOT NULL DEFAULT 0,
    is_subtotal         INTEGER NOT NULL DEFAULT 0,
    is_total            INTEGER NOT NULL DEFAULT 0,
    page_number         INTEGER,
    page_status         TEXT NOT NULL CHECK (page_status {_STATUS_CHECK}),
    bbox_json           TEXT,
    source_quote        TEXT,
    quote_status        TEXT NOT NULL CHECK (quote_status {_STATUS_CHECK}),
    evidence_json       TEXT,
    legacy_item_id      INTEGER,
    UNIQUE (block_id, ordinal),
    CHECK ((page_status IN ('available', 'legacy_unverified')) = (page_number IS NOT NULL)),
    CHECK ((quote_status IN ('available', 'legacy_unverified')) = (source_quote IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_extracted_rider_items_block ON extracted_rider_line_items(block_id);

CREATE TABLE IF NOT EXISTS materialized_rider_blocks (
    rider_summary_block_id INTEGER PRIMARY KEY,
    extracted_block_id     INTEGER NOT NULL REFERENCES extracted_rider_blocks(id),
    promotion_log_id       INTEGER NOT NULL REFERENCES promotion_log(id)
);

-- Every change to the rider read model, whoever made it (promotion, loader,
-- cleanup script), so no reprocessing path can lose a reading silently.
CREATE TABLE IF NOT EXISTS rider_summary_revisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name   TEXT NOT NULL,
    row_id       INTEGER NOT NULL,
    block_id     INTEGER,
    operation    TEXT NOT NULL CHECK (operation IN ('update', 'delete')),
    old_row_json TEXT NOT NULL,
    new_row_json TEXT,
    changed_at   TEXT NOT NULL,
    changed_by   TEXT NOT NULL,
    operation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_rider_summary_revisions_row
ON rider_summary_revisions(table_name, row_id, id);

CREATE TABLE IF NOT EXISTS charge_overrides (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id          INTEGER NOT NULL,
    action              TEXT NOT NULL CHECK (action IN ('add', 'replace', 'suppress')),
    natural_key         TEXT,
    payload_json        TEXT,
    source_kind         TEXT NOT NULL CHECK (source_kind IN ('correction', 'manual', 'llm')),
    source_ref          TEXT NOT NULL,
    evidence_pdf_sha256 TEXT,
    evidence_page       INTEGER,
    evidence_quote      TEXT,
    approved_by         TEXT NOT NULL,
    approved_at         TEXT NOT NULL,
    retired_at          TEXT,
    retired_by          TEXT,
    retire_reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_charge_overrides_version ON charge_overrides(version_id, retired_at);

CREATE TABLE IF NOT EXISTS metadata_revisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name   TEXT NOT NULL,
    row_id       INTEGER NOT NULL,
    column_name  TEXT NOT NULL,
    operation    TEXT NOT NULL CHECK (operation IN ('update', 'delete', 'revert')),
    old_value    TEXT,
    new_value    TEXT,
    changed_at   TEXT NOT NULL,
    changed_by   TEXT NOT NULL,
    operation_id TEXT,
    evidence     TEXT,
    reverted_by  INTEGER REFERENCES metadata_revisions(id)
);
CREATE INDEX IF NOT EXISTS idx_metadata_revisions_row
ON metadata_revisions(table_name, row_id, column_name, id);

CREATE TABLE IF NOT EXISTS document_payload_revisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name   TEXT NOT NULL,
    row_id       INTEGER NOT NULL,
    column_name  TEXT NOT NULL,
    operation    TEXT NOT NULL CHECK (operation IN ('update', 'delete')),
    old_payload  TEXT,
    new_payload_length INTEGER,
    changed_at   TEXT NOT NULL,
    changed_by   TEXT NOT NULL,
    operation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_payload_revisions_row
ON document_payload_revisions(table_name, row_id, id);

CREATE TABLE IF NOT EXISTS tariff_charge_revisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tariff_charge_id INTEGER NOT NULL,
    version_id       INTEGER,
    operation        TEXT NOT NULL CHECK (operation IN ('insert', 'update', 'delete')),
    old_row_json     TEXT,
    new_row_json     TEXT,
    changed_at       TEXT NOT NULL,
    changed_by       TEXT NOT NULL,
    operation_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tariff_charge_revisions_charge
ON tariff_charge_revisions(tariff_charge_id, id);
CREATE INDEX IF NOT EXISTS idx_tariff_charge_revisions_version
ON tariff_charge_revisions(version_id, id);
"""


def _row_json(prefix: str, columns: tuple[str, ...] = TARIFF_CHARGE_COLUMNS) -> str:
    pairs = ", ".join(f"'{col}', {prefix}.{col}" for col in columns)
    return f"json_object({pairs})"


def _immutability_triggers() -> list[str]:
    stmts: list[str] = []
    # A run may be updated only until it is sealed; it can never be deleted.
    stmts.append(
        """
        CREATE TRIGGER IF NOT EXISTS trg_extraction_runs_sealed
        BEFORE UPDATE ON extraction_runs
        WHEN OLD.sealed_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'extraction run is sealed and immutable'); END;
        """
    )
    for table in (
        "extraction_runs",
        "extracted_charges",
        "extracted_documents",
        "promotion_log",
        "metadata_revisions",
        "document_payload_revisions",
        "tariff_charge_revisions",
        "charge_overrides",
        "extracted_rider_blocks",
        "extracted_rider_line_items",
        "rider_summary_revisions",
    ):
        stmts.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
            """
        )
    for table in (
        "extracted_charges",
        "extracted_documents",
        "promotion_log",
        "document_payload_revisions",
        "tariff_charge_revisions",
        "extracted_rider_blocks",
        "extracted_rider_line_items",
        "rider_summary_revisions",
    ):
        stmts.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END;
            """
        )
    for table in (
        "extracted_charges",
        "extracted_documents",
        "extracted_rider_blocks",
        "extracted_rider_line_items",
    ):
        stmts.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_run_open
            BEFORE INSERT ON {table}
            WHEN (SELECT sealed_at FROM extraction_runs WHERE id = NEW.run_id) IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'cannot add output to a sealed extraction run'); END;
            """
        )
    # metadata_revisions: only reverted_by may be set, and only once.
    stmts.append(
        """
        CREATE TRIGGER IF NOT EXISTS trg_metadata_revisions_update
        BEFORE UPDATE ON metadata_revisions
        WHEN OLD.reverted_by IS NOT NULL
          OR NEW.id IS NOT OLD.id OR NEW.table_name IS NOT OLD.table_name
          OR NEW.row_id IS NOT OLD.row_id OR NEW.column_name IS NOT OLD.column_name
          OR NEW.operation IS NOT OLD.operation OR NEW.old_value IS NOT OLD.old_value
          OR NEW.new_value IS NOT OLD.new_value OR NEW.changed_at IS NOT OLD.changed_at
          OR NEW.changed_by IS NOT OLD.changed_by OR NEW.operation_id IS NOT OLD.operation_id
          OR NEW.evidence IS NOT OLD.evidence
        BEGIN SELECT RAISE(ABORT, 'metadata_revisions rows are immutable except reverted_by'); END;
        """
    )
    # charge_overrides: only the retirement fields may be set, and only once.
    stmts.append(
        """
        CREATE TRIGGER IF NOT EXISTS trg_charge_overrides_update
        BEFORE UPDATE ON charge_overrides
        WHEN OLD.retired_at IS NOT NULL
          OR NEW.version_id IS NOT OLD.version_id OR NEW.action IS NOT OLD.action
          OR NEW.natural_key IS NOT OLD.natural_key OR NEW.payload_json IS NOT OLD.payload_json
          OR NEW.source_kind IS NOT OLD.source_kind OR NEW.source_ref IS NOT OLD.source_ref
          OR NEW.approved_by IS NOT OLD.approved_by OR NEW.approved_at IS NOT OLD.approved_at
        BEGIN SELECT RAISE(ABORT, 'charge_overrides rows are immutable except retirement'); END;
        """
    )
    return stmts


def _audit_triggers(conn: sqlite3.Connection) -> list[str]:
    stmts: list[str] = []
    for table, columns in AUDITED_METADATA_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            continue
        for column in columns:
            if column not in present:
                continue
            stmts.append(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_audit_{table}_{column}
                AFTER UPDATE OF {column} ON {table}
                WHEN OLD.{column} IS NOT NEW.{column}
                BEGIN
                    INSERT INTO metadata_revisions
                        (table_name, row_id, column_name, operation, old_value, new_value,
                         changed_at, changed_by, operation_id, evidence)
                    VALUES ('{table}', OLD.id, '{column}', {_REVISION_OP},
                            CAST(OLD.{column} AS TEXT), CAST(NEW.{column} AS TEXT),
                            {_NOW}, {_ACTOR}, {_OPERATION}, {_EVIDENCE});
                END;
                """
            )
        # Deleting a row loses every audited column at once: keep them all.
        audited = [c for c in columns if c in present]
        pairs = ", ".join(f"'{c}', OLD.{c}" for c in audited)
        stmts.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_audit_{table}_delete
            AFTER DELETE ON {table}
            BEGIN
                INSERT INTO metadata_revisions
                    (table_name, row_id, column_name, operation, old_value, new_value,
                     changed_at, changed_by, operation_id, evidence)
                VALUES ('{table}', OLD.id, '*', 'delete', json_object({pairs}), NULL,
                        {_NOW}, {_ACTOR}, {_OPERATION}, {_EVIDENCE});
            END;
            """
        )
    for table, column in AUDITED_PAYLOAD_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            continue
        stmts.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_payload_{table}_{column}
            AFTER UPDATE OF {column} ON {table}
            WHEN OLD.{column} IS NOT NEW.{column}
            BEGIN
                INSERT INTO document_payload_revisions
                    (table_name, row_id, column_name, operation, old_payload,
                     new_payload_length, changed_at, changed_by, operation_id)
                VALUES ('{table}', OLD.id, '{column}', 'update', OLD.{column},
                        length(NEW.{column}), {_NOW}, {_ACTOR}, {_OPERATION});
            END;
            """
        )
        stmts.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_payload_{table}_{column}_delete
            AFTER DELETE ON {table}
            WHEN OLD.{column} IS NOT NULL
            BEGIN
                INSERT INTO document_payload_revisions
                    (table_name, row_id, column_name, operation, old_payload,
                     new_payload_length, changed_at, changed_by, operation_id)
                VALUES ('{table}', OLD.id, '{column}', 'delete', OLD.{column},
                        NULL, {_NOW}, {_ACTOR}, {_OPERATION});
            END;
            """
        )
    for operation, when_old, when_new in (
        ("insert", "NULL", _row_json("NEW")),
        ("update", _row_json("OLD"), _row_json("NEW")),
        ("delete", _row_json("OLD"), "NULL"),
    ):
        row = "NEW" if operation == "insert" else "OLD"
        stmts.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_tariff_charges_{operation}
            AFTER {operation.upper()} ON tariff_charges
            BEGIN
                INSERT INTO tariff_charge_revisions
                    (tariff_charge_id, version_id, operation, old_row_json, new_row_json,
                     changed_at, changed_by, operation_id)
                VALUES ({row}.id, {row}.version_id, '{operation}', {when_old}, {when_new},
                        {_NOW}, {_ACTOR}, {_OPERATION});
            END;
            """
        )
    for table, columns, block_col in (
        ("rider_summary_blocks", RIDER_BLOCK_COLUMNS, "id"),
        ("rider_line_items", RIDER_ITEM_COLUMNS, "block_id"),
    ):
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            continue
        cols = tuple(c for c in columns if c in present)
        for operation, new_json in (("update", _row_json("NEW", cols)), ("delete", "NULL")):
            stmts.append(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_rider_audit_{table}_{operation}
                AFTER {operation.upper()} ON {table}
                BEGIN
                    INSERT INTO rider_summary_revisions
                        (table_name, row_id, block_id, operation, old_row_json, new_row_json,
                         changed_at, changed_by, operation_id)
                    VALUES ('{table}', OLD.id, OLD.{block_col}, '{operation}',
                            {_row_json("OLD", cols)}, {new_json}, {_NOW}, {_ACTOR}, {_OPERATION});
                END;
                """
            )
    return stmts


def _widen_target_kinds(conn: sqlite3.Connection) -> None:
    """Rebuild a schema-v1 ``extraction_runs`` so its CHECK admits every target kind.

    SQLite cannot alter a CHECK constraint, so the table is copied into a new
    one with the current definition (same columns, ids and rows) and renamed.
    Its triggers and index are recreated by the caller. Atomic.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='extraction_runs'"
    ).fetchone()
    if row is None or "rider_summary_source" in row[0]:
        return
    start = PROVENANCE_DDL.index("CREATE TABLE IF NOT EXISTS extraction_runs (")
    ddl = PROVENANCE_DDL[start : PROVENANCE_DDL.index(");", start) + 2]
    ddl = ddl.replace("IF NOT EXISTS extraction_runs (", "extraction_runs__v2 (")
    columns = [r[1] for r in conn.execute("PRAGMA table_info(extraction_runs)")]
    cols = ", ".join(columns)
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    # Keep other tables' REFERENCES/trigger text pointing at "extraction_runs".
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM extraction_runs").fetchone()
        conn.execute(ddl)
        conn.execute(f"INSERT INTO extraction_runs__v2 ({cols}) SELECT {cols} FROM extraction_runs")
        conn.execute("DROP TABLE extraction_runs")
        conn.execute("ALTER TABLE extraction_runs__v2 RENAME TO extraction_runs")
        after = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM extraction_runs").fetchone()
        if tuple(before) != tuple(after):
            raise RuntimeError(f"extraction_runs rebuild lost rows: {tuple(before)} -> {tuple(after)}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")


def provenance_schema_installed(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provenance_schema_meta'"
    ).fetchone()
    if not row:
        return False
    version = conn.execute(
        "SELECT value FROM provenance_schema_meta WHERE key='schema_version'"
    ).fetchone()
    return bool(version)


#: Columns added after a schema version shipped: (table, column, type).
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("extracted_charges", "table_ref_json", "TEXT"),  # Phase 2B: table/row/col/backend
)


def ensure_provenance_schema(conn: sqlite3.Connection) -> None:
    """Create the provenance tables and triggers (idempotent, additive).

    Upgrading a v1 schema rebuilds ``extraction_runs`` to widen its target
    CHECK (rows and ids unchanged); everything else is added.
    """
    _widen_target_kinds(conn)
    conn.executescript(PROVENANCE_DDL)
    conn.executescript(CLUE_LEDGER_DDL)
    for table, column, typedef in _ADDED_COLUMNS:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
    for stmt in _immutability_triggers() + _audit_triggers(conn):
        conn.execute(stmt)
    conn.execute(
        "INSERT INTO provenance_schema_meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(PROVENANCE_SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO provenance_schema_meta(key, value) "
        "VALUES ('installed_at', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
    )
    conn.commit()


class ProvenanceSchemaInUseError(RuntimeError):
    """Dropping the schema would discard non-legacy runs or audit history."""


def provenance_usage(conn: sqlite3.Connection) -> dict[str, int]:
    """Count rows that would be lost if the schema were dropped."""
    if not provenance_schema_installed(conn):
        return {}
    q = conn.execute
    return {
        "non_legacy_runs": q(
            "SELECT COUNT(*) FROM extraction_runs WHERE parser_name <> 'legacy'"
        ).fetchone()[0],
        "promotions": q(
            "SELECT COUNT(*) FROM promotion_log WHERE action <> 'activate_legacy'"
        ).fetchone()[0],
        "metadata_revisions": q("SELECT COUNT(*) FROM metadata_revisions").fetchone()[0],
        "payload_revisions": q("SELECT COUNT(*) FROM document_payload_revisions").fetchone()[0],
        "charge_revisions": q("SELECT COUNT(*) FROM tariff_charge_revisions").fetchone()[0],
        "rider_revisions": q("SELECT COUNT(*) FROM rider_summary_revisions").fetchone()[0]
        if q("SELECT 1 FROM sqlite_master WHERE name='rider_summary_revisions'").fetchone()
        else 0,
        "overrides": q("SELECT COUNT(*) FROM charge_overrides").fetchone()[0],
        **clue_ledger_usage(conn),
    }


def drop_provenance_schema(conn: sqlite3.Connection, *, force: bool = False) -> None:
    """Reverse :func:`ensure_provenance_schema` (migration rollback).

    Legacy tables are never touched. Refuses when the schema holds anything
    other than the legacy backfill, unless ``force`` is set, because that
    history cannot be regenerated.
    """
    usage = provenance_usage(conn)
    if usage and not force and any(usage.values()):
        raise ProvenanceSchemaInUseError(
            f"provenance schema holds non-reproducible history: {usage}"
        )
    triggers = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND "
            "(name LIKE 'trg_audit_%' OR name LIKE 'trg_payload_%' OR name LIKE 'trg_tariff_charges_%' "
            " OR name LIKE 'trg_rider_audit_%' "
            " OR tbl_name IN (" + ",".join("?" * len(PROVENANCE_TABLES)) + "))",
            PROVENANCE_TABLES,
        )
    ]
    for name in triggers:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    for table in reversed(PROVENANCE_TABLES):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
