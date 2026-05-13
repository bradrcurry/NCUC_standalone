# CLI Refactor Plan — Typer Sub-Apps

**Status:** In progress (1 of 10 phases complete)
**Branch:** `refactor/cli-sub-apps`
**Started:** 2026-05-12
**Plan origin:** This document captures the in-flight refactor of `src/duke_rates/cli.py` (~19k lines, ~275 commands) into Typer sub-apps organized by domain.

## Why

`src/duke_rates/cli.py` had grown to 19,471 lines and 275 commands, all registered flat on a single `typer.Typer()` app. Pain points:

- **Too large for GitNexus to index.** Tree-sitter scope extraction fails on the file; it has no reliable clusters or execution flows in the graph.
- **Hard to navigate.** Adding a command means scrolling through 19k lines to find the right spot.
- **Slow CLI startup.** All 275 commands and their imports load on every invocation.
- **Hard to share helpers.** Sub-app modules can't reach into cli.py without circular imports.

## Approach

Split commands by domain into `src/duke_rates/cli_commands/<domain>.py` modules, each exposing a `typer.Typer()` sub-app. Wire them into cli.py via `app.add_typer(sub_app, name="<domain>")`.

User-visible: command names change. For example `show-ocr-queue-nc` becomes `ocr show-queue-nc`. Names that are already part of a logical group lose the redundant prefix (e.g. `enqueue-ocr-nc` → `ocr enqueue-nc`).

Backward compatibility is **not preserved**. Every reference inside the codebase (operator-facing suggestion strings, tests, agent docs) is updated as part of each phase.

## What's Done

### Phase 0 (prerequisite) — ✅ Complete (commit `dec7258`)

Established the sub-app wiring pattern with the OCR pilot.

- **`src/duke_rates/cli_commands/_cli_utils.py`** — shared helpers used by all sub-app modules: `_bootstrap`, `_safe_cli_text`, `_format_optional_pct`, `_count_rows`.
- **`src/duke_rates/cli_commands/_ocr_reports.py`** — shared OCR report builders (`_safe_text_file_length`, `_classify_ocr_route`, `_build_ocr_benchmark_nc_report`, `_build_ocr_remediation_candidates_nc_report`). Co-located here because both `cli.py` (for `validate-document-diagnostics`) and `ocr.py` need them.
- **`src/duke_rates/cli_commands/ocr.py`** — the `ocr` sub-app with 7 commands.
- `cli.py` registers the sub-app via `app.add_typer(ocr_app, name="ocr")` and re-imports the helpers so `cli.X` lookups still resolve (needed for the test-monkeypatch path).
- `test_agent_manifests` extended to discover sub-app commands as `"<subapp> <cmd>"` — future phases don't need test updates here.

**Pattern lessons:**

- When a sub-app's command is called by code still in cli.py (e.g. `execute-workflow-next-action-nc` calling `enqueue_ocr_remediation_nc`), import the function back into cli.py at the top: `from duke_rates.cli_commands.ocr import enqueue_ocr_remediation_nc`. The function is now reachable as both `cli.enqueue_ocr_remediation_nc` (for monkeypatching) and `ocr_module.enqueue_ocr_remediation_nc` (for direct calls).
- Tests that monkeypatch `cli._bootstrap` or `cli.triage_pdf` to redirect database access need to **also** patch the sub-app module (`monkeypatch.setattr(ocr_module, "_bootstrap", ...)`). The sub-app's command body looks up the name in its own module namespace.
- Operator-facing suggestion strings (e.g. `recommended_command="python -m duke_rates ocr process-queue-nc --limit 1"`) need updating throughout cli.py and the agent docs.

### Command renames in Phase 0

| Old | New |
|---|---|
| `enqueue-ocr-nc` | `ocr enqueue-nc` |
| `show-ocr-queue-nc` | `ocr show-queue-nc` |
| `report-ocr-benchmark-nc` | `ocr report-benchmark-nc` |
| `show-ocr-remediation-candidates-nc` | `ocr show-remediation-candidates-nc` |
| `enqueue-ocr-remediation-nc` | `ocr enqueue-remediation-nc` |
| `process-ocr-queue-nc` | `ocr process-queue-nc` |
| `process-ocr-backlog-nc` | `ocr process-backlog-nc` |

## What's Left

Each phase ships independently. Recommended execution order is roughly smallest/safest first, then largest:

| # | Phase | Sub-app | Count | Notes |
|---|---|---|---|---|
| 1 | `reprocess.py` | `reprocess` | 9 | Reprocess-queue commands. Mostly contiguous in cli.py (lines ~4133-4691). |
| 2 | `export_audit.py` | `export` + `audit` | 22 | Pure reporting commands (`export-nc-*`, `export-dep-*`, `audit-*`). Self-contained, easiest to move. |
| 3 | `historical_workflow.py` | `workflow` | 11 | Missing-doc remediation cluster. |
| 4 | `search.py` | `search` | 12 | Already has `search-*` prefix. |
| 5 | `lineage.py` | `lineage` | 34 | Historical CRUD, dedup, canonicalize, repair. |
| 6 | `ncuc.py` | `ncuc` | 29 | Portal, docket, discovery, wayback. |
| 7 | `billing.py` | `billing` | 23 | Billing + EIA/OpenEI commands. |
| 8 | `progress.py` | `progress` | 40 | The `*-progress-nc` cluster. |
| 9 | `doc_intel.py` | `doc-intel` | 47 | Largest group: benchmarks, overnight loops, LLM probe, workflow, diagnose. Scattered across cli.py lines 2336–19291 — needs careful extraction. |

### Per-phase checklist

For each phase, follow the OCR pilot pattern:

1. **Identify the commands** — grep `^@app.command\("<prefix>` in cli.py and confirm line ranges.
2. **Identify private helpers** used only by those commands. Co-locate them in the new sub-app module unless they're shared with commands that stay in cli.py — in which case extract to `_<domain>_reports.py` and import from both sides.
3. **Create the sub-app module** at `src/duke_rates/cli_commands/<domain>.py` with `<domain>_app = typer.Typer(help="...")` and `@<domain>_app.command(...)` decorators (strip the redundant prefix from command names).
4. **Wire into cli.py**: add `from duke_rates.cli_commands.<domain> import <domain>_app` near the existing sub-app imports, and `app.add_typer(<domain>_app, name="<domain>")` after the `app = typer.Typer(...)` line.
5. **If cli.py still references the moved functions by name** (e.g. `execute-workflow-next-action-nc` calling `process_ocr_queue_nc`), also import those names back: `from duke_rates.cli_commands.<domain> import <func1>, <func2>`.
6. **Delete the moved commands and helpers from cli.py.** Use a Python one-liner for large deletions (see commit `dec7258` for the pattern).
7. **Update in-tree references**:
   - Suggestion strings in cli.py: `python -m duke_rates <old-name>` → `python -m duke_rates <new-name>`.
   - Tests: update `runner.invoke(cli.app, ["<old>", ...])` to `runner.invoke(cli.app, ["<group>", "<sub>", ...])`. Add `monkeypatch.setattr(<domain>_module, "_bootstrap", ...)` next to any existing `monkeypatch.setattr(cli, "_bootstrap", ...)`.
   - `docs/agent_tool_registry.json` and `docs/agent_workflows.json`: tool IDs and `command` fields.
   - `docs/cli_command_reference.md`: rename entries in the relevant section table.
8. **Run the smoke tests**: `python -m duke_rates --help`, `python -m duke_rates <group> --help`, plus pytest for the impact area.
9. **Verify zero regressions**: run the full suite, compare against `git stash` baseline if anything looks suspicious.
10. **Commit** with a message that lists the renames and notes what was moved.

### After all phases ship

- `cli.py` should be ~1,500 lines: hardware setup, ~20 root-level commands (`crawl`, `tariff-update`, `list-docs`, `show-doc`, `parse`, `parse-batch`, `classify-docs`, `build-tariff-families`, `extract-rates-nc`, `gpu-status`, `ingest-ncuc`, `load-ncuc-ingest`, etc.), and the `app.add_typer(...)` registration block.
- `scripts/update-gitnexus-index.ps1` — add new mirror groups for the new sub-app modules so they get indexed separately.
- Re-run `pwsh scripts/update-gitnexus-index.ps1` to refresh the graph.
- Delete the `_DEAD` and stale-reference comments in cli.py from the pilot.

## Verification

After each phase:

```powershell
duke-rates --help                          # root commands still present
duke-rates <subapp> --help                 # sub-app help renders
duke-rates <subapp> <command> --help       # command help works
python -m pytest tests/ -q                  # full suite, ignore known pre-existing failures
```

After all phases:

```powershell
# GitNexus should now index cli.py in the main pass.
pwsh scripts/update-gitnexus-index.ps1
```
