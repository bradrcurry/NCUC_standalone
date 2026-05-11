param(
    [string]$DeadlineTime = "09:00",  # Stop at this time (24h format)
    [int]$MaxRuntimeMinutes = 55,
    [int]$ExtractLimit = 12,           # docs per extract_staged iteration
    [int]$RulesLimit = 8,              # docs per generate_per_doc_rules iteration
    [int]$SectionsLimit = 50,          # docs per populate_sections iteration
    [int]$BoundaryLimit = 20,          # docs per analyze_document_structure iteration
    [int]$MaxConsecutiveFailures = 15
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
Write-Host "Limits:     extract=$ExtractLimit rules=$RulesLimit sections=$SectionsLimit boundary=$BoundaryLimit"
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
# Rotation order is by priority: extract_staged is the highest-value
# pipeline (model-per-stage just optimized), then per-doc rules (largest
# parser-coverage gap), then boundary refinement (helps future extracts),
# then a quick populate_sections refresh.
$rotation = @(
    @{ name="extract_staged"; tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="extract_staged"; tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="per_doc_rules";  tasks="generate_per_doc_rules,detect_rule_promotions"; limit=$RulesLimit },
    @{ name="extract_staged"; tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="boundary";       tasks="populate_sections,analyze_document_structure"; limit=$BoundaryLimit },
    @{ name="extract_staged"; tasks="extract_staged"; limit=$ExtractLimit },
    @{ name="per_doc_rules";  tasks="generate_per_doc_rules,detect_rule_promotions"; limit=$RulesLimit }
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
