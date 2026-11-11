# Build a clean source release zip (no .venv, no data, no secrets, no logs).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Version = "1.1.0"
$ReleaseDir = Join-Path $Root "release"
$Stage = Join-Path $Root ".release_stage"
$Zip = Join-Path $ReleaseDir "WenLianJia_v$Version.zip"

if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Path $Stage | Out-Null
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null

$items = @(
    "core", "gui", "models", "prompts", "templates", "tests", "docs", "scripts", "samples",
    "main.py", "requirements.txt", "README.md", "pytest.ini", ".env.example", ".gitignore"
)
foreach ($item in $items) {
    $source = Join-Path $Root $item
    if (-not (Test-Path $source)) { throw "Missing release item: $item" }
    Copy-Item $source (Join-Path $Stage $item) -Recurse -Force
}

# Remove caches and accidental local state from the staging copy.
$junk = @("__pycache__", ".pytest_cache", "*.pyc", "*.pyo", "*.db", "*.db-wal", "*.db-shm", "*.bin")
foreach ($pattern in $junk) {
    Get-ChildItem $Stage -Recurse -Force -Filter $pattern -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
Get-ChildItem $Stage -Recurse -Force -Directory |
    Where-Object { $_.Name -in @("__pycache__", ".pytest_cache") } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

if (Test-Path $Zip) { Remove-Item -Force $Zip }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($Stage, $Zip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
Remove-Item -Recurse -Force $Stage

$size = (Get-Item $Zip).Length / 1MB
Write-Host ("Release: {0} ({1:N1} MB)" -f $Zip, $size) -ForegroundColor Green
Write-Host "Verify: Expand-Archive the zip to a clean folder and run scripts\install.ps1."
