"""
Acquisition stage for the autonomous loop.

Connects docket-coverage gap detection to the NCUC portal fetch →
import → bootstrap → extract pipeline.  When corrective actions on
existing data are exhausted, this stage acquires new documents to
sustain the loop.

Requires NCID credentials and Playwright/Chrome for portal access.
Gracefully degrades when auth is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duke_rates.document_intelligence.action_registry import CorrectiveAction
from duke_rates.document_intelligence.idle_work import IDLE_REGISTRY

IDLE_REGISTRY_NAMES = [a.name for a in IDLE_REGISTRY]


def _heartbeat(msg: str) -> None:
    """Print a timestamped progress line to stdout, flushed immediately.

    The autonomous loop has multiple stages that can take 30s-10min
    each (portal smoke test, report builds, ``extract-rates-nc``
    drain, acquisition subprocesses). When the script doesn't emit
    anything during these gaps, an external observer cannot
    distinguish "running" from "frozen" -- and at least one prior
    operator killed the process under that misread. Every blocking
    step in this module emits one of these lines on entry/exit.

    Format is ``[HH:MM:SS] <msg>`` so progress is grep-friendly and
    individual stages are timestamp-comparable.
    """
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _run_subprocess_streaming(
    cmd: list[str],
    *,
    label: str,
    timeout_s: int = 900,
) -> tuple[int, str]:
    """Run a subprocess and stream its stderr to our stdout in real time.

    The standard ``subprocess.run(capture_output=True)`` buffers all
    output until completion, leaving the loop's stdout silent for
    minutes during long subprocesses (drain, post-steps). That gap
    looks like a freeze to external observers.

    This helper uses ``Popen`` to consume the subprocess's stderr line
    by line, prefixing each with the label and our timestamp, while
    accumulating the full stderr for return so error tails are still
    available. Stdout is captured silently (mostly the command's own
    "summary at end" lines that the caller doesn't need).

    Returns (returncode, full_stderr). Raises ``subprocess.TimeoutExpired``
    on timeout (caller's existing handler catches it).
    """
    import threading

    stderr_chunks: list[str] = []
    stderr_lock = threading.Lock()

    def _drain(stream, chunks: list[str]) -> None:
        for line in stream:
            line = line.rstrip()
            if line:
                _heartbeat(f"      {label}: {line[:200]}")
                with stderr_lock:
                    chunks.append(line + "\n")
        try:
            stream.close()
        except Exception:
            pass

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
    )
    t = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks), daemon=True)
    t.start()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
        t.join(timeout=2)
        raise
    t.join(timeout=5)
    full_stderr = "".join(stderr_chunks)
    return proc.returncode, full_stderr


# State persistence (cooldown / stuck counters survive restarts)
DEFAULT_STATE_PATH = Path("data/state/loop_state.json")
DEFAULT_HISTORY_DIR = Path("data/state/loop_history")
STATE_SCHEMA_VERSION = 1


def _load_loop_state(path: Path) -> dict[str, Any]:
    """Load persisted cooldown / stuck-counter state.

    Missing file or unreadable JSON -> empty state. Old schema
    versions are discarded silently rather than crashing the loop.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != STATE_SCHEMA_VERSION:
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        logger.debug("Failed to read loop state at %s", path, exc_info=True)
        return {}


def _save_loop_state(
    path: Path,
    *,
    stuck_counter: dict[str, int],
    cooldown_remaining: dict[str, int],
    last_run_at: str,
    last_outcomes: dict[str, Any] | None = None,
    category_yield: dict[str, float] | None = None,
    exhaustion_counter: dict[str, int] | None = None,
    timeout_counter: dict[str, int] | None = None,
    idle_yield: dict[str, float] | None = None,
) -> None:
    """Atomically persist loop state. Best-effort — never raise."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "stuck_counter": stuck_counter,
            "cooldown_remaining": cooldown_remaining,
            "last_run_at": last_run_at,
            "last_outcomes": last_outcomes or {},
            "category_yield": category_yield or {},
            "exhaustion_counter": exhaustion_counter or {},
            "timeout_counter": timeout_counter or {},
            "idle_yield": idle_yield or {},
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.debug("Failed to write loop state at %s", path, exc_info=True)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------


def check_acquisition_capabilities() -> dict[str, Any]:
    """Check which acquisition stages are available in this environment.

    Authoritative pre-flight for the canonical NCUC portal flow
    (documented in docs/NCUC_PORTAL_WORKING_METHOD.md). All three
    prerequisites must be present:

    1. NCID credentials in ``.env`` (read via ``Settings``, NOT
       ``os.environ`` -- Pydantic BaseSettings reads .env into the
       settings instance but does NOT propagate to ``os.environ``,
       so the previous ``os.environ.get`` check returned False even
       when ``.env`` had valid credentials. This was the bug that
       disabled acquisition for the entire Session 47 run.)

    2. Playwright Python package importable.

    3. Installed Chrome or Edge (NOT bundled Chromium -- the bundled
       browser fails Cloudflare's bot detection and gets HTTP 403).

    Returns a dict of capability flags plus diagnostic detail strings
    suitable for the operator to see exactly which prereq is missing.
    """
    caps: dict[str, Any] = {
        "ncid_auth": False,
        "playwright": False,
        "real_browser": False,
        "portal_resolve": False,
        "portal_fetch": False,
        "local_import": True,
        "local_extract": True,
        "details": {},
    }

    # 1. NCID credentials -- read .env via Settings, not os.environ
    try:
        from duke_rates.config import Settings
        settings = Settings()
        has_user = bool(settings.ncid_username)
        has_pass = bool(settings.ncid_password)
        caps["ncid_auth"] = has_user and has_pass
        caps["details"]["ncid_source"] = (
            ".env" if has_user else "missing"
        )
        caps["details"]["ncid_username_set"] = has_user
        caps["details"]["ncid_password_set"] = has_pass
    except Exception as exc:
        caps["details"]["ncid_error"] = str(exc)

    # 2. Playwright importable
    try:
        import importlib
        importlib.import_module("playwright")
        caps["playwright"] = True
    except ImportError:
        caps["details"]["playwright"] = "not installed (pip install playwright)"

    # 3. Real Chrome or Edge installed (bundled Chromium is blocked by CF)
    try:
        from duke_rates.historical.ncuc.session import _find_chrome
        chrome_path = _find_chrome()
        caps["real_browser"] = chrome_path is not None
        caps["details"]["browser_path"] = chrome_path or "no installed Chrome/Edge found"
    except Exception as exc:
        caps["details"]["browser_error"] = str(exc)

    caps["portal_resolve"] = (
        caps["ncid_auth"] and caps["playwright"] and caps["real_browser"]
    )
    caps["portal_fetch"] = caps["portal_resolve"]

    return caps


def run_portal_smoke_test(timeout_s: int = 120) -> dict[str, Any]:
    """Run the canonical NCUC portal smoke test in-process.

    Mirrors what ``ncuc portal-smoke-test`` does on the CLI but
    returns a structured dict instead of printing. The autonomous
    loop calls this once at startup (when ``--portal-precheck`` is
    on) so we fail fast if the credentials are valid but the live
    portal flow is broken (e.g. NCID password rotated, Cloudflare
    challenge changed, login form selectors moved).

    Returns ``{"ok": bool, "stage": str, "detail": str, "duration_s": float}``
    where ``stage`` identifies where it failed (login / resolve /
    docket_details / inventory) so the operator knows what to fix.

    Never raises -- a smoke-test failure should not crash the loop;
    it should just disable acquisition for the run.
    """
    import time as _time

    t0 = _time.perf_counter()
    out: dict[str, Any] = {
        "ok": False,
        "stage": "init",
        "detail": "",
        "duration_s": 0.0,
        "documents_found": 0,
    }

    try:
        from duke_rates.config import Settings
        from duke_rates.historical.ncuc.session import (
            NcucSessionError,
            close_authenticated_context,
            create_authenticated_context,
            get_docket_documents,
            resolve_docket_ids,
            test_authenticated_access,
        )

        settings = Settings()
        if not settings.ncid_username or not settings.ncid_password:
            out["stage"] = "credentials"
            out["detail"] = "DUKE_RATES_NCID_USERNAME or _PASSWORD missing in .env"
            return out

        # Use a known-good docket as the probe target. E-2, Sub 1354 was
        # validated 2026-04-21 in NCUC_PORTAL_WORKING_METHOD.md.
        probe_docket = "E-2, Sub 1354"

        out["stage"] = "login"
        try:
            pw, ctx, page = create_authenticated_context(settings)
        except NcucSessionError as exc:
            out["detail"] = f"login failed: {exc}"
            return out

        try:
            out["stage"] = "resolve"
            matches = resolve_docket_ids(page, probe_docket)
            if not matches:
                out["detail"] = f"resolve returned 0 matches for {probe_docket}"
                return out

            best = matches[0]
            for m in matches:
                if m.get("match_type") == "exact":
                    best = m
                    break
            docket_id = best.get("docket_id")
            if not docket_id:
                out["detail"] = "resolve returned no docket_id"
                return out

            out["stage"] = "docket_details"
            access = test_authenticated_access(page, docket_id)
            if not access.get("accessible"):
                out["detail"] = (
                    f"DocketDetails inaccessible "
                    f"status={access.get('status_code')} "
                    f"cf_blocked={access.get('cf_blocked')}"
                )
                return out

            out["stage"] = "inventory"
            docs = get_docket_documents(page, docket_id)
            out["documents_found"] = len(docs)
            if not docs:
                out["detail"] = "document inventory returned 0 documents"
                return out

            out["ok"] = True
            out["stage"] = "complete"
            out["detail"] = (
                f"login + resolve + DocketDetails + inventory all succeeded; "
                f"found {len(docs)} documents in {probe_docket}"
            )
        finally:
            try:
                close_authenticated_context(pw, ctx)
            except Exception:
                pass
    except Exception as exc:
        out["detail"] = f"unexpected error in stage {out['stage']}: {exc}"
    finally:
        out["duration_s"] = round(_time.perf_counter() - t0, 1)

    return out


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AcquisitionResult:
    docket_number: str
    action: str
    outcome: str  # acquired | skipped_auth | skipped_no_uuid | failed
    docket_uuid: str | None = None
    docs_discovered: int = 0
    docs_imported: int = 0
    versions_bootstrapped: int = 0
    charges_extracted: int = 0
    error: str | None = None
    duration_ms: int = 0
    # Per-stage return codes + last 300 chars of stderr on non-zero
    # exit. Lets a morning review see "extract-rates crashed in
    # cycle 4 with this stderr" instead of silently treating it as
    # a successful acquisition because the earlier fetch worked.
    stage_outcomes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AcquisitionCycleResult:
    results: list[AcquisitionResult] = field(default_factory=list)
    total_docs_imported: int = 0
    total_charges_added: int = 0
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Acquisition pipeline
# ---------------------------------------------------------------------------


def acquire_dockets(
    recommendations: list[dict[str, Any]],
    *,
    database_path: str,
    max_dockets: int = 2,
    dry_run: bool = True,
    timeout_per_stage_s: int = 300,
    skip_if_no_auth: bool = True,
) -> AcquisitionCycleResult:
    """Run the acquisition pipeline for recommended dockets.

    For each docket with ``recommended_action='fetch'``:
        1. Resolve docket number → NCUC UUID (requires portal auth)
        2. Fetch docket documents with ``--download``
        3. Import via ``ncuc import-pipeline --all-downloaded``
        4. Bootstrap versions via ``bootstrap-missing-versions-nc``
        5. Extract rates via ``extract-rates-nc``

    For dockets with ``recommended_action='import'``:
        Skip directly to steps 3–5 (documents already downloaded).

    Stops after *max_dockets* acquisitions or when actions are exhausted.
    When *dry_run* is True, only reports what would happen.
    """
    caps = check_acquisition_capabilities()
    t0 = time.perf_counter()
    result = AcquisitionCycleResult()

    actionable = [
        r for r in recommendations
        if r.get("recommended_action") in ("fetch", "import")
    ]
    actionable.sort(
        key=lambda r: (
            r.get("discovery_records_count", 0),
            -(r.get("historical_docs_count", 0)),
        ),
        reverse=True,
    )

    # Partition by action up-front so we can skip-fast on fetches when
    # auth is unavailable (the previous implementation attempted each
    # one and ate a 600s timeout per docket -- 14 hours of dead waiting
    # in the Session 47 production run).
    fetch_recs = [r for r in actionable if r.get("recommended_action") == "fetch"]
    import_recs = [r for r in actionable if r.get("recommended_action") == "import"]

    # Skip-fast: if no portal auth (or the run-level smoke test failed),
    # mark every fetch as skipped without spawning a subprocess. Reserves
    # all our timeout budget for the local import lane that doesn't need
    # auth.
    portal_runtime_disabled = bool(
        os.environ.get("DUKE_RATES_PORTAL_DISABLED_THIS_RUN")
    )
    if skip_if_no_auth and (not caps["portal_resolve"] or portal_runtime_disabled):
        reason = (
            "Portal smoke test failed for this run"
            if portal_runtime_disabled
            else "Portal auth not available (NCID + Chrome + Playwright required)"
        )
        for rec in fetch_recs:
            result.results.append(AcquisitionResult(
                docket_number=rec.get("docket_number", ""),
                action="fetch",
                outcome="skipped_auth",
                error=reason,
                docs_discovered=rec.get("discovery_records_count", 0),
            ))
        fetch_recs = []  # don't process them again below

    acquired = 0

    # ── Lane 1: Fetch (portal-bound, one docket at a time) ────────────
    for rec in fetch_recs:
        if acquired >= max_dockets:
            break

        docket = rec.get("docket_number", "")
        disc_count = rec.get("discovery_records_count", 0)

        if dry_run:
            result.results.append(AcquisitionResult(
                docket_number=docket,
                action="fetch",
                outcome="acquired",
                docs_discovered=disc_count,
            ))
            acquired += 1
            continue

        if not caps["portal_resolve"]:
            # Belt-and-braces: we already filtered above, but if
            # skip_if_no_auth=False we can still land here.
            result.results.append(AcquisitionResult(
                docket_number=docket, action="fetch",
                outcome="skipped_auth",
                error="Portal auth not available",
            ))
            continue

        docket_uuid = _resolve_docket_uuid(
            docket,
            timeout_s=min(timeout_per_stage_s, 60),
        )
        if docket_uuid is None:
            result.results.append(AcquisitionResult(
                docket_number=docket, action="fetch",
                outcome="skipped_no_uuid",
                error="Could not resolve docket UUID",
            ))
            continue

        ar = _acquire_one(
            docket_number=docket,
            docket_uuid=docket_uuid,
            action="fetch",
            database_path=database_path,
            timeout_per_stage_s=timeout_per_stage_s,
            run_global_post_steps=False,  # we'll run them once at end
        )
        result.results.append(ar)
        result.total_docs_imported += ar.docs_imported
        result.total_charges_added += ar.charges_extracted
        if ar.error:
            result.errors.append(f"{docket}: {ar.error}")
        if ar.outcome == "acquired":
            acquired += 1

    # ── Lane 2: Import (local-only, runs ONCE for all import-recs) ────
    # Previously each import-recommended docket spawned its own
    # ncuc import-pipeline --all-downloaded -- but that command scans
    # *every* pending download regardless of which docket triggered it.
    # Running it 4x in series is pure waste. Run it once, then count
    # the budget against all import-recs.
    if import_recs and not dry_run:
        global_ar = _run_global_post_steps(
            database_path=database_path,
            timeout_per_stage_s=timeout_per_stage_s,
        )
        # Distribute the global outcome across the import recommendations
        # so the JSON history retains per-docket structure.
        per_docket_imported = (
            global_ar.docs_imported // max(1, len(import_recs))
            if global_ar.docs_imported else 0
        )
        for rec in import_recs[:max_dockets]:
            ar = AcquisitionResult(
                docket_number=rec.get("docket_number", ""),
                action="import",
                outcome=global_ar.outcome,
                docs_discovered=rec.get("discovery_records_count", 0),
                docs_imported=per_docket_imported,
                charges_extracted=0,  # attributed to the global step below
                error=global_ar.error,
                stage_outcomes=list(global_ar.stage_outcomes),
            )
            result.results.append(ar)
            if ar.outcome == "acquired":
                acquired += 1
        result.total_docs_imported = global_ar.docs_imported
        result.total_charges_added = global_ar.charges_extracted
        if global_ar.error:
            result.errors.append(f"global_post_steps: {global_ar.error}")
    elif import_recs and dry_run:
        for rec in import_recs[:max_dockets]:
            result.results.append(AcquisitionResult(
                docket_number=rec.get("docket_number", ""),
                action="import",
                outcome="acquired",
                docs_discovered=rec.get("discovery_records_count", 0),
            ))
            acquired += 1

    result.duration_ms = int((time.perf_counter() - t0) * 1000)
    return result


def acquire_and_cycle(
    database_path: str,
    *,
    limit: int = 50,
    max_dockets: int = 2,
    max_cycles: int = 10,
    max_runtime_minutes: int = 480,
    dry_run: bool = True,
    sleep_between_cycles_s: int = 60,
    action_batch_limit: int | None = None,
    state_path: Path | None = None,
    history_dir: Path | None = None,
    reset_state: bool = False,
    portal_precheck: bool = True,
    include_categories: set[str] | None = None,
    exclude_categories: set[str] | None = None,
    dynamic_routing: bool = True,
) -> dict[str, Any]:
    """Run the full continuous autonomous loop with acquisition.

    Each cycle:
    1. Run database intelligence report (detect)
    2. Run corrective actions (act on existing issues)
    3. If corrective actions yielded no improvement, enter acquisition
    4. Acquire new docket documents → import → bootstrap → extract
    5. Re-run report to measure delta
    6. Sleep and repeat

    Stops when: max_cycles reached, max_runtime exceeded, or no improvement
    across two consecutive cycles.
    """
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.action_registry import decide_actions, run_cycle
    from duke_rates.document_intelligence.database_reports import (
        build_database_intelligence_report,
    )

    t0 = time.perf_counter()
    max_runtime_s = max_runtime_minutes * 60

    history: list[dict[str, Any]] = []
    cycles_without_improvement = 0
    # Per-category memory: how many consecutive cycles produced zero
    # measurable progress for this category. After 3 stuck cycles the
    # category enters cooldown and is skipped for COOLDOWN_DURATION
    # cycles. This prevents the loop from spending time re-running
    # bootstrap-missing-versions-nc against the same 50 unfetchable
    # families every cycle for 8 hours.
    STUCK_THRESHOLD = 3
    COOLDOWN_DURATION = 5

    # Resolve state paths and load persisted cooldown / stuck counters
    # so a restart doesn't re-discover stuck categories from scratch.
    state_file = state_path or DEFAULT_STATE_PATH
    history_d = history_dir or DEFAULT_HISTORY_DIR
    persisted = {} if reset_state else _load_loop_state(state_file)
    stuck_counter: dict[str, int] = dict(persisted.get("stuck_counter") or {})
    cooldown_remaining: dict[str, int] = dict(
        persisted.get("cooldown_remaining") or {}
    )
    # EMA of per-category charge yield across cycles. Persists across
    # runs so the loop "remembers" which categories pay best. Updated
    # at end of each cycle when work attributable to a category produced
    # measurable charge growth. ALPHA controls how fast new data weighs
    # in vs history: 0.3 means 30% new / 70% prior.
    category_yield: dict[str, float] = dict(
        persisted.get("category_yield") or {}
    ) if dynamic_routing else {}
    YIELD_EMA_ALPHA = 0.3
    # M1: per-category consecutive-exhaustion counter. Increments when
    # an action ran successfully (rc=0) but moved 0 items (parsed from
    # stdout). Reset on any successful move. After EXHAUSTION_THRESHOLD
    # consecutive 0-move cycles, the category goes into cooldown.
    exhaustion_counter: dict[str, int] = dict(
        persisted.get("exhaustion_counter") or {}
    )
    EXHAUSTION_THRESHOLD = 2
    # M3: per-category consecutive-timeout counter. Increments on
    # subprocess.TimeoutExpired. After 2, --limit halves; after 3,
    # cooldown like a stuck category.
    timeout_counter: dict[str, int] = dict(
        persisted.get("timeout_counter") or {}
    )
    TIMEOUT_HALVE_THRESHOLD = 2
    TIMEOUT_COOLDOWN_THRESHOLD = 3
    # M2: idle-work yield EMA. Mirrors category_yield but for the
    # idle-work pool (run-overnight-parse-improvement-nc, etc.).
    # Updated when an idle action runs and produces effective_count > 0.
    idle_yield: dict[str, float] = dict(
        persisted.get("idle_yield") or {}
    ) if dynamic_routing else {}
    idle_skip_counter: dict[str, int] = {}  # in-memory: round-robin among zero-yield
    if persisted:
        logger.info(
            "Loaded loop state from %s (stuck=%s cooldown=%s yield=%s)",
            state_file, stuck_counter, cooldown_remaining,
            {k: round(v, 1) for k, v in category_yield.items()},
        )

    # Per-run JSONL checkpoint so a crash doesn't lose history.
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    history_jsonl: Path | None = None
    try:
        history_d.mkdir(parents=True, exist_ok=True)
        history_jsonl = history_d / f"loop_{run_id}.jsonl"
    except OSError:
        logger.debug("Could not create history dir %s", history_d, exc_info=True)
        history_jsonl = None

    sleep_s = sleep_between_cycles_s
    SLEEP_MIN = max(5, min(sleep_between_cycles_s, 30))
    SLEEP_MAX = max(sleep_between_cycles_s * 4, 600)

    # Cache the previous cycle's "after_counts"/"after_outcomes" so we
    # can reuse them as the next cycle's "before" measurement instead
    # of re-running 7 SQL queries. Each report build takes ~10-20s on
    # the production DB; halving the report calls cuts measurement
    # overhead in half.
    cached_before_counts: dict[str, int] | None = None
    cached_before_outcomes: dict[str, Any] | None = None
    cached_before_report: dict[str, Any] | None = None

    # Pre-flight portal smoke test. We trust the static capability
    # check, but it can pass even when the live login is broken
    # (e.g. NCID password rotated, Cloudflare flag changed). Run the
    # canonical smoke test once at startup so we discover this in
    # ~30s instead of after burning 600s timeouts per cycle. If the
    # smoke test fails, downgrade portal_fetch and let the loop
    # continue with corrective actions only.
    # Clear any stale runtime-disabled flag from a prior invocation
    # in this same process (rare in practice, but tests do this).
    os.environ.pop("DUKE_RATES_PORTAL_DISABLED_THIS_RUN", None)

    # Inventory how much fetch-eligible work actually exists. Even
    # when caps['portal_fetch'] is True, the loop has historically
    # produced 0 fetch attempts because the docket-level recommender
    # surfaces "import" for dockets that have ANY downloaded files,
    # masking the record-level fetch need (a docket with 5 failed and
    # 95 successful records gets action=import, hiding the 5). This
    # inventory gives a definitive answer to "does the portal have
    # any work to do at all?"
    fetch_inventory = _inventory_fetch_eligible(database_path)
    logger.info(
        "Fetch inventory: %d records eligible (pending=%d failed=%d requires_browser=%d) "
        "across %d distinct dockets",
        fetch_inventory["total_eligible"],
        fetch_inventory["by_status"].get("pending", 0),
        fetch_inventory["by_status"].get("failed", 0),
        fetch_inventory["by_status"].get("requires_browser", 0),
        fetch_inventory["distinct_dockets"],
    )

    portal_precheck_result: dict[str, Any] | None = None
    caps = check_acquisition_capabilities()
    if portal_precheck and caps.get("portal_fetch") and not dry_run:
        _heartbeat("Running NCUC portal pre-flight smoke test (~30s)...")
        portal_precheck_result = run_portal_smoke_test()
        _heartbeat(
            f"Portal smoke test: {'PASS' if portal_precheck_result['ok'] else 'FAIL'} "
            f"stage={portal_precheck_result['stage']} "
            f"({portal_precheck_result['duration_s']}s)"
        )
        if not portal_precheck_result["ok"]:
            _heartbeat(
                f"  WARNING: smoke test failed -- disabling portal fetch "
                f"for this run. Detail: {portal_precheck_result['detail'][:200]}"
            )
            # Force the loop to skip-fast on fetch recommendations
            os.environ["DUKE_RATES_PORTAL_DISABLED_THIS_RUN"] = "1"

    for cycle_idx in range(1, max_cycles + 1):
        cycle_start = time.perf_counter()
        _heartbeat(
            f"=== Cycle {cycle_idx}/{max_cycles} starting "
            f"(elapsed: {(cycle_start - t0)/60:.1f}m of {max_runtime_minutes}m budget) ==="
        )

        # Runtime check
        elapsed_s = cycle_start - t0
        if elapsed_s >= max_runtime_s:
            _heartbeat(
                f"Max runtime {max_runtime_minutes}m reached after "
                f"{cycle_idx - 1} cycles. Stopping."
            )
            break

        # 1. DETECT (reuses prior cycle's "after" when available)
        if cached_before_counts is not None and cached_before_report is not None:
            _heartbeat(f"  [1/5] DETECT: reusing cached report from prior cycle")
            report = cached_before_report
            before_counts = dict(cached_before_counts)
            before_outcomes = dict(cached_before_outcomes or {})
        else:
            _heartbeat(f"  [1/5] DETECT: building intelligence report (~10-30s)...")
            detect_t0 = time.perf_counter()
            conn = connect(database_path)
            try:
                report = build_database_intelligence_report(conn, limit=limit)
            finally:
                conn.close()
            before_counts = dict(report.get("summary_counts", {}))
            before_outcomes = dict(report.get("outcome_metrics", {}))
            _heartbeat(
                f"        DETECT done in {time.perf_counter() - detect_t0:.1f}s "
                f"({sum(before_counts.values())} total findings)"
            )

        # 2. DECIDE + ACT (corrective on existing data)
        active_cooldown = {
            cat for cat, n in cooldown_remaining.items() if n > 0
        }
        _heartbeat(
            f"  [2/5] DECIDE + ACT: running corrective actions"
            f"{' (cooldown: ' + ','.join(sorted(active_cooldown)) + ')' if active_cooldown else ''}"
        )
        act_t0 = time.perf_counter()
        cycle_result = run_cycle(
            database_path,
            limit=limit,
            max_actions=2,
            dry_run=dry_run,
            action_batch_limit=action_batch_limit,
            cooldown_categories=active_cooldown,
            include_categories=include_categories,
            exclude_categories=exclude_categories,
            category_yield=category_yield if dynamic_routing else None,
            timeout_counter=timeout_counter,
        )
        _heartbeat(
            f"        ACT done in {time.perf_counter() - act_t0:.1f}s "
            f"(taken={len(cycle_result.actions_taken)} skipped={len(cycle_result.actions_skipped)} "
            f"errors={len(cycle_result.errors)})"
        )
        for o in cycle_result.outcomes:
            if o.return_code is not None:
                marker = "OK" if o.success else f"FAIL rc={o.return_code}"
                _heartbeat(
                    f"          [{marker}] {o.cli_command} "
                    f"({o.duration_ms/1000:.1f}s, {o.finding_count} findings)"
                )

        # 3. ACQUISITION
        # Two firing conditions, each independently sufficient:
        #   (a) Corrective actions ran out of work for *every* category
        #       this cycle — fall back to fetching new dockets.
        #   (b) There's a high-value docket lead (lots of discovery
        #       records, ~zero historical docs) — these are unblocked
        #       by acquisition, NOT by corrective actions on existing
        #       data. Run them in parallel with corrective so we don't
        #       spend the whole night re-running dedup while obviously
        #       missing dockets sit unfetched.
        acquisition_result: AcquisitionCycleResult | None = None
        corrective_exhausted = (
            not cycle_result.actions_taken
            and not cycle_result.actions_skipped
        )
        recommendations = _get_docket_recommendations(database_path, limit=5)
        high_value_leads = [
            r for r in recommendations
            if (r.get("discovery_records_count", 0) or 0) >= 10
            and (r.get("historical_docs_count", 0) or 0) == 0
        ]
        should_acquire = corrective_exhausted or bool(high_value_leads)
        acquisition_skip_reason: str | None = None
        if should_acquire and recommendations:
            target = high_value_leads if high_value_leads else recommendations
            _heartbeat(
                f"  [3/5] ACQUISITION: docket-level lane "
                f"(target={len(target)} dockets, max={max_dockets}, "
                f"may take up to {600 * max_dockets}s)..."
            )
            acq_t0 = time.perf_counter()
            acquisition_result = acquire_dockets(
                target,
                database_path=database_path,
                max_dockets=max_dockets,
                dry_run=dry_run,
            )
            _heartbeat(
                f"        ACQUISITION done in {time.perf_counter() - acq_t0:.1f}s "
                f"(acquired={_count_acquired(acquisition_result)} "
                f"docs={acquisition_result.total_docs_imported} "
                f"charges={acquisition_result.total_charges_added})"
            )
        elif not recommendations:
            acquisition_skip_reason = "no docket recommendations available"
            _heartbeat(f"  [3/5] ACQUISITION: skipped -- {acquisition_skip_reason}")
        elif not should_acquire:
            acquisition_skip_reason = (
                "corrective work in progress and no high-value leads "
                f"(need >=10 discovery records and 0 historical docs; "
                f"got {len(recommendations)} recommendations, "
                f"{len(high_value_leads)} high-value)"
            )
            _heartbeat(f"  [3/5] ACQUISITION: skipped -- {acquisition_skip_reason}")

        # Record-level fetch lane.
        # The docket-level recommender hides record-level fetch needs:
        # a docket with 5 failed and 95 successful records gets
        # action=import, never surfacing the 5 failures. The record-
        # level inventory built at startup found these stragglers.
        # Run a small fetch batch each cycle when the portal is healthy
        # and the inventory is non-empty -- this is the work that was
        # never happening before.
        record_level_result: AcquisitionCycleResult | None = None
        portal_runtime_disabled = bool(
            os.environ.get("DUKE_RATES_PORTAL_DISABLED_THIS_RUN")
        )
        if (
            caps.get("portal_fetch")
            and not portal_runtime_disabled
            and (fetch_inventory.get("total_eligible") or 0) > 0
            and max_dockets > 0
        ):
            _heartbeat(
                f"  [3.1/5] RECORD-LEVEL FETCH: "
                f"{fetch_inventory.get('total_eligible', 0)} eligible records "
                f"across {fetch_inventory.get('distinct_dockets', 0)} dockets "
                f"(fetching top {max_dockets})..."
            )
            rlf_t0 = time.perf_counter()
            record_level_result = fetch_record_level_dockets(
                fetch_inventory,
                database_path=database_path,
                max_dockets=max_dockets,
                dry_run=dry_run,
            )
            _heartbeat(
                f"          RECORD-LEVEL FETCH done in {time.perf_counter() - rlf_t0:.1f}s "
                f"(acquired={_count_acquired(record_level_result)} "
                f"docs_imported={record_level_result.total_docs_imported})"
            )
            for r in record_level_result.results:
                _heartbeat(
                    f"            {r.docket_number}: outcome={r.outcome}"
                    f"{' err=' + r.error[:80] if r.error else ''}"
                )
            # Refresh inventory after fetch so the next cycle's
            # decision uses current state.
            if not dry_run and record_level_result.total_docs_imported:
                fetch_inventory = _inventory_fetch_eligible(database_path)
        elif (fetch_inventory.get("total_eligible") or 0) == 0:
            _heartbeat(
                f"  [3.1/5] RECORD-LEVEL FETCH: skipped -- 0 eligible records "
                f"(everything already downloaded)"
            )
        elif portal_runtime_disabled:
            _heartbeat(
                f"  [3.1/5] RECORD-LEVEL FETCH: skipped -- portal disabled "
                f"for this run (smoke test failed)"
            )

        # 3.5. DRAIN — close the loop on redirect-semantic actions.
        #
        # Actions like ``reprocess enqueue-parser-improvement-nc`` are
        # "redirect" semantic: they populate ``historical_reprocess_queue``
        # but their own category in summary_counts doesn't shrink and
        # the outcome metrics (charges, evidence) don't move until
        # something extracts from the queue. Without a drain step the
        # loop sees redirect actions as "no improvement" and stops
        # after 2 cycles.
        #
        # Run extract-rates-nc with a bounded --limit so the drain is
        # comparable in size to whatever was just enqueued. Only fire
        # when at least one redirect action succeeded this cycle.
        drain_result: dict[str, Any] | None = None
        redirect_succeeded = any(
            o.success and o.return_code == 0 and o.delta_semantics == "redirect"
            for o in cycle_result.outcomes
        )
        if redirect_succeeded and not dry_run:
            drain_limit = action_batch_limit or 50
            # 30-minute deadline. Each queue item triggers Ollama
            # embedding + LLM-generate calls; per-item cost is 30s-2min.
            # 50 items at the upper bound is ~100min, so we can't drain
            # to completion in one cycle, but a partial drain still
            # produces measurable charge growth (verified 2026-05-04:
            # +14 charges + 2 versions in a cycle that timed out).
            drain_deadline = 1800
            _heartbeat(
                f"  [4/5] DRAIN: reprocess process-queue-nc --limit {drain_limit} "
                f"(consumes the queue our redirect actions just populated, deadline {drain_deadline}s)..."
            )
            drain_t0 = time.perf_counter()
            try:
                # Use `reprocess process-queue-nc`, NOT extract-rates-nc.
                #
                # The previous implementation called ``extract-rates-nc
                # --limit 250`` -- but that walks the extractor's default
                # scan order, which is the SAME 250 docs every cycle. So
                # cycle 2's drain re-extracted exactly what cycle 1 had
                # already done, producing 0 net charge growth and making
                # the loop look "stuck". Other-agent feedback observed
                # OL doc 2525 extracted 8+ times in one drain pass.
                #
                # `reprocess process-queue-nc` consumes from the actual
                # ``historical_reprocess_queue`` table -- the queue the
                # redirect-action just enqueued. Each item is dequeued
                # on success, so we never re-process the same item
                # twice. This is the closing step the loop has been
                # missing.
                drain_rc, drain_stderr = _run_subprocess_streaming(
                    [
                        sys.executable, "-m", "duke_rates",
                        "reprocess", "process-queue-nc",
                        "--limit", str(drain_limit),
                        "--workers", "2",
                    ],
                    label="drain",
                    timeout_s=drain_deadline,
                )
                drain_result = {
                    "command": "reprocess process-queue-nc",
                    "limit": drain_limit,
                    "return_code": drain_rc,
                    "success": drain_rc == 0,
                    "duration_ms": int((time.perf_counter() - drain_t0) * 1000),
                    "stderr_tail": drain_stderr[-300:] if drain_rc != 0 else "",
                }
                _heartbeat(
                    f"        DRAIN done in {time.perf_counter() - drain_t0:.1f}s "
                    f"(rc={drain_rc})"
                )
                if drain_rc != 0:
                    logger.warning(
                        "Drain stage `reprocess process-queue-nc` exited %d: %s",
                        drain_rc, drain_result["stderr_tail"][:200],
                    )
            except subprocess.TimeoutExpired:
                # Partial drain is still progress -- the queue was
                # consumed for as long as we waited and any items
                # that completed before the kill are persisted in
                # tariff_charges. Don't treat timeout as a hard
                # failure; the next cycle's measurement will reflect
                # whatever did finish.
                drain_result = {
                    "command": "reprocess process-queue-nc",
                    "limit": drain_limit,
                    "return_code": None,
                    "success": False,
                    "partial_progress": True,
                    "duration_ms": int((time.perf_counter() - drain_t0) * 1000),
                    "error": f"timeout ({drain_deadline}s) -- partial drain, see outcome_delta",
                }
                _heartbeat(
                    f"        DRAIN timed out at {drain_deadline}s -- "
                    f"partial drain. Outcome metrics will reflect "
                    f"whatever items completed before the kill."
                )
            except Exception as exc:
                drain_result = {
                    "command": "reprocess process-queue-nc",
                    "limit": drain_limit,
                    "return_code": None,
                    "success": False,
                    "duration_ms": int((time.perf_counter() - drain_t0) * 1000),
                    "error": str(exc),
                }

        # 3.6. POST-BOOTSTRAP EXTRACT — close the loop on bootstrap actions.
        #
        # `bootstrap-missing-versions-nc` creates new tariff_version rows
        # but does NOT enqueue them in historical_reprocess_queue, so the
        # preceding drain step (which consumes that queue) doesn't help.
        # Without this step, bootstrap closes the missing_versions gap
        # but the new versions stay at 0 charges indefinitely. Fire
        # extract-rates-nc with a bounded --limit so newly-linked
        # versions get charges materialized this cycle.
        extract_drain_result: dict[str, Any] | None = None
        bootstrap_succeeded = any(
            o.success and o.return_code == 0
            and o.cli_command == "bootstrap-missing-versions-nc"
            for o in cycle_result.outcomes
        )
        if bootstrap_succeeded and not dry_run:
            extract_limit = action_batch_limit or 200
            extract_deadline = 1800
            _heartbeat(
                f"  [4.5/5] EXTRACT: extract-rates-nc --limit {extract_limit} "
                f"(materializes charges for versions bootstrap just created, "
                f"deadline {extract_deadline}s)..."
            )
            extract_t0 = time.perf_counter()
            try:
                extract_rc, extract_stderr = _run_subprocess_streaming(
                    [
                        sys.executable, "-m", "duke_rates",
                        "extract-rates-nc",
                        "--limit", str(extract_limit),
                        "--progress",
                    ],
                    label="extract",
                    timeout_s=extract_deadline,
                )
                extract_drain_result = {
                    "command": "extract-rates-nc",
                    "limit": extract_limit,
                    "return_code": extract_rc,
                    "success": extract_rc == 0,
                    "duration_ms": int((time.perf_counter() - extract_t0) * 1000),
                    "stderr_tail": extract_stderr[-300:] if extract_rc != 0 else "",
                }
                _heartbeat(
                    f"        EXTRACT done in {time.perf_counter() - extract_t0:.1f}s "
                    f"(rc={extract_rc})"
                )
                if extract_rc != 0:
                    logger.warning(
                        "Post-bootstrap extract `extract-rates-nc` exited %d: %s",
                        extract_rc, extract_drain_result["stderr_tail"][:200],
                    )
            except subprocess.TimeoutExpired:
                extract_drain_result = {
                    "command": "extract-rates-nc",
                    "limit": extract_limit,
                    "return_code": None,
                    "success": False,
                    "partial_progress": True,
                    "duration_ms": int((time.perf_counter() - extract_t0) * 1000),
                    "error": f"timeout ({extract_deadline}s) -- partial extract",
                }
                _heartbeat(
                    f"        EXTRACT timed out at {extract_deadline}s -- "
                    f"partial extract. Outcome metrics will reflect partial progress."
                )
            except Exception as exc:
                extract_drain_result = {
                    "command": "extract-rates-nc",
                    "limit": extract_limit,
                    "return_code": None,
                    "success": False,
                    "duration_ms": int((time.perf_counter() - extract_t0) * 1000),
                    "error": str(exc),
                }

        # 3.7. IDLE WORK — productive LLM/maintenance fallback.
        #
        # When corrective actions all reported effective_count=0 (or
        # didn't run), AND acquisition imported nothing, AND drain
        # made no progress, run one idle-work action instead of
        # letting the cycle be wasted. Idle actions are self-bounded
        # (own --limit), independent of the reprocess queue, and
        # produce future-cycle benefits (parser fixes) or direct
        # charge growth (LLM promotion).
        idle_result: dict[str, Any] | None = None
        corrective_did_real_work = any(
            o.success and o.effective_count and o.effective_count > 0
            for o in cycle_result.outcomes
        )
        # Acquisition counts as real work only when it actually
        # imported docs or added charges (dry-run results don't).
        acquisition_did_real_work_so_far = bool(
            (acquisition_result and (
                acquisition_result.total_docs_imported > 0
                or acquisition_result.total_charges_added > 0
            ))
            or (record_level_result and record_level_result.total_docs_imported > 0)
        )
        drain_did_real_work = bool(
            (drain_result and drain_result.get("success"))
            or (extract_drain_result and extract_drain_result.get("success"))
        )
        cycle_was_unproductive = (
            not corrective_did_real_work
            and not acquisition_did_real_work_so_far
            and not drain_did_real_work
        )
        if cycle_was_unproductive and not dry_run:
            from duke_rates.document_intelligence.idle_work import (
                select_idle_action,
                run_idle_action,
            )
            idle_action = select_idle_action(
                database_path=database_path,
                idle_yield=idle_yield,
                consecutive_skipped=idle_skip_counter,
            )
            if idle_action is not None:
                _heartbeat(
                    f"  [3.7/5] IDLE WORK: corrective cycle unproductive "
                    f"-- running {idle_action.name} ({idle_action.description[:60]})"
                )
                idle_result = run_idle_action(
                    idle_action,
                    dry_run=False,
                    heartbeat=_heartbeat,
                )
                # Track skips of OTHER idle actions for round-robin
                for other in IDLE_REGISTRY_NAMES:
                    if other != idle_action.name:
                        idle_skip_counter[other] = idle_skip_counter.get(other, 0) + 1
                # Reset this action's skip counter since we ran it
                idle_skip_counter[idle_action.name] = 0
            else:
                _heartbeat(
                    f"  [3.7/5] IDLE WORK: skipped -- no eligible idle "
                    f"actions (all pools empty)"
                )

        # 4. MEASURE — only re-query when work was attempted. If
        # nothing ran (skipped / dry-run / no findings), the "after"
        # state is identical to the "before" we just measured. Saves
        # the second 10-20s report build on no-op cycles.
        after_counts: dict[str, int] = {}
        after_outcomes: dict[str, Any] = {}
        any_work = (
            bool(cycle_result.actions_taken)
            or bool(
                acquisition_result and any(
                    r.outcome == "acquired" and (
                        r.docs_imported > 0 or r.charges_extracted > 0
                    ) for r in acquisition_result.results
                )
            )
            or bool(drain_result and drain_result.get("success"))
            or bool(extract_drain_result and extract_drain_result.get("success"))
            or bool(
                record_level_result and record_level_result.total_docs_imported > 0
            )
            or bool(idle_result and idle_result.get("success"))
        )
        if any_work:
            _heartbeat(f"  [5/5] MEASURE: re-running intelligence report (~10-30s)...")
            measure_t0 = time.perf_counter()
            conn = connect(database_path)
            try:
                after_report = build_database_intelligence_report(conn, limit=limit)
                after_counts = dict(after_report.get("summary_counts", {}))
                after_outcomes = dict(after_report.get("outcome_metrics", {}))
            finally:
                conn.close()
            _heartbeat(
                f"        MEASURE done in {time.perf_counter() - measure_t0:.1f}s "
                f"(charges={after_outcomes.get('tariff_charges_total', 0)} "
                f"coverage={after_outcomes.get('extraction_coverage_pct', 0)}%)"
            )
        else:
            _heartbeat(f"  [5/5] MEASURE: skipped -- no work attempted this cycle")
            after_report = report
            after_counts = dict(before_counts)
            after_outcomes = dict(before_outcomes)

        # Cache for the next cycle so we don't re-build the same report
        cached_before_counts = dict(after_counts)
        cached_before_outcomes = dict(after_outcomes)
        cached_before_report = after_report

        # Per-category deltas (positive = backlog shrank for that category)
        per_category_delta = {
            k: before_counts.get(k, 0) - after_counts.get(k, 0)
            for k in set(before_counts) | set(after_counts)
        }
        improved_categories = [
            k for k, d in per_category_delta.items() if d > 0
        ]
        # Total delta retained for reporting only — it is NOT the
        # improvement signal, since one category growing can mask
        # another shrinking.
        total_before = sum(before_counts.values())
        total_after = sum(after_counts.values())
        delta = total_before - total_after

        # Did this cycle actually attempt any real work?
        # Important: dry-run acquisition fills ``results`` with
        # ``outcome="acquired"`` entries that did nothing. Only count
        # acquisition as work if it actually imported docs OR added
        # charges — i.e. the underlying subprocesses really ran.
        acquisition_did_work = bool(
            acquisition_result and (
                acquisition_result.total_docs_imported > 0
                or acquisition_result.total_charges_added > 0
            )
        )
        work_attempted = bool(
            cycle_result.actions_taken or acquisition_did_work
        )
        # Outcome-metric deltas. These are the numbers the user
        # actually cares about (charges in DB, version coverage,
        # evidence coverage). They can move even when summary_counts
        # don't — e.g. dedup may not shrink the duplicate-group count
        # but consolidates charges under survivors.
        outcome_keys = (
            "tariff_charges_total",
            "versions_with_charges",
            "docs_with_evidence",
        )
        outcome_delta = {
            k: (after_outcomes.get(k) or 0) - (before_outcomes.get(k) or 0)
            for k in outcome_keys
        }
        improved_outcomes = [k for k, d in outcome_delta.items() if d > 0]

        improvement_observed = (
            bool(improved_categories)
            or bool(improved_outcomes)
            or bool(
                acquisition_result and acquisition_result.total_docs_imported > 0
            )
        )

        # Update per-category stuck counters.
        #
        # "Stuck" means: action ran successfully, but its category did
        # not move in the expected direction.
        #   - drain semantics: count must DECREASE on success (cat_delta > 0
        #     in our before-after = positive convention)
        #   - schedule semantics: count is allowed to grow (enqueue-* adds
        #     items to the reprocess queue, which is correct behavior).
        #     For these, "stuck" means the count did not GROW.
        #   - neutral semantics: read-only commands; never count as stuck.
        #
        # Critically we look ONLY at this category's count -- NOT at the
        # cycle's overall improvement. Otherwise random ±1 noise in an
        # unrelated category keeps resetting every other category's
        # stuck counter and the loop runs useless actions for hours.
        # That was the Session 47 bug.
        succeeded_outcomes = [
            o for o in cycle_result.outcomes
            if o.success and o.return_code == 0
        ]
        for o in succeeded_outcomes:
            cat = o.category
            cat_delta = per_category_delta.get(cat, 0)
            if o.delta_semantics in ("neutral", "redirect"):
                # neutral: read-only commands; redirect: enqueue-*
                # commands whose own category isn't directly affected.
                # Effect shows up downstream via outcome metrics;
                # don't increment per-category stuck counter.
                continue
            else:  # "drain" (default)
                # Drain: success means the count SHRANK (after < before
                # -> per_category_delta > 0).
                made_progress = cat_delta > 0

            if not made_progress:
                stuck_counter[cat] = stuck_counter.get(cat, 0) + 1
                if stuck_counter[cat] >= STUCK_THRESHOLD:
                    cooldown_remaining[cat] = COOLDOWN_DURATION
                    stuck_counter[cat] = 0
                    logger.info(
                        "Category %r stuck after %d cycles "
                        "(semantics=%s, delta=%d) -- cooldown for %d cycles",
                        cat, STUCK_THRESHOLD, o.delta_semantics,
                        cat_delta, COOLDOWN_DURATION,
                    )
            else:
                stuck_counter[cat] = 0

        # M1: exhaustion detection. For each action whose stdout
        # parsed to a concrete effective_count, increment or reset
        # the per-category counter. effective_count=0 with rc=0 means
        # the action ran successfully but moved nothing — typically
        # the candidate pool is drained for this run.
        for o in cycle_result.outcomes:
            if o.effective_count is None:
                continue  # can't tell — leave counter alone
            cat = o.category
            if o.success and o.effective_count == 0:
                exhaustion_counter[cat] = exhaustion_counter.get(cat, 0) + 1
                if exhaustion_counter[cat] >= EXHAUSTION_THRESHOLD:
                    cooldown_remaining[cat] = COOLDOWN_DURATION
                    exhaustion_counter[cat] = 0
                    logger.info(
                        "Category %r exhausted (effective_count=0 for "
                        "%d cycles) -- cooldown for %d cycles",
                        cat, EXHAUSTION_THRESHOLD, COOLDOWN_DURATION,
                    )
            elif o.effective_count > 0:
                exhaustion_counter[cat] = 0

        # M3: timeout detection. Track per-category consecutive
        # timeouts. At 2, halve --limit next time. At 3, cooldown.
        for o in cycle_result.outcomes:
            cat = o.category
            if o.timed_out:
                timeout_counter[cat] = timeout_counter.get(cat, 0) + 1
                if timeout_counter[cat] >= TIMEOUT_COOLDOWN_THRESHOLD:
                    cooldown_remaining[cat] = COOLDOWN_DURATION
                    timeout_counter[cat] = 0
                    logger.info(
                        "Category %r timed out %d times consecutively "
                        "-- cooldown for %d cycles",
                        cat, TIMEOUT_COOLDOWN_THRESHOLD, COOLDOWN_DURATION,
                    )
            elif o.success:
                timeout_counter[cat] = 0

        # Decay existing cooldown counters by 1 cycle
        cooldown_remaining = {
            cat: n - 1 for cat, n in cooldown_remaining.items() if n - 1 > 0
        }

        # Update per-category yield EMA. Attribute this cycle's
        # charge delta to whatever categories had successful actions,
        # weighted by their finding_count share. Skip when no charge
        # growth (yield stays at prior EMA) or no successful actions.
        if dynamic_routing:
            cycle_charge_delta = outcome_delta.get("tariff_charges_total", 0) or 0
            if cycle_charge_delta > 0 and succeeded_outcomes:
                total_findings = sum(
                    o.finding_count for o in succeeded_outcomes
                ) or 1
                for o in succeeded_outcomes:
                    share = o.finding_count / total_findings
                    cat_yield_observation = cycle_charge_delta * share
                    prior = category_yield.get(o.category, 0.0)
                    category_yield[o.category] = (
                        YIELD_EMA_ALPHA * cat_yield_observation
                        + (1.0 - YIELD_EMA_ALPHA) * prior
                    )

            # M2: idle-work yield EMA. When an idle action ran AND
            # the cycle saw charge growth that wasn't attributable
            # to corrective actions, attribute to idle. Use the
            # action's own effective_count as a secondary signal.
            if idle_result and idle_result.get("success"):
                idle_name = idle_result.get("name") or "unknown"
                # Charge growth not attributable to corrective work
                idle_attributable_delta = (
                    cycle_charge_delta if not corrective_did_real_work else 0
                )
                ec = idle_result.get("effective_count") or 0
                # Combine: prefer charge_delta (direct signal); fall
                # back to effective_count (e.g., proposals staged)
                observation = float(
                    idle_attributable_delta if idle_attributable_delta > 0 else ec
                )
                prior = idle_yield.get(idle_name, 0.0)
                idle_yield[idle_name] = (
                    YIELD_EMA_ALPHA * observation
                    + (1.0 - YIELD_EMA_ALPHA) * prior
                )

        # Adaptive sleep target for the *next* cycle. Three signals:
        #   - Acquisition ran -> portal politeness floor (>=60s).
        #   - Improvement observed -> halve sleep (more to drain).
        #   - Work attempted but no improvement -> double sleep
        #     (backoff so external state can settle / avoid spin).
        #   - No work at all -> next cycle will short-circuit anyway,
        #     leave the sleep value alone.
        next_sleep = sleep_s
        if acquisition_result and any(
            r.outcome == "acquired" for r in acquisition_result.results
        ):
            next_sleep = max(next_sleep, 60)
        if improvement_observed:
            next_sleep = max(SLEEP_MIN, next_sleep // 2)
        elif work_attempted:
            next_sleep = min(SLEEP_MAX, max(next_sleep * 2, SLEEP_MIN))

        cycle_entry = {
            "cycle": cycle_idx,
            "before_counts": before_counts,
            "after_counts": after_counts,
            "delta": delta,
            "per_category_delta": per_category_delta,
            "improved_categories": improved_categories,
            "before_outcomes": before_outcomes,
            "after_outcomes": after_outcomes,
            "outcome_delta": outcome_delta,
            "improved_outcomes": improved_outcomes,
            "work_attempted": work_attempted,
            "improvement_observed": improvement_observed,
            "stuck_counter": dict(stuck_counter),
            "cooldown_remaining": dict(cooldown_remaining),
            "active_cooldown": sorted(active_cooldown),
            "category_yield": {k: round(v, 2) for k, v in category_yield.items()},
            "exhaustion_counter": dict(exhaustion_counter),
            "timeout_counter": dict(timeout_counter),
            "idle_yield": {k: round(v, 2) for k, v in idle_yield.items()},
            "sleep_s_used": sleep_s,
            "sleep_s_next": next_sleep,
            "corrective_actions": cycle_result.actions_taken,
            "corrective_skipped": cycle_result.actions_skipped,
            "corrective_exhausted": corrective_exhausted,
            "corrective_outcomes": [
                {
                    "category": o.category,
                    "command": o.cli_command,
                    "args": o.args,
                    "finding_count": o.finding_count,
                    "return_code": o.return_code,
                    "success": o.success,
                    "duration_ms": o.duration_ms,
                    "stderr_tail": o.stderr_tail,
                    "error": o.error,
                    "delta_semantics": o.delta_semantics,
                    "effective_count": o.effective_count,
                    "timed_out": o.timed_out,
                }
                for o in cycle_result.outcomes
            ],
            "corrective_errors": cycle_result.errors,
            "drain": drain_result,
            "extract_drain": extract_drain_result,
            "idle_work": idle_result,
            "acquisition_skip_reason": acquisition_skip_reason,
            "fetch_inventory": fetch_inventory,
            "record_level_fetch": (
                {
                    "dockets_acquired": _count_acquired(record_level_result),
                    "docs_imported": record_level_result.total_docs_imported,
                    "results": [
                        {
                            "docket": r.docket_number,
                            "outcome": r.outcome,
                            "docs_imported": r.docs_imported,
                            "error": r.error,
                            "stage_outcomes": r.stage_outcomes,
                        }
                        for r in record_level_result.results
                    ],
                } if record_level_result else None
            ),
            "acquisition": {
                "dockets_acquired": _count_acquired(acquisition_result),
                "docs_imported": acquisition_result.total_docs_imported if acquisition_result else 0,
                "charges_added": acquisition_result.total_charges_added if acquisition_result else 0,
                "results": [
                    {
                        "docket": r.docket_number,
                        "outcome": r.outcome,
                        "docs_imported": r.docs_imported,
                        "charges_extracted": r.charges_extracted,
                        "error": r.error,
                        "stage_outcomes": r.stage_outcomes,
                    }
                    for r in acquisition_result.results
                ],
            } if acquisition_result else None,
            "duration_ms": int((time.perf_counter() - cycle_start) * 1000),
        }
        history.append(cycle_entry)
        # Per-cycle outcome line, mirrored to stdout, so the cycle's
        # measurable result is visible alongside the stage heartbeats.
        chg = outcome_delta.get("tariff_charges_total", 0)
        vch = outcome_delta.get("versions_with_charges", 0)
        _heartbeat(
            f"  Cycle {cycle_idx} done: dur={(time.perf_counter() - cycle_start):.1f}s "
            f"work={work_attempted} improvement={improvement_observed} "
            f"+{chg} charges +{vch} versions "
            f"next_sleep={next_sleep}s"
        )

        # Checkpoint each cycle to JSONL so a mid-run crash doesn't
        # lose the prior 26 cycles of work. Also flush the
        # stuck/cooldown state file so a restart resumes correctly.
        if history_jsonl is not None:
            try:
                with history_jsonl.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(cycle_entry, default=str) + "\n")
            except OSError:
                logger.debug("Failed to append history JSONL", exc_info=True)
        _save_loop_state(
            state_file,
            stuck_counter=stuck_counter,
            cooldown_remaining=cooldown_remaining,
            last_run_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            last_outcomes=after_outcomes,
            category_yield=category_yield,
            exhaustion_counter=exhaustion_counter,
            timeout_counter=timeout_counter,
            idle_yield=idle_yield,
        )

        sleep_s = next_sleep

        # No-improvement budget. We only count a cycle "wasted" when
        # work was attempted AND nothing measurable moved.
        #
        # Cycles where the only successful actions were ``redirect``
        # semantic (enqueue-*) are tolerated longer: they intentionally
        # don't shrink summary_counts in their own category, and the
        # drain step's measurable effect (charge growth) is sometimes
        # lagged because OCR / Docling / parser improvements take time
        # to land. Two consecutive enqueue-only no-progress cycles is
        # not enough evidence to give up.
        only_redirect_acted = bool(cycle_result.outcomes) and all(
            (not o.success) or o.delta_semantics == "redirect"
            for o in cycle_result.outcomes
        )
        # Bumped 2 -> 3 (and 4 -> 5 for redirect-only) on 2026-05-24.
        # The 2026-05-23 overnight stopped at cycle 4 after 2 zero-
        # delta cycles, when the underlying problem was the MEASURE
        # under-count bug (now fixed) AND queue-dedup making cycles
        # 3+4 actually enqueue 0 items. With the metric fix giving
        # us truer signal, an extra cycle of tolerance lets the loop
        # try acquisition / a different category before stopping.
        no_improvement_threshold = 5 if only_redirect_acted else 3

        if work_attempted and not improvement_observed:
            cycles_without_improvement += 1
        elif improvement_observed:
            cycles_without_improvement = 0
        # else: no work attempted -- counter unchanged

        if cycles_without_improvement >= no_improvement_threshold:
            logger.info(
                "No improvement after %d cycles where work was attempted "
                "(threshold=%d, only_redirect=%s). Stopping.",
                cycles_without_improvement, no_improvement_threshold,
                only_redirect_acted,
            )
            break

        # If no work could be attempted at all (dry-run + no auth, no
        # findings), there is no point continuing — stop after the
        # first such cycle so we don't burn runtime.
        if not work_attempted and cycle_idx >= 1:
            logger.info(
                "Cycle %d attempted no work (dry-run/no-auth/no-findings). Stopping.",
                cycle_idx,
            )
            break

        # Sleep between cycles (skip if this is the last cycle or dry run).
        # Chunk the sleep into 30s segments and emit a heartbeat each one
        # so observers can see "still alive, sleeping" instead of silence.
        if dry_run:
            pass  # never sleep in dry-run mode
        elif cycle_idx < max_cycles and cycles_without_improvement < 2:
            remaining_s = max_runtime_s - (time.perf_counter() - t0)
            if remaining_s > sleep_s:
                _heartbeat(
                    f"  Sleeping {sleep_s}s before cycle {cycle_idx + 1}..."
                )
                slept = 0
                CHUNK = 30
                while slept < sleep_s:
                    chunk = min(CHUNK, sleep_s - slept)
                    time.sleep(chunk)
                    slept += chunk
                    if slept < sleep_s:
                        _heartbeat(
                            f"    sleeping... ({slept}/{sleep_s}s elapsed, "
                            f"{sleep_s - slept}s remaining)"
                        )

    last_attempted = history[-1].get("work_attempted") if history else None
    stopped_reason: str
    if cycles_without_improvement >= 2:
        stopped_reason = "no_improvement"
    elif (time.perf_counter() - t0) >= max_runtime_s:
        stopped_reason = "max_runtime"
    elif history and last_attempted is False:
        stopped_reason = "no_work_possible"
    else:
        stopped_reason = "max_cycles"

    return {
        "cycles_completed": len(history),
        "total_duration_ms": int((time.perf_counter() - t0) * 1000),
        "stopped_reason": stopped_reason,
        "run_id": run_id,
        "state_path": str(state_file),
        "history_jsonl": str(history_jsonl) if history_jsonl else None,
        "loaded_state": bool(persisted),
        "final_stuck_counter": dict(stuck_counter),
        "final_cooldown_remaining": dict(cooldown_remaining),
        "final_category_yield": {k: round(v, 2) for k, v in category_yield.items()},
        "final_exhaustion_counter": dict(exhaustion_counter),
        "final_timeout_counter": dict(timeout_counter),
        "final_idle_yield": {k: round(v, 2) for k, v in idle_yield.items()},
        "portal_precheck": portal_precheck_result,
        "capabilities": caps,
        "history": history,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_docket_uuid(docket_number: str, timeout_s: int = 120) -> str | None:
    """Resolve a human-readable docket number to an NCUC DocketId GUID."""
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "duke_rates",
                "ncuc", "resolve-docket-ids",
                "--docket-number", docket_number,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            logger.warning("ncuc resolve-docket-ids failed: %s", proc.stderr[:500])
            return None

        # Parse output like: "E-2, Sub 1354: abc123-def456...  /docket/..."
        for line in proc.stdout.splitlines():
            if docket_number in line and ":" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    uuid_candidate = parts[1].strip().split()[0]
                    if "-" in uuid_candidate and len(uuid_candidate) > 20:
                        return uuid_candidate
        return None
    except subprocess.TimeoutExpired:
        logger.warning("ncuc resolve-docket-ids timed out for %s", docket_number)
        return None
    except Exception:
        logger.debug("Failed to resolve docket UUID", exc_info=True)
        return None


def _acquire_one(
    *,
    docket_number: str,
    docket_uuid: str | None,
    action: str,
    database_path: str,
    timeout_per_stage_s: int = 300,
    run_global_post_steps: bool = True,
) -> AcquisitionResult:
    """Execute the full acquisition pipeline for one docket.

    When ``run_global_post_steps=False``, the per-docket fetch
    runs but the global import/bootstrap/extract are deferred to
    ``_run_global_post_steps`` so the loop can run them once across
    all dockets in a cycle instead of N times.
    """
    t0 = time.perf_counter()
    ar = AcquisitionResult(
        docket_number=docket_number,
        action=action,
        outcome="acquired",
        docket_uuid=docket_uuid,
    )

    def _record_stage(stage: str, proc: subprocess.CompletedProcess) -> bool:
        """Append per-stage outcome to ar; return True iff stage succeeded."""
        entry: dict[str, Any] = {
            "stage": stage,
            "return_code": proc.returncode,
            "success": proc.returncode == 0,
        }
        if proc.returncode != 0:
            entry["stderr_tail"] = (proc.stderr or "")[-300:]
        ar.stage_outcomes.append(entry)
        return proc.returncode == 0

    try:
        # Step 1: Fetch (if needed)
        if action == "fetch" and docket_uuid:
            _heartbeat(
                f"      ncuc docket-fetch {docket_number} (deadline {timeout_per_stage_s}s)..."
            )
            stage_t0 = time.perf_counter()
            proc = subprocess.run(
                [
                    sys.executable, "-m", "duke_rates",
                    "ncuc", "docket-fetch", docket_uuid,
                    "--docket-number", docket_number,
                    "--download",
                ],
                capture_output=True,
                text=True,
                timeout=timeout_per_stage_s,
                check=False,
            )
            _heartbeat(
                f"        fetch done in {time.perf_counter() - stage_t0:.1f}s rc={proc.returncode}"
            )
            if not _record_stage("fetch", proc):
                ar.outcome = "failed"
                ar.error = f"ncuc docket-fetch failed: {proc.stderr[:300]}"
                return ar
            # Count discovered docs from stdout
            for line in proc.stdout.splitlines():
                if "Found" in line and "documents" in line:
                    try:
                        ar.docs_discovered = int(line.split()[1])
                    except (ValueError, IndexError):
                        pass

        # Steps 2-4 (import, bootstrap, extract) are deferred to
        # _run_global_post_steps when run_global_post_steps=False so
        # the loop runs them once across all dockets per cycle.
        if run_global_post_steps:
            global_ar = _run_global_post_steps(
                database_path=database_path,
                timeout_per_stage_s=timeout_per_stage_s,
            )
            ar.stage_outcomes.extend(global_ar.stage_outcomes)
            ar.docs_imported += global_ar.docs_imported
            ar.charges_extracted += global_ar.charges_extracted
            if global_ar.error:
                ar.error = (
                    f"{ar.error}; " if ar.error else ""
                ) + global_ar.error

        ar.duration_ms = int((time.perf_counter() - t0) * 1000)
    except subprocess.TimeoutExpired:
        ar.outcome = "failed"
        ar.error = "Timeout during acquisition"
        ar.duration_ms = int((time.perf_counter() - t0) * 1000)
    except Exception as exc:
        ar.outcome = "failed"
        ar.error = str(exc)
        ar.duration_ms = int((time.perf_counter() - t0) * 1000)

    return ar


def _run_global_post_steps(
    *,
    database_path: str,
    timeout_per_stage_s: int = 300,
) -> AcquisitionResult:
    """Run import -> bootstrap -> extract once for the whole cycle.

    These three commands operate on the entire pending queue, not on
    a specific docket -- so running them once amortizes their cost
    across however many dockets the cycle is acquiring. The previous
    per-docket loop ran each command N times against the same data,
    which is what burned 50,000+ seconds in the Session 47 run.

    The aggressive per-stage timeouts (300s default) prevent any one
    hung stage from blocking the whole cycle.
    """
    t0 = time.perf_counter()
    ar = AcquisitionResult(
        docket_number="<global>",
        action="post_steps",
        outcome="acquired",
    )

    def _record(stage: str, proc: subprocess.CompletedProcess) -> bool:
        entry: dict[str, Any] = {
            "stage": stage,
            "return_code": proc.returncode,
            "success": proc.returncode == 0,
            "duration_ms": getattr(proc, "_duration_ms", None),
        }
        if proc.returncode != 0:
            entry["stderr_tail"] = (proc.stderr or "")[-300:]
        ar.stage_outcomes.append(entry)
        return proc.returncode == 0

    def _run(cmd_args: list[str], timeout_s: int, label: str) -> subprocess.CompletedProcess:
        _heartbeat(
            f"      post-steps/{label}: {' '.join(cmd_args)} (deadline {timeout_s}s)..."
        )
        sub_t0 = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-m", "duke_rates", *cmd_args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        proc._duration_ms = int((time.perf_counter() - sub_t0) * 1000)  # type: ignore[attr-defined]
        _heartbeat(
            f"        post-steps/{label} done in {time.perf_counter() - sub_t0:.1f}s rc={proc.returncode}"
        )
        return proc

    try:
        # Import: scans every "downloaded" NCUC discovery record and
        # imports it into the historical pipeline. With 4,000+ records
        # this can take 10-25 minutes on a cold cache. Give it a
        # 30-minute deadline (vs the 5min default) so a single oversized
        # corpus doesn't perpetually cause "Timeout in global post-steps".
        # Other-agent feedback (2026-05-04) misread this timeout as
        # "Playwright not installed".
        import_timeout = max(timeout_per_stage_s, 1800)
        proc = _run(
            ["ncuc", "import-pipeline", "--all-downloaded"],
            import_timeout, "import",
        )
        if not _record("import", proc):
            ar.outcome = "failed"
            ar.error = f"ncuc import-pipeline rc={proc.returncode}"
            ar.duration_ms = int((time.perf_counter() - t0) * 1000)
            return ar

        # Bootstrap is fast: 60s ceiling
        proc = _run(["bootstrap-missing-versions-nc"], min(timeout_per_stage_s, 60), "bootstrap")
        if not _record("bootstrap", proc):
            ar.outcome = "failed"
            ar.error = f"bootstrap-missing-versions-nc rc={proc.returncode}"
            ar.duration_ms = int((time.perf_counter() - t0) * 1000)
            return ar

        # Extract is the slow one; cap by --limit so it doesn't walk
        # the entire 1300+ NC version corpus on every cycle. The
        # 2026-05-23 overnight burned 1200s per cycle (4 cycles, 80
        # min total) on an unlimited extract that timed out before
        # finishing. With --limit 200 a typical cycle finishes in
        # 5-10 min. The MAIN drain step at line 870 still consumes
        # the reprocess queue, so anything missed here gets picked
        # up next cycle anyway.
        proc = _run(
            ["extract-rates-nc", "--limit", "200", "--progress"],
            timeout_per_stage_s * 2, "extract",
        )
        if not _record("extract", proc):
            # Extract failure is non-fatal: docs are imported, just
            # not parsed yet. Record it but don't mark outcome=failed.
            ar.error = f"extract-rates-nc rc={proc.returncode}"

        ar.duration_ms = int((time.perf_counter() - t0) * 1000)
    except subprocess.TimeoutExpired as exc:
        ar.outcome = "failed"
        ar.error = f"Timeout in global post-steps ({exc.cmd[-1] if exc.cmd else '?'})"
        ar.duration_ms = int((time.perf_counter() - t0) * 1000)
    except Exception as exc:
        ar.outcome = "failed"
        ar.error = str(exc)
        ar.duration_ms = int((time.perf_counter() - t0) * 1000)

    return ar


def _get_docket_recommendations(
    database_path: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Get docket recommendations from the database intelligence report."""
    import json

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.database_reports import (
        find_missing_docket_coverage,
    )

    conn = connect(database_path)
    try:
        report = find_missing_docket_coverage(conn, limit=limit)
        recs = report.get("recommendations", [])
        # Accept all four action types. The previous filter dropped
        # "reprocess" and "classify" silently, hiding work the loop
        # could have done.
        return [
            r for r in recs
            if r.get("recommended_action") in ("fetch", "import", "reprocess", "classify")
        ]
    except Exception:
        logger.debug("Failed to get docket recommendations", exc_info=True)
        return []
    finally:
        conn.close()


def _inventory_fetch_eligible(database_path: str) -> dict[str, Any]:
    """Count NCUC discovery records that genuinely need a portal fetch.

    Returns a structured summary of records by ``fetch_status`` plus
    the count of distinct dockets. Used at loop startup so the
    operator knows whether the portal has any work to do at all.

    Records are considered fetch-eligible when:
      - ``fetch_status`` is one of ``pending``, ``failed``, ``requires_browser``
      - OR ``fetch_status`` is NULL with no ``local_path``
    """
    from duke_rates.db.sqlite import connect

    out: dict[str, Any] = {
        "total_eligible": 0,
        "distinct_dockets": 0,
        "by_status": {},
        "top_dockets": [],
    }
    try:
        conn = connect(database_path)
        try:
            rows = conn.execute(
                """
                SELECT COALESCE(fetch_status, '<null>') AS st, COUNT(*) AS n
                FROM ncuc_discovery_records
                WHERE (
                    fetch_status IN ('pending', 'failed', 'requires_browser')
                    OR (fetch_status IS NULL AND (local_path IS NULL OR local_path = ''))
                )
                GROUP BY st
                """
            ).fetchall()
            for r in rows:
                out["by_status"][r["st"]] = int(r["n"])
                out["total_eligible"] += int(r["n"])

            distinct = conn.execute(
                """
                SELECT COUNT(DISTINCT docket_number)
                FROM ncuc_discovery_records
                WHERE docket_number IS NOT NULL
                  AND (
                    fetch_status IN ('pending', 'failed', 'requires_browser')
                    OR (fetch_status IS NULL AND (local_path IS NULL OR local_path = ''))
                  )
                """
            ).fetchone()
            out["distinct_dockets"] = int(distinct[0]) if distinct and distinct[0] else 0

            # Top 10 dockets ranked by fetch-eligible record count
            top = conn.execute(
                """
                SELECT docket_number, COUNT(*) AS n
                FROM ncuc_discovery_records
                WHERE docket_number IS NOT NULL
                  AND (
                    fetch_status IN ('pending', 'failed', 'requires_browser')
                    OR (fetch_status IS NULL AND (local_path IS NULL OR local_path = ''))
                  )
                GROUP BY docket_number
                ORDER BY n DESC
                LIMIT 10
                """
            ).fetchall()
            out["top_dockets"] = [
                {"docket_number": r["docket_number"], "eligible_count": int(r["n"])}
                for r in top
            ]
        finally:
            conn.close()
    except Exception:
        logger.debug("Failed to inventory fetch-eligible records", exc_info=True)

    return out


def fetch_record_level_dockets(
    fetch_inventory: dict[str, Any],
    *,
    database_path: str,
    max_dockets: int = 2,
    dry_run: bool = True,
    timeout_per_stage_s: int = 300,
) -> AcquisitionCycleResult:
    """Fetch the top fetch-eligible dockets identified by record-level inventory.

    This complements the docket-level recommender. The recommender's
    ``recommended_action="fetch"`` only fires for dockets where ALL
    records need fetching (rare). The record-level inventory surfaces
    dockets that are mostly-fetched but have a few stragglers --
    those stragglers are real portal work.

    Resolves each docket number to a portal UUID (60s timeout), then
    runs ``ncuc docket-fetch GUID --docket-number "X" --download`` to
    pull missing records. Results are reported per-docket.
    """
    t0 = time.perf_counter()
    result = AcquisitionCycleResult()

    caps = check_acquisition_capabilities()
    portal_runtime_disabled = bool(
        os.environ.get("DUKE_RATES_PORTAL_DISABLED_THIS_RUN")
    )
    if not caps["portal_resolve"] or portal_runtime_disabled:
        # Surfaced upstream; nothing to do here.
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    top = fetch_inventory.get("top_dockets") or []
    if not top:
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    acquired = 0
    for entry in top:
        if acquired >= max_dockets:
            break
        docket = entry.get("docket_number") or ""
        eligible = int(entry.get("eligible_count") or 0)

        if dry_run:
            result.results.append(AcquisitionResult(
                docket_number=docket,
                action="fetch_record_level",
                outcome="acquired",
                docs_discovered=eligible,
            ))
            acquired += 1
            continue

        # Resolve to portal UUID with a tight 60s timeout
        docket_uuid = _resolve_docket_uuid(docket, timeout_s=60)
        if docket_uuid is None:
            result.results.append(AcquisitionResult(
                docket_number=docket,
                action="fetch_record_level",
                outcome="skipped_no_uuid",
                error="Could not resolve docket UUID",
            ))
            continue

        ar = _acquire_one(
            docket_number=docket,
            docket_uuid=docket_uuid,
            action="fetch",
            database_path=database_path,
            timeout_per_stage_s=timeout_per_stage_s,
            run_global_post_steps=False,
        )
        ar.action = "fetch_record_level"
        result.results.append(ar)
        result.total_docs_imported += ar.docs_imported
        result.total_charges_added += ar.charges_extracted
        if ar.error:
            result.errors.append(f"{docket}: {ar.error}")
        if ar.outcome == "acquired":
            acquired += 1

    result.duration_ms = int((time.perf_counter() - t0) * 1000)
    return result


def _count_acquired(acquisition_result: AcquisitionCycleResult | None) -> int:
    if acquisition_result is None:
        return 0
    return sum(1 for r in acquisition_result.results if r.outcome == "acquired")
