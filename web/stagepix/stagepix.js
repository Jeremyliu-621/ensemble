// Pixel-art stage — a self-contained alternative to web/stage/. Reuses the same
// shared modules (ws, clock, synth, protocol) and the same server messages
// (roster, sched.notes), so it's a drop-in reskin: swap the URL, nothing else.
//
// Behaviour mirrors the current stage: the laptop plays the orchestra when no
// phone has joined as a section; a phone/webcam is the wand (QR). On top of that
// it renders a pixel-art scene where each performer bounces the instant its note
// sounds — visuals are driven off sched.notes directly (timed on the synced
// clock), so the room animates whether the laptop or the phones are sounding.

import { Conn } from "../shared/ws.js";
import { Clock } from "../shared/clock.js";
import { Synth } from "../shared/synth.js";
import * as P from "../shared/protocol.js";

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";
const demoN = parseInt(params.get("demo") || "0", 10);   // preview: fake N sections
const el = (id) => document.getElementById(id);

// Instruments we have sprites for. When no phones have joined, this "house
// ensemble" stands in for the laptop-as-orchestra so the stage is never empty.
const SPRITES = ["violin", "cello", "flute", "trumpet", "harp", "drums", "piano", "synth"];
const HOUSE = ["violin", "cello", "flute", "harp", "drums"];

let conn = null, clock = null, synth = null;
let started = false;
let readySections = 0;
let readyIds = new Set();            // sections currently connected+ready
let performers = [];                 // [{id, instrument, connected, node}]
const seen = new Set();              // sched.notes ids already handled (dedupe)
const lastPulse = new Map();         // performer id -> last pulse ms (throttle)

// --- performer placement ----------------------------------------------------
// Flat, orthographic: musicians stand in a single straight row on the floor
// line, evenly spaced and all the same size (no perspective, no depth). Sprites
// auto-fit their slot so any number of performers fills the row without overlap.
const SPRITE_AR = 128 / 176;   // width / height of a musician sprite

function positionScene() {
  const wrap = el("stagewrap");
  const W = wrap.clientWidth, H = wrap.clientHeight;
  if (!H) return;
  const n = performers.length || 1;
  const spanL = 10, spanR = 90;                // % of stage width used by the row
  const slot = (spanR - spanL) / n;            // width of each performer's slot (%)
  let wPx = Math.min(0.15 * W, (slot * 0.9 / 100) * W);    // sprite width, capped
  let hPx = wPx / SPRITE_AR;
  const maxH = 0.40 * H;                        // don't let a lone musician get huge
  if (hPx > maxH) { hPx = maxH; wPx = hPx * SPRITE_AR; }
  performers.forEach((p, i) => {
    const x = spanL + slot * (i + 0.5);        // centre of the slot
    const node = p.node;
    node.style.left = x + "%";
    node.style.top = "84%";                     // feet on the floor line
    node.style.zIndex = 12 + i;
    node.querySelector(".sprite").style.height = hPx + "px";
  });
  // Ornaments sized from the stage height so everything scales as one.
  el("wand").style.height = (0.16 * H) + "px";
  el("podium").style.height = (0.15 * H) + "px";
  el("chandelier").style.height = (0.20 * H) + "px";
  document.querySelectorAll(".beam").forEach((b) => { b.style.height = (0.72 * H) + "px"; });
}
window.addEventListener("resize", positionScene);

// Map a section to a sprite: honour an explicit instrument if we have art for it,
// else cycle deterministically by section index so the orchestra looks varied.
function spriteFor(id, instrument, idx) {
  if (instrument && instrument !== "synth" && SPRITES.includes(instrument)) return instrument;
  return SPRITES[idx % SPRITES.length];
}

function buildPerformers(sections) {
  const container = el("players");
  container.innerHTML = "";
  performers = [];

  let list;
  if (sections && sections.length) {
    list = sections.map((s, i) => ({
      id: s.id, instrument: spriteFor(s.id, s.instrument, i),
      connected: s.connected, ready: s.ready, theta: s.theta,
    }));
  } else {
    // Laptop-orchestra: a fixed house ensemble; every event is SECTION_ALL so
    // they all bounce together.
    list = HOUSE.map((inst, i) => ({ id: `house${i}`, instrument: inst, connected: true, ready: true }));
  }

  list.forEach((p) => {
    const node = document.createElement("div");
    node.className = "player" + (p.connected ? "" : " dropped");
    const isHouse = p.id.startsWith("house");
    node.innerHTML =
      `<div class="shadow"></div>` +
      `<img class="sprite" src="../assets/${p.instrument}.png" alt="${p.instrument}">` +
      (isHouse ? "" : `<span class="tag">${p.id}${p.ready ? "" : " <span class='off'>·wait</span>"}</span>`);
    container.appendChild(node);
    p.node = node;
    performers.push(p);
  });
  el("count").textContent = list.length;
  positionScene();
  requestAnimationFrame(positionScene);   // re-place once the stage box has real dimensions
}

// --- note visuals -----------------------------------------------------------
function bump(p, vel) {
  if (!p || !p.node) return;
  p.node.classList.remove("hit");
  void p.node.offsetWidth;                 // restart the CSS transition
  p.node.classList.add("hit");
  setTimeout(() => p.node && p.node.classList.remove("hit"), 150);

  // Throttle the floating-note VFX per performer so dense bars don't spam.
  const now = performance.now();
  if (now - (lastPulse.get(p.id) || 0) > 120) {
    lastPulse.set(p.id, now);
    spawnPulse(p, vel);
  }
}

function spawnPulse(p, vel) {
  const wrap = el("stagewrap");
  const rect = p.node.getBoundingClientRect();
  const wr = wrap.getBoundingClientRect();
  const img = document.createElement("img");
  img.className = "pulse";
  img.src = "../assets/note.png";
  const size = 22 + 16 * Math.min(1, vel || 0.7);
  img.style.width = size + "px";
  img.style.left = (rect.left - wr.left + rect.width / 2) + "px";
  img.style.top = (rect.top - wr.top + 6) + "px";
  el("fx").appendChild(img);
  setTimeout(() => img.remove(), 800);
}

// --- ambient sparkle + a gentle (never flashy) beat response ----------------
function makeSparkle(xPct, yPct, sizePx, durMs) {
  const img = document.createElement("img");
  img.src = "../assets/sparkle.png";
  img.className = "sparkle-fx";
  img.style.left = xPct + "%";
  img.style.top = yPct + "%";
  img.style.width = sizePx + "px";
  img.style.animationDuration = durMs + "ms";
  el("fx").appendChild(img);
  setTimeout(() => img.remove(), durMs);
}
function burstSparkles(count) {
  for (let i = 0; i < count; i++) {
    makeSparkle(16 + Math.random() * 68, 34 + Math.random() * 44,
                9 + Math.random() * 13, 700 + Math.random() * 500);
  }
}
let lastBeatFx = 0;
function stageBeat() {
  const now = performance.now();
  if (now - lastBeatFx < 110) return;          // throttle dense bars
  lastBeatFx = now;
  const fl = el("footlights");
  fl.classList.add("beat");
  setTimeout(() => fl.classList.remove("beat"), 130);
  burstSparkles(2);
}
function revealStage() { el("stagewrap").classList.add("reveal"); }

function targetsFor(section) {
  if (section === P.SECTION_ALL) return performers;
  const p = performers.find((x) => x.id === section);
  return p ? [p] : (performers.length ? performers : []);  // fall back to whole ensemble
}

// Schedule each note's bounce at the instant it sounds, on the synced clock.
function visualize(ev) {
  if (seen.has(ev.id)) return;
  seen.add(ev.id);
  if (seen.size > 2000) seen.clear();
  const targets = targetsFor(ev.section);
  let delay = 0;
  if (clock && clock.theta !== null) {
    delay = Math.max(0, Math.min(1500, ev.at - clock.serverNow()));
  }
  setTimeout(() => {
    targets.forEach((t) => bump(t, ev.vel));
    if (ev.section === P.SECTION_ALL) stageBeat();   // whole-room beat → gentle flourish
  }, delay);
}

// --- roster / QR ------------------------------------------------------------
function renderRoster(m) {
  readyIds = new Set(m.sections.filter((s) => s.connected && s.ready).map((s) => s.id));
  readySections = readyIds.size;
  const w = m.wand || {};
  el("wanddot").classList.toggle("ok", !!w.connected);
  el("wandstate").textContent = w.connected ? `connected (${w.variant})` : "none";
  el("wand").classList.toggle("armed", !!w.connected);

  if (!demoN) buildPerformers(m.sections);

  const thetas = m.sections.filter((s) => s.connected && s.theta != null).map((s) => s.theta);
  if (thetas.length >= 2) {
    const spread = Math.max(...thetas) - Math.min(...thetas);
    el("spread").textContent = spread.toFixed(1) + " ms";
    el("spread").style.color = spread <= 30 ? "#46d17a" : "#e5a23d";
  } else {
    el("spread").textContent = readySections ? "(sync…)" : "laptop only";
    el("spread").style.color = "";
  }
}

function renderQR(text) {
  if (!window.qrcode || !text) return;
  const qr = window.qrcode(0, "M");
  qr.addData(text);
  qr.make();
  el("qr").innerHTML = qr.createSvgTag({ cellSize: 5, margin: 1, scalable: true });
}

// --- wire up ----------------------------------------------------------------
conn = new Conn({ role: "stage", session });
clock = new Clock((obj) => conn.send(obj));
synth = new Synth(clock, null);            // visuals handled separately from audio

conn.on(P.CLOCK_PONG, (m) => clock.handlePong(m));
conn.on(P.ROSTER, renderRoster);
conn.on(P.SCHED_NOTES, (m) => {
  for (const e of m.events) {
    visualize(e);                                        // always animate
    if (!started) continue;
    // Laptop is the orchestra when no phone is ready — and it also covers any
    // event targeted at a section that dropped mid-bar, so the room never goes
    // silent waiting for the conductor to re-target.
    const covers = e.section === P.SECTION_ALL ? readySections === 0
                                               : !readyIds.has(e.section);
    if (covers) synth.schedule(e);
  }
});
conn.on(P.SCHED_CANCEL, (m) => { if (m.allnotesoff) synth.panic(); });
conn.on(P.FX_TENSION, (m) => synth.setTension(m.value));

// Wand aim: glow the performer the conductor is pointing at.
conn.on(P.WAND_STATE, (m) => {
  performers.forEach((p) => {
    if (p.node) p.node.style.filter = p.id === m.aim_section ? "drop-shadow(0 0 12px #e7c583)" : "";
  });
});

// Commentator lines from the announcer -> a fading toast over the stage.
conn.on(P.ANNOUNCE, (m) => {
  const d = document.createElement("div");
  d.textContent = m.text;
  d.style.cssText = "position:absolute;left:50%;top:12%;transform:translateX(-50%);" +
    "background:rgba(20,12,8,.88);color:#e7c583;padding:10px 22px;border:1px solid #a8712a;" +
    "border-radius:8px;font-size:20px;z-index:40;max-width:70%;text-align:center;" +
    "transition:opacity .6s;opacity:0;pointer-events:none";
  el("stagewrap").appendChild(d);
  requestAnimationFrame(() => { d.style.opacity = "1"; });
  setTimeout(() => { d.style.opacity = "0"; setTimeout(() => d.remove(), 700); }, 6000);
  if (m.audio_b64) {
    try { new Audio(`data:${m.mime || "audio/mpeg"};base64,${m.audio_b64}`).play(); } catch {}
  }
});

conn.onOpen((welcome) => {
  el("status").textContent = `session ${welcome.config.session}`;
  renderQR(welcome.config.wand_url || welcome.config.join_url);
  clock.checkEpoch(welcome.server_time);   // a server restart mustn't poison the fit
});
conn.onClose(() => { el("status").textContent = "reconnecting…"; });

// Start splash: unlock audio (user gesture) + begin transport.
el("splash").addEventListener("click", async () => {
  el("splash").style.display = "none";
  setTimeout(revealStage, 300);                // raise the curtain on the show
  try {
    await synth.unlock();
    clock.attachAudio(synth.ctx);
  } catch (e) { console.warn("[stagepix] audio unlock failed", e); }
  clock.start();
  started = true;
  sendStart();
});
// A fast splash click can beat the ws handshake; retry until we're welcomed
// (Conn.send silently drops frames on a socket that isn't open yet).
function sendStart() {
  if (conn.welcome) conn.send({ t: P.ADMIN_CMD, cmd: "start" });
  else setTimeout(sendStart, 150);
}
el("start2").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "start" }));
el("stop").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "stop" }));
el("panic").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "allnotesoff" }));
el("introbtn").addEventListener("click", (e) => {
  e.preventDefault();
  el("stagewrap").classList.remove("reveal");
  setTimeout(revealStage, 450);                // replay the curtain-raise
});
setInterval(() => burstSparkles(1), 1500);     // gentle ambient shimmer

// --- embedded webcam-wand dock ----------------------------------------------
// The hand-tracking conductor (../cvwand/) runs in an iframe so you can conduct
// with your hand right on the stage screen. Same origin, so getUserMedia works
// on localhost; it opens its own ws connection as the wand and the stage's wand
// sprite lights up via the roster. MediaPipe is loaded only when first opened.
const camframe = el("camframe");
const camSrc = `../cvwand/?s=${encodeURIComponent(session)}`;
function openCam() {
  if (!camframe.getAttribute("src")) camframe.src = camSrc;
  el("camdock").hidden = false;
  el("camtoggle").hidden = true;
}
function closeCam() { el("camdock").hidden = true; el("camtoggle").hidden = false; }
el("camtoggle").addEventListener("click", openCam);
el("camclose").addEventListener("click", closeCam);
el("cvlink").addEventListener("click", openCam);
el("campop").href = camSrc;

conn.connect();

// --- demo preview (no phones needed): fabricate performers + fake beats -------
if (demoN) {
  buildPerformers(Array.from({ length: demoN }, (_, i) => ({
    id: `s${i + 1}`, instrument: "synth", connected: true, ready: true, theta: 0,
  })));
  el("count").textContent = demoN;
  el("splash").style.display = "none";
  setTimeout(revealStage, 700);                // auto curtain-raise for preview
  let beat = 0;
  setInterval(() => {
    // On-beat: whole ensemble + a stage flourish; off-beats: a random performer.
    if (beat % 2 === 0) { performers.forEach((p) => bump(p, 0.9)); stageBeat(); }
    else bump(performers[Math.floor(performers.length * (beat % 7) / 7)], 0.7);
    beat++;
  }, 500);
}
