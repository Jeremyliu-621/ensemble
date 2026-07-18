// Message-type constants — mirror of server/protocol.py. Keep in sync.
export const PROTOCOL_VERSION = 1;

// Client -> Server
export const HELLO = "hello";
export const CLOCK_PING = "clock.ping";
export const SECTION_READY = "section.ready";
export const WAND_IMU = "wand.imu";
export const WAND_POSE = "wand.pose";   // CV (webcam) wand: [tw, x, y, z, roll_deg]
export const WAND_GRAB = "wand.grab";
export const WAND_FEEDBACK = "wand.feedback";
export const WAND_RECAL = "wand.recal";
export const STAGE_PLACE = "stage.place";
export const STAGE_ASSIGN = "stage.assign";
export const ADMIN_CMD = "admin.cmd";
export const SONG_LOAD = "song.load";
export const CLOCK_REPORT = "clock.report";

// Server -> Client
export const WELCOME = "welcome";
export const CLOCK_PONG = "clock.pong";
export const SECTION_CONFIG = "section.config";
export const SCHED_NOTES = "sched.notes";
export const SCHED_CANCEL = "sched.cancel";
export const ROSTER = "roster";
export const WAND_STATE = "wand.state";
export const ERR = "err";

export const SECTION_ALL = "all";
export const WS_PATH = "/ws";

// Build the ws:// or wss:// URL for the current origin.
export function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${WS_PATH}`;
}
