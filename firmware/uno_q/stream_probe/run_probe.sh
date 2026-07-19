#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
REMOTE_APP="/home/arduino/ArduinoApps/phoneharmonic-stream-probe"

BOARD=""
SERVER_IP=""
SERVER_PORT="8080"
SESSION="lol1"
DURATION="30"
KEEP_RUNNING=0
DRY_RUN=0
PREFLIGHT_ONLY=0

usage() {
  printf '%s\n' \
    'Deploy and run the Phoneharmonic UNO Q IMU stream probe.' \
    '' \
    'Usage:' \
    '  run_probe.sh --board USER@HOST --server-ip ADDRESS [options]' \
    '' \
    'Required:' \
    '  --board USER@HOST    SSH destination for the UNO Q' \
    '  --server-ip ADDRESS  Numeric laptop IPv4 or IPv6 address reachable by the UNO Q' \
    '' \
    'Options:' \
    '  --server-port PORT   Phoneharmonic WebSocket port (default: 8080)' \
    '  --session NAME       Phoneharmonic session (default: lol1)' \
    '  --duration SECONDS   Guided test duration (default: 30)' \
    '  --keep-running       Leave the board app running after a successful test' \
    '  --preflight-only     Run local static/dependency checks without contacting the board' \
    '  --dry-run            Validate arguments and print actions without mutation' \
    '  -h, --help           Show this help'
}

# Deploy and run the Phoneharmonic UNO Q IMU stream probe.
#
# Usage:
#   run_probe.sh --board USER@HOST --server-ip ADDRESS [options]
#
# Required:
#   --board USER@HOST    SSH destination for the UNO Q
#   --server-ip ADDRESS  Numeric laptop IPv4 or IPv6 address reachable by the UNO Q
#
# Options:
#   --server-port PORT   Phoneharmonic WebSocket port (default: 8080)
#   --session NAME       Phoneharmonic session (default: lol1)
#   --duration SECONDS   Guided test duration (default: 30)
#   --keep-running       Leave the board app running after a successful test
#   --dry-run            Validate arguments and print actions without mutation
#   -h, --help           Show this help

while (($#)); do
  case "$1" in
    --board)
      BOARD="${2-}"
      shift 2
      ;;
    --server-ip)
      SERVER_IP="${2-}"
      shift 2
      ;;
    --server-port)
      SERVER_PORT="${2-}"
      shift 2
      ;;
    --session)
      SESSION="${2-}"
      shift 2
      ;;
    --duration)
      DURATION="${2-}"
      shift 2
      ;;
    --keep-running)
      KEEP_RUNNING=1
      shift
      ;;
    --preflight-only)
      PREFLIGHT_ONLY=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$BOARD" || -z "$SERVER_IP" ]]; then
  echo "--board and --server-ip are required" >&2
  usage >&2
  exit 2
fi
if [[ ! "$BOARD" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "--board must be a USER@HOST SSH destination" >&2
  exit 2
fi
if [[ ! "$SERVER_PORT" =~ ^[0-9]+$ ]] || ((SERVER_PORT < 1 || SERVER_PORT > 65534)); then
  echo "--server-port must be an integer from 1 to 65534" >&2
  exit 2
fi
if [[ ! "$DURATION" =~ ^[0-9]+([.][0-9]+)?$ ]] \
    || [[ "$DURATION" =~ ^0+([.]0+)?$ ]]; then
  echo "--duration must be a positive number" >&2
  exit 2
fi
if [[ -z "$SESSION" || "$SESSION" == *$'\n'* ]]; then
  echo "--session must be a non-empty single-line value" >&2
  exit 2
fi

# Repo venv first (either layout: POSIX .venv/venv or Windows venv/Scripts),
# then system python3. Windows' bare `python3` is often the Store stub, so the
# venv checks matter there.
PYTHON_BIN=""
for candidate in "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/venv/bin/python" \
                 "$REPO_ROOT/.venv/Scripts/python.exe" "$REPO_ROOT/venv/Scripts/python.exe"; do
  if [[ -x "$candidate" ]]; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]] && command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "no python found: create the repo venv or install python3" >&2
  exit 1
fi

MONITOR="$REPO_ROOT/server/tools/wand_monitor.py"
STREAMER="$SCRIPT_DIR/python/main.py"
BOARD_REQUIREMENTS="$SCRIPT_DIR/python/requirements.txt"
SKETCH="$SCRIPT_DIR/sketch/sketch.ino"
SKETCH_PROFILE="$SCRIPT_DIR/sketch/sketch.yaml"

SERVER_IP="$("$PYTHON_BIN" "$REPO_ROOT/server/network_address.py" "$SERVER_IP")"
if [[ "$SERVER_IP" == *:* ]]; then
  URL_HOST="[$SERVER_IP]"
else
  URL_HOST="$SERVER_IP"
fi
WS_URL="ws://$URL_HOST:$SERVER_PORT/ws"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] board:      $BOARD"
  echo "[dry-run] server:     $WS_URL"
  echo "[dry-run] session:    $SESSION"
  echo "[dry-run] duration:   ${DURATION}s"
  echo "[dry-run] remote app: $REMOTE_APP"
  echo "[dry-run] no local or remote state changed"
  exit 0
fi

echo "[probe] running local preflight checks"
for required_file in \
  "$SCRIPT_DIR/app.yaml" "$SCRIPT_DIR/README.md" \
  "$STREAMER" "$BOARD_REQUIREMENTS" "$SKETCH" "$SKETCH_PROFILE" \
  "$MONITOR" "$REPO_ROOT/server/network_address.py" "$REPO_ROOT/server/main.py"; do
  if [[ ! -s "$required_file" ]]; then
    echo "required probe file is missing or empty: $required_file" >&2
    exit 1
  fi
done

bash -n "$0"
"$PYTHON_BIN" -m py_compile \
  "$STREAMER" "$MONITOR" "$REPO_ROOT/server/network_address.py" "$REPO_ROOT/server/main.py"
"$PYTHON_BIN" -c \
  'import mido, serial, websockets; from websockets.asyncio.client import connect' || {
  echo "local Python is missing compatible server dependencies; install server/requirements.txt" >&2
  exit 1
}
"$PYTHON_BIN" "$MONITOR" --help >/dev/null

"$PYTHON_BIN" - "$SKETCH_PROFILE" "$BOARD_REQUIREMENTS" "$STREAMER" "$SKETCH" <<'PY'
from pathlib import Path
import sys

profile = Path(sys.argv[1]).read_text(encoding="utf-8")
requirements = Path(sys.argv[2]).read_text(encoding="utf-8")
streamer = Path(sys.argv[3]).read_text(encoding="utf-8")
sketch = Path(sys.argv[4]).read_text(encoding="utf-8")

required_libraries = (
    "Arduino_Modulino (0.9.0)",
    "STM32duino VL53L4CD (1.0.5)",
    "STM32duino VL53L4ED (1.0.1)",
    "Arduino_LSM6DSOX (1.1.2)",
    "Arduino_LPS22HB (1.0.2)",
    "Arduino_HS300x (1.0.0)",
    "ArduinoGraphics (1.1.5)",
    "Arduino_LTR381RGB (1.0.1)",
)
missing = [library for library in required_libraries if library not in profile]
if missing:
    raise SystemExit(f"sketch profile is missing required libraries: {', '.join(missing)}")
if "Arduino_RPClite (0.2.1)" in profile:
    raise SystemExit("sketch profile pins obsolete Arduino_RPClite 0.2.1")
if "websockets>=16,<17" not in requirements.replace(" ", ""):
    raise SystemExit("board requirements must pin websockets>=16,<17")
if 'Bridge.provide("imu_sample"' not in streamer:
    raise SystemExit("board Python does not provide the imu_sample Bridge topic")
if 'Bridge.notify("imu_sample"' not in sketch:
    raise SystemExit("MCU sketch does not notify the imu_sample Bridge topic")
PY

echo "[probe] local preflight PASS"
if [[ "$PREFLIGHT_ONLY" -eq 1 ]]; then
  exit 0
fi

for command_name in ssh scp; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "required command not found: $command_name" >&2
    exit 1
  fi
done

STAGE_DIR=""
SERVER_PID=""
BOARD_STARTED=0
PROBE_PASSED=0

cleanup() {
  exit_status=$?
  trap - EXIT INT TERM

  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi

  if [[ "$BOARD_STARTED" -eq 1 && ("$KEEP_RUNNING" -ne 1 || "$PROBE_PASSED" -ne 1) ]]; then
    echo "[probe] stopping isolated board app"
    ssh "$BOARD" "arduino-app-cli app stop '$REMOTE_APP'" >/dev/null 2>&1 || true
  fi

  if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then
    rm -rf -- "$STAGE_DIR"
  fi
  exit "$exit_status"
}
trap cleanup EXIT INT TERM

echo "[probe] checking UNO Q access: $BOARD"
ssh "$BOARD" "command -v arduino-app-cli >/dev/null"

STAGE_DIR="$(mktemp -d)"
STAGE_APP="$STAGE_DIR/phoneharmonic-stream-probe"
mkdir -p "$STAGE_APP/python" "$STAGE_APP/sketch"
cp "$SCRIPT_DIR/app.yaml" "$SCRIPT_DIR/README.md" "$STAGE_APP/"
cp "$SCRIPT_DIR/python/main.py" "$SCRIPT_DIR/python/requirements.txt" "$STAGE_APP/python/"
cp "$SCRIPT_DIR/sketch/sketch.ino" "$SCRIPT_DIR/sketch/sketch.yaml" "$STAGE_APP/sketch/"

"$PYTHON_BIN" - "$STAGE_APP/python/probe_config.json" \
  "$WS_URL" "$SESSION" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps({"ws_url": sys.argv[2], "session": sys.argv[3]}) + "\n", encoding="utf-8")
PY

echo "[probe] deploying isolated app to $BOARD:$REMOTE_APP"
ssh "$BOARD" "arduino-app-cli app stop '$REMOTE_APP' >/dev/null 2>&1 || true; mkdir -p '$REMOTE_APP/python' '$REMOTE_APP/sketch'"
scp -q -r "$STAGE_APP/." "$BOARD:$REMOTE_APP/"

echo "[probe] compiling, flashing, and starting the UNO Q app"
# From this point cleanup may safely issue a stop even if compilation or startup
# fails partway through—the target is exclusively this isolated probe App.
BOARD_STARTED=1
ssh "$BOARD" "arduino-app-cli app start '$REMOTE_APP'"

set +e
"$PYTHON_BIN" "$MONITOR" --check-server --ip "$SERVER_IP" --port "$SERVER_PORT" --session "$SESSION"
server_status=$?
set -e

case "$server_status" in
  0)
    echo "[probe] reusing compatible Phoneharmonic server at $WS_URL"
    ;;
  2)
    echo "[probe] starting Phoneharmonic server at $WS_URL"
    SERVER_LOG="$STAGE_DIR/server.log"
    (
      cd "$REPO_ROOT"
      exec env WM_LAN_IP="$SERVER_IP" WM_HTTP_PORT="$SERVER_PORT" \
        WM_HTTPS_PORT="$((SERVER_PORT + 1))" \
        "$PYTHON_BIN" server/main.py >"$SERVER_LOG" 2>&1
    ) &
    SERVER_PID=$!

    ready=0
    for _ in {1..60}; do
      set +e
      "$PYTHON_BIN" "$MONITOR" --check-server --ip "$SERVER_IP" --port "$SERVER_PORT" \
        --session "$SESSION"
      server_status=$?
      set -e
      if [[ "$server_status" -eq 0 ]]; then
        ready=1
        break
      fi
      if [[ "$server_status" -eq 3 ]]; then
        break
      fi
      sleep 0.5
    done
    if [[ "$ready" -ne 1 ]]; then
      echo "Phoneharmonic server did not become ready:" >&2
      tail -n 40 "$SERVER_LOG" >&2 || true
      exit 1
    fi
    ;;
  3)
    echo "a listener exists at $WS_URL but is not a compatible Phoneharmonic server" >&2
    exit 1
    ;;
  *)
    echo "unexpected server check status: $server_status" >&2
    exit 1
    ;;
esac

echo "[probe] starting guided ${DURATION}s physical test"
set +e
"$PYTHON_BIN" "$MONITOR" --probe --ip "$SERVER_IP" --port "$SERVER_PORT" \
  --session "$SESSION" --duration "$DURATION"
probe_status=$?
set -e

if [[ "$probe_status" -eq 0 ]]; then
  PROBE_PASSED=1
  echo "[probe] hardware stream PASS"
  if [[ "$KEEP_RUNNING" -eq 1 ]]; then
    echo "[probe] leaving $REMOTE_APP running on $BOARD"
  fi
else
  echo "[probe] hardware stream FAIL" >&2
fi
exit "$probe_status"
