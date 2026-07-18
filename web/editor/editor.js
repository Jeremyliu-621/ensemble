// Control room. The live stage runs in the centre iframe (it plays the audio and
// shows the orchestra); this page is the control surface around it — transport,
// tempo, accompaniment override, MIDI drop + piano-roll, and instrument
// assignment. It holds its own ws connection (role admin — NOT stage, so it
// never shares a client_id with the stage tab and evicts it from the hub) for
// roster/engine status and to send commands; it makes no sound of its own.

import { Conn } from "../shared/ws.js";
import * as P from "../shared/protocol.js";

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";
const el = (id) => document.getElementById(id);

const INSTRUMENTS = ["violin", "viola", "cello", "flute", "clarinet", "piano", "bass", "synth", "bell"];
const NICE = {
  auto: "Auto", lower_imitation: "Lower imitation", contrary_motion: "Contrary motion",
  sustained: "Sustained chord", delayed: "Delayed echo", rhythmic_dense: "Rhythmic (busy)", rest: "Rest (silence)",
};

let forced = "auto";

// Embed the live stage.
el("stageframe").src = `../stagepix/?s=${encodeURIComponent(session)}`;

const conn = new Conn({ role: "admin", session });

// --- candidate override buttons (built once) ---
function renderCandidates(list) {
  if (el("candidates").children.length) return;
  for (const c of ["auto", ...list]) {
    const btn = document.createElement("button");
    btn.textContent = NICE[c] || c;
    btn.dataset.cand = c;
    if (c === "auto") btn.className = "active";
    btn.addEventListener("click", () => {
      forced = c;
      conn.send({ t: P.ADMIN_CMD, cmd: "force", args: { candidate: c } });
      [...el("candidates").children].forEach((x) => x.classList.toggle("active", x.dataset.cand === c));
    });
    el("candidates").appendChild(btn);
  }
}

// --- song info + piano-roll ---
const KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
function renderSong(eng) {
  if (!eng || !eng.song) return;
  el("songname").textContent = eng.song;
  el("songmeta").textContent = `${KEYS[eng.key_root] || "?"} major · ${eng.bpm} BPM · ${eng.bars} bars`;
  const tracks = eng.tracks || [];
  if (tracks.length) {
    el("tracks").innerHTML = tracks.map((t) => `<tr>
      <td>${t.is_melody ? '<span class="tag">melody</span>' : (t.is_drum ? "🥁" : "")}</td>
      <td>${t.name}</td><td>${t.instrument}</td><td>${t.note_count}</td></tr>`).join("");
    drawRoll(tracks, eng.bars);
  }
}

const ROLL_COLORS = ["#e7c583", "#7fd1ff", "#6fcf7f", "#e5686a", "#c77fff", "#e5a23d"];
function drawRoll(tracks, bars) {
  const cv = el("roll");
  const dpr = window.devicePixelRatio || 1;
  const W = (cv.width = cv.clientWidth * dpr);
  const H = (cv.height = 130 * dpr);
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, W, H);
  const notes = [];
  tracks.forEach((t, ti) => (t.roll || []).forEach(([b, on, dur, p]) => notes.push({ b, on, dur, p, ti, drum: t.is_drum })));
  if (!notes.length) return;
  const ps = notes.map((n) => n.p);
  const pmin = Math.min(...ps), pmax = Math.max(...ps);
  const totalBars = bars || (Math.max(...notes.map((n) => n.b)) + 1);
  const slots = totalBars * 16;
  const rowH = H / (pmax - pmin + 2);
  ctx.strokeStyle = "rgba(231,197,131,0.08)";
  for (let b = 0; b <= totalBars; b++) { const x = (b * 16 / slots) * W; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
  notes.forEach((n) => {
    const x = ((n.b * 16 + n.on) / slots) * W;
    const w = Math.max(2 * dpr, (n.dur / slots) * W - dpr);
    const y = H - (n.p - pmin + 1) * rowH;
    ctx.fillStyle = n.drum ? "#6a5a55" : ROLL_COLORS[n.ti % ROLL_COLORS.length];
    ctx.fillRect(x, y, w, Math.max(2 * dpr, rowH - dpr));
  });
}

// --- MIDI drop ---
function abToBase64(ab) {
  const bytes = new Uint8Array(ab);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
async function uploadMidi(file) {
  if (!file) return;
  el("drop").textContent = `reading ${file.name}…`;
  const ab = await file.arrayBuffer();
  conn.send({ t: P.SONG_LOAD, name: file.name, data: abToBase64(ab) });
  el("drop").textContent = "⬇ Drop a .mid file here, or click to choose";
}
const drop = el("drop");
drop.addEventListener("click", () => el("midifile").click());
el("midifile").addEventListener("change", (e) => uploadMidi(e.target.files[0]));
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => uploadMidi(e.dataTransfer.files[0]));
conn.on(P.ERR, (m) => { if (m.code === "bad_midi") el("drop").textContent = `⚠ ${m.msg} — try another file`; });

// --- instruments ---
function instrumentSelect(sid, current) {
  const opts = INSTRUMENTS.map((i) => `<option value="${i}" ${i === current ? "selected" : ""}>${i}</option>`).join("");
  return `<select data-sid="${sid}">${opts}</select>`;
}
function bindSelects() {
  el("rows").querySelectorAll("select").forEach((sel) =>
    sel.addEventListener("change", () => conn.send({ t: P.STAGE_ASSIGN, section_id: sel.dataset.sid, instrument: sel.value })));
}

// --- roster ---
let tempoDragging = false;
conn.on(P.ROSTER, (m) => {
  const eng = m.engine || {};
  if (eng.candidates) renderCandidates(eng.candidates);
  renderSong(eng);
  el("nowplaying").textContent = eng.last_choice ? (NICE[eng.last_choice] || eng.last_choice) : "—";
  if (eng.bpm && !tempoDragging) { el("tempo").value = eng.bpm; el("tempoval").textContent = eng.bpm + " BPM"; }

  const w = m.wand || {};
  el("wanddot").classList.toggle("ok", !!w.connected);
  el("wandstate").textContent = w.connected ? w.variant : "none";
  const g = eng.gesture;
  for (const k of ["energy", "size", "vertical", "rotation"]) el("g_" + k).textContent = g ? g[k].toFixed(2) : "—";

  if (m.sections.length === 0) {
    el("rows").innerHTML = `<tr><td colspan="4" class="muted">no phones yet — scan the stage QR to add instruments</td></tr>`;
  } else {
    el("rows").innerHTML = m.sections.map((s) => `<tr>
      <td><span class="dot ${s.connected ? "ok" : ""}"></span></td>
      <td>${s.id}</td><td>${instrumentSelect(s.id, s.instrument)}</td>
      <td>${s.theta == null ? "—" : s.theta.toFixed(1) + "ms"}</td></tr>`).join("");
    bindSelects();
  }
});

conn.onOpen((wc) => { el("status").textContent = `connected · session ${wc.config.session}`; });
conn.onClose(() => { el("status").textContent = "reconnecting…"; });

// --- transport (commands only; the stage iframe makes the sound) ---
el("start").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "start" }));
el("stop").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "stop" }));
el("panic").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "allnotesoff" }));
el("tempo").addEventListener("input", (e) => {
  tempoDragging = true;
  el("tempoval").textContent = e.target.value + " BPM";
  conn.send({ t: P.ADMIN_CMD, cmd: "tempo", args: { bpm: parseInt(e.target.value, 10) } });
});
el("tempo").addEventListener("change", () => { tempoDragging = false; });

conn.connect();
