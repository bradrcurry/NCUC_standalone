SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_page_url TEXT NOT NULL,
    document_url TEXT NOT NULL,
    state TEXT,
    company TEXT,
    category TEXT NOT NULL,
    kind TEXT NOT NULL,
    effective_date TEXT,
    local_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_type TEXT,
    status_code INTEGER,
    discovered_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    metadata_json TEXT,
    tariff_identifier TEXT,
    schedule_code TEXT,
    rev_token TEXT,
    UNIQUE(document_url, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_documents_state_company ON documents(state, company);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);

CREATE TABLE IF NOT EXISTS parse_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    parser_name TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(document_id) REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS historical_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    current_document_id INTEGER,
    family_key TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT,
    company TEXT,
    category TEXT NOT NULL,
    kind TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    archived_url TEXT NOT NULL,
    snapshot_timestamp TEXT NOT NULL,
    local_path TEXT NOT NULL,
    raw_text_path TEXT,
    content_hash TEXT NOT NULL,
    content_type TEXT,
    direct_status_code INTEGER,
    direct_downloadable INTEGER NOT NULL DEFAULT 0,
    revision_label TEXT,
    supersedes_label TEXT,
    leaf_no TEXT,
    effective_start TEXT,
    effective_end TEXT,
    retrieved_at TEXT NOT NULL,
    metadata_json TEXT,
    parsed_result_json TEXT,
    start_page INTEGER,
    end_page INTEGER,
    evidence_json TEXT,
    UNIQUE(archived_url)
);

CREATE INDEX IF NOT EXISTS idx_historical_family
ON historical_documents(family_key, snapshot_timestamp);
CREATE INDEX IF NOT EXISTS idx_historical_state_company ON historical_documents(state, company);

CREATE TABLE IF NOT EXISTS bill_statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    account_number TEXT,
    bill_date TEXT,
    due_date TEXT,
    service_start TEXT,
    service_end TEXT,
    total_amount_due REAL,
    content_hash TEXT NOT NULL,
    raw_text_path TEXT,
    statement_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_path, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_bill_statements_bill_date ON bill_statements(bill_date);

CREATE TABLE IF NOT EXISTS bill_component_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    section_name TEXT NOT NULL,
    rate_code TEXT,
    component_key TEXT NOT NULL,
    component_label TEXT NOT NULL,
    amount REAL NOT NULL,
    service_start TEXT,
    service_end TEXT,
    period_start TEXT,
    period_end TEXT,
    days_in_period INTEGER,
    quantity_basis_kwh REAL,
    inferred_unit TEXT,
    inferred_value REAL,
    confidence REAL NOT NULL,
    notes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(bill_id) REFERENCES bill_statements(id)
);

CREATE INDEX IF NOT EXISTS idx_bill_component_observations_bill
ON bill_component_observations(bill_id);
CREATE INDEX IF NOT EXISTS idx_bill_component_observations_component
ON bill_component_observations(component_key, service_end);

CREATE TABLE IF NOT EXISTS historical_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT NOT NULL,
    target_leaf_no TEXT,
    target_code TEXT,
    target_title TEXT NOT NULL,
    family_type TEXT NOT NULL,
    category TEXT NOT NULL,
    source_class TEXT NOT NULL,
    provenance_class TEXT NOT NULL,
    source_label TEXT,
    source_location TEXT,
    source_url TEXT,
    extracted_url TEXT,
    extracted_title TEXT,
    attachment_url TEXT,
    viewer_url TEXT,
    hostname TEXT,
    path_fragment TEXT,
    filename TEXT,
    docket_number TEXT,
    schedule_code TEXT,
    rider_code TEXT,
    leaf_reference TEXT,
    effective_start TEXT,
    effective_end TEXT,
    extraction_method TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0.0,
    disposition TEXT NOT NULL DEFAULT 'new',
    score_notes_json TEXT NOT NULL,
    notes_json TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_historical_leads_family
ON historical_leads(family_key, confidence_score DESC);
CREATE INDEX IF NOT EXISTS idx_historical_leads_docket
ON historical_leads(docket_number);

CREATE TABLE IF NOT EXISTS candidate_url_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT NOT NULL,
    lead_id INTEGER,
    variant_url TEXT NOT NULL,
    hostname TEXT NOT NULL,
    path_family TEXT NOT NULL,
    filename TEXT,
    heuristic TEXT NOT NULL,
    direct_status_code INTEGER,
    direct_downloadable INTEGER NOT NULL DEFAULT 0,
    wayback_snapshot_count INTEGER NOT NULL DEFAULT 0,
    wayback_first_timestamp TEXT,
    score REAL NOT NULL DEFAULT 0.0,
    disposition TEXT NOT NULL DEFAULT 'candidate',
    notes_json TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(variant_url),
    FOREIGN KEY(lead_id) REFERENCES historical_leads(id)
);

CREATE INDEX IF NOT EXISTS idx_candidate_url_variants_family
ON candidate_url_variants(family_key, score DESC);

CREATE TABLE IF NOT EXISTS historical_search_packs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT NOT NULL UNIQUE,
    target_leaf_no TEXT,
    target_code TEXT,
    target_title TEXT NOT NULL,
    family_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    notes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regulatory_docket_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT NOT NULL,
    docket_number TEXT NOT NULL,
    utility TEXT NOT NULL,
    proceeding_type TEXT,
    date_start TEXT,
    date_end TEXT,
    referenced_codes_json TEXT NOT NULL,
    evidence_source TEXT NOT NULL,
    evidence_source_type TEXT NOT NULL,
    evidence_source_location TEXT,
    title TEXT,
    contains_tariff_text INTEGER NOT NULL DEFAULT 0,
    clue_only INTEGER NOT NULL DEFAULT 1,
    confidence_score REAL NOT NULL DEFAULT 0.0,
    notes_json TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_regulatory_docket_leads_family
ON regulatory_docket_leads(family_key, docket_number);

CREATE TABLE IF NOT EXISTS evidence_anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT NOT NULL,
    anchor_type TEXT NOT NULL,
    anchor_value TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    source_type TEXT NOT NULL,
    source_location TEXT,
    confidence_score REAL NOT NULL DEFAULT 0.0,
    notes_json TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_anchors_family
ON evidence_anchors(family_key, anchor_type);

CREATE TABLE IF NOT EXISTS ncuc_discovery_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    docket_number TEXT,
    sub_number TEXT,
    utility TEXT NOT NULL DEFAULT 'Duke Energy Progress',
    filing_title TEXT,
    filing_date TEXT,
    proceeding_type TEXT,
    filing_classification TEXT NOT NULL DEFAULT 'other',
    exhibit_label TEXT,
    referenced_schedule_codes_json TEXT NOT NULL DEFAULT '[]',
    referenced_rider_codes_json TEXT NOT NULL DEFAULT '[]',
    referenced_leaf_nos_json TEXT NOT NULL DEFAULT '[]',
    family_keys_json TEXT NOT NULL DEFAULT '[]',
    discovered_url TEXT,
    viewer_url TEXT,
    attachment_url TEXT,
    download_url TEXT,
    acquisition_method TEXT NOT NULL DEFAULT 'manual_seed',
    fetch_status TEXT NOT NULL DEFAULT 'pending',
    local_path TEXT,
    content_hash TEXT,
    content_type TEXT,
    file_size_bytes INTEGER,
    provenance_notes_json TEXT NOT NULL DEFAULT '[]',
    search_query TEXT,
    page_title TEXT,
    error_detail TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    fetched_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_ncuc_discovery_docket
ON ncuc_discovery_records(docket_number, filing_date DESC);
CREATE INDEX IF NOT EXISTS idx_ncuc_discovery_status
ON ncuc_discovery_records(fetch_status);
CREATE INDEX IF NOT EXISTS idx_ncuc_discovery_hash
ON ncuc_discovery_records(content_hash);

-- -----------------------------------------------------------------------
-- Generalized versioned tariff schema (Phase 2c / Phase 4a) — ACTIVE
-- -----------------------------------------------------------------------
-- These tables ARE the active billing path for the Phase 4a tariff engine
-- (src/duke_rates/billing/tariff_engine.py).  They are populated by the
-- multi-state PDF parsers (src/duke_rates/parse/) and queried by:
--   • TariffBillingEngine — estimates bills for all 7 state/company combos
--   • cli.py calculate-bill and compare-tariff-rates commands
--   • repository.py read/write helpers
--
-- A separate LEGACY path also exists for the original NC residential work:
--   ncuc_ingest_segments  — raw parsed rate segments from DEP/DEC NCUC filings
--   rider_summary_blocks  — normalized DEP/DEC rider summary rows (Leaf 600)
-- That path is used by ncuc_loader.py:calculate_bill() and the DEP/DEC
-- analytics functions.  Both paths are operational; they do not overlap.
--
-- Do NOT confuse these tables with the legacy path or assume only one is live.
-- -----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tariff_families (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    company TEXT NOT NULL,
    tariff_identifier TEXT,
    schedule_code TEXT,
    family_type TEXT NOT NULL,
    title TEXT,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    current_document_id INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(current_document_id) REFERENCES documents(id)
);

CREATE INDEX IF NOT EXISTS idx_tariff_families_state_company
ON tariff_families(state, company);
CREATE INDEX IF NOT EXISTS idx_tariff_families_schedule_code
ON tariff_families(schedule_code);
CREATE INDEX IF NOT EXISTS idx_tariff_families_tariff_identifier
ON tariff_families(tariff_identifier);

CREATE TABLE IF NOT EXISTS tariff_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT NOT NULL,
    document_id INTEGER,
    historical_document_id INTEGER,
    effective_start TEXT,
    effective_end TEXT,
    revision_label TEXT,
    supersedes_label TEXT,
    source_type TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0.0,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(family_key) REFERENCES tariff_families(family_key),
    FOREIGN KEY(document_id) REFERENCES documents(id),
    FOREIGN KEY(historical_document_id) REFERENCES historical_documents(id)
);

CREATE INDEX IF NOT EXISTS idx_tariff_versions_family
ON tariff_versions(family_key, effective_start);
CREATE INDEX IF NOT EXISTS idx_tariff_versions_document
ON tariff_versions(document_id);

CREATE TABLE IF NOT EXISTS tariff_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    family_key TEXT NOT NULL,
    charge_type TEXT NOT NULL,
    charge_label TEXT,
    rate_value REAL,
    rate_unit TEXT,
    tier_min REAL,
    tier_max REAL,
    tou_period TEXT,
    season TEXT,
    customer_class TEXT,
    source_snippet TEXT,
    confidence_score REAL NOT NULL DEFAULT 0.0,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(version_id) REFERENCES tariff_versions(id),
    FOREIGN KEY(family_key) REFERENCES tariff_families(family_key)
);

CREATE INDEX IF NOT EXISTS idx_tariff_charges_version
ON tariff_charges(version_id);
CREATE INDEX IF NOT EXISTS idx_tariff_charges_family
ON tariff_charges(family_key, charge_type);

CREATE TABLE IF NOT EXISTS rider_applicability (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rider_family_key TEXT NOT NULL,
    applies_to_family_key TEXT NOT NULL,
    mandatory INTEGER NOT NULL DEFAULT 1,
    applicability_notes TEXT,
    effective_start TEXT,
    effective_end TEXT,
    source_type TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    UNIQUE(rider_family_key, applies_to_family_key, effective_start),
    FOREIGN KEY(rider_family_key) REFERENCES tariff_families(family_key),
    FOREIGN KEY(applies_to_family_key) REFERENCES tariff_families(family_key)
);

CREATE INDEX IF NOT EXISTS idx_rider_applicability_rider
ON rider_applicability(rider_family_key);
CREATE INDEX IF NOT EXISTS idx_rider_applicability_base
ON rider_applicability(applies_to_family_key);

-- =========================================================================
-- Classification observability (Option 1 of the classification redesign).
--
-- Goal: every classification decision the pipeline makes (document type,
-- family mapping, parser profile selection, OCR routing, etc.) is recorded
-- here with confidence, evidence, and the runner-up alternatives. Lets us
-- see disagreements and low-confidence decisions instead of silently
-- accepting whichever rule fired first.
--
-- Subject is polymorphic on purpose — we want to classify things beyond
-- historical_documents in the future (raw PDFs, ad-hoc documents, etc.).
-- =========================================================================

CREATE TABLE IF NOT EXISTS document_classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_kind TEXT NOT NULL,            -- 'historical_document' | 'discovery_record' | 'span' | 'pdf_path'
    subject_id TEXT NOT NULL,              -- str of the FK so polymorphism is cheap
    stage TEXT NOT NULL,                   -- 'document_type' | 'family_mapping' | 'parser_profile' | 'ocr_route' | (extensible)
    label TEXT NOT NULL,
    confidence REAL NOT NULL,              -- 0.0..1.0
    classifier TEXT NOT NULL,
    classifier_version TEXT NOT NULL DEFAULT '',
    evidence_json TEXT,                    -- list of {kind, value, weight}
    alternatives_json TEXT,                -- list of [label, score, evidence] for runner-ups
    metadata_json TEXT,
    superseded_by INTEGER,                 -- self-FK when a later classifier overrides
    created_at TEXT NOT NULL,
    UNIQUE(subject_kind, subject_id, stage, classifier, classifier_version),
    FOREIGN KEY(superseded_by) REFERENCES document_classifications(id)
);

CREATE INDEX IF NOT EXISTS idx_classifications_subject
ON document_classifications(subject_kind, subject_id);
CREATE INDEX IF NOT EXISTS idx_classifications_stage
ON document_classifications(stage, label);
CREATE INDEX IF NOT EXISTS idx_classifications_active
ON document_classifications(subject_kind, subject_id, stage)
WHERE superseded_by IS NULL;

-- =========================================================================
-- Document types — taxonomy-as-data for the Phase 2 document_type stage.
--
-- Seeded with a small set of terminal types covering the most common
-- clusters in the corpus. Long-tail types stay as UNKNOWN until cluster
-- reports surface them and a deliberate decision is made to add them.
-- See docs/document_intelligence_roadmap.md Phase 2 for the design.
-- =========================================================================

CREATE TABLE IF NOT EXISTS document_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,             -- e.g. 'TARIFF_SHEET', 'ORDER_FINAL'
    primary_category TEXT NOT NULL,        -- e.g. 'TARIFF_AND_RATE_DOCUMENTS'
    parent_type TEXT,                      -- code of parent type, NULL for top-level
    description TEXT NOT NULL,
    is_terminal INTEGER NOT NULL DEFAULT 1, -- 1 = leaf type a classifier may emit
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_types_category
ON document_types(primary_category);

-- =========================================================================
-- Document fingerprints v2.
--
-- A flat record of "we saw this PDF, here's what its observable signals
-- look like." Independent of any classifier output. Populated for every
-- PDF the pipeline encounters, regardless of whether we know how to
-- classify it yet — this lets us cluster unfamiliar document types
-- before designing classifiers for them.
--
-- '_v2' to coexist with the existing document_fingerprints table used by
-- the redline-detection logic (different feature set, different consumers).
-- =========================================================================

CREATE TABLE IF NOT EXISTS document_fingerprints_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pdf TEXT NOT NULL,
    file_hash TEXT,
    page_count INTEGER,

    text_chars INTEGER,
    has_tables INTEGER,                    -- 0/1
    has_scanned_pages INTEGER,             -- 0/1
    avg_chars_per_page REAL,

    token_signals_json TEXT,               -- {"docket": 3, "tariff": 0, ...}
    first_page_signature TEXT,             -- e.g. 'STATE_OF_NC_UC' | 'VIA_ELECTRONIC_FILING' | 'unknown'
    title_candidates_json TEXT,
    leaf_numbers_json TEXT,
    schedule_codes_json TEXT,
    rider_codes_json TEXT,

    cluster_signature_v1 TEXT,             -- short deterministic string for SQL grouping

    fingerprinter_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_pdf, file_hash, fingerprinter_version)
);

CREATE INDEX IF NOT EXISTS idx_fingerprints_v2_cluster
ON document_fingerprints_v2(cluster_signature_v1);
CREATE INDEX IF NOT EXISTS idx_fingerprints_v2_source
ON document_fingerprints_v2(source_pdf);
"""


def migrate(conn) -> None:
    """Apply additive schema migrations safe to run on existing databases."""
    for col, typedef in [
        ("tariff_identifier", "TEXT"),
        ("schedule_code", "TEXT"),
        ("rev_token", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {typedef}")
            conn.commit()
        except Exception:
            pass  # column already exists
    # Indexes that depend on columns added by migration above
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_tariff_identifier ON documents(tariff_identifier);
        CREATE INDEX IF NOT EXISTS idx_documents_rev_token ON documents(rev_token);
        """
    )

    # --- Migration: add docket/order provenance to tariff_versions ---
    for col, typedef in [
        ("docket_number", "TEXT"),
        ("order_date", "TEXT"),
        ("leaf_no", "TEXT"),
        ("source_pdf", "TEXT"),
        ("docket_dir", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE tariff_versions ADD COLUMN {col} {typedef}")
            conn.commit()
        except Exception:
            pass

    # --- Migration: NCUC ingest tables ---
    conn.executescript(
        """
        -- One row per parsed leaf segment with its extracted rate data
        CREATE TABLE IF NOT EXISTS ncuc_ingest_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            docket_dir TEXT NOT NULL,
            source_pdf TEXT NOT NULL,
            leaf_no TEXT,
            schedule_code TEXT,
            effective_date TEXT,
            revision_label TEXT,
            supersedes TEXT,
            docket_number TEXT,
            order_date TEXT,
            tier INTEGER NOT NULL DEFAULT 1,
            confidence REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL,
            page_start INTEGER,
            page_end INTEGER,
            energy_charges_json TEXT NOT NULL DEFAULT '[]',
            fixed_charges_json TEXT NOT NULL DEFAULT '[]',
            demand_charges_json TEXT NOT NULL DEFAULT '[]',
            raw_segment_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ncuc_segments_docket
        ON ncuc_ingest_segments(docket_dir, schedule_code);
        CREATE INDEX IF NOT EXISTS idx_ncuc_segments_effective
        ON ncuc_ingest_segments(schedule_code, effective_date);

        -- Leaf 600 rider summary: one row per rate-class block per source PDF
        CREATE TABLE IF NOT EXISTS rider_summary_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            docket_dir TEXT NOT NULL,
            source_pdf TEXT NOT NULL,
            leaf_no TEXT,
            effective_date TEXT,
            docket_number TEXT,
            order_date TEXT,
            supersedes TEXT,
            rate_class TEXT NOT NULL,
            applicable_schedules_json TEXT NOT NULL DEFAULT '[]',
            total_cents_per_kwh REAL,
            total_dollars_per_kw REAL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rider_blocks_docket
        ON rider_summary_blocks(docket_dir, rate_class);
        CREATE INDEX IF NOT EXISTS idx_rider_blocks_effective
        ON rider_summary_blocks(effective_date, rate_class);

        -- Individual line items within a rider summary block
        CREATE TABLE IF NOT EXISTS rider_line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            rider_code TEXT,
            cents_per_kwh REAL,
            dollars_per_kw REAL,
            line_effective_date TEXT,
            is_section_header INTEGER NOT NULL DEFAULT 0,
            is_subtotal INTEGER NOT NULL DEFAULT 0,
            is_total INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(block_id) REFERENCES rider_summary_blocks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_rider_items_block
        ON rider_line_items(block_id);
        CREATE INDEX IF NOT EXISTS idx_rider_items_code
        ON rider_line_items(rider_code, line_effective_date);

        -- Document/span fingerprint records produced during ingest parsing.
        CREATE TABLE IF NOT EXISTS document_fingerprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT NOT NULL,
            docket_dir TEXT,
            page_start INTEGER,
            page_end INTEGER,
            leaf_no TEXT,
            schedule_code TEXT,
            title TEXT,
            text_length INTEGER NOT NULL DEFAULT 0,
            line_count INTEGER NOT NULL DEFAULT 0,
            numeric_line_count INTEGER NOT NULL DEFAULT 0,
            has_table_rows INTEGER NOT NULL DEFAULT 0,
            has_rider_summary INTEGER NOT NULL DEFAULT 0,
            review_flags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_document_fingerprints_source
        ON document_fingerprints(source_pdf, page_start, page_end);

        -- Parse-attempt log records for diagnostics and parser learning.
        CREATE TABLE IF NOT EXISTS parse_attempt_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT NOT NULL,
            docket_dir TEXT,
            page_start INTEGER,
            page_end INTEGER,
            parser_stage TEXT NOT NULL,
            parser_profile TEXT,
            status TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.0,
            utility TEXT,
            schedule_code TEXT,
            effective_date TEXT,
            charge_count INTEGER NOT NULL DEFAULT 0,
            review_flags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_parse_attempt_logs_source
        ON parse_attempt_logs(source_pdf, page_start, page_end, created_at);

        -- Review outcomes attached to parse attempts for manual and rule-based feedback.
        CREATE TABLE IF NOT EXISTS parse_review_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parse_attempt_id INTEGER,
            source_pdf TEXT NOT NULL,
            docket_dir TEXT,
            page_start INTEGER,
            page_end INTEGER,
            parser_stage TEXT,
            parser_profile TEXT,
            utility TEXT,
            review_source TEXT NOT NULL,
            outcome TEXT NOT NULL,
            correction_count INTEGER NOT NULL DEFAULT 0,
            notes_json TEXT NOT NULL DEFAULT '{}',
            corrections_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(parse_attempt_id) REFERENCES parse_attempt_logs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_parse_review_outcomes_attempt
        ON parse_review_outcomes(parse_attempt_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_parse_review_outcomes_source
        ON parse_review_outcomes(source_pdf, page_start, page_end, created_at);

        -- Versioned processing runs for historical document extraction.
        CREATE TABLE IF NOT EXISTS historical_processing_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            historical_document_id INTEGER NOT NULL,
            source_pdf TEXT NOT NULL,
            family_key TEXT,
            content_hash TEXT,
            parser_stage TEXT NOT NULL,
            parser_profile TEXT,
            parser_version TEXT,
            processing_mode TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL,
            outcome_quality TEXT,
            charge_count INTEGER NOT NULL DEFAULT 0,
            review_flags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            FOREIGN KEY(historical_document_id) REFERENCES historical_documents(id)
        );
        CREATE INDEX IF NOT EXISTS idx_historical_processing_runs_doc
        ON historical_processing_runs(historical_document_id, completed_at);
        CREATE INDEX IF NOT EXISTS idx_historical_processing_runs_source
        ON historical_processing_runs(source_pdf, completed_at);

        -- Targeted reprocessing queue for historical documents.
        CREATE TABLE IF NOT EXISTS historical_reprocess_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            historical_document_id INTEGER NOT NULL,
            source_pdf TEXT NOT NULL,
            family_key TEXT,
            priority INTEGER NOT NULL DEFAULT 50,
            queue_reason TEXT NOT NULL,
            requested_by TEXT NOT NULL DEFAULT 'system',
            status TEXT NOT NULL DEFAULT 'pending',
            latest_run_id INTEGER,
            error_message TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            requested_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            FOREIGN KEY(historical_document_id) REFERENCES historical_documents(id),
            FOREIGN KEY(latest_run_id) REFERENCES historical_processing_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_historical_reprocess_queue_status
        ON historical_reprocess_queue(status, priority DESC, requested_at);
        CREATE INDEX IF NOT EXISTS idx_historical_reprocess_queue_doc
        ON historical_reprocess_queue(historical_document_id, status, requested_at);

        -- Execution history for missing-document remediation planning and runs.
        CREATE TABLE IF NOT EXISTS missing_doc_remediation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_key TEXT,
            selected_reason TEXT,
            selected_scope TEXT,
            selected_weighted_score REAL,
            executed INTEGER NOT NULL DEFAULT 0,
            before_step_count INTEGER NOT NULL DEFAULT 0,
            after_step_count INTEGER NOT NULL DEFAULT 0,
            before_deferred_discovery_count INTEGER NOT NULL DEFAULT 0,
            before_deferred_historical_count INTEGER NOT NULL DEFAULT 0,
            after_deferred_discovery_count INTEGER NOT NULL DEFAULT 0,
            after_deferred_historical_count INTEGER NOT NULL DEFAULT 0,
            requested_by TEXT NOT NULL DEFAULT 'system',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_missing_doc_remediation_runs_family
        ON missing_doc_remediation_runs(family_key, created_at);
        CREATE INDEX IF NOT EXISTS idx_missing_doc_remediation_runs_reason
        ON missing_doc_remediation_runs(selected_reason, created_at);

        -- OCR artifact cache for scanned document processing.
        CREATE TABLE IF NOT EXISTS ocr_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discovery_record_id INTEGER,
            source_pdf TEXT NOT NULL,
            file_hash TEXT,
            backend TEXT NOT NULL,
            status TEXT NOT NULL,
            text_sidecar_path TEXT,
            pages_sidecar_path TEXT,
            page_count INTEGER NOT NULL DEFAULT 0,
            ocr_confidence REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(discovery_record_id) REFERENCES ncuc_discovery_records(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ocr_artifacts_source
        ON ocr_artifacts(source_pdf, file_hash, backend, updated_at);

        -- OCR work queue for scanned historical/document-mining paths.
        CREATE TABLE IF NOT EXISTS ocr_processing_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discovery_record_id INTEGER,
            source_pdf TEXT NOT NULL,
            file_hash TEXT,
            backend TEXT NOT NULL DEFAULT 'pytesseract_cpu',
            priority INTEGER NOT NULL DEFAULT 50,
            status TEXT NOT NULL DEFAULT 'pending',
            ocr_confidence REAL,
            structure_complexity REAL,
            gpu_candidate INTEGER NOT NULL DEFAULT 0,
            latest_artifact_id INTEGER,
            error_message TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            requested_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            FOREIGN KEY(discovery_record_id) REFERENCES ncuc_discovery_records(id),
            FOREIGN KEY(latest_artifact_id) REFERENCES ocr_artifacts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ocr_processing_queue_status
        ON ocr_processing_queue(status, priority DESC, requested_at);
        CREATE INDEX IF NOT EXISTS idx_ocr_processing_queue_source
        ON ocr_processing_queue(source_pdf, status, requested_at);

        -- Guided workflow action receipts for resumable weak-agent execution.
        CREATE TABLE IF NOT EXISTS workflow_action_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow TEXT NOT NULL,
            action_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'started',
            target_family_key TEXT,
            target_historical_document_id INTEGER,
            target_parser_profile TEXT,
            command_text TEXT,
            requested_limit INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_action_receipts_workflow
        ON workflow_action_receipts(workflow, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_workflow_action_receipts_status
        ON workflow_action_receipts(status, started_at DESC);

        -- Docling structured-conversion artifact cache.
        -- Keyed by (source_pdf, file_hash, backend_version, accelerator).
        -- One row per successful Docling run; updated in place on re-run.
        CREATE TABLE IF NOT EXISTS docling_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discovery_record_id INTEGER,
            source_pdf TEXT NOT NULL,
            file_hash TEXT,
            backend_version TEXT NOT NULL,
            accelerator TEXT NOT NULL DEFAULT 'cpu',
            status TEXT NOT NULL,
            json_sidecar_path TEXT,
            text_sidecar_path TEXT,
            tables_sidecar_path TEXT,
            page_count INTEGER NOT NULL DEFAULT 0,
            conversion_confidence REAL,
            table_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(discovery_record_id) REFERENCES ncuc_discovery_records(id)
        );
        CREATE INDEX IF NOT EXISTS idx_docling_artifacts_source
        ON docling_artifacts(source_pdf, file_hash, backend_version, accelerator, updated_at);

        -- Cached mined page artifacts to avoid re-running full-document text mining.
        CREATE TABLE IF NOT EXISTS ncuc_page_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discovery_record_id INTEGER,
            source_pdf TEXT NOT NULL,
            file_hash TEXT,
            artifact_version TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            text_length INTEGER NOT NULL DEFAULT 0,
            text_content TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_pdf, file_hash, artifact_version, page_number),
            FOREIGN KEY(discovery_record_id) REFERENCES ncuc_discovery_records(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ncuc_page_artifacts_source
        ON ncuc_page_artifacts(source_pdf, file_hash, artifact_version, page_number);

        -- Cached bounded-span artifacts to avoid repeated segmentation work.
        CREATE TABLE IF NOT EXISTS ncuc_span_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discovery_record_id INTEGER,
            source_pdf TEXT NOT NULL,
            file_hash TEXT,
            artifact_version TEXT NOT NULL,
            span_index INTEGER NOT NULL,
            start_page INTEGER NOT NULL,
            end_page INTEGER NOT NULL,
            doc_type TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.0,
            extracted_leaf_nos_json TEXT NOT NULL DEFAULT '[]',
            extracted_schedule_titles_json TEXT NOT NULL DEFAULT '[]',
            header_footer_snippets_json TEXT NOT NULL DEFAULT '[]',
            dates_json TEXT NOT NULL DEFAULT '[]',
            evidence_score_breakdown_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_pdf, file_hash, artifact_version, span_index),
            FOREIGN KEY(discovery_record_id) REFERENCES ncuc_discovery_records(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ncuc_span_artifacts_source
        ON ncuc_span_artifacts(source_pdf, file_hash, artifact_version, start_page, end_page);

        -- EIA Form 861 state-level average retail electricity rates
        -- Source: https://www.eia.gov/electricity/data/state/
        -- Columns map to EIA's "Average retail price" table (¢/kWh by sector)
        CREATE TABLE IF NOT EXISTS eia_state_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            state TEXT NOT NULL,             -- 2-letter abbreviation
            sector TEXT NOT NULL,            -- 'residential','commercial','industrial','transportation','all_sectors'
            avg_cents_per_kwh REAL,          -- average retail price ¢/kWh
            revenue_million_dollars REAL,    -- total revenue ($M)
            sales_million_kwh REAL,          -- total sales (million kWh)
            customers INTEGER,               -- number of customers
            source_file TEXT,                -- original CSV filename
            created_at TEXT NOT NULL,
            UNIQUE(year, state, sector)
        );
        CREATE INDEX IF NOT EXISTS idx_eia_state_rates_state
        ON eia_state_rates(state, year, sector);
        CREATE INDEX IF NOT EXISTS idx_eia_state_rates_year
        ON eia_state_rates(year, sector);

        -- Human-readable descriptions for each rider code
        CREATE TABLE IF NOT EXISTS rider_descriptions (
            rider_code TEXT PRIMARY KEY,
            short_name TEXT NOT NULL,
            full_name TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            created_by_event TEXT,
            rate_type TEXT NOT NULL DEFAULT 'cents_per_kwh',
            applies_to_schedules_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT,
            created_at TEXT NOT NULL
        );

        -- Derived/provisional DEP rider history used when clean Leaf 600 blocks
        -- do not exist for older periods.
        CREATE TABLE IF NOT EXISTS dep_provisional_rider_totals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_code TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            docket_dir TEXT,
            source_pdf TEXT,
            component_count INTEGER NOT NULL DEFAULT 0,
            component_codes_json TEXT NOT NULL DEFAULT '[]',
            provisional_rider_cents_per_kwh REAL,
            coverage_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(schedule_code, effective_date)
        );
        CREATE INDEX IF NOT EXISTS idx_dep_prov_totals_schedule_effective
        ON dep_provisional_rider_totals(schedule_code, effective_date);

        CREATE TABLE IF NOT EXISTS dep_provisional_rider_components (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_id INTEGER NOT NULL,
            schedule_code TEXT NOT NULL,
            effective_date TEXT NOT NULL,          -- sheet-level date: when this provisional total was effective (inherited from dep_provisional_rider_totals)
            rider_code TEXT NOT NULL,
            rider_effective_date TEXT,             -- component-level date: when this specific rider rate changed (may differ from effective_date; use this for per-rider timelines)
            cents_per_kwh REAL,
            docket_dir TEXT,
            source_pdf TEXT,
            component_source_pdf TEXT,
            component_source_docket_dir TEXT,
            parser_source TEXT,
            source_pages TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(schedule_code, effective_date, rider_code),
            FOREIGN KEY(total_id) REFERENCES dep_provisional_rider_totals(id)
        );
        CREATE INDEX IF NOT EXISTS idx_dep_prov_components_schedule_effective
        ON dep_provisional_rider_components(schedule_code, effective_date, rider_code);
        """
    )
    conn.commit()

    # --- Migration: EIA API v2 tables ---
    conn.executescript(
        """
        -- -----------------------------------------------------------------------
        -- EIA retail electricity sales, revenue, price, and customers
        -- Source: EIA API v2 electricity/retail-sales
        -- Facets: stateid (state), sectorid (sector code)
        -- Frequency: annual | monthly | quarterly
        -- Coverage: 2001-01 to present
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS eia_retail_sales (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset                 TEXT NOT NULL DEFAULT 'retail-sales',
            frequency               TEXT NOT NULL,           -- annual | monthly | quarterly
            period                  TEXT NOT NULL,           -- YYYY, YYYY-MM, or YYYY-Qn
            year                    INTEGER,
            month                   INTEGER,                 -- NULL for annual/quarterly
            state                   TEXT NOT NULL,           -- 2-letter code or US
            state_name              TEXT,
            sector                  TEXT NOT NULL,           -- RES | COM | IND | TRA | ALL
            sector_name             TEXT,
            sales_million_kwh       REAL,                    -- million kWh
            revenue_million_dollars REAL,                    -- million USD
            price_cents_per_kwh     REAL,                    -- ¢/kWh
            customers               INTEGER,
            batch_id                TEXT,                    -- ingestion batch identifier
            ingested_at             TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_eia_retail_sales_key
        ON eia_retail_sales(period, state, sector, frequency);
        CREATE INDEX IF NOT EXISTS idx_eia_retail_sales_state_year
        ON eia_retail_sales(state, year, sector, frequency);

        -- -----------------------------------------------------------------------
        -- EIA net electricity generation by state, fuel type, and sector
        -- Source: EIA API v2 electricity/electric-power-operational-data
        -- Facets: location (state), fueltypeid, sectorid (numeric)
        -- Frequency: annual | monthly | quarterly
        -- Coverage: 2001-01 to present
        -- Units: generation_thousand_mwh = thousand MWh; generation_mwh = derived MWh
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS eia_generation_by_fuel (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset                 TEXT NOT NULL DEFAULT 'generation-by-fuel',
            frequency               TEXT NOT NULL,
            period                  TEXT NOT NULL,
            year                    INTEGER,
            month                   INTEGER,
            state                   TEXT NOT NULL,
            sector                  TEXT NOT NULL,           -- numeric EIA sector code (99=all)
            fuel_type               TEXT NOT NULL,           -- NG | COW | NUC | HYC | WND | SUN | ALL etc.
            fuel_type_name          TEXT,
            generation_thousand_mwh REAL,                    -- thousand MWh (EIA native unit)
            generation_mwh          REAL,                    -- MWh (derived: * 1000)
            batch_id                TEXT,
            ingested_at             TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_eia_gen_key
        ON eia_generation_by_fuel(period, state, sector, fuel_type, frequency);
        CREATE INDEX IF NOT EXISTS idx_eia_gen_state_year
        ON eia_generation_by_fuel(state, year, fuel_type, frequency);

        -- -----------------------------------------------------------------------
        -- EIA state electricity profile summary (annual state rankings)
        -- Source: EIA API v2 electricity/state-electricity-profiles/summary
        -- Coverage: 2008 to present, annual only
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS eia_state_profile_summary (
            id                                  INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset                             TEXT NOT NULL DEFAULT 'state-profile-summary',
            frequency                           TEXT NOT NULL DEFAULT 'annual',
            period                              TEXT NOT NULL,
            year                                INTEGER,
            state                               TEXT NOT NULL,
            state_name                          TEXT,
            net_summer_capacity_mw              REAL,        -- MW
            net_generation_mwh                  REAL,        -- MWh
            total_retail_sales_mwh              REAL,        -- MWh
            average_retail_price_cents_per_kwh  REAL,        -- ¢/kWh
            co2_thousand_metric_tons            REAL,
            net_summer_capacity_rank            INTEGER,     -- 1 = largest in US
            net_generation_rank                 INTEGER,
            total_retail_sales_rank             INTEGER,
            average_retail_price_rank           INTEGER,     -- 1 = cheapest
            batch_id                            TEXT,
            ingested_at                         TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_eia_profile_summary_key
        ON eia_state_profile_summary(period, state);
        CREATE INDEX IF NOT EXISTS idx_eia_profile_summary_state_year
        ON eia_state_profile_summary(state, year);

        -- -----------------------------------------------------------------------
        -- EIA state electricity supply and disposition balance
        -- Source: EIA API v2 electricity/state-electricity-profiles/source-disposition
        -- Coverage: 1990 to present, annual only.  Units: MWh.
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS eia_source_disposition (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset                     TEXT NOT NULL DEFAULT 'source-disposition',
            frequency                   TEXT NOT NULL DEFAULT 'annual',
            period                      TEXT NOT NULL,
            year                        INTEGER,
            state                       TEXT NOT NULL,
            state_name                  TEXT,
            total_net_generation_mwh    REAL,
            total_supply_mwh            REAL,
            retail_sales_mwh            REAL,
            net_interstate_trade_mwh    REAL,   -- positive = net exporter
            estimated_losses_mwh        REAL,
            direct_use_mwh              REAL,
            batch_id                    TEXT,
            ingested_at                 TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_eia_source_disp_key
        ON eia_source_disposition(period, state);

        -- -----------------------------------------------------------------------
        -- EIA net summer generating capacity by state and energy source
        -- Source: EIA API v2 electricity/state-electricity-profiles/capability
        -- Coverage: 1990 to present, annual only.  Units: MW.
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS eia_state_capability (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset                 TEXT NOT NULL DEFAULT 'capability',
            frequency               TEXT NOT NULL DEFAULT 'annual',
            period                  TEXT NOT NULL,
            year                    INTEGER,
            state                   TEXT NOT NULL,
            energy_source           TEXT NOT NULL,   -- ALL | NG | NUC | HYC | WND | SOL | COL | PET ...
            producer_type           TEXT NOT NULL,   -- TOT | EU | IPP
            net_summer_capacity_mw  REAL,
            batch_id                TEXT,
            ingested_at             TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_eia_capability_key
        ON eia_state_capability(period, state, energy_source, producer_type);

        -- -----------------------------------------------------------------------
        -- Reference: state -> census region / division
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS eia_state_region_lookup (
            state           TEXT PRIMARY KEY,
            census_division TEXT NOT NULL,
            census_region   TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );

        -- -----------------------------------------------------------------------
        -- Reference: state -> market structure and RTO
        -- Maintained manually; update as state policy changes.
        -- market_structure: regulated | hybrid | restructured
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS eia_market_structure_lookup (
            state            TEXT PRIMARY KEY,
            market_structure TEXT NOT NULL,    -- regulated | hybrid | restructured
            retail_choice    TEXT NOT NULL,    -- yes | limited | no
            rto              TEXT,             -- PJM | MISO | SPP | ERCOT | ISO-NE | NYISO | CAISO | WECC | TVA | SERC | None
            rto_full         TEXT,
            notes            TEXT,
            created_at       TEXT NOT NULL
        );
        """
    )
    conn.commit()

    # --- Migration TD-001: add utility discriminator column ---
    # Prevents silent data mix-ups when multiple utilities share a schedule code.
    for table in ("ncuc_ingest_segments", "rider_summary_blocks"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN utility TEXT")
            conn.commit()
        except Exception:
            pass  # column already exists

    # --- Migration TD-002: enforce uniqueness on rider_summary_blocks ---
    # DB-level guard against duplicate rows from repeated imports.
    # Clean up any pre-existing duplicates first (keep lowest id per key).
    conn.executescript(
        """
        DELETE FROM rider_summary_blocks WHERE id NOT IN (
            SELECT MIN(id) FROM rider_summary_blocks
            GROUP BY docket_dir, source_pdf, rate_class, effective_date
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rider_blocks_unique
        ON rider_summary_blocks(docket_dir, source_pdf, rate_class, effective_date);
        """
    )
    conn.commit()

    # --- Migration TD-OPT-001: add enrollment_type to rider_applicability ---
    # Distinguishes mandatory riders from opt-in, conditional, opt-out, and geographic.
    # Backfill existing rows to 'mandatory' (all existing rows are mandatory).
    try:
        conn.execute(
            "ALTER TABLE rider_applicability ADD COLUMN enrollment_type TEXT "
            "CHECK(enrollment_type IN ('mandatory','opt_in','opt_out','conditional','geographic')) "
            "DEFAULT 'mandatory'"
        )
        conn.execute("UPDATE rider_applicability SET enrollment_type = 'mandatory'")
        conn.commit()
    except Exception:
        pass  # column already exists

    # --- Migration TD-V4-005: add in_rider_summary to rider_applicability ---
    # TRUE (1) = rider appears in the leaf-600 "Summary of Rider Adjustments" consolidated line.
    # FALSE (0) = rider is added directly to the bill statement separately (e.g. STS, SSR storm riders).
    # Only in_rider_summary=TRUE riders are counted in the validate_rider_total() cross-check.
    try:
        conn.execute(
            "ALTER TABLE rider_applicability ADD COLUMN in_rider_summary INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()
    except Exception:
        pass  # column already exists

    # --- Migration AUDIT-001: completeness audit views ---
    # v_version_charge_summary: pre-computes charge_count and null_rate_count per tariff_version
    # so the audit service can detect empty/null versions without per-row Python loops.
    # v_rider_coverage_gaps: static view showing how many versions each rider/schedule pair has
    # and how many of those have charges vs. nulls — useful for bulk completeness scanning.
    conn.executescript(
        """
        CREATE VIEW IF NOT EXISTS v_version_charge_summary AS
        SELECT
            tv.id                AS version_id,
            tv.family_key,
            tv.effective_start,
            tv.effective_end,
            tv.revision_label,
            tv.supersedes_label,
            tv.source_type,
            tv.confidence_score,
            COUNT(tc.id)         AS charge_count,
            SUM(CASE WHEN tc.rate_value IS NULL THEN 1 ELSE 0 END) AS null_rate_count
        FROM tariff_versions tv
        LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
        GROUP BY tv.id;

        CREATE VIEW IF NOT EXISTS v_rider_coverage_gaps AS
        SELECT
            ra.applies_to_family_key  AS schedule_key,
            ra.rider_family_key       AS rider_key,
            tf.title                  AS rider_title,
            ra.in_rider_summary,
            ra.enrollment_type,
            COUNT(tv.id)              AS version_count,
            SUM(CASE WHEN vcs.charge_count > 0 THEN 1 ELSE 0 END)    AS versions_with_charges,
            SUM(CASE WHEN vcs.null_rate_count > 0 THEN 1 ELSE 0 END)  AS versions_with_nulls
        FROM rider_applicability ra
        LEFT JOIN tariff_families tf  ON tf.family_key = ra.rider_family_key
        LEFT JOIN tariff_versions tv  ON tv.family_key = ra.rider_family_key
        LEFT JOIN v_version_charge_summary vcs ON vcs.version_id = tv.id
        GROUP BY ra.rider_family_key, ra.applies_to_family_key;
        """
    )
    conn.commit()

    # --- Migration INTEL-001: decision-maker tracking and causal chain tables ---
    #
    # These tables support the "regulatory intelligence" layer:
    #   - Who appears in NCUC proceedings, in what role, across which dockets
    #   - What outcomes dockets produced (rate increases, riders, denials)
    #   - How tariff changes are causally linked to docket outcomes
    #   - Open-ended relationship storage (entity_relationships) for any
    #     pattern not yet anticipated
    #
    # Designed to be populated incrementally as new document types are ingested
    # (testimonies, orders, settlement agreements, public notices, exhibits).
    # Extraction methods: regex heuristics initially, NER model when available.
    conn.executescript(
        """
        -- -----------------------------------------------------------------------
        -- People who appear in NCUC proceedings: commissioners, witnesses,
        -- attorneys, public staff, utility staff, intervenors.
        -- One row per unique person; appearances are in docket_appearances.
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS decision_makers (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name           TEXT NOT NULL,
            normalized_name     TEXT NOT NULL,       -- lowercase, deduped key
            role_type           TEXT,                -- COMMISSIONER | WITNESS | ATTORNEY |
                                                     -- ALJ | PUBLIC_STAFF | UTILITY_STAFF |
                                                     -- INTERVENOR | UNKNOWN
            affiliation         TEXT,                -- "Duke Energy Progress", "Public Staff",
                                                     -- law firm name, company name
            title               TEXT,                -- "VP Regulatory Affairs", "Commissioner"
            first_seen_docket   TEXT,
            last_seen_docket    TEXT,
            appearance_count    INTEGER NOT NULL DEFAULT 1,
            notes               TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            UNIQUE(normalized_name, affiliation)     -- same person at same org = one row
        );
        CREATE INDEX IF NOT EXISTS idx_decision_makers_name
        ON decision_makers(normalized_name);
        CREATE INDEX IF NOT EXISTS idx_decision_makers_role
        ON decision_makers(role_type, affiliation);

        -- -----------------------------------------------------------------------
        -- Each time a person appears in a document: their role in that proceeding,
        -- the document that surfaces them, and the evidence snippet.
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS docket_appearances (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_maker_id   INTEGER NOT NULL REFERENCES decision_makers(id),
            source_pdf          TEXT NOT NULL,
            docket_dir          TEXT,
            appearance_role     TEXT,                -- "testifying_witness" | "signing_commissioner" |
                                                     -- "attorney_of_record" | "hearing_examiner" |
                                                     -- "public_staff_director" | "intervenor_rep"
            document_type       TEXT,                -- "testimony" | "order" | "settlement" |
                                                     -- "notice" | "exhibit" | "brief"
            page_number         INTEGER,
            evidence_text       TEXT,                -- sentence or phrase that surfaced this person
            extraction_method   TEXT NOT NULL DEFAULT 'regex',  -- "regex" | "ner_model" | "manual"
            confidence          REAL NOT NULL DEFAULT 0.5,
            created_at          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_docket_appearances_person
        ON docket_appearances(decision_maker_id, docket_dir);
        CREATE INDEX IF NOT EXISTS idx_docket_appearances_docket
        ON docket_appearances(docket_dir, appearance_role);
        CREATE INDEX IF NOT EXISTS idx_docket_appearances_pdf
        ON docket_appearances(source_pdf);

        -- -----------------------------------------------------------------------
        -- Commission orders and their outcomes: what was decided, when, and the
        -- quantified impact (rate change, revenue impact).
        -- One row per docket+order_date; a docket may have multiple orders.
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS docket_outcomes (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            docket_dir              TEXT NOT NULL,
            order_date              TEXT,
            outcome_type            TEXT,            -- "approved" | "denied" | "modified" |
                                                     -- "settled" | "withdrawn" | "pending" |
                                                     -- "interim_approved" | "remanded"
            rate_change_pct         REAL,            -- e.g. 0.12 for 12% increase; negative = decrease
            rate_change_dollars     REAL,            -- annual revenue impact in dollars
            effective_date          TEXT,            -- when approved rates take effect
            utility                 TEXT,            -- "DEP" | "DEC" | "Duke Power" etc.
            schedule_codes_json     TEXT NOT NULL DEFAULT '[]',  -- affected rate schedules
            order_pdf               TEXT,            -- path to the order document
            docket_summary          TEXT,            -- brief human-readable description
            source_confidence       REAL NOT NULL DEFAULT 0.5,
            extraction_method       TEXT NOT NULL DEFAULT 'regex',
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL,
            UNIQUE(docket_dir, order_date, outcome_type)
        );
        CREATE INDEX IF NOT EXISTS idx_docket_outcomes_docket
        ON docket_outcomes(docket_dir, order_date);
        CREATE INDEX IF NOT EXISTS idx_docket_outcomes_effective
        ON docket_outcomes(effective_date, utility);

        -- -----------------------------------------------------------------------
        -- Causal links: connect tariff versions to the docket outcomes that
        -- created or modified them. Also links rider filings to their originating
        -- docket and orders to the documents that triggered them.
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS tariff_causal_links (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            -- Source side: what caused the change (one of these will be non-null)
            docket_outcome_id   INTEGER REFERENCES docket_outcomes(id),
            source_docket_dir   TEXT,                -- denormalized for convenience
            source_pdf          TEXT,
            -- Target side: what was changed (one of these will be non-null)
            tariff_version_id   INTEGER,             -- FK to tariff_versions
            tariff_family_key   TEXT,                -- denormalized
            rider_family_key    TEXT,
            -- Link metadata
            link_type           TEXT NOT NULL,       -- "approved_by" | "superseded_by" |
                                                     -- "filed_under" | "modified_by" |
                                                     -- "triggered_by" | "settled_under"
            evidence_text       TEXT,                -- snippet supporting this link
            confidence          REAL NOT NULL DEFAULT 0.5,
            extraction_method   TEXT NOT NULL DEFAULT 'regex',
            created_at          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_causal_links_outcome
        ON tariff_causal_links(docket_outcome_id);
        CREATE INDEX IF NOT EXISTS idx_causal_links_version
        ON tariff_causal_links(tariff_version_id);
        CREATE INDEX IF NOT EXISTS idx_causal_links_docket
        ON tariff_causal_links(source_docket_dir);

        -- -----------------------------------------------------------------------
        -- Open-ended entity relationship store.
        -- Captures any relationship between two named entities that doesn't fit
        -- the structured tables above. Designed for discovery and future
        -- pattern analysis — don't force relationships into rigid tables
        -- before the pattern is understood.
        --
        -- Examples:
        --   (person:Jane Smith) -[testified_for]-> (docket:E-2 Sub-1234)
        --   (law_firm:Hunton Andrews) -[represented]-> (utility:DEP)
        --   (docket:E-2 Sub-1234) -[referenced]-> (docket:E-2 Sub-1100)
        --   (order:2019-03-15) -[approved]-> (rider:BA)
        --   (person:John Doe) -[voted_yes]-> (docket:E-2 Sub-1206)
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS entity_relationships (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            -- Subject (left side of relationship)
            subject_type        TEXT NOT NULL,       -- "person" | "docket" | "order" | "rider" |
                                                     -- "utility" | "law_firm" | "document" | "rate_schedule"
            subject_id          TEXT NOT NULL,       -- natural key (name, docket_dir, family_key etc.)
            subject_label       TEXT,                -- human-readable display name
            -- Predicate
            relationship        TEXT NOT NULL,       -- free-form verb: "testified_for", "voted_yes",
                                                     -- "represented", "referenced", "approved", "opposed"
            -- Object (right side)
            object_type         TEXT NOT NULL,
            object_id           TEXT NOT NULL,
            object_label        TEXT,
            -- Provenance
            source_pdf          TEXT,
            docket_dir          TEXT,
            page_number         INTEGER,
            evidence_text       TEXT,                -- supporting snippet
            extraction_method   TEXT NOT NULL DEFAULT 'regex',
            confidence          REAL NOT NULL DEFAULT 0.5,
            valid_from          TEXT,                -- date range this relationship was active
            valid_to            TEXT,
            created_at          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entity_rel_subject
        ON entity_relationships(subject_type, subject_id, relationship);
        CREATE INDEX IF NOT EXISTS idx_entity_rel_object
        ON entity_relationships(object_type, object_id, relationship);
        CREATE INDEX IF NOT EXISTS idx_entity_rel_docket
        ON entity_relationships(docket_dir);

        -- -----------------------------------------------------------------------
        -- Document type registry: tracks what kinds of documents have been
        -- ingested, their extraction maturity, and which pipeline stages apply.
        -- Lets the pipeline self-describe its coverage as new doc types are added.
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS document_type_registry (
            doc_type            TEXT PRIMARY KEY,    -- "tariff_sheet" | "commission_order" |
                                                     -- "testimony" | "exhibit" | "settlement" |
                                                     -- "public_notice" | "annual_report" | "brief"
            display_name        TEXT NOT NULL,
            description         TEXT,
            extraction_stages_json TEXT NOT NULL DEFAULT '[]',  -- pipeline stages for this type
            maturity_level      TEXT NOT NULL DEFAULT 'planned', -- "planned" | "prototype" | "production"
            record_count        INTEGER NOT NULL DEFAULT 0,
            first_ingested_at   TEXT,
            last_ingested_at    TEXT,
            notes               TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        );
        """
    )
    conn.commit()

    # --- Migration INTEL-002: influence mapping tables ---
    #
    # These tables extend the regulatory intelligence layer with structured
    # data for the three most important "outside-the-docket" influence patterns:
    #
    #   financial_relationships  — campaign contributions and financial ties
    #                              between entities (commissioners, legislators,
    #                              utility PACs, law firms).
    #   employment_history       — revolving-door tracking: movement between
    #                              regulated utility, regulator, and lobbying roles.
    #   legislative_actions      — NC General Assembly bills and amendments that
    #                              affect rate-setting authority, IRP requirements,
    #                              renewable mandates, or utility cost recovery.
    #
    # Sources: NC Board of Elections (NCSBE), FEC/OpenSecrets, SEC EDGAR proxy
    # statements, NC General Assembly bill text, Ballotpedia.
    #
    conn.executescript(
        """
        -- -----------------------------------------------------------------------
        -- Campaign finance and financial relationships.
        -- Tracks money flows: PAC contributions, honoraria, board memberships
        -- with compensation, law-firm retainers.
        -- subject → financial_relationship_type → object, with amount + cycle.
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS financial_relationships (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            -- Donor / payer side
            donor_type              TEXT NOT NULL,      -- "utility_pac" | "individual" | "law_firm" |
                                                        -- "trade_association" | "other"
            donor_name              TEXT NOT NULL,
            donor_normalized        TEXT NOT NULL,      -- lowercase dedup key
            -- Recipient side
            recipient_type          TEXT NOT NULL,      -- "commissioner" | "legislator" | "candidate" |
                                                        -- "political_party" | "super_pac" | "other"
            recipient_name          TEXT NOT NULL,
            recipient_normalized    TEXT NOT NULL,
            -- Transaction details
            relationship_type       TEXT NOT NULL,      -- "campaign_contribution" | "pac_contribution" |
                                                        -- "honorarium" | "board_compensation" |
                                                        -- "legal_retainer" | "lobbying_fee" | "other"
            amount_dollars          REAL,
            election_cycle          TEXT,               -- e.g. "2020", "2022" for campaign cycles
            transaction_date        TEXT,
            filing_id               TEXT,               -- NCSBE / FEC filing reference
            -- Provenance
            source_system           TEXT,               -- "ncsbe" | "fec" | "edgar" | "manual"
            source_url              TEXT,
            evidence_text           TEXT,
            confidence              REAL NOT NULL DEFAULT 0.8,  -- financial records are high confidence
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_financial_rel_donor
        ON financial_relationships(donor_normalized, relationship_type);
        CREATE INDEX IF NOT EXISTS idx_financial_rel_recipient
        ON financial_relationships(recipient_normalized, election_cycle);
        CREATE INDEX IF NOT EXISTS idx_financial_rel_cycle
        ON financial_relationships(election_cycle, relationship_type);

        -- -----------------------------------------------------------------------
        -- Employment history: revolving-door and career trajectory tracking.
        -- One row per role/period. Overlap between regulatory and utility roles
        -- is the primary signal of conflict-of-interest patterns.
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS employment_history (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            person_name             TEXT NOT NULL,
            person_normalized       TEXT NOT NULL,      -- lowercase dedup key; join to decision_makers
            decision_maker_id       INTEGER REFERENCES decision_makers(id),
            -- Role details
            employer_name           TEXT NOT NULL,
            employer_type           TEXT NOT NULL,      -- "utility" | "regulator" | "public_staff" |
                                                        -- "law_firm" | "legislature" | "lobbyist" |
                                                        -- "think_tank" | "trade_association" | "other"
            role_title              TEXT,               -- "Commissioner", "VP Regulatory Affairs", etc.
            role_category           TEXT,               -- "executive" | "regulatory_staff" | "attorney" |
                                                        -- "commissioner" | "lobbyist" | "legislator"
            -- Tenure
            start_date              TEXT,
            end_date                TEXT,               -- NULL = current
            is_current              INTEGER NOT NULL DEFAULT 0,
            -- Provenance
            source_system           TEXT,               -- "linkedin" | "ncuc_bio" | "annual_report" |
                                                        -- "testimony" | "manual"
            source_url              TEXT,
            evidence_text           TEXT,
            confidence              REAL NOT NULL DEFAULT 0.5,
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_employment_person
        ON employment_history(person_normalized, employer_type);
        CREATE INDEX IF NOT EXISTS idx_employment_employer
        ON employment_history(employer_name, start_date);
        CREATE INDEX IF NOT EXISTS idx_employment_dm
        ON employment_history(decision_maker_id);

        -- -----------------------------------------------------------------------
        -- NC General Assembly legislative actions that affect utility regulation.
        -- Tracks bills that modify NCUC authority, cost-recovery statutes,
        -- renewable portfolio mandates, or rate-case procedure.
        -- -----------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS legislative_actions (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            -- Bill identity
            bill_number             TEXT NOT NULL,      -- e.g. "SB 559", "HB 951"
            session_year            TEXT NOT NULL,      -- e.g. "2021-2022", "2023-2024"
            chamber                 TEXT,               -- "Senate" | "House" | "Conference"
            -- Content
            short_title             TEXT NOT NULL,
            summary                 TEXT,               -- plain-language description
            full_text_url           TEXT,
            -- Regulatory impact
            impact_category         TEXT,               -- "rate_setting_authority" | "cost_recovery" |
                                                        -- "renewable_mandate" | "irp_requirement" |
                                                        -- "rate_case_procedure" | "consumer_protection" |
                                                        -- "grid_modernization" | "other"
            utilities_affected      TEXT,               -- JSON array: ["DEP", "DEC", "Duke Power"]
            -- Legislative progress
            status                  TEXT,               -- "introduced" | "committee" | "passed_one_chamber" |
                                                        -- "enrolled" | "signed" | "vetoed" | "failed"
            introduced_date         TEXT,
            passed_date             TEXT,
            signed_date             TEXT,
            effective_date          TEXT,
            -- Key actors
            primary_sponsor         TEXT,               -- lead legislator name
            sponsor_party           TEXT,               -- "R" | "D" | "I"
            committee_assignments   TEXT,               -- JSON array of committee names
            -- Links to outcomes
            linked_docket_dirs      TEXT NOT NULL DEFAULT '[]',  -- JSON: dockets this bill affected
            -- Provenance
            source_url              TEXT,
            evidence_text           TEXT,
            confidence              REAL NOT NULL DEFAULT 0.8,
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL,
            UNIQUE(bill_number, session_year)
        );
        CREATE INDEX IF NOT EXISTS idx_legislative_session
        ON legislative_actions(session_year, status);
        CREATE INDEX IF NOT EXISTS idx_legislative_impact
        ON legislative_actions(impact_category, status);
        CREATE INDEX IF NOT EXISTS idx_legislative_sponsor
        ON legislative_actions(primary_sponsor, sponsor_party);
        """
    )
    conn.commit()

    # --- Migration DOCLING-001: store Docling content in DB, not on disk ---
    #
    # Adds three content columns to docling_artifacts so the full conversion
    # output lives in the database rather than in sidecar files on disk.
    # Sidecar file path columns are kept for backward compatibility but will
    # be populated as empty strings / NULL going forward.
    #
    for col, typedef in [
        ("doc_json_content", "TEXT"),        # full Docling export_to_dict() JSON
        ("plain_text_content", "TEXT"),      # export_to_markdown() plain text
        ("tables_json_content", "TEXT"),     # extracted table grid data JSON
        ("pipeline", "TEXT"),                # "standard" | "vlm"
    ]:
        try:
            conn.execute(f"ALTER TABLE docling_artifacts ADD COLUMN {col} {typedef}")
            conn.commit()
        except Exception:
            pass  # column already exists

    # --- Migration FPRINT-001: redline detection, document quality tiers, compliance book flag ---
    #
    # document_fingerprints gains:
    #   is_redline_candidate  — 1 if dual-rate pairs or "NEW/OLD/PROPOSED" markers detected in text
    #   redline_confidence    — 0.0–1.0 fraction of text lines with redline indicators
    #   doc_quality_tier      — "T1" (Duke website current), "T2" (NCUC compliance download),
    #                           "T3" (NCUC search engine / rate-case exhibit), or NULL
    #   is_compliance_book    — 1 if document spans multiple tariff leaves (TOC or ≥2 leaf_nos)
    #
    # ncuc_discovery_records gains:
    #   doc_quality_tier        — same tier enum applied to the discovery record
    #   search_confidence_score — 0.0–1.0 confidence from ResultScorer at discovery time
    #   search_ideality         — "ideal" | "probable" | "possible" | "skip" from IdealityAssessment
    #
    for col, typedef in [
        ("is_redline_candidate",  "INTEGER NOT NULL DEFAULT 0"),
        ("redline_confidence",    "REAL NOT NULL DEFAULT 0.0"),
        ("redline_detector_version", "TEXT"),
        ("redline_signals_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("red_text_samples_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("strikethrough_samples_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("red_is_index_only", "INTEGER NOT NULL DEFAULT 0"),
        ("doc_quality_tier",      "TEXT"),
        ("is_compliance_book",    "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE document_fingerprints ADD COLUMN {col} {typedef}")
            conn.commit()
        except Exception:
            pass  # column already exists

    for col, typedef in [
        ("doc_quality_tier",         "TEXT"),
        ("search_confidence_score",  "REAL"),
        ("search_ideality",          "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE ncuc_discovery_records ADD COLUMN {col} {typedef}")
            conn.commit()
        except Exception:
            pass  # column already exists

    # Backfill tier assignments from known acquisition/path patterns.
    # T1 = Duke website direct downloads (manual, named leaf-no-* / ncride* files).
    # T2 = NCUC compliance docket downloads (playwright or docket_scrape, specific sub-dockets).
    # T3 = NCUC search-engine or direct_http results (lower-confidence NCUC portal docs).
    # Uses REPLACE() to normalise Windows backslashes for LIKE matching.
    conn.executescript("""
        UPDATE ncuc_discovery_records SET doc_quality_tier = 'T1'
        WHERE doc_quality_tier IS NULL
          AND acquisition_method = 'manual_seed'
          AND (
            REPLACE(local_path, '\\', '/') LIKE '%historical/manual/leaf-no-%'
            OR REPLACE(local_path, '\\', '/') LIKE '%historical/manual/ncride%'
            OR REPLACE(local_path, '\\', '/') LIKE '%historical/manual/%ry%.pdf'
          );

        UPDATE ncuc_discovery_records SET doc_quality_tier = 'T2'
        WHERE doc_quality_tier IS NULL
          AND acquisition_method IN ('playwright', 'docket_scrape')
          AND (
            REPLACE(local_path, '\\', '/') LIKE '%downloads/ncuc_tariff/%'
            OR REPLACE(local_path, '\\', '/') LIKE '%historical/ncuc/e-%sub-%'
          );

        UPDATE ncuc_discovery_records SET doc_quality_tier = 'T3'
        WHERE doc_quality_tier IS NULL
          AND acquisition_method IN ('search_engine', 'direct_http')
          AND REPLACE(local_path, '\\', '/') LIKE '%historical/ncuc/%';
    """)
    conn.commit()

    # --- Migration: adding index optimizations for frontend visualizations ---
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_rider_blocks_utility_class_date
        ON rider_summary_blocks(utility, rate_class, effective_date);

        CREATE INDEX IF NOT EXISTS idx_dep_prov_components_eff_rider
        ON dep_provisional_rider_components(effective_date, rider_code);
        """
    )

    # --- Migration DIAG-001: v_document_diagnostics single-source-of-truth view ---
    # Joins historical_documents with the latest historical_processing_run, latest
    # ocr_artifact, latest docling_artifacts (per accelerator), and aggregated
    # ncuc_page_artifacts text. Computes route_reason and recommended_lane that
    # mirror _classify_ocr_route in cli.py — the CLI report stays authoritative
    # for filesystem-precise raw_text length, but this view is the SQL-side
    # routing signal for queries and downstream commands.
    #
    # Intentional caveat: SQL cannot read raw_text_path from disk. The view treats
    # "raw_text_path IS NOT NULL AND != ''" as has_usable_text=True without
    # measuring the file. The CLI report still does the filesystem max() check
    # for the precise number used in lane decisions.
    conn.executescript(
        """
        CREATE VIEW IF NOT EXISTS v_document_diagnostics AS
        WITH latest_runs AS (
            SELECT hpr.*
            FROM historical_processing_runs hpr
            JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                WHERE historical_document_id IS NOT NULL
                GROUP BY historical_document_id
            ) latest ON latest.max_id = hpr.id
        ),
        latest_ocr AS (
            SELECT oa.*
            FROM ocr_artifacts oa
            JOIN (
                SELECT source_pdf, file_hash, MAX(id) AS max_id
                FROM ocr_artifacts
                GROUP BY source_pdf, file_hash
            ) latest ON latest.max_id = oa.id
        ),
        latest_docling AS (
            SELECT
                da.source_pdf,
                da.file_hash,
                MAX(CASE WHEN da.accelerator = 'cuda' THEN da.status END) AS docling_cuda_status,
                MAX(CASE WHEN da.accelerator = 'cpu'  THEN da.status END) AS docling_cpu_status,
                MAX(da.backend_version) AS docling_backend_version,
                MAX(da.updated_at)      AS docling_last_updated_at
            FROM docling_artifacts da
            GROUP BY da.source_pdf, da.file_hash
        ),
        page_text AS (
            SELECT
                source_pdf,
                file_hash,
                SUM(text_length) AS page_artifact_text_chars,
                COUNT(*)         AS page_artifact_count
            FROM ncuc_page_artifacts
            GROUP BY source_pdf, file_hash
        )
        SELECT
            hd.id                                      AS historical_document_id,
            hd.family_key,
            hd.company,
            hd.state,
            hd.title,
            hd.local_path,
            hd.raw_text_path,
            hd.content_hash,
            hd.start_page,
            hd.end_page,
            hd.effective_start,
            -- Latest processing run
            lr.id                                      AS latest_run_id,
            lr.parser_profile                          AS latest_parser_profile,
            lr.parser_stage                            AS latest_parser_stage,
            lr.status                                  AS latest_run_status,
            lr.outcome_quality                         AS latest_outcome_quality,
            lr.charge_count                            AS latest_charge_count,
            lr.review_flags_json                       AS latest_review_flags_json,
            lr.metadata_json                           AS latest_metadata_json,
            lr.completed_at                            AS latest_run_completed_at,
            -- OCR
            lo.backend                                 AS ocr_backend,
            lo.status                                  AS ocr_status,
            lo.ocr_confidence                          AS ocr_confidence,
            lo.page_count                              AS ocr_page_count,
            -- Docling per-accelerator status
            ld.docling_cuda_status,
            ld.docling_cpu_status,
            ld.docling_backend_version,
            ld.docling_last_updated_at,
            -- Page-artifact aggregate
            COALESCE(pt.page_artifact_text_chars, 0)   AS page_artifact_text_chars,
            COALESCE(pt.page_artifact_count, 0)        AS page_artifact_count,
            -- Boolean signals
            CASE WHEN hd.raw_text_path IS NOT NULL AND hd.raw_text_path != ''
                 THEN 1 ELSE 0 END                     AS raw_text_path_set,
            CASE WHEN lo.id IS NOT NULL THEN 1 ELSE 0 END AS has_ocr_artifact,
            CASE WHEN ld.source_pdf IS NOT NULL THEN 1 ELSE 0 END AS has_docling_artifact,
            -- Effective page_count used by layout-heavy decision
            MAX(
                1,
                COALESCE(lo.page_count, 0),
                CASE
                    WHEN hd.start_page IS NOT NULL AND hd.end_page IS NOT NULL
                        THEN MAX(hd.end_page - hd.start_page + 1, 0)
                    ELSE 0
                END
            )                                          AS effective_page_count,
            -- Filesystem-free has_usable_text proxy
            CASE
                WHEN COALESCE(pt.page_artifact_text_chars, 0) > 0 THEN 1
                WHEN hd.raw_text_path IS NOT NULL AND hd.raw_text_path != '' THEN 1
                ELSE 0
            END                                        AS has_usable_text_proxy,
            -- Computed route_reason mirroring _classify_ocr_route
            CASE
                WHEN COALESCE(pt.page_artifact_text_chars, 0) = 0
                     AND (hd.raw_text_path IS NULL OR hd.raw_text_path = '')
                     AND (lr.parser_profile IS NULL OR lr.parser_profile = 'unknown')
                    THEN 'no_usable_text_unknown_profile'
                WHEN COALESCE(pt.page_artifact_text_chars, 0) = 0
                     AND (hd.raw_text_path IS NULL OR hd.raw_text_path = '')
                    THEN 'no_usable_text'
                WHEN lr.outcome_quality IN ('weak','empty') AND lo.id IS NULL
                    THEN 'weak_without_ocr'
                WHEN lr.outcome_quality IN ('weak','empty')
                     AND (
                        COALESCE(lo.page_count, 0) >= 5
                        OR (hd.start_page IS NOT NULL AND hd.end_page IS NOT NULL
                            AND (hd.end_page - hd.start_page + 1) >= 5)
                        OR LOWER(COALESCE(hd.title,'')) LIKE '%summary%'
                        OR LOWER(COALESCE(hd.title,'')) LIKE '%compliance%'
                        OR LOWER(COALESCE(hd.title,'')) LIKE '%book%'
                     )
                    THEN 'weak_layout_sensitive'
                WHEN lr.outcome_quality IN ('weak','empty')
                    THEN 'weak_after_text_recovery'
                ELSE 'healthy_or_non_ocr_issue'
            END                                        AS route_reason,
            -- Computed recommended_lane mirroring _classify_ocr_route
            CASE
                WHEN COALESCE(pt.page_artifact_text_chars, 0) = 0
                     AND (hd.raw_text_path IS NULL OR hd.raw_text_path = '')
                     AND (
                        COALESCE(lo.page_count, 0) >= 5
                        OR (hd.start_page IS NOT NULL AND hd.end_page IS NOT NULL
                            AND (hd.end_page - hd.start_page + 1) >= 5)
                        OR LOWER(COALESCE(hd.title,'')) LIKE '%summary%'
                        OR LOWER(COALESCE(hd.title,'')) LIKE '%compliance%'
                        OR LOWER(COALESCE(hd.title,'')) LIKE '%book%'
                     )
                    THEN 'run_docling_or_paddle_structure'
                WHEN COALESCE(pt.page_artifact_text_chars, 0) = 0
                     AND (hd.raw_text_path IS NULL OR hd.raw_text_path = '')
                    THEN 'queue_ocr_or_paddle'
                WHEN lr.outcome_quality IN ('weak','empty') AND lo.id IS NULL
                    THEN 'queue_ocr_or_paddle'
                WHEN lr.outcome_quality IN ('weak','empty')
                     AND (
                        COALESCE(lo.page_count, 0) >= 5
                        OR (hd.start_page IS NOT NULL AND hd.end_page IS NOT NULL
                            AND (hd.end_page - hd.start_page + 1) >= 5)
                        OR LOWER(COALESCE(hd.title,'')) LIKE '%summary%'
                        OR LOWER(COALESCE(hd.title,'')) LIKE '%compliance%'
                        OR LOWER(COALESCE(hd.title,'')) LIKE '%book%'
                     )
                    THEN 'run_docling_or_paddle_structure'
                WHEN lr.outcome_quality IN ('weak','empty')
                    THEN 'parser_or_page_level_glm_review'
                ELSE 'no_ocr_action'
            END                                        AS recommended_lane,
            -- Exclusion / review reason: explains why a doc is or is not actionable
            CASE
                WHEN hd.local_path IS NULL                     THEN 'no_local_path'
                WHEN hd.state != 'NC'                          THEN 'non_nc_state'
                WHEN lr.id IS NULL                             THEN 'never_processed'
                WHEN lr.status LIKE 'skipped_%'                THEN lr.status
                WHEN lr.outcome_quality = 'strong'             THEN 'healthy'
                ELSE NULL
            END                                        AS exclusion_reason
        FROM historical_documents hd
        LEFT JOIN latest_runs lr  ON lr.historical_document_id = hd.id
        LEFT JOIN latest_ocr  lo  ON lo.source_pdf = hd.local_path
                                  AND (lo.file_hash IS hd.content_hash OR lo.file_hash = hd.content_hash)
        LEFT JOIN latest_docling ld ON ld.source_pdf = hd.local_path
                                  AND (ld.file_hash IS hd.content_hash OR ld.file_hash = hd.content_hash)
        LEFT JOIN page_text   pt  ON pt.source_pdf = hd.local_path
                                  AND (pt.file_hash IS hd.content_hash OR pt.file_hash = hd.content_hash);
        """
    )

    # --- Migration OL-001: Ollama model run evidence store (Phase 2.5) ---
    #
    # Records every Ollama call made by the orchestrator. Stores the resolved
    # model, prompt version, status, timing, token counts, and the raw payload
    # (truncated). Used by:
    #   - check-ollama-models-nc for health telemetry
    #   - run-overnight-doc-intelligence-nc for resume/skip logic
    #   - run-llm-doc-probe-nc for manual smoke tests
    #   - Every classifier that uses the orchestrator (Phase 4/5/6)
    #
    # Indexed on (subject_kind, subject_id, stage) for resume queries and
    # (role, status, created_at) for overnight loop monitoring.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ollama_model_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_kind    TEXT NOT NULL,          -- 'pdf' | 'historical_document' | 'span' | 'page'
            subject_id      TEXT NOT NULL,          -- str FK, polymorphic
            stage           TEXT NOT NULL,          -- 'document_type' | 'flag_is_final' | 'rate_row_extraction' | ...
            role            TEXT NOT NULL,          -- 'balanced_classifier' | 'fast_classifier' | 'structured_extractor' | ...
            model           TEXT NOT NULL,          -- resolved model name actually called
            prompt_version  TEXT NOT NULL DEFAULT 'v1',
            status          TEXT NOT NULL,          -- 'ok' | 'http_error' | 'timeout' | 'json_parse_error' | 'validation_error' | 'fallback_used'
            duration_ms     INTEGER,
            tokens_in       INTEGER,
            tokens_out      INTEGER,
            raw_payload     TEXT,                   -- truncated to ~32 KB
            validation_error TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ollama_runs_subject
        ON ollama_model_runs(subject_kind, subject_id, stage);
        CREATE INDEX IF NOT EXISTS idx_ollama_runs_role_status
        ON ollama_model_runs(role, status, created_at);
        """
    )
    conn.commit()

    # --- Migration OL-002: Document embeddings store (Phase 4) ---
    #
    # Holds embedding vectors for each (source_pdf, embedding_kind, model) tuple.
    # Vectors are stored as float32 BLOBs via struct.pack.
    # embedding_kind classifies the text slice that was embedded:
    #   full_text | first_3_pages | title_block | rate_table_text | order_conclusion_section
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS document_embeddings (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf          TEXT NOT NULL,
            file_hash           TEXT NOT NULL,
            embedding_kind      TEXT NOT NULL,
            embedding_model     TEXT NOT NULL,
            embedding_version   TEXT NOT NULL DEFAULT 'v1',
            vector              BLOB NOT NULL,
            metadata_json       TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(source_pdf, file_hash, embedding_kind, embedding_model, embedding_version)
        );
        CREATE INDEX IF NOT EXISTS idx_doc_embeddings_source
        ON document_embeddings(source_pdf, file_hash);
        CREATE INDEX IF NOT EXISTS idx_doc_embeddings_kind_model
        ON document_embeddings(embedding_kind, embedding_model);
        """
    )
    conn.commit()

    # --- Migration OL-003: LLM-assisted parse diagnosis tables (Phase 5.6) ---
    #
    # Four additive tables that store parse failure diagnoses, regex/normalization
    # suggestions, deterministic validation results, and LLM candidate rate
    # extractions. All are purely additive — no existing tables are modified.
    # llm_candidate_rate_extractions stores ADVISORY rows only, never production
    # tariff_charges.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_parse_diagnostics (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            parse_attempt_id    INTEGER,
            subject_kind        TEXT NOT NULL DEFAULT 'parse_attempt',
            subject_id          TEXT NOT NULL,
            failure_type        TEXT NOT NULL,
            confidence          REAL NOT NULL DEFAULT 0.0,
            evidence_json       TEXT NOT NULL DEFAULT '[]',
            recommended_action  TEXT NOT NULL,
            model               TEXT NOT NULL,
            model_role          TEXT NOT NULL,
            prompt_version      TEXT NOT NULL DEFAULT 'v1',
            notes               TEXT,
            ollama_run_id       INTEGER,
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_parse_diag_attempt
        ON llm_parse_diagnostics(parse_attempt_id);
        CREATE INDEX IF NOT EXISTS idx_llm_parse_diag_subject
        ON llm_parse_diagnostics(subject_kind, subject_id);
        CREATE INDEX IF NOT EXISTS idx_llm_parse_diag_type
        ON llm_parse_diagnostics(failure_type, created_at);

        CREATE TABLE IF NOT EXISTS llm_regex_suggestions (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            diagnosis_id            INTEGER,
            suggestion_type         TEXT NOT NULL,
            target_profile          TEXT,
            target_field            TEXT,
            missed_text             TEXT,
            likely_issue            TEXT,
            candidate_regex         TEXT,
            candidate_normalization TEXT,
            expected_unit           TEXT,
            risk                    TEXT,
            positive_test_cases_json TEXT NOT NULL DEFAULT '[]',
            negative_test_cases_json TEXT NOT NULL DEFAULT '[]',
            confidence              REAL NOT NULL DEFAULT 0.0,
            model                   TEXT NOT NULL,
            model_role              TEXT NOT NULL,
            prompt_version          TEXT NOT NULL DEFAULT 'v1',
            ollama_run_id           INTEGER,
            status                  TEXT NOT NULL DEFAULT 'pending_review',
            created_at              TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_regex_sugg_status
        ON llm_regex_suggestions(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_llm_regex_sugg_profile
        ON llm_regex_suggestions(target_profile, created_at);

        CREATE TABLE IF NOT EXISTS llm_regex_validation_results (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            suggestion_id           INTEGER NOT NULL,
            status                  TEXT NOT NULL,
            before_matched_fields   INTEGER NOT NULL DEFAULT 0,
            before_charge_count     INTEGER NOT NULL DEFAULT 0,
            after_matched_fields    INTEGER NOT NULL DEFAULT 0,
            after_charge_count      INTEGER NOT NULL DEFAULT 0,
            regression_failures_json TEXT NOT NULL DEFAULT '[]',
            test_document_ids_json  TEXT NOT NULL DEFAULT '[]',
            notes                   TEXT,
            created_at              TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_regex_val_suggestion
        ON llm_regex_validation_results(suggestion_id);

        CREATE TABLE IF NOT EXISTS llm_candidate_rate_extractions (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            historical_document_id  INTEGER,
            source_pdf              TEXT NOT NULL,
            rate_rows_json          TEXT NOT NULL,
            document_signals_json   TEXT NOT NULL DEFAULT '{}',
            extraction_confidence   REAL NOT NULL DEFAULT 0.0,
            warnings_json           TEXT NOT NULL DEFAULT '[]',
            model                   TEXT NOT NULL,
            model_role              TEXT NOT NULL,
            prompt_version          TEXT NOT NULL DEFAULT 'v1',
            ollama_run_id           INTEGER,
            status                  TEXT NOT NULL DEFAULT 'candidate',
            created_at              TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_cand_extract_hd
        ON llm_candidate_rate_extractions(historical_document_id);
        CREATE INDEX IF NOT EXISTS idx_llm_cand_extract_status
        ON llm_candidate_rate_extractions(status, created_at);

        CREATE TABLE IF NOT EXISTS llm_candidate_rate_row_validations (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_id           INTEGER NOT NULL,
            row_index               INTEGER NOT NULL,
            historical_document_id  INTEGER,
            source_pdf              TEXT NOT NULL,
            charge_type             TEXT,
            value                   REAL,
            unit                    TEXT,
            inferred_unit           TEXT,
            inferred_unit_reason    TEXT,
            source_quote            TEXT,
            source_quote_grounded   INTEGER NOT NULL DEFAULT 0,
            value_grounded          INTEGER NOT NULL DEFAULT 0,
            unit_grounded           INTEGER NOT NULL DEFAULT 0,
            validation_score        REAL NOT NULL DEFAULT 0.0,
            recommended_status      TEXT NOT NULL,
            issues_json             TEXT NOT NULL DEFAULT '[]',
            validated_at            TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(extraction_id, row_index)
        );
        CREATE INDEX IF NOT EXISTS idx_llm_row_val_status
        ON llm_candidate_rate_row_validations(recommended_status, validated_at);
        CREATE INDEX IF NOT EXISTS idx_llm_row_val_hd
        ON llm_candidate_rate_row_validations(historical_document_id);

        CREATE TABLE IF NOT EXISTS llm_candidate_rate_row_repairs (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            validation_id               INTEGER NOT NULL,
            extraction_id               INTEGER NOT NULL,
            row_index                   INTEGER NOT NULL,
            repair_type                 TEXT NOT NULL,
            original_charge_type        TEXT,
            proposed_charge_type        TEXT,
            original_unit               TEXT,
            proposed_unit               TEXT,
            evidence_quote              TEXT,
            confidence                  REAL NOT NULL DEFAULT 0.0,
            reason                      TEXT,
            validation_status           TEXT NOT NULL,
            validation_issues_json      TEXT NOT NULL DEFAULT '[]',
            model                       TEXT,
            model_role                  TEXT,
            status                      TEXT NOT NULL DEFAULT 'pending',
            created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_row_repairs_validation
        ON llm_candidate_rate_row_repairs(validation_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_llm_row_repairs_status
        ON llm_candidate_rate_row_repairs(status, validation_status, created_at);

        CREATE TABLE IF NOT EXISTS llm_rate_charge_promotion_proposals (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            validation_id               INTEGER NOT NULL,
            extraction_id               INTEGER NOT NULL,
            row_index                   INTEGER NOT NULL,
            repair_id                   INTEGER,
            historical_document_id      INTEGER,
            version_id                  INTEGER,
            family_key                  TEXT,
            charge_type                 TEXT NOT NULL,
            charge_label                TEXT,
            rate_value                  REAL,
            rate_unit                   TEXT,
            tou_period                  TEXT,
            season                      TEXT,
            customer_class              TEXT,
            source_quote                TEXT,
            evidence_quote              TEXT,
            effective_status            TEXT NOT NULL,
            eligibility_status          TEXT NOT NULL,
            eligibility_issues_json     TEXT NOT NULL DEFAULT '[]',
            duplicate_status            TEXT NOT NULL,
            conflict_status             TEXT NOT NULL,
            promotion_status            TEXT NOT NULL DEFAULT 'pending',
            tariff_charge_id            INTEGER,
            created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
            promoted_at                 TEXT,
            UNIQUE(validation_id, repair_id)
        );
        CREATE INDEX IF NOT EXISTS idx_llm_charge_prop_status
        ON llm_rate_charge_promotion_proposals(
            promotion_status, eligibility_status, duplicate_status, conflict_status
        );
        CREATE INDEX IF NOT EXISTS idx_llm_charge_prop_version
        ON llm_rate_charge_promotion_proposals(version_id);

        CREATE TABLE IF NOT EXISTS llm_promoted_charge_audit (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id          INTEGER NOT NULL,
            tariff_charge_id     INTEGER NOT NULL,
            validation_id        INTEGER NOT NULL,
            repair_id            INTEGER,
            extraction_id        INTEGER NOT NULL,
            row_index            INTEGER NOT NULL,
            source_quote         TEXT,
            evidence_quote       TEXT,
            promoted_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_promoted_charge_audit_charge
        ON llm_promoted_charge_audit(tariff_charge_id);
        """
    )
    conn.commit()

    # --- Migration DB_INTEL-001: Database intelligence run log (Phase 6.5) ---
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS database_intelligence_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_type        TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'started',
            question        TEXT,
            generated_sql   TEXT,
            safety_check    TEXT,
            execution_status TEXT,
            row_count       INTEGER,
            report_sections_json TEXT,
            summary_json    TEXT,
            error_message   TEXT,
            duration_ms     INTEGER,
            config_json     TEXT NOT NULL DEFAULT '{}',
            output_path     TEXT,
            ollama_run_id   INTEGER,
            requested_by    TEXT NOT NULL DEFAULT 'system',
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_db_intel_runs_type
        ON database_intelligence_runs(run_type, created_at);
        CREATE INDEX IF NOT EXISTS idx_db_intel_runs_status
        ON database_intelligence_runs(status, created_at);
        """
    )
    conn.commit()

    _seed_document_types(conn)


_DOCUMENT_TYPE_SEEDS: tuple[tuple[str, str, str], ...] = (
    # (code, primary_category, description)
    ("TARIFF_SHEET",            "TARIFF_AND_RATE_DOCUMENTS",     "A revised or original tariff leaf sheet — schedule pages, leaf-numbered rate text."),
    ("RIDER",                   "TARIFF_AND_RATE_DOCUMENTS",     "A rate rider attached to a base schedule (e.g. fuel, DSM/EE, storm)."),
    ("RATE_SCHEDULE",           "TARIFF_AND_RATE_DOCUMENTS",     "A full rate schedule document covering one or more leaves of a single schedule."),
    ("ORDER_FINAL",             "ORDERS_AND_DECISIONS",          "A final commission order approving, denying, or modifying a filing."),
    ("ORDER_PROCEDURAL",        "ORDERS_AND_DECISIONS",          "A procedural order — scheduling, motion rulings, intervention grants."),
    ("TESTIMONY",               "TESTIMONY_AND_EXHIBITS",        "Direct, rebuttal, or supplemental testimony filed in a docket."),
    ("COVER_LETTER",            "PROCEDURAL_AND_ADMINISTRATIVE", "Filing cover letters, transmittal letters, electronic filing notes."),
    ("CERTIFICATE_OF_SERVICE",  "PROCEDURAL_AND_ADMINISTRATIVE", "A certificate of service or proof of distribution."),
    ("NOTICE_OF_HEARING",       "PROCEDURAL_AND_ADMINISTRATIVE", "Hearing notices, public notices, scheduling notices."),
    ("APPLICATION",             "APPLICATIONS_AND_PETITIONS",    "An application or petition initiating a docket or seeking relief."),
    ("COMPLIANCE_FILING",       "REPORTS_AND_COMPLIANCE",        "A compliance filing made pursuant to a prior order."),
    ("UNKNOWN",                 "NOISE_OR_DUPLICATES",           "Sentinel — used when no terminal type matches with sufficient confidence."),
)


def _seed_document_types(conn) -> None:
    """Insert any missing document_types rows. Idempotent (UNIQUE on code).

    Safe to call on every migrate(). Existing rows are never modified — if
    a description needs updating, do it via an explicit migration so the
    change is reviewable.
    """
    from datetime import UTC, datetime
    now = datetime.now(UTC).isoformat()
    for code, category, description in _DOCUMENT_TYPE_SEEDS:
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO document_types
                    (code, primary_category, parent_type, description, is_terminal, created_at)
                VALUES (?, ?, NULL, ?, 1, ?)
                """,
                (code, category, description, now),
            )
        except Exception:
            pass
    conn.commit()
