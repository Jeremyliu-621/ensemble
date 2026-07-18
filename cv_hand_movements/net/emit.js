// Gesture -> server message mapping. This is the ONLY place that knows how the
// CV app's local gesture vocabulary translates onto the Phoneharmonic wire
// protocol. Everything here is best-effort: if the socket is down, Conn.send
// queues (then drops), so the CV app still works fully offline.
//
// Server contract (server/protocol.py):
//   admin.cmd {cmd: "start"|"stop"|"rewind"|"forward"}   transport (admin role allowed)
//   wand.mode {mode: "ai"|"det"}                          edit mode (no role guard)
//   cv.state {gesture, mode, confidence}                    current debounced CV state
//
// SELECT has no server message: instrument selection is realized by the
// physical wand's aim (server integrates its IMU yaw), so SELECT is a local UI
// state only. Latching a selection is a documented follow-up.

const ADMIN_CMD = "admin.cmd";
const WAND_MODE = "wand.mode";
const CV_STATE = "cv.state";

// Our four local modes -> server wand.mode value (or null = no server message).
const MODE_TO_SERVER = {
  DETERMINISTIC: "det",
  AI: "ai",
  SELECT: null,   // selection lives in the wand's aim, not a mode message
  NONE: null,
};

export class ServerEmitter {
  constructor(conn, { onEmit } = {}) {
    this.conn = conn;
    this.onEmit = onEmit || (() => {});
    this._lastServerMode = null;
    this._cvState = { t: CV_STATE, gesture: null, mode: "NONE", confidence: 0 };
    this._lastCvSignature = null;
  }

  // Transport verb: "start" | "stop" | "rewind" | "forward".
  transport(cmd) {
    this.conn.send({ t: ADMIN_CMD, cmd });
    this.onEmit(`admin.cmd ${cmd}`);
  }

  // Called on every local mode change. Only emits when the *server-visible*
  // mode actually changes (SELECT/NONE collapse to no-op, and don't thrash the
  // server when toggling between them).
  mode(localMode) {
    const serverMode = MODE_TO_SERVER[localMode] ?? null;
    if (serverMode === null || serverMode === this._lastServerMode) return;
    this._lastServerMode = serverMode;
    this.conn.send({ t: WAND_MODE, mode: serverMode });
    this.onEmit(`wand.mode ${serverMode}`);
  }

  // Called every camera frame, but only sends when the debounced gesture or
  // sticky local mode changes. Confidence is sampled at that transition so the
  // server gets useful context without receiving a 30/60 fps log stream.
  state(gesture, mode, confidence = 0) {
    const boundedConfidence = Number.isFinite(confidence)
      ? Math.max(0, Math.min(1, confidence))
      : 0;
    const next = {
      t: CV_STATE,
      gesture: gesture || null,
      mode: typeof mode === "string" ? mode : "NONE",
      confidence: boundedConfidence,
    };
    const signature = `${next.gesture ?? ""}|${next.mode}`;
    this._cvState = next;
    if (signature === this._lastCvSignature) return;
    this._lastCvSignature = signature;
    this.conn.send(next);
  }

  // Re-advertise the latest state after a reconnect. The server also dedupes,
  // so a queued pre-welcome state and this sync cannot produce duplicate logs.
  syncState() {
    this.conn.send({ ...this._cvState });
  }
}
