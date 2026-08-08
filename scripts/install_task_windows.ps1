# Register a Windows Scheduled Task that runs the scrape every N hours (default 6 => 4x/day).
# Run in an ELEVATED PowerShell:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1 -Hours 6
# Re-run with a different -Hours to change the interval.
param([int]$Hours = 6)
$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $projectDir "run.ps1"
$taskName = "BuyingGroupLedger"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`"" `
    -WorkingDirectory $projectDir

# Repeat every N hours, indefinitely, starting a minute from now.
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Hours $Hours) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# IgnoreNew = don't start a second run if one is still going (complements main.py's lockfile).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Output "Registered scheduled task '$taskName' to run every $Hours hours."
Write-Output "Logs: $projectDir\logs\cron.log and logs\run.log"
Write-Output "Inspect/disable in Task Scheduler, or remove with: Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
