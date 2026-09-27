#!/usr/bin/env bash
# Install a daily token-burn snapshot cron entry (idempotent — replaces any prior one).
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$(command -v python3)"
# Chain: mirror factory transcripts, fetch the GitHub outcome cache (issues/PRs), then
# snapshot. Each step is `;`-separated (not `&&`), so a failed step never blocks the
# rest of the chain -- same failure-tolerance convention as sync-factory.sh's own
# internal `|| echo WARNING` fallback. `outcomes.py` only touches the local cache
# (spec Sec5.2); a stale/missing cache just means the dashboard's Outcomes section
# shows "outcomes as of" an older timestamp, never a broken snapshot.
LINE="0 9 * * * cd '$DIR' && ./sync-factory.sh >> '$DIR/snapshot.log' 2>&1; '$PY' outcomes.py >> '$DIR/snapshot.log' 2>&1; '$PY' snapshot.py >> '$DIR/snapshot.log' 2>&1"
# Read the current crontab (empty if none), strip any prior token-burn entry for THIS
# repo, then append ours. `|| true` keeps `set -e` from aborting when crontab is empty or
# grep matches nothing — otherwise an empty pipe to `crontab -` would wipe the crontab.
existing="$(crontab -l 2>/dev/null || true)"
# Filter on token-burn's own marker/script name as well as this DIR, so an entry left
# behind by a PREVIOUS location of the repo is replaced rather than duplicated. A filter
# keyed only on the current DIR can't recognise the repo's past paths.
filtered="$(printf '%s\n' "$existing" | grep -vF "# token-burn snapshot" | grep -vE "token-burn/snapshot\\.py|token-burn' &&" | grep -vF "$DIR/snapshot.py" | grep -vF "cd '$DIR' &&" || true)"
{ [ -n "$filtered" ] && printf '%s\n' "$filtered" ; echo "# token-burn snapshot" ; echo "$LINE" ; } | crontab -
echo "installed daily cron (09:00 local):"
echo "  $LINE"
echo "view with: crontab -l   |   logs: $DIR/snapshot.log"
