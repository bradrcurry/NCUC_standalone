# Tool And Workflow Backlog

Use this file for small, durable improvement candidates for the local tool stack and sanctioned workflows.

Keep items short. If a candidate becomes implemented, remove it from this file and update the canonical docs in the same task.

## Current Backlog

| Priority | Area | Problem | Preferred Improvement Target | Status |
|---|---|---|---|---|
| P1 | Explainability | It is still hard to explain one document's full path from discovery through OCR, parsing, review, and reprocess state in one place. | Add a single-document lineage/explain CLI surface. | **Implemented** — `diagnose-document-nc --hd-id X` shows full pipeline path, profile selection, candidates, signals, text metrics, and recommended next action. Also added `validate-parser-change-nc`, `deduplicate-family-nc`, `canonicalize-doc-families-nc`, `show-extraction-coverage-nc`. |
| P1 | Workflow summary | Session start still benefits from several queue summaries after `show-workflow-status-nc`. | Add a stronger compact pipeline doctor / health summary command or extend the existing status command. | Open |
| P1 | Benchmarking | OCR/reporting now exists, but table-backend comparison is not surfaced alongside OCR outcomes yet. | Extend `report-ocr-benchmark-nc` with `table_backend` and related cohort summaries. | Open |
| P2 | Tool routing | JSON manifests are strong, but there is still no compact human-readable policy doc for tool promotion and anti-patterns. | Keep [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md) current as the canonical human-facing policy layer. | Implemented |
| P2 | Historical handoff hygiene | Live handoff docs tend to accumulate accomplishment history. | Keep session narratives in reports and preserve only active state in `NEXT_SESSION_*` docs. | In progress |
| P2 | Reprocess parallelism | `process-reprocess-queue-nc` is sequential (25 items/batch). Ollama supports concurrent requests and is GPU-backed. | Add `--workers N` option to `process-reprocess-queue-nc` to parallelize GLM-OCR calls. | **Implemented** — `--workers N` + `ThreadPoolExecutor` added to both `process-reprocess-queue-nc` and `process-ocr-queue-nc`. |
| P2 | Stale metric accuracy | `stale_historical` in `show-workflow-status-nc` did not distinguish truly-stale from never-processed docs. | Split count into `stale_historical` vs `never_processed` buckets. | **Implemented** — status command now reports both counts separately with aligned queries. |
| P2 | Script promotion | Some maintenance/debug scripts still answer recurring questions better than the CLI. | Promote reusable scripts into parameterized CLI commands when the workflow stabilizes. | Open |
| P1 | OCR bottleneck | 271/282 empty/weak docs are OCR-blocked — import pipeline only used CPU Tesseract. | Wire progressive OCR escalation: CPU Tesseract → Docling GPU → GLM-OCR GPU, using decision matrix. | **Implemented** — `extract_pages_with_progressive_ocr()` in `ocr.py`; `select_ocr_backend()` decision matrix; importer passes triage signals; `gpu_ocr_candidate` triggers GPU path. |
| P1 | OCR normalization | `normalize_ocr_text()` was only called from Carolinas parser, not Progress parser or extraction pipeline. | Add universal normalization at BulkExtractor level, normalize at storage time, bump version. | **Implemented** — `normalize_ocr_text()` called in `BulkExtractor.extract_charges_from_document()`; page text normalized before `save_page_artifacts()`; `OCR_NORMALIZATION_VERSION` bumped to v2. |
| P1 | OCR normalization patterns | Only 64 lines with 10 word replacements and 8 regexes — missing digit↔letter, whitespace fixes, garbage chars. | Expand normalization: digit↔letter (0↔O, 1↔l, S→$), whitespace fragmentation ($14.\\n00), garbage char filtering, column merge prevention. | **Implemented** — `ocr_normalization.py` expanded to ~155 lines: _WHOLE_WORD_REPLACEMENTS 27 entries, _DIGIT_LETTER_FIXES, _GARBAGE_CHARS_RE, _GARBAGE_LINE_RE, _fix_whitespace_fragmentation, _fix_column_merge. |
| P3 | Report ergonomics | DB-generated reports are strong, but operators still need a clearer “current reports to trust first” surface. | Keep `docs/reports/README.md` curated and aligned with the current audit/export set. | In progress |

## Rules

- Do not turn this into a second roadmap.
- Keep entries focused on the shared tool/workflow surface.
- Prefer one line per candidate.
- Remove implemented items after updating the canonical docs that replaced the gap.
