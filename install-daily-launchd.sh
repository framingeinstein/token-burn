#!/usr/bin/env bash
# Install the daily token-burn chain (run-daily.sh) as a macOS LaunchAgent at 09:00
# local, replacing the cron entry from install-cron.sh. A LaunchAgent runs inside the
# login session, so `gh` can read its keychain token; cron can't. Idempotent.
#   ./install-daily-launchd.sh              install (or reinstall), remove the cron entry
#   ./install-daily-launchd.sh --uninstall  stop + remove the agent (cron left alone)
#   ./install-daily-launchd.sh --run-now    install, then run the chain once right away
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="com.token-burn.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

if [ "${1:-}" = "--uninstall" ]; then
  if loaded; then launchctl bootout "$DOMAIN/$LABEL"; fi
  rm -f "$PLIST"
  echo "removed $LABEL ($PLIST)"
  exit 0
fi

# Actor login resolved once, at install time (TOKEN_BURN_ACTOR wins, else gh api user),
# with the same validation as install-cron.sh.
ACTOR="${TOKEN_BURN_ACTOR:-}"
if [ -z "$ACTOR" ] && command -v gh >/dev/null 2>&1; then
  ACTOR="$(gh api user --jq .login 2>/dev/null || true)"
fi
ACTOR_XML=""
if printf '%s' "$ACTOR" | grep -Eq '^[A-Za-z0-9-]{1,39}$'; then
  ACTOR_XML="      <string>--actor</string>
      <string>$ACTOR</string>"
else
  echo "WARNING: could not resolve a GitHub login (set TOKEN_BURN_ACTOR or run 'gh auth login');" \
       "installing without --actor -- snapshot.py will try 'gh api user' at run time." >&2
fi

esc() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>$(esc "$DIR/run-daily.sh")</string>
$ACTOR_XML
    </array>
    <key>WorkingDirectory</key>
    <string>$(esc "$DIR")</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
      <key>PYTHONUNBUFFERED</key>
      <string>1</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key>
      <integer>9</integer>
      <key>Minute</key>
      <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$(esc "$DIR/snapshot.log")</string>
    <key>StandardErrorPath</key>
    <string>$(esc "$DIR/snapshot.log")</string>
  </dict>
</plist>
PLIST
if command -v plutil >/dev/null 2>&1; then plutil -lint "$PLIST" >/dev/null; fi

if loaded; then launchctl bootout "$DOMAIN/$LABEL"; sleep 1; fi
launchctl bootstrap "$DOMAIN" "$PLIST"

# Remove the cron entry this replaces (same filters as install-cron.sh), leaving
# every unrelated entry alone.
existing="$(crontab -l 2>/dev/null || true)"
if printf '%s\n' "$existing" | grep -qE "token-burn snapshot|token-burn/snapshot\\.py|cd '$DIR' &&"; then
  filtered="$(printf '%s\n' "$existing" | grep -vF "# token-burn snapshot" | grep -vE "token-burn/snapshot\\.py|token-burn' &&" | grep -vF "$DIR/snapshot.py" | grep -vF "cd '$DIR' &&" || true)"
  printf '%s\n' "$filtered" | crontab -
  echo "removed the token-burn cron entry (replaced by $LABEL)"
fi

echo "installed $LABEL — runs $DIR/run-daily.sh daily at 09:00 (logs: $DIR/snapshot.log)"
if [ "${1:-}" = "--run-now" ]; then
  launchctl kickstart "$DOMAIN/$LABEL"
  echo "started one run now"
fi
