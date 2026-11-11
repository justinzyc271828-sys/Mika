# Run the automated suite and the deterministic pipeline smoke test.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "Virtual environment not found. Run scripts\install.ps1 first."
}

Write-Host "pytest..." -ForegroundColor Cyan
& $Py -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "pipeline smoke..." -ForegroundColor Cyan
& $Py (Join-Path $Root "tests\pipeline_smoke.py")
exit $LASTEXITCODE
