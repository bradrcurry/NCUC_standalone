param(
    [int]$DurationHours = 8,
    [int]$MaxRuntimeMinutes = 55,
    [int]$Limit = 10,
    [int]$MaxConsecutiveFailures = 12
)

$deadline = (Get-Date).AddHours($DurationHours)

Write-Host "=== Staged extraction loop (Phase 6E) ==="
Write-Host "Duration: $DurationHours h, slice cap: ${MaxRuntimeMinutes}m, limit: $Limit"
Write-Host "Deadline: $deadline"
Write-Host "Pipeline: filter -> find-lines -> classify-per-line"
Write-Host "Per-iteration JSON reports: docs/reports/overnight_parse_improvement/<run_id>.json"
Write-Host ""

# Seed phase: refresh diagnose to find new candidate parse attempts.
Write-Host "=== Seed phase (diagnose) ==="
python -m duke_rates.cli run-overnight-parse-improvement-nc `
    --task-kind diagnose `
    --limit 200 `
    --rediagnose-unknown
Write-Host "Seed exit code: $LASTEXITCODE"
Write-Host ""

Write-Host "=== Staged extraction loop ==="
$loopCount = 0
while ((Get-Date) -lt $deadline) {
    $loopCount++
    $remaining = [math]::Max(1, [int](($deadline - (Get-Date)).TotalMinutes))
    $slice = [math]::Min($MaxRuntimeMinutes, $remaining)

    Write-Host "--- Loop $loopCount at $(Get-Date), slice=${slice}min ---"
    python -m duke_rates.cli run-overnight-parse-improvement-nc `
        --task-kind extract_staged `
        --max-runtime-minutes $slice `
        --limit $Limit `
        --max-consecutive-failures $MaxConsecutiveFailures `
        --exit-when-idle

    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 42) {
        Write-Host "Loop ${loopCount}: idle. Breaking."
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
Write-Host "Tip: run 'python -m duke_rates.cli aggregate-overnight-reports-nc --since <date>'"
Write-Host "     to summarize extraction results across iterations."
