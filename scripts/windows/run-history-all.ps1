param(
    [int]$MaxAccounts = 0,
    [string]$StartAccount = "",
    [string]$Metrics = "all",
    [int]$ResumePages = 0,
    [string]$OutputRoot = "output\history-all"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Python venv not found: $venvPython"
}

$runName = "mongo-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff")
$absoluteOutput = Join-Path $projectRoot (Join-Path $OutputRoot $runName)
New-Item -ItemType Directory -Force -Path $absoluteOutput | Out-Null

Write-Host "History collection mode: scans every visible article until the profile has no more messages." -ForegroundColor Yellow
Write-Host "This is a long-running job. Do not start it while the control panel is running a task." -ForegroundColor Yellow
Write-Host "Output directory: $absoluteOutput" -ForegroundColor Cyan
if ($ResumePages -gt 0) {
    Write-Host "Resume mode: skipping $ResumePages profile pages before continuing." -ForegroundColor Yellow
}

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
    "all",
    "--max-articles",
    "0",
    "--task-timeout-minutes",
    "0",
    "--window-layout",
    "auto",
    "--output-dir",
    $absoluteOutput,
    "--export-jsonl",
    (Join-Path $absoluteOutput "articles.jsonl"),
    "--export-csv",
    (Join-Path $absoluteOutput "articles.csv")
)
if ($StartAccount) {
    $arguments += @("--start-account", $StartAccount)
}
if ($MaxAccounts -gt 0) {
    $arguments += @("--max-accounts", [string]$MaxAccounts)
}
if ($ResumePages -gt 0) {
    $arguments += @("--history-resume-pages", [string]$ResumePages)
}

& $venvPython @arguments
if ($LASTEXITCODE -ne 0) {
    throw "History collection failed with exit code: $LASTEXITCODE"
}
