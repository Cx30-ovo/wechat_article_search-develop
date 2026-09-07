param(
    [int]$AccountsPerVm = 120,
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

Write-Host "Make sure WeChat is logged in and the search window is visible." -ForegroundColor Yellow
Write-Host "Do not use the mouse or keyboard while prewarming." -ForegroundColor Yellow
Write-Host "Output directory: $absoluteOutput" -ForegroundColor Cyan
Write-Host "Accounts per VM: $AccountsPerVm" -ForegroundColor Cyan

if (Test-Path -LiteralPath $poolState) {
    Write-Host "Existing profile pool state detected; prewarm will rescan current tabs." -ForegroundColor Yellow
}

$arguments = @(
    "-u",
    "-m",
    "wechat_rpa.runtime.collector_runtime",
    "--watch-accounts-from-mongo",
    "--bootstrap-profile-pool",
    "--live",
    "--local-only",
    "--accounts-per-vm",
    [string]$AccountsPerVm,
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
    throw "Profile pool prewarm failed with exit code: $LASTEXITCODE"
}

Write-Host "Prewarm finished. Keep WeChat tabs open, then run start-watch-all.ps1." -ForegroundColor Green
