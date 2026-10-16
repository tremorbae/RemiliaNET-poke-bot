#!/bin/bash
# Installs poke_browser.py as two always-on launchd jobs: the poker, which times its own pokes as set in settings.ini,
# and a --watch-feed listener that notes who shows up in the activity feed. --uninstall removes both.
# Sets up Playwright in its own Python environment and, the first time, opens Chrome for you to sign in.
#
# Everything lives in ~/Library/Application Support because macOS blocks
# background jobs from reading ~/Documents. Re-run this after editing poke_browser.py.
set -euo pipefail

LABEL="local.remilianet-poke-browser"
FEED_LABEL="local.remilianet-feed-watch"
APP_DIR="$HOME/Library/Application Support/remilianet-poke-browser"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
FEED_PLIST="$HOME/Library/LaunchAgents/$FEED_LABEL.plist"
LOG="$HOME/Library/Logs/remilianet-poke-browser.log"
PLAYWRIGHT_VERSION=1.60.0  # the last release that runs on macOS's built-in Python 3.9

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$FEED_LABEL" 2>/dev/null || true

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$PLIST" "$FEED_PLIST" "$APP_DIR/poke_browser.py" "$APP_DIR/settings.ini"
  rm -rf "$APP_DIR/venv"
  echo "Uninstalled. Your sign-in, browser profile and poke history are still in $APP_DIR (delete the folder to remove them)."
  exit 0
fi

if [[ ! -d "/Applications/Google Chrome.app" ]]; then
  echo "This needs Google Chrome in /Applications. Install it, then run this again." >&2
  exit 1
fi

if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  echo "Setting up Playwright (one time)..."
  mkdir -p "$APP_DIR"
  /usr/bin/python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --disable-pip-version-check "playwright==$PLAYWRIGHT_VERSION"
cp "$(dirname "$0")/poke_browser.py" "$APP_DIR/poke_browser.py"
cp "$(dirname "$0")/settings.ini" "$APP_DIR/settings.ini"

if [[ ! -f "$APP_DIR/cookies.json" ]]; then
  "$APP_DIR/venv/bin/python" "$APP_DIR/poke_browser.py" --login
fi

# write_plist PATH LABEL FLAG: an always-on job running poke_browser.py FLAG.
write_plist() {
  cat > "$1" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$2</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/venv/bin/python</string>
    <string>$APP_DIR/poke_browser.py</string>
    <string>$3</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
EOF
}

mkdir -p "$HOME/Library/LaunchAgents"
write_plist "$FEED_PLIST" "$FEED_LABEL" --watch-feed
write_plist "$PLIST" "$LABEL" --scheduled

launchctl bootstrap "gui/$(id -u)" "$FEED_PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Pokes one person at a time, friends first, as set in settings.ini; the feed listener stays on. Log: $LOG"
