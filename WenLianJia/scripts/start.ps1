# Start the WenLianJia desktop application from the repository root.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "Virtual environment not found. Run scripts\install.ps1 first."
}

Write-Host "Starting WenLianJia..."
& $Py (Join-Path $Root "main.py")
exit $LASTEXITCODE
