#!/usr/bin/env bash
# Install a daily token-burn snapshot cron entry (idempotent — replaces any prior one).
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$(command -v python3)"
LINE="0 9 * * * cd '$DIR' && '$PY' snapshot.py >> '$DIR/snapshot.log' 2>&1"
# Read the current crontab (empty if none), strip any prior token-burn entry for THIS
# repo, then append ours. `|| true` keeps `set -e` from aborting when crontab is empty or
# grep matches nothing — otherwise an empty pipe to `crontab -` would wipe the crontab.
existing="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$existing" | grep -vF "# token-burn snapshot" | grep -vF "$DIR/snapshot.py" | grep -vF "cd '$DIR' &&" || true)"
{ [ -n "$filtered" ] && printf '%s\n' "$filtered" ; echo "# token-burn snapshot" ; echo "$LINE" ; } | crontab -
echo "installed daily cron (09:00 local):"
echo "  $LINE"
echo "view with: crontab -l   |   logs: $DIR/snapshot.log"
