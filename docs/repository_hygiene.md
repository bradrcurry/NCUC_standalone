# Repository Hygiene And GitHub Prep

## Purpose

This document defines the intended repository shape for publishing this project
to GitHub without leaking local artifacts, bulky generated outputs, or temporary
development files.

It is not a one-time cleanup checklist only. It is the baseline for where new
files should live going forward.

## Existing Guidance

There is already partial layout guidance in:
- [README.md](/c:/Python/Duke/Standalone/README.md)
- [architecture.md](/c:/Python/Duke/Standalone/docs/architecture.md)

Those documents explain the codebase at a high level. This document is narrower:
- what belongs in version control
- what should stay local
- what should move out of the repo root
- what kinds of generated artifacts should be deleted or ignored

For JSON and JSONL cleanup specifically, see:
- [json_artifact_inventory.md](/c:/Python/Duke/Standalone/docs/json_artifact_inventory.md)

## Target Repository Shape

```text
src/duke_rates/         Application code and library modules
tests/                  Regression, parser, billing, and integration tests
docs/                   Durable documentation and architecture notes
scripts/                Operational scripts worth keeping
data/
  raw/.gitkeep
  historical/
  processed/.gitkeep
  manifests/.gitkeep
  db/.gitkeep
  tmp/.gitkeep
```

### What belongs in `src/`

Only reusable application code:
- parsers
- billing engine code
- DB models and repository code
- CLI logic
- OCR and historical pipeline code

Do not put one-off debugging helpers or export runners in `src/`.

### What belongs in `scripts/`

Keep scripts here only if they are operationally useful and likely to be reused:
- import/backfill jobs
- export jobs
- cleanup/repair utilities
- reproducible maintenance commands
- reusable AI-agent-created helpers that fill a real operator gap

Examples that likely belong in `scripts/` rather than repo root:
- export helpers
- DB inspection helpers
- PDF mining utilities
- one-off repair tools that remain useful after the immediate incident

Agent-created helpers should not remain as anonymous residue. If another AI
agent had to create a helper to operate the pipeline, either:

- keep it in the appropriate `scripts/` subfolder and document its usage
- promote it into the CLI if it is part of a recurring operator workflow
- delete it if it was truly disposable and taught no reusable lesson

### What belongs in `docs/`

Keep durable project knowledge here:
- architecture
- roadmap
- known issues
- parser/OCR strategy
- GitHub/repository hygiene notes

Session notes are only worth committing if they remain useful as durable project
history. If they are just conversational residue, they should be deleted or
 consolidated into a real doc.

### What belongs in `data/`

`data/` is local working state and should generally not be committed, except for
placeholder `.gitkeep` files or carefully chosen tiny fixtures.

Use:
- `data/raw/` for current source documents
- `data/historical/` for archived/manual/NCUC PDFs
- `data/processed/` for generated CSV/JSON exports
- `data/manifests/` for generated search/index manifests
- `data/db/` for SQLite databases
- `data/tmp/` for short-lived working files

Generated exports and downloaded PDFs should not be the default GitHub payload.

## Root Directory Rules

The repository root should stay small and intentional.

Good root-level files:
- `README.md`
- `pyproject.toml`
- `Makefile`
- `.gitignore`
- `.env.example`
- a small number of high-value top-level reports if they are truly central

Files that should usually not remain in root:
- temporary logs
- local databases
- ad hoc inspection scripts
- temporary HTML files
- one-off export wrappers
- exploratory notebooks or scratch programs

## Current Cleanup Candidates

These items should be reviewed first before GitHub publication.

### Safe local artifacts to ignore or remove

- root local DBs, temporary logs, and scratch HTML files
  - these have already been added to `.gitignore`
  - obvious root artifacts from local runs have been removed
- `data/tmp/`
- scratch run directories already covered by `.gitignore`:
  - `data_api_*`
  - `data_browser/`
  - `data_eval/`
  - `data_history_eval/`
  - `data_smoke/`

### Root scripts to review for relocation

The first script cleanup pass is complete. Reusable root utility scripts were moved into:

- `scripts/exports/`
- `scripts/maintenance/`
- `scripts/debug/`

Further cleanup should focus on whether each remaining script is:
- worth keeping
- in the right subfolder
- better expressed as a CLI command instead of a standalone script

### Root markdown files to review for consolidation

The report-consolidation pass is mostly complete.

Durable planning and investigation notes now belong under:
- `docs/`
- `docs/reports/`

Recommended rule:
- if it is durable project documentation, move it under `docs/`
- if it is superseded session residue, delete it
- avoid adding new session-style markdown files back to the repo root

## Recommended Script Layout

As the next cleanup pass, prefer:

```text
scripts/
  exports/
  maintenance/
  debug/
  ingestion/
```

Suggested mapping:
- export scripts -> `scripts/exports/`
- DB repair/cleanup tools -> `scripts/maintenance/`
- triage/inspection helpers -> `scripts/debug/`
- ingest/backfill runners -> `scripts/ingestion/`

## GitHub Prep Checklist

### Phase 1. Ignore and remove local artifacts

- [x] ignore root local DBs and log files
- [x] ignore `data/tmp/`
- [x] delete the empty root `duke_rates.db`
- [x] remove obvious temporary local HTML/log artifacts

### Phase 2. Decide what data stays out of Git

- [ ] confirm that downloaded PDFs remain excluded
- [ ] confirm that generated CSV/JSON outputs remain excluded
- [ ] confirm that SQLite DBs remain excluded
- [ ] keep only `.gitkeep` placeholders for expected local directories

### Phase 3. Normalize root layout

- [x] move reusable operational scripts into `scripts/`
- [x] move durable markdown docs into `docs/`
- [ ] delete obsolete session notes and generated reports
- [x] reduce root to only intentional project-entry files

### Phase 4. Repo publication readiness

- [ ] verify `.env` is excluded and `.env.example` is sufficient
- [ ] review README quick start for a fresh clone experience
- [ ] verify no secrets or local absolute paths are required for basic startup
- [ ] verify tests that should pass in CI do not depend on local data files
- [ ] decide whether to add GitHub Actions for lint/test

## Principles For Future Additions

Before adding a new file, ask:

1. Is this reusable code?
   - put it in `src/`
2. Is this an operational helper?
   - put it in `scripts/`
3. Is this durable project knowledge?
   - put it in `docs/`
4. Is this generated or downloaded state?
   - put it in `data/` and ignore it
5. Is this just temporary debugging output?
   - keep it local and delete it when done

## Policy For Agent-Created Helpers

If an AI agent creates a new helper while working on ingestion, parsing, OCR,
review, or reparsing, the default should be to capture it intentionally.

Use this decision rule:

1. Is it likely to be reused by another operator or agent?
   - keep it under `scripts/` in the correct subfolder
2. Is it a recurring operational workflow?
   - promote it into the CLI and document it in the pipeline guide
3. Is it only useful for a narrow local inspection?
   - keep it under `scripts/debug/` with a clear name and narrow scope
4. Is it truly throwaway?
   - delete it after use and do not leave it in the repo root

Minimum standard for any kept helper:

- descriptive filename
- no repo-root placement
- no hidden dependence on a single local path without documentation
- short usage note in a durable doc if another agent may need it later

## Recommended Next Actions

1. Delete obsolete session notes and generated reports that are no longer worth keeping.
2. Run a final repo audit before `git init` / first publish:
   - root file review
   - ignored-file review
   - secret scan
   - fresh-clone setup check
3. Decide whether any remaining standalone maintenance scripts should become CLI commands.
