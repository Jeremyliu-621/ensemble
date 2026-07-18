"""WebSocket message-type constants, shared by every server module.

Wire format: JSON text frames shaped `{"t": <type>, ...}`.
The `t` values here MUST stay in sync with web/shared/protocol.js.
All server-authored timestamps are milliseconds on the server monotonic clock.
"""
from __future__ import annotations

# --- Client -> Server ---
HELLO = "hello"                 # {role, session, client_id|null, name?}  first frame on every connection
CLOCK_PING = "clock.ping"       # {id, t0}   t0 = client performance.now() ms, echoed back
SECTION_READY = "section.ready" # {}         audio unlocked + samples loaded
WAND_IMU = "wand.imu"           # {seq, frames:[[tw_ms, ax,ay,az, gx,gy,gz], ...]}  hw/phone wand
WAND_POSE = "wand.pose"         # {seq, frames:[[tw_ms, x, y, z, roll_deg], ...]}   CV (webcam) wand
WAND_GRAB = "wand.grab"         # {state:"start"|"end", tw}
WAND_FEEDBACK = "wand.feedback" # {value: 1|-1}
WAND_RECAL = "wand.recal"       # {tw}        zero the aiming yaw
WAND_TOUCH = "wand.touch"       # {pad:0-11, state:"down"|"up"}  MPR121 pads: 0-5 force a candidate
WAND_RANGE = "wand.range"       # {mm}        ToF distance -> proximity tension (fx.tension)
WAND_MODE = "wand.mode"         # {mode:"ai"|"det"}  physical toggle: gestures compose vs continuous control
WAND_GESTURE = "wand.gesture"   # {label, strength?}  on-wand TinyML classification (optional path)
STAGE_PLACE = "stage.place"     # {section_id, azimuth_deg, pos:[x,y,z]}
STAGE_ASSIGN = "stage.assign"   # {section_id, instrument}
STAGE_RECORD = "stage.record"   # {sha256, bytes, dur_s}  finished room recording -> ledger
ADMIN_CMD = "admin.cmd"         # {cmd:"start"|"stop"|"clicktest"|"resync"|"allnotesoff"|"tempo"|"force", args?}
SONG_LOAD = "song.load"         # {name, data}  data = base64 of a .mid file -> replaces the song
SONG_HUM = "song.hum"           # {frames:[[t_ms, midi_float, rms], ...]}  hummed melody -> new song
CLOCK_REPORT = "clock.report"   # {theta, rtt}  section's own sync estimate (debug/health readout)

# --- Server -> Client ---
WELCOME = "welcome"             # {v, client_id, role, server_time, config}
CLOCK_PONG = "clock.pong"       # {id, t0, ts}   ts = server_time_ms() at handling
SECTION_CONFIG = "section.config"  # {section_id, instrument, color, samples}
SCHED_NOTES = "sched.notes"     # {events:[{id, section, at, dur, note, vel, art}]}
SCHED_CANCEL = "sched.cancel"   # {ids?:[...], section?, after?} | {allnotesoff:true}
ROSTER = "roster"               # {playing, sections:[...], wand:{...}, engine:{...}}
ENGINE_STATE = "engine.state"   # {last_choice, gesture, playing, bpm, song}  live, on each change
WAND_STATE = "wand.state"       # {grabbed, aim_section, yaw_deg}   -> stage/admin, throttled
ANNOUNCE = "announce"           # {text, audio_b64?, mime?}  commentator line -> stage/admin
FX_TENSION = "fx.tension"       # {value: 0..1}  proximity build-up -> sections + stage
FX_EXPR = "fx.expr"             # {section, semis, gain}  deterministic-mode expression warp
ERR = "err"                     # {code, msg}

# Special section id meaning "every section plays this event".
SECTION_ALL = "all"

# Wand front-ends that all occupy the single wand slot (latest wins). They differ
# only in modality: hw = ESP32+IMU, sim = phone DeviceMotion, cv = webcam hand-tracking.
WAND_ROLES = ("wand", "wand-sim", "wand-cv")
WAND_VARIANT = {"wand": "hw", "wand-sim": "sim", "wand-cv": "cv"}

ROLES = ("stage", "section", "admin", *WAND_ROLES)
