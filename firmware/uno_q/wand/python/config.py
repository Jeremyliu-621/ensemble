"""Board-side configuration for the WiFi wand link.

Runs on the UNO Q's Linux SoC (Arduino App Lab). The board joins the same WiFi
as the phones and connects straight to the laptop server — no laptop-side bridge.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# The laptop's address is normally DISCOVERED (the server broadcasts a UDP
# beacon — see wand_link.resolve_ws_url), so nothing needs typing on the
# board. WAND_LAPTOP_IP remains a manual override that always wins.
#
# arduino-app-cli headless deploys don't forward the deploying shell's env
# into the app's container (unlike App Lab GUI runs, where WAND_LAPTOP_IP as
# documented does reach the process), so a headless deploy also accepts a
# wand_config.json dropped next to this file at deploy time — same pattern
# stream_probe/python/main.py already uses for its ws_url/session.
def _file_laptop_ip() -> str | None:
    try:
        raw = json.loads(Path(__file__).with_name("wand_config.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    ip = raw.get("laptop_ip")
    return ip if isinstance(ip, str) and ip else None

LAPTOP_IP = os.environ.get("WAND_LAPTOP_IP") or _file_laptop_ip()   # None = auto-discover
WS_PORT = int(os.environ.get("WAND_WS_PORT", "8080"))
SESSION = os.environ.get("WAND_SESSION", "lol1")

WS_URL = f"ws://{LAPTOP_IP}:{WS_PORT}/ws" if LAPTOP_IP else None

# Discovery: listen for the server's beacon / probe it on this UDP port
# (must match the server's WM_DISCOVERY_PORT), and remember the last URL
# that worked so reconnects are instant.
DISCOVERY_PORT = int(os.environ.get("WAND_DISCOVERY_PORT", "41234"))
DISCOVERY_WAIT_S = float(os.environ.get("WAND_DISCOVERY_WAIT_S", "6"))
CACHE_FILE = os.path.expanduser(os.environ.get("WAND_CACHE_FILE", "~/.phoneharmonic_wand.json"))

PROTOCOL_VERSION = 1
BATCH = 5                       # IMU frames per wand.imu message (~10-12 Hz)
RECONNECT_BACKOFF_S = 1.0

# Squeeze-to-conduct: covering the ToF sensor opens a gesture window (the grab
# the MPR121 provided in the original design), uncovering closes it. Hysteresis
# so a hovering hand doesn't flicker. Tension (wand.range) still ramps as the
# hand approaches — it peaks exactly while grabbed, one sensor, both feelings.
GRAB_ON_MM = float(os.environ.get("WAND_GRAB_ON_MM", "100"))
GRAB_OFF_MM = float(os.environ.get("WAND_GRAB_OFF_MM", "150"))

# Future on-device AI model artifact (None = defer to the server's Freesolo model).
MODEL_PATH = os.environ.get("WAND_MODEL_PATH") or None
