// Phone wand: the phone's IMU stands in for the ESP32+MPU6050 wand.
//
// Streams wand.imu frames [tw, ax,ay,az, gx,gy,gz] — the SAME message the real
// firmware will send — so the hardware drops in later with zero server changes.
// Touch-and-hold anywhere = "grab" (the MPR121 capacitive pad's role): hold
// starts a gesture window, release ends it.
//
// Requires a secure context (HTTPS) — phones block DeviceMotion on plain http.
// Served on :8443; see README for the cert step.

import { Conn } from "../shared/ws.js";
import * as P from "../shared/protocol.js";

const IMU_BATCH = 5;   // frames per wand.imu packet (~60Hz devicemotion -> ~12 packets/s)

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";
const el = (id) => document.getElementById(id);

let conn = null;
let grabbed = false;
let seq = 0;
let imuBuf = [];
let lastAccel = 0;
let pktCount = 0, rate = 0;

// ---------------------------------------------------------------------------
// Axis mapping — phone held like a wand: top edge = tip, screen facing you.
// The MPU6050 firmware MUST mirror this exact convention (see firmware/README).
//   accel: accelerationIncludingGravity {x,y,z}  m/s^2, gravity included (matches MPU6050)
//   gyro : rotationRate {alpha,beta,gamma}        deg/s (spec) — verify per-browser
//          alpha = about screen-normal (z), beta = about left-right (x), gamma = about top-bottom (y)
// ---------------------------------------------------------------------------
function toFrame(tw, acc, rot) {
  return [
    Math.round(tw),
    r3(acc.x), r3(acc.y), r3(acc.z),
    r3(rot.beta), r3(rot.gamma), r3(rot.alpha),
  ];
}
const r3 = (v) => Math.round((v ?? 0) * 1000) / 1000;

function onMotion(e) {
  const acc = e.accelerationIncludingGravity || { x: 0, y: 0, z: 0 };
  const rot = e.rotationRate || { alpha: 0, beta: 0, gamma: 0 };
  const tw = performance.now();
  if (grabbed) {              // server only reads frames inside a grab; save the wifi
    imuBuf.push(toFrame(tw, acc, rot));
    if (imuBuf.length >= IMU_BATCH) flushImu();
  }

  // Feedback UI: gravity-inclusive magnitude and a tilt dot.
  lastAccel = Math.hypot(acc.x || 0, acc.y || 0, acc.z || 0);
  moveTilt(acc);
}

function flushImu() {
  if (!imuBuf.length || !conn) return;
  conn.send({ t: P.WAND_IMU, seq: seq++, frames: imuBuf });
  pktCount++;
  imuBuf = [];
}

function moveTilt(acc) {
  // Map tilt (gravity direction) to a dot position, purely as visual feedback.
  const pad = el("pad"), dot = el("tilt");
  const w = pad.clientWidth, h = pad.clientHeight;
  const x = w / 2 - (acc.x || 0) / 9.8 * (w / 2);
  const y = h / 2 + (acc.y || 0) / 9.8 * (h / 2);
  dot.style.left = Math.max(0, Math.min(w - 26, x)) + "px";
  dot.style.top = Math.max(0, Math.min(h - 26, y)) + "px";
}

// --- grab (touch/mouse) ---
function startGrab() {
  if (grabbed) return;
  grabbed = true;
  el("pad").classList.add("grab");
  el("padlabel").textContent = "GRABBED";
  el("grabstate").textContent = "GRABBED";
  el("grabstate").className = "grab";
  if (conn) conn.send({ t: P.WAND_GRAB, state: "start", tw: Math.round(performance.now()) });
}
function endGrab() {
  if (!grabbed) return;
  grabbed = false;
  el("pad").classList.remove("grab");
  el("padlabel").textContent = "HOLD TO GRAB";
  el("grabstate").textContent = "open";
  el("grabstate").className = "";
  flushImu();
  if (conn) conn.send({ t: P.WAND_GRAB, state: "end", tw: Math.round(performance.now()) });
}

function wireGrab() {
  const pad = el("pad");
  pad.addEventListener("touchstart", (e) => { e.preventDefault(); startGrab(); }, { passive: false });
  pad.addEventListener("touchend", (e) => { e.preventDefault(); endGrab(); }, { passive: false });
  pad.addEventListener("touchcancel", endGrab);
  pad.addEventListener("mousedown", startGrab);   // desktop testing
  window.addEventListener("mouseup", endGrab);
}

// --- enable + connect (iOS needs requestPermission inside a user gesture) ---
el("enable").addEventListener("click", async () => {
  const errEl = el("err");
  try {
    if (typeof DeviceMotionEvent !== "undefined" &&
        typeof DeviceMotionEvent.requestPermission === "function") {
      const res = await DeviceMotionEvent.requestPermission();
      if (res !== "granted") { errEl.textContent = "Motion permission denied."; return; }
    }
    window.addEventListener("devicemotion", onMotion);

    // Give it a beat to confirm events actually arrive (some desktops send none).
    let got = false;
    const probe = () => { got = true; window.removeEventListener("devicemotion", probe); };
    window.addEventListener("devicemotion", probe);
    setTimeout(() => {
      if (!got) errEl.textContent = "No motion events — is this a phone on HTTPS?";
    }, 1200);

    el("enable").style.display = "none";
    el("wand").style.display = "flex";
    wireGrab();

    conn = new Conn({ role: "wand-sim", session });
    conn.onOpen(() => el("dot").classList.add("ok"));
    conn.onClose(() => el("dot").classList.remove("ok"));
    conn.connect();
  } catch (e) {
    errEl.textContent = (e && e.message) ? e.message : "Could not enable motion.";
  }
});

// --- controls ---
el("thumbup").addEventListener("click", () => conn && conn.send({ t: P.WAND_FEEDBACK, value: 1 }));
el("thumbdown").addEventListener("click", () => conn && conn.send({ t: P.WAND_FEEDBACK, value: -1 }));
el("recal").addEventListener("click", () => conn && conn.send({ t: P.WAND_RECAL, tw: Math.round(performance.now()) }));
el("cycle").addEventListener("click", () => console.log("[wandsim] cycle section (aim routing wired in P3/P5)"));

// --- HUD refresh (accel readout + packet rate) ---
setInterval(() => {
  el("accel").textContent = lastAccel ? lastAccel.toFixed(1) : "—";
  rate = pktCount * IMU_BATCH; pktCount = 0;   // frames/sec
  el("rate").textContent = rate;
}, 1000);
