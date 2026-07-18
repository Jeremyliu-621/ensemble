"""Board-side configuration for the WiFi wand link.

Runs on the UNO Q's Linux SoC (Arduino App Lab). The board joins the same WiFi
as the phones and connects straight to the laptop server — no laptop-side bridge.
"""
from __future__ import annotations

import os

# LAN IP of the laptop running server/main.py — the address the phones use.
# The server prints it on startup (detect_lan_ip); override via env for the venue.
LAPTOP_IP = os.environ.get("WAND_LAPTOP_IP", "192.168.1.100")
WS_PORT = int(os.environ.get("WAND_WS_PORT", "8080"))
SESSION = os.environ.get("WAND_SESSION", "lol1")

WS_URL = f"ws://{LAPTOP_IP}:{WS_PORT}/ws"

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
