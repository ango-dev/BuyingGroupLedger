#!/usr/bin/env bash
# Generate a crontab from RUN_INTERVAL_HOURS, then hand off to supercronic (which keeps the
# container alive and runs the scrape on schedule). Job output shows in `docker logs`; the app
# also writes logs/run.log in the mounted volume.
set -euo pipefail

HOURS="${RUN_INTERVAL_HOURS:-6}"
mkdir -p /app/logs
echo "0 */${HOURS} * * * cd /app && python main.py" > /app/crontab

echo "[entrypoint] scheduled every ${HOURS}h -> $(cat /app/crontab)"

# Optionally do one run immediately on container start (handy for first-boot / testing).
if [ "${RUN_ON_START:-false}" = "true" ]; then
    echo "[entrypoint] RUN_ON_START=true -> running once now"
    (cd /app && python main.py) || echo "[entrypoint] initial run failed (continuing to schedule)"
fi

exec supercronic /app/crontab
