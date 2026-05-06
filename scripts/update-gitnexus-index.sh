#!/usr/bin/env bash
# Re-index all GitNexus repos after code changes.
# Run from anywhere — uses script location to find repo root.
#
# Usage:
#   bash scripts/update-gitnexus-index.sh
#   bash scripts/update-gitnexus-index.sh --skip-main   # skip duke-standalone (faster)
#
# Repos indexed:
#   duke-standalone  — full project (excluding cli.py which exceeds size cap)
#   duke-pipeline    — parser_profiles, bulk_extractor, rate_extractor, ocr_normalization, document_prep
#   duke-parse       — nc_progress, nc_carolinas, rider_summary, heuristics
#   duke-db          — repository, schema, ncuc_loader
#
# The sub-indexes (duke-pipeline/parse/db) exist because Tree-sitter scope
# extraction overflows when >~600 nodes accumulate before processing a large
# complex Python file. Each sub-index stays well under that threshold.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SKIP_MAIN=0
for arg in "$@"; do
  [[ "$arg" == "--skip-main" ]] && SKIP_MAIN=1
done

# --- Sync file mirrors into sub-index directories ---

PIPELINE_DIR="$REPO_ROOT/pipeline_index/pipeline"
PARSE_DIR="$REPO_ROOT/pipeline_index/parse"
DB_DIR="$REPO_ROOT/pipeline_index/db"

SRC_PIPELINE="$REPO_ROOT/src/duke_rates/historical/ncuc/pipeline"
SRC_PARSE="$REPO_ROOT/src/duke_rates/parse"
SRC_DB="$REPO_ROOT/src/duke_rates/db"

echo "==> Syncing pipeline file mirrors..."
cp "$SRC_PIPELINE/parser_profiles.py"   "$PIPELINE_DIR/"
cp "$SRC_PIPELINE/bulk_extractor.py"    "$PIPELINE_DIR/"
cp "$SRC_PIPELINE/rate_extractor.py"    "$PIPELINE_DIR/"
cp "$SRC_PIPELINE/ocr_normalization.py" "$PIPELINE_DIR/"
cp "$SRC_PIPELINE/document_prep.py"     "$PIPELINE_DIR/"

echo "==> Syncing parse file mirrors..."
cp "$SRC_PARSE/nc_progress.py"   "$PARSE_DIR/"
cp "$SRC_PARSE/nc_carolinas.py"  "$PARSE_DIR/"
cp "$SRC_PARSE/rider_summary.py" "$PARSE_DIR/"
cp "$SRC_PARSE/heuristics.py"    "$PARSE_DIR/"

echo "==> Syncing db file mirrors..."
cp "$SRC_DB/repository.py"  "$DB_DIR/"
cp "$SRC_DB/schema.py"      "$DB_DIR/"
cp "$SRC_DB/ncuc_loader.py" "$DB_DIR/"

# --- Re-index each sub-repo ---

GN_FLAGS="--skip-git --max-file-size 4096"

echo ""
echo "==> Indexing duke-pipeline (~13s)..."
GITNEXUS_SINGLE_WORKER=1 npx gitnexus@latest analyze $GN_FLAGS --name duke-pipeline "$PIPELINE_DIR"

echo ""
echo "==> Indexing duke-parse (~8s)..."
GITNEXUS_SINGLE_WORKER=1 npx gitnexus@latest analyze $GN_FLAGS --name duke-parse "$PARSE_DIR"

echo ""
echo "==> Indexing duke-db (~8s)..."
GITNEXUS_SINGLE_WORKER=1 npx gitnexus@latest analyze $GN_FLAGS --name duke-db "$DB_DIR"

if [[ "$SKIP_MAIN" -eq 0 ]]; then
  echo ""
  echo "==> Indexing duke-standalone (full repo, ~13s)..."
  GITNEXUS_SINGLE_WORKER=1 npx gitnexus@latest analyze $GN_FLAGS --name duke-standalone "$REPO_ROOT"
else
  echo ""
  echo "==> Skipping duke-standalone (--skip-main)"
fi

echo ""
echo "Done. Four repos registered in ~/.gitnexus/registry.json:"
echo "  duke-standalone  full project"
echo "  duke-pipeline    bulk_extractor, parser_profiles, rate_extractor, ocr_normalization, document_prep"
echo "  duke-parse       nc_progress, nc_carolinas, rider_summary, heuristics"
echo "  duke-db          repository, schema, ncuc_loader"
echo ""
echo "Use '--repo duke-pipeline' (etc.) in MCP tool calls to target a specific sub-index."
