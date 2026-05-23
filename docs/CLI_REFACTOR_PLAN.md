# CLI Refactor Plan — Typer Sub-Apps

**Status:** ✅ Phases 0–9 complete (228 commands moved; `cli.py` 20,932 → 6,672 lines, 68% reduction)
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

## Addendum — Methodology Lessons (Phases 1–9)

These are the gotchas that bit us during execution. Future agents picking up Phase 10+ or doing a similar split should read this before writing any bulk-edit script.

### 1. Helper-overlap delete ranges (Phase 5)

Symptom: in lineage, `canonicalize-doc-families-nc` lived at lines 16659–16868, and `_apply_canonicalization` (a private helper used only by that command) lived at 16819–16868 — **inside** the command's range. My initial delete script listed both ranges and processed deletes high-to-low, double-deleting and overshooting.

Rule: when a private helper is defined between a command's `@app.command` line and the next `@app.command` (or column-0 `def`), it's **already contained** in the command's extent. Don't add it as a separate delete range. Build ranges from a single boundary pass, never union ranges from independent scans.

### 2. Interleaved helpers between commands (Phase 6)

Symptom: in ncuc, several private helpers (`_pick_best_ncuc_docket_match`, `_print_ncuc_docket_documents`, `_classify_ncuc_access_failure`, `_build_parser_selection_audit_nc_report`, `_build_parser_improvement_candidates_nc_report`) sat between two `@app.command` decorators. My naive boundary detector ("body ends at the next `@app.command`") pulled all of them into the prior command's body.

Rule: a command body ends at the first of:
- the next `@app.command` line at column 0
- the next `def` at column 0 (i.e. a sibling top-level function)

And — when extracting — skip the *first* `def cmd_name(` line after the decorator (that's the command itself, not a foreign def).

### 3. Bulk-rename argv form (Phase 6)

Symptom: a sweep `data.replace('ncuc-portal-smoke-test', 'ncuc portal-smoke-test')` rewrote test argvs:

```python
runner.invoke(cli.app, ["ncuc-portal-smoke-test", ...])
# became
runner.invoke(cli.app, ["ncuc portal-smoke-test", ...])   # INVALID — single arg
```

Typer parses this as one literal arg, not two. The fix is to rewrite argv list elements specifically:

```python
re.sub(r'"(ncuc) ([a-z-]+)"', r'"\1", "\2"', src)
```

Rule: bulk command renames need *two* substitution passes — one for prose/suggestion strings (`"ncuc portal-smoke-test"`) and one for argv literals (`"ncuc", "portal-smoke-test"`). Don't conflate them.

### 4. CRLF/LF mixing breaks line numbers (Phase 7)

Symptom: a Python edit script that wrote `"\n".join(lines)` to a CRLF file produced a hybrid where 3 lines were LF-only. After that, `grep -n` and `text.split("\r\n")` disagreed by progressively-growing offsets, and subsequent line-range deletes hit wrong content.

Rule: when programmatically editing Windows-CRLF source files, always normalize on write:

```python
text = text.replace("\r\n", "\n").replace("\n", "\r\n")
```

…or read/write in binary mode and never let Python's universal-newline translation touch the buffer.

### 5. Prefix-overlap rename collisions + decorator self-rewrite (Phase 7)

Two related hazards from running a flat `(old, new)` table through `str.replace`:

**Prefix overlap.** Rule `('parse-bill', 'billing parse')` matched inside `parse-bill-relevant-progress-nc` (a Phase 8 command) and produced the nonsense `billing parse-relevant-progress-nc`. Sort renames longest-first, or anchor on word boundaries / quotes: `re.sub(r'"parse-bill"', '"billing", "parse"', src)`.

**Decorator self-rewrite.** The bulk-rename also ran over the new sub-app module itself, turning `@billing_app.command("compare-tariff-rates")` into `@billing_app.command("billing compare-tariff-rates")`. Fix:

```python
re.compile(r'(@(billing|data)_app\.command\(")\2 ([^"]+)("\))').subn(r'\1\3\4', text)
```

Rule: bulk-rename scripts must exclude the new sub-app modules from their input set, *or* re-strip the group prefix from `@<group>_app.command("…")` decorators as a final cleanup pass.

### 6. Multi-line def signatures and column-0 boundary detection (Phase 9)

Symptom: in doc-intel, several commands had multi-line type-annotated signatures where the closing `) -> None:` sat at column 0:

```python
@app.command("foo")
def cmd_foo(
    arg1: str,
    arg2: int,
) -> None:
    ...
```

My "stop at column-0 statement" heuristic treated `) -> None:` as a sibling top-level statement and truncated the body to ~13 lines (just the signature).

Rule: regex/line-based boundary detection is fundamentally unreliable for Python. Switch to AST:

```python
import ast
tree = ast.parse(text)
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and _has_app_command_decorator(node):
        start, end = node.lineno, node.end_lineno   # ast already knows
```

`node.end_lineno` gives the exact end including the body, regardless of signature formatting. We adopted this for Phase 9 and would use it from the start in any redo.

### Recommended skeleton for future phases

```python
# 1. Parse with ast; collect (name, decorator_arg, start_line, end_line) for every command.
# 2. Build delete ranges from a single ordered scan — no manual range unions.
# 3. Extract command bodies + any helpers that sit BEFORE the first command
#    (helpers between commands are inside the prior command's range; don't double-count).
# 4. Write the new sub-app module with the right imports (smoke-import it; fix NameErrors).
# 5. Apply renames as TWO passes:
#       - argv literals:        '"<old>"'  →  '"<group>", "<sub>"'
#       - prose/suggestions:    "<old>"   →  "<group> <sub>"
#    Sort longest-first to avoid prefix overlap. Exclude the new sub-app file from the input.
# 6. Delete from cli.py in a single high-to-low pass over the ranges from step 2.
# 7. Normalize line endings on write.
# 8. Snapshot test: tests/test_cli_command_surface.py asserts the full command tree
#    against cli_command_surface.snapshot.txt. Update the snapshot in the same commit.
```

### Test infrastructure that paid off

`tests/test_cli_command_surface.py` + `cli_command_surface.snapshot.txt` caught every accidental drop or rename. Strongly recommended for any future split — it converts "did I move every command?" from a vibes question into a single failing diff.

### Pre-existing failures (baseline, unchanged by refactor)

The 23 test failures observed at the end of each phase match `main` HEAD and are unrelated to the refactor. Clusters:

- `tests/test_tariff_engine_live.py` — live tariff engine, depends on extracted data state.
- `tests/test_ncuc_pipeline.py` — page miner / OCR reintegration tests.
- `tests/test_historical_parser_profiles.py` — bulk extractor profile selection.
- `tests/test_document_classification_audit.py`, `tests/test_seed_document_type_gold.py` — gold-set seeding pipeline.

Don't get distracted chasing these during a refactor phase; compare against the HEAD baseline before assuming a regression.
