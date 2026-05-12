from __future__ import annotations

import sqlite3

from duke_rates.document_intelligence.llm_row_evidence_locator import (
    LLMRowEvidenceLocator,
    RowEvidenceProposal,
    RowReclassificationProposal,
    _context_around_quote,
    _evidence_clues,
)


class _RunResult:
    def __init__(self, result, *, status="ok", model="fake-model"):
        self.status = status
        self.result = result
        self.model = model


class _FakeOrchestrator:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return _RunResult(self.proposal)


def _init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE llm_candidate_rate_row_validations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_id INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            historical_document_id INTEGER,
            source_pdf TEXT NOT NULL,
            charge_type TEXT,
            value REAL,
            unit TEXT,
            inferred_unit TEXT,
            inferred_unit_reason TEXT,
            source_quote TEXT,
            source_quote_grounded INTEGER NOT NULL DEFAULT 0,
            value_grounded INTEGER NOT NULL DEFAULT 0,
            unit_grounded INTEGER NOT NULL DEFAULT 0,
            validation_score REAL NOT NULL DEFAULT 0.0,
            recommended_status TEXT NOT NULL,
            issues_json TEXT NOT NULL DEFAULT '[]',
            validated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(extraction_id, row_index)
        );
        CREATE TABLE ncuc_page_artifacts (
            source_pdf TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            text_content TEXT
        );
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            raw_text_path TEXT
        );
        """
    )
    return conn


def test_row_evidence_locator_accepts_grounded_unit_repair(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "Lamp Rating Per Month Per Luminaire\nLED 50 light emitting diode 2.42",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_row_validations (
            extraction_id, row_index, historical_document_id, source_pdf,
            charge_type, value, unit, source_quote, source_quote_grounded,
            value_grounded, unit_grounded, validation_score, recommended_status,
            issues_json
        )
        VALUES (10, 3, 1, 'doc.pdf', 'Lighting Charge', 2.42, '$',
                'LED 50 light emitting diode 2.42', 1, 1, 0, 0.7,
                'review_candidate', '["unit_not_grounded"]')
        """
    )
    conn.commit()
    conn.close()

    orch = _FakeOrchestrator(
        RowEvidenceProposal(
            supported_unit="$/month",
            evidence_quote="Lamp Rating Per Month Per Luminaire",
            reason="Table header proves monthly luminaire unit.",
            confidence=0.91,
        )
    )
    locator = LLMRowEvidenceLocator(orch, db_path)

    report = locator.locate(issue="unit_not_grounded", limit=5, execute=True)

    assert report["summary"]["accepted"] == 1
    assert report["rows"][0]["validation_status"] == "accepted"
    conn = sqlite3.connect(db_path)
    repair = conn.execute(
        """
        SELECT proposed_unit, validation_status, status
        FROM llm_candidate_rate_row_repairs
        """
    ).fetchone()
    conn.close()
    assert repair == ("$/month", "accepted", "accepted")


def test_row_evidence_locator_rejects_ungrounded_evidence(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts VALUES (?, ?, ?)",
        ("doc.pdf", 1, "LED 50 light emitting diode 2.42"),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_row_validations (
            extraction_id, row_index, historical_document_id, source_pdf,
            charge_type, value, unit, source_quote, source_quote_grounded,
            value_grounded, unit_grounded, validation_score, recommended_status,
            issues_json
        )
        VALUES (10, 3, 1, 'doc.pdf', 'Lighting Charge', 2.42, '$',
                'LED 50 light emitting diode 2.42', 1, 1, 0, 0.7,
                'review_candidate', '["unit_not_grounded"]')
        """
    )
    conn.commit()
    conn.close()

    orch = _FakeOrchestrator(
        RowEvidenceProposal(
            supported_unit="$/month",
            evidence_quote="Lamp Rating Per Month Per Luminaire",
            reason="Table header proves monthly luminaire unit.",
            confidence=0.91,
        )
    )
    locator = LLMRowEvidenceLocator(orch, db_path)

    report = locator.locate(issue="unit_not_grounded", limit=5, execute=True)

    assert report["summary"]["rejected"] == 1
    assert "evidence_quote_not_grounded" in report["rows"][0]["validation_issues"]


def test_context_expansion_surfaces_monthly_table_clues():
    source_text = "\n".join(
        [
            "SERVICE",
            "Some intro.",
            "MONTHLY RATE",
            "The following amount will be added to each monthly bill:",
            "Monthly Charge",
            "Per Customer",
            "1 light per 5 customers or major fraction thereof:",
            "LED 50 light emitting diode 2.42",
        ]
    )

    context = _context_around_quote(
        "LED 50 light emitting diode 2.42",
        source_text,
        window=20,
    )
    clues = _evidence_clues(context)

    assert "MONTHLY RATE" in context
    assert "Monthly Charge" in clues
    assert "Per Customer" in clues


def test_row_evidence_locator_prompt_includes_deterministic_clues(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "MONTHLY RATE\nMonthly Charge\nPer Customer\nLED 50 light emitting diode 2.42",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_row_validations (
            extraction_id, row_index, historical_document_id, source_pdf,
            charge_type, value, unit, source_quote, source_quote_grounded,
            value_grounded, unit_grounded, validation_score, recommended_status,
            issues_json
        )
        VALUES (10, 3, 1, 'doc.pdf', 'Lighting Charge', 2.42, '$',
                'LED 50 light emitting diode 2.42', 1, 1, 0, 0.7,
                'review_candidate', '["unit_not_grounded"]')
        """
    )
    conn.commit()
    conn.close()

    orch = _FakeOrchestrator(
        RowEvidenceProposal(
            supported_unit="$/month",
            evidence_quote="Monthly Charge",
            reason="The table header indicates a monthly charge.",
            confidence=0.9,
        )
    )
    locator = LLMRowEvidenceLocator(orch, db_path)

    locator.locate(issue="unit_not_grounded", limit=1)

    prompt = orch.calls[0]["prompt"]
    assert "Candidate evidence clues" in prompt
    assert "bare number" in prompt
    assert "Monthly Charge" in prompt
    assert "Per Customer" in prompt


def test_row_reclassification_accepts_grounded_charge_type_and_unit_repair(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts VALUES (?, ?, ?)",
        (
            "doc.pdf",
            1,
            "Lamp Rating Per Month Per Luminaire\n7,500 75 Urban (4) $10.60 NA NA",
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_row_validations (
            extraction_id, row_index, historical_document_id, source_pdf,
            charge_type, value, unit, inferred_unit, inferred_unit_reason,
            source_quote, source_quote_grounded, value_grounded, unit_grounded,
            validation_score, recommended_status, issues_json
        )
        VALUES (20, 13, 1, 'doc.pdf', 'Energy Charge', 10.60, '$/kWh',
                '$/month', 'lighting_table_per_month_per_luminaire',
                '7,500 75 Urban (4) $10.60 NA NA', 1, 1, 0, 0.9,
                'review_candidate', '["unit_not_grounded","unit_conflicts_with_inferred"]')
        """
    )
    conn.commit()
    conn.close()

    orch = _FakeOrchestrator(
        RowReclassificationProposal(
            proposed_charge_type="Lighting Charge",
            proposed_unit="$/month",
            evidence_quote="Lamp Rating Per Month Per Luminaire",
            reason="The row is in a lighting table with monthly luminaire charges.",
            confidence=0.92,
        )
    )
    locator = LLMRowEvidenceLocator(orch, db_path)

    report = locator.reclassify_conflicts(limit=5, execute=True)

    assert report["summary"]["accepted"] == 1
    assert report["rows"][0]["proposed_charge_type"] == "Lighting Charge"
    conn = sqlite3.connect(db_path)
    repair = conn.execute(
        """
        SELECT repair_type, original_charge_type, proposed_charge_type,
               original_unit, proposed_unit, validation_status, status
        FROM llm_candidate_rate_row_repairs
        """
    ).fetchone()
    conn.close()
    assert repair == (
        "row_reclassification",
        "Energy Charge",
        "Lighting Charge",
        "$/kWh",
        "$/month",
        "accepted",
        "accepted",
    )


def test_row_reclassification_rejects_noop_repair(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        "INSERT INTO ncuc_page_artifacts VALUES (?, ?, ?)",
        ("doc.pdf", 1, "Lamp Rating Per Month Per Luminaire\n7,500 75 Urban (4) $10.60 NA NA"),
    )
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_row_validations (
            extraction_id, row_index, historical_document_id, source_pdf,
            charge_type, value, unit, inferred_unit, inferred_unit_reason,
            source_quote, source_quote_grounded, value_grounded, unit_grounded,
            validation_score, recommended_status, issues_json
        )
        VALUES (20, 13, 1, 'doc.pdf', 'Lighting Charge', 10.60, '$/month',
                '$/month', 'lighting_table_per_month_per_luminaire',
                '7,500 75 Urban (4) $10.60 NA NA', 1, 1, 0, 0.9,
                'review_candidate', '["unit_conflicts_with_inferred"]')
        """
    )
    conn.commit()
    conn.close()

    orch = _FakeOrchestrator(
        RowReclassificationProposal(
            proposed_charge_type="Lighting Charge",
            proposed_unit="$/month",
            evidence_quote="Lamp Rating Per Month Per Luminaire",
            reason="No change.",
            confidence=0.92,
        )
    )
    locator = LLMRowEvidenceLocator(orch, db_path)

    report = locator.reclassify_conflicts(limit=5, execute=False)

    assert report["summary"]["rejected"] == 1
    assert "no_reclassification_change" in report["rows"][0]["validation_issues"]


def test_apply_deterministic_repairs_creates_lighting_table_repair(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute("INSERT INTO historical_documents (id, raw_text_path) VALUES (1, '')")
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_row_validations (
            extraction_id, row_index, historical_document_id, source_pdf,
            charge_type, value, unit, inferred_unit, inferred_unit_reason,
            source_quote, source_quote_grounded, value_grounded, unit_grounded,
            validation_score, recommended_status, issues_json
        )
        VALUES (30, 9, 1, 'doc.pdf', 'Lighting Charge', 23.12, '$/kWh',
                '$/month', 'lighting_table_per_month_per_luminaire',
                '40,000 155 Urban $23.12 NA NA', 1, 1, 0, 0.9,
                'review_candidate', '["unit_not_grounded","unit_conflicts_with_inferred"]')
        """
    )
    conn.commit()
    conn.close()

    locator = LLMRowEvidenceLocator(None, db_path)
    report = locator.apply_deterministic_repairs(limit=10, execute=True)

    assert report["summary"]["accepted"] == 1
    conn = sqlite3.connect(db_path)
    repair = conn.execute(
        """
        SELECT repair_type, original_charge_type, proposed_charge_type,
               original_unit, proposed_unit, validation_status, model
        FROM llm_candidate_rate_row_repairs
        """
    ).fetchone()
    conn.close()
    assert repair == (
        "deterministic_lighting_table_repair",
        "Lighting Charge",
        "Lighting Charge",
        "$/kWh",
        "$/month",
        "accepted",
        "deterministic",
    )


def test_effective_status_report_counts_accepted_repairs(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = _init_db(db_path)
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_row_validations (
            id, extraction_id, row_index, source_pdf, recommended_status,
            validation_score, issues_json
        )
        VALUES
            (1, 10, 0, 'a.pdf', 'validated', 1.0, '[]'),
            (2, 10, 1, 'a.pdf', 'rejected', 0.2, '[]'),
            (3, 10, 2, 'a.pdf', 'review_candidate', 0.7, '["unit_not_grounded"]'),
            (4, 10, 3, 'a.pdf', 'review_candidate', 0.7, '["unit_not_grounded"]')
        """
    )
    conn.commit()
    conn.close()

    locator = LLMRowEvidenceLocator(None, db_path)
    locator.apply_deterministic_repairs(limit=10, execute=False)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO llm_candidate_rate_row_repairs (
            validation_id, extraction_id, row_index, repair_type,
            proposed_unit, validation_status, status
        )
        VALUES (3, 10, 2, 'unit_evidence', '$/month', 'accepted', 'accepted')
        """
    )
    conn.commit()
    conn.close()

    report = locator.effective_status_report()

    assert report["effective_status_counts"] == {
        "rejected": 1,
        "review_candidate": 1,
        "validated": 1,
        "validated_with_repair": 1,
    }
    assert report["unresolved_review_rows"] == 1
