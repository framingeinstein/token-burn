#!/usr/bin/env bash
# Install a daily token-burn snapshot cron entry (idempotent — replaces any prior one).
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$(command -v python3)"
LINE="0 9 * * * cd '$DIR' && '$PY' snapshot.py >> '$DIR/snapshot.log' 2>&1"
( crontab -l 2>/dev/null | grep -vF "# token-burn snapshot" | grep -vF "$DIR/snapshot.py" | grep -vF "cd '$DIR' &&" ; \
  echo "# token-burn snapshot" ; echo "$LINE" ) | crontab -
echo "installed daily cron (09:00 local):"
echo "  $LINE"
echo "view with: crontab -l   |   logs: $DIR/snapshot.log"
