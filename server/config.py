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

# How long a dropped phone keeps its slot (instrument/placement) before the
# section is deleted from the roster. Long enough to survive a screen-off nap +
# ws ping timeout; short enough that closed tabs actually disappear.
SECTION_GRACE_S = float(os.environ.get("WM_SECTION_GRACE_S", "90"))

# --- Protocol ---
PROTOCOL_VERSION = 1

# --- Scheduler / timing (all milliseconds on the server monotonic clock) ---
SCHED_TICK_MS = 100.0     # how often the scheduler pulls from the engine and broadcasts
LOOKAHEAD_MS = 600.0      # pull events up to now + this
MIN_LEAD_MS = 150.0       # every emitted event must satisfy at >= now + this (else dropped + logged)

# --- Gesture semantics ---
# Instant per-gesture flourish/sting (the "room answers immediately" effect).
# Off by default — it reads as an annoying sound effect during song conducting;
# WM_PICKUP=1 re-enables it (e.g. for the hardware wand's sting pad).
PICKUP = os.environ.get("WM_PICKUP", "0") != "0"

# A gesture bends the song for this many bars, then it returns to normal —
# a conductor's cue, not a permanent remix.
GESTURE_BARS = int(os.environ.get("WM_GESTURE_BARS", "5"))

# --- Metronome stub (P1) ---
METRONOME_BPM = 120.0     # 120 BPM = one click every 500 ms (matches the clicktest cadence)
METRONOME_BEATS_PER_BAR = 4

# --- Session lifecycle ---
DISCONNECT_GRACE_S = 60.0  # keep a section's slot (instrument/placement) this long after it drops

# --- Data / logs ---
DATA_DIR = SERVER_DIR / "data"
DECISIONS_DIR = DATA_DIR / "decisions"   # per-run JSONL decision logs (training harvest)
DECISION_LOG = os.environ.get("WM_DECISION_LOG", "1") != "0"
SHOWS_DIR = pathlib.Path(os.environ.get("WM_SHOWS_DIR", str(DATA_DIR / "shows")))

# --- Backboard.io commentator (optional; unset key = inert) ---
BACKBOARD_URL = os.environ.get("WM_BACKBOARD_URL", "https://app.backboard.io/api")
BACKBOARD_KEY = os.environ.get("WM_BACKBOARD_KEY", "")
BACKBOARD_ASSISTANT = os.environ.get("WM_BACKBOARD_ASSISTANT", "")  # reuse one assistant = set-to-set memory
BACKBOARD_PROVIDER = os.environ.get("WM_BACKBOARD_PROVIDER", "openai")
BACKBOARD_MODEL = os.environ.get("WM_BACKBOARD_MODEL", "gpt-4o")
# ElevenLabs voice id: set to have Backboard speak announcements (voice.tts).
ANNOUNCER_VOICE = os.environ.get("WM_ANNOUNCER_VOICE", "")

# --- Session persistence (instruments/placement survive a server restart) ---
SESSION_FILE = pathlib.Path(os.environ.get("WM_SESSION_FILE", str(SERVER_DIR / "session.json")))
# Last loaded/edited song, restored on boot — a restart must never silently
# revert the show to the built-in loop.
SONG_CACHE = pathlib.Path(os.environ.get("WM_SONG_CACHE", str(SERVER_DIR / "data" / "last_song")))

# UDP discovery beacon: the wand board finds this server with no typed
# commands (see server/discovery.py). WM_DISCOVERY_OFF=1 disables.
DISCOVERY_PORT = int(os.environ.get("WM_DISCOVERY_PORT", "41234"))

# --- Decision model (optional; unset = heuristic ranker only) ---
# Any OpenAI-compatible serving base works, e.g. a Freesolo deploy:
#   WM_MODEL_URL=https://<serving-host>/v1  WM_MODEL_NAME=<run-id>  WM_MODEL_KEY=<key>
MODEL_URL = os.environ.get("WM_MODEL_URL", "")
MODEL_NAME = os.environ.get("WM_MODEL_NAME", "")
MODEL_KEY = os.environ.get("WM_MODEL_KEY", "")
# Measured Freesolo serving latency: ~0.9-1.1s warm. A reply that misses this
# bar lands at the next one (decisions persist until the next gesture), so the
# budget is generous; the heuristic covers anything slower.
MODEL_TIMEOUT_MS = float(os.environ.get("WM_MODEL_TIMEOUT_MS", "2000"))

# --- Bar-line model (optional; the "music editing" generator) ---
# Writes a fresh accompaniment line per bar, offered as the "generated"
# candidate. Prefetched a bar ahead, so its budget is looser.
BARMODEL_URL = os.environ.get("WM_BARMODEL_URL", "")
BARMODEL_NAME = os.environ.get("WM_BARMODEL_NAME", "")
BARMODEL_KEY = os.environ.get("WM_BARMODEL_KEY", "")
# Measured ~4.6s median per composed bar on Freesolo serving; the conductor
# prefetches two bars ahead to absorb it. A faster host (e.g. Parasail) can
# tighten both this and the prefetch horizon.
BARMODEL_TIMEOUT_MS = float(os.environ.get("WM_BARMODEL_TIMEOUT_MS", "7000"))
# How many bars ahead composed lines are requested. 2 absorbs Freesolo's ~4.6s
# median; a fast host (Fireworks dedicated: ~0.5s) can run at 1 so a gesture's
# styled line lands at the NEXT bar line instead of the one after.
BARMODEL_PREFETCH = max(1, int(os.environ.get("WM_BARMODEL_PREFETCH", "2")))
# Styles the DEPLOYED adapter is trusted to speak (what it was trained AND
# ear/judge-proven on). Styles outside this set are served by the deterministic
# theory in engine/harmony.py instead — the same generators that teach the
# model, so the sound is identical by construction. When the theory-device run
# (v3) is deployed and proven, widen via WM_BARMODEL_STYLES.
BARMODEL_STYLES = set(filter(None, os.environ.get(
    "WM_BARMODEL_STYLES", "harmonize,dense,calm,counter,echo,free").split(",")))
