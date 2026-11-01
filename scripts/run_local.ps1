# Run Predicta on your own Windows PC instead of Railway.
# Usage (PowerShell, from anywhere):   .\scripts\run_local.ps1
# First time: copy .env.example to .env in the repo root and fill in your keys
# (ALPACA_API_KEY, ALPACA_SECRET_KEY, FINNHUB_API_KEY, ANTHROPIC_API_KEY, ...).
# NOT YET RUN ON A REAL WINDOWS MACHINE: written carefully but untested.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".env")) {
    Write-Host "No .env file found. Copy .env.example to .env and add your keys first." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path ".venv")) {
    Write-Host "Creating Python environment (first run only)..."
    python -m venv .venv
}
& .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt --quiet

# Keep the database next to the code unless you set PREDICTA_DB_PATH yourself.
if (-not $env:PORT) { $env:PORT = "8000" }
Write-Host "Predicta is starting at http://127.0.0.1:$($env:PORT)  (leave this window open; Ctrl+C to stop)"
uvicorn app:app --host 127.0.0.1 --port $env:PORT
