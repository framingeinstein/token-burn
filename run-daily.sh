#!/usr/bin/env bash
# The daily token-burn chain: mirror factory transcripts, refresh the GitHub outcome
# cache, then freeze completed days (ledgers + usage records + team upload).
# Each step is independent: a failed step never blocks the next (same convention as
# the cron line). Run by the com.token-burn.daily launchd agent (install-daily-launchd.sh),
# which runs inside the login session so `gh` can read its token from the keychain --
# cron can't (2026-09-29: "gh auth token failed" at 09:00).
#   ./run-daily.sh [--actor <github-login>]
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
PY="${TOKEN_BURN_PYTHON:-python3}"
ACTOR_ARGS=()
if [ "${1:-}" = "--actor" ] && [ -n "${2:-}" ]; then ACTOR_ARGS=(--actor "$2"); fi
echo "== token-burn daily $(date '+%F %T') =="
./sync-factory.sh || echo "  WARNING: factory sync step failed"
"$PY" outcomes.py || echo "  WARNING: outcome fetch step failed"
"$PY" snapshot.py "${ACTOR_ARGS[@]}" || echo "  WARNING: snapshot step failed"
