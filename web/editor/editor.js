// The Editor. A DAW-style surface around the live orchestra: the editable
// piano-roll (pianoroll.js) is the centrepiece; this module wires it to the
// engine — seeding it from the current song, pushing hand-edits back as
// `song.edit`, and driving a synced playhead from the shared clock. It also
// hosts the transport, the track mixer, per-phone instruments, accompaniment
// override, gesture readout and gesture recorder. It makes no sound itself;
// the console tab and the phones play the orchestra.

import { Conn } from "../shared/ws.js";
import { Clock } from "../shared/clock.js";
import * as P from "../shared/protocol.js";
import { PianoRoll } from "./pianoroll.js";

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";
const el = (id) => document.getElementById(id);

const KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const INSTRUMENTS = ["violin", "viola", "cello", "flute", "clarinet", "piano", "bass", "synth", "bell", "drums"];
const TRACK_COLORS = ["#d9534a", "#e8a13c", "#57a639", "#8e5bd4", "#4a76d8", "#2f9e9e", "#d858a8", "#8a6d4f"];
const NICE = {
  auto: "Auto", lower_imitation: "Lower imitation", contrary_motion: "Contrary motion",
  sustained: "Sustained chord", delayed: "Delayed echo", rhythmic_dense: "Rhythmic (busy)", rest: "Rest (silence)",
  generated: "AI-written line",
};

// ---- piano roll ----
const pr = new PianoRoll(el("pianoroll"));

// ---- ws + clock ----
// ephemeral: every editor tab is its own client — two tabs sharing one
// persisted id would evict each other from the hub in a reconnect storm.
const conn = new Conn({ role: "stage", session, key: "editor", ephemeral: true });
const clock = new Clock((o) => conn.send(o));
conn.on(P.CLOCK_PONG, (m) => clock.handlePong(m));

let songName = null;         // identity of the song we've seeded from (stable across edits)
let curBpm = 100;
let forced = "auto";
let recording = false;
let tempoDragging = false;
let transport = null;        // {playing, anchor, s16_ms, n_bars} for the playhead
let pushTimer = null;

// ---- playhead: computed every frame from the synced clock ----
pr.setPlayheadSource(() => {
  if (!transport || !transport.playing || !transport.s16_ms || clock.theta == null) return null;
  const total = Math.max(1, transport.n_bars * 16);
  const pos = (clock.serverNow() - transport.anchor) / transport.s16_ms;
  return pos < 0 ? 0 : pos % total;
});

// ---- pushing edits ----
pr.onChange = schedulePush;
function schedulePush() {
  el("editstate").textContent = "editing…";
  el("editstate").classList.add("busy");
  clearTimeout(pushTimer);
  pushTimer = setTimeout(pushEdit, 280);
}
function pushEdit() {
  const parts = pr.serialize().tracks;
  conn.send({ t: P.SONG_EDIT, song: { name: songName || "edited", bpm: curBpm, parts } });
  el("editstate").textContent = "synced ✓";
  el("editstate").classList.remove("busy");
}

// ---- seeding from the engine's song (only when the song identity changes, so
// our local edits are never clobbered by the roster echoing them back) ----
function seedFromEngine(eng) {
  const tracks = (eng.tracks || []).map((t, i) => ({
    name: t.name, instrument: t.instrument, isDrum: t.is_drum, isMelody: t.is_melody,
    color: TRACK_COLORS[i % TRACK_COLORS.length],
    notes: (t.roll || []).map(([bar, on, dur, pitch, vel]) => ({ start: bar * 16 + on, dur, pitch, vel: vel ?? 0.8 })),
  }));
  pr.load({ tracks });
  el("songname").textContent = eng.song || "—";
  el("keyinfo").textContent = `${KEYS[eng.key_root] ?? "?"} major · ${eng.bars} bars`;
  el("songmeta").textContent = `${(eng.tracks || []).length} tracks`;
  renderMixer();
}

// ---- track mixer ----
function instOptions(sel) {
  return INSTRUMENTS.map((i) => `<option value="${i}" ${i === sel ? "selected" : ""}>${i}</option>`).join("");
}
function renderMixer() {
  el("tracklist").innerHTML = pr.tracks.map((t) => `
    <div class="track ${t.id === pr.activeId ? "active" : ""}" data-id="${t.id}">
      <span class="sw" style="background:${t.color}"></span>
      <div class="tn"><span class="nm">${t.name}${t.isMelody ? " ★" : ""}</span>
        <select data-inst>${instOptions(t.instrument)}</select></div>
      <div class="ctl">
        <button data-act="mute" class="${t.muted ? "on" : ""}" title="mute">M</button>
        <button data-act="solo" class="${t.solo ? "on" : ""}" title="solo">S</button>
        <button data-act="star" class="star ${t.isMelody ? "on" : ""}" title="lead / melody">★</button>
        <button data-act="del" title="delete track">✕</button>
      </div>
    </div>`).join("") || `<div class="muted" style="font-size:12px">no tracks — add one below</div>`;
}
el("tracklist").addEventListener("click", (e) => {
  const row = e.target.closest(".track"); if (!row) return;
  const id = row.dataset.id, btn = e.target.closest("button");
  if (btn) {
    e.stopPropagation();
    const t = pr.tracks.find((x) => x.id === id); if (!t) return;
    const act = btn.dataset.act;
    if (act === "mute") t.muted = !t.muted;
    else if (act === "solo") t.solo = !t.solo;
    else if (act === "star") { pr.tracks.forEach((x) => (x.isMelody = false)); t.isMelody = true; }
    else if (act === "del") pr.removeTrack(id);
    pr.redraw(); renderMixer(); pushEdit();
  } else { pr.setActive(id); renderMixer(); }
});
el("tracklist").addEventListener("change", (e) => {
  const sel = e.target.closest("select[data-inst]"); if (!sel) return;
  const id = e.target.closest(".track").dataset.id;
  const t = pr.tracks.find((x) => x.id === id); if (!t) return;
  t.instrument = sel.value; t.name = sel.value; t.isDrum = sel.value === "drums";
  pr.redraw(); renderMixer(); pushEdit();
});
el("addtrack").addEventListener("click", () => {
  const inst = el("addinstr").value;
  pr.addTrack({ name: inst, instrument: inst, isDrum: inst === "drums", isMelody: pr.tracks.length === 0,
    color: TRACK_COLORS[pr.tracks.length % TRACK_COLORS.length] });
  renderMixer(); pushEdit();
});

// ---- tools / snap / zoom ----
el("tool-pencil").addEventListener("click", () => setTool("pencil"));
el("tool-select").addEventListener("click", () => setTool("select"));
function setTool(t) {
  pr.setTool(t);
  el("tool-pencil").classList.toggle("active", t === "pencil");
  el("tool-select").classList.toggle("active", t === "select");
}
el("snapseg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-snap]"); if (!b) return;
  pr.setSnap(+b.dataset.snap);
  [...el("snapseg").children].forEach((x) => x.classList.toggle("active", x === b));
});
el("zoomin").addEventListener("click", () => pr.zoom(1.25, 1.12));
el("zoomout").addEventListener("click", () => pr.zoom(0.8, 0.9));
el("follow").addEventListener("click", () => {
  const on = !el("follow").classList.contains("active");
  el("follow").classList.toggle("active", on); pr.setFollow(on);
});
// keep the tool buttons in sync when changed via keyboard inside the roll
el("pianoroll").addEventListener("keydown", (e) => {
  if (e.key === "1") setTool("pencil"); if (e.key === "2") setTool("select");
});

// ---- transport ----
el("start").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "start" }));
el("stop").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "stop" }));
el("panic").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "allnotesoff" }));
el("tempo").addEventListener("input", (e) => {
  tempoDragging = true; curBpm = parseInt(e.target.value, 10);
  el("tempoval").textContent = curBpm + " BPM";
  conn.send({ t: P.ADMIN_CMD, cmd: "tempo", args: { bpm: curBpm } });
});
el("tempo").addEventListener("change", () => { tempoDragging = false; });
// Space toggles play (unless typing in a field)
window.addEventListener("keydown", (e) => {
  if (e.code === "Space" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName || "")) {
    e.preventDefault();
    conn.send({ t: P.ADMIN_CMD, cmd: transport && transport.playing ? "stop" : "start" });
  }
});

// ---- accompaniment override ----
function renderCandidates(list) {
  if (el("candidates").children.length) return;
  for (const c of ["auto", ...list]) {
    const btn = document.createElement("button");
    btn.className = "chip" + (c === "auto" ? " active" : "");
    btn.textContent = NICE[c] || c; btn.dataset.cand = c;
    btn.addEventListener("click", () => {
      forced = c;
      conn.send({ t: P.ADMIN_CMD, cmd: "force", args: { candidate: c } });
      [...el("candidates").children].forEach((x) => x.classList.toggle("active", x.dataset.cand === c));
    });
    el("candidates").appendChild(btn);
  }
}

// ---- phone instruments + volume ----
function renderSections(sections) {
  el("rows").innerHTML = sections.length ? sections.map((s) => `<tr data-sid="${s.id}">
    <td><span class="dot ${s.connected ? "ok" : ""}"></span></td>
    <td>${s.id}</td>
    <td><select data-assign>${instOptions(s.instrument)}</select></td>
    <td><input class="vol" type="range" min="0" max="1" step="0.05" value="${s.volume ?? 1}"></td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">no phones — scan the stage QR to add instruments</td></tr>`;
}
el("rows").addEventListener("change", (e) => {
  const tr = e.target.closest("tr[data-sid]"); if (!tr) return;
  const sid = tr.dataset.sid;
  if (e.target.matches("select[data-assign]")) conn.send({ t: P.STAGE_ASSIGN, section_id: sid, instrument: e.target.value });
});
el("rows").addEventListener("input", (e) => {
  const tr = e.target.closest("tr[data-sid]"); if (!tr) return;
  if (e.target.matches("input.vol")) conn.send({ t: P.ADMIN_CMD, cmd: "volume", args: { section_id: tr.dataset.sid, volume: parseFloat(e.target.value) } });
});

// ---- gesture recorder ----
el("recbtn").addEventListener("click", () => {
  conn.send({ t: P.ADMIN_CMD, cmd: "record", args: { action: recording ? "stop" : "start", label: el("reclabel").value } });
});

// ---- MIDI drop (replaces the song) ----
function abToBase64(ab) {
  const bytes = new Uint8Array(ab); let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
async function uploadMidi(file) {
  if (!file) return;
  el("drop").textContent = `reading ${file.name}…`;
  const ab = await file.arrayBuffer();
  conn.send({ t: P.SONG_LOAD, name: file.name, data: abToBase64(ab) });
  el("drop").textContent = "⬇ .mid";
}
const drop = el("drop");
drop.addEventListener("click", () => el("midifile").click());
el("midifile").addEventListener("change", (e) => uploadMidi(e.target.files[0]));
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => uploadMidi(e.dataTransfer.files[0]));
conn.on(P.ERR, (m) => {
  if (m.code === "bad_midi") el("drop").textContent = `⚠ ${m.msg}`;
  if (m.code === "bad_edit") { el("editstate").textContent = "⚠ edit rejected"; el("editstate").classList.add("busy"); }
});

// ---- roster + engine state ----
function applyEngineCommon(eng) {
  if (eng.candidates) renderCandidates(eng.candidates);
  const brain = eng.decision_source ? ` · ${eng.decision_source} brain` : "";
  const rows = eng.training_rows ? ` · ${eng.training_rows} rows logged` : "";
  el("nowplaying").textContent =
    (eng.last_choice ? (NICE[eng.last_choice] || eng.last_choice) : "—") + brain + rows;
  const g = eng.gesture;
  for (const k of ["energy", "size", "vertical", "rotation"]) el("g_" + k).textContent = g ? g[k].toFixed(2) : "—";
  if (eng.transport) transport = eng.transport;
}
conn.on(P.ROSTER, (m) => {
  const eng = m.engine || {};
  applyEngineCommon(eng);
  if (eng.tracks && eng.song !== songName) { songName = eng.song; seedFromEngine(eng); }
  if (eng.bpm && !tempoDragging) { curBpm = eng.bpm; el("tempo").value = eng.bpm; el("tempoval").textContent = eng.bpm + " BPM"; }

  const w = m.wand || {};
  el("wanddot").classList.toggle("ok", !!w.connected);
  el("wandstate").textContent = w.connected ? w.variant : "none";

  const rec = m.record || {};
  recording = !!rec.recording;
  el("reccount").textContent = rec.count || 0;
  el("recstate").textContent = rec.recording ? `recording "${rec.label}"` : "idle";
  el("recbtn").classList.toggle("active", recording);
  el("recbtn").textContent = recording ? "■ Stop" : "● Record";

  renderSections(m.sections || []);
});

// live (per-change) engine updates: nowplaying, gesture, and transport for the playhead
conn.on(P.ENGINE_STATE, (m) => applyEngineCommon(m));

conn.onOpen((wc) => { el("status").textContent = `session ${wc.config.session}`; });
conn.onClose(() => { el("status").textContent = "reconnecting…"; });

conn.connect();
clock.start();
