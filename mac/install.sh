#!/bin/sh
# Install the auto-open LaunchAgent (reads only NTFY_TOPIC from the repo .env).
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/Application Support/iphone-autoopen"
mkdir -p "$DEST"
cp "$REPO/mac/autoopen.py" "$DEST/"
grep '^NTFY_TOPIC=' "$REPO/.env" > "$DEST/.env"
chmod 600 "$DEST/.env"
cp "$REPO/mac/com.wuhonchi.iphone-autoopen.plist" "$HOME/Library/LaunchAgents/"
launchctl bootout "gui/$(id -u)/com.wuhonchi.iphone-autoopen" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.wuhonchi.iphone-autoopen.plist"
echo "installed"
