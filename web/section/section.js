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
let recvCount = 0;
let left = false;          // true after the Leave button — stops reconnect + overlays

// big pixel icon + name up top — this is what the audience sees on the phone
const ICONS = ["drums", "piano", "bass", "violin", "cello", "viola", "flute",
  "clarinet", "trumpet", "harp", "bell", "synth"];
function showInstrument(inst) {
  el("instico").src = `../assets/pixel/icon_${ICONS.includes(inst) ? inst : "synth"}.png`;
  el("instnm").textContent = inst || "—";
}

function onPlay(ev, peak) {
  pulse.style.background = peak >= 0.9 ? "#a5d8a0" : "#dccfb0";
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
  // audio-context state: "running" = good; anything else = silent (needs a tap)
  if (synth && synth.ctx) {
    const st = synth.ctx.state;
    el("audio").textContent = st;
    el("audio").style.color = st === "running" ? "#3fae4a" : "#d9534a";
    if (st !== "running") {
      synth.ctx.resume().catch(() => {});               // quiet self-heal first…
      if (!left) el("unmute").style.display = "flex";   // …and an unmissable prompt
    } else {
      el("unmute").style.display = "none";
    }
  }
}
setInterval(updateHud, 500);

// The unmute tap runs inside a user gesture, so resume() is allowed. If the
// context is beyond saving (iOS sometimes bricks it after a long nap), rebuild
// it from scratch — new AudioContext, re-anchored clock, same instrument.
el("unmute").addEventListener("click", async () => {
  if (!synth) return;
  try { await synth.ctx.resume(); } catch { /* fall through to rebuild */ }
  if (synth.ctx.state !== "running") {
    const old = synth.ctx;
    try { await synth.unlock(); clock.attachAudio(synth.ctx); } catch {}
    try { old.close(); } catch {}
  }
  if (synth.ctx.state === "running") el("unmute").style.display = "none";
});

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
      if (e.section === P.SECTION_ALL || e.section === myId) {
        recvCount++;                      // received (network ok) — vs "played" (audio ok)
        el("recv").textContent = recvCount;
        synth.schedule(e);
      }
    }
  });
  conn.on(P.SCHED_CANCEL, (m) => {   // global panic, or a targeted cut of MY notes (solo)
    if (m.allnotesoff || m.section === myId) synth.panic();
  });
  conn.on(P.FX_TENSION, (m) => synth.setTension(m.value));
  conn.on(P.FX_EXPR, (m) => {   // targeted phones warp; everyone else resets
    if (m.section === P.SECTION_ALL || m.section === myId) synth.setExpression(m.semis, m.gain);
    else synth.setExpression(0, 1);
  });
  conn.on(P.SECTION_CONFIG, (m) => {         // live instrument reassignment from the editor
    synth.setInstrument(m.instrument);
    showInstrument(m.instrument);
    el("sid").textContent = `${myId} · ${m.instrument}`;
  });

  conn.onOpen((welcome) => {
    myId = welcome.config.section_id;
    synth.setInstrument(welcome.config.instrument);
    showInstrument(welcome.config.instrument);
    el("sid").textContent = `${myId} · ${welcome.config.instrument}`;
    el("dot").classList.add("ok");
    clock.checkEpoch(welcome.server_time);
    clock.start();
    conn.send({ t: P.SECTION_READY });
  });
  conn.onClose(() => { el("dot").classList.remove("ok"); el("sid").textContent = "reconnecting…"; });

  conn.connect();
});

// Leave: tell the server explicitly (slot frees at once, no grace period),
// close the socket, and offer a clean rejoin.
el("leave").addEventListener("click", () => {
  left = true;
  if (conn) { conn.send({ t: P.SECTION_LEAVE }); conn.close(); }
  try { synth && synth.panic(); } catch {}
  el("unmute").style.display = "none";
  el("leftscreen").style.display = "flex";
});
el("leftscreen").addEventListener("click", () => location.reload());

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (synth && synth.ctx) synth.ctx.resume().catch(() => {});
  requestWakeLock();   // the OS silently releases wake locks on hide — take it back
});
// Tapping the performing screen re-unlocks audio if the context got suspended
// (iOS/Android suspend it when backgrounded; resume must be in a user gesture).
stageScreen.addEventListener("pointerdown", () => {
  if (synth && synth.ctx && synth.ctx.state === "suspended") synth.ctx.resume().catch(() => {});
});
