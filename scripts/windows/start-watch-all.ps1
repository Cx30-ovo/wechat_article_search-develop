param(
    [int]$AccountsPerVm = 120,
    [int]$PollInterval = 1800,
    [int]$RecentCardLimit = 5,
    [string]$StartAccount = "",
    [int]$MaxAccounts = 0,
    [string]$OutputDir = "output\watch-all"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Python venv not found: $venvPython"
}

$absoluteOutput = Join-Path $projectRoot $OutputDir
$poolState = Join-Path $absoluteOutput "profile-pool-state.json"
if (-not (Test-Path -LiteralPath $poolState)) {
    throw "Profile pool state not found: $poolState`nRun start-watch-prewarm.ps1 first."
}

Write-Host "Incremental watch will start now; do not use the mouse or keyboard." -ForegroundColor Yellow
Write-Host "Output directory: $absoluteOutput" -ForegroundColor Cyan
Write-Host "Poll interval: $PollInterval seconds; recent card limit: $RecentCardLimit" -ForegroundColor Cyan
Write-Host "Stop with Ctrl+C. WeChat tabs are preserved." -ForegroundColor Yellow

$arguments = @(
    "-u",
    "-m",
    "wechat_rpa.runtime.collector_runtime",
    "--watch-accounts-from-mongo",
    "--watch-existing-profile-pool",
    "--live",
    "--local-only",
    "--accounts-per-vm",
    [string]$AccountsPerVm,
    "--poll-interval",
    [string]$PollInterval,
    "--recent-card-limit",
    [string]$RecentCardLimit,
    "--metrics",
    "share",
    "--write-mongo",
    "--window-layout",
    "auto",
    "--output-dir",
    $absoluteOutput
)

if ($StartAccount) {
    $arguments += @("--start-account", $StartAccount)
}
if ($MaxAccounts -gt 0) {
    $arguments += @("--max-accounts", [string]$MaxAccounts)
}

& $venvPython @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Incremental watch exited with code: $LASTEXITCODE"
}
