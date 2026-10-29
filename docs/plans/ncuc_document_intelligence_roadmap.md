# NCUC document intelligence roadmap

Started 2026-10-29. Scope and safety boundaries: user's long-running D4–D10 brief.

## Baseline

- Repository: `C:/Python/Duke/Standalone` (WSL `/mnt/c/Python/Duke/Standalone`).
- Starting branch: `phase2e/d3-live-discovery-binding`, HEAD `de376bb`.
- Development branch: `codex/d4-complete-docket-retrieval`.
- One worktree. No tracked changes at takeover. Existing untracked audit scripts,
  reports, and `fake.db` are unrelated and preserved.
- No application commits after D3. October 29 disk audit/cleanup reports document
  environment cleanup only; D3 research databases and downloads were retained.
- A–D3 implementation and tests exist. D4 is incomplete: `docket_inventory_search`
  delegates to a first-page-only reader; the captured E-7 Sub 1026 inventory has
  100 rows (33 orders) and six advertised pages.
- Historical documentation uses several incompatible milestone numbering schemes;
  D4–D10 below refer exclusively to the user's October 29 brief.

## Safety and evidence

Production database, archive, historical charges, effective dates and regulatory
status must remain untouched. Use test databases or explicit scratch copies. Local
commits only; no push, merge, destructive migrations, large crawl, large model
download or non-isolated dependency installation. Agent source review is never
human verification. Incomplete inventories never count as verified negative evidence.

## Milestones and acceptance gates

| Milestone | Status | Acceptance gates |
| --- | --- | --- |
| D4 complete inventory retrieval | Engineering validated; production-preservation gate failed | Six verified live pages, 568 entries, 83 public orders; 1,894 tests passed; replay/ledger benchmark and report complete. Keep the preflight incident visible; no claim of complete acceptance. Local delivery on the D4 branch. |
| D5 redline integrity | Completed locally: `7dc5caa` | Extend native page evidence only after detector/pathway audit; measured precision/recall with explicit label provenance; prioritized exposure queue; close unattended authoritative promotion bypasses; preserve proposed research; extraction/billing checks as applicable; report and commit. |
| D6 evidence-driven discovery | Completed locally: `2555e98` | Preserve clue and relationship evidence; local/inventory/order-first searches; multi-docket attribution; durable content-addressed retrieval; quantify candidates, gaps and requests avoided; tests, report and commit. |
| D7 large-document intelligence | D7A checkpoint ready; D7B incomplete | Independently source-reviewed boundaries for representative filings; reuse existing segmentation and bounded extraction; original page ownership; quantify boundary/recall/time/resource results; tests, report and commit. |
| D8 Docling modernization | Pending | Inspect isolated existing environments; configurable layout/model cache identity; TableFormer v1 baseline; no adoption before numerical and resource gates; rollback; report and commit. |
| D9 historical extraction | Pending | Recompute gap taxonomy; targeted structured profiles; numerical/unit/page/TOTAL validation; candidate-only remediation including Leaf 600; extraction and billing benchmarks; report and commit. |
| D10 training readiness | Pending | Separate observed/automated/reviewed/human labels; reproducible export if justified; related-document split isolation; targeted lightweight-model plan; report and commit. |

## Execution notes

- GitNexus MCP is not exposed in this session. An existing cached Windows CLI
  is available; use the same GitNexus query/context/impact/detect-changes functions
  through that CLI. The stored index predates final D3 commits; refresh before
  symbol edits. Do not substitute text search for required impact analysis.
- External WaitingForPower source and MIT license inspected online. Its session
  state observations are hypotheses to validate locally, not proof about this run.
- Targeted D2/D3 discovery baseline: **31 passed in 8.80 s**. Full baseline in
  preserved `.venv312`: **1,857 passed, 92 skipped, 4 xfailed**, 135 warnings,
  767.69 s. No live-DB snapshot configured; existing skip markers remained active. Output:
  `reports/phase2e_d4/baseline_tests.txt`.
- Bounded portal reconnaissance: existing-session smoke checks succeeded;
  E-7 Sub 1026 page 1 remains 100 rows / 33 orders. Three exploratory attempts
  yielded no verified page 2: first postback failed, second encountered an
  `href` KeyError, third captured a stale page after clicking page 2. Preserve
  the captures as partial evidence; do not report beyond-page-1 discovery.
- GitNexus refresh encountered worker startup failure, incremental graph load
  failure, then a full rebuild error identifying a 256 MiB database buffer
  pool limit. A full rebuild using the existing runtime, one worker, and a
  1 GiB pool succeeded: 20,183 nodes, 39,364 edges, 979 flows. A pre-force local
  index copy is preserved in scratch. Runtime launcher:
  `node.exe --max-old-space-size=4096 scratch/d4_gitnexus.cjs`; it prepends Git's
  DLL directory and sets `GITNEXUS_LBUG_BUFFER_POOL_SIZE=1073741824`. Use native
  Windows Git in this checkout to avoid cross-platform index-stat/EOL noise.
- **Safety incident:** the documented `run-continuous-loop-nc --dry-run
  --max-cycles 0` preflight opened production through the migration-capable
  connection helper. Its mtime and SHA-256 changed. See
  `docs/reports/2026-10-29_d4_preflight_safety_incident.md`. Nine checked data
  tables match the preserved D1 research copy exactly; this is not proof of
  whole-database identity. No restoration attempted. All future CLI probes must
  point to explicit scratch databases. This defect must be covered before D4
  completion; production-unchanged acceptance cannot be claimed for this run.
- Completed a broader read-only comparison: all 79 shared application tables
  match the D1 research reference row for row. Only the shared internal
  `sqlite_sequence` table differs. This strengthens the row-level evidence but
  does not undo the file write or establish an exact pre-command snapshot.
- Impact checks on the fresh index: inventory search (one CLI callback), parser
  (one outcome builder), fetch inventory (two loop callers), and ledger methods
  (attempt wrapper, network/local-resolution flows) are LOW. New unindexed
  helpers have no existing graph nodes; CLI registration callbacks have no
  recorded graph callers (UNKNOWN), and their wiring was inspected directly.
- Implemented `docket_inventory.py` as a traversal adapter around the existing
  D3 parser/outcome. Exact page and stable-ID checks, deduplication, count and
  pager validation, caps, bounded retries, blocked-response stop, sanitized
  per-page checkpoints and transient-navigation handling. Recovery replays and
  revalidates current pages; it does not yet skip previously fetched pages.
- `remote_discovery` 1.2.0 wires traversal into the existing exact-docket search;
  old injected non-browser tests retain their single-page outcome. Existing
  `--capture-dir` now enables inventory checkpoints. Search ledger preserves
  returned error details. Parser handles an anchor without `href` conservatively.
- `_inventory_fetch_eligible` now uses `mode=ro` and `query_only`; scratch tests
  prove no byte/mtime mutation and no creation of a missing database.
- Focused new tests: **23 passed**, Ruff clean. Broader discovery/CLI check before
  the final navigation-read fix: **159 passed**. Full post-change suite pending.
- First implementation live probe hit a transient `page.content()` navigation
  exception before its HTTP comparison; regression fixed and tested. A bounded
  browser-vs-session-HTTP comparison is running in one authenticated context,
  at most six pages per transport, zero PDF downloads. Do not mark live complete
  until its source evidence is checked.
- **Final live result:** all six E-7 Sub 1026 pages, 568 unique entries, 85 typed
  orders, 11 restricted entries (two orders). Public order candidates: 83, up
  from page 1's 33. Verified listing for the September 24, 2013 general-rate
  order on page 3 (`413b681f-f9ba-4bfe-9b73-a410602aaffb`). Zero PDF downloads.
  Traversal from an already loaded first page took 18.73 s. HTTP's first
  authenticated-context GET returned 403 in 0.129 s and was not retried/adopted.
- A shared-caller audit found the canonical CLI would still read page 1 through
  `session.get_docket_documents`. GitNexus rated that change HIGH (four direct
  callers, four affected process entry points); the risk was reported before
  editing. The same walker now backs that list API; partial reads raise with
  their outcome, restricted download URLs are withheld, and the bounded
  discovery executor uses an explicit first-page loader. No authentication or
  download implementation changed.
- Latest targeted set: **180 passed**, Ruff clean. An intermediate full run
  passed 1,884 tests before final pager/shared-session changes. The final full
  suite is running on the frozen implementation; earlier interrupted runs are
  retained rather than reported as passing final validation.
- Initial staged GitNexus change detection reports CRITICAL aggregate scope and
  uses outdated symbol positions. A separate AST comparison confirms only the
  intended existing functions/classes changed; `check_identity` is unchanged.
  Refresh the index and rerun detect-changes before committing. No commit yet.
- Draft D4 implementation report:
  `docs/reports/2026-10-29_phase2e_d4_complete_docket_retrieval.md`.
- Final full suite: **1,894 passed, 92 skipped, 4 xfailed** in 670.54 s;
  180 targeted tests passed, Ruff clean. Final production SHA-256 remains
  `0f6f8f3551f21908533019c5f568d49e254fdd0149fa65ceedef9aa37031b6ca`, identical
  to the post-incident hash. Offline replay: 0.0624 s median / 100 entries
  (partial) versus 0.9212 s / 568 entries (complete), 83 total candidate rows
  after recovery, and one completed query avoided. A full GitNexus rebuild
  corrected an inconsistent incremental index. Final detection has 16 expected
  flows, CRITICAL aggregate risk, no error/partial result; reviewed against the
  AST diff. Local source/evidence delivery is ready on the D4 branch.

## Requested stopping checkpoint

The user requested PR publication, safe merge of the complete prerequisite/session
chain and a clean main checkout. D4–D6 are delivered locally (`90be8be`, `7dc5caa`,
`2555e98`). D7A segmentation/routing/cache metadata and both importer guards form
the next logical checkpoint. Its source-backed study covers four filings / 2,121
pages / 104 formal tariff components, with all 317 tariff pages recovered and
zero false tariff pages on the declared development reference. Cached component
boundaries still have six cases requiring reconciliation.

Checkpoint validation is complete: 1,998 passed, 92 skipped, four xfailed; all
12 bill records and canonical snapshot hashes match D6. Publish/merge the complete
prerequisite/session chain and pause here. RAG PR #25 is held after compatibility
review because its documented citation-grounding regression remains unresolved. D7B bounded-conversion hardening and D8–D10
remain unfinished. Preserve existing loose files while preparing a clean main
worktree. The original production-preservation gate remains failed due to the
recorded D4 incident. See `docs/reports/2026-10-29_goal_session_checkpoint_summary.md`.
