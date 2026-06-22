# Predicta — PowerShell setup script
# Run from the project root: .\setup.ps1

$ErrorActionPreference = "Stop"

Write-Host "Creating virtual environment..." -ForegroundColor Cyan
python -m venv .venv

Write-Host "Activating virtual environment..." -ForegroundColor Cyan
.\.venv\Scripts\Activate.ps1

Write-Host "Installing dependencies..." -ForegroundColor Cyan
pip install -r requirements.txt

Write-Host "Initialising database..." -ForegroundColor Cyan
python cli.py init

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "Quick start:" -ForegroundColor Yellow
Write-Host "  Start API:      uvicorn app:app --reload"
Write-Host "  Add a match:    python cli.py add-match --sport soccer --a 'Team A' --b 'Team B' --date 2024-06-01T15:00:00"
Write-Host "  Run prediction: python cli.py predict --match-id 1"
Write-Host "  Open docs:      http://127.0.0.1:8000/docs"
