# Creates backend/.venv and installs dev dependencies. Run from anywhere.
$ErrorActionPreference = "Stop"
$backend = Join-Path (Split-Path $PSScriptRoot -Parent) "backend"
Set-Location $backend

if (-not (Test-Path ".venv")) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3.11 -m venv .venv 2>$null }
    if (-not (Test-Path ".venv")) { python -m venv .venv }
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev,aws]"
& .\.venv\Scripts\python.exe -m pytest -q
Write-Host "`nDone. Activate with: backend\.venv\Scripts\Activate.ps1"
