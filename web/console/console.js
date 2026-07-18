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
import * as P from "../shared/protocol.js";

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";
const el = (id) => document.getElementById(id);

// Boot beacon: if the header still says "connecting", this module never ran;
// "js up — dialing" means we're alive and it's the WebSocket that's stuck.
el("connlbl").textContent = "js up — dialing";
console.log("[console] boot, ws target:", location.host);

const SEMI = { C: 0, "C#": 1, D: 2, "D#": 3, E: 4, F: 5, "F#": 6, G: 7, "G#": 8, A: 9, "A#": 10, B: 11 };
const noteToMidi = (n) => { const m = /^([A-G]#?)(-?\d+)$/.exec(n || ""); return m ? (parseInt(m[2], 10) + 1) * 12 + SEMI[m[1]] : 60; };
const NICE = { lower_imitation: "Lower imitation", contrary_motion: "Contrary motion", sustained: "Sustained chord",
  delayed: "Delayed echo", rhythmic_dense: "Rhythmic — busy", rhythmic: "Rhythmic — busy", rest: "Rest — silence" };
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
// keep cards from landing on top of the camera hub: nudge radially out
function nudgeOffHub(px, py) {
  const dx = px, dy = (py - 0.5) * 2;               // roughly square-ish units
  const d = Math.hypot(dx, dy);
  if (d >= 0.42) return { px, py };
  const f = 0.42 / (d || 1e-6);
  return { px: Math.max(-1, Math.min(1, dx * f)), py: Math.max(0, Math.min(1, 0.5 + (dy * f) / 2)) };
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
    node.querySelector("img").src = iconFor(g.instrument);
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
  let out = "";
  groups.forEach((g) => {
    const node = cardEls.get(g.key);
    const p = dragging?.key === g.key && node
      ? { x: parseFloat(node.style.left), y: parseFloat(node.style.top) }
      : toScreen(g.px, g.py, r);
    const c = colorOf(g.instrument);
    const aimed = g.key === aimedGroup;
    // dark underlay first so the cable reads on the busy room art
    out += `<line x1="${hub.x}" y1="${hub.y}" x2="${p.x}" y2="${p.y}" stroke="#362619" stroke-opacity="0.3" stroke-width="${aimed ? 7 : 5.4}"/>`;
    out += `<line x1="${hub.x}" y1="${hub.y}" x2="${p.x}" y2="${p.y}" stroke="${c}" stroke-opacity="${aimed ? 0.95 : 0.5}" stroke-width="${aimed ? 3.4 : 2}"/>`;
    out += `<line class="flow" x1="${hub.x}" y1="${hub.y}" x2="${p.x}" y2="${p.y}" stroke="${c}" stroke-opacity="${aimed ? 1 : 0.85}" stroke-width="${aimed ? 4.4 : 3}"/>`;
  });
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
    const pos = nudgeOffHub(raw.px, raw.py);
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
  el("pcount").textContent = sections.length;
  el("devcell").textContent = sections.length;
  const w = m.wand || {};
  el("wanddot").classList.toggle("ok", !!w.connected);
  el("wandvar").textContent = w.connected ? w.variant : "—";

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
  el("roomqr").hidden = sections.length > 0 && el("roomqr").dataset.user !== "open";
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
  });
}

// ── engine state: gesture, action, lanes, transport ──────────────────────────
let bpmNow = 100;                    // last engine bpm; the header ± nudges from here

function bar(id, v, max) { el("f-" + id).style.width = Math.max(0, Math.min(1, v / max)) * 100 + "%"; }
function applyEngine(eng) {
  if (!eng) return;
  const bpm = Math.round(eng.bpm);
  bpmNow = bpm;
  el("bpmlbl").textContent = bpm; el("bpmcell").textContent = bpm;
  el("songname").textContent = eng.song || "—";
  if (eng.song !== curSong) { curSong = eng.song; dropIdle(); }   // new song landed
  el("barslbl").textContent = eng.bars ? eng.bars + " bars" : "";
  if (eng.transport) transport = eng.transport;
  engineAimed = eng.aimed || null;

  const g = eng.gesture;
  if (g) {
    el("v-energy").textContent = (g.energy ?? 0).toFixed(2); bar("energy", g.energy ?? 0, 1);
    el("v-size").textContent = (g.size ?? 0).toFixed(2); bar("size", g.size ?? 0, 1);
    el("v-vertical").textContent = (g.vertical ?? 0).toFixed(2); bar("vertical", Math.abs(g.vertical ?? 0), 1);
    el("v-rotation").textContent = (g.rotation ?? 0).toFixed(2); bar("rotation", g.rotation ?? 0, 1);
  }

  const label = eng.last_choice ? (NICE[eng.last_choice] || eng.last_choice) : "—";
  el("nowplaying").innerHTML = eng.playing ? `now playing <b>${label}</b>` : "press ▶ to start";
  if (eng.last_choice && eng.last_choice !== lastChoice) {
    lastChoice = eng.last_choice;
    const oct = g && g.vertical > 0.6 ? '<span class="oct">⬆ octave up</span>'
      : (g && g.vertical < -0.6 ? '<span class="oct">⬇ octave down</span>' : "");
    el("what").innerHTML = label + oct;
  }
  renderLanes(eng.tracks || []);
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
    const now = clock && clock.theta !== null ? clock.serverNow() : null;
    if (now === null) return;
    const pxPerMs = W / WINDOW_MS;
    const headX = W - FUTURE_MS * pxPerMs;
    ctx.strokeStyle = "rgba(54,38,25,.5)"; ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath(); ctx.moveTo(headX, 0); ctx.lineTo(headX, H); ctx.stroke();
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
conn.on(P.SCHED_CANCEL, (m) => { if (m.allnotesoff) synth.panic(); });

conn.onOpen((welcome) => {
  el("conndot").classList.add("ok");
  el("connlbl").textContent = "connected";
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
  // Laptops don't scan QRs — show the URL, click to copy.
  if (join) {
    const link = el("joinlink");
    link.textContent = join.replace(/^https?:\/\//, "");
    link.onclick = async () => {
      try { await navigator.clipboard.writeText(join); } catch { /* http non-localhost may deny */ }
      link.textContent = "✓ copied";
      link.classList.add("copied");
      setTimeout(() => { link.textContent = join.replace(/^https?:\/\//, ""); link.classList.remove("copied"); }, 1400);
    };
  }
});
conn.onClose(() => { el("conndot").classList.remove("ok"); el("connlbl").textContent = "reconnecting"; });

el("start").addEventListener("click", async () => {
  await ensureAudio();
  started = true;
  conn.send({ t: P.ADMIN_CMD, cmd: "start" });
});
el("stop").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "stop" }));
el("panic").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "allnotesoff" }));
function nudgeTempo(d) {
  bpmNow = Math.max(60, Math.min(180, bpmNow + d));
  el("bpmlbl").textContent = bpmNow; el("bpmcell").textContent = bpmNow;
  conn.send({ t: P.ADMIN_CMD, cmd: "tempo", args: { bpm: bpmNow } });
}
el("tdown").addEventListener("click", () => nudgeTempo(-4));
el("tup").addEventListener("click", () => nudgeTempo(4));

// camera hub — seamless: the camera wand is simply ON. The iframe loads at
// boot (its page auto-starts the webcam; the browser's permission prompt is
// the only gate). Insecure origins (LAN IP over http) get a quiet steer to
// localhost instead — phones keep joining via the QR either way.
if (window.isSecureContext) {
  el("camframe").src = `../cvwand/?s=${encodeURIComponent(session)}`;
  camStarted = true;
} else {
  const local = `http://localhost:${location.port || 80}${location.pathname}${location.search}`;
  el("camhint").innerHTML =
    `<div class="big">🔒 Camera needs localhost</div>
     <div class="sub">On the hosting laptop open
       <a href="${local}">localhost:${location.port || 80}</a>
       — music still works here, and phones keep using the QR.</div>`;
  el("camhint").hidden = false;
}
// Audio needs one real user gesture — take the first click/tap anywhere.
window.addEventListener("pointerdown", () => { ensureAudio(); }, { once: true });

// header Join button re-opens the QR card even with phones present
el("joinbtn").addEventListener("click", () => {
  const q = el("roomqr");
  const show = q.hidden;
  q.hidden = !show;
  q.dataset.user = show ? "open" : "";
});

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
