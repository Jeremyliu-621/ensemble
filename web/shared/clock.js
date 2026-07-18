// Clock sync — the #1 technical risk lives here.
//
// Estimates this device's clock relative to the server as an AFFINE model
//   serverTime ≈ a + b · performance.now()
// where `a` is the offset and `b` is the clock-RATE ratio (drift compensation).
// Offset-only sync slides audibly over a multi-minute set because cheap phone
// crystals drift tens–hundreds of PPM; fitting `b` by regression over the
// best-RTT ping samples fixes that. (This is what IRCAM's Soundworks does; see
// docs/audio-sync-research.md.)
//
// Timebases:
//   server : server_time_ms()      (monotonic, from the server)
//   client : performance.now()      (monotonic, this tab)
//   audio  : ctx.currentTime * 1000 (this device's audio hardware clock)

import { CLOCK_PING, CLOCK_PONG } from "./protocol.js";

const BURST_COUNT = 10;
const BURST_SPACING_MS = 150;
const PERIODIC_MS = 2000;

const MAX_POINTS = 80;        // ping points retained for the fit
const MAX_AGE_MS = 120_000;   // ...and no older than this
const RTT_TOL_MS = 8;         // keep points within this of the best RTT (least jitter)
const TRAIN_MS = 12_000;      // until points span this long, use offset-only (b = 1)
const MIN_FIT = 8;            // ...and this many good points
const MAX_DRIFT = 0.0005;     // clamp b to ±500 PPM so a re-fit is never audible
const SNAP_MS = 50;           // a serverNow jump bigger than this signals a resync
const EPOCH_MS = 3000;        // pong further than this from the model's prediction
                              // = the timebase itself changed, not jitter

const A2P_SAMPLES = 5;        // median window for the performance->audio anchor

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function linreg(pts) {
  // least-squares serverTime = a + b·localTime, centered for numeric stability
  const n = pts.length;
  let mx = 0, my = 0;
  for (const p of pts) { mx += p.local; my += p.server; }
  mx /= n; my /= n;
  let num = 0, den = 0;
  for (const p of pts) { const dx = p.local - mx; num += dx * (p.server - my); den += dx * dx; }
  const b = den > 0 ? num / den : 1;
  return [my - b * mx, b];
}

export class Clock {
  constructor(send) {
    this._send = send;              // (obj) => void
    this._pending = new Map();      // ping id -> t0
    this._points = [];              // {local, server, rtt}
    this._nextId = 1;
    this._a = null;                 // affine offset; null until first pong
    this._b = 1;                    // affine rate (server ms per client ms)
    this.theta = null;              // derived instantaneous offset (for HUD/health displays)
    this.rtt = null;
    this._ctx = null;
    this._a2p = null;               // ms: performance.now() - ctx.currentTime*1000
    this._a2pSamples = [];
    this.trimSec = 0;               // per-device output-latency compensation
    this.onResync = null;           // callback(deltaMs) on a >SNAP_MS jump
    this._timers = [];
  }

  start() {
    // Idempotent: reconnects call start() again; never stack ping timers.
    this._timers.forEach(clearTimeout);
    this._timers = [];
    if (this._periodic) clearInterval(this._periodic);
    for (let i = 0; i < BURST_COUNT; i++) {
      this._timers.push(setTimeout(() => this._ping(), i * BURST_SPACING_MS));
    }
    this._periodic = setInterval(() => this._ping(), PERIODIC_MS);
  }

  // A welcome's server_time exposes a server restart: old fit points then
  // belong to a dead epoch and would poison the regression for MAX_AGE_MS.
  checkEpoch(serverTimeMs) {
    if (this._a === null || typeof serverTimeMs !== "number") return;
    if (Math.abs(serverTimeMs - this.serverNow()) > 1000) {
      this._points = [];
      this._pending.clear();
      this._a = serverTimeMs - performance.now();  // coarse anchor until pings refine it
      this._b = 1;
    }
  }

  stop() {
    this._timers.forEach(clearTimeout);
    clearInterval(this._periodic);
    if (this._a2pTimer) clearInterval(this._a2pTimer);
  }

  _ping() {
    for (const [id, t0] of this._pending) {       // prune pings lost to a reconnect
      if (performance.now() - t0 > 10_000) this._pending.delete(id);
    }
    const id = this._nextId++;
    const t0 = performance.now();
    this._pending.set(id, t0);
    this._send({ t: CLOCK_PING, id, t0 });
  }

  // Called by the ws layer for every clock.pong.
  handlePong(msg) {
    if (msg.t !== CLOCK_PONG) return;
    const t0 = this._pending.get(msg.id);
    if (t0 === undefined) return;
    this._pending.delete(msg.id);
    const t1 = performance.now();
    const sample = { local: (t0 + t1) / 2, server: msg.ts, rtt: t1 - t0 };
    // Epoch guard: the server clock is per-process monotonic, so a RESTARTED
    // server answers from a brand-new timebase (and a laptop waking from sleep
    // froze ours). Blending those samples with the old ones poisons the fit for
    // up to MAX_AGE_MS — serverNow() lands minutes off and every consumer of
    // server time (roll, playhead, scheduling) silently breaks while the ws
    // looks healthy. Seconds of disagreement can't be jitter: drop the dead
    // timebase and retrain from this sample alone (self-heals in one pong).
    if (this._a !== null && Math.abs(this._a + this._b * sample.local - sample.server) > EPOCH_MS) {
      const jump = sample.server - (this._a + this._b * sample.local);
      console.warn(`[clock] server timebase changed (${(jump / 1000).toFixed(1)}s) — retraining`);
      this._points = [];
      this._a = null;
      this._b = 1;
      if (this.onResync) this.onResync(jump);
    }
    this._points.push(sample);
    const cutoff = performance.now() - MAX_AGE_MS;
    this._points = this._points.filter((p) => p.local >= cutoff);
    if (this._points.length > MAX_POINTS) this._points.shift();
    this._recompute();
  }

  _recompute() {
    const pts = this._points;
    if (!pts.length) return;
    let best = pts[0];
    for (const p of pts) if (p.rtt < best.rtt) best = p;
    this.rtt = best.rtt;

    const good = pts.filter((p) => p.rtt <= best.rtt + RTT_TOL_MS);
    const span = good.length ? good[good.length - 1].local - good[0].local : 0;

    let a, b;
    if (good.length < MIN_FIT || span < TRAIN_MS) {
      // Training phase: offset only, from the least-jittered sample.
      b = 1;
      a = best.server - best.local;
    } else {
      [a, b] = linreg(good);
      b = Math.max(1 - MAX_DRIFT, Math.min(1 + MAX_DRIFT, b));
    }

    const now = performance.now();
    const prev = this._a === null ? null : this._a + this._b * now;
    this._a = a;
    this._b = b;
    const cur = a + b * now;
    this.theta = cur - now;
    if (prev !== null && Math.abs(cur - prev) > SNAP_MS && this.onResync) {
      this.onResync(cur - prev);
    }
  }

  // Server monotonic ms, estimated on this device's clock right now.
  serverNow() {
    return this._a === null ? performance.now() : this._a + this._b * performance.now();
  }

  // --- audio mapping ---
  attachAudio(ctx) {
    this._ctx = ctx;
    this._sampleA2P();
    this._a2pTimer = setInterval(() => this._sampleA2P(), 1000);
  }

  _sampleA2P() {
    if (!this._ctx) return;
    const p0 = performance.now();
    const c = this._ctx.currentTime * 1000;
    const p1 = performance.now();
    this._a2pSamples.push((p0 + p1) / 2 - c);
    if (this._a2pSamples.length > A2P_SAMPLES) this._a2pSamples.shift();
    this._a2p = median(this._a2pSamples);
  }

  // Map a server time (ms) to this AudioContext's time (seconds) for scheduling.
  serverToAudioTime(serverMs) {
    if (this._a === null || this._a2p === null) return null;
    const clientPerf = (serverMs - this._a) / this._b;   // invert the affine model
    return (clientPerf - this._a2p) / 1000 - this.trimSec;
  }

  ready() {
    return this._a !== null && this._a2p !== null;
  }
}
