<#
.SYNOPSIS
  Overnight backlog-reduction loop.

.DESCRIPTION
  Round-robin rotation across four backlog-reduction phases:
    1. OCR drain      — enqueue OCR remediation candidates and drain the
                        OCR queue (Tesseract lane).
    2. Stale reprocess — queue stale historical docs for reprocess, then
                         drain the reprocess queue.
    3. Bootstrap      — bootstrap missing tariff_versions for never-processed
                        docs, then run targeted extract.
    4. LLM extract+
       promotion      — run staged extraction, grounded-rule generation, and
                        validated promotion to tariff_charges.

  Each rotation also reconciles stuck "running" queue items (so workers that
  died mid-job don't permanently block the queue).

  The loop runs until the configured deadline (default: 08:00 tomorrow).
  If every phase reports zero work for a full rotation, the loop exits early.

.PARAMETER DeadlineTime
  Stop at this clock time (24h HH:mm). Default: 08:00. If the time is already
  past today, the deadline is set to that time tomorrow.

.PARAMETER MaxSliceMinutes
  Per-phase wall-clock cap (minutes). Default: 30.

.PARAMETER OcrEnqueueLimit
  Max OCR remediation candidates to enqueue per OCR-drain iteration. Default: 50.

.PARAMETER OcrWorkers
  Parallel Tesseract workers. Default: 4. Safe for local file processing.

.PARAMETER ReprocessLimit
  Max stale items to queue per stale-reprocess iteration. Default: 20.

.PARAMETER ReprocessWorkers
  Parallel reprocess-queue workers. Default: 2.

.PARAMETER BootstrapLimit
  Max bootstrap candidates per iteration. Default: 50.

.PARAMETER ExtractLimit
  Docs per LLM staged-extraction iteration. Default: 12.

.PARAMETER GroundedLimit
  Docs per grounded-rule-generation iteration. Default: 10.

.PARAMETER DryRunPromotions
  If set, runs `run-llm-promotion-overnight-nc` without `--execute-safe`.
  Default: --execute-safe is enabled.

.PARAMETER LogFile
  Path to write a tail-able log. Default:
  docs/reports/overnight/backlog_drain_<timestamp>.log

.EXAMPLE
  pwsh scripts\overnight\backlog_drain_overnight.ps1
    Runs with defaults until 08:00 tomorrow.

.EXAMPLE
  pwsh scripts\overnight\backlog_drain_overnight.ps1 -DeadlineTime "06:30"
    Stops at 06:30 instead.
#>
param(
    [string]$DeadlineTime = "08:00",
    [int]$MaxSliceMinutes = 30,
    [int]$OcrEnqueueLimit = 50,
    [int]$OcrWorkers = 4,
    [int]$ReprocessLimit = 20,
    [int]$ReprocessWorkers = 2,
    [int]$BootstrapLimit = 50,
    [int]$ExtractLimit = 12,
    [int]$GroundedLimit = 10,
    [switch]$DryRunPromotions,
    [string]$LogFile = ""
)

$ErrorActionPreference = "Continue"

# Compute deadline — today at $DeadlineTime, or tomorrow if that's already past.
$now = Get-Date
$deadline = [datetime]::ParseExact($DeadlineTime, "HH:mm", $null)
$deadline = (Get-Date).Date.Add($deadline.TimeOfDay)
if ($deadline -le $now) {
    $deadline = $deadline.AddDays(1)
}
$hoursToRun = ($deadline - $now).TotalHours

# Set up logging
if ([string]::IsNullOrEmpty($LogFile)) {
    $logDir = Join-Path (Split-Path -Parent $PSScriptRoot) "..\docs\reports\overnight"
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Path $logDir | Out-Null
    }
    $stamp = $now.ToString("yyyyMMdd_HHmmss")
    $LogFile = Join-Path $logDir "backlog_drain_${stamp}.log"
}

function Write-Both {
    param([string]$Message)
    $line = "$(Get-Date -Format 'HH:mm:ss')  $Message"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function Get-DbScalar {
    param([string]$Sql)

    $previousSql = $env:DUKE_RATES_SCALAR_SQL
    $env:DUKE_RATES_SCALAR_SQL = $Sql
    try {
        $value = & python -c 'import os, sqlite3; from duke_rates.config import get_settings; conn = sqlite3.connect(get_settings().database_path); print(conn.execute(os.environ["DUKE_RATES_SCALAR_SQL"]).fetchone()[0]); conn.close()' 2>&1
    } finally {
        if ($null -eq $previousSql) {
            Remove-Item Env:\DUKE_RATES_SCALAR_SQL -ErrorAction SilentlyContinue
        } else {
            $env:DUKE_RATES_SCALAR_SQL = $previousSql
        }
    }

    $text = ($value | Select-Object -Last 1).ToString().Trim()
    if (-not ($text -match '^-?\d+$')) {
        throw "Scalar SQL returned non-integer output for [$Sql]: $value"
    }
    return [int]$text
}

Write-Both "=================================================================="
Write-Both "Overnight backlog-drain loop"
Write-Both "=================================================================="
Write-Both "Now:        $($now.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Both "Deadline:   $($deadline.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Both "Hours:      $('{0:N2}' -f $hoursToRun)"
Write-Both "Slice cap:  ${MaxSliceMinutes}m per phase"
Write-Both "OCR:        enqueue=$OcrEnqueueLimit workers=$OcrWorkers"
Write-Both "Reprocess:  limit=$ReprocessLimit workers=$ReprocessWorkers"
Write-Both "Bootstrap:  limit=$BootstrapLimit"
Write-Both "LLM:        extract=$ExtractLimit grounded=$GroundedLimit"
Write-Both "Promotions: $($DryRunPromotions ? 'dry-run only' : '--execute')"
Write-Both "Log file:   $LogFile"
Write-Both ""

# ---- Initial workflow status snapshot ----
Write-Both "=== Initial workflow status ==="
$initialStatus = & python -m duke_rates show-workflow-status-nc 2>&1
$initialStatus | ForEach-Object { Write-Both "  $_" }
Write-Both ""

# ---- Reconcile stuck running items once at the start ----
Write-Both "=== Reconciling stuck queue items ==="
$reconcile = & python -m duke_rates reconcile-workflow-action-receipts-nc --limit 100 2>&1
$reconcile | Select-Object -First 5 | ForEach-Object { Write-Both "  $_" }
Write-Both ""

# ---- Recover stale-running reprocess rows once at the start ----
Write-Both "=== Recovering stale-running reprocess rows ==="
$staleRunning = & python -m duke_rates reprocess recover-stale-nc `
    --limit $ReprocessLimit `
    --older-than-minutes 240 `
    --requested-by overnight_backlog_drain `
    --execute 2>&1
$staleRunning | Select-Object -First 8 | ForEach-Object { Write-Both "  $_" }
Write-Both ""

# ---- Phase definitions ----
# Each phase is a script-block that returns the number of "work units" done
# (used to detect idleness). 0 = idle for that phase.

$phaseOcr = {
    Write-Both "--- Phase: OCR drain ---"
    # Enqueue remediation candidates (queue_ocr_or_paddle lane)
    & python -m duke_rates ocr enqueue-remediation-nc `
        --limit $OcrEnqueueLimit `
        --backend pytesseract_cpu `
        --requested-by overnight_backlog_drain `
        --execute 2>&1 | Select-Object -Last 8 | ForEach-Object { Write-Both "  $_" }
    # Drain the queue with bounded workers + until-empty
    & python -m duke_rates ocr process-queue-nc `
        --workers $OcrWorkers `
        --until-empty 2>&1 | Select-Object -Last 5 | ForEach-Object { Write-Both "  $_" }
    # Return queue pending count for idle detection
    $pending = Get-DbScalar "SELECT COUNT(*) FROM ocr_processing_queue WHERE status IN ('pending','running')"
    Write-Both "  ocr_queue_pending_after=$pending"
    return [int]$pending
}

$phaseStale = {
    Write-Both "--- Phase: Stale reprocess ---"
    & python -m duke_rates reprocess enqueue-stale-nc `
        --limit $ReprocessLimit `
        --requested-by overnight_backlog_drain 2>&1 | Select-Object -Last 5 | ForEach-Object { Write-Both "  $_" }
    & python -m duke_rates reprocess process-queue-nc `
        --workers $ReprocessWorkers `
        --limit ($ReprocessLimit * 2) 2>&1 | Select-Object -Last 5 | ForEach-Object { Write-Both "  $_" }
    # Cheap proxy: count pending+running items in historical_reprocess_queue
    $pendingReprocess = Get-DbScalar "SELECT COUNT(*) FROM historical_reprocess_queue WHERE status IN ('pending','running')"
    Write-Both "  reprocess_queue_pending=$pendingReprocess"
    return [int]$pendingReprocess
}

$phaseBootstrap = {
    Write-Both "--- Phase: Bootstrap never-processed ---"
    $missingVersionsBefore = Get-DbScalar "SELECT COUNT(*) FROM historical_documents hd WHERE hd.state = 'NC' AND hd.effective_start IS NOT NULL AND hd.local_path IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tariff_versions tv WHERE tv.historical_document_id = hd.id)"
    & python -m duke_rates bootstrap-missing-versions-nc `
        --limit $BootstrapLimit 2>&1 | Select-Object -Last 5 | ForEach-Object { Write-Both "  $_" }
    $missingVersionsAfter = Get-DbScalar "SELECT COUNT(*) FROM historical_documents hd WHERE hd.state = 'NC' AND hd.effective_start IS NOT NULL AND hd.local_path IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tariff_versions tv WHERE tv.historical_document_id = hd.id)"
    if ($missingVersionsBefore -gt $missingVersionsAfter) {
        # Only pay the expensive extraction cost when bootstrap actually linked new versions.
        & python -m duke_rates extract-rates-nc 2>&1 | Select-Object -Last 5 | ForEach-Object { Write-Both "  $_" }
    } else {
        Write-Both "  no newly bootstrapped versions; skipping extract-rates-nc"
    }
    $nullEffectiveStart = Get-DbScalar "SELECT COUNT(*) FROM historical_documents WHERE state = 'NC' AND effective_start IS NULL"
    Write-Both "  missing_versions_remaining=$missingVersionsAfter"
    Write-Both "  null_effective_start_remaining=$nullEffectiveStart"
    if ($nullEffectiveStart -gt 0) {
        Write-Both "  note: null effective-start rows need workflow remediate-nc-missing-doc-effective-start, not bootstrap"
    }
    return [int]$missingVersionsAfter
}

$phaseLlm = {
    Write-Both "--- Phase: LLM extract + grounded rules + promotion ---"
    $slice = [math]::Min($MaxSliceMinutes, [int](($deadline - (Get-Date)).TotalMinutes))
    if ($slice -lt 5) { $slice = 5 }
    # Two extract slices, then grounded rules + sections, then promotion
    & python -m duke_rates run-overnight-parse-improvement-nc `
        --task-kind extract_staged `
        --limit $ExtractLimit `
        --max-runtime-minutes ([math]::Floor($slice / 2)) `
        --max-consecutive-failures 15 `
        --exit-when-idle 2>&1 | Select-Object -Last 5 | ForEach-Object { Write-Both "  $_" }
    & python -m duke_rates run-overnight-parse-improvement-nc `
        --task-kind generate_grounded_rules,detect_rule_promotions `
        --limit $GroundedLimit `
        --max-runtime-minutes ([math]::Floor($slice / 3)) `
        --max-consecutive-failures 15 `
        --exit-when-idle 2>&1 | Select-Object -Last 5 | ForEach-Object { Write-Both "  $_" }
    # Promotion pass
    if ($DryRunPromotions) {
        & python -m duke_rates run-llm-promotion-overnight-nc `
            --validation-limit 500 --repair-limit 1000 `
            --proposal-limit 10000 --promotion-limit 500 --json 2>&1 |
                Select-Object -Last 3 | ForEach-Object { Write-Both "  $_" }
    } else {
        & python -m duke_rates run-llm-promotion-overnight-nc `
            --validation-limit 500 --repair-limit 1000 `
            --proposal-limit 10000 --promotion-limit 500 --execute --json 2>&1 |
                Select-Object -Last 3 | ForEach-Object { Write-Both "  $_" }
    }
    # Cheap idle proxy: count pending llm_rate_charge_promotion_proposals
    $pending = Get-DbScalar "SELECT COUNT(*) FROM llm_rate_charge_promotion_proposals WHERE promotion_status = 'pending'"
    Write-Both "  llm_proposals_pending=$pending"
    return [int]$pending
}

$rotation = @(
    @{ name="ocr_drain";      block=$phaseOcr },
    @{ name="stale_reprocess"; block=$phaseStale },
    @{ name="bootstrap";       block=$phaseBootstrap },
    @{ name="llm_extract_promote"; block=$phaseLlm }
)

$loopCount = 0
$idleStreak = 0
$startTime = Get-Date

while ((Get-Date) -lt $deadline) {
    $rotIdx = $loopCount % $rotation.Count
    $phase = $rotation[$rotIdx]
    $loopCount++
    $remaining = [int](($deadline - (Get-Date)).TotalMinutes)
    Write-Both ""
    Write-Both "=== Loop $loopCount [phase=$($phase.name)] remaining=${remaining}min ==="

    try {
        $workUnits = & $phase.block
        if ([int]$workUnits -eq 0) {
            $idleStreak++
            Write-Both "Phase $($phase.name): idle. Streak=$idleStreak"
            if ($idleStreak -ge $rotation.Count) {
                Write-Both "All $($rotation.Count) phases idle — workload exhausted. Stopping early."
                break
            }
        } else {
            $idleStreak = 0
            Write-Both "Phase $($phase.name) done. work_units=$workUnits"
        }
    } catch {
        Write-Both "Phase $($phase.name) ERROR: $_"
        $idleStreak = 0
    }

    $elapsed = [math]::Round(((Get-Date) - $startTime).TotalMinutes, 1)
    $remainingMin = [math]::Round(($deadline - (Get-Date)).TotalMinutes, 1)
    Write-Both "Loop $loopCount complete. Elapsed: ${elapsed}min. Remaining: ${remainingMin}min."

    # Short breather between phases so SQLite locks fully release.
    Start-Sleep -Seconds 5
}

Write-Both ""
Write-Both "=================================================================="
Write-Both "Overnight run complete at $(Get-Date)"
Write-Both "Total iterations: $loopCount"
Write-Both "=================================================================="

# Final workflow status snapshot
Write-Both ""
Write-Both "=== Final workflow status ==="
$finalStatus = & python -m duke_rates show-workflow-status-nc 2>&1
$finalStatus | ForEach-Object { Write-Both "  $_" }
