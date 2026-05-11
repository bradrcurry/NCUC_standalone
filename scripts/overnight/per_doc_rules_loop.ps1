param(
    [int]$DurationHours = 1,
    [int]$MaxRuntimeMinutes = 55,
    [int]$Limit = 10,
    [int]$MaxConsecutiveFailures = 12
)

$deadline = (Get-Date).AddHours($DurationHours)

Write-Host "=== Per-doc rules loop starting at $(Get-Date) ==="
Write-Host "Duration: $DurationHours h, slice cap: ${MaxRuntimeMinutes}m, limit: $Limit"
Write-Host "Deadline: $deadline"
Write-Host "Per-iteration JSON reports: docs/reports/overnight_parse_improvement/<run_id>.json"
Write-Host ""

Write-Host "=== Seed phase (diagnose + populate + bind) ==="
python -m duke_rates.cli run-overnight-parse-improvement-nc `
    --task-kind diagnose,populate_identity,populate_routing_tier,bind_tier1 `
    --limit 500 `
    --rediagnose-unknown
Write-Host "Seed exit code: $LASTEXITCODE"
Write-Host ""

Write-Host "=== Loop (per-doc rules + promotions) ==="
$loopCount = 0
while ((Get-Date) -lt $deadline) {
    $loopCount++
    $remaining = [math]::Max(1, [int](($deadline - (Get-Date)).TotalMinutes))
    $slice = [math]::Min($MaxRuntimeMinutes, $remaining)

    Write-Host "--- Loop $loopCount at $(Get-Date), slice=${slice}min ---"
    python -m duke_rates.cli run-overnight-parse-improvement-nc `
        --task-kind generate_per_doc_rules,detect_rule_promotions `
        --max-runtime-minutes $slice `
        --limit $Limit `
        --max-consecutive-failures $MaxConsecutiveFailures `
        --exit-when-idle

    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 42) {
        Write-Host "Loop ${loopCount}: idle (no more work). Breaking."
        break
    }
    if ($exitCode -ne 0) {
        Write-Host "Loop ${loopCount}: exit code $exitCode. Continuing..."
    }

    Write-Host "Loop $loopCount done. Sleeping 5s..."
    Start-Sleep -Seconds 5
}

Write-Host ""
Write-Host "=== Run complete at $(Get-Date), iterations: $loopCount ==="
