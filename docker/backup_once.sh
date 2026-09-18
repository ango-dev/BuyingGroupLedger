#!/usr/bin/env bash
# The container's scheduled backup: supercronic runs this on the `backups.*` schedule from
# config.json (docker/entrypoint.sh appends the line). Same shape as run_once.sh so the compose
# logs show where a backup started and ended and how it exited. The work is
# `python -m scripts.backup --scheduled`: one zip, then the retention rule, an Activity entry, and
# an alert when it fails.
set -uo pipefail
cd /app
echo "=== backup started $(date -u +%FT%TZ) ==="
python -m scripts.backup --scheduled
status=$?
echo "=== backup finished $(date -u +%FT%TZ) (exit ${status}) ==="
exit "$status"
