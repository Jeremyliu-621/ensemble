// The demo, minus the wand, on one page: transport, one-click songs, canned
// gestures (byte-identical to what the hardware wand sends), hum-to-melody,
// and a big readout of every AI decision as it lands.

import { Conn } from "../shared/ws.js";
import { Clock } from "../shared/clock.js";
import { Synth } from "../shared/synth.js";
import * as P from "../shared/protocol.js";

const el = (id) => document.getElementById(id);
const conn = new Conn({ role: "stage", session: "lol1" });
const clock = new Clock((o) => conn.send(o));
const synth = new Synth(clock, null);
let seq = 0;

const COLORS = {
  verbatim: "#888", hush: "#7fd1ff", harmonize: "#e7c583", arpeggio: "#c77fff",
  passing: "#6fcf7f", swelling: "#e5a23d",
  rhythmic_dense: "#e5686a", contrary_motion: "#7fd1ff", sustained: "#6fcf7f",
  delayed: "#e5a23d", lower_imitation: "#c77fff", rest: "#777", generated: "#e7c583",
};

function log(msg) {
  const d = document.createElement("div");
  d.textContent = `${new Date().toLocaleTimeString()}  ${msg}`;
  el("log").prepend(d);
}

// --- 1: transport -----------------------------------------------------------
el("start").addEventListener("click", async () => {
  await synth.unlock();
  clock.attachAudio(synth.ctx);
  clock.start();
  conn.send({ t: P.ADMIN_CMD, cmd: "start" });
  log("started — gesture, load a song, or hum");
});
el("stop").addEventListener("click", () => conn.send({ t: P.ADMIN_CMD, cmd: "stop" }));

// --- 2: songs ---------------------------------------------------------------
el("song-zelda").addEventListener("click", () => {
  conn.send({ t: P.SONG_FILE, name: "zelda-fairy.mid" });
  log("loading Great Fairy Fountain…");
});
el("song-zora").addEventListener("click", () => {
  conn.send({ t: P.SONG_FILE, name: "zora-domain.mid" });
  log("loading Zora's Domain…");
});
el("song-canon").addEventListener("click", () => {
  conn.send({ t: P.SONG_FILE, name: "canon.mid" });
  log("loading Canon in D…");
});
el("song-yt").addEventListener("click", () => {
  conn.send({ t: P.SONG_FILE, name: "yt-song.mid" });
  log("loading your YouTube song…");
});

// --- 3: canned gestures -----------------------------------------------------
function imu(accel, gyro, ay, durMs, n) {
  const out = [], total = accel + 9.8;
  for (let i = 0; i < n; i++) {
    out.push([Math.round(i * durMs / (n - 1)), total * (i % 2 ? 1 : -1), ay, 0, gyro, 0, 0]);
  }
  return out;
}
// The final ear-ranked vocabulary, one button per device. Frame recipes are
// solved against server/gestures/features.py so each lands in its style band:
// energy=accel/12 · size=accel*dur/6 · rotation=gyro/200 · vertical=ay/9.8.
const GESTURES = {
  "🙌 PUSH → chords (harmonize)":  () => imu(9, 0, 0, 700, 30),     // e=.85: firm push
  "🍃 GENTLE → hush":              () => imu(0.3, 0, 0, 600, 12),   // target≈0 → thin out
  "🌀 TWIST → arpeggio":           () => imu(2, 160, 0, 700, 30),   // rotation .8 lifts it
  "🪶 LIGHT TOUCH → passing tones": () => imu(7, 0, 0, 700, 30),    // e=.68: gentlest push
  "⚡ SHARP FLICK → sting":        () => imu(12, 0, 0, 300, 18),    // accent, instant
  "🌅 SWELL → 4-bar build":        () => imu(6, 0, 8.5, 1200, 50),  // slow lift arms the arc
};
for (const [name, gen] of Object.entries(GESTURES)) {
  const b = document.createElement("button");
  b.textContent = name;
  b.addEventListener("click", () => {
    const frames = gen();
    const tw = Math.round(performance.now());
    conn.send({ t: P.WAND_GRAB, state: "start", tw });
    conn.send({ t: P.WAND_IMU, seq: seq++, frames });
    conn.send({ t: P.WAND_GRAB, state: "end", tw: tw + frames[frames.length - 1][0] });
    log(`sent ${name}`);
  });
  el("gestures").appendChild(b);
}

// --- 4: hum-to-melody -------------------------------------------------------
function pitchOf(buf, sr) {
  let rms = 0;
  for (const v of buf) rms += v * v;
  rms = Math.sqrt(rms / buf.length);
  if (rms < 0.01) return [null, rms];
  const minLag = Math.floor(sr / 800), maxLag = Math.min(buf.length - 1, Math.floor(sr / 80));
  let best = 0, bestCorr = 0, energy = 0;
  for (let i = 0; i < buf.length; i++) energy += buf[i] * buf[i];
  for (let lag = minLag; lag <= maxLag; lag++) {
    let corr = 0;
    for (let i = 0; i < buf.length - lag; i++) corr += buf[i] * buf[i + lag];
    if (corr > bestCorr) { bestCorr = corr; best = lag; }
  }
  if (!best || bestCorr / energy < 0.3) return [null, rms];
  return [69 + 12 * Math.log2(sr / best / 440), rms];
}

el("hum").addEventListener("click", async () => {
  if (el("hum").disabled) return;
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch { el("humstate").textContent = "mic blocked"; return; }
  el("hum").disabled = true;
  const actx = synth.ctx || new (window.AudioContext || window.webkitAudioContext)();
  const an = actx.createAnalyser();
  an.fftSize = 2048;
  actx.createMediaStreamSource(stream).connect(an);
  const buf = new Float32Array(an.fftSize);
  const frames = [];
  const t0 = performance.now();
  let voicedAt = 0, lastVoiced = 0;
  await new Promise((done) => {
    (function tick() {
      const now = performance.now() - t0;
      an.getFloatTimeDomainData(buf);
      const [midi, rms] = pitchOf(buf, actx.sampleRate);
      if (midi && midi > 30 && midi < 96) {
        frames.push([Math.round(now), +midi.toFixed(2), +rms.toFixed(3)]);
        lastVoiced = now;
        if (!voicedAt) voicedAt = now;
      }
      el("humstate").textContent = voicedAt ? `● listening ${((8000 - now) / 1000).toFixed(0)}s` : "● hum now…";
      if (now > 8000 || (voicedAt && now - lastVoiced > 1200)) return done();
      requestAnimationFrame(tick);
    })();
  });
  stream.getTracks().forEach((tr) => tr.stop());
  el("hum").disabled = false;
  el("humstate").textContent = "";
  if (frames.length >= 12) {
    conn.send({ t: P.SONG_HUM, frames: frames.slice(0, 500) });
    log("melody sent — the orchestra picks it up at the next bar");
  } else {
    el("humstate").textContent = "couldn't hear a melody — hum louder/longer";
  }
});

// --- readout ----------------------------------------------------------------
let lastState = {}, barCount = 0, barStart = 0, barMs = 2400;

conn.on(P.CLOCK_PONG, (m) => clock.handlePong(m));
conn.on(P.SCHED_NOTES, (m) => {
  if (m.events.length > 2) {
    barStart = Math.min(...m.events.map((e) => e.at));
    if (lastState.bpm) barMs = 60000 / lastState.bpm * 4;
  }
  for (const e of m.events) {
    if (el("mute").checked && e.vel === 0.9 && e.art === "pluck") continue;
    synth.schedule(e);
  }
  barCount = m.events.length;
});
conn.on(P.SCHED_CANCEL, (m) => { if (m.allnotesoff) synth.panic(); });
conn.on(P.FX_TENSION, (m) => synth.setTension(m.value));
conn.on(P.FX_EXPR, (m) => { if (m.section === P.SECTION_ALL) synth.setExpression(m.semis, m.gain); });
conn.on(P.ERR, (m) => log(`⚠ ${m.msg}`));

conn.on(P.ENGINE_STATE, (m) => {
  lastState = m;
  if (typeof m.intensity === "number") {
    const i = m.intensity;
    const mode = i > 0.53 ? "HARMONY" : i < 0.47 ? "HUSH" : "verbatim";
    el("mode").textContent = mode + (mode === "HARMONY" ? " — chords blooming" :
                                     mode === "HUSH" ? " — texture thinning" : "");
    el("mode").style.color = mode === "HARMONY" ? "#e7c583" : mode === "HUSH" ? "#7fd1ff" : "#888";
    const w = Math.abs(i - 0.5) * 100;                 // 0..50%
    el("imeter").style.width = `${w}%`;
    el("imeter").style.left = i >= 0.5 ? "50%" : `${50 - w}%`;
    el("imeter").style.background = i >= 0.5 ? "#e7c583" : "#7fd1ff";
  }
  el("choice").textContent = m.device || m.last_choice || "—";
  el("choice").style.color = COLORS[(m.device || m.last_choice || "").split(" ")[0]] || "#ddd";
  const src = m.decision_source || "?";
  el("source").textContent = src === "model" ? "🧠 MODEL decided (your trained brain)" : `${src} decided`;
  el("source").className = src;
  const g = m.gesture;
  el("feat").textContent = g
    ? `gesture read as: energy ${g.energy.toFixed(2)} · vertical ${g.vertical.toFixed(2)} · ` +
      `rotation ${g.rotation.toFixed(2)} · ${g.duration.toFixed(2)}s`
    : "";
  log(`device: ${m.device || "—"} · decision: ${m.last_choice} [${src}] — ${m.song}`);
  renderMeta();
});
conn.on(P.ROSTER, (m) => {
  const eng = m.engine || {};
  if (eng.training_rows) el("meta").dataset.rows = eng.training_rows;
  renderMeta();
});

function renderMeta() {
  el("meta").textContent =
    `${lastState.song || ""} · ${lastState.bpm || ""} BPM · ${barCount} notes/batch` +
    (el("meta").dataset.rows ? ` · ${el("meta").dataset.rows} training rows harvested` : "");
}

(function barTick() {
  if (barStart && clock.theta !== null) {
    const p = ((clock.serverNow() - barStart) % barMs) / barMs;
    el("barpos").style.width = `${Math.max(0, Math.min(100, p * 100))}%`;
    el("barcap").textContent =
      `stings are instant · the rhythm itself changes in ${((1 - p) * barMs / 1000).toFixed(1)}s`;
  }
  requestAnimationFrame(barTick);
})();

setInterval(() => {
  if (clock.theta !== null) conn.send({ t: P.CLOCK_REPORT, theta: clock.theta, rtt: clock.rtt });
}, 2000);

conn.onOpen((w) => { clock.checkEpoch(w.server_time); log("connected"); });
conn.onClose(() => log("reconnecting…"));
conn.connect();
