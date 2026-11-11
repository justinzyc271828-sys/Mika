# WenLianJia Windows installer.
# Creates a local virtual environment, installs runtime/test dependencies,
# initializes the database, and runs the environment doctor.
# Does not require administrator rights and does not install TeX Live.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "WenLianJia installer" -ForegroundColor Cyan
Write-Host "Project: $Root"

$Venv = Join-Path $Root ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"

if (Test-Path $Py) {
    $version = & $Py -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
    Write-Host "Using existing virtual environment (Python $version)"
} else {
    $Python = $null
    foreach ($candidate in @("py -3.13", "py -3.12", "py -3.11")) {
        try {
            $parts = $candidate.Split(" ")
            $exe = $parts[0]
            $cargs = @()
            if ($parts.Length -gt 1) { $cargs += $parts[1] }
            $version = & $exe @cargs -c "import sys; print('.'.join(map(str, sys.version_info[:2])))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version) {
                $Python = $candidate
                Write-Host "Using $candidate (Python $version)"
                break
            }
        } catch {
            continue
        }
    }
    if (-not $Python) {
        throw "Python 3.11-3.13 was not found. Install Python 3.13 from python.org (tick 'py launcher'), then rerun this script."
    }
    Write-Host "Creating virtual environment..."
    $parts = $Python.Split(" ")
    $exe = $parts[0]
    $cargs = @()
    if ($parts.Length -gt 1) { $cargs += $parts[1] }
    & $exe @cargs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
}

if (-not (Test-Path $Py)) { throw "Virtual environment python not found: $Py" }

# uv-created venvs may omit pip.  Bootstrap it when necessary.
& $Py -m pip --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Bootstrapping pip..."
    & $Py -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed." }
}

Write-Host "Upgrading pip..."
& $Py -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

Write-Host "Installing requirements (first run downloads OCR models)..."
& $Py -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

Write-Host "Initializing database..."
& $Py (Join-Path $Root "scripts\init_db.py")
if ($LASTEXITCODE -ne 0) { throw "Database initialization failed." }

Write-Host "Running doctor..."
& $Py (Join-Path $Root "scripts\doctor.py")
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Doctor reported missing items. Fix them before a real production run; see docs\KNOWN_ISSUES.md."
}

Write-Host ""
Write-Host "Done. Start with: powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1" -ForegroundColor Green
Write-Host "For real AI/cloud work, open Settings and enter credentials. No credentials are bundled."
