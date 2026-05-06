<# 
Re-index all GitNexus repos after code changes.

Usage:
  powershell -ExecutionPolicy Bypass -File scripts/update-gitnexus-index.ps1
  powershell -ExecutionPolicy Bypass -File scripts/update-gitnexus-index.ps1 -SkipMain

The sub-indexes are intentionally small mirror directories. GitNexus can fail
scope extraction when many complex Python files accumulate in one full-repo
batch, so large/complex modules are indexed again in focused named repos.
#>

param(
    [switch]$SkipMain
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$PipelineRoot = Join-Path $RepoRoot "pipeline_index"

$env:GITNEXUS_SINGLE_WORKER = "1"
$env:npm_config_yes = "true"

function Initialize-MirrorDir {
    param([Parameter(Mandatory=$true)][string]$Path)

    $resolvedPipelineRoot = [System.IO.Path]::GetFullPath($PipelineRoot)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($resolvedPipelineRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to reset mirror outside pipeline_index: $fullPath"
    }

    if (Test-Path -LiteralPath $Path) {
        Get-ChildItem -LiteralPath $Path -Force | Remove-Item -Recurse -Force
    } else {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Sync-Mirror {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$Directory,
        [Parameter(Mandatory=$true)][string[]]$Files
    )

    Write-Host "==> Syncing $Name file mirrors..."
    Initialize-MirrorDir -Path $Directory
    foreach ($relativePath in $Files) {
        $source = Join-Path $RepoRoot $relativePath
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Missing mirror source: $relativePath"
        }
        Copy-Item -LiteralPath $source -Destination $Directory -Force
    }
}

function Invoke-GitNexusAnalyze {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$Directory
    )

    Write-Host ""
    Write-Host "==> Indexing $Name..."
    npx gitnexus@latest analyze --skip-git --max-file-size 4096 --name $Name $Directory
}

$Mirrors = @(
    @{
        Name = "duke-pipeline"
        Directory = Join-Path $PipelineRoot "pipeline"
        Files = @(
            "src/duke_rates/historical/ncuc/pipeline/parser_profiles.py",
            "src/duke_rates/historical/ncuc/pipeline/bulk_extractor.py",
            "src/duke_rates/historical/ncuc/pipeline/rate_extractor.py",
            "src/duke_rates/historical/ncuc/pipeline/ocr_normalization.py",
            "src/duke_rates/historical/ncuc/pipeline/document_prep.py"
        )
    },
    @{
        Name = "duke-pipeline-docling"
        Directory = Join-Path $PipelineRoot "pipeline-docling"
        Files = @(
            "src/duke_rates/historical/ncuc/pipeline/docling_backend.py",
            "src/duke_rates/historical/ncuc/pipeline/docling_page_miner.py",
            "src/duke_rates/historical/ncuc/pipeline/document_prep.py",
            "src/duke_rates/historical/ncuc/pipeline/metadata_extractor.py"
        )
    },
    @{
        Name = "duke-parse"
        Directory = Join-Path $PipelineRoot "parse"
        Files = @(
            "src/duke_rates/parse/nc_progress.py",
            "src/duke_rates/parse/nc_carolinas.py",
            "src/duke_rates/parse/rider_summary.py",
            "src/duke_rates/parse/heuristics.py"
        )
    },
    @{
        Name = "duke-db"
        Directory = Join-Path $PipelineRoot "db"
        Files = @(
            "src/duke_rates/db/repository.py",
            "src/duke_rates/db/schema.py",
            "src/duke_rates/db/ncuc_loader.py"
        )
    },
    @{
        Name = "duke-cli"
        Directory = Join-Path $PipelineRoot "cli"
        Files = @(
            "src/duke_rates/cli.py"
        )
    },
    @{
        Name = "duke-doc-intel"
        Directory = Join-Path $PipelineRoot "doc-intel"
        Files = @(
            "src/duke_rates/document_intelligence/acquisition.py",
            "src/duke_rates/document_intelligence/database_reports.py",
            "src/duke_rates/document_intelligence/model_benchmark.py",
            "src/duke_rates/document_intelligence/normalization.py"
        )
    },
    @{
        Name = "duke-doc-triage"
        Directory = Join-Path $PipelineRoot "doc-triage"
        Files = @(
            "src/duke_rates/document_intelligence/parse_diagnosis.py",
            "src/duke_rates/document_intelligence/parse_improvement_loop.py",
            "src/duke_rates/document_intelligence/regex_shadow_test.py",
            "src/duke_rates/document_intelligence/regex_validation.py",
            "src/duke_rates/document_intelligence/regex_suggestions.py",
            "src/duke_rates/cli_commands/parse_refactor.py"
        )
    },
    @{
        Name = "duke-billing"
        Directory = Join-Path $PipelineRoot "billing"
        Files = @(
            "src/duke_rates/billing/tariff_engine.py",
            "src/duke_rates/billing/reconciliation.py",
            "src/duke_rates/billing/engine.py",
            "src/duke_rates/billing/calculators.py"
        )
    },
    @{
        Name = "duke-analytics"
        Directory = Join-Path $PipelineRoot "analytics"
        Files = @(
            "src/duke_rates/analytics/tariff_completeness_audit.py",
            "src/duke_rates/analytics/nc_document_intelligence_audit.py",
            "src/duke_rates/analytics/nc_coverage_assessment.py"
        )
    },
    @{
        Name = "duke-ncuc-workflow"
        Directory = Join-Path $PipelineRoot "ncuc-workflow"
        Files = @(
            "src/duke_rates/historical/ncuc/missing_doc_workflow.py",
            "src/duke_rates/historical/ncuc/missing_clean_doc_search.py",
            "src/duke_rates/historical/ncuc/importer.py",
            "src/duke_rates/historical/ncuc/family_search_terms.py",
            "src/duke_rates/historical/ncuc/discovery.py"
        )
    },
    @{
        Name = "duke-apps"
        Directory = Join-Path $PipelineRoot "apps"
        Files = @(
            "dashboard_views/rate_comparison.py",
            "app/streamlit_rate_comparison_app.py",
            "app/streamlit_res_comparison_app.py"
        )
    },
    @{
        Name = "duke-tests-core"
        Directory = Join-Path $PipelineRoot "tests-core"
        Files = @(
            "tests/test_tariff_engine.py",
            "tests/test_reprocess_queue.py",
            "tests/test_repository.py",
            "tests/test_ncuc_pipeline.py",
            "tests/test_historical_parser_profiles.py"
        )
    }
)

foreach ($mirror in $Mirrors) {
    Sync-Mirror -Name $mirror.Name -Directory $mirror.Directory -Files $mirror.Files
}

foreach ($mirror in $Mirrors) {
    Invoke-GitNexusAnalyze -Name $mirror.Name -Directory $mirror.Directory
}

if (-not $SkipMain) {
    Invoke-GitNexusAnalyze -Name "duke-standalone" -Directory $RepoRoot
} else {
    Write-Host ""
    Write-Host "==> Skipping duke-standalone (-SkipMain)"
}

Write-Host ""
Write-Host "Done. GitNexus repos refreshed:"
Write-Host "  duke-standalone       full project"
foreach ($mirror in $Mirrors) {
    Write-Host ("  {0,-21} {1}" -f $mirror.Name, $mirror.Directory)
}
