# Run the Buying Group Ledger scrape for all configured retailers x profiles.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File run.ps1 [retailer ...]
# Invoked by Task Scheduler (see scripts/install_task_windows.ps1). Runs from the project root.
#
# Kept behaviourally identical to run.sh and docker/run_once.sh: it always records how a run ENDED,
# rotates its log, and stamps the same logs/.last_run heartbeat — so a Windows host is diagnosable
# the same way as a Linux one.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
New-Item -ItemType Directory -Force -Path logs | Out-Null

$log = "logs\cron.log"
$stamp = "logs\.last_run"
$maxLogBytes = 10MB

# Rotate before appending. Unbounded, this file eventually fills the disk months later, long after
# anyone is watching.
if ((Test-Path $log) -and ((Get-Item $log).Length -gt $maxLogBytes)) {
    Move-Item -Force $log "$log.1"
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

"=== run started $((Get-Date).ToUniversalTime().ToString('o')) ===" | Out-File -FilePath $log -Append -Encoding utf8
& $py main.py @args *>> $log
$status = $LASTEXITCODE
"=== run finished $((Get-Date).ToUniversalTime().ToString('o')) (exit $status) ===" | Out-File -FilePath $log -Append -Encoding utf8

# Stamped whatever the outcome: this answers "is the scheduler alive?", not "did the run succeed?".
# main.py already alerts per-retailer on failure, and conflating the two would make one failing
# retailer look like a dead scheduler.
(Get-Date).ToUniversalTime().ToString('o') | Out-File -FilePath $stamp -Encoding utf8

exit $status
