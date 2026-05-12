param(
    [int]$DurationHours = 6,
    [int]$MaxRuntimeMinutes = 45,
    [int]$Limit = 15,
    [int]$ExtractLimit = 10,
    [int]$MaxConsecutiveFailures = 15,
    [switch]$ExecutePromotions   # Pass to allow safe promotion at end of each cycle
)

# Compute deadline.
$now = Get-Date
$deadline = $now.AddHours($DurationHours)

Write-Host "=================================================================="
Write-Host "Extraction-grounded rules + staged extract loop"
Write-Host "=================================================================="
Write-Host "Now:        $($now.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "Deadline:   $($deadline.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "Hours:      $('{0:N2}' -f $DurationHours)"
Write-Host "Slice cap:  ${MaxRuntimeMinutes}m"
Write-Host "Limits:     grounded=$Limit, extract_staged=$ExtractLimit"
Write-Host "Promotions: $($ExecutePromotions ? 'execute-safe (after each cycle)' : 'dry-run only (pass -ExecutePromotions to enable)')"
Write-Host "Reports:    docs/reports/overnight_parse_improvement/<run_id>.json"
Write-Host ""

Write-Host "=== Seed phase ==="
python -m duke_rates.cli run-overnight-parse-improvement-nc `
    --task-kind populate_identity,populate_routing_tier,bind_tier1 `
    --limit 500
Write-Host "Seed exit code: $LASTEXITCODE"
Write-Host ""

# Rotation: extract_staged feeds the grounded generator. Now that the
# grounded pool is exhausted (all 6 source_pdfs at cap), we need more
# high-confidence extractions before grounded rules can run. Use 4:2
# extract:grounded ratio so new rows are available each cycle.
$rotation = @(
    @{ name="extract_staged";   tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="extract_staged";   tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="grounded_rules";   tasks="generate_grounded_rules"; limit=$Limit },
    @{ name="extract_staged";   tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="extract_staged";   tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="grounded_rules";   tasks="generate_grounded_rules"; limit=$Limit },
    @{ name="detect_promotions";tasks="detect_rule_promotions"; limit=5 }
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

    # After each full rotation cycle, run the promotion funnel so extraction
    # work lands in tariff_charges without requiring a manual follow-up.
    # Runs dry-run every cycle; only promotes when -ExecutePromotions is set.
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
    Write-Host "Loop $loopCount done. Elapsed: ${elapsed}min. Remaining: ${remainingMin}min."
    Start-Sleep -Seconds 5
}

Write-Host ""
Write-Host "=================================================================="
Write-Host "Grounded-rules loop complete at $(Get-Date), iterations: $loopCount"
Write-Host "=================================================================="
$startStamp = $now.ToString('yyyy-MM-ddTHH:mm:ss')
Write-Host ""
Write-Host "Aggregate results:"
Write-Host "  python -m duke_rates.cli aggregate-overnight-reports-nc --since $startStamp"
