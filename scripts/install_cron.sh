#!/usr/bin/env bash
# Install/update a cron job that runs the scrape every N hours (default 6 => 4x/day, 6h apart).
# Usage: ./scripts/install_cron.sh [HOURS]
#   ./scripts/install_cron.sh        # every 6 hours (00:00, 06:00, 12:00, 18:00)
#   ./scripts/install_cron.sh 4      # every 4 hours
# Re-run with a different HOURS to change the interval; runs 'crontab -e' to inspect/edit manually.
set -euo pipefail

HOURS="${1:-6}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$PROJECT_DIR/run.sh"
chmod +x "$RUN"

MARKER="# buying-group-ledger"
CRON_LINE="0 */$HOURS * * * $RUN $MARKER"

# Replace any existing entry (matched by marker), keep everything else.
( crontab -l 2>/dev/null | grep -v -- "$MARKER" || true; echo "$CRON_LINE" ) | crontab -

echo "Installed cron job (every $HOURS hours):"
echo "  $CRON_LINE"
echo "Logs: $PROJECT_DIR/logs/cron.log  and  logs/run.log"
echo "To remove: crontab -l | grep -v '$MARKER' | crontab -"
