# Make Predicta start by itself every time you sign in to Windows.
# Run ONCE from PowerShell:   .\scripts\install_autostart.ps1
# Remove later with:          Unregister-ScheduledTask -TaskName Predicta -Confirm:$false
# NOT YET RUN ON A REAL WINDOWS MACHINE: written carefully but untested.
$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "run_local.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Minimized -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "Predicta" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Done. Predicta will start when you sign in. Also set Windows Power & Sleep to 'Never' while plugged in, or scans will miss the market hours."
