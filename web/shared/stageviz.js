// Stage overlay: a live note stream (see the MIDI playing + changing in real time)
// and a change indicator (what your last gesture did). Self-contained — it injects
// its own DOM + styles and opens its own read-only ws connection, so any stage page
// gets it by adding one <script> tag. No audio; visuals only.

import { Conn } from "./ws.js";
import { Clock } from "./clock.js";
import * as P from "./protocol.js";

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";

const SEMI = { C: 0, "C#": 1, D: 2, "D#": 3, E: 4, F: 5, "F#": 6, G: 7, "G#": 8, A: 9, "A#": 10, B: 11 };
const noteToMidi = (n) => { const m = /^([A-G]#?)(-?\d+)$/.exec(n); return m ? (parseInt(m[2], 10) + 1) * 12 + SEMI[m[1]] : 60; };
const NICE = { lower_imitation: "Lower imitation", contrary_motion: "Contrary motion", sustained: "Sustained chord",
  delayed: "Delayed echo", rhythmic_dense: "Rhythmic — busy", rest: "Rest — silence" };

// section id -> a stable colour; drums are grey.
const PALETTE = ["#e7c583", "#7fd1ff", "#6fcf7f", "#e58a6a", "#c79bff", "#ffd76a", "#79d6c0"];
function colorFor(section, drum) {
  if (drum) return "#8a8378";
  if (section === "all" || !section) return "#e7c583";
  let h = 0; for (const c of section) h = (h * 31 + c.charCodeAt(0)) % PALETTE.length;
  return PALETTE[h];
}

// --- inject DOM + CSS ---
const css = `
  #sv-stream { position: fixed; left: 0; right: 0; bottom: 0; height: 94px; z-index: 60; pointer-events: none;
    background: linear-gradient(180deg, rgba(12,6,8,0) 0%, rgba(12,6,8,.72) 45%, rgba(12,6,8,.88) 100%); }
  #sv-canvas { width: 100%; height: 100%; display: block; }
  #sv-tag { position: fixed; left: 12px; bottom: 74px; z-index: 61; pointer-events: none;
    font: 700 10px ui-monospace, monospace; color: #b9903f; letter-spacing: .1em; text-transform: uppercase; }
  #sv-now { position: fixed; top: 12%; left: 50%; transform: translateX(-50%); z-index: 80; pointer-events: none;
    font: 600 13px "Segoe UI", system-ui; color: #b9903f; white-space: nowrap; text-shadow: 0 1px 3px #000; }
  #sv-now b { color: #ffe3a3; }
  #sv-banner { position: fixed; top: calc(12% + 22px); left: 50%; transform: translateX(-50%) translateY(6px); z-index: 81;
    pointer-events: none; opacity: 0; transition: opacity .25s, transform .25s; text-align: center;
    font: 800 clamp(20px, 4vw, 36px)/1 "Cinzel", "Bodoni MT", Georgia, serif; letter-spacing: .04em;
    color: #ffe3a3; text-shadow: 0 0 14px rgba(255,178,76,.7), 0 2px 0 #6a3d10; }
  #sv-banner.show { opacity: 1; transform: translateX(-50%) translateY(0); }
  #sv-banner .oct { display: block; font-size: .5em; color: #7fd1ff; letter-spacing: .1em; }
`;
const style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);
const stream = document.createElement("div"); stream.id = "sv-stream";
const canvas = document.createElement("canvas"); canvas.id = "sv-canvas"; stream.appendChild(canvas);
const tag = document.createElement("div"); tag.id = "sv-tag"; tag.textContent = "♪ live notes";
const nowEl = document.createElement("div"); nowEl.id = "sv-now";
const banner = document.createElement("div"); banner.id = "sv-banner";
document.body.append(stream, tag, nowEl, banner);

// --- ws + clock (read-only) ---
const conn = new Conn({ role: "stage", session, key: "stageviz" });
const clock = new Clock((o) => conn.send(o));
conn.on(P.CLOCK_PONG, (m) => clock.handlePong(m));

const notes = [];               // {at, dur, pitch, color}
const seen = new Set();
conn.on(P.SCHED_NOTES, (m) => {
  for (const e of m.events) {
    if (seen.has(e.id)) continue;
    seen.add(e.id);
    notes.push({ at: e.at, dur: e.dur || 200, pitch: noteToMidi(e.note), color: colorFor(e.section, e.art === "drum") });
  }
  if (seen.size > 4000) seen.clear();
});

let lastChoice = null;
function applyEngine(eng) {
  if (!eng) return;
  const label = eng.last_choice ? (NICE[eng.last_choice] || eng.last_choice) : "—";
  nowEl.innerHTML = `now playing <b>${label}</b>${eng.song ? ` · ${eng.song}` : ""}`;
  if (eng.last_choice && eng.last_choice !== lastChoice) {
    lastChoice = eng.last_choice;
    const g = eng.gesture;
    const oct = g && g.vertical > 0.6 ? "⬆ octave up" : (g && g.vertical < -0.6 ? "⬇ octave down" : "");
    banner.innerHTML = `${label}${oct ? `<span class="oct">${oct}</span>` : ""}`;
    banner.classList.add("show");
    clearTimeout(banner._t);
    banner._t = setTimeout(() => banner.classList.remove("show"), 1600);
  }
}
conn.on(P.ROSTER, (m) => applyEngine(m.engine));
conn.on(P.ENGINE_STATE, (m) => applyEngine(m));

conn.connect();
clock.start();

// --- render loop: a scrolling piano-roll with a playhead ---
const WINDOW_MS = 4000, FUTURE_MS = 1500, LO = 36, HI = 96;
function draw() {
  const dpr = window.devicePixelRatio || 1;
  const W = (canvas.width = stream.clientWidth * dpr);
  const H = (canvas.height = stream.clientHeight * dpr);
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, W, H);
  const now = clock.serverNow();
  const pxPerMs = W / WINDOW_MS;
  const headX = W - FUTURE_MS * pxPerMs;

  // playhead line
  ctx.strokeStyle = "rgba(255,227,163,.5)"; ctx.lineWidth = 1.5 * dpr;
  ctx.beginPath(); ctx.moveTo(headX, 0); ctx.lineTo(headX, H); ctx.stroke();

  for (let i = notes.length - 1; i >= 0; i--) {
    const n = notes[i];
    const x = W - (now + FUTURE_MS - n.at) * pxPerMs;
    const w = Math.max(3 * dpr, n.dur * pxPerMs);
    if (x + w < 0) { notes.splice(i, 1); continue; }        // scrolled off the left
    if (x > W) continue;                                     // not on screen yet
    const y = H - ((Math.max(LO, Math.min(HI, n.pitch)) - LO) / (HI - LO)) * (H - 8 * dpr) - 4 * dpr;
    const played = n.at <= now;
    ctx.globalAlpha = played ? 0.95 : 0.45;                  // dim = still upcoming
    ctx.fillStyle = n.color;
    ctx.fillRect(x, y - 3 * dpr, w, 6 * dpr);
  }
  ctx.globalAlpha = 1;
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
