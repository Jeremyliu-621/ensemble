// Message-type constants — mirror of server/protocol.py. Keep in sync.
export const PROTOCOL_VERSION = 1;

// Client -> Server
export const HELLO = "hello";
export const CLOCK_PING = "clock.ping";
export const SECTION_READY = "section.ready";
export const SECTION_LEAVE = "section.leave";
export const WAND_IMU = "wand.imu";
export const WAND_POSE = "wand.pose";   // CV (webcam) wand: [tw, x, y, z, roll_deg]
export const WAND_GRAB = "wand.grab";
export const WAND_FEEDBACK = "wand.feedback";
export const WAND_RECAL = "wand.recal";
export const WAND_TOUCH = "wand.touch";   // {pad, state} MPR121 pads (hw wand)
export const WAND_RANGE = "wand.range";   // {mm} ToF distance (hw wand)
export const WAND_MODE = "wand.mode";     // {mode: "ai"|"det"}
export const WAND_GESTURE = "wand.gesture"; // {label, strength?} on-wand TinyML
export const STAGE_PLACE = "stage.place";
export const STAGE_ASSIGN = "stage.assign";
export const STAGE_RECORD = "stage.record";  // {sha256, bytes, dur_s}
export const ADMIN_CMD = "admin.cmd";
export const SONG_LOAD = "song.load";
export const SONG_EDIT = "song.edit";   // editor-authored notes -> replaces/updates the song
export const SONG_HUM = "song.hum";       // {frames: [[t_ms, midi_float, rms], ...]}
export const SONG_FILE = "song.file";     // {name} load songs/<name>.mid server-side
export const CLOCK_REPORT = "clock.report";
export const CV_STATE = "cv.state";           // {gesture|null, mode, confidence}

// Server -> Client
export const WELCOME = "welcome";
export const CLOCK_PONG = "clock.pong";
export const SECTION_CONFIG = "section.config";
export const SCHED_NOTES = "sched.notes";
export const SCHED_CANCEL = "sched.cancel";
export const ROSTER = "roster";
export const ENGINE_STATE = "engine.state";   // live accompaniment/gesture updates
export const WAND_STATE = "wand.state";     // optional imu stream-health diagnostics for stage/admin
export const WAND_CMD = "wand.cmd";       // server -> wand: reflect show state {playing, mode, aim, seq}
export const ANNOUNCE = "announce";       // {text, audio_b64?, mime?}
export const FX_TENSION = "fx.tension";   // {value: 0..1}
export const FX_EXPR = "fx.expr";         // {section, semis, gain}
export const ERR = "err";

export const SECTION_ALL = "all";
export const WS_PATH = "/ws";

// Build the ws:// or wss:// URL for the current origin.
export function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${WS_PATH}`;
}
