#!/usr/bin/env bash
# Container entrypoint: verify the deployment is sane, generate a crontab from RUN_INTERVAL_HOURS,
# then hand off to supercronic (which keeps the container alive and runs the scrape on schedule).
# Job output shows in `docker compose logs`; the app also writes logs/run.log in the mounted volume.
set -euo pipefail

mkdir -p /app/logs /app/data

# Records when this container came up, so the healthcheck can tell "hasn't run YET" (normal for the
# first few hours after a deploy, since RUN_ON_START is false by default) apart from "stopped
# running" (a real fault). Without it the container reports unhealthy from 2 minutes after every
# deploy until the first scheduled run — and a warning that is usually false gets ignored.
date -u +%FT%TZ > /app/logs/.started

# --- preflight ------------------------------------------------------------------------------
# Runs on EVERY container start, because the failures it catches are the silent ones: a missing
# dependency degrades three retailers to the paid agent, and a bind mount whose host file is absent
# becomes an empty directory that reads as "not configured". Both keep working while doing the wrong
# thing, so nothing would ever raise. See scripts/preflight.py for the full reasoning.
#
# It does NOT abort the container. A container that refuses to start also stops scraping, and a
# missed order costs more than a wasted agent run — so we alert and carry on degraded, which matches
# the project's "reliability beats cost" rule. Set PREFLIGHT_STRICT=true to fail fast instead.
echo "[entrypoint] running preflight checks..."
if ! (cd /app && python -m scripts.preflight --alert); then
    if [ "${PREFLIGHT_STRICT:-false}" = "true" ]; then
        echo "[entrypoint] PREFLIGHT FAILED and PREFLIGHT_STRICT=true -> refusing to start." >&2
        exit 1
    fi
    echo "[entrypoint] PREFLIGHT FAILED -- alert sent. Continuing DEGRADED (see the report above)." >&2
fi

# --- schedule -------------------------------------------------------------------------------
HOURS="${RUN_INTERVAL_HOURS:-6}"
if ! [[ "$HOURS" =~ ^[0-9]+$ ]] || [ "$HOURS" -lt 1 ] || [ "$HOURS" -gt 23 ]; then
    echo "[entrypoint] RUN_INTERVAL_HOURS='$HOURS' is not an integer in 1..23; falling back to 6." >&2
    HOURS=6
fi

# Runs via run_once.sh rather than `python main.py` directly, so every run stamps the heartbeat the
# healthcheck reads.
echo "0 */${HOURS} * * * /usr/local/bin/run_once.sh" > /app/crontab

echo "[entrypoint] timezone: $(date +%Z) ($(date -u +%FT%TZ) UTC)"
echo "[entrypoint] scheduled every ${HOURS}h -> $(cat /app/crontab)"

# Optionally do one run immediately on container start (handy for first-boot / testing).
if [ "${RUN_ON_START:-false}" = "true" ]; then
    echo "[entrypoint] RUN_ON_START=true -> running once now"
    /usr/local/bin/run_once.sh || echo "[entrypoint] initial run failed (continuing to schedule)"
fi

# The ABSOLUTE path is load-bearing — `exec supercronic` (resolved via PATH) makes this container
# die on startup, every time. As PID 1 supercronic enables process reaping, which re-execs argv[0]
# via a raw fork+exec that does NOT do a PATH lookup, so a bare "supercronic" fails with
# "Failed to fork exec: no such file or directory" and the container restart-loops forever, running
# nothing and alerting nobody. Only reproduces as PID 1, which is exactly how it runs in production.
exec /usr/local/bin/supercronic /app/crontab
