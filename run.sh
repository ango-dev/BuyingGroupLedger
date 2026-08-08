#!/usr/bin/env bash
# Run the Buying Group Ledger scrape for all configured retailers x profiles.
# Usage: ./run.sh [retailer ...]     (no args = all retailers)
# Invoked by cron (see scripts/install_cron.sh). Runs from the project root regardless of CWD.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs

PY="./.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

echo "=== run started $(date -u +%FT%TZ) ===" >> logs/cron.log
"$PY" main.py "$@" >> logs/cron.log 2>&1
echo "=== run finished $(date -u +%FT%TZ) ===" >> logs/cron.log
