# Run the Buying Group Ledger scrape for all configured retailers x profiles.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File run.ps1 [retailer ...]
# Invoked by Task Scheduler (see scripts/install_task_windows.ps1). Runs from the project root.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
New-Item -ItemType Directory -Force -Path logs | Out-Null

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

"=== run started $((Get-Date).ToUniversalTime().ToString('o')) ===" | Out-File -FilePath logs\cron.log -Append -Encoding utf8
& $py main.py @args *>> logs\cron.log
"=== run finished $((Get-Date).ToUniversalTime().ToString('o')) ===" | Out-File -FilePath logs\cron.log -Append -Encoding utf8
