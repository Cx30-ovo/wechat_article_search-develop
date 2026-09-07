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

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outputDir = Join-Path $projectRoot "output\mongo-$stamp"

Write-Host "Confirm that WeChat is logged in and the Search window is visible." -ForegroundColor Yellow
Write-Host "Do not use the mouse or keyboard while the RPA is running." -ForegroundColor Yellow
Write-Host "Output directory: $outputDir" -ForegroundColor Cyan
if (Test-Path -LiteralPath (Join-Path $projectRoot ".env")) {
    Write-Host "Project configuration: .env (loaded by Python)" -ForegroundColor Cyan
} else {
    Write-Host "Project configuration: .env not found; built-in defaults/system variables will be used." -ForegroundColor Yellow
}

$arguments = @(
    "-u", "-m", "wechat_rpa.runtime.collector_runtime",
    "--run-search-accounts",
    "--live",
    "--accounts-from-mongo",
    "--write-mongo",
    "--metrics", "share",
    "--window-layout", "auto",
    "--max-articles", "20",
    "--output-dir", $outputDir,
    "--export-jsonl", (Join-Path $outputDir "articles.jsonl"),
    "--export-csv", (Join-Path $outputDir "articles.csv")
)

& $venvPython @arguments
if ($LASTEXITCODE -ne 0) {
    throw "The collector exited with code $LASTEXITCODE. Check run.log in the output directory."
}
