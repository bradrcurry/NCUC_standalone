<#
.SYNOPSIS
  Overnight remediation loop — Docling → OCR → Reprocess → LLM Extract → Promote.

.DESCRIPTION
  Structured in three sequential phases optimised for tonight's specific backlog:

    Phase 1 — Docling + OCR (parallel on GPU/CPU)
      • doc-intel process-docling-batch : 171 NC docs with no Docling artifact (GPU/CUDA)
      • ocr enqueue + drain    : 179 NC docs missing txt sidecars (Tesseract/CPU)
      Both run concurrently; Phase 2 starts once BOTH finish or deadline nears.

    Phase 2 — Reprocess newly unblocked docs
      • reprocess enqueue-stale-nc : queue docs whose Docling artifact just appeared
      • reprocess process-queue-nc : drain the queue (extracts charges from newly
        processed docs using deterministic parser profiles)
      Also promotes the 41 pending LLM proposals at the start of this phase.

    Phase 3 — LLM extract + grounded rules + promote (remaining time)
      • run-overnight-parse-improvement-nc : extract_staged → grounded_rules rotation
        targeting top gap profiles: generic_residential, unknown, carolinas_flat_fee_rider
      • run-llm-promotion-overnight-nc     : promote after each full rotation cycle

  Exits early if all queues go idle before 09:00.

.PARAMETER DeadlineTime
  Stop at this time (HH:mm, 24h). Default: 09:00.

.PARAMETER DoclingLimit
  Max docs for the Docling batch. Default: 200 (covers all 171 + buffer).

.PARAMETER OcrLimit
  Max OCR remediation candidates to enqueue. Default: 200.

.PARAMETER OcrWorkers
  Parallel Tesseract workers. Default: 4.

.PARAMETER ReprocessLimit
  Max items to enqueue per stale-reprocess call. Default: 100.

.PARAMETER ReprocessWorkers
  Parallel reprocess workers. Default: 2.

.PARAMETER ExtractLimit
  Docs per extract_staged iteration. Default: 12.

.PARAMETER GroundedLimit
  Docs per grounded_rules iteration. Default: 10.

.PARAMETER LogFile
  Path for the tail-able log. Auto-generated in docs/reports/overnight/ if empty.

.EXAMPLE
  pwsh scripts\overnight\tonight_9am.ps1
    Runs with defaults until 09:00 tomorrow.

.EXAMPLE
  pwsh scripts\overnight\tonight_9am.ps1 -DeadlineTime "07:00" -DoclingLimit 50
#>
param(
    [string]$DeadlineTime     = "09:00",
    [int]$DoclingLimit        = 200,
    [int]$OcrLimit            = 200,
    [int]$OcrWorkers          = 4,
    [int]$ReprocessLimit      = 100,
    [int]$ReprocessWorkers    = 2,
    [int]$ExtractLimit        = 12,
    [int]$GroundedLimit       = 10,
    [string]$LogFile          = ""
)

$ErrorActionPreference = "Continue"

# ── Deadline ────────────────────────────────────────────────────────────────
$now      = Get-Date
$deadline = [datetime]::ParseExact($DeadlineTime, "HH:mm", $null)
$deadline = (Get-Date).Date.Add($deadline.TimeOfDay)
if ($deadline -le $now) { $deadline = $deadline.AddDays(1) }
$hoursToRun = ($deadline - $now).TotalHours

# ── Logging ─────────────────────────────────────────────────────────────────
if ([string]::IsNullOrEmpty($LogFile)) {
    $logDir = Join-Path $PSScriptRoot "..\..\docs\reports\overnight"
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
    $stamp   = $now.ToString("yyyyMMdd_HHmmss")
    $LogFile = Join-Path $logDir "tonight_9am_${stamp}.log"
}

function Write-Log {
    param([string]$Msg)
    $line = "$(Get-Date -Format 'HH:mm:ss')  $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function Get-DbScalar {
    param([string]$Sql)
    $env:DUKE_RATES_SCALAR_SQL = $Sql
    $val = python -c "import os,sqlite3; from duke_rates.config import get_settings; c=sqlite3.connect(get_settings().database_path); print(c.execute(os.environ['DUKE_RATES_SCALAR_SQL']).fetchone()[0])" 2>&1
    Remove-Item Env:\DUKE_RATES_SCALAR_SQL -ErrorAction SilentlyContinue
    $text = ($val | Select-Object -Last 1).ToString().Trim()
    if ($text -match '^-?\d+$') { return [int]$text }
    Write-Log "  WARNING: scalar SQL non-integer: $val"
    return 0
}

function Minutes-Left { [int](($deadline - (Get-Date)).TotalMinutes) }
function Deadline-Near { (Minutes-Left) -lt 10 }

# ── Banner ───────────────────────────────────────────────────────────────────
Write-Log "=================================================================="
Write-Log "tonight_9am.ps1 — Docling → OCR → Reprocess → LLM → Promote"
Write-Log "=================================================================="
Write-Log "Now:       $($now.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Log "Deadline:  $($deadline.ToString('yyyy-MM-dd HH:mm:ss'))  (~$('{0:N1}' -f $hoursToRun)h)"
Write-Log "Log:       $LogFile"
Write-Log ""

# ── Initial workflow snapshot ────────────────────────────────────────────────
Write-Log "=== Initial workflow status ==="
python -m duke_rates show-workflow-status-nc 2>&1 | ForEach-Object { Write-Log "  $_" }
Write-Log ""

# ── Housekeeping: clear stuck queues ────────────────────────────────────────
Write-Log "=== Housekeeping: recover stale-running items ==="
python -m duke_rates reprocess recover-stale-nc `
    --older-than-minutes 60 --limit 50 --execute 2>&1 |
    Select-Object -Last 4 | ForEach-Object { Write-Log "  $_" }
python -m duke_rates reconcile-workflow-action-receipts-nc --limit 100 2>&1 |
    Select-Object -Last 3 | ForEach-Object { Write-Log "  $_" }
Write-Log ""

# ── Instant win: promote the 41 pending LLM proposals now ───────────────────
Write-Log "=== Quick-win: promote pending LLM proposals ==="
python -m duke_rates run-llm-promotion-overnight-nc `
    --validation-limit 200 --repair-limit 500 `
    --proposal-limit 5000 --promotion-limit 200 `
    --execute-safe --json 2>&1 |
    Select-Object -Last 5 | ForEach-Object { Write-Log "  $_" }
Write-Log ""

# ════════════════════════════════════════════════════════════════════════════
# PHASE 1: Docling (GPU) + OCR (CPU) — run concurrently as background jobs
# ════════════════════════════════════════════════════════════════════════════
Write-Log "=================================================================="
Write-Log "PHASE 1: Docling (GPU) + OCR remediation (CPU) — parallel"
Write-Log "  Docling target: ~171 NC docs with no artifact"
Write-Log "  OCR target:     ~179 NC docs missing txt sidecar"
Write-Log "=================================================================="

# -- Docling job (GPU/CUDA) --------------------------------------------------
$doclingJob = Start-Job -ScriptBlock {
    param($limit, $logFile)
    Set-Location "c:\Python\Duke\Standalone"
    $out = python -m duke_rates doc-intel process-docling-batch `
        --limit $limit `
        --source historical `
        --accelerator cuda 2>&1
    $out | ForEach-Object { Add-Content -Path $logFile -Value "  [DOCLING] $_" -Encoding utf8 }
    return $out | Select-Object -Last 3
} -ArgumentList $DoclingLimit, $LogFile

Write-Log "  Docling job started (job id $($doclingJob.Id))"

# -- OCR remediation job (CPU) -----------------------------------------------
# Brief delay so the two processes don't both hit the DB at the exact same instant.
Start-Sleep -Seconds 3

$ocrJob = Start-Job -ScriptBlock {
    param($limit, $workers, $logFile)
    Set-Location "c:\Python\Duke\Standalone"
    # Enqueue candidates
    $enqOut = python -m duke_rates ocr enqueue-remediation-nc `
        --limit $limit `
        --backend pytesseract_cpu `
        --requested-by tonight_9am `
        --execute 2>&1
    $enqOut | Select-Object -Last 3 | ForEach-Object {
        Add-Content -Path $logFile -Value "  [OCR-ENQ] $_" -Encoding utf8
    }
    # Drain queue
    $procOut = python -m duke_rates ocr process-queue-nc `
        --workers $workers `
        --until-empty 2>&1
    $procOut | Select-Object -Last 3 | ForEach-Object {
        Add-Content -Path $logFile -Value "  [OCR-PROC] $_" -Encoding utf8
    }
    return $procOut | Select-Object -Last 1
} -ArgumentList $OcrLimit, $OcrWorkers, $LogFile

Write-Log "  OCR job started (job id $($ocrJob.Id))"
Write-Log ""

# -- Wait for both jobs, reporting progress every 5 minutes ------------------
$phase1Done = $false
while (-not $phase1Done -and -not (Deadline-Near)) {
    $docDone = $doclingJob.State -in ('Completed','Failed','Stopped')
    $ocrDone  = $ocrJob.State  -in ('Completed','Failed','Stopped')

    if ($docDone -and $ocrDone) {
        $phase1Done = $true
    } else {
        $remaining = Minutes-Left
        Write-Log "  Phase 1 in progress... Docling=$($doclingJob.State) OCR=$($ocrJob.State) | ${remaining}min left"
        Start-Sleep -Seconds 300   # check every 5 min
    }
}

# Collect results
$docResult = Receive-Job $doclingJob -Wait -AutoRemoveJob 2>&1
$ocrResult = Receive-Job $ocrJob     -Wait -AutoRemoveJob 2>&1
Write-Log "  Docling finished: $($docResult | Select-Object -Last 1)"
Write-Log "  OCR finished:     $($ocrResult | Select-Object -Last 1)"

$doclingNow = Get-DbScalar "SELECT COUNT(DISTINCT hd.id) FROM historical_documents hd WHERE hd.state='NC' AND hd.local_path IS NOT NULL AND NOT EXISTS (SELECT 1 FROM docling_artifacts da WHERE da.source_pdf = hd.local_path)"
$ocrNow     = Get-DbScalar "SELECT COUNT(*) FROM historical_documents WHERE state='NC' AND local_path IS NOT NULL AND raw_text_path IS NULL"
Write-Log "  NC docs still needing Docling: $doclingNow"
Write-Log "  NC docs still missing txt:     $ocrNow"
Write-Log ""

if (Deadline-Near) {
    Write-Log "Deadline near after Phase 1 — skipping Phases 2 and 3."
    python -m duke_rates show-workflow-status-nc 2>&1 | ForEach-Object { Write-Log "  $_" }
    exit 0
}

# ════════════════════════════════════════════════════════════════════════════
# PHASE 2: Reprocess newly unblocked docs
# ════════════════════════════════════════════════════════════════════════════
Write-Log "=================================================================="
Write-Log "PHASE 2: Reprocess — extract charges from newly Docling'd docs"
Write-Log "  Targets: docs that now have Docling artifacts but 0 charges"
Write-Log "=================================================================="

# Run up to 3 rounds: enqueue stale → drain → repeat until queue empty or time up
$phase2Round = 0
$phase2MaxRounds = 3

while ($phase2Round -lt $phase2MaxRounds -and -not (Deadline-Near)) {
    $phase2Round++
    Write-Log "--- Phase 2 round $phase2Round ---"

    # Enqueue stale (catches docs with new Docling artifacts since last run)
    python -m duke_rates reprocess enqueue-stale-nc `
        --limit $ReprocessLimit `
        --requested-by tonight_9am 2>&1 |
        Select-Object -Last 5 | ForEach-Object { Write-Log "  $_" }

    $pending = Get-DbScalar "SELECT COUNT(*) FROM historical_reprocess_queue WHERE status IN ('pending','running')"
    Write-Log "  Reprocess queue pending: $pending"

    if ($pending -eq 0) {
        Write-Log "  Reprocess queue empty — Phase 2 done early."
        break
    }

    # Drain queue
    python -m duke_rates reprocess process-queue-nc `
        --workers $ReprocessWorkers `
        --limit ($ReprocessLimit * 2) 2>&1 |
        Select-Object -Last 5 | ForEach-Object { Write-Log "  $_" }

    $chargesNow = Get-DbScalar "SELECT COUNT(*) FROM tariff_charges"
    Write-Log "  tariff_charges after round ${phase2Round}: $chargesNow"
    Write-Log ""
}

# Targeted profile-impact reprocess for the top problem profiles
Write-Log "--- Phase 2: profile-impact reprocess (generic_residential, unknown) ---"
foreach ($profile in @("generic_residential", "unknown", "carolinas_flat_fee_rider")) {
    if (Deadline-Near) { break }
    Write-Log "  Enqueuing profile-impact: $profile"
    python -m duke_rates reprocess enqueue-profile-impact-nc `
        --parser-profile $profile `
        --limit 30 2>&1 |
        Select-Object -Last 3 | ForEach-Object { Write-Log "  $_" }
}
$impactPending = Get-DbScalar "SELECT COUNT(*) FROM historical_reprocess_queue WHERE status='pending'"
if ($impactPending -gt 0) {
    Write-Log "  Draining $impactPending profile-impact items..."
    python -m duke_rates reprocess process-queue-nc `
        --workers $ReprocessWorkers `
        --limit ($impactPending + 20) 2>&1 |
        Select-Object -Last 5 | ForEach-Object { Write-Log "  $_" }
}
Write-Log ""

if (Deadline-Near) {
    Write-Log "Deadline near after Phase 2 — skipping Phase 3."
    python -m duke_rates show-workflow-status-nc 2>&1 | ForEach-Object { Write-Log "  $_" }
    exit 0
}

# ════════════════════════════════════════════════════════════════════════════
# PHASE 3: LLM extract_staged + grounded_rules + promote (remaining time)
# ════════════════════════════════════════════════════════════════════════════
Write-Log "=================================================================="
Write-Log "PHASE 3: LLM extraction + grounded rules + promotion"
Write-Log "  top gap profiles: generic_residential, unknown, carolinas_flat_fee_rider"
Write-Log "=================================================================="

# Seed once: refresh diagnostics + routing so the extract queue is current
Write-Log "--- Phase 3 seed: diagnose + populate_identity + bind_tier1 ---"
python -m duke_rates run-overnight-parse-improvement-nc `
    --task-kind diagnose,populate_identity,populate_routing_tier,bind_tier1 `
    --limit 200 `
    --rediagnose-unknown `
    --max-runtime-minutes 15 2>&1 |
    Select-Object -Last 5 | ForEach-Object { Write-Log "  $_" }
Write-Log ""

# Rotation: 4:2 extract_staged:grounded_rules — same proven ratio as overnight_until_9am
# Boundary/populate_sections dropped: no new docs since Docling already ran.
$rotation = @(
    @{ name="extract";  tasks="extract_staged";                              limit=$ExtractLimit  },
    @{ name="extract";  tasks="extract_staged";                              limit=$ExtractLimit  },
    @{ name="grounded"; tasks="generate_grounded_rules,detect_rule_promotions"; limit=$GroundedLimit },
    @{ name="extract";  tasks="extract_staged";                              limit=$ExtractLimit  },
    @{ name="extract";  tasks="extract_staged";                              limit=$ExtractLimit  },
    @{ name="grounded"; tasks="generate_grounded_rules,detect_rule_promotions"; limit=$GroundedLimit }
)

$loopCount  = 0
$idleStreak = 0

while (-not (Deadline-Near)) {
    $rotIdx  = $loopCount % $rotation.Count
    $stage   = $rotation[$rotIdx]
    $loopCount++
    $slice   = [math]::Min(40, [math]::Max(5, (Minutes-Left) - 5))

    Write-Log "--- P3 loop $loopCount [$($stage.name)] slice=${slice}min | $(Minutes-Left)min left ---"

    python -m duke_rates run-overnight-parse-improvement-nc `
        --task-kind $stage.tasks `
        --limit $stage.limit `
        --max-runtime-minutes $slice `
        --max-consecutive-failures 15 `
        --exit-when-idle 2>&1 |
        Select-Object -Last 5 | ForEach-Object { Write-Log "  $_" }

    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 42) {
        $idleStreak++
        Write-Log "  idle (exit 42). streak=$idleStreak"
        if ($idleStreak -ge $rotation.Count) {
            Write-Log "All $($rotation.Count) stages idle — LLM queue exhausted."
            break
        }
    } else {
        $idleStreak = 0
    }

    # After each full rotation, promote whatever landed in proposals
    if ($rotIdx -eq ($rotation.Count - 1) -and -not (Deadline-Near)) {
        Write-Log "--- P3 promotion pass ---"
        python -m duke_rates run-llm-promotion-overnight-nc `
            --validation-limit 500 --repair-limit 1000 `
            --proposal-limit 10000 --promotion-limit 500 `
            --execute-safe --json 2>&1 |
            Select-Object -Last 5 | ForEach-Object { Write-Log "  $_" }
        $chargesNow = Get-DbScalar "SELECT COUNT(*) FROM tariff_charges"
        Write-Log "  tariff_charges now: $chargesNow"
    }

    Start-Sleep -Seconds 5
}

# ── Final promotion pass ─────────────────────────────────────────────────────
Write-Log ""
Write-Log "=== Final promotion pass ==="
python -m duke_rates run-llm-promotion-overnight-nc `
    --validation-limit 1000 --repair-limit 2000 `
    --proposal-limit 20000 --promotion-limit 2000 `
    --execute --json 2>&1 |
    Select-Object -Last 8 | ForEach-Object { Write-Log "  $_" }

# ── Final workflow snapshot ──────────────────────────────────────────────────
Write-Log ""
Write-Log "=================================================================="
Write-Log "Run complete at $(Get-Date)"
Write-Log "=================================================================="
python -m duke_rates show-workflow-status-nc 2>&1 | ForEach-Object { Write-Log "  $_" }
Write-Log ""
Write-Log "Aggregate results:"
Write-Log "  python -m duke_rates aggregate-overnight-reports-nc --since $($now.ToString('yyyy-MM-ddTHH:mm:ss'))"
