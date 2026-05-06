from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import pdfplumber

from duke_rates.document_intelligence.normalization import _render_fitz_page
from duke_rates.document_intelligence.text_quality import analyze_text_quality

logger = logging.getLogger(__name__)


@dataclass
class RedlineAnalysisResult:
    label: str
    pdf_path: str
    page_number: int
    native_text_chars: int
    native_redline_signals: list[str]
    native_text_preview: str | None
    glm_available: bool
    elapsed_s: float
    redline_present: bool | None = None
    page_role: str | None = None
    visual_evidence: list[str] | None = None
    before_after_examples: list[dict[str, str]] | None = None
    notes: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_redline_analysis(
    pdf_path: str,
    *,
    page_number: int,
    label: str,
    ollama_host: str = "http://localhost:11434",
    glm_model: str = "glm-ocr",
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    source_pdf = Path(pdf_path)
    native_text = _extract_native_page_text(source_pdf, page_number=page_number)
    quality = analyze_text_quality(native_text)
    result = RedlineAnalysisResult(
        label=label,
        pdf_path=str(source_pdf),
        page_number=page_number,
        native_text_chars=len(native_text),
        native_redline_signals=quality.redline_codes + quality.suspicious_codes,
        native_text_preview=_preview_text(native_text),
        glm_available=True,
        elapsed_s=0.0,
    )
    start = time.perf_counter()
    try:
        payload = _analyze_page_image_with_glm(
            source_pdf,
            page_number=page_number,
            ollama_host=ollama_host,
            glm_model=glm_model,
            timeout_seconds=timeout_seconds,
        )
        result.redline_present = _coerce_bool(payload.get("redline_present"))
        result.page_role = _normalize_page_role(payload.get("page_role"))
        result.visual_evidence = _coerce_string_list(payload.get("visual_evidence"))
        result.before_after_examples = _coerce_before_after_examples(payload.get("before_after_examples"))
        result.notes = _coerce_string(payload.get("notes"))
    except Exception as exc:
        result.glm_available = False
        result.error = str(exc)
    result.elapsed_s = time.perf_counter() - start
    return result.as_dict()


def print_redline_analysis(result: dict[str, Any]) -> None:
    print(f"\n=== {result['label']} | page {result['page_number']} ===")
    print(result["pdf_path"])
    print(f"Native text chars: {result['native_text_chars']}")
    if result["native_redline_signals"]:
        print(f"Native signals: {', '.join(result['native_redline_signals'])}")
    if result.get("native_text_preview"):
        print(f"Native preview: {result['native_text_preview']}")
    if result.get("error"):
        print(f"GLM analysis failed: {result['error']}")
        return
    print(f"GLM redline_present: {result.get('redline_present')}")
    print(f"GLM page_role: {result.get('page_role')}")
    if result.get("visual_evidence"):
        print(f"Visual evidence: {', '.join(result['visual_evidence'])}")
    if result.get("before_after_examples"):
        print("Before/after examples:")
        for example in result["before_after_examples"]:
            print(f"  before: {example.get('before', '')}")
            print(f"  after : {example.get('after', '')}")
    if result.get("notes"):
        print(f"Notes: {result['notes']}")


def write_redline_analysis_json(results: list[dict[str, Any]], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")


def write_redline_analysis_markdown(results: list[dict[str, Any]], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Redline Analysis Benchmark", ""]
    for result in results:
        lines.extend(
            [
                f"## {result['label']} (page {result['page_number']})",
                "",
                f"- PDF: `{result['pdf_path']}`",
                f"- Native text chars: `{result['native_text_chars']}`",
                f"- Native signals: `{', '.join(result.get('native_redline_signals') or []) or 'none'}`",
                f"- GLM redline present: `{result.get('redline_present')}`",
                f"- GLM page role: `{result.get('page_role')}`",
            ]
        )
        if result.get("visual_evidence"):
            lines.append(f"- Visual evidence: `{'; '.join(result['visual_evidence'])}`")
        if result.get("notes"):
            lines.append(f"- Notes: {result['notes']}")
        if result.get("error"):
            lines.append(f"- Error: `{result['error']}`")
        if result.get("before_after_examples"):
            lines.extend(["", "| Before | After |", "| --- | --- |"])
            for example in result["before_after_examples"]:
                before = (example.get("before") or "").replace("|", "\\|")
                after = (example.get("after") or "").replace("|", "\\|")
                lines.append(f"| {before} | {after} |")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _analyze_page_image_with_glm(
    pdf_path: Path,
    *,
    page_number: int,
    ollama_host: str,
    glm_model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    import fitz

    with fitz.open(pdf_path) as pdf:
        page = pdf[page_number - 1]
        image = _render_fitz_page(page, dpi=180)
    image_bytes = io.BytesIO()
    image.save(image_bytes, format="PNG")
    encoded = base64.b64encode(image_bytes.getvalue()).decode("ascii")
    prompt = (
        "Analyze this tariff page image for redline or tracked-change evidence. "
        "Return only valid JSON with keys: "
        "redline_present (boolean), "
        "page_role ('clean'|'redline'|'unknown'), "
        "visual_evidence (array of short strings), "
        "before_after_examples (array of objects with keys 'before' and 'after'), "
        "notes (short string). "
        "Look for strike-through text, inserted or replacement text, duplicated revision labels, "
        "merged revision tokens, color cues if visible, and clean-vs-redline context."
    )
    payload = {
        "model": glm_model,
        "prompt": prompt,
        "images": [encoded],
        "stream": False,
    }
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.post(f"{ollama_host.rstrip('/')}/api/generate", json=payload)
        response.raise_for_status()
        data = response.json()
    text = str(data.get("response") or "").strip()
    parsed = _parse_json_object(text)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"GLM did not return JSON object: {text[:300]}")
    return parsed


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            normalized = _normalize_loose_json(stripped)
            if normalized is not None:
                return normalized
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        snippet = stripped[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            return _normalize_loose_json(snippet)
    return None


def _normalize_loose_json(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    candidate = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)", r'\1"\2"\3', candidate)
    candidate = re.sub(r":\s*'([^']*)'", lambda m: ': "' + m.group(1).replace('"', '\\"') + '"', candidate)
    candidate = re.sub(r"\btrue\b", "true", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bfalse\b", "false", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bnull\b", "null", candidate, flags=re.IGNORECASE)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _extract_native_page_text(pdf_path: Path, *, page_number: int) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        return (pdf.pages[page_number - 1].extract_text() or "").strip()


def _preview_text(text: str, limit: int = 400) -> str | None:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes"}:
            return True
        if lowered in {"false", "no"}:
            return False
    return None


def _normalize_page_role(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in {"clean", "redline", "unknown"}:
        return lowered
    return value.strip()


def _coerce_string_list(value: Any) -> list[str] | None:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return None


def _coerce_before_after_examples(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list):
        return None
    examples: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            before = str(item.get("before") or "").strip()
            after = str(item.get("after") or "").strip()
            if before or after:
                examples.append({"before": before, "after": after})
    return examples or None


def _coerce_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
