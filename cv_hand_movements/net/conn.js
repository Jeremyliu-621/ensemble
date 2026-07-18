// Reconnecting WebSocket client for the CV gesture app.
//
// Adapted from web/shared/ws.js, but kept self-contained (no import from web/)
// so cv_hand_movements stays a standalone directory. The CV app joins the
// Phoneharmonic server as role "admin" — NOT a wand role. All wand roles share
// one wand slot (latest wins), so joining as "wand-cv" would clobber the
// physical Arduino wand. As "admin" the CV app drives transport (admin.cmd) and
// mode (wand.mode) while the Arduino owns the wand slot with its IMU stream.

const PROTOCOL_VERSION = 1;
const HELLO = "hello";
const WELCOME = "welcome";

const BACKOFF_START = 500;
const BACKOFF_MAX = 4000;

// Default to the laptop server on :8080. The CV page is usually served from its
// own static server (e.g. :8765) on the same machine, so location.host is the
// wrong port — target :8080 explicitly. Override with ?ws=ws://host:port/ws.
function defaultWsUrl() {
  const override = new URLSearchParams(location.search).get("ws");
  if (override) return override;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const host = location.hostname || "localhost";
  return `${proto}//${host}:8080/ws`;
}

export class Conn {
  constructor({ role = "admin", session = "lol1", name = "cv", url = null } = {}) {
    this.role = role;
    this.session = session;
    this.name = name;
    this.url = url || defaultWsUrl();
    this.clientId = localStorage.getItem(`wm.clientId.${role}`) || null;
    this.ws = null;
    this.welcome = null;
    this._queue = [];              // sends before the socket opens, flushed on connect
    this._handlers = new Map();    // type -> fn(msg)
    this._onOpen = null;
    this._onClose = null;
    this._backoff = BACKOFF_START;
    this._closed = false;
  }

  on(type, fn) { this._handlers.set(type, fn); return this; }
  onOpen(fn) { this._onOpen = fn; return this; }
  onClose(fn) { this._onClose = fn; return this; }

  connect() { this._closed = false; this._open(); }

  close() { this._closed = true; if (this.ws) this.ws.close(); }

  get connected() {
    return Boolean(this.ws && this.ws.readyState === WebSocket.OPEN && this.welcome);
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    } else if (this._queue.length < 50) {
      this._queue.push(obj);       // gesture fired before connect — deliver on open
    }
  }

  _open() {
    let ws;
    try {
      ws = new WebSocket(this.url);
    } catch (e) {
      // Malformed URL etc. — retry with backoff so the app never hard-fails.
      this._retry();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this._backoff = BACKOFF_START;
      ws.send(JSON.stringify({
        t: HELLO, v: PROTOCOL_VERSION, role: this.role,
        session: this.session, client_id: this.clientId, name: this.name,
      }));
      for (const m of this._queue.splice(0)) ws.send(JSON.stringify(m));
    };

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.t === WELCOME) {
        this.welcome = msg;
        this.clientId = msg.client_id;
        if (msg.client_id) localStorage.setItem(`wm.clientId.${this.role}`, msg.client_id);
        if (this._onOpen) this._onOpen(msg);
        return;
      }
      const h = this._handlers.get(msg.t);
      if (h) h(msg);
    };

    ws.onclose = () => {
      this.welcome = null;
      if (this._onClose) this._onClose();
      if (!this._closed) this._retry();
    };

    ws.onerror = () => { try { ws.close(); } catch {} };
  }

  _retry() {
    setTimeout(() => this._open(), this._backoff);
    this._backoff = Math.min(this._backoff * 2, BACKOFF_MAX);
  }
}
