#!/usr/bin/env bash
# Report the container unhealthy when the scheduler has stopped producing runs.
#
# `docker ps` showing "Up 3 weeks" proves supercronic is alive, NOT that it ever ran anything — a
# crashing job, a bad crontab or a wedged run lock all leave the container happily "Up" while the
# ledger silently goes stale. This compares the heartbeat run_once.sh stamps against the schedule.
set -uo pipefail

# Overridable only so this logic can be exercised outside a container; the defaults are the real paths.
STAMP="${HEARTBEAT_FILE:-/app/logs/.last_run}"
STARTED="${STARTED_FILE:-/app/logs/.started}"
HOURS="${RUN_INTERVAL_HOURS:-6}"

# Two missed intervals before complaining: one run can legitimately overrun its slot (the agent
# fallback is slow) or be skipped by main.py's overlap lock, and flapping unhealthy on a single
# late run would train you to ignore it.
GRACE=$(( HOURS * 2 * 3600 ))

now=$(date +%s)

if [ ! -f "$STAMP" ]; then
    # No run has COMPLETED yet. Right after a deploy that is simply normal — RUN_ON_START is false
    # by default, so the first run waits for the next cron slot, up to a full interval away. Judge
    # it against how long the container has been up instead, so a fresh deploy isn't reported as
    # broken for hours (an alarm that is usually false is one you stop reading).
    if [ -f "$STARTED" ]; then
        waiting=$(( now - $(date -r "$STARTED" +%s) ))
        if [ "$waiting" -le "$GRACE" ]; then
            echo "no run yet, but only up $(( waiting / 60 ))m (first run due within ${HOURS}h)"
            exit 0
        fi
        echo "up $(( waiting / 3600 ))h and NO run has completed; expected one every ${HOURS}h"
        exit 1
    fi
    echo "no run has completed yet (${STAMP} absent)"
    exit 1
fi

age=$(( now - $(date -r "$STAMP" +%s) ))
if [ "$age" -gt "$GRACE" ]; then
    echo "last run was $(( age / 3600 ))h ago; expected one every ${HOURS}h"
    exit 1
fi

echo "last run $(( age / 60 ))m ago (interval ${HOURS}h)"
exit 0
