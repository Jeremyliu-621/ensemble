#!/bin/sh
# One-command wand deploy: stop, re-copy, point at THIS laptop, restart.
# The README's headless-deploy block, scripted. Run from anywhere:
#
#   firmware/uno_q/deploy_wand.sh                      # defaults
#   firmware/uno_q/deploy_wand.sh arduino@myboard.local 192.168.1.20
#
# Requires: the board on the same network, ssh access (default password
# 'arduino'). arduino-app-cli lives ON the board — nothing to install here.
set -e

BOARD="${1:-arduino@arduino.local}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
WAND_APP="/home/arduino/ArduinoApps/phoneharmonic-wand"

LAPTOP_IP="${2:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')}"
if [ -z "$LAPTOP_IP" ]; then
  echo "couldn't auto-detect this machine's LAN IP — pass it: $0 $BOARD <ip>" >&2
  exit 1
fi

echo "deploying wand -> $BOARD (laptop: $LAPTOP_IP)"
ssh "$BOARD" "arduino-app-cli app stop $WAND_APP" || true
ssh "$BOARD" "mkdir -p $WAND_APP"
scp -r "$REPO/firmware/uno_q/wand/." "$BOARD:$WAND_APP/"

# Headless deploys don't forward env into the app process — wand_config.json
# (checked before auto-discovery) carries the laptop's address instead.
printf '{"laptop_ip": "%s"}\n' "$LAPTOP_IP" | ssh "$BOARD" "cat > $WAND_APP/python/wand_config.json"

ssh "$BOARD" "arduino-app-cli app start $WAND_APP"
echo "done — verify with: venv/bin/python server/tools/wand_watch.py"
