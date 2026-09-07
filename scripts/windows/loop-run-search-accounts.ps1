param(
    [int]$MaxAccounts = 0,
    [string]$StartAccount = "",
    [int]$MaxArticles = 20,
    [string]$Metrics = "all",
    [string]$ScanRange = "today_yesterday",
    [int]$GapSeconds = 30,
    [string]$OutputRoot = "output\stability-loop",
    [switch]$StopOnFailure
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Python venv not found: $venvPython"
}

Write-Host "Stability loop mode: runs the full account batch repeatedly." -ForegroundColor Yellow
Write-Host "Do not start this while the control panel is running a manual/scheduled batch." -ForegroundColor Yellow
Write-Host "Output root: $OutputRoot" -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop between rounds." -ForegroundColor Cyan

$round = 0
while ($true) {
    $round += 1
    $roundName = "mongo-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff")
    $absoluteOutput = Join-Path $projectRoot (Join-Path $OutputRoot $roundName)
    New-Item -ItemType Directory -Force -Path $absoluteOutput | Out-Null

    Write-Host ""
    Write-Host "Round $round started: $roundName" -ForegroundColor Green

    $arguments = @(
        "-u",
        "-m",
        "wechat_rpa.runtime.collector_runtime",
        "--run-search-accounts",
        "--live",
        "--accounts-from-mongo",
        "--write-mongo",
        "--metrics",
        $Metrics,
        "--scan-range",
        $ScanRange,
        "--window-layout",
        "auto",
        "--max-articles",
        [string]$MaxArticles,
        "--output-dir",
        $absoluteOutput,
        "--export-jsonl",
        (Join-Path $absoluteOutput "articles.jsonl"),
        "--export-csv",
        (Join-Path $absoluteOutput "articles.csv"),
        "--stop-after-known-url"
    )
    if ($StartAccount) {
        $arguments += @("--start-account", $StartAccount)
    }
    if ($MaxAccounts -gt 0) {
        $arguments += @("--max-accounts", [string]$MaxAccounts)
    }

    & $venvPython @arguments
    $exitCode = $LASTEXITCODE
    Write-Host "Round $round finished with exit code $exitCode" -ForegroundColor Green

    if ($StopOnFailure -and $exitCode -ne 0) {
        throw "Round $round failed with exit code $exitCode; stop requested."
    }
    if ($GapSeconds -gt 0) {
        Write-Host "Waiting $GapSeconds seconds before the next round..." -ForegroundColor Cyan
        Start-Sleep -Seconds $GapSeconds
    }
}
