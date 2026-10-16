#!/bin/bash
# Installs poke_friends.py as a launchd job that runs every 15 minutes, or removes it with --uninstall.
# Run `python3 poke_friends.py --login CLIENT_ID` first.
#
# The script is copied to ~/Library/Application Support because macOS blocks
# background jobs from reading ~/Documents. Re-run this after editing poke_friends.py.
set -euo pipefail

LABEL="local.remilianet-poke"
APP_DIR="$HOME/Library/Application Support/remilianet-poke"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/remilianet-poke.log"
INTERVAL=900  # keep in sync with RUN_INTERVAL in poke_friends.py

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$PLIST" "$APP_DIR/poke_friends.py"
  echo "Uninstalled. Your sign-in and poke history are still in $APP_DIR (delete the folder to remove them)."
  exit 0
fi

if [[ ! -f "$APP_DIR/auth.json" ]]; then
  echo "Not signed in yet. Run: python3 poke_friends.py --login CLIENT_ID" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cp "$(dirname "$0")/poke_friends.py" "$APP_DIR/poke_friends.py"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$APP_DIR/poke_friends.py</string>
    <string>--scheduled</string>
  </array>
  <key>StartInterval</key>
  <integer>$INTERVAL</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Runs now and then every 15 minutes. Log: $LOG"
