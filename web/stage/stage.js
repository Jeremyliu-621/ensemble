// Stage = the orchestra on the computer. It plays the audio (when no phones have
// joined as sections), shows a QR that turns a phone into the wand, and displays
// the roster. Start unlocks audio + begins the transport.

import { Conn } from "../shared/ws.js";
import { Clock } from "../shared/clock.js";
import { Synth } from "../shared/synth.js";
import * as P from "../shared/protocol.js";

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";
const el = (id) => document.getElementById(id);

let conn = null;
let clock = null;
let synth = null;
let started = false;
let readySections = 0;    // phones acting as instruments; 0 => laptop is the orchestra

function flashPulse() {
  el("pulse").style.background = "#46d17a";
  setTimeout(() => { el("pulse").style.background = "#1e2433"; }, 90);
}

function renderQR(text) {
  if (!window.qrcode) return;
  const qr = window.qrcode(0, "M");
  qr.addData(text);
  qr.make();
  el("qr").innerHTML = qr.createSvgTag({ cellSize: 6, margin: 2, scalable: true });
}

function renderRoster(m) {
  readySections = m.sections.filter((s) => s.connected && s.ready).length;
  // wand status
  const w = m.wand || {};
  el("wanddot").classList.toggle("ok", !!w.connected);
  el("wandstate").textContent = w.connected ? `wand connected (${w.variant})` : "no wand connected";
  // sections table
  if (m.sections.length === 0) {
    el("rows").innerHTML = `<tr><td colspan="5" class="muted">laptop is the orchestra (no phones joined as sections)</td></tr>`;
  } else {
    el("rows").innerHTML = m.sections.map((s) => `<tr>
      <td><span class="dot ${s.connected ? "ok" : ""}"></span></td>
      <td>${s.id}</td><td>${s.instrument}</td>
      <td>${s.ready ? "✓" : "—"}</td>
      <td>${s.theta == null ? "—" : s.theta.toFixed(1) + "ms"}</td></tr>`).join("");
  }
  const thetas = m.sections.filter((s) => s.connected && s.theta != null).map((s) => s.theta);
  el("spread").textContent = thetas.length >= 2 ? (Math.max(...thetas) - Math.min(...thetas)).toFixed(1) + " ms"
    : (readySections ? "(sync in progress)" : "n/a — laptop only");
}

conn = new Conn({ role: "stage", session });
clock = new Clock((obj) => conn.send(obj));
synth = new Synth(clock, () => flashPulse());

conn.on(P.CLOCK_PONG, (m) => clock.handlePong(m));
conn.on(P.ROSTER, renderRoster);
conn.on(P.SCHED_NOTES, (m) => {
  // The laptop is the orchestra only when no phones are playing sections; then
  // the conductor routes everything to SECTION_ALL, so we play those events.
  if (!started || readySections > 0) return;
  for (const e of m.events) if (e.section === P.SECTION_ALL) synth.schedule(e);
});
conn.on(P.SCHED_CANCEL, (m) => { if (m.allnotesoff) synth.panic(); });

conn.onOpen((welcome) => {
  el("status").textContent = `connected · session ${welcome.config.session}`;
  if (welcome.config.wand_url) renderQR(welcome.config.wand_url);
  if (welcome.config.cv_url) el("cvlink").href = welcome.config.cv_url;
  clock.checkEpoch(welcome.server_time);   // a server restart mustn't poison the fit
});
conn.onClose(() => { el("status").textContent = "reconnecting…"; });

// Start = unlock audio (user gesture) + begin transport.
el("start").addEventListener("click", async () => {
  el("start").style.display = "none";
  await synth.unlock();
  clock.attachAudio(synth.ctx);
  clock.start();
  started = true;
  conn.send({ t: P.ADMIN_CMD, cmd: "start" });
});

el("stop").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "stop" }));
el("restart").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "start" }));
el("panic").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "allnotesoff" }));

conn.connect();
