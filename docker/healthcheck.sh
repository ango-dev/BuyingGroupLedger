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
# The knobs entrypoint.sh resolved from config.json (RUN_INTERVAL_HOURS, WEB_ENABLED, ...). A
# healthcheck runs with the container's ORIGINAL environment, not the entrypoint's, so without this
# a value that lives only in config.json would be invisible here and the shell defaults would judge.
if [ -f "${CONTAINER_ENV_FILE:-/tmp/container.env}" ]; then
    # shellcheck disable=SC1090
    . "${CONTAINER_ENV_FILE:-/tmp/container.env}"
fi
HOURS="${RUN_INTERVAL_HOURS:-6}"

# Two missed intervals before complaining: one run can legitimately overrun its slot (the agent
# fallback is slow) or be skipped by main.py's overlap lock, and flapping unhealthy on a single
# late run would train you to ignore it.
GRACE=$(( HOURS * 2 * 3600 ))

now=$(date +%s)

# THE UNHEALTHY STATE USED TO NOTIFY NOBODY. It showed in `docker ps` / `docker inspect` -- which
# nobody is watching at 3am -- while the ledger went stale. So the check now sends the alert itself,
# through the same channels as everything else, ONCE per episode: a marker records that this
# outage has been announced, and its removal on recovery sends the all-clear. Best-effort: the
# alert must never change the verdict, and a failed send is retried at the next 15-minute check.
MARKER="${UNHEALTHY_MARKER:-/app/logs/.unhealthy_alerted}"
NOTIFY="${NOTIFY_CMD:-python -m alerts.notifier}"

unhealthy() {  # $1 = reason
    echo "$1"
    if [ ! -f "$MARKER" ]; then
        body="$1. Do: check 'docker compose logs --tail=200', logs/.last_run and the run lock (logs/.run.lock). Sent once; an all-clear follows when a run completes."
        if (cd /app 2>/dev/null || true; ALERT_KIND=health $NOTIFY "Ledger container unhealthy: no runs completing" "$body" >/dev/null 2>&1); then
            date -u +%FT%TZ > "$MARKER"
        fi
    fi
    exit 1
}

healthy() {  # $1 = status line
    echo "$1"
    if [ -f "$MARKER" ]; then
        since="$(cat "$MARKER" 2>/dev/null || echo unknown)"
        (ALERT_KIND=health $NOTIFY "Ledger container healthy again" "$1 (unhealthy since ${since})." >/dev/null 2>&1) || true
        rm -f "$MARKER"
    fi
    exit 0
}

if [ ! -f "$STAMP" ]; then
    # No run has COMPLETED yet. Right after a deploy that is simply normal — RUN_ON_START is false
    # by default, so the first run waits for the next cron slot, up to a full interval away. Judge
    # it against how long the container has been up instead, so a fresh deploy isn't reported as
    # broken for hours (an alarm that is usually false is one you stop reading).
    if [ -f "$STARTED" ]; then
        waiting=$(( now - $(date -r "$STARTED" +%s) ))
        if [ "$waiting" -le "$GRACE" ]; then
            healthy "no run yet, but only up $(( waiting / 60 ))m (first run due within ${HOURS}h)"
        fi
        unhealthy "up $(( waiting / 3600 ))h and NO run has completed; expected one every ${HOURS}h"
    fi
    unhealthy "no run has completed yet (${STAMP} absent)"
fi

age=$(( now - $(date -r "$STAMP" +%s) ))
if [ "$age" -gt "$GRACE" ]; then
    unhealthy "last run was $(( age / 3600 ))h ago; expected one every ${HOURS}h"
fi

# The dashboard runs in this container too (entrypoint.sh). A dead one is worth knowing about,
# but it must never be mistaken for a dead scheduler, so its reason names it. Probed with Python
# because curl was purged from the image; WEB_ENABLED=false skips the probe.
WEB_URL="${WEB_HEALTH_URL:-http://127.0.0.1:8765/health}"
if [ "${WEB_ENABLED:-true}" = "true" ]; then
    if ! python -c "import sys, urllib.request; urllib.request.urlopen('$WEB_URL', timeout=5)" >/dev/null 2>&1; then
        unhealthy "scheduler fine (last run $(( age / 60 ))m ago) but the web dashboard is not answering on ${WEB_URL}"
    fi
fi

healthy "last run $(( age / 60 ))m ago (interval ${HOURS}h)"
