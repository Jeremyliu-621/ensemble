// Section page: join -> unlock audio -> sync clock -> play scheduled notes in
// sample-accurate agreement with every other device.

import { Conn } from "../shared/ws.js";
import { Clock } from "../shared/clock.js";
import { Synth } from "../shared/synth.js";
import * as P from "../shared/protocol.js";

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";

const el = (id) => document.getElementById(id);
const joinScreen = el("join");
const stageScreen = el("stage");
const pulse = el("pulse");

let conn = null;
let clock = null;
let synth = null;
let myId = null;
let noteCount = 0;

function onPlay(ev, peak) {
  pulse.style.background = peak >= 0.9 ? "#e7c583" : "#a8712a";
  pulse.textContent = ev.note;
  setTimeout(() => { pulse.style.background = "transparent"; pulse.textContent = "—"; }, 90);
  noteCount++;
  el("clicks").textContent = noteCount;
  el("last").textContent = ev.note;
}

async function requestWakeLock() {
  try { await navigator.wakeLock.request("screen"); } catch { /* NoSleep fallback in P2 */ }
}

function updateHud() {
  if (!clock) return;
  el("theta").textContent = clock.theta === null ? "—" : clock.theta.toFixed(1) + "ms";
  el("rtt").textContent = clock.rtt === null ? "—" : clock.rtt.toFixed(1) + "ms";
}
setInterval(updateHud, 500);

setInterval(() => {
  if (conn && clock && clock.theta !== null) {
    conn.send({ t: P.CLOCK_REPORT, theta: clock.theta, rtt: clock.rtt });
  }
}, 2000);

el("trim").addEventListener("input", (e) => {
  const ms = parseInt(e.target.value, 10);
  el("trimval").textContent = ms + "ms";
  if (clock) clock.trimSec = ms / 1000;
  localStorage.setItem("wm.trim", String(ms));
});

joinScreen.addEventListener("click", async () => {
  joinScreen.style.display = "none";
  stageScreen.style.display = "flex";

  conn = new Conn({ role: "section", session });
  clock = new Clock((obj) => conn.send(obj));
  synth = new Synth(clock, onPlay);

  await synth.unlock();
  clock.attachAudio(synth.ctx);
  requestWakeLock();

  const savedTrim = parseInt(localStorage.getItem("wm.trim") || "0", 10);
  el("trim").value = savedTrim;
  el("trimval").textContent = savedTrim + "ms";
  clock.trimSec = savedTrim / 1000;
  clock.onResync = (d) => console.log(`[clock] resync ${d.toFixed(1)}ms`);

  conn.on(P.CLOCK_PONG, (m) => clock.handlePong(m));
  conn.on(P.SCHED_NOTES, (m) => {
    for (const e of m.events) {
      if (e.section === P.SECTION_ALL || e.section === myId) synth.schedule(e);
    }
  });
  conn.on(P.SCHED_CANCEL, (m) => { if (m.allnotesoff) synth.panic(); });
  conn.on(P.SECTION_CONFIG, (m) => {         // live instrument reassignment from the editor
    synth.setInstrument(m.instrument);
    el("sid").textContent = `${myId} · ${m.instrument}`;
  });

  conn.onOpen((welcome) => {
    myId = welcome.config.section_id;
    synth.setInstrument(welcome.config.instrument);
    el("sid").textContent = `${myId} · ${welcome.config.instrument}`;
    el("dot").classList.add("ok");
    clock.start();
    conn.send({ t: P.SECTION_READY });
  });
  conn.onClose(() => { el("dot").classList.remove("ok"); el("sid").textContent = "reconnecting…"; });

  conn.connect();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && synth && synth.ctx) synth.ctx.resume();
});
