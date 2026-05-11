param(
    [int]$DurationHours = 10,
    [int]$MaxRuntimeMinutes = 55,
    [int]$Limit = 50,
    [int]$MaxConsecutiveFailures = 12
)

$deadline = (Get-Date).AddHours($DurationHours)

Write-Host "=== Phase 6 boundary refinement loop ==="
Write-Host "Duration: $DurationHours h, slice cap: ${MaxRuntimeMinutes}m, limit: $Limit"
Write-Host "Deadline: $deadline"
Write-Host "Per-iteration JSON reports: docs/reports/overnight_parse_improvement/<run_id>.json"
Write-Host ""

# populate_sections is idempotent — running it each iteration ensures docs that
# arrive mid-run get section bundles before analyze_document_structure tries
# to refine them.
$loopCount = 0
while ((Get-Date) -lt $deadline) {
    $loopCount++
    $remaining = [math]::Max(1, [int](($deadline - (Get-Date)).TotalMinutes))
    $slice = [math]::Min($MaxRuntimeMinutes, $remaining)

    Write-Host "--- Loop $loopCount at $(Get-Date), slice=${slice}min ---"
    python -m duke_rates.cli run-overnight-parse-improvement-nc `
        --task-kind populate_sections,diagnose,suggest,extract,analyze_document_structure `
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

    $elapsed = [math]::Round(((Get-Date) - ($deadline.AddHours(-$DurationHours))).TotalMinutes, 1)
    Write-Host "Loop $loopCount done. Elapsed: ${elapsed}min. Sleeping 5s..."
    Start-Sleep -Seconds 5
}

Write-Host ""
Write-Host "=== Run complete at $(Get-Date), iterations: $loopCount ==="
