#!/usr/bin/env bash
# Install/update a cron job that runs the scrape every N hours (default 6 => 4x/day, 6h apart).
# Usage: ./scripts/install_cron.sh [HOURS]
#   ./scripts/install_cron.sh        # every 6 hours (00:00, 06:00, 12:00, 18:00)
#   ./scripts/install_cron.sh 4      # every 4 hours
# Re-run with a different HOURS to change the interval; run 'crontab -e' to inspect/edit manually.
set -euo pipefail

HOURS="${1:-6}"

# Validate before touching the crontab. `*/25` is out of cron's 0-23 hour range, so `crontab -`
# rejects the WHOLE file — including the entries this script preserved from the existing crontab.
# Catching it here means a typo costs you an error message instead of a confusing partial install.
# (Mirrors the same check in docker/entrypoint.sh, so both schedulers agree on what is valid.)
if ! [[ "$HOURS" =~ ^[0-9]+$ ]] || [ "$HOURS" -lt 1 ] || [ "$HOURS" -gt 23 ]; then
    echo "Error: HOURS must be a whole number from 1 to 23 (got '$HOURS')." >&2
    echo "       For less than hourly, edit the crontab by hand with 'crontab -e'." >&2
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$PROJECT_DIR/run.sh"

if [ ! -f "$RUN" ]; then
    echo "Error: $RUN not found." >&2
    exit 1
fi
chmod +x "$RUN"

# How many times `0 */H * * *` actually fires: cron enumerates multiples of H within 0-23, so an
# interval that doesn't divide 24 evenly gives an uneven day (H=5 -> 0,5,10,15,20 = 5, not 4.8).
RUNS_PER_DAY=$(( 23 / HOURS + 1 ))

# MaxOutDeals allows 10 received-items calls a day and every run spends exactly one, so the schedule
# is bounded by a third party rather than by anything here. Warn rather than refuse: going over only
# stops the payout/premium/status write-back (tracking submission has a far higher limit), and the
# user may accept that for fresher shipment status. Mirrors scripts/preflight.py's check, which
# covers the Docker path.
MOD_DAILY_PAYOUT_READS=10
if [ "$RUNS_PER_DAY" -gt "$MOD_DAILY_PAYOUT_READS" ]; then
    echo "WARNING: every ${HOURS}h is ${RUNS_PER_DAY} runs/day, but MaxOutDeals allows only" >&2
    echo "         ${MOD_DAILY_PAYOUT_READS} payout reads per day and each run spends one." >&2
    echo "         Payout Amount / Insurance / paid status will stop updating once the quota is" >&2
    echo "         gone (tracking submission is unaffected). A manual sync_tracking spends one" >&2
    echo "         too, even as a DRY RUN. Use 3h or longer to stay inside it." >&2
    echo >&2
fi

MARKER="# buying-group-ledger"
CRON_LINE="0 */$HOURS * * * $RUN $MARKER"

# Replace any existing entry (matched by marker), keep everything else.
( crontab -l 2>/dev/null | grep -v -- "$MARKER" || true; echo "$CRON_LINE" ) | crontab -

echo "Installed cron job (every $HOURS hours):"
echo "  $CRON_LINE"
echo
echo "Logs:      $PROJECT_DIR/logs/cron.log  (rotated at 10MB)  and  logs/run.log"
echo "Heartbeat: $PROJECT_DIR/logs/.last_run — if this stops advancing, cron has stopped firing."
echo "Watch:     tail -f $PROJECT_DIR/logs/cron.log"
echo "Remove:    crontab -l | grep -v '$MARKER' | crontab -"
echo
echo "Cron runs with a minimal environment, so if a scheduled run behaves differently from a manual"
echo "one, reproduce it with:  env -i $RUN"
