$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$packageReady = $false
if (Test-Path -LiteralPath $venvPython) {
    & $venvPython -c "import wechat_rpa" *> $null
    $packageReady = $LASTEXITCODE -eq 0
}
if (-not $packageReady) {
    Write-Host "Project environment or current package entry is missing. Installing it now..." -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "setup-env.ps1")
}
& $venvPython -c "import wechat_rpa" *> $null
if ($LASTEXITCODE -ne 0) {
    throw "The current wechat_rpa package is not installed. Run setup-env.bat and retry."
}

Write-Host "Starting WeChat RPA control panel..." -ForegroundColor Cyan
Write-Host "The page will open at http://127.0.0.1:8010/" -ForegroundColor Cyan
& $venvPython -m wechat_rpa.runtime.control_panel_runtime
