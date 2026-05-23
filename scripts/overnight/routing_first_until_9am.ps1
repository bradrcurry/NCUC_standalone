<#
.SYNOPSIS
  Routing-first overnight backlog loop that runs until 09:00 by default.

.DESCRIPTION
  This loop prioritizes the highest-leverage backlog reducers:
    1. Route unknown / weak families into explicit profile-impact queues.
    2. Drain the historical reprocess queue with bounded parallel workers.
    3. Re-measure the routing and workflow state after each cycle.

  It intentionally skips the broader OCR/extraction lanes unless the user
  chooses to add them later, because the current backlog shows better leverage
  from routing synthesis and reprocess drainage than from repeated broad OCR or
  LLM extraction passes.

  The loop runs until the configured deadline (default: 09:00 tomorrow if the
  time has already passed today). It exits early if routing impact is idle and
  the reprocess queue is empty.

.PARAMETER DeadlineTime
  Stop at this clock time (24h HH:mm). Default: 09:00.

.PARAMETER ReprocessLimit
  Max reprocess items to claim per drain pass. Default: 25.

.PARAMETER ReprocessWorkers
  Parallel workers for the reprocess queue. Default: 4.

.PARAMETER RoutingAuditLimit
  Number of unknown-routing families to inspect per cycle. Default: 10.

.PARAMETER ImpactLimit
  Max impacted documents to enqueue per candidate profile. Default: 25.

.PARAMETER LogFile
  Optional log file path. If omitted, writes to docs/reports/overnight.

.EXAMPLE
  pwsh scripts\overnight\routing_first_until_9am.ps1

.EXAMPLE
  pwsh scripts\overnight\routing_first_until_9am.ps1 -DeadlineTime "09:00"
#>
param(
    [string]$DeadlineTime = "09:00",
    [int]$ReprocessLimit = 25,
    [int]$ReprocessWorkers = 4,
    [int]$RoutingAuditLimit = 10,
    [int]$ImpactLimit = 25,
    [string]$LogFile = ""
)

$ErrorActionPreference = "Continue"

$now = Get-Date
$deadline = [datetime]::ParseExact($DeadlineTime, "HH:mm", $null)
$deadline = (Get-Date).Date.Add($deadline.TimeOfDay)
if ($deadline -le $now) {
    $deadline = $deadline.AddDays(1)
}

if ([string]::IsNullOrEmpty($LogFile)) {
    $logDir = Join-Path (Split-Path -Parent $PSScriptRoot) "..\docs\reports\overnight"
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Path $logDir | Out-Null
    }
    $stamp = $now.ToString("yyyyMMdd_HHmmss")
    $LogFile = Join-Path $logDir "routing_first_until_9am_${stamp}.log"
}

function Write-Both {
    param([string]$Message)
    $line = "$(Get-Date -Format 'HH:mm:ss')  $Message"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function Invoke-LoggedPython {
    param(
        [string[]]$Args,
        [int]$Tail = 12
    )
    & python @Args 2>&1 | Select-Object -Last $Tail
}

function Get-WorkflowStatusSnapshot {
    $statusText = Invoke-LoggedPython -Args @("-m", "duke_rates", "show-workflow-status-nc") -Tail 20
    foreach ($line in $statusText) {
        Write-Both "  $line"
    }
}

function Get-UnknownRoutingAuditJson {
    $raw = & python -m duke_rates show-unknown-routing-audit-nc --limit $RoutingAuditLimit --json 2>&1
    $jsonText = ($raw | Out-String)
    return $jsonText | ConvertFrom-Json
}

function Get-CandidateProfilesFromAudit {
    param($Audit)

    $profiles = New-Object System.Collections.Generic.List[string]
    foreach ($row in ($Audit.rows | Sort-Object document_count -Descending)) {
        $profile = [string]$row.synthesized_profile_name
        $kind = [string]$row.synthesized_profile_kind
        $next = [string]$row.synthesized_next_command
        if ([string]::IsNullOrWhiteSpace($profile)) {
            continue
        }
        if ($kind -ne "existing_profile") {
            continue
        }
        if ([string]::IsNullOrWhiteSpace($next)) {
            continue
        }
        if (-not $profiles.Contains($profile)) {
            $profiles.Add($profile) | Out-Null
        }
    }
    return $profiles
}

function Test-ReprocessQueueEmpty {
    $pending = & python -m duke_rates reprocess show-queue-nc --status pending --limit 1 2>&1
    $text = ($pending | Out-String).Trim()
    return [string]::IsNullOrWhiteSpace($text)
}

Write-Both "=================================================================="
Write-Both "Routing-first overnight loop"
Write-Both "=================================================================="
Write-Both "Now:        $($now.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Both "Deadline:   $($deadline.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Both "Hours:      $('{0:N2}' -f (($deadline - $now).TotalHours))"
Write-Both "Routing:    unknown audit synthesis -> profile-impact enqueue"
Write-Both "Drain:      reprocess queue workers=$ReprocessWorkers limit=$ReprocessLimit"
Write-Both "Audit:      top $RoutingAuditLimit families per cycle"
Write-Both "Impact:     up to $ImpactLimit docs per candidate profile"
Write-Both "Log file:   $LogFile"
Write-Both ""

Write-Both "=== Baseline workflow status ==="
Get-WorkflowStatusSnapshot
Write-Both ""

Write-Both "=== Baseline unknown routing audit ==="
$baselineAudit = Get-UnknownRoutingAuditJson
Write-Both "  problem_documents=$($baselineAudit.summary.problem_document_count)"
Write-Both "  problem_families=$($baselineAudit.summary.problem_family_count)"
foreach ($item in ($baselineAudit.summary.recommended_action_counts | Select-Object -First 5)) {
    Write-Both "  action=$($item.recommended_action) count=$($item.count)"
}
Write-Both ""

Write-Both "=== Reconcile stale queue items ==="
& python -m duke_rates reprocess show-stale-nc --limit 10 2>&1 | ForEach-Object { Write-Both "  $_" }
& python -m duke_rates reprocess recover-stale-nc `
    --limit $ReprocessLimit `
    --older-than-minutes 240 `
    --requested-by routing_first_until_9am `
    --execute 2>&1 | ForEach-Object { Write-Both "  $_" }
Write-Both ""

$seenProfiles = New-Object 'System.Collections.Generic.HashSet[string]'
$cycle = 0

while ((Get-Date) -lt $deadline) {
    $cycle++
    $remaining = [math]::Round(($deadline - (Get-Date)).TotalMinutes, 1)
    Write-Both "=== Cycle $cycle remaining=${remaining}m ==="

    $audit = Get-UnknownRoutingAuditJson
    $candidateProfiles = Get-CandidateProfilesFromAudit -Audit $audit
    $enqueuedThisCycle = 0
    if ($candidateProfiles.Count -eq 0) {
        Write-Both "  No synthesized existing-profile candidates found."
    } else {
        foreach ($profile in $candidateProfiles) {
            if ($seenProfiles.Contains($profile)) {
                Write-Both "  seen_profile=$profile skipped"
                continue
            }
            Write-Both "  enqueue_profile_impact=$profile"
            & python -m duke_rates reprocess enqueue-profile-impact-nc `
                --parser-profile $profile `
                --limit $ImpactLimit `
                --requested-by routing_first_until_9am 2>&1 |
                    Select-Object -Last 8 | ForEach-Object { Write-Both "    $_" }
            [void]$seenProfiles.Add($profile)
            $enqueuedThisCycle++
        }
    }

    Write-Both "  reprocess_drain"
    & python -m duke_rates reprocess process-queue-nc `
        --workers $ReprocessWorkers `
        --limit $ReprocessLimit `
        --until-empty 2>&1 | Select-Object -Last 12 | ForEach-Object { Write-Both "    $_" }

    Write-Both "  status_snapshot"
    Get-WorkflowStatusSnapshot

    $queueEmpty = Test-ReprocessQueueEmpty
    if (($enqueuedThisCycle -eq 0) -and $queueEmpty) {
        Write-Both "  idle_condition_met=true"
        break
    }

    if ((Get-Date) -ge $deadline) {
        break
    }

    Start-Sleep -Seconds 20
}

Write-Both ""
Write-Both "=== Run complete at $(Get-Date) ==="
Write-Both "Final workflow status"
Get-WorkflowStatusSnapshot
Write-Both "Final unknown routing audit"
$finalAudit = Get-UnknownRoutingAuditJson
Write-Both "  problem_documents=$($finalAudit.summary.problem_document_count)"
Write-Both "  problem_families=$($finalAudit.summary.problem_family_count)"
