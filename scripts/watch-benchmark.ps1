param(
    [int]$RefreshSeconds = 15
)

$taskOutputFile = "$env:TEMP\claude\C--Python-Duke-Standalone\3608dd0a-ef1d-4512-86b7-d7f2440d91d2\tasks\ba15takf4.output"
$targetTotal = 124
$startTime = [DateTime]::Now

Clear-Host
Write-Host "===== Duke Rates Ollama Benchmark Monitor =====" -ForegroundColor Cyan
Write-Host "Target:   $targetTotal model runs"
Write-Host "Refresh:  every ${RefreshSeconds}s"
Write-Host "Watching: $taskOutputFile"
Write-Host "Press Ctrl+C to stop"
Write-Host ""

while ($true) {
    Clear-Host
    Write-Host "===== Duke Rates Ollama Benchmark Monitor =====" -ForegroundColor Cyan
    Write-Host ""

    if (-not (Test-Path $taskOutputFile)) {
        Write-Host "WAITING: Output file not found yet..." -ForegroundColor Yellow
        Start-Sleep -Seconds $RefreshSeconds
        continue
    }

    $lines = Get-Content $taskOutputFile
    $okCalls = ($lines | Select-String -Pattern "HTTP/1.1 200 OK" -SimpleMatch).Count
    $timeouts = ($lines | Select-String -Pattern "HTTP 500" -SimpleMatch).Count
    $httpErrors = ($lines | Select-String -Pattern "HTTP [4-5][0-9][0-9]" -SimpleMatch).Count
    $schemaErrors = ($lines | Select-String -Pattern "validation_error" -SimpleMatch).Count

    # Extract last task header (e.g. "=== Ollama Role Benchmark ===" and "Task:")
    $taskHeaders = $lines | Select-String -Pattern "Task:" -SimpleMatch
    $currentTask = if ($taskHeaders.Count -gt 0) { $taskHeaders[-1].Line.Trim() } else { "starting up..." }

    # Find latest report file
    $reportDir = "docs/reports/ollama_model_benchmarks"
    $reports = @()
    if (Test-Path $reportDir) {
        $reports = Get-ChildItem $reportDir -Filter "*parse_diagnosis*" | Sort-Object LastWriteTime -Descending
    }
    $latestReport = if ($reports.Count -gt 0) { $reports[0].Name } else { "none yet" }

    # Compute timing
    $elapsed = [DateTime]::Now - $startTime
    $runRate = if ($okCalls -gt 0) { $okCalls / $elapsed.TotalMinutes } else { 0 }
    $remaining = if ($runRate -gt 0) { ($targetTotal - $okCalls) / $runRate } else { 99 }
    $remainingStr = if ($remaining -ge 60) { "{0:0}h {1:0}m" -f [Math]::Floor($remaining/60), ($remaining % 60) } else { "{0:0}m" -f $remaining }

    # Progress bar
    $pct = [Math]::Min(100, [Math]::Round($okCalls / $targetTotal * 100))
    $barLen = 40
    $filled = [Math]::Floor($barLen * $pct / 100)
    $bar = ("#" * $filled) + ("." * ($barLen - $filled))

    Write-Host "PROGRESS: [$bar] $pct%" -ForegroundColor Green
    Write-Host ""
    Write-Host ("{0,-30} {1,4} / {2}" -f "Completed runs:", $okCalls, $targetTotal) -ForegroundColor White
    Write-Host ("{0,-30} {1}" -f "Current task:", $currentTask) -ForegroundColor Gray
    Write-Host ("{0,-30} {1}" -f "Latest report:", $latestReport) -ForegroundColor Gray
    Write-Host ""

    # Errors
    if ($timeouts -gt 0 -or $httpErrors -gt 0 -or $schemaErrors -gt 0) {
        Write-Host "ERRORS:" -ForegroundColor Red
        if ($timeouts -gt 0)   { Write-Host ("  HTTP 500s:      {0}" -f $timeouts) -ForegroundColor Red }
        if ($httpErrors -gt 0) { Write-Host ("  HTTP 4xx/5xx:   {0}" -f $httpErrors) -ForegroundColor Red }
        if ($schemaErrors -gt 0) { Write-Host ("  Schema errors:  {0}" -f $schemaErrors) -ForegroundColor Yellow }
        Write-Host ""
    }

    # Timing
    Write-Host ("{0,-30} {1:0.0}m" -f "Elapsed:", $elapsed.TotalMinutes) -ForegroundColor Cyan
    Write-Host ("{0,-30} {1:0.0} runs/min" -f "Rate:", $runRate) -ForegroundColor Cyan
    Write-Host ("{0,-30} {1}" -f "Estimated remaining:", $remainingStr) -ForegroundColor Cyan
    Write-Host ""

    # Latest benchmark summary if available
    if ($reports.Count -gt 0) {
        $reportFile = Join-Path $reportDir $reports[0].Name
        $reportJson = Get-Content $reportFile -Raw | ConvertFrom-Json 2>$null
        if ($reportJson -and $reportJson.summary) {
            Write-Host "Last report summary:" -ForegroundColor Yellow
            $reportJson.summary.PSObject.Properties | ForEach-Object {
                $m = $_.Value
                Write-Host ("  {0,-25} valid={1,5}% action={2,4}%  avg={3,5}ms  conf={4,4}" -f $_.Name, $m.valid_pct, $m.actionable_pct, [Math]::Round($m.avg_duration_ms), $m.avg_confidence)
            }
        }
    }

    # Last 5 log lines (compact)
    Write-Host ""
    Write-Host "Recent activity:" -ForegroundColor DarkGray
    $last5ok = ($lines | Select-String -Pattern "\d{2}:\d{2}:\d{2},\d+ INFO httpx" -SimpleMatch) | Select-Object -Last 5
    foreach ($l in $last5ok) {
        $match = [regex]::Match($l.Line, '(\d{2}:\d{2}:\d{2}),\d+ INFO.*')
        Write-Host ("  {0}" -f $match.Value) -ForegroundColor DarkGray
    }

    # Check if done
    if ($okCalls -ge $targetTotal) {
        Write-Host ""
        Write-Host "BENCHMARK COMPLETE!" -ForegroundColor Green
        exit 0
    }

    Start-Sleep -Seconds $RefreshSeconds
}
