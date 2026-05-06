"""
Ollama model benchmarks for document-intelligence roles.

This module compares explicit local Ollama models against representative
production prompts and schemas without mutating the application database.
It is intentionally separate from the overnight loops so model selection can
be tested before changing role mappings in ``config/ollama_models.yaml``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

import httpx

from duke_rates.document_intelligence.llm_classifier import (
    LLMAdjudicationVerdict,
    LLMAdjudicator,
)
from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator
from duke_rates.document_intelligence.parse_diagnosis import (
    ALLOWED_FAILURE_TYPES,
    ALLOWED_RECOMMENDED_ACTIONS,
    ParseFailureDiagnosis,
    ParseFailureDiagnoser,
    _DIAGNOSIS_SYSTEM_PROMPT,
)
from duke_rates.document_intelligence.regex_suggestions import (
    ALLOWED_RISK_LEVELS,
    ALLOWED_SUGGESTION_TYPES,
    RegexSuggestion,
    RegexSuggestionGenerator,
    _SUGGESTION_SYSTEM_PROMPT,
)
from duke_rates.document_intelligence.schema_extraction import (
    ALLOWED_CHARGE_TYPES,
    ALLOWED_TOU_PERIODS,
    ALLOWED_UNITS,
    CandidateRateExtraction,
    SchemaGuidedExtractor,
    _EXTRACTION_SYSTEM_PROMPT,
)


TASK_TO_ROLE: dict[str, str] = {
    "parse_diagnosis": "parse_failure_triage",
    "hard_parse_diagnosis": "hard_parse_diagnosis",
    "regex_suggestion": "regex_suggestion",
    "structured_rate_extraction": "structured_rate_extraction",
    "document_classification": "balanced_classifier",
}

TASK_CHOICES: tuple[str, ...] = tuple(TASK_TO_ROLE)
DEFAULT_SPECIALIZATION_TASKS: tuple[str, ...] = (
    "parse_diagnosis",
    "regex_suggestion",
    "structured_rate_extraction",
    "document_classification",
)


@dataclass
class BenchmarkCase:
    task: str
    case_id: str
    subject_id: str
    subject_label: str
    prompt: str
    schema: type
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkRun:
    task: str
    role: str
    model: str
    case_id: str
    subject_id: str
    subject_label: str
    status: str
    duration_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_per_second: float = 0.0
    raw_payload: str | None = None
    validation_error: str | None = None
    parsed: dict[str, Any] | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


def run_ollama_role_benchmark(
    *,
    db_path: Path,
    task: str,
    models: list[str] | None = None,
    limit: int = 5,
    max_runtime_minutes: float | None = None,
    config_path: Path | None = None,
    output_path: Path | None = None,
    timeout_s: float | None = None,
    fixtures_path: Path | None = None,
) -> dict[str, Any]:
    """Run a bounded benchmark for one document-intelligence task."""
    if task not in TASK_TO_ROLE:
        raise ValueError(f"Unsupported benchmark task {task!r}; expected one of {TASK_CHOICES}")

    orchestrator = OllamaOrchestrator(config_path=config_path, db_path=None)
    role = TASK_TO_ROLE[task]
    role_cfg = orchestrator.roles.get(role)
    if role_cfg is None:
        raise ValueError(f"Role {role!r} is not configured")

    selected_models = models or [role_cfg.primary, *role_cfg.fallback]
    selected_models = [m for m in selected_models if m]
    if not selected_models:
        raise ValueError(f"No models selected for role {role!r}")

    fixture_case_ids = expected_fixture_case_ids(fixtures_path, task)
    cases = build_cases(
        db_path=db_path,
        task=task,
        limit=limit,
        fixture_case_ids=fixture_case_ids,
    )
    apply_expected_fixtures(cases, fixtures_path)
    deadline = (
        time.monotonic() + (max_runtime_minutes * 60.0)
        if max_runtime_minutes and max_runtime_minutes > 0
        else None
    )
    request_timeout_s = timeout_s or role_cfg.timeout_s or 120.0

    runs: list[BenchmarkRun] = []
    stop_reason = "completed"
    for case in cases:
        for model in selected_models:
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "max_runtime_minutes"
                break
            runs.append(
                call_model_for_case(
                    host=orchestrator.host,
                    model=model,
                    task=task,
                    role=role,
                    case=case,
                    timeout_s=request_timeout_s,
                    options=role_cfg.options,
                )
            )
        if stop_reason != "completed":
            break

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task": task,
        "role": role,
        "models": selected_models,
        "limit": limit,
        "cases_selected": len(cases),
        "gold_case_count": sum(1 for case in cases if case.metadata.get("expected")),
        "runs_completed": len(runs),
        "stop_reason": stop_reason,
        "host": orchestrator.host,
        "summary": summarize_runs(runs),
        "cases": [
            {
                "case_id": case.case_id,
                "subject_id": case.subject_id,
                "subject_label": case.subject_label,
                "metadata": case.metadata,
            }
            for case in cases
        ],
        "runs": [run_to_dict(run) for run in runs],
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    return report


def run_ollama_specialization_benchmark(
    *,
    db_path: Path,
    tasks: list[str],
    models: list[str] | None = None,
    limit: int = 5,
    max_runtime_minutes: float | None = None,
    config_path: Path | None = None,
    output_path: Path | None = None,
    timeout_s: float | None = None,
    fixtures_path: Path | None = None,
) -> dict[str, Any]:
    """Run a bounded benchmark across multiple tasks for specialization analysis."""
    selected_tasks = normalize_task_list(tasks)
    orchestrator = OllamaOrchestrator(config_path=config_path, db_path=None)

    if models:
        selected_models = [m for m in models if m]
    else:
        seen: set[str] = set()
        selected_models = []
        for task in selected_tasks:
            role_cfg = orchestrator.roles.get(TASK_TO_ROLE[task])
            if not role_cfg:
                continue
            for model in [role_cfg.primary, *role_cfg.fallback]:
                if model and model not in seen:
                    selected_models.append(model)
                    seen.add(model)
    if not selected_models:
        raise ValueError("No models selected for specialization benchmark")

    deadline = (
        time.monotonic() + (max_runtime_minutes * 60.0)
        if max_runtime_minutes and max_runtime_minutes > 0
        else None
    )
    task_reports: dict[str, Any] = {}
    all_runs: list[dict[str, Any]] = []
    stop_reason = "completed"

    for task in selected_tasks:
        if deadline is not None and time.monotonic() >= deadline:
            stop_reason = "max_runtime_minutes"
            break
        remaining_minutes = (
            max((deadline - time.monotonic()) / 60.0, 0.01)
            if deadline is not None
            else None
        )
        report = run_ollama_role_benchmark(
            db_path=db_path,
            task=task,
            models=selected_models,
            limit=limit,
            max_runtime_minutes=remaining_minutes,
            config_path=config_path,
            output_path=None,
            timeout_s=timeout_s,
            fixtures_path=fixtures_path,
        )
        task_reports[task] = report
        all_runs.extend(report["runs"])
        if report.get("stop_reason") != "completed":
            stop_reason = str(report.get("stop_reason") or "stopped")
            break

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task": "multi_task_specialization",
        "tasks": selected_tasks,
        "models": selected_models,
        "limit": limit,
        "gold_case_count": sum(int(r.get("gold_case_count") or 0) for r in task_reports.values()),
        "runs_completed": len(all_runs),
        "stop_reason": stop_reason,
        "host": orchestrator.host,
        "task_reports": task_reports,
        "specialization": summarize_specialization(task_reports),
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    return report


def normalize_task_list(tasks: list[str]) -> list[str]:
    """Normalize CLI task tokens, supporting comma-separated values and all."""
    normalized: list[str] = []
    for raw in tasks:
        for item in str(raw).split(","):
            task = item.strip()
            if not task:
                continue
            if task == "all":
                for default_task in DEFAULT_SPECIALIZATION_TASKS:
                    if default_task not in normalized:
                        normalized.append(default_task)
                continue
            if task not in TASK_TO_ROLE:
                raise ValueError(
                    f"Unsupported benchmark task {task!r}; expected one of "
                    f"{', '.join((*TASK_CHOICES, 'all'))}"
                )
            if task not in normalized:
                normalized.append(task)
    return normalized or ["parse_diagnosis"]


def apply_expected_fixtures(cases: list[BenchmarkCase], fixtures_path: Path | None) -> None:
    """Attach expected labels/metrics to matching cases, if a fixture file exists."""
    if not fixtures_path:
        return
    fixtures = load_expected_fixtures(fixtures_path)
    if not fixtures:
        return
    for case in cases:
        expected = fixtures.get((case.task, case.case_id)) or fixtures.get(("*", case.case_id))
        if expected:
            case.metadata["expected"] = expected


def load_expected_fixtures(fixtures_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Load benchmark gold fixtures.

    Supported JSON shapes:
    - {"fixtures": [{"task": "parse_diagnosis", "case_id": "...", "expected": {...}}]}
    - [{"task": "parse_diagnosis", "case_id": "...", "expected": {...}}]
    """
    if not fixtures_path.exists():
        raise FileNotFoundError(f"Benchmark fixture file not found: {fixtures_path}")
    raw = json.loads(fixtures_path.read_text(encoding="utf-8"))
    rows = raw.get("fixtures") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("Benchmark fixture file must contain a list or a {'fixtures': [...]} object")

    fixtures: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            continue
        task = str(row.get("task") or "*").strip() or "*"
        expected = row.get("expected")
        if not isinstance(expected, dict):
            expected = {
                key: value
                for key, value in row.items()
                if key not in {"task", "case_id", "subject_id", "notes"}
            }
        if expected:
            fixtures[(task, case_id)] = expected
    return fixtures


def expected_fixture_case_ids(fixtures_path: Path | None, task: str) -> set[str]:
    """Return case IDs explicitly listed in the gold fixtures for one task."""
    if not fixtures_path:
        return set()
    fixtures = load_expected_fixtures(fixtures_path)
    return {
        case_id
        for fixture_task, case_id in fixtures
        if fixture_task in (task, "*")
    }


def build_cases(
    *,
    db_path: Path,
    task: str,
    limit: int,
    fixture_case_ids: set[str] | None = None,
) -> list[BenchmarkCase]:
    """Build deterministic benchmark cases for a task."""
    if task in ("parse_diagnosis", "hard_parse_diagnosis"):
        return build_parse_diagnosis_cases(db_path, task, limit)
    if task == "regex_suggestion":
        return build_regex_suggestion_cases(db_path, limit, fixture_case_ids=fixture_case_ids)
    if task == "structured_rate_extraction":
        return build_structured_extraction_cases(db_path, limit)
    if task == "document_classification":
        return build_document_classification_cases(db_path, limit)
    raise ValueError(f"Unsupported benchmark task {task!r}")


def build_parse_diagnosis_cases(db_path: Path, task: str, limit: int) -> list[BenchmarkCase]:
    diagnoser = ParseFailureDiagnoser(
        OllamaOrchestrator(db_path=None),
        db_path,
        role=TASK_TO_ROLE[task],
        hard_role=TASK_TO_ROLE[task],
    )
    candidates = diagnoser.select_candidates(limit=limit)
    cases: list[BenchmarkCase] = []
    for candidate in candidates:
        source_pdf = candidate.get("source_pdf", "")
        family_key = candidate.get("family_key") or "unknown"
        metadata_json = candidate.get("metadata_json", "{}")
        confidence = float(candidate.get("confidence") or 0.0)
        text = diagnoser.get_document_text(
            source_pdf,
            candidate.get("historical_document_id"),
        )
        prompt = _DIAGNOSIS_SYSTEM_PROMPT.format(
            document_path=source_pdf,
            family_key=family_key,
            parser_profile=candidate.get("parser_profile") or "unknown",
            effective_date=candidate.get("effective_date") or "unknown",
            parser_status=candidate.get("status") or "unknown",
            parser_confidence=f"{confidence:.3f}",
            charge_count=candidate.get("charge_count", 0),
            expected_charges=(
                f"{diagnoser.get_family_peak_charges(family_key)} (peak for family)"
                if diagnoser.get_family_peak_charges(family_key)
                else "unknown"
            ),
            parser_evidence=diagnoser.get_parser_evidence(metadata_json),
            document_text=text if text else "(no text available)",
            text_quality=str(diagnoser.get_text_quality(source_pdf))[:500],
            allowed_failure_types=", ".join(ALLOWED_FAILURE_TYPES),
            allowed_actions=", ".join(ALLOWED_RECOMMENDED_ACTIONS),
        )
        cases.append(
            BenchmarkCase(
                task=task,
                case_id=f"parse_attempt:{candidate.get('parse_attempt_id')}",
                subject_id=str(candidate.get("parse_attempt_id")),
                subject_label=str(family_key),
                prompt=prompt,
                schema=ParseFailureDiagnosis,
                metadata={
                    "source_pdf": source_pdf,
                    "historical_document_id": candidate.get("historical_document_id"),
                    "parser_profile": candidate.get("parser_profile"),
                    "charge_count": candidate.get("charge_count"),
                    "text_chars": len(text),
                },
            )
        )
    return cases


def build_regex_suggestion_cases(
    db_path: Path,
    limit: int,
    *,
    fixture_case_ids: set[str] | None = None,
) -> list[BenchmarkCase]:
    generator = RegexSuggestionGenerator(OllamaOrchestrator(db_path=None), db_path)
    rows = generator.select_diagnoses_for_suggestion(limit=limit)
    existing_ids = {
        f"diagnosis:{row.get('diagnosis_id')}"
        for row in rows
        if row.get("diagnosis_id") is not None
    }
    fixture_ids = {
        int(case_id.split(":", 1)[1])
        for case_id in (fixture_case_ids or set())
        if case_id.startswith("diagnosis:")
        and case_id not in existing_ids
        and case_id.split(":", 1)[1].isdigit()
    }
    if fixture_ids:
        rows.extend(_select_regex_diagnoses_by_ids(db_path, sorted(fixture_ids)))
    cases: list[BenchmarkCase] = []
    for row in rows:
        parse_meta: dict[str, Any] = {}
        try:
            parse_meta = json.loads(row.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            parse_meta = {}
        parser_profile = row.get("parser_profile") or "unknown"
        missed_text = generator._get_missed_text(row, parse_meta)
        prompt = _SUGGESTION_SYSTEM_PROMPT.format(
            failure_type=row.get("failure_type") or "unknown",
            target_field=parse_meta.get("target_field", "") or "(unknown)",
            target_profile=parser_profile,
            current_patterns=generator.get_current_patterns(parser_profile),
            expected_schema=generator.get_expected_schema(parser_profile),
            missed_text=missed_text[:1500] if missed_text else "(no text available)",
            successful_examples=generator.get_successful_examples(parser_profile),
            allowed_types=", ".join(ALLOWED_SUGGESTION_TYPES),
            allowed_risks=", ".join(ALLOWED_RISK_LEVELS),
        )
        cases.append(
            BenchmarkCase(
                task="regex_suggestion",
                case_id=f"diagnosis:{row.get('diagnosis_id')}",
                subject_id=str(row.get("parse_attempt_id")),
                subject_label=parser_profile,
                prompt=prompt,
                schema=RegexSuggestion,
                metadata={
                    "diagnosis_id": row.get("diagnosis_id"),
                    "failure_type": row.get("failure_type"),
                    "text_chars": len(missed_text),
                },
            )
        )
    return cases


def _select_regex_diagnoses_by_ids(db_path: Path, diagnosis_ids: list[int]) -> list[dict[str, Any]]:
    """Load explicit regex-suggestion fixture rows, including already-suggested diagnoses."""
    if not diagnosis_ids:
        return []
    placeholders = ",".join("?" for _ in diagnosis_ids)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT ld.id AS diagnosis_id,
                   ld.parse_attempt_id,
                   ld.failure_type,
                   ld.confidence AS diagnosis_confidence,
                   ld.evidence_json,
                   ld.recommended_action,
                   pal.source_pdf,
                   pal.parser_profile,
                   pal.metadata_json,
                   pal.effective_date
            FROM llm_parse_diagnostics ld
            LEFT JOIN parse_attempt_logs pal ON pal.id = ld.parse_attempt_id
            WHERE ld.failure_type IN ('regex_gap', 'normalization_gap', 'ocr_noise')
              AND ld.id IN ({placeholders})
            ORDER BY ld.confidence DESC, ld.id ASC
            """,
            tuple(diagnosis_ids),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def build_structured_extraction_cases(db_path: Path, limit: int) -> list[BenchmarkCase]:
    extractor = SchemaGuidedExtractor(OllamaOrchestrator(db_path=None), db_path)
    candidates = extractor.select_extraction_candidates(limit=max(limit * 4, limit))
    scored_cases: list[tuple[int, BenchmarkCase]] = []
    for candidate in candidates:
        signals = extractor.get_document_signals(candidate)
        text = extractor.get_document_text(
            candidate.get("source_pdf", ""),
            candidate.get("historical_document_id"),
        )
        prompt = _EXTRACTION_SYSTEM_PROMPT.format(
            utility=signals.utility or "(unknown)",
            tariff_family=signals.tariff_family or "(unknown)",
            effective_date=signals.effective_date or "(unknown)",
            leaf_number=signals.leaf_number or "(unknown)",
            is_redline=str(signals.is_redline),
            document_text=text,
            allowed_charge_types=", ".join(ALLOWED_CHARGE_TYPES),
            allowed_tou_periods=", ".join(ALLOWED_TOU_PERIODS),
            allowed_units=", ".join(ALLOWED_UNITS),
        )
        case = BenchmarkCase(
            task="structured_rate_extraction",
            case_id=f"parse_attempt:{candidate.get('parse_attempt_id')}",
            subject_id=str(candidate.get("parse_attempt_id")),
            subject_label=str(candidate.get("family_key") or "unknown"),
            prompt=prompt,
            schema=CandidateRateExtraction,
            metadata={
                "source_pdf": candidate.get("source_pdf"),
                "historical_document_id": candidate.get("historical_document_id"),
                "parser_profile": candidate.get("parser_profile"),
                "text_chars": len(text),
            },
        )
        scored_cases.append((_rate_signal_score(text), case))
    scored_cases.sort(key=lambda item: item[0], reverse=True)
    return [case for score, case in scored_cases if score > 0][:limit] or [
        case for _, case in scored_cases[:limit]
    ]


def build_document_classification_cases(db_path: Path, limit: int) -> list[BenchmarkCase]:
    adjudicator = LLMAdjudicator(OllamaOrchestrator(db_path=None), db_path)
    cases: list[BenchmarkCase] = []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT hd.id, hd.title, hd.family_key, hd.raw_text_path
            FROM historical_documents hd
            WHERE COALESCE(hd.raw_text_path, '') != ''
            ORDER BY hd.id DESC
            LIMIT ?
            """,
            (max(limit * 5, limit),),
        ).fetchall()

    for row in rows:
        text = _read_text_path(row["raw_text_path"], 2500)
        if len(text.strip()) < 50:
            continue
        prompt = adjudicator._build_prompt(text, None, None)
        cases.append(
            BenchmarkCase(
                task="document_classification",
                case_id=f"historical_document:{row['id']}",
                subject_id=str(row["id"]),
                subject_label=row["title"] or row["family_key"] or "unknown",
                prompt=prompt,
                schema=LLMAdjudicationVerdict,
                metadata={
                    "family_key": row["family_key"],
                    "title": row["title"],
                    "text_chars": len(text),
                },
            )
        )
        if len(cases) >= limit:
            break
    return cases


def call_model_for_case(
    *,
    host: str,
    model: str,
    task: str,
    role: str,
    case: BenchmarkCase,
    timeout_s: float,
    options: dict[str, Any],
) -> BenchmarkRun:
    """Call one explicit model for one benchmark case."""
    start = time.perf_counter()
    payload = {
        "model": model,
        "prompt": case.prompt,
        "stream": False,
        "format": "json",
        "options": dict(options or {}),
    }
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(f"{host.rstrip('/')}/api/generate", json=payload)
    except httpx.TimeoutException:
        return _run_error(task, role, model, case, start, "timeout", "request timed out")
    except Exception as exc:
        return _run_error(task, role, model, case, start, "http_error", str(exc))

    duration_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code != 200:
        return BenchmarkRun(
            task=task,
            role=role,
            model=model,
            case_id=case.case_id,
            subject_id=case.subject_id,
            subject_label=case.subject_label,
            status="http_error",
            duration_ms=duration_ms,
            raw_payload=response.text[:2000],
            validation_error=f"HTTP {response.status_code}",
        )

    try:
        data = response.json()
    except Exception as exc:
        return BenchmarkRun(
            task=task,
            role=role,
            model=model,
            case_id=case.case_id,
            subject_id=case.subject_id,
            subject_label=case.subject_label,
            status="response_json_error",
            duration_ms=duration_ms,
            raw_payload=response.text[:2000],
            validation_error=str(exc),
        )

    raw_text = str(data.get("response") or "")
    tokens_in = _int_or(data.get("prompt_eval_count"), 0)
    tokens_out = _int_or(data.get("eval_count"), 0)
    eval_duration_ns = _int_or(data.get("eval_duration"), 0)
    tokens_per_second = (
        round(tokens_out / (eval_duration_ns / 1_000_000_000), 2)
        if tokens_out and eval_duration_ns
        else 0.0
    )

    try:
        parsed_json = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return BenchmarkRun(
            task=task,
            role=role,
            model=model,
            case_id=case.case_id,
            subject_id=case.subject_id,
            subject_label=case.subject_label,
            status="json_parse_error",
            duration_ms=duration_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_per_second=tokens_per_second,
            raw_payload=raw_text[:2000],
            validation_error=str(exc),
        )

    try:
        validated = case.schema.model_validate(parsed_json)
    except Exception as exc:
        return BenchmarkRun(
            task=task,
            role=role,
            model=model,
            case_id=case.case_id,
            subject_id=case.subject_id,
            subject_label=case.subject_label,
            status="validation_error",
            duration_ms=duration_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_per_second=tokens_per_second,
            raw_payload=raw_text[:2000],
            validation_error=str(exc)[:2000],
        )

    parsed = validated.model_dump()
    return BenchmarkRun(
        task=task,
        role=role,
        model=model,
        case_id=case.case_id,
        subject_id=case.subject_id,
        subject_label=case.subject_label,
        status="ok",
        duration_ms=duration_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_per_second=tokens_per_second,
        raw_payload=raw_text[:2000],
        parsed=parsed,
        metrics=score_task_output(task, parsed, case.metadata.get("expected")),
    )


def score_task_output(
    task: str,
    parsed: dict[str, Any],
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute task-specific quality proxies from a validated response."""
    if task in ("parse_diagnosis", "hard_parse_diagnosis"):
        failure_type = str(parsed.get("failure_type") or "unknown")
        evidence = parsed.get("evidence") or []
        confidence = float(parsed.get("confidence") or 0.0)
        metrics = {
            "actionable": failure_type != "unknown" and confidence > 0.0,
            "failure_type": failure_type,
            "recommended_action": parsed.get("recommended_action"),
            "confidence": confidence,
            "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
        }
        return _with_expected_metrics(metrics, expected, "failure_type")
    if task == "regex_suggestion":
        confidence = float(parsed.get("confidence") or 0.0)
        candidate_regex = str(parsed.get("candidate_regex") or "")
        candidate_normalization = str(parsed.get("candidate_normalization") or "")
        positives = parsed.get("positive_test_cases") or []
        negatives = parsed.get("negative_test_cases") or []
        metrics = {
            "actionable": bool(candidate_regex or candidate_normalization),
            "suggestion_type": parsed.get("suggestion_type"),
            "confidence": confidence,
            "has_regex": bool(candidate_regex),
            "has_normalization": bool(candidate_normalization),
            "positive_tests": len(positives) if isinstance(positives, list) else 0,
            "negative_tests": len(negatives) if isinstance(negatives, list) else 0,
        }
        return _with_expected_metrics(metrics, expected, "suggestion_type")
    if task == "structured_rate_extraction":
        rows = parsed.get("rate_rows") or []
        warnings = parsed.get("warnings") or []
        confidence = float(parsed.get("extraction_confidence") or 0.0)
        metrics = {
            "actionable": bool(rows),
            "rate_row_count": len(rows) if isinstance(rows, list) else 0,
            "confidence": confidence,
            "warning_count": len(warnings) if isinstance(warnings, list) else 0,
        }
        return _with_expected_metrics(metrics, expected, "min_rate_row_count")
    if task == "document_classification":
        document_type = str(parsed.get("document_type") or parsed.get("label") or "UNKNOWN")
        confidence = float(parsed.get("confidence") or 0.0)
        signals = parsed.get("key_signals") or []
        metrics = {
            "actionable": document_type != "UNKNOWN" and confidence > 0.0,
            "document_type": document_type,
            "confidence": confidence,
            "key_signal_count": len(signals) if isinstance(signals, list) else 0,
        }
        return _with_expected_metrics(metrics, expected, "document_type")
    return {}


def summarize_runs(runs: list[BenchmarkRun]) -> dict[str, Any]:
    """Aggregate benchmark runs by model."""
    by_model: dict[str, list[BenchmarkRun]] = {}
    for run in runs:
        by_model.setdefault(run.model, []).append(run)

    summary: dict[str, Any] = {}
    for model, items in by_model.items():
        ok = [r for r in items if r.status == "ok"]
        durations = [r.duration_ms for r in items]
        tps_values = [r.tokens_per_second for r in ok if r.tokens_per_second]
        actionable = [r for r in ok if r.metrics.get("actionable")]
        confidences = [
            float(r.metrics["confidence"])
            for r in ok
            if isinstance(r.metrics.get("confidence"), (int, float))
        ]
        expected_runs = [r for r in ok if "expected_match" in r.metrics]
        expected_matches = [r for r in expected_runs if r.metrics.get("expected_match")]
        statuses: dict[str, int] = {}
        for run in items:
            statuses[run.status] = statuses.get(run.status, 0) + 1
        summary[model] = {
            "runs": len(items),
            "ok": len(ok),
            "valid_pct": round((len(ok) / len(items)) * 100, 2) if items else 0.0,
            "actionable": len(actionable),
            "actionable_pct": round((len(actionable) / len(ok)) * 100, 2) if ok else 0.0,
            "avg_duration_ms": round(mean(durations), 2) if durations else 0.0,
            "p50_duration_ms": round(median(durations), 2) if durations else 0.0,
            "avg_tokens_per_second": round(mean(tps_values), 2) if tps_values else 0.0,
            "avg_confidence": round(mean(confidences), 4) if confidences else 0.0,
            "gold_runs": len(expected_runs),
            "accuracy_pct": round((len(expected_matches) / len(expected_runs)) * 100, 2)
            if expected_runs
            else None,
            "label_bias_score": _label_bias_score(ok),
            "diversity_count": len(_task_distribution(ok)),
            "statuses": statuses,
            "task_distribution": _task_distribution(ok),
        }
    return summary


def summarize_specialization(task_reports: dict[str, Any]) -> dict[str, Any]:
    """Build cross-task summaries for selecting specialist models."""
    by_model: dict[str, dict[str, Any]] = {}
    by_task: dict[str, list[dict[str, Any]]] = {}

    for task, report in task_reports.items():
        task_rankings: list[dict[str, Any]] = []
        for model, stats in (report.get("summary") or {}).items():
            score = _specialization_score(stats)
            row = {
                "model": model,
                "task": task,
                "score": score,
                "valid_pct": stats.get("valid_pct", 0.0),
                "actionable_pct": stats.get("actionable_pct", 0.0),
                "avg_duration_ms": stats.get("avg_duration_ms", 0.0),
                "avg_tokens_per_second": stats.get("avg_tokens_per_second", 0.0),
                "avg_confidence": stats.get("avg_confidence", 0.0),
                "label_bias_score": stats.get("label_bias_score", 1.0),
                "diversity_count": stats.get("diversity_count", 0),
                "task_distribution": stats.get("task_distribution", {}),
            }
            task_rankings.append(row)
            by_model.setdefault(model, {"tasks": {}, "avg_score": 0.0})
            by_model[model]["tasks"][task] = row
        task_rankings.sort(key=lambda r: r["score"], reverse=True)
        by_task[task] = task_rankings

    for model, data in by_model.items():
        scores = [float(row["score"]) for row in data["tasks"].values()]
        data["avg_score"] = round(mean(scores), 4) if scores else 0.0
        data["best_task"] = max(
            data["tasks"].values(),
            key=lambda row: row["score"],
            default={},
        ).get("task")

    return {
        "best_by_task": {
            task: rows[0] if rows else None
            for task, rows in by_task.items()
        },
        "rankings_by_task": by_task,
        "model_profiles": by_model,
        "complementarity_by_task": {
            task: _complementarity(report.get("runs") or [])
            for task, report in task_reports.items()
        },
    }


def run_to_dict(run: BenchmarkRun) -> dict[str, Any]:
    return {
        "task": run.task,
        "role": run.role,
        "model": run.model,
        "case_id": run.case_id,
        "subject_id": run.subject_id,
        "subject_label": run.subject_label,
        "status": run.status,
        "duration_ms": run.duration_ms,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "tokens_per_second": run.tokens_per_second,
        "validation_error": run.validation_error,
        "metrics": run.metrics,
        "parsed": run.parsed,
        "raw_payload": run.raw_payload,
    }


def default_output_path(task: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_task = task.replace(",", "_").replace(" ", "_")
    return Path("docs/reports/ollama_model_benchmarks") / f"{timestamp}_{safe_task}.json"


def _task_distribution(runs: list[BenchmarkRun]) -> dict[str, int]:
    values: dict[str, int] = {}
    for run in runs:
        label = (
            run.metrics.get("failure_type")
            or run.metrics.get("suggestion_type")
            or run.metrics.get("document_type")
            or ("rows" if run.metrics.get("rate_row_count") else "empty")
        )
        label = str(label)
        values[label] = values.get(label, 0) + 1
    return values


def _label_bias_score(runs: list[BenchmarkRun]) -> float:
    distribution = _task_distribution(runs)
    total = sum(distribution.values())
    if not total:
        return 1.0
    return round(max(distribution.values()) / total, 4)


def _rate_signal_score(text: str) -> int:
    lower = text.lower()
    signals = (
        "monthly rate",
        "rate:",
        "¢/kwh",
        "cents/kwh",
        "per kwh",
        "$/kwh",
        "$/month",
        "basic customer charge",
        "basic facilities charge",
        "demand charge",
        "energy charge",
        "storm recovery charges",
        "cents per kilowatt",
    )
    return sum(1 for signal in signals if signal in lower)


def _specialization_score(stats: dict[str, Any]) -> float:
    valid = float(stats.get("valid_pct") or 0.0)
    actionable = float(stats.get("actionable_pct") or 0.0)
    tps = float(stats.get("avg_tokens_per_second") or 0.0)
    confidence = float(stats.get("avg_confidence") or 0.0)
    bias = float(stats.get("label_bias_score") or 1.0)
    accuracy = stats.get("accuracy_pct")
    speed_score = min(tps / 20.0, 1.0) * 100.0
    diversity_score = (1.0 - min(max(bias, 0.0), 1.0)) * 100.0
    accuracy_score = float(accuracy) if isinstance(accuracy, (int, float)) else valid
    score = (
        accuracy_score * 0.25
        + valid * 0.20
        + actionable * 0.25
        + speed_score * 0.15
        + diversity_score * 0.15
        + (confidence * 100.0) * 0.10
    )
    return round(score, 4)


def _with_expected_metrics(
    metrics: dict[str, Any],
    expected: dict[str, Any] | None,
    primary_key: str,
) -> dict[str, Any]:
    if not expected:
        return metrics

    expected_match = True
    checks: dict[str, bool] = {}
    for key, expected_value in expected.items():
        if key == "min_confidence":
            actual_conf = float(metrics.get("confidence") or 0.0)
            ok = actual_conf >= float(expected_value)
        elif key == "min_rate_row_count":
            actual_rows = int(metrics.get("rate_row_count") or 0)
            ok = actual_rows >= int(expected_value)
        elif key == "actionable":
            ok = bool(metrics.get("actionable")) is bool(expected_value)
        else:
            actual_value = metrics.get(key)
            if isinstance(expected_value, list):
                ok = actual_value in expected_value
            else:
                ok = actual_value == expected_value
        checks[key] = ok
        expected_match = expected_match and ok

    metrics["expected"] = expected
    metrics["expected_primary_key"] = primary_key
    metrics["expected_checks"] = checks
    metrics["expected_match"] = expected_match
    return metrics


def _run_label(run: dict[str, Any]) -> str:
    metrics = run.get("metrics") or {}
    return str(
        metrics.get("failure_type")
        or metrics.get("suggestion_type")
        or metrics.get("document_type")
        or ("rows" if metrics.get("rate_row_count") else "empty")
    )


def _complementarity(runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        if run.get("status") != "ok":
            continue
        by_case.setdefault(str(run.get("case_id")), []).append(run)

    pair_counts: dict[str, dict[str, int]] = {}
    for case_runs in by_case.values():
        for idx, left in enumerate(case_runs):
            for right in case_runs[idx + 1 :]:
                left_model = str(left.get("model"))
                right_model = str(right.get("model"))
                key = " | ".join(sorted([left_model, right_model]))
                pair_counts.setdefault(key, {"same": 0, "different": 0})
                if _run_label(left) == _run_label(right):
                    pair_counts[key]["same"] += 1
                else:
                    pair_counts[key]["different"] += 1

    result: dict[str, Any] = {}
    for pair, counts in pair_counts.items():
        total = counts["same"] + counts["different"]
        result[pair] = {
            **counts,
            "disagreement_pct": round((counts["different"] / total) * 100.0, 2)
            if total
            else 0.0,
        }
    return result


def _run_error(
    task: str,
    role: str,
    model: str,
    case: BenchmarkCase,
    start: float,
    status: str,
    error: str,
) -> BenchmarkRun:
    return BenchmarkRun(
        task=task,
        role=role,
        model=model,
        case_id=case.case_id,
        subject_id=case.subject_id,
        subject_label=case.subject_label,
        status=status,
        duration_ms=int((time.perf_counter() - start) * 1000),
        validation_error=error[:2000],
    )


def _read_text_path(path_value: str, max_chars: int) -> str:
    try:
        path = Path(path_value)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
