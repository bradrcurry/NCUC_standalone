"""
Ollama model orchestration layer (Phase 2.5).

Single entry point for all Ollama calls in Phase 3+. Every component asks
for a model by *role*, not by hardcoded name. The orchestrator handles
fallback, health probes, JSON schema validation, and optional run
persistence to ``ollama_model_runs``.

Does NOT decide what to do with results — it returns them. Callers
(Phase 4/5 classifiers) decide how to record into ``document_classifications``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RoleConfig:
    primary: str
    fallback: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    max_tokens: int = 1024
    description: str = ""
    probe_kind: str = "generate"  # generate | embed | tags_only
    timeout_s: float | None = None  # overrides default request_timeout_s for this role


@dataclass
class RoleHealth:
    role: str
    available: bool
    primary: str
    message: str | None = None


@dataclass
class OllamaRunResult:
    role: str
    model: str
    status: str  # ok | http_error | timeout | json_parse_error | validation_error | fallback_used
    duration_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    result: Any = None  # parsed Pydantic model on ok, else None
    raw_payload: str | None = None
    validation_error: str | None = None
    fallback_from: str | None = None  # set when status is fallback_used


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{(\w+)(?::-[^}]*)?\}")


def _resolve_env_vars(value: str) -> str:
    """Resolve ``${VAR:-default}`` patterns in *value*."""

    def _replace(m: re.Match[str]) -> str:
        var = m.group(1)
        full = m.group(0)
        # Check for :-default syntax by looking at the full match
        if ":-" in full:
            default = full[full.index(":-") + 2 : -1]
            return os.environ.get(var, default)
        return os.environ.get(var, "")

    return _ENV_VAR_RE.sub(_replace, value)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class OllamaOrchestrator:
    """Single entry point for all Ollama calls in Phase 2.5+.

    Parameters
    ----------
    config_path:
        Path to ``ollama_models.yaml``. Defaults to
        ``config/ollama_models.yaml`` relative to the project root.
    db_path:
        Path to the SQLite database. When set, every ``generate_json``,
        ``generate_text``, and ``embed`` call persists a row to
        ``ollama_model_runs``. When ``None``, runs are not persisted
        (useful for smoke tests and health checks).
    """

    _DEFAULT_CONFIG = Path("config/ollama_models.yaml")

    def __init__(
        self,
        config_path: Path | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        self._config_path = Path(config_path or self._DEFAULT_CONFIG)
        self._db_path = Path(db_path) if db_path else None
        self._config: dict[str, Any] = {}
        self._roles: dict[str, RoleConfig] = {}
        self._health_cache: dict[str, tuple[bool, str | None]] = {}
        self._host: str = "http://localhost:11434"
        self._request_timeout_s: float = 120.0
        self._load_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # Cross-process health cache: 5-minute TTL. Avoids re-probing every
    # role at the start of each wrapper-script iteration.
    _HEALTH_CACHE_FILE = Path(".cache/ollama_health.json")
    _HEALTH_CACHE_TTL_S = 300

    def health_probe(self, role: str) -> tuple[bool, str | None]:
        """Probe *role*'s primary model, then fallbacks.

        Caches the result for the process lifetime AND across processes
        (5-minute TTL on disk). Same fail-fast pattern as
        ``GlmOcrNormalizer.is_available()``.
        """
        if role in self._health_cache:
            return self._health_cache[role]

        disk_cached = self._read_disk_health_cache(role)
        if disk_cached is not None:
            self._health_cache[role] = disk_cached
            return disk_cached

        cfg = self._roles.get(role)
        if cfg is None:
            msg = f"Unknown role {role!r}"
            self._health_cache[role] = (False, msg)
            return self._health_cache[role]

        models_to_probe = [cfg.primary] + cfg.fallback
        probe_timeout = cfg.timeout_s or self._request_timeout_s
        for model in models_to_probe:
            ok, err = self._probe_model(model, cfg.probe_kind, probe_timeout)
            if ok:
                self._health_cache[role] = (True, None)
                self._write_disk_health_cache(role, True, None)
                return self._health_cache[role]

        msg = f"all {len(models_to_probe)} model(s) failed for role {role!r}"
        self._health_cache[role] = (False, msg)
        # Don't persist failures to disk — failure may be transient (e.g.
        # Ollama still starting) and we want the next process to retry.
        return self._health_cache[role]

    def _read_disk_health_cache(self, role: str) -> tuple[bool, str | None] | None:
        try:
            if not self._HEALTH_CACHE_FILE.exists():
                return None
            payload = json.loads(self._HEALTH_CACHE_FILE.read_text(encoding="utf-8"))
            entry = payload.get(role)
            if not entry:
                return None
            ts = float(entry.get("ts", 0))
            if time.time() - ts > self._HEALTH_CACHE_TTL_S:
                return None
            return (bool(entry.get("ok")), entry.get("err"))
        except Exception:
            return None

    def _write_disk_health_cache(
        self, role: str, ok: bool, err: str | None
    ) -> None:
        try:
            self._HEALTH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {}
            if self._HEALTH_CACHE_FILE.exists():
                try:
                    payload = json.loads(
                        self._HEALTH_CACHE_FILE.read_text(encoding="utf-8")
                    )
                except Exception:
                    payload = {}
            payload[role] = {"ok": ok, "err": err, "ts": time.time()}
            self._HEALTH_CACHE_FILE.write_text(
                json.dumps(payload), encoding="utf-8"
            )
        except Exception:
            pass

    def list_available_roles(self) -> list[RoleHealth]:
        """Return health status for every configured role."""
        result: list[RoleHealth] = []
        for name, cfg in self._roles.items():
            ok, msg = self.health_probe(name)
            result.append(RoleHealth(role=name, available=ok, primary=cfg.primary, message=msg))
        return result

    def generate_json(
        self,
        role: str,
        prompt: str,
        schema: type,
        *,
        subject_kind: str = "adhoc",
        subject_id: str = "0",
        stage: str = "adhoc",
    ) -> OllamaRunResult:
        """Call *role* in JSON mode, validate against *schema*.

        On primary failure, tries each fallback **once**. On all failures,
        returns a result with ``status='http_error'`` and the last error.
        On schema validation failure, returns ``status='validation_error'``
        with the raw payload — never raises.
        """
        return self._call_with_fallback(
            role=role,
            prompt=prompt,
            json_mode=True,
            schema=schema,
            subject_kind=subject_kind,
            subject_id=str(subject_id),
            stage=stage,
        )

    def generate_text(
        self,
        role: str,
        prompt: str,
        *,
        subject_kind: str = "adhoc",
        subject_id: str = "0",
        stage: str = "adhoc",
    ) -> OllamaRunResult:
        """Call *role* in plain-text mode (no JSON parsing)."""
        return self._call_with_fallback(
            role=role,
            prompt=prompt,
            json_mode=False,
            schema=None,
            subject_kind=subject_kind,
            subject_id=str(subject_id),
            stage=stage,
        )

    def embed(self, role: str, text: str) -> list[float]:
        """Return the embedding vector for *text* using *role*'s model.

        Embeddings are not persisted to ``ollama_model_runs`` — they are
        stateless and high-volume.
        """
        cfg = self._roles.get(role)
        if cfg is None:
            raise ValueError(f"Unknown role {role!r}")

        models_to_try = [cfg.primary] + cfg.fallback
        last_error: Exception | None = None
        for model in models_to_try:
            try:
                with httpx.Client(timeout=self._request_timeout_s) as client:
                    resp = client.post(
                        f"{self._host}/api/embeddings",
                        json={"model": model, "prompt": text},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    embedding = data.get("embedding")
                    if embedding and isinstance(embedding, list):
                        return [float(v) for v in embedding]
            except Exception as exc:
                last_error = exc
                continue

        raise RuntimeError(f"embed failed for role {role!r}: {last_error}")

    @property
    def host(self) -> str:
        return self._host

    @property
    def roles(self) -> dict[str, RoleConfig]:
        return dict(self._roles)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        if not self._config_path.exists():
            logger.warning(
                "ollama_models.yaml not found at %s — orchestrator will have no roles",
                self._config_path,
            )
            return

        with open(self._config_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        defaults = raw.get("defaults", {}) or {}
        self._host = _resolve_env_vars(str(defaults.get("host", "http://localhost:11434")))
        self._request_timeout_s = float(defaults.get("request_timeout_s", 120))

        for name, spec in (raw.get("roles") or {}).items():
            # max_tokens uses None-coalescing that correctly handles 0
            _mt = spec.get("max_tokens")
            if _mt is None:
                _mt = 1024
            _timeout = spec.get("timeout_s")
            self._roles[name] = RoleConfig(
                primary=spec.get("primary", ""),
                fallback=list(spec.get("fallback") or []),
                options=dict(spec.get("options") or {}),
                max_tokens=int(_mt),
                description=str(spec.get("description", "")),
                probe_kind=str(spec.get("probe_kind", "generate")),
                timeout_s=float(_timeout) if _timeout is not None else None,
            )

        self._config = raw

    def _probe_model(
        self, model: str, probe_kind: str = "generate", timeout_s: float | None = None
    ) -> tuple[bool, str | None]:
        """Check that *model* exists and can serve requests.

        *probe_kind* selects the validation endpoint:
        - ``"generate"`` — ``/api/generate`` with a tiny prompt (default)
        - ``"embed"``   — ``/api/embeddings`` (embedding-only models)
        - ``"tags_only"`` — only check ``/api/tags`` (vision/OCR models
          that cannot satisfy text-only probes)
        """
        _timeout = timeout_s or self._request_timeout_s
        try:
            with httpx.Client(timeout=5.0) as client:
                tags = client.get(f"{self._host}/api/tags")
                tags.raise_for_status()
                names = {m.get("name", "") for m in (tags.json().get("models") or [])}
                if model not in names and f"{model}:latest" not in names:
                    return False, f"model {model!r} not present at {self._host}"
        except Exception as exc:
            return False, f"/api/tags probe failed: {exc}"

        if probe_kind == "tags_only":
            return True, None

        if probe_kind == "embed":
            try:
                with httpx.Client(timeout=_timeout) as client:
                    resp = client.post(
                        f"{self._host}/api/embeddings",
                        json={"model": model, "prompt": "ok"},
                    )
                    if resp.status_code != 200:
                        body = resp.text[:200]
                        return False, f"embed probe failed status={resp.status_code} body={body!r}"
            except Exception as exc:
                return False, f"embed probe failed: {exc}"
            return True, None

        # Default: generate probe.
        try:
            with httpx.Client(timeout=_timeout) as client:
                gen = client.post(
                    f"{self._host}/api/generate",
                    json={"model": model, "prompt": "ok", "stream": False},
                )
                if gen.status_code != 200:
                    body = gen.text[:200]
                    return False, f"generate probe failed status={gen.status_code} body={body!r}"
        except Exception as exc:
            return False, f"generate probe failed: {exc}"

        return True, None

    def _call_with_fallback(
        self,
        *,
        role: str,
        prompt: str,
        json_mode: bool,
        schema: type | None,
        subject_kind: str,
        subject_id: str,
        stage: str,
    ) -> OllamaRunResult:
        cfg = self._roles.get(role)
        if cfg is None:
            return OllamaRunResult(
                role=role,
                model="",
                status="http_error",
                duration_ms=0,
                validation_error=f"Unknown role {role!r}",
            )

        models_to_try = [cfg.primary] + cfg.fallback
        last_result: OllamaRunResult | None = None
        call_timeout = cfg.timeout_s or self._request_timeout_s

        for idx, model in enumerate(models_to_try):
            result = self._call_model(
                model=model,
                prompt=prompt,
                json_mode=json_mode,
                schema=schema,
                role=role,
                options=cfg.options,
                max_tokens=cfg.max_tokens,
                timeout_s=call_timeout,
                subject_kind=subject_kind,
                subject_id=subject_id,
                stage=stage,
            )

            if result.status == "ok":
                if idx > 0:
                    result.fallback_from = models_to_try[0]
                    result.status = "fallback_used"
                self._persist_run(result, subject_kind, subject_id, stage, role)
                return result

            if result.status == "validation_error":
                # Schema validation failed — persist and try fallback
                last_result = result
                continue

            last_result = result

        # All models failed
        final = last_result or OllamaRunResult(
            role=role, model="", status="http_error", duration_ms=0,
        )
        self._persist_run(final, subject_kind, subject_id, stage, role)
        return final

    def _call_model(
        self,
        *,
        model: str,
        prompt: str,
        json_mode: bool,
        schema: type | None,
        role: str,
        options: dict[str, Any],
        max_tokens: int,
        timeout_s: float,
        subject_kind: str,
        subject_id: str,
        stage: str,
    ) -> OllamaRunResult:
        t0 = time.perf_counter()
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": options,
        }
        if json_mode:
            payload["format"] = "json"

        try:
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.post(f"{self._host}/api/generate", json=payload)
        except httpx.TimeoutException:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return OllamaRunResult(role=role, model=model, status="timeout", duration_ms=elapsed)
        except Exception as exc:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return OllamaRunResult(
                role=role, model=model, status="http_error", duration_ms=elapsed,
                validation_error=str(exc),
            )

        elapsed = int((time.perf_counter() - t0) * 1000)

        if resp.status_code != 200:
            return OllamaRunResult(
                role=role, model=model, status="http_error", duration_ms=elapsed,
                raw_payload=_truncate(resp.text),
                validation_error=f"HTTP {resp.status_code}",
            )

        try:
            data = resp.json()
        except Exception:
            return OllamaRunResult(
                role=role, model=model, status="json_parse_error", duration_ms=elapsed,
                raw_payload=_truncate(resp.text),
                validation_error="Ollama response is not valid JSON",
            )

        raw_text = str(data.get("response") or "")
        tokens_in = _int_or(data.get("prompt_eval_count"), 0)
        tokens_out = _int_or(data.get("eval_count"), 0)

        if not json_mode or schema is None:
            return OllamaRunResult(
                role=role, model=model, status="ok", duration_ms=elapsed,
                tokens_in=tokens_in, tokens_out=tokens_out,
                result=raw_text, raw_payload=_truncate(raw_text),
            )

        # JSON mode — parse and validate
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return OllamaRunResult(
                role=role, model=model, status="json_parse_error", duration_ms=elapsed,
                tokens_in=tokens_in, tokens_out=tokens_out,
                raw_payload=_truncate(raw_text),
                validation_error=str(exc),
            )

        try:
            validated = schema.model_validate(parsed)
        except Exception as exc:
            return OllamaRunResult(
                role=role, model=model, status="validation_error", duration_ms=elapsed,
                tokens_in=tokens_in, tokens_out=tokens_out,
                raw_payload=_truncate(raw_text),
                validation_error=str(exc),
            )

        return OllamaRunResult(
            role=role, model=model, status="ok", duration_ms=elapsed,
            tokens_in=tokens_in, tokens_out=tokens_out,
            result=validated, raw_payload=_truncate(raw_text),
        )

    def _persist_run(
        self,
        result: OllamaRunResult,
        subject_kind: str,
        subject_id: str,
        stage: str,
        role: str,
    ) -> None:
        if self._db_path is None:
            return
        import sqlite3
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                """
                INSERT INTO ollama_model_runs
                    (subject_kind, subject_id, stage, role, model,
                     prompt_version, status, duration_ms, tokens_in, tokens_out,
                     raw_payload, validation_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subject_kind, subject_id, stage, role, result.model,
                    self._prompt_version_for(role), result.status,
                    result.duration_ms, result.tokens_in, result.tokens_out,
                    result.raw_payload, result.validation_error,
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.debug("Failed to persist ollama_model_runs row", exc_info=True)

    def _prompt_version_for(self, role: str) -> str:
        pv = self._config.get("prompt_versions", {}) or {}
        return str(pv.get(role, "v1"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_PAYLOAD_CHARS = 32768  # ~32 KB


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:_MAX_PAYLOAD_CHARS]


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
