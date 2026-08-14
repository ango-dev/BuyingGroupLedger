#!/usr/bin/env bash
# One scheduled scrape, wrapped so the container can tell whether runs are actually happening.
#
# The heartbeat is the point. Without it, a container whose cron never fires looks IDENTICAL to one
# with nothing to do: no error, no log line, no alert. The only existing way to notice was running
# `scripts/audit_sheet.py --stale-days` by hand. Now every completed run stamps logs/.last_run, and
# healthcheck.sh marks the container unhealthy when that stamp goes stale.
set -uo pipefail

cd /app

STAMP=/app/logs/.last_run
started="$(date -u +%FT%TZ)"
echo "=== run started ${started} ==="

python main.py
status=$?

# Stamped on failure too, deliberately. The heartbeat answers "is the scheduler alive?", which is a
# different question from "did the run succeed?" — main.py already alerts per-retailer on failure.
# Conflating them would make a single failing retailer look like a dead container.
date -u +%FT%TZ > "$STAMP"

echo "=== run finished $(date -u +%FT%TZ) (exit ${status}) ==="
exit "$status"
