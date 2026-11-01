# Run Predicta on your own Windows PC and use it from your browser.
# Easiest: double-click "Start Predicta.bat" in the repo folder (it runs this file).
# Or from PowerShell:   .\scripts\run_local.ps1
# First time only: copy .env.example to .env in the repo folder and fill in your keys
# (ALPACA_API_KEY, ALPACA_SECRET_KEY, FINNHUB_API_KEY, ANTHROPIC_API_KEY, ...).
# NOT YET RUN ON A REAL WINDOWS MACHINE: written carefully but untested.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python was not found. Install it from python.org (tick 'Add python.exe to PATH'), then run this again." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path ".env")) {
    Write-Host "No .env file found. Copy .env.example to .env and add your keys first." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path ".venv")) {
    Write-Host "Setting things up (first run only, takes a few minutes)..."
    python -m venv .venv
}
& .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt --quiet

if (-not $env:PORT) { $env:PORT = "8000" }
$url = "http://127.0.0.1:$($env:PORT)/trading"

# Open the browser a few seconds after the server starts.
Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 8; Start-Process $u } -ArgumentList $url | Out-Null

Write-Host ""
Write-Host "Predicta is starting. Your browser will open $url" -ForegroundColor Green
Write-Host "KEEP THIS WINDOW OPEN while you want Predicta to run (the daily scans happen inside it). Ctrl+C to stop." -ForegroundColor Green
Write-Host ""
uvicorn app:app --host 127.0.0.1 --port $env:PORT
