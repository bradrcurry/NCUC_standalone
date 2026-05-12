param(
    [string]$DeadlineTime = "09:00",  # Stop at this time (24h format)
    [int]$MaxRuntimeMinutes = 55,
    [int]$ExtractLimit = 12,           # docs per extract_staged iteration
    [int]$GroundedLimit = 10,          # docs per generate_grounded_rules iteration
    [int]$SectionsLimit = 50,          # docs per populate_sections iteration
    [int]$BoundaryLimit = 20,          # docs per analyze_document_structure iteration
    [int]$MaxConsecutiveFailures = 15,
    [switch]$ExecutePromotions         # Pass to allow safe promotion after each cycle
)

# Compute deadline - today at $DeadlineTime, or tomorrow if that's already past.
$now = Get-Date
$deadline = [datetime]::ParseExact($DeadlineTime, "HH:mm", $null)
$deadline = (Get-Date).Date.Add($deadline.TimeOfDay)
if ($deadline -le $now) {
    $deadline = $deadline.AddDays(1)
}
$hoursToRun = ($deadline - $now).TotalHours

Write-Host "=================================================================="
Write-Host "Overnight loop - running until $($deadline.ToString('yyyy-MM-dd HH:mm'))"
Write-Host "=================================================================="
Write-Host "Now:        $($now.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "Hours:      $('{0:N2}' -f $hoursToRun)"
Write-Host "Slice cap:  ${MaxRuntimeMinutes}m per iteration"
Write-Host "Limits:     extract=$ExtractLimit grounded=$GroundedLimit sections=$SectionsLimit boundary=$BoundaryLimit"
Write-Host "Promotions: $($ExecutePromotions ? 'execute-safe (after each cycle)' : 'dry-run only (pass -ExecutePromotions to enable)')"
Write-Host "Reports:    docs/reports/overnight_parse_improvement/<run_id>.json"
Write-Host ""

# Seed: refresh diagnostics & identity once before looping. Keeps the
# extract/rules queues fresh as new docs arrive throughout the run.
Write-Host "=== Seed phase ==="
python -m duke_rates.cli run-overnight-parse-improvement-nc `
    --task-kind diagnose,populate_identity,populate_routing_tier,bind_tier1 `
    --limit 500 `
    --rediagnose-unknown
Write-Host "Seed exit code: $LASTEXITCODE"
Write-Host ""

# --- Workload rotation ---
# Each iteration runs ONE focused task so the wall-clock budget is spent
# on the work that has signal, instead of fragmenting across stages.
# Rotation order by priority:
#   1. extract_staged — highest-value path, feeds the promotion funnel directly.
#   2. generate_grounded_rules — builds doc-scoped rules from confirmed extraction
#      rows (much higher acceptance rate than generate_per_doc_rules which is
#      parked until its prompt anchoring is fixed).
#   3. boundary / populate_sections — structural work that improves future extracts.
# generate_per_doc_rules is intentionally excluded: ~72% rejection rate with
# "0 matches" failures; use generate_grounded_rules instead.
$rotation = @(
    @{ name="extract_staged";    tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="extract_staged";    tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="grounded_rules";    tasks="generate_grounded_rules,detect_rule_promotions"; limit=$GroundedLimit },
    @{ name="extract_staged";    tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="boundary";          tasks="populate_sections,analyze_document_structure"; limit=$BoundaryLimit },
    @{ name="extract_staged";    tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="grounded_rules";    tasks="generate_grounded_rules,detect_rule_promotions"; limit=$GroundedLimit }
)

$loopCount = 0
$idleStreak = 0
while ((Get-Date) -lt $deadline) {
    $rotIdx = $loopCount % $rotation.Count
    $stage = $rotation[$rotIdx]
    $loopCount++
    $remaining = [math]::Max(1, [int](($deadline - (Get-Date)).TotalMinutes))
    $slice = [math]::Min($MaxRuntimeMinutes, $remaining)

    Write-Host "--- Loop $loopCount [$($stage.name)] at $(Get-Date), slice=${slice}min ---"
    python -m duke_rates.cli run-overnight-parse-improvement-nc `
        --task-kind $stage.tasks `
        --max-runtime-minutes $slice `
        --limit $stage.limit `
        --max-consecutive-failures $MaxConsecutiveFailures `
        --exit-when-idle

    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 42) {
        $idleStreak++
        Write-Host "Loop ${loopCount}: idle. Streak=$idleStreak"
        # If every stage in a full rotation reports idle, all queues are empty.
        if ($idleStreak -ge $rotation.Count) {
            Write-Host "All $($rotation.Count) stages idle - workload exhausted. Stopping early."
            break
        }
    } else {
        $idleStreak = 0
        if ($exitCode -ne 0) {
            Write-Host "Loop ${loopCount}: exit code $exitCode. Continuing..."
        }
    }

    # After each full rotation cycle, run the promotion funnel.
    if ($rotIdx -eq ($rotation.Count - 1)) {
        Write-Host "--- Promotion pass at $(Get-Date) ---"
        if ($ExecutePromotions) {
            python -m duke_rates.cli run-llm-promotion-overnight-nc `
                --validation-limit 500 `
                --repair-limit 1000 `
                --proposal-limit 10000 `
                --promotion-limit 500 `
                --execute-safe `
                --json
        } else {
            python -m duke_rates.cli run-llm-promotion-overnight-nc `
                --validation-limit 500 `
                --repair-limit 1000 `
                --proposal-limit 10000 `
                --promotion-limit 500 `
                --json
        }
        Write-Host "Promotion pass exit code: $LASTEXITCODE"
    }

    $elapsed = [math]::Round(((Get-Date) - $now).TotalMinutes, 1)
    $remainingMin = [math]::Round(($deadline - (Get-Date)).TotalMinutes, 1)
    Write-Host "Loop $loopCount done. Elapsed: ${elapsed}min. Remaining: ${remainingMin}min. Sleeping 5s..."
    Start-Sleep -Seconds 5
}

Write-Host ""
Write-Host "=================================================================="
Write-Host "Overnight run complete at $(Get-Date)"
Write-Host "Iterations: $loopCount"
Write-Host "=================================================================="
Write-Host ""
Write-Host "Aggregate the results:"
$startStamp = $now.ToString('yyyy-MM-ddTHH:mm:ss')
Write-Host "  python -m duke_rates.cli aggregate-overnight-reports-nc --since $startStamp"
