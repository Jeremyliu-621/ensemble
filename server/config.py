"""Central tunables. Every magic number the system trusts lives here."""
from __future__ import annotations

import os
import pathlib

# --- Paths ---
SERVER_DIR = pathlib.Path(__file__).resolve().parent
REPO_DIR = SERVER_DIR.parent
WEB_DIR = REPO_DIR / "web"
CERT_DIR = REPO_DIR / "certs"

# --- Network (override with WM_HTTP_PORT / WM_HTTPS_PORT, e.g. to run a second
#     instance for testing without stopping the main one) ---
HTTP_PORT = int(os.environ.get("WM_HTTP_PORT", "8080"))   # plain http/ws — sections, ESP32 wand
HTTPS_PORT = int(os.environ.get("WM_HTTPS_PORT", "8443"))  # https/wss — wand-sim (secure context)
WS_PATH = "/ws"           # everything else on the port is served as a static file
DEFAULT_SESSION = "lol1"

# --- Protocol ---
PROTOCOL_VERSION = 1

# --- Scheduler / timing (all milliseconds on the server monotonic clock) ---
SCHED_TICK_MS = 100.0     # how often the scheduler pulls from the engine and broadcasts
LOOKAHEAD_MS = 600.0      # pull events up to now + this
MIN_LEAD_MS = 150.0       # every emitted event must satisfy at >= now + this (else dropped + logged)

# --- Metronome stub (P1) ---
METRONOME_BPM = 120.0     # 120 BPM = one click every 500 ms (matches the clicktest cadence)
METRONOME_BEATS_PER_BAR = 4

# --- Session lifecycle ---
DISCONNECT_GRACE_S = 60.0  # keep a section's slot (instrument/placement) this long after it drops

# --- Data / logs ---
DATA_DIR = SERVER_DIR / "data"
DECISIONS_DIR = DATA_DIR / "decisions"   # per-run JSONL decision logs (training harvest)
DECISION_LOG = os.environ.get("WM_DECISION_LOG", "1") != "0"

# --- Decision model (optional; unset = heuristic ranker only) ---
# Any OpenAI-compatible serving base works, e.g. a Freesolo deploy:
#   WM_MODEL_URL=https://<serving-host>/v1  WM_MODEL_NAME=<run-id>  WM_MODEL_KEY=<key>
MODEL_URL = os.environ.get("WM_MODEL_URL", "")
MODEL_NAME = os.environ.get("WM_MODEL_NAME", "")
MODEL_KEY = os.environ.get("WM_MODEL_KEY", "")
MODEL_TIMEOUT_MS = float(os.environ.get("WM_MODEL_TIMEOUT_MS", "800"))  # then the heuristic covers
