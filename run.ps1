# Launcher: activates nothing, just runs the agent from the project's venv.
# Usage:  .\run.ps1            interactive chat
#         .\run.ps1 --check    verify setup
#         .\run.ps1 --auth     sign in to Google
#         .\run.ps1 -p "..."   one-shot prompt

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "No virtualenv found. Creating one..." -ForegroundColor Yellow
    python -m venv (Join-Path $PSScriptRoot ".venv")
    & $python -m pip install --quiet --upgrade pip
    & $python -m pip install --quiet -r (Join-Path $PSScriptRoot "requirements.txt")
    Write-Host "Dependencies installed." -ForegroundColor Green

    # The browser tools drive a real Chromium, which pip does not bring. ~150 MB,
    # once. Not fatal if it fails: everything else works without it, and
    # `--check` says so.
    Write-Host "Downloading Chromium for the browser tools (about 150 MB)..." -ForegroundColor Yellow
    & $python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Chromium did not install. The browser tools will be unavailable until you run:" -ForegroundColor Yellow
        Write-Host "  .\.venv\Scripts\python.exe -m playwright install chromium" -ForegroundColor Yellow
    }
}

& $python -m agent @args
