# GitNexus Usage Guide
**Last Updated:** 2026-04-26
**Purpose:** Code navigation, impact analysis, and AI-agent efficiency layer using the GitNexus knowledge graph.

GitNexus indexes this codebase into a graph of symbols, call chains, and clusters. It exposes that graph to Claude Code (and other AI agents) via MCP, enabling structural queries that grep alone can't answer.

---

## Quick Start

### Re-index after significant changes

```bash
# From the repo root (c:\Python\Duke\Standalone)
bash scripts/update-gitnexus-index.sh
```

This syncs file mirrors and re-indexes all four repos (~40s total). To skip the full-project index (faster iteration):

```bash
bash scripts/update-gitnexus-index.sh --skip-main
```

### MCP is already configured

`.mcp.json` at the repo root registers GitNexus with Claude Code automatically. After `analyze`, the tools are live in any Claude Code session in this directory.

### Web UI visualization

```bash
npx gitnexus serve
# Then open https://gitnexus.vercel.app and connect to the local server
```

Shows the full knowledge graph as an interactive WebGL cluster diagram — useful for exploring which modules are tightly coupled.

---

## Multi-Repo Architecture

GitNexus indexes this codebase as **four named repos** to work around Tree-sitter scope extraction limits (see Architecture Notes). All four are served by the same MCP server and queryable simultaneously.

| Repo name | Directory | What it covers |
|---|---|---|
| `duke-standalone` | `C:\Python\Duke\Standalone` | Full project — analytics, billing, models, utils, EIA, Streamlit apps, test files, smaller modules |
| `duke-pipeline` | `pipeline_index/pipeline/` | `parser_profiles`, `bulk_extractor`, `rate_extractor`, `ocr_normalization`, `document_prep` |
| `duke-parse` | `pipeline_index/parse/` | `nc_progress`, `nc_carolinas`, `rider_summary`, `heuristics` |
| `duke-db` | `pipeline_index/db/` | `repository`, `schema`, `ncuc_loader` |

The files in `pipeline_index/` are **mirrors** — copies of the actual source files under `src/duke_rates/`. `update-gitnexus-index.sh` keeps them in sync before re-indexing. Do not edit them directly; edit the source originals.

When targeting a specific repo in MCP tool calls, pass `--repo duke-pipeline` (or whichever applies). If the repo is omitted, the server defaults to the first registered repo.

---

## What Works and What Doesn't

### Tools that work reliably

| Tool | What it does |
|---|---|
| `cypher` | Raw Cypher queries against the graph — most powerful, bypasses FTS bug |
| `impact` | Blast radius of a symbol change — callers, modules, risk level |
| `context` | 360° view of a symbol: all callers, callees, process membership |
| `list_repos` | List all registered repos and their paths |
| `detect_changes` | Maps local git diffs to affected processes (needs `.git`) |
| `rename` | Multi-file rename with graph + regex confidence tags |

### Known limitation: FTS search bug

The `query` tool (natural-language / BM25 keyword search) currently returns empty results due to a read-only database error when GitNexus tries to build the FTS index lazily. Use `cypher` for direct lookups instead — it is more precise anyway.

### Previously failing files — now indexed via sub-repos

These 12 files previously failed scope extraction in the main index. They now live in the sub-repos above and are fully indexed:

| File | Now in repo |
|---|---|
| `src/duke_rates/historical/ncuc/pipeline/parser_profiles.py` | `duke-pipeline` |
| `src/duke_rates/historical/ncuc/pipeline/bulk_extractor.py` | `duke-pipeline` |
| `src/duke_rates/historical/ncuc/pipeline/rate_extractor.py` | `duke-pipeline` |
| `src/duke_rates/historical/ncuc/pipeline/ocr_normalization.py` | `duke-pipeline` |
| `src/duke_rates/historical/ncuc/pipeline/document_prep.py` | `duke-pipeline` |
| `src/duke_rates/parse/nc_progress.py` | `duke-parse` |
| `src/duke_rates/parse/nc_carolinas.py` | `duke-parse` |
| `src/duke_rates/parse/rider_summary.py` | `duke-parse` |
| `src/duke_rates/parse/heuristics.py` | `duke-parse` |
| `src/duke_rates/db/repository.py` | `duke-db` |
| `src/duke_rates/db/schema.py` | `duke-db` |
| `src/duke_rates/db/ncuc_loader.py` | `duke-db` |

**Still not indexed in any repo:** `src/duke_rates/cli.py` (629KB, explicitly skipped — use `docs/cli_command_reference.md` instead).

---

## Answering the Four Navigation Questions

### 1. Which parser/extractor handles a given document type?

Use `cypher` on `duke-pipeline` to find all profile classes:

```cypher
MATCH (c:Class)
WHERE c.filePath CONTAINS 'parser_profiles'
RETURN c.name, c.filePath
ORDER BY c.name
LIMIT 50
```

Or find which profile supports a specific family key:

```bash
# Direct grep — fastest for a single lookup
grep -n "nc-progress-leaf-504\|nc-carolinas-rider" src/duke_rates/historical/ncuc/pipeline/parser_profiles.py

# Use the built-in audit CLI (most complete)
python -m duke_rates show-near-miss-profiles-nc --family-key nc-progress-leaf-504
```

To explore the `supports()` methods of all profile classes via the graph:

```cypher
MATCH (c:Class)-[:CodeRelation {type:'HAS_METHOD'}]->(m:Method {name:'supports'})
WHERE c.filePath CONTAINS 'parser_profiles'
RETURN c.name, m.filePath
ORDER BY c.name
```

### 2. What downstream tables/UI views depend on a parsed rider/rate field?

Use `impact` on `ExtractedCharge` (in `duke-pipeline`):

```
impact("ExtractedCharge", repo="duke-pipeline", direction="downstream")
```

Or use Cypher to trace `tariff_charges` table consumers in `duke-standalone`:

```cypher
MATCH (f)-[:CodeRelation {type:'CALLS'}]->(g)
WHERE g.filePath CONTAINS 'tariff_charges' OR g.name CONTAINS 'tariff_charge'
RETURN f.name, f.filePath, g.name
LIMIT 20
```

For Streamlit dashboard dependencies:

```cypher
MATCH (f)-[:CodeRelation]->(g)
WHERE f.filePath CONTAINS 'app/' AND g.filePath CONTAINS 'analytics'
RETURN f.name, f.filePath, g.name, g.filePath
LIMIT 20
```

The definitive answer is in `docs/architecture.md` (table lineage section) and by grepping:

```bash
grep -rn "tariff_charges" src/duke_rates/ app/ --include="*.py" -l
```

### 3. What code paths are affected by changing OCR or fingerprinting logic?

`normalize_ocr_text` is now indexed in `duke-pipeline`. Use `impact`:

```
impact("normalize_ocr_text", repo="duke-pipeline", direction="upstream")
```

For `document_fingerprints` table:

```
impact("ArtifactFingerprint", repo="duke-standalone", direction="upstream")
```

To find all OCR-related functions across pipeline and parse repos:

```cypher
MATCH (f:Function)
WHERE f.filePath CONTAINS 'ocr' OR f.name CONTAINS 'ocr'
RETURN f.name, f.filePath
ORDER BY f.filePath
LIMIT 30
```

The complete OCR call path is documented in `docs/document_parsing_pipeline_guide.md` (OCR Remediation section).

### 4. Where is docket/source PDF provenance preserved and surfaced?

`discovery.py`, `ncuc_loader.py` (now in `duke-db`), and `repository.py` (now in `duke-db`) are indexed. Use Cypher:

```cypher
MATCH (f)-[:CodeRelation]->(g)
WHERE g.name CONTAINS 'discovery_record' OR g.name CONTAINS 'docket'
RETURN f.name, f.filePath, g.name
LIMIT 20
```

Or grep for where provenance fields are written:

```bash
grep -rn "docket_number\|local_path\|source_pdf" src/duke_rates/db/schema.py | head -20
python -m duke_rates show-historical-doc-nc --hd-id 311
```

---

## Useful Cypher Queries for This Codebase

All of these run via the `cypher` MCP tool or via `context`/`impact`. Specify `--repo` when targeting sub-indexes.

### Explore the cluster map

```cypher
-- Top functional clusters by symbol count
MATCH (c:Community)
RETURN c.heuristicLabel, c.symbolCount, c.keywords
ORDER BY c.symbolCount DESC
LIMIT 20
```

### Find all callers of a function

```cypher
MATCH (a)-[:CodeRelation {type:'CALLS'}]->(b:Function {name:'normalize_ocr_label'})
RETURN a.name, a.filePath
```

### Trace the billing calculation chain

```cypher
MATCH path = (f:Function)-[:CodeRelation*1..3 {type:'CALLS'}]->(g:Function)
WHERE f.filePath CONTAINS 'billing' AND g.filePath CONTAINS 'billing'
RETURN f.name, g.name, f.filePath
LIMIT 30
```

### Find classes in a module

```cypher
MATCH (f:File {path:'src/duke_rates/analytics/tariff_completeness_audit.py'})-[:CodeRelation {type:'DEFINES'}]->(c:Class)
RETURN c.name
```

### Check what Streamlit apps import

```cypher
MATCH (f:File)-[:CodeRelation {type:'IMPORTS'}]->(g)
WHERE f.path CONTAINS 'app/'
RETURN f.path, g.name, g.filePath
ORDER BY f.path
LIMIT 30
```

### Find all process entry points

```cypher
MATCH (s)-[:CodeRelation {type:'ENTRY_POINT_OF'}]->(p:Process)
RETURN s.name, s.filePath, p.heuristicLabel
ORDER BY p.heuristicLabel
LIMIT 25
```

### List all parser profile class names (duke-pipeline)

```cypher
MATCH (c:Class)
WHERE c.filePath CONTAINS 'parser_profiles'
RETURN c.name
ORDER BY c.name
```

### Find methods on BulkExtractor (duke-pipeline)

```cypher
MATCH (c:Class {name:'BulkExtractor'})-[:CodeRelation {type:'HAS_METHOD'}]->(m:Method)
RETURN m.name, m.startLine
ORDER BY m.startLine
```

---

## Re-indexing Workflow

```bash
# Full re-index (sync mirrors + index all 4 repos)
bash scripts/update-gitnexus-index.sh

# Skip full-project index for faster pipeline-only changes
bash scripts/update-gitnexus-index.sh --skip-main

# Manual one-repo re-index (if you only changed db files)
GITNEXUS_SINGLE_WORKER=1 npx gitnexus@latest analyze --skip-git --max-file-size 4096 --name duke-db pipeline_index/db

# Check what was indexed
cat .gitnexus/meta.json
node -e "const r=require(require('os').homedir()+'/.gitnexus/registry.json'); r.forEach(x=>console.log(x.name, x.path))"

# Verify MCP tools are responsive
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | npx gitnexus@latest mcp 2>/dev/null | python -c "import sys,json; d=json.loads(sys.stdin.read()); print('OK:', d['result']['serverInfo'])"
```

---

## Architecture Notes

- **Index location:** `.gitnexus/lbug/` (LadybugDB embedded graph, gitignored)
- **Sub-index location:** `pipeline_index/` (file mirrors for sub-repos, gitignored)
- **No external services:** All processing is local. Nothing is uploaded.
- **Version:** `gitnexus@latest` resolves to 1.6.3 as of 2026-04-26
- **Python support level:** Handles imports, function/class definitions, call chains. Fails on files >512KB (raise with `--max-file-size`) and files that accumulate too many nodes in a single batch (workaround: sub-index directories with ≤5 large files each).
- **Node accumulation overflow:** Root cause of scope extraction failures — when >~600 nodes accumulate from prior files in a batch, Tree-sitter's scope query buffer overflows for large complex files. Files that fail in a full index pass in isolation (e.g., `parser_profiles.py` alone: 933 nodes, succeeds). Sub-index directories with ≤5 large files stay under the threshold.
- **FTS search bug:** The `query` tool (hybrid BM25 + semantic search) fails on this codebase due to a lazy FTS index write attempting a read-only DB connection. Use `cypher` instead.
- **`GITNEXUS_SINGLE_WORKER=1`:** Custom env var in the patched npx worker-pool.js — forces single-threaded extraction (investigation artifact, does not fix the node accumulation overflow, kept for consistency).
- **Existing duke-rates MCP stub:** `src/duke_rates/mcp/server.py` is a separate, domain-specific MCP server exposing bill estimation tools. It is unrelated to GitNexus. Both can run concurrently on different named servers.
