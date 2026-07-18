// Reconnecting WebSocket wrapper. Handles hello/welcome handshake, persists a
// client_id in localStorage so a section rejoins as itself, and dispatches
// messages by type to registered handlers.

import { HELLO, WELCOME, PROTOCOL_VERSION, wsUrl } from "./protocol.js";

const BACKOFF_START = 500;
const BACKOFF_MAX = 4000;

export class Conn {
  constructor({ role, session, name = "", key = null }) {
    this.role = role;
    this.session = session;
    this.name = name;
    // Distinct storage key per logical client, so two connections of the same
    // role in one tab (e.g. the stage + its overlay) don't share a client-id and
    // clobber each other in the server registry.
    this._key = key || role;
    this.clientId = localStorage.getItem(`wm.clientId.${this._key}`) || null;
    this.ws = null;
    this.welcome = null;
    this._handlers = new Map();     // type -> fn(msg)
    this._onOpen = null;            // fn(welcome)
    this._onClose = null;
    this._backoff = BACKOFF_START;
    this._closed = false;
  }

  on(type, fn) { this._handlers.set(type, fn); return this; }
  onOpen(fn) { this._onOpen = fn; return this; }
  onClose(fn) { this._onClose = fn; return this; }

  connect() {
    this._closed = false;
    this._open();
  }

  close() {
    this._closed = true;
    if (this.ws) this.ws.close();
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  _open() {
    const ws = new WebSocket(wsUrl());
    this.ws = ws;

    ws.onopen = () => {
      this._backoff = BACKOFF_START;
      ws.send(JSON.stringify({
        t: HELLO, v: PROTOCOL_VERSION, role: this.role,
        session: this.session, client_id: this.clientId, name: this.name,
      }));
    };

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.t === WELCOME) {
        this.welcome = msg;
        this.clientId = msg.client_id;
        localStorage.setItem(`wm.clientId.${this._key}`, msg.client_id);
        if (this._onOpen) this._onOpen(msg);
        return;
      }
      const h = this._handlers.get(msg.t);
      if (h) h(msg);
    };

    ws.onclose = () => {
      if (this._onClose) this._onClose();
      if (!this._closed) {
        setTimeout(() => this._open(), this._backoff);
        this._backoff = Math.min(this._backoff * 2, BACKOFF_MAX);
      }
    };

    ws.onerror = () => { try { ws.close(); } catch {} };
  }
}
