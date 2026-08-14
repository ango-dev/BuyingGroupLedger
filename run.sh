#!/usr/bin/env bash
# Run the Buying Group Ledger scrape for all configured retailers x profiles.
# Usage: ./run.sh [retailer ...]     (no args = all retailers)
# Invoked by cron (see scripts/install_cron.sh). Runs from the project root regardless of CWD.
#
# This is the non-Docker equivalent of docker/run_once.sh and keeps the same two guarantees:
# it always records how the run ENDED, and it stamps a heartbeat so a scheduler that has quietly
# stopped firing is detectable.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs

LOG=logs/cron.log
STAMP=logs/.last_run
MAX_LOG_BYTES=${MAX_LOG_BYTES:-10485760}   # 10 MB

# Rotate before appending. Unbounded, this file fills a Raspberry Pi's SD card months later and
# takes the whole host down — the kind of failure that arrives long after anyone is watching.
if [ -f "$LOG" ]; then
    size=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt "$MAX_LOG_BYTES" ]; then
        mv -f "$LOG" "$LOG.1"
    fi
fi

PY="./.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

# A SUBSHELL, not a { } group: `exit` inside a group would leave the whole script and skip the
# heartbeat stamp below.
(
    echo "=== run started $(date -u +%FT%TZ) ==="
    "$PY" main.py "$@"
    status=$?
    # NOT under `set -e`: a non-zero exit must still reach the "finished" line below. Previously a
    # failing run just stopped mid-file, so the log ended with a start line and no explanation.
    echo "=== run finished $(date -u +%FT%TZ) (exit ${status}) ==="
    exit "$status"
) >> "$LOG" 2>&1
status=$?

# Stamped whatever the outcome: this answers "is the scheduler alive?", not "did the run succeed?".
# main.py already alerts per-retailer on failure, and conflating the two would make one failing
# retailer look like a dead cron.
date -u +%FT%TZ > "$STAMP"

exit "$status"
