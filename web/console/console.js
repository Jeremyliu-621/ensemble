// Maestro Console — the room IS the interface.
//
// CENTER  the playground: live camera-wand hub in the middle; each INSTRUMENT is
//         a draggable card wired to the hub. Dragging a card tells the server
//         where those phones really sit (stage.place, live); doubled phones (two
//         phones, one instrument) share one card and move together. Tap a card
//         to conduct just that instrument.
// LEFT    the score: one lane per track, colour-keyed to the room cards.
// RIGHT   live gesture bars + what your last move did + target + status.
// BOTTOM  transport + a live piano roll of every note under a playhead.
//
// One ws connection as `stage` (roster/notes/engine state + transport). The
// camera iframe (../cvwand/) opens its own wand connection. When no phone has
// joined, this screen is also the orchestra (plays the SECTION_ALL stream).

import { Conn } from "../shared/ws.js";
import { Clock } from "../shared/clock.js";
import { Synth } from "../shared/synth.js";
import { artistFor } from "../shared/artists.js";
import { effectLabel } from "../shared/vocab.js";
import * as P from "../shared/protocol.js";

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";
const el = (id) => document.getElementById(id);

console.log("[console] boot, ws target:", location.host);

const SEMI = { C: 0, "C#": 1, D: 2, "D#": 3, E: 4, F: 5, "F#": 6, G: 7, "G#": 8, A: 9, "A#": 10, B: 11 };
const noteToMidi = (n) => { const m = /^([A-G]#?)(-?\d+)$/.exec(n || ""); return m ? (parseInt(m[2], 10) + 1) * 12 + SEMI[m[1]] : 60; };
const ICONS = ["drums", "piano", "bass", "violin", "cello", "viola", "flute",
  "clarinet", "trumpet", "harp", "bell", "synth"];

// ── one accent per instrument, everywhere it appears (reads on cream paper) ──
const PALETTE = ["#d9534a", "#e8a13c", "#57a639", "#8e5bd4", "#4a76d8", "#2f9e9e", "#d858a8", "#8a6d4f"];
const colorMap = new Map();          // instrument -> colour (assigned in first-seen order)
function colorOf(instrument) {
  if (!instrument) return "#8a6d4f";
  if (!colorMap.has(instrument)) colorMap.set(instrument, PALETTE[colorMap.size % PALETTE.length]);
  return colorMap.get(instrument);
}
const iconFor = (inst) => `../assets/pixel/icon_${ICONS.includes(inst) ? inst : "synth"}.png`;

let conn = null, clock = null, synth = null;
let started = false, camStarted = false, audioReady = false;
let sections = [];                   // connected sections from the roster
let groups = [];                     // [{key, instrument, members[], px, py, placed}]
let aimedGroup = null;               // group key being conducted (null = everyone)
let engineAimed = null;              // server's aimed section id
let transport = null;                // engine.transport for the bar position
const cardEls = new Map();           // group key -> card element
const secGroup = new Map();          // section id -> group key (for note pulses)
const seeded = new Set();            // group keys we've auto-fanned once
const posCache = new Map();          // group key -> {px, py, placed}: last known good
                                     // position, so a roster without placement (old
                                     // server, race) can never NaN the card/link
const notes = [];                    // bottom-roll notes
const seen = new Set();
const secInstrument = new Map();     // section id -> instrument (roll colours)
let lastChoice = null;
let curSong = null;                  // engine song identity (clears the drop's busy state)
let dragging = null;                 // {key, moved, lastSend} during a card drag
let wandYaw = null, wandGrabbed = false, wandSeen = 0;   // hardware-wand pointing beam

// ── room coordinates: px ∈ [-1,1], py ∈ [0,1], hub at (0, 0.5) = map centre ──
function roomBox() { const r = el("room").getBoundingClientRect(); return r; }
function toScreen(px, py, r = roomBox()) {
  return { x: r.width * (0.5 + px * 0.44), y: r.height * (0.94 - py * 0.88) };
}
function fromScreen(sx, sy, r = roomBox()) {
  return {
    px: Math.max(-1, Math.min(1, (sx / r.width - 0.5) / 0.44)),
    py: Math.max(0, Math.min(1, (0.94 - sy / r.height) / 0.88)),
  };
}
// keep a card fully OFF the camera hub: clamp its centre out of the real #cambox
// rectangle grown by half the card (so the whole card, not just its centre,
// clears the live CV). Works in screen space, then converts back to room coords.
function clampOffHub(px, py, halfW = 50, halfH = 58, r = roomBox()) {
  const cam = el("cambox").getBoundingClientRect();
  if (!cam.width || !r.width) return { px, py };
  const padX = halfW + 4, padY = halfH + 4;               // card half-size + a hair
  const fx0 = (cam.left - r.left) - padX, fx1 = (cam.right - r.left) + padX;
  const fy0 = (cam.top - r.top) - padY, fy1 = (cam.bottom - r.top) + padY;
  const s = toScreen(px, py, r);
  let x = s.x, y = s.y;
  if (x > fx0 && x < fx1 && y > fy0 && y < fy1) {         // inside → push to nearest edge
    const dL = x - fx0, dR = fx1 - x, dT = y - fy0, dB = fy1 - y;
    const m = Math.min(dL, dR, dT, dB);
    if (m === dL) x = fx0; else if (m === dR) x = fx1;
    else if (m === dT) y = fy0; else y = fy1;
  }
  return fromScreen(x, y, r);
}

// ── grouping: phones sharing an instrument = one card, dragged together ──────
function rebuildGroups() {
  const byInst = new Map();
  secGroup.clear();
  for (const s of sections) {
    const key = s.instrument || s.id;
    if (!byInst.has(key)) byInst.set(key, []);
    byInst.get(key).push(s);
    secGroup.set(s.id, key);
    secInstrument.set(s.id, s.instrument);
  }
  groups = [...byInst.entries()].map(([key, members]) => {
    // Prefer the server's placement; fall back to the last position this client
    // knew (seed or drag) so the card and its link never jump to (0,0).
    const lead = members.find((m) => m.placed && Number.isFinite(m.px) && Number.isFinite(m.py));
    const cached = posCache.get(key);
    const px = lead ? lead.px : (cached ? cached.px : 0);
    const py = lead ? lead.py : (cached ? cached.py : 0.9);
    const placed = !!lead || !!(cached && cached.placed);
    return { key, instrument: members[0].instrument, members, px, py, placed };
  });

  // Fan brand-new groups around the hub so every card starts somewhere sensible,
  // and push that placement so the server knows immediately. User drags to refine.
  const fresh = groups.filter((g) => !g.placed && !seeded.has(g.key));
  fresh.forEach((g) => {
    const i = groups.indexOf(g), n = Math.max(4, groups.length);
    const a = (i / n) * 2 * Math.PI;                 // 0 = top of the map, clockwise
    g.px = 0.62 * Math.sin(a); g.py = 0.5 + 0.36 * Math.cos(a);
    seeded.add(g.key);
    posCache.set(g.key, { px: g.px, py: g.py, placed: false });
    sendPlace(g);
  });
}
function sendPlace(g) {
  for (const m of g.members) conn.send({ t: P.STAGE_PLACE, section_id: m.id, px: g.px, py: g.py });
}

// ── render: cards + links ────────────────────────────────────────────────────
function upsertCards() {
  for (const [key, node] of cardEls) {
    if (!groups.some((g) => g.key === key)) { node.remove(); cardEls.delete(key); }
  }
  const r = roomBox();
  groups.forEach((g) => {
    let node = cardEls.get(g.key);
    if (!node) {
      node = document.createElement("div");
      node.className = "card";
      node.innerHTML = `<div class="box"><span class="xn" hidden></span><img alt=""><div class="nm"></div><div class="mem"></div></div>`;
      el("room").appendChild(node);
      cardEls.set(g.key, node);
      attachDrag(node, g.key);
    }
    node.style.setProperty("--c", colorOf(g.instrument));
    node.querySelector("img").src = artistFor(g.instrument);   // the performer, not an icon
    node.querySelector(".nm").textContent = g.instrument;
    const xn = node.querySelector(".xn");
    xn.hidden = g.members.length < 2;
    xn.textContent = "×" + g.members.length;
    node.querySelector(".mem").innerHTML = g.members
      .map((m) => `<span class="chip${m.ready ? "" : " wait"}">${m.id}</span>`).join("");
    node.classList.toggle("ghost", !g.placed);
    node.classList.toggle("dropped", !g.members.some((m) => m.ready));
    node.classList.toggle("aimed", g.key === aimedGroup);
    if (dragging?.key !== g.key) {
      const p = toScreen(g.px, g.py, r);
      node.style.left = p.x + "px";
      node.style.top = p.y + "px";
    }
  });
  drawLinks();
}

function drawLinks() {
  const r = roomBox();
  const hub = { x: r.width / 2, y: r.height / 2 };
  // one shared soft-glow filter so the cables read as luminous strands, not flat lines
  let out = `<defs><filter id="cableglow" x="-50%" y="-50%" width="200%" height="200%">`
    + `<feGaussianBlur stdDeviation="2.6"/></filter></defs>`;
  groups.forEach((g) => {
    const node = cardEls.get(g.key);
    const p = dragging?.key === g.key && node
      ? { x: parseFloat(node.style.left), y: parseFloat(node.style.top) }
      : toScreen(g.px, g.py, r);
    const c = colorOf(g.instrument);
    const aimed = g.key === aimedGroup;
    // orthogonal "flow-chart" elbow: run along the dominant axis out of the hub,
    // turn once, and come into the card square (rounded corner via linejoin)
    const horizFirst = Math.abs(p.x - hub.x) >= Math.abs(p.y - hub.y);
    const cx = horizFirst ? p.x : hub.x;
    const cy = horizFirst ? hub.y : p.y;
    const d = `M ${hub.x} ${hub.y} L ${cx} ${cy} L ${p.x} ${p.y}`;
    // soft coloured glow → dark ink underlay (reads on busy art) → solid core → bright flowing strand
    out += `<path d="${d}" stroke="${c}" stroke-opacity="${aimed ? 0.55 : 0.3}" stroke-width="${aimed ? 10 : 7.5}" filter="url(#cableglow)"/>`;
    out += `<path d="${d}" stroke="#362619" stroke-opacity="0.3" stroke-width="${aimed ? 7 : 5.4}"/>`;
    out += `<path d="${d}" stroke="${c}" stroke-opacity="${aimed ? 0.95 : 0.55}" stroke-width="${aimed ? 3.4 : 2}"/>`;
    out += `<path class="flow" d="${d}" stroke="#fbf2dd" stroke-opacity="${aimed ? 0.9 : 0.6}" stroke-width="${aimed ? 2 : 1.5}"/>`;
  });

  // ── the wand's pointing beam: where the hardware wand aims, live ──────────
  // wand.state carries yaw_deg on the SAME azimuth convention as placement
  // (0 = top of the map, + = right), so the beam and the cards share one
  // geometry — the card inside the ±40° lock cone glows via the engine's aim.
  if (wandYaw !== null && performance.now() - wandSeen < 1500) {
    const R = Math.hypot(r.width, r.height);
    const at = (deg) => {
      const a = (deg * Math.PI) / 180;
      return { x: hub.x + R * Math.sin(a), y: hub.y - R * Math.cos(a) };
    };
    const tip = at(wandYaw), le = at(wandYaw - 40), re = at(wandYaw + 40);
    const c = wandGrabbed ? "#3fae4a" : "#8a6d4f";
    out += `<polygon points="${hub.x},${hub.y} ${le.x},${le.y} ${re.x},${re.y}" fill="${c}" fill-opacity="0.06" stroke="none"/>`;
    out += `<line x1="${hub.x}" y1="${hub.y}" x2="${tip.x}" y2="${tip.y}" stroke="#362619" stroke-opacity="0.3" stroke-width="6"/>`;
    out += `<line class="flow" x1="${hub.x}" y1="${hub.y}" x2="${tip.x}" y2="${tip.y}" stroke="${c}" stroke-opacity="0.95" stroke-width="3.4"/>`;
  }
  el("links").innerHTML = out;
}

// ── drag + tap on cards ──────────────────────────────────────────────────────
function attachDrag(node, key) {
  node.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    node.setPointerCapture(e.pointerId);
    dragging = { key, moved: 0, x0: e.clientX, y0: e.clientY, lastSend: 0 };
  });
  node.addEventListener("pointermove", (e) => {
    if (!dragging || dragging.key !== key) return;
    dragging.moved += Math.abs(e.clientX - dragging.x0) + Math.abs(e.clientY - dragging.y0);
    dragging.x0 = e.clientX; dragging.y0 = e.clientY;
    const r = roomBox();
    const raw = fromScreen(e.clientX - r.left, e.clientY - r.top, r);
    const pos = clampOffHub(raw.px, raw.py, node.offsetWidth / 2, node.offsetHeight / 2, r);
    const g = groups.find((x) => x.key === key);
    if (!g) return;
    g.px = pos.px; g.py = pos.py; g.placed = true;
    posCache.set(key, { px: g.px, py: g.py, placed: true });
    node.classList.remove("ghost");
    const p = toScreen(g.px, g.py, r);
    node.style.left = p.x + "px"; node.style.top = p.y + "px";
    drawLinks();
    const now = performance.now();                    // live placement, throttled
    if (now - dragging.lastSend > 120) { dragging.lastSend = now; sendPlace(g); }
  });
  node.addEventListener("pointerup", () => {
    if (!dragging || dragging.key !== key) return;
    const wasTap = dragging.moved < 6;
    const g = groups.find((x) => x.key === key);
    dragging = null;
    if (wasTap) setAim(aimedGroup === key ? null : key);
    else if (g) sendPlace(g);                          // commit final spot
  });
}

// ── aim: tap a card → conduct just that instrument ───────────────────────────
// (the room cards are now the whole UI for this — tap to aim, tap again to clear)
function setAim(key) {
  aimedGroup = key;
  const g = key && groups.find((x) => x.key === key);
  conn.send({ t: P.ADMIN_CMD, cmd: "aim", args: { section_id: g ? g.members[0].id : "all" } });
  upsertCards();
}

// ── roster ───────────────────────────────────────────────────────────────────
function renderRoster(m) {
  sections = (m.sections || []).filter((s) => s.connected);

  const th = sections.filter((s) => s.ready && s.theta != null).map((s) => s.theta);
  const sp = el("spread");
  if (th.length >= 2) {
    const v = Math.max(...th) - Math.min(...th);
    sp.textContent = v.toFixed(0) + "ms";
    sp.classList.toggle("good", v <= 30);
  } else { sp.textContent = sections.length ? "…" : "solo"; sp.classList.remove("good"); }

  rebuildGroups();
  // keep aim in sync if the aimed group vanished
  if (aimedGroup && !groups.some((g) => g.key === aimedGroup)) aimedGroup = null;
  upsertCards();
  el("roomhint").hidden = sections.length === 0;
  applyEngine(m.engine);
}

// pulse card + link when a section's note sounds
function pulse(section) {
  const keys = section === P.SECTION_ALL ? [...cardEls.keys()] : [secGroup.get(section)].filter(Boolean);
  keys.forEach((k) => {
    const node = cardEls.get(k);
    if (!node) return;
    node.classList.add("hit");
    clearTimeout(node._t); node._t = setTimeout(() => node.classList.remove("hit"), 130);
    // sustained "performing" sway: stays on while notes keep landing
    node.classList.add("playing");
    clearTimeout(node._p); node._p = setTimeout(() => node.classList.remove("playing"), 700);
  });
}

// ── engine state: gesture, action, lanes, transport ──────────────────────────
let bpmNow = 100;                    // last engine bpm; the header ± nudges from here

function bar(id, v, max) { el("f-" + id).style.width = Math.max(0, Math.min(1, v / max)) * 100 + "%"; }
function applyEngine(eng) {
  if (!eng) return;
  const bpm = Math.round(eng.bpm);
  bpmNow = bpm;
  el("bpmlbl").textContent = bpm;
  el("songname").textContent = eng.song || "—";
  if (eng.song !== curSong) { curSong = eng.song; dropIdle(); }   // new song landed
  if (eng.bars) el("barslbl").textContent = eng.bars + " bars";
  if (eng.transport) transport = eng.transport;
  // The server's aim is the truth (tap OR the camera's SELECT pointing) — the
  // glowing card must follow it, whoever set it. ENGINE_STATE omits `aimed`,
  // so only rosters (which carry the full status) may move it.
  if (eng.aimed !== undefined) {
    engineAimed = eng.aimed || null;
    const g2 = engineAimed ? groups.find((x) => x.members.some((m) => m.id === engineAimed)) : null;
    const key = g2 ? g2.key : null;
    if (key !== aimedGroup) { aimedGroup = key; upsertCards(); }
  }

  const g = eng.gesture;
  if (g) {
    el("v-energy").textContent = (g.energy ?? 0).toFixed(2); bar("energy", g.energy ?? 0, 1);
    el("v-size").textContent = (g.size ?? 0).toFixed(2); bar("size", g.size ?? 0, 1);
    el("v-vertical").textContent = (g.vertical ?? 0).toFixed(2); bar("vertical", Math.abs(g.vertical ?? 0), 1);
    el("v-rotation").textContent = (g.rotation ?? 0).toFixed(2); bar("rotation", g.rotation ?? 0, 1);
  }

  // ONE vocabulary everywhere: the engine's `device` = what the last gesture
  // ACTUALLY did (same words as the moves card and the camera flash).
  if (eng.device !== undefined && eng.device !== lastChoice) {
    lastChoice = eng.device;
    const fx = effectLabel(eng.device);
    el("what").textContent = `${fx.icon} ${fx.label}`;
  }
  const fx = effectLabel(eng.device);
  el("nowplaying").innerHTML = eng.playing
    ? `now playing <b>${eng.song || "—"}</b> · ${fx.icon} ${fx.label}` : "press ▶ to start";
  // ENGINE_STATE is a light update WITHOUT tracks (they ride on ROSTER only) —
  // rebuilding lanes from it would blank the score until the next roster.
  if (eng.tracks) renderLanes(eng.tracks);
}

// ── left lanes ───────────────────────────────────────────────────────────────
let laneSig = "";
function renderLanes(tracks) {
  const playable = tracks.filter((t) => t.roll).slice(0, 12);
  el("leftempty").hidden = playable.length > 0 && playable[0].name !== "melody";
  // who plays each instrument (chips linking lanes to room cards)
  const who = new Map();
  sections.forEach((s) => {
    if (!who.has(s.instrument)) who.set(s.instrument, []);
    who.get(s.instrument).push(s.id);
  });
  const sig = playable.map((t) => t.name + ":" + t.note_count).join("|") + "©" + [...who.keys()].join(",");
  if (sig === laneSig) return;
  laneSig = sig;
  const host = el("lanes");
  host.innerHTML = "";
  const bars = Math.max(1, ...playable.map((t) => (t.roll.length ? Math.max(...t.roll.map((x) => x[0])) + 1 : 1)));
  playable.forEach((t) => {
    const col = t.is_drum ? "#9a8a74" : colorOf(t.instrument);
    const lane = document.createElement("div");
    lane.className = "lane";
    lane.style.borderColor = col;
    const players = (who.get(t.instrument) || []).join(" ");
    lane.innerHTML = `<div class="lbl"><img class="ico" src="${iconFor(t.instrument)}" alt="">` +
      `<span class="nm" style="color:${col}">${t.instrument || t.name}</span>` +
      (t.is_melody ? `<span class="star">⭐</span>` : "") +
      `<span class="who">${players || "laptop"}</span></div><canvas></canvas>`;
    host.appendChild(lane);
    drawLane(lane.querySelector("canvas"), t, bars, col);
  });
}
function drawLane(canvas, track, bars, col) {
  const dpr = window.devicePixelRatio || 1;
  const W = (canvas.width = canvas.clientWidth * dpr);
  const H = (canvas.height = 30 * dpr);
  const ctx = canvas.getContext("2d");
  const total = bars * 16, LO = 36, HI = 84;
  ctx.fillStyle = col;
  for (const [b, on, dur, pitch] of track.roll) {
    const x = ((b * 16 + on) / total) * W;
    const w = Math.max(1.2 * dpr, (dur / total) * W);
    const y = H - ((Math.max(LO, Math.min(HI, pitch)) - LO) / (HI - LO)) * (H - 4 * dpr) - 2 * dpr;
    ctx.fillRect(x, y - 1.3 * dpr, w, 2.6 * dpr);
  }
}

// ── bottom roll ──────────────────────────────────────────────────────────────
const WINDOW_MS = 4200, FUTURE_MS = 1500, RLO = 36, RHI = 96;
// The roll's x-position is purely a function of wall-clock "now" vs. each
// note's timestamp — nothing in it ever consulted `transport.playing`, so
// even correctly-held notes kept crawling left forever because real time
// itself never stops. `rollNow` holds still while paused, and on resume eases
// back up to live time over a few frames instead of snapping straight to it
// (a live note's real timestamp never moved while frozen, so jumping directly
// to "now" would yank every bar sideways in one frame).
let rollNow = null;
function drawRoll() {
  // try/finally: one bad frame must never kill the animation loop for good —
  // a dead rAF chain looks exactly like "the roll stopped working".
  try {
    const canvas = el("rollcanvas");
    const dpr = window.devicePixelRatio || 1;
    const W = (canvas.width = canvas.clientWidth * dpr);
    const H = (canvas.height = canvas.clientHeight * dpr);
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    const live = clock && clock.theta !== null ? clock.serverNow() : null;
    const playing = !!(transport && transport.playing);
    if (live != null) {
      if (rollNow == null) rollNow = live;
      else if (playing) {
        const gap = live - rollNow;
        rollNow += Math.abs(gap) < 4 ? gap : gap * 0.35;   // ease in, then snap the last bit
      }
      // else: paused — rollNow holds at whatever it last was, untouched.
    }
    const now = rollNow;
    if (now == null) return;
    const pxPerMs = W / WINDOW_MS;
    for (let i = notes.length - 1; i >= 0; i--) {
      const n = notes[i];
      const x = W - (now + FUTURE_MS - n.at) * pxPerMs;
      const w = Math.max(3 * dpr, n.dur * pxPerMs);
      if (x + w < 0) { notes.splice(i, 1); continue; }
      if (x > W) {          // not visible yet — but a note stamped in a dead
        if (n.at - now > 60_000) notes.splice(i, 1);   // timebase never will be
        continue;
      }
      const y = H - ((Math.max(RLO, Math.min(RHI, n.pitch)) - RLO) / (RHI - RLO)) * (H - 8 * dpr) - 4 * dpr;
      ctx.globalAlpha = n.at <= now ? 0.95 : 0.4;
      ctx.fillStyle = n.color;
      ctx.fillRect(x, y - 3 * dpr, w, 6 * dpr);
    }
    ctx.globalAlpha = 1;
  } finally {
    requestAnimationFrame(drawRoll);
  }
}
function ingest(e) {
  if (seen.has(e.id)) return;
  seen.add(e.id);
  if (seen.size > 4000) seen.clear();
  const col = e.art === "drum" ? "#9a8a74"
    : (e.section === P.SECTION_ALL ? "#8a6d4f" : colorOf(secInstrument.get(e.section)));
  notes.push({ at: e.at, dur: e.dur || 200, pitch: noteToMidi(e.note), color: col });
  const delay = clock && clock.theta !== null ? Math.max(0, Math.min(1500, e.at - clock.serverNow())) : 0;
  setTimeout(() => pulse(e.section), delay);
}

// bar position ticker (from the engine transport, on the synced clock)
setInterval(() => {
  // wand gone quiet -> the stroke line goes quiet too (same rule as the beam)
  if (wandSeen && performance.now() - wandSeen > 1500 && el("stroke").textContent !== "…") {
    el("stroke").textContent = "…";
    el("stroke").classList.remove("on");
  }
  if (!transport || !transport.playing || !clock || clock.theta === null) { el("barpos").textContent = "–"; return; }
  const t = clock.serverNow() - transport.anchor;
  if (t < 0 || !transport.bar_ms || !transport.n_bars) { el("barpos").textContent = "–"; return; }
  const b = Math.floor(t / transport.bar_ms) % transport.n_bars;
  el("barpos").textContent = `${b + 1}/${transport.n_bars}`;
}, 200);

// ── audio ────────────────────────────────────────────────────────────────────
async function ensureAudio() {
  if (audioReady) return;
  try { await synth.unlock(); clock.attachAudio(synth.ctx); audioReady = true; }
  catch (err) { console.warn("[console] audio unlock failed", err); }
}

// ── wire up ──────────────────────────────────────────────────────────────────
conn = new Conn({ role: "stage", session, key: "console", ephemeral: true });
clock = new Clock((o) => conn.send(o));
synth = new Synth(clock, null);

conn.on(P.CLOCK_PONG, (m) => clock.handlePong(m));
conn.on(P.ROSTER, renderRoster);
conn.on(P.ENGINE_STATE, applyEngine);
conn.on(P.SCHED_NOTES, (m) => {
  let readyCount = sections.filter((s) => s.ready).length;
  for (const e of m.events) {
    ingest(e);
    if (started && readyCount === 0 && e.section === P.SECTION_ALL) synth.schedule(e);
  }
});
// allnotesoff (FIST/stop): kill the audio only. The roll itself now freezes
// on `transport.playing` going false (see drawRoll), so there's no need to
// touch `notes` here — trying to prune it on pause made already-visible
// upcoming bars vanish outright, which read as worse than the thing being
// fixed. Left untouched, they just sit frozen in place until resume.
conn.on(P.SCHED_CANCEL, (m) => { if (m.allnotesoff) synth.panic(); });
// Deterministic-mode expression: the console IS the orchestra when no phones
// have joined, so it must honour the tilt-driven pitch/volume/filter stream
// (section pages do the same; without this, det mode is silent here).
conn.on(P.FX_EXPR, (m) => { if (m.section === P.SECTION_ALL) synth.setExpression(m.semis, m.gain); });
conn.on(P.FX_TENSION, (m) => synth.setTension(m.value));
// hardware-wand streaming (~7 Hz): pointing beam + live meters + stroke intent
const STROKE_LABELS = {
  LEFT_SWIPE: "← LEFT SWIPE", RIGHT_SWIPE: "→ RIGHT SWIPE",
  POINT_LEFT: "← POINT LEFT", POINT_RIGHT: "→ POINT RIGHT",
  RAISE: "↑ RAISE", LOWER: "↓ LOWER", CIRCLE: "⟳ CIRCLE",
  ROLL_LEFT: "↺ ROLL LEFT", ROLL_RIGHT: "↻ ROLL RIGHT",
  STAB: "➤ STAB", SHAKE: "≋ SHAKE",
  HARMONY: "⚡ HARMONY", ARPEGGIO: "🎶 ARPEGGIO", RUNS: "🏃 RUNS",
  SWELL: "🙌 SWELL", HUSH: "🤫 HUSH",
};
conn.on(P.WAND_STATE, (m) => {
  if (m.pose_captured !== undefined) {
    el("posestat").textContent = m.pose_captured
      ? `taught: ${(m.poses || []).join(" · ")}`
      : "no wand data yet — is the board streaming?";
    return;
  }
  if (m.yaw_deg === undefined) return;
  wandYaw = m.yaw_deg;
  wandGrabbed = !!m.grabbed;
  wandSeen = performance.now();
  drawLinks();
  if (m.live) {                       // the panel breathes with the wand, live
    el("v-energy").textContent = (m.live.energy ?? 0).toFixed(2); bar("energy", m.live.energy ?? 0, 1);
    el("v-size").textContent = (m.live.size ?? 0).toFixed(2); bar("size", m.live.size ?? 0, 1);
    el("v-vertical").textContent = (m.live.lift ?? 0).toFixed(2); bar("vertical", Math.abs(m.live.lift ?? 0), 1);
    el("v-rotation").textContent = (m.live.swirl ?? 0).toFixed(2); bar("rotation", m.live.swirl ?? 0, 1);
  }
  const lbl = STROKE_LABELS[m.stroke];
  el("stroke").textContent = lbl || "…";
  el("stroke").classList.toggle("on", !!lbl);
});

conn.onOpen((welcome) => {
  // A fresh server process restarts its note-id counter; stale "seen" ids would
  // make the roll drop every new event while the audio path keeps playing.
  seen.clear();
  notes.length = 0;
  const cfg = welcome.config || {};
  const join = cfg.wand_url || cfg.join_url;
  if (join && window.qrcode) {
    const qr = window.qrcode(0, "M"); qr.addData(join); qr.make();
    el("qr").innerHTML = qr.createSvgTag({ cellSize: 5, margin: 1, scalable: true });
  }
});
el("start").addEventListener("click", async () => {
  await ensureAudio();
  started = true;
  conn.send({ t: P.ADMIN_CMD, cmd: "start" });
});
el("stop").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "stop" }));
el("panic").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "allnotesoff" }));
function nudgeTempo(d) {
  bpmNow = Math.max(60, Math.min(180, bpmNow + d));
  el("bpmlbl").textContent = bpmNow;
  conn.send({ t: P.ADMIN_CMD, cmd: "tempo", args: { bpm: bpmNow } });
}
el("tdown").addEventListener("click", () => nudgeTempo(-4));
el("tup").addEventListener("click", () => nudgeTempo(4));
// One-click songs from songs/ — same message the demo page sends.
document.querySelectorAll(".songbtn[data-song]").forEach((b) =>
  b.addEventListener("click", () => conn.send({ t: P.SONG_FILE, name: b.dataset.song })));
// Aim recalibration: the IMU has no compass, yaw drifts over minutes. Point
// the wand at the laptop, click, and the beam re-zeroes (wandio.recal).
el("recal").addEventListener("click", () => {
  conn.send({ t: P.WAND_RECAL, tw: Math.round(performance.now()) });
  el("recal").textContent = "🎯 neutral set — music reset ✓";
  setTimeout(() => { el("recal").textContent = "🎯 recalibrate — hold neutral, click"; }, 1500);
});
// Pose teaching: hold the wand in a pose, click its button — the server
// records the live sensor reading; classification = nearest taught pose.
document.querySelectorAll(".posebtn").forEach((b) =>
  b.addEventListener("click", () => {
    conn.send({ t: P.WAND_POSE_CAPTURE, pose: b.dataset.pose });
    b.textContent = "✓ " + b.textContent.replace(/^✓ /, "");
  }));

// camera hub — seamless: the camera wand is simply ON. The iframe loads at
// boot (its page auto-starts the webcam; the browser's permission prompt is
// the only gate). Insecure origins (LAN IP over http) get a quiet steer to
// HTTPS instead — phones keep joining via the QR either way. When TLS is
// configured, the server also upgrades a LAN :8080 console request to :8443.
if (window.isSecureContext) {
  el("camframe").src = `../cvwand/?s=${encodeURIComponent(session)}`;
  camStarted = true;
} else {
  const local = `http://localhost:${location.port || 80}${location.pathname}${location.search}`;
  const secure = `https://${location.hostname}:8443${location.pathname}${location.search}`;
  el("camhint").innerHTML =
    `<div class="big">🔒 Camera needs HTTPS</div>
     <div class="sub">Open <a href="${secure}">the secure LAN console</a>
       or, on the hosting laptop, <a href="${local}">localhost:${location.port || 80}</a>.
       Music still works here, and phones keep using the QR.</div>`;
  el("camhint").hidden = false;
}
// Audio needs one real user gesture — take the first click/tap anywhere.
window.addEventListener("pointerdown", () => { ensureAudio(); }, { once: true });


// ── MIDI drop (replaces the song, live) ──────────────────────────────────────
// Same path the editor uses: base64 the file, send SONG_LOAD; the server parses
// the MIDI, rebuilds the song, and the roster/lanes/room repopulate on their own.
function abToBase64(ab) {
  const bytes = new Uint8Array(ab); let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
const drop = el("drop"), dropTxt = drop.querySelector(".dt");
const dropIdle = () => { dropTxt.textContent = "Drop a MIDI here"; drop.classList.remove("busy", "err"); };
async function uploadMidi(file) {
  if (!file) return;
  if (!/\.midi?$/i.test(file.name)) { drop.classList.add("err"); dropTxt.textContent = "not a .mid file"; setTimeout(dropIdle, 1800); return; }
  drop.classList.remove("err"); drop.classList.add("busy");
  dropTxt.textContent = `reading ${file.name}…`;
  try {
    const ab = await file.arrayBuffer();
    conn.send({ t: P.SONG_LOAD, name: file.name, data: abToBase64(ab) });
    dropTxt.textContent = `loading ${file.name}…`;
    setTimeout(dropIdle, 4000);   // same-name reloads don't change the song id — don't stick busy
  } catch (err) {
    console.warn("[console] midi read failed", err);
    drop.classList.remove("busy"); drop.classList.add("err"); dropTxt.textContent = "couldn't read file";
    setTimeout(dropIdle, 2200);
  }
}
drop.addEventListener("click", (e) => { e.preventDefault(); el("midifile").click(); });
el("midifile").addEventListener("change", (e) => { uploadMidi(e.target.files[0]); e.target.value = ""; });
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "dragend"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); uploadMidi(e.dataTransfer.files[0]); });
// when the new song's tracks land, the lane sig changes — clear the busy state
conn.on(P.ERR, (m) => {
  if (m.code === "bad_midi") { drop.classList.remove("busy"); drop.classList.add("err"); dropTxt.textContent = `⚠ ${m.msg || "bad MIDI"}`; setTimeout(dropIdle, 2600); }
});

window.addEventListener("resize", () => upsertCards());

conn.connect();
clock.start();
requestAnimationFrame(drawRoll);
