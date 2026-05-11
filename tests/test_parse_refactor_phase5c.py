from pathlib import Path

from duke_rates.document_intelligence.document_identity import ensure_schema as ensure_identity_schema
from duke_rates.document_intelligence.document_specific_rules import ensure_schema as ensure_rule_schema
from duke_rates.document_intelligence.parse_improvement_loop import VALID_TASK_KINDS
from duke_rates.document_intelligence.parse_improvement_loop import ParseImprovementLoop
from duke_rates.document_intelligence.per_doc_rule_generator import PerDocRuleGenerator
from duke_rates.document_intelligence.regex_suggestions import RegexSuggestion
from duke_rates.document_intelligence.rule_promotion import PromotionDetector


class _DummyOrchestrator:
    pass


def test_phase4_tasks_are_valid_overnight_task_kinds() -> None:
    assert {
        "populate_identity",
        "populate_routing_tier",
        "bind_tier1",
        "generate_per_doc_rules",
        "detect_rule_promotions",
    }.issubset(VALID_TASK_KINDS)


def test_deterministic_refresh_tasks_do_not_count_as_documents_analyzed() -> None:
    assert ParseImprovementLoop._document_count_for_task(
        "populate_identity",
        {"ok": 5, "fail": 0},
    ) == 0
    # ``candidates`` is queue size, NOT processed count. The processed count
    # for an LLM task is ok + fail + skip from actual outcomes.
    assert ParseImprovementLoop._document_count_for_task(
        "generate_per_doc_rules",
        {"ok": 1, "fail": 4, "skip": 0, "candidates": 5},
    ) == 5
    # Candidates with no outcomes yet (e.g. stage skipped) must count as 0.
    assert ParseImprovementLoop._document_count_for_task(
        "generate_per_doc_rules",
        {"ok": 0, "fail": 0, "skip": 0, "candidates": 50},
    ) == 0


def test_per_doc_validation_accepts_cents_per_kwh_with_normalization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    generator = PerDocRuleGenerator(_DummyOrchestrator(), tmp_path / "rules.db")

    monkeypatch.setattr(
        generator,
        "_get_document_text",
        lambda source_pdf, **kwargs: (
            "Schedule RES-48\n"
            "Residential Service\n"
            "Summer Energy Charge 10.369¢ per kWh\n"
        ),
    )
    monkeypatch.setattr(generator, "_select_siblings", lambda candidate, *, limit: [])

    suggestion = RegexSuggestion(
        target_field="energy_charge",
        candidate_regex=r"Schedule\s+RES-48[\s\S]*?(\d+\.\d+)¢\s+per\s+kWh",
        candidate_normalization="divide captured cents per kWh by 100",
        expected_unit="¢/kWh",
        confidence=0.8,
    )

    result = generator.validate(
        suggestion,
        {
            "source_pdf": "doc.pdf",
            "document_identity_id": 1,
            "schedule_codes_strong_json": '["RES-48"]',
        },
    )

    assert result.accept is True
    assert result.target_matches == 1


def test_per_doc_validation_uses_last_numeric_capture_for_scoped_patterns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    generator = PerDocRuleGenerator(_DummyOrchestrator(), tmp_path / "rules.db")

    monkeypatch.setattr(
        generator,
        "_get_document_text",
        lambda source_pdf, **kwargs: "Schedule RES-48 Energy Charge 10.369¢ per kWh",
    )
    monkeypatch.setattr(generator, "_select_siblings", lambda candidate, *, limit: [])

    suggestion = RegexSuggestion(
        target_field="energy_charge",
        candidate_regex=r"(RES-48)[\s\S]*?(\d+\.\d+)¢\s+per\s+kWh",
        candidate_normalization="divide captured cents per kWh by 100",
        expected_unit="¢/kWh",
        confidence=0.8,
    )

    result = generator.validate(
        suggestion,
        {
            "source_pdf": "doc.pdf",
            "document_identity_id": 1,
            "schedule_codes_strong_json": '["RES-48"]',
        },
    )

    assert result.accept is True


def test_per_doc_validation_rejects_unanchored_regex_when_identity_has_anchor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    generator = PerDocRuleGenerator(_DummyOrchestrator(), tmp_path / "rules.db")

    monkeypatch.setattr(
        generator,
        "_get_document_text",
        lambda source_pdf, **kwargs: "Schedule RES-48 Energy Charge 10.369¢ per kWh",
    )
    monkeypatch.setattr(generator, "_select_siblings", lambda candidate, *, limit: [])

    suggestion = RegexSuggestion(
        target_field="energy_charge",
        candidate_regex=r"(\d+\.\d+)¢\s+per\s+kWh",
        candidate_normalization="divide captured cents per kWh by 100",
        expected_unit="¢/kWh",
        confidence=0.8,
    )

    result = generator.validate(
        suggestion,
        {
            "source_pdf": "doc.pdf",
            "document_identity_id": 1,
            "schedule_codes_strong_json": '["RES-48"]',
        },
    )

    assert result.accept is False
    assert "lacks a document-specific" in result.reason


def test_deterministic_template_short_circuits_llm_when_signals_unambiguous(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Single strong anchor + 3 matching rate values → no LLM call needed."""
    from duke_rates.document_intelligence.document_specific_rules import ensure_schema

    db_path = tmp_path / "rules.db"
    ensure_schema(db_path)
    generator = PerDocRuleGenerator(_DummyOrchestrator(), db_path)

    monkeypatch.setattr(
        generator,
        "_get_document_text",
        lambda source_pdf, **kwargs: (
            "Schedule RES-48 Residential Service\n"
            "Summer Energy Charge: 10.369 cents per kWh\n"
            "Winter Energy Charge: 9.482 cents per kWh\n"
            "Off-peak Energy Charge: 4.215 cents per kWh\n"
        ),
    )
    monkeypatch.setattr(generator, "_select_siblings", lambda candidate, *, limit: [])

    candidate = {
        "source_pdf": "doc.pdf",
        "document_identity_id": 1,
        "schedule_codes_strong_json": '["RES-48"]',
        "rider_codes_strong_json": "[]",
        "detected_titles_json": "[]",
        "filename_signals_json": "[]",
        "overall_confidence": 0.85,
    }

    suggestion = generator._try_deterministic_template(candidate)
    assert suggestion is not None
    assert "RES\\-48" in suggestion.candidate_regex
    assert suggestion.expected_unit == "¢/kWh"
    assert "divide captured cents per kWh by 100" in suggestion.candidate_normalization


def test_deterministic_template_skips_when_two_anchors_present(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Two strong anchors → ambiguous, fall back to LLM."""
    generator = PerDocRuleGenerator(_DummyOrchestrator(), tmp_path / "rules.db")
    monkeypatch.setattr(
        generator,
        "_get_document_text",
        lambda source_pdf, **kwargs: (
            "Schedule RES-48 ... 10.369 cents per kWh ... 9.482 cents per kWh "
            "... 4.215 cents per kWh"
        ),
    )

    candidate = {
        "source_pdf": "doc.pdf",
        "document_identity_id": 1,
        "schedule_codes_strong_json": '["RES-48", "RES-49"]',
        "rider_codes_strong_json": "[]",
    }

    assert generator._try_deterministic_template(candidate) is None


def test_deterministic_template_skips_when_too_few_rate_hits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Fewer than 3 same-shape rate values → don't trust the template."""
    generator = PerDocRuleGenerator(_DummyOrchestrator(), tmp_path / "rules.db")
    monkeypatch.setattr(
        generator,
        "_get_document_text",
        lambda source_pdf, **kwargs: (
            "Schedule RES-48: Energy Charge 10.369 cents per kWh"  # only 1 hit
        ),
    )

    candidate = {
        "source_pdf": "doc.pdf",
        "document_identity_id": 1,
        "schedule_codes_strong_json": '["RES-48"]',
        "rider_codes_strong_json": "[]",
    }

    assert generator._try_deterministic_template(candidate) is None


def test_past_mistakes_block_surfaces_recent_rejections(
    tmp_path: Path,
) -> None:
    """When 2+ rejected rules exist for the same anchor, they're listed in prompts."""
    import sqlite3
    from duke_rates.document_intelligence.document_specific_rules import ensure_schema

    db_path = tmp_path / "rules.db"
    ensure_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        for i, regex_str in enumerate([
            r"Schedule\s+RES-48\s+Energy\s+Charge:\s+(\d+\.\d+)",
            r"RES-48[\s\S]*?\$(\d+\.\d+)\s*per\s+kWh",
        ]):
            conn.execute(
                """INSERT INTO document_specific_rules
                   (document_identity_id, candidate_regex, status, notes)
                   VALUES (?, ?, 'rejected', ?)""",
                (i + 1, regex_str, f"per-doc rule generated; validation: zero matches"),
            )
        conn.commit()
    finally:
        conn.close()

    generator = PerDocRuleGenerator(_DummyOrchestrator(), db_path)
    block = generator._render_past_mistakes("RES-48", target_field=None)

    assert "PAST MISTAKES" in block
    assert "RES-48" in block
    assert block.count("regex:") == 2


def test_past_mistakes_block_silent_with_fewer_than_two_failures(
    tmp_path: Path,
) -> None:
    """One failure isn't enough signal to warn the LLM."""
    import sqlite3
    from duke_rates.document_intelligence.document_specific_rules import ensure_schema

    db_path = tmp_path / "rules.db"
    ensure_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """INSERT INTO document_specific_rules
               (document_identity_id, candidate_regex, status, notes)
               VALUES (1, 'Schedule RES-99 bad', 'rejected',
                       'per-doc rule generated; validation: zero matches')""",
        )
        conn.commit()
    finally:
        conn.close()

    generator = PerDocRuleGenerator(_DummyOrchestrator(), db_path)
    block = generator._render_past_mistakes("RES-99", target_field=None)

    assert block == ""


def test_staged_extractor_filters_brochure_docs_at_stage_1(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Stage 1 skips docs with too few $/¢ tokens — no LLM call needed."""
    from duke_rates.document_intelligence.schema_extraction import (
        SchemaGuidedExtractor, CandidateRateExtraction,
    )
    import sqlite3
    from duke_rates.db.schema import SCHEMA_SQL, migrate

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()

    class _Orch:
        called = False
        def generate_json(self, **kwargs):
            type(self).called = True
            raise AssertionError("Stage 1 should have filtered before LLM call")

    extractor = SchemaGuidedExtractor(_Orch(), db_path)
    monkeypatch.setattr(
        extractor, "get_document_text",
        lambda pdf, hd=None: (
            "This program description discusses incentives and credits. "
            "See Appendix A for rates. No quantifiable charges are listed here."
        ),
    )

    result = extractor.extract_candidate_staged({
        "source_pdf": "brochure.pdf",
        "historical_document_id": 1,
        "parse_attempt_id": 1,
    })

    assert result is not None
    assert result.rate_rows == []
    assert _Orch.called is False
    assert "Skipped at stage 1" in result.warnings[0]


def test_staged_extractor_runs_find_then_classify_when_text_has_rates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """When text has rate tokens, stage 2 (find) + stage 3 (classify) run."""
    from duke_rates.document_intelligence.schema_extraction import (
        SchemaGuidedExtractor, CandidateRateExtraction, CandidateRateRow,
        RateLineList,
    )
    import sqlite3
    from duke_rates.db.schema import SCHEMA_SQL, migrate

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()

    class _FakeRun:
        def __init__(self, result):
            self.status = "ok"
            self.model = "fake-model"
            self.result = result

    call_log: list[str] = []

    class _Orch:
        def generate_json(self, *, role, prompt, schema, **_):
            call_log.append(schema.__name__)
            if schema is RateLineList:
                return _FakeRun(RateLineList(rate_lines=[
                    "Basic Customer Charge per month $14.00",
                    "Energy Charge 10.369 cents per kWh",
                ]))
            # Per-line classify path
            # The source line appears in the "Source line:" section of the
            # classify prompt — match on that section, not the whole prompt.
            source_section = prompt.split("Source line:", 1)[-1].split("Context", 1)[0]
            if "Basic Customer" in source_section:
                return _FakeRun(CandidateRateRow(
                    charge_type="Fixed Monthly Charge",
                    value=14.00,
                    unit="$/month",
                    source_quote="Basic Customer Charge per month $14.00",
                    confidence=0.95,
                ))
            return _FakeRun(CandidateRateRow(
                charge_type="Energy Charge",
                value=10.369,
                unit="¢/kWh",
                source_quote="Energy Charge 10.369 cents per kWh",
                confidence=0.9,
            ))

    extractor = SchemaGuidedExtractor(_Orch(), db_path)
    rate_text = (
        "Schedule RES-28 Residential\n"
        "Basic Customer Charge per month $14.00\n"
        "Energy Charge 10.369 cents per kWh\n"
        "Fuel Adjustment $0.001 per kWh\n"
        "Demand Charge $7.50 per kW\n"  # additional $ tokens to pass stage 1
    )
    monkeypatch.setattr(extractor, "get_document_text", lambda pdf, hd=None: rate_text)

    result = extractor.extract_candidate_staged({
        "source_pdf": "tariff.pdf",
        "historical_document_id": 1,
        "parse_attempt_id": 1,
    })

    assert result is not None
    assert len(result.rate_rows) == 2
    assert call_log[0] == "RateLineList"
    # Classifier should have been invoked once per found line.
    assert call_log.count("CandidateRateRow") == 2

    types = {r.charge_type for r in result.rate_rows}
    assert types == {"Fixed Monthly Charge", "Energy Charge"}
    units = {r.unit for r in result.rate_rows}
    assert units == {"$/month", "¢/kWh"}


def test_staged_extractor_flags_low_confidence_for_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """When average row confidence is below 0.5, a review warning is emitted."""
    from duke_rates.document_intelligence.schema_extraction import (
        SchemaGuidedExtractor, CandidateRateRow, RateLineList,
    )
    import sqlite3
    from duke_rates.db.schema import SCHEMA_SQL, migrate

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()

    class _FakeRun:
        def __init__(self, result):
            self.status = "ok"
            self.model = "fake-model"
            self.result = result

    class _Orch:
        def generate_json(self, *, schema, **_):
            if schema is RateLineList:
                return _FakeRun(RateLineList(rate_lines=[
                    "Some Rider $0.001 per kWh",
                ]))
            return _FakeRun(CandidateRateRow(
                charge_type="Rider Adjustment",
                value=0.001,
                unit="$/kWh",
                source_quote="Some Rider $0.001 per kWh",
                confidence=0.3,  # below threshold
            ))

    extractor = SchemaGuidedExtractor(_Orch(), db_path)
    monkeypatch.setattr(
        extractor, "get_document_text",
        lambda pdf, hd=None: (
            "Schedule X Some Rider $0.001 per kWh applies $0.001 cents and "
            "another $0.002 per kWh."  # 3+ tokens to pass stage 1
        ),
    )

    result = extractor.extract_candidate_staged({
        "source_pdf": "x.pdf",
        "historical_document_id": 1,
        "parse_attempt_id": 1,
    })

    assert result is not None
    assert len(result.rate_rows) == 1
    assert any("Low average confidence" in w for w in result.warnings)


def test_grounded_validator_accepts_correct_capture(tmp_path: Path) -> None:
    """Phase 6F: validation accepts a regex that captures the expected value."""
    from duke_rates.document_intelligence.extraction_grounded_rules import (
        ExtractionGroundedRuleGenerator,
    )
    from duke_rates.document_intelligence.document_specific_rules import ensure_schema
    from duke_rates.document_intelligence.regex_suggestions import RegexSuggestion

    db = tmp_path / "rules.db"
    ensure_schema(db)
    gen = ExtractionGroundedRuleGenerator(_DummyOrchestrator(), db)
    suggestion = RegexSuggestion(
        suggestion_type="regex_candidate",
        candidate_regex=r"Basic\s+Customer\s+Charge[\s\S]*?\$(\d+\.\d+)",
        expected_unit="$/month",
        confidence=0.9,
    )
    candidate = {
        "extraction_id": 1,
        "source_pdf": "fake.pdf",
        "row_index": 0,
        "source_quote": "I. Basic Customer Charge, per month $14.00",
        "value": 14.0,
        "unit": "$/month",
        "charge_type": "Fixed Monthly Charge",
        "target_field": "fixed_charge",
        "anchors": ["Basic Customer Charge"],
    }
    result = gen.validate(suggestion, candidate)
    assert result.accept is True
    assert result.captures_expected_value is True
    assert result.captured_values == ["14.00"]


def test_grounded_validator_rejects_when_anchor_missing(tmp_path: Path) -> None:
    """Validator rejects regexes that lack a document-specific anchor."""
    from duke_rates.document_intelligence.extraction_grounded_rules import (
        ExtractionGroundedRuleGenerator,
    )
    from duke_rates.document_intelligence.document_specific_rules import ensure_schema
    from duke_rates.document_intelligence.regex_suggestions import RegexSuggestion

    db = tmp_path / "rules.db"
    ensure_schema(db)
    gen = ExtractionGroundedRuleGenerator(_DummyOrchestrator(), db)
    suggestion = RegexSuggestion(
        suggestion_type="regex_candidate",
        candidate_regex=r"\$(\d+\.\d+)",  # no anchor
        expected_unit="$/month",
        confidence=0.9,
    )
    candidate = {
        "extraction_id": 1,
        "source_pdf": "fake.pdf",
        "row_index": 0,
        "source_quote": "I. Basic Customer Charge, per month $14.00",
        "value": 14.0,
        "unit": "$/month",
        "charge_type": "Fixed Monthly Charge",
        "target_field": "fixed_charge",
        "anchors": ["RES-28"],
    }
    result = gen.validate(suggestion, candidate)
    assert result.accept is False
    assert "lacks a document-specific anchor" in result.reason


def test_grounded_validator_rejects_when_value_off(tmp_path: Path) -> None:
    """Validator rejects when captured value doesn't match expected (within 1%)."""
    from duke_rates.document_intelligence.extraction_grounded_rules import (
        ExtractionGroundedRuleGenerator,
    )
    from duke_rates.document_intelligence.document_specific_rules import ensure_schema
    from duke_rates.document_intelligence.regex_suggestions import RegexSuggestion

    db = tmp_path / "rules.db"
    ensure_schema(db)
    gen = ExtractionGroundedRuleGenerator(_DummyOrchestrator(), db)
    suggestion = RegexSuggestion(
        suggestion_type="regex_candidate",
        candidate_regex=r"Basic\s+Customer\s+Charge[\s\S]*?\$(\d+)",  # captures int part only
        expected_unit="$/month",
        confidence=0.9,
    )
    candidate = {
        "extraction_id": 1,
        "source_pdf": "fake.pdf",
        "row_index": 0,
        "source_quote": "I. Basic Customer Charge, per month $14.00",
        "value": 99.0,  # expecting 99 but regex would capture 14
        "unit": "$/month",
        "charge_type": "Fixed Monthly Charge",
        "target_field": "fixed_charge",
        "anchors": ["Basic Customer Charge"],
    }
    result = gen.validate(suggestion, candidate)
    assert result.accept is False
    assert "didn't capture expected value" in result.reason


def test_grounded_compact_for_anchor_strips_regex_tokens() -> None:
    """The compaction helper strips \\s+ and similar regex tokens before comparison."""
    from duke_rates.document_intelligence.extraction_grounded_rules import (
        _compact_for_anchor_check,
    )

    # Anchor "Basic Customer Charge" should be findable inside a regex that
    # uses \s+ between tokens.
    out = _compact_for_anchor_check(r"Basic\s+Customer\s+Charge[\s\S]*?\$(\d+\.\d+)")
    assert "basiccustomercharge" in out
    # Should also handle [\s\S]*? and (\d+\.\d+) gracefully
    out2 = _compact_for_anchor_check(r"RES-28[\s\S]*?\$(\d+)")
    assert "res28" in out2


def test_staged_classify_benchmark_scores_unit_match() -> None:
    """The staged_classify_line scorer detects unit-match against expected unit."""
    from duke_rates.document_intelligence.model_benchmark import score_task_output

    parsed_good = {
        "charge_type": "Energy Charge",
        "unit": "¢/kWh",
        "value": 10.369,
        "confidence": 0.9,
    }
    parsed_bare_dollar = {
        "charge_type": "Fixed Monthly Charge",
        "unit": "$",
        "value": 14.0,
        "confidence": 0.8,
    }
    parsed_empty_unit = {
        "charge_type": "Demand Charge",
        "unit": "",
        "value": 2.53,
        "confidence": 0.0,
    }

    ctx_cents = {"expected_unit": "¢/kWh"}
    m1 = score_task_output("staged_classify_line", parsed_good, context=ctx_cents)
    assert m1["unit_matches_expected"] is True
    assert m1["bare_dollar_bug"] is False
    assert m1["empty_unit_bug"] is False
    assert m1["confidence_nonzero"] is True

    ctx_monthly = {"expected_unit": "$/month"}
    m2 = score_task_output("staged_classify_line", parsed_bare_dollar, context=ctx_monthly)
    assert m2["unit_matches_expected"] is False
    assert m2["bare_dollar_bug"] is True
    assert m2["empty_unit_bug"] is False

    ctx_kw = {"expected_unit": "$/kW"}
    m3 = score_task_output("staged_classify_line", parsed_empty_unit, context=ctx_kw)
    assert m3["unit_matches_expected"] is False
    assert m3["bare_dollar_bug"] is False
    assert m3["empty_unit_bug"] is True
    assert m3["confidence_nonzero"] is False


def test_staged_find_lines_benchmark_computes_recall() -> None:
    """The find-lines scorer compares returned lines against the deterministic baseline."""
    from duke_rates.document_intelligence.model_benchmark import score_task_output

    parsed = {"rate_lines": [
        "Basic Customer Charge per month $14.00",
        "Energy Charge 10.369 cents per kWh",
        "Demand Charge $7.50 per kW",
        "This is not a rate line",  # no $/¢
    ]}
    ctx = {"baseline_line_count": 3}
    metrics = score_task_output("staged_find_lines", parsed, context=ctx)

    assert metrics["line_count"] == 4
    assert metrics["valid_line_count"] == 3  # the 4th has no $ or ¢
    assert metrics["recall_ratio"] == 1.333  # 4/3 capped at 1.5
    assert metrics["actionable"] is True


def test_benchmark_speed_gate_penalizes_fast_broken_regex() -> None:
    """A model that is fast but produces broken regex must score lower than one
    that is equally fast but produces compilable regex.
    """
    from duke_rates.document_intelligence.model_benchmark import _specialization_score

    base_stats = {
        "valid_pct": 80.0,
        "actionable_pct": 80.0,
        "avg_tokens_per_second": 30.0,  # well above the 20 tps cap
        "avg_confidence": 0.8,
        "label_bias_score": 0.2,
        "accuracy_pct": 80.0,
        "regex_matches_target_pct": 60.0,
    }

    good = {**base_stats, "regex_compiles_pct": 80.0}
    broken = {**base_stats, "regex_compiles_pct": 10.0}

    good_score = _specialization_score(good)
    broken_score = _specialization_score(broken)
    assert good_score > broken_score
    # The compile-rate gate should remove most of the speed component for
    # the broken model. With a 90% drop in compile rate the gate cuts the
    # speed component roughly 5x.
    assert (good_score - broken_score) >= 10.0


def test_promotion_detection_uses_unresolved_schedule_bucket_without_consensus(
    tmp_path: Path,
) -> None:
    import sqlite3

    db_path = tmp_path / "rules.db"
    ensure_identity_schema(db_path)
    ensure_rule_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        for i in range(3):
            conn.execute(
                """
                INSERT INTO document_identity
                    (source_pdf, schedule_codes_strong_json, overall_confidence)
                VALUES (?, ?, 0.8)
                """,
                (f"doc-{i}.pdf", f'["RES-{40 + i}"]'),
            )
            doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """
                INSERT INTO document_specific_rules
                    (document_identity_id, candidate_regex, expected_unit, status)
                VALUES (?, ?, '¢/kWh', 'accepted')
                """,
                (
                    doc_id,
                    r"Schedule\s+RES-\d+[\s\S]*?(\d+\.\d+)¢\s+per\s+kWh",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    candidates = PromotionDetector(db_path).detect_all()

    assert len(candidates) == 1
    assert candidates[0].target_template == "unresolved_schedule:RES"
    assert candidates[0].cluster_size == 3
