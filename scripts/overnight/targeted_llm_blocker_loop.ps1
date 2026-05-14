param(
    [int]$DurationHours = 1,
    [int]$MaxRuntimeMinutes = 15,
    [int]$ExtractLimit = 10,
    [int]$ValidationLimit = 200,
    [int]$EvidenceLimit = 50,
    [int]$ConflictLimit = 50,
    [int]$RepairLimit = 200,
    [int]$ProposalLimit = 10000,
    [int]$PromotionLimit = 500,
    [switch]$ExecutePromotions
)

$deadline = (Get-Date).AddHours($DurationHours)
$profiles = @('progress_single_value_rider', 'generic_residential')
$routingProfiles = @(
    'progress_single_value_rider',
    'generic_residential',
    'zero_charge_program',
    'progress_current_leaf_bridge'
)
$loopCount = 0

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$logDir = Join-Path (Split-Path -Parent $PSScriptRoot) "..\docs\reports\overnight"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$logFile = Join-Path $logDir "llm_targeted_blocker_loop_${stamp}.log"

function Write-Both {
    param([string]$Message)
    $line = "$(Get-Date -Format 'HH:mm:ss')  $Message"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

Write-Both "=================================================================="
Write-Both "Targeted LLM blocker loop"
Write-Both "=================================================================="
Write-Both "Now:        $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Both "Deadline:   $($deadline.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Both "Hours:      $('{0:N2}' -f ($deadline - (Get-Date)).TotalHours)"
Write-Both "Profiles:   $($profiles -join ', ')"
Write-Both "Routing:    $($routingProfiles -join ', ')"
Write-Both "Slice cap:  ${MaxRuntimeMinutes}m per extract pass"
Write-Both "Limits:     extract=$ExtractLimit validation=$ValidationLimit evidence=$EvidenceLimit conflict=$ConflictLimit repair=$RepairLimit"
Write-Both "Promotion:  $($ExecutePromotions ? 'execute on promotion pass' : 'dry-run only')"
Write-Both "Log file:   $logFile"
Write-Both ""

Write-Both "=== Baseline workflow status ==="
python -m duke_rates show-workflow-status-nc 2>&1 | ForEach-Object { Write-Both "  $_" }
Write-Both ""
Write-Both "=== Baseline parser improvement candidates ==="
python -m duke_rates show-parser-improvement-candidates-nc --limit 10 2>&1 | ForEach-Object { Write-Both "  $_" }
Write-Both ""
Write-Both "=== Baseline near-miss profiles ==="
python -m duke_rates show-near-miss-profiles-nc --limit 10 2>&1 | ForEach-Object { Write-Both "  $_" }
Write-Both ""
Write-Both "=== Baseline unknown routing audit ==="
python -m duke_rates show-unknown-routing-audit-nc --limit 10 2>&1 | ForEach-Object { Write-Both "  $_" }
Write-Both ""
Write-Both "=== Baseline LLM effective status ==="
python -m duke_rates show-llm-row-effective-status-nc --json 2>&1 | ForEach-Object { Write-Both "  $_" }
Write-Both ""

while ((Get-Date) -lt $deadline) {
    $profile = $profiles[$loopCount % $profiles.Count]
    $loopCount++
    $remaining = [math]::Round(($deadline - (Get-Date)).TotalMinutes, 1)

    Write-Both "=== Cycle $loopCount routing-first profile=$profile remaining=${remaining}m ==="
    Write-Both "Routing diagnostics"
    python -m duke_rates show-parser-improvement-candidates-nc --limit 10 2>&1 | ForEach-Object { Write-Both "  $_" }
    python -m duke_rates show-near-miss-profiles-nc --limit 10 2>&1 | ForEach-Object { Write-Both "  $_" }
    python -m duke_rates show-unknown-routing-audit-nc --limit 10 2>&1 | ForEach-Object { Write-Both "  $_" }

    Write-Both "Stale reprocess recovery"
    python -m duke_rates show-stale-reprocess-nc --limit 10 2>&1 | ForEach-Object { Write-Both "  $_" }
    python -m duke_rates recover-stale-reprocess-nc `
        --limit 10 `
        --older-than-minutes 240 `
        --requested-by targeted_llm_blocker_loop `
        --execute 2>&1 | ForEach-Object { Write-Both "  $_" }

    Write-Both "Profile impact enqueue"
    foreach ($routingProfile in $routingProfiles) {
        python -m duke_rates enqueue-profile-impact-nc `
            --parser-profile $routingProfile `
            --limit 25 `
            --requested-by targeted_llm_blocker_loop 2>&1 | ForEach-Object { Write-Both "  $_" }
    }

    Write-Both "Profile impact drain"
    python -m duke_rates process-reprocess-queue-nc `
        --limit 25 `
        --workers 4 2>&1 | Select-Object -Last 20 | ForEach-Object { Write-Both "  $_" }

    Write-Both "Targeted extraction"
    python -m duke_rates run-overnight-parse-improvement-nc `
        --task-kind extract_staged `
        --max-runtime-minutes $MaxRuntimeMinutes `
        --limit $ExtractLimit `
        --resume `
        --auto-rediagnose-unknown `
        --profile $profile 2>&1 | Select-Object -Last 20 | ForEach-Object { Write-Both "  $_" }

    if ((Get-Date) -ge $deadline) {
        break
    }

    Write-Both "Validation refresh"
    python -m duke_rates validate-llm-rate-extractions-nc `
        --limit $ValidationLimit `
        --execute 2>&1 | Select-Object -Last 20 | ForEach-Object { Write-Both "  $_" }

    if ((Get-Date) -ge $deadline) {
        break
    }

    Write-Both "Evidence refresh"
    python -m duke_rates locate-llm-row-evidence-nc `
        --issue unit_missing `
        --limit $EvidenceLimit `
        --execute 2>&1 | Select-Object -Last 20 | ForEach-Object { Write-Both "  $_" }

    if ((Get-Date) -ge $deadline) {
        break
    }

    Write-Both "Conflict refresh"
    python -m duke_rates reclassify-llm-row-conflicts-nc `
        --limit $ConflictLimit `
        --execute 2>&1 | Select-Object -Last 20 | ForEach-Object { Write-Both "  $_" }

    if ((Get-Date) -ge $deadline) {
        break
    }

    Write-Both "Deterministic repair refresh"
    python -m duke_rates apply-deterministic-llm-row-repairs-nc `
        --limit $RepairLimit `
        --execute 2>&1 | Select-Object -Last 20 | ForEach-Object { Write-Both "  $_" }

    if ((Get-Date) -ge $deadline) {
        break
    }

    Write-Both "Proposal refresh"
    python -m duke_rates propose-llm-charge-promotions-nc `
        --limit $ProposalLimit `
        --refresh-existing `
        --json 2>&1 | Select-Object -Last 12 | ForEach-Object { Write-Both "  $_" }

    if ((Get-Date) -ge $deadline) {
        break
    }

    Write-Both "Promotion dry-run"
    if ($ExecutePromotions) {
        python -m duke_rates promote-llm-charge-proposals-nc `
            --limit $PromotionLimit `
            --execute `
            --json 2>&1 | Select-Object -Last 12 | ForEach-Object { Write-Both "  $_" }
    } else {
        python -m duke_rates promote-llm-charge-proposals-nc `
            --limit $PromotionLimit `
            --json 2>&1 | Select-Object -Last 12 | ForEach-Object { Write-Both "  $_" }
    }

    if ((Get-Date) -ge $deadline) {
        break
    }

    Write-Both "Status snapshot"
    python -m duke_rates show-llm-row-effective-status-nc --json 2>&1 | Select-Object -Last 20 | ForEach-Object { Write-Both "  $_" }

    $elapsed = [math]::Round(((Get-Date) - ($deadline.AddHours(-$DurationHours))).TotalMinutes, 1)
    $remainingMin = [math]::Round(($deadline - (Get-Date)).TotalMinutes, 1)
    Write-Both "Cycle $loopCount complete. Elapsed: ${elapsed}min. Remaining: ${remainingMin}min."
    Start-Sleep -Seconds 5
}

Write-Both ""
Write-Both "=== Run complete at $(Get-Date), cycles: $loopCount ==="
Write-Both "Final workflow status"
python -m duke_rates show-workflow-status-nc 2>&1 | ForEach-Object { Write-Both "  $_" }
Write-Both "Final LLM effective status"
python -m duke_rates show-llm-row-effective-status-nc --json 2>&1 | ForEach-Object { Write-Both "  $_" }
