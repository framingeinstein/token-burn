#!/usr/bin/env bash
# Install the token-burn dashboard server as a macOS LaunchAgent (idempotent — replaces
# any prior one). The server then starts at login and is restarted by launchd if it dies.
#
#   ./install-launchd.sh              install (or reinstall) + start + health-check
#   ./install-launchd.sh --uninstall  stop + remove the agent
#
# Capture stays with cron (install-cron.sh); this only supervises the read-only serve.py.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$(command -v python3)"
LABEL="com.token-burn.serve"
PORT="${PORT:-8799}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/.config/token-burn"
DOMAIN="gui/$(id -u)"

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
answering() { curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; }

if [ "${1:-}" = "--uninstall" ]; then
  if loaded; then launchctl bootout "$DOMAIN/$LABEL"; fi
  rm -f "$PLIST"
  echo "removed $LABEL ($PLIST)"
  exit 0
fi

# serve.py silently walks to the next free port if $PORT is taken, so a stray hand-launched
# server would push the supervised one to $PORT+1 and the URL people bookmark would go stale.
# Refuse rather than paper over that.
if ! loaded && answering; then
  echo "something un-supervised already answers on 127.0.0.1:$PORT — stop it first:" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
  exit 1
fi

mkdir -p "$LOGDIR" "$(dirname "$PLIST")"

# Paths are baked in absolute (launchd has no shell profile / PATH). XML-escape '&' so a
# repo path containing one can't corrupt the plist.
esc() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
      <string>$(esc "$PY")</string>
      <string>$(esc "$DIR/serve.py")</string>
      <string>--port</string>
      <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(esc "$DIR")</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PYTHONUNBUFFERED</key>
      <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$(esc "$LOGDIR/serve.out")</string>
    <key>StandardErrorPath</key>
    <string>$(esc "$LOGDIR/serve.err")</string>
  </dict>
</plist>
PLIST
plutil -lint "$PLIST" >/dev/null

# Reload: bootout the old definition (if any) so an edited plist actually takes effect.
if loaded; then launchctl bootout "$DOMAIN/$LABEL"; sleep 1; fi
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart "$DOMAIN/$LABEL"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if answering; then
    echo "installed $LABEL — serving http://127.0.0.1:$PORT"
    echo "  plist:   $PLIST"
    echo "  logs:    $LOGDIR/serve.{out,err}"
    echo "  status:  launchctl print $DOMAIN/$LABEL | head -30"
    echo "  restart: launchctl kickstart -k $DOMAIN/$LABEL   (after editing serve.py / dashboard.html)"
    exit 0
  fi
  sleep 1
done
echo "agent loaded but nothing answers on :$PORT after 10s — see $LOGDIR/serve.err" >&2
launchctl print "$DOMAIN/$LABEL" 2>&1 | grep -E 'state|last exit|pid' >&2 || true
exit 1
