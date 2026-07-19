// CV wand: webcam hand-tracking as a hardware-free wand.
//
// Index fingertip = wand tip. Pinch (thumb+index) = "grab", the same segmentation
// role the MPR121 capacitive sensor plays on the real wand: pinch closed starts a
// gesture window, opening ends it. Streams wand.pose frames continuously (the
// server buffers the ones inside a grab, exactly as it will for wand.imu) plus
// wand.grab start/end. Joins the single wand slot as variant "cv".
//
// Runs on the laptop (getUserMedia needs a secure context — localhost qualifies).

import { HandLandmarker, FilesetResolver }
  from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs";
import { Conn } from "../shared/ws.js";
import { effectLabel } from "../shared/vocab.js";
import * as P from "../shared/protocol.js";

const MP_VER = "0.10.14";
const WASM = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MP_VER}/wasm`;
const MODEL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task";

// MediaPipe hand landmark indices.
const WRIST = 0, THUMB_TIP = 4, INDEX_MCP = 5, INDEX_TIP = 8, MIDDLE_MCP = 9, PINKY_MCP = 17;
const INDEX_PIP = 6, MIDDLE_TIP = 12, MIDDLE_PIP = 10, RING_TIP = 16, RING_PIP = 14,
      PINKY_TIP = 20, PINKY_PIP = 18;

// Pinch hysteresis: grab when the thumb–index gap (relative to hand width) drops
// below GRAB_ON, release when it rises above GRAB_OFF. Two thresholds prevent flicker.
const GRAB_ON = 0.55, GRAB_OFF = 0.80;
// Tighter pinch threshold used only to disambiguate fist vs. pinch when every
// finger reads curled (see classify()) — a deliberate pinch is an actual touch,
// a fist's incidental thumb proximity is close but rarely this tight.
const GRAB_ON_TIGHT = 0.30;

const POSE_BATCH = 4;         // frames per wand.pose packet
const TRAIL_LEN = 48;

// Shake-to-select-all: a fast, repeated back-and-forth sweep while in SELECT
// mode clears aim to "all" (every instrument), same as the hardware wand's
// shake. Direction reversals in the fingertip's x are the signal — a real
// point only moves one way.
const SHAKE_WINDOW_MS = 500;
const SHAKE_MIN_DELTA = 0.035;   // per-frame xm jump fast enough to count
const SHAKE_MIN_REVERSALS = 4;
const SELECT_ALL_LATCH_MS = 1500; // "all" sticks this long before pointing resumes aiming

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";
const el = (id) => document.getElementById(id);

const video = el("video");
const canvas = el("canvas");
const ctx = canvas.getContext("2d");

let landmarker = null;
let conn = null;
let running = false;
let grabbed = false;
let seq = 0;
let poseBuf = [];
const trail = [];             // {xm, y, grabbed}
let playing = true;

// ── the demo-flow gesture vocabulary (docs/demo_flow.md) ─────────────────────
// The webcam hand sets MODES and transport; conducting happens in AI mode.
//   ✋ PALM = play      ✊ FIST = pause     🤏 PINCH: AI = conduct, DET = drag
//                  ↕ volume + swipe ↔ tempo (every instrument), else inert
//   ☝ ONE_FINGER = SELECT (point at an instrument to target AI/DET edits at
//                  it, nothing is muted; shake to target "all" instead)
//   ✌ TWO_FINGERS = DETERMINISTIC mode    🤟 THREE_FINGERS = AI mode
// Discrete-gesture reliability: classify off smoothed landmarks (raw ones are
// too jittery for finger-count geometry) and debounce with asymmetric
// confirm/release windows, so a single noisy frame can neither restart a hold
// nor prematurely drop an already-active gesture. Conducting stays instant —
// it reads raw landmarks below, untouched by any of this.
const SMOOTH_ALPHA = 0.5;           // EMA weight for classification-only landmarks
const CONFIRM_FRAMES = 3;           // consecutive frames a new gesture must hold to commit
const RELEASE_FRAMES = 4;           // consecutive misses before an active gesture drops
const COMMIT_COOLDOWN_MS = 250;     // minimum gap between two gesture commits
let cvMode = "AI";                  // start ready to conduct
let cvGesture = null;
let smoothLm = null;                // EMA-smoothed landmarks, classification only
let candGesture = null, candCount = 0, missCount = 0, lastCommitMs = -Infinity;
let roster = [];                    // connected sections, left->right (for SELECT aiming)
let lastAimId = null, lastAimMs = 0;
let shakeBuf = [];                  // {t, xm} recent SELECT-mode samples
let lastShakeMs = -Infinity, selectAllUntil = 0;

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

// --- start camera + tracker: AUTOMATICALLY, on load. No buttons, no dials —
// the camera IS the wand and it should simply be on. (getUserMedia needs no
// user gesture; the browser's own permission prompt is the only gate, and it
// remembers the answer.) Failures show a friendly note and retry.
async function boot() {
  // Browsers only expose the camera on secure pages (localhost or https) —
  // over plain http on a LAN IP, navigator.mediaDevices simply doesn't exist.
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    el("loading").innerHTML =
      "🔒 the camera only works on a secure page<br>" +
      `on the laptop open <b>http://localhost:${location.port || 80}</b>` +
      `<br>(or https://${location.hostname}:8443 after trusting the cert)`;
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } }, audio: false,
    });
    video.srcObject = stream;
    await video.play();
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;

    el("loading").textContent = "waking the hand tracker…";
    const fileset = await FilesetResolver.forVisionTasks(WASM);
    landmarker = await HandLandmarker.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: MODEL, delegate: "GPU" },
      runningMode: "VIDEO",
      numHands: 1,
    });
    el("loading").hidden = true;

    // one short hint, then the hand speaks for itself
    const hint = el("hint");
    hint.hidden = false;
    setTimeout(() => { hint.style.opacity = "0"; setTimeout(() => (hint.hidden = true), 1100); }, 6000);

    connect();
    running = true;
    requestAnimationFrame(loop);
  } catch (e) {
    console.error("[cvwand] init failed", e);
    el("loading").hidden = false;
    el("loading").textContent = "⚠ " + (e && e.name === "NotAllowedError"
      ? "camera blocked — allow it in the address bar and this retries itself"
      : (e && e.message ? e.message : "camera/model failed — check webcam + internet"));
    setTimeout(boot, 4000);      // self-heal: permission granted later just works
  }
}
boot();

// --- WebSocket ---
let lastDevice = null;
function connect() {
  // ephemeral: the hub iframe and a popped-out camera tab must be two distinct
  // clients — a shared persisted id makes them evict each other from the single
  // wand slot in a 1 Hz reconnect storm (the wand "randomly dying" bug).
  conn = new Conn({ role: "wand-cv", session, ephemeral: true });
  conn.onOpen(() => { el("dot").classList.add("ok"); });
  conn.onClose(() => { el("dot").classList.remove("ok"); });
  // No guesswork: the engine reports what each gesture ACTUALLY did (`device`),
  // and we flash it on screen in the exact words of the console's moves card.
  conn.on(P.ENGINE_STATE, (m) => {
    if (m.playing !== undefined) playing = m.playing;   // transport truth
    if (m.device === undefined || m.device === lastDevice) return;
    const first = lastDevice === null;
    lastDevice = m.device;
    if (first) return;                    // initial sync, not something you did
    const fx = effectLabel(m.device);
    flashCmd(`${fx.icon} ${fx.label}`);
  });
  // engine.state only broadcasts while notes are scheduled, so it never reports
  // the "just paused" transition — wand.cmd is the reliable, unconditional sync
  // (sent on connect and after every admin cmd), so PALM/FIST don't go stale.
  conn.on(P.WAND_CMD, (m) => {
    if (m.playing !== undefined) playing = m.playing;
  });
  // roster: SELECT-mode pointing sweeps the connected phones left -> right
  conn.on(P.ROSTER, (m) => {
    roster = (m.sections || []).filter((s) => s.connected)
      .map((s) => ({ id: s.id, px: s.px ?? 0, instrument: s.instrument }))
      .sort((a, b) => a.px - b.px);
  });
  conn.connect();
}

function send(obj) { if (conn) conn.send(obj); }

// --- per-frame loop ---
function loop(now) {
  if (!running) return;
  let landmarks = null;
  try {
    const res = landmarker.detectForVideo(video, now);
    if (res.landmarks && res.landmarks.length) landmarks = res.landmarks[0];
  } catch (e) { /* transient frames before video is ready */ }

  if (landmarks) processHand(landmarks, now);
  else {
    if (grabbed) endGrab(now);      // hand left frame while grabbing -> release
    smoothLm = null;                // don't ease in from a stale hand position on re-entry
    candGesture = null; candCount = 0; missCount = 0;
    if (cvGesture !== null) { cvGesture = null; sendCvState(); }
  }

  draw(landmarks);
  requestAnimationFrame(loop);
}

function processHand(lm, now) {
  const tip = lm[INDEX_TIP];
  const handW = dist(lm[INDEX_MCP], lm[PINKY_MCP]) || 0.001;
  const pinchRatio = dist(lm[THUMB_TIP], lm[INDEX_TIP]) / handW;
  const xm = 1 - tip.x;   // mirror x for a selfie-natural feel

  // ── discrete gesture layer: debounced, drives modes/transport/cv.state ────
  const g = classify(lm, pinchRatio);
  if (g === rawGesture) rawCount++;
  else { rawGesture = g; rawCount = 1; }
  if (rawCount === STABLE_FRAMES && g !== cvGesture) commitGesture(g, xm);
  tickTransportHold(now);

  // Conducting pinch, mode poses, select/aim, and the pose stream are all
  // CUT: the camera is transport-only now (server enforces it too — wand.*
  // from the cv role is dropped). The wand conducts; the camera plays/pauses.

  trail.push({ xm, y: tip.y, grabbed });
  if (trail.length > TRAIL_LEN) trail.shift();
}

// One discrete gesture per frame, from the closed cv.state vocabulary.
function classify(lm, pinchRatio) {
  const ext = (t2, p2) => dist(lm[t2], lm[WRIST]) > dist(lm[p2], lm[WRIST]) * 1.15;
  const f = [ext(INDEX_TIP, INDEX_PIP), ext(MIDDLE_TIP, MIDDLE_PIP),
             ext(RING_TIP, RING_PIP), ext(PINKY_TIP, PINKY_PIP)];
  const n = f.filter(Boolean).length;
  // A closed fist's thumb naturally rests near/over the curled fingers, which
  // reads as a loose thumb-index pinch — that swallow was the real bug: FIST
  // silently became PINCH, so pause never committed and closing your hand
  // instead spammed the non-AI scrub path (rewind). But a deliberate pinch with
  // the rest of the hand also relaxed/curled (how most people actually pinch)
  // reads as n===0 too, so a flat "n===0 wins" rule swallows pinch the other
  // way. When every finger curls, use the tighter GRAB_ON_TIGHT threshold to
  // tell them apart: a real pinch is an actual touch, a fist's incidental
  // thumb proximity is close but rarely that tight.
  if (n === 0) return pinchRatio < GRAB_ON_TIGHT ? "PINCH" : "FIST";
  if (pinchRatio < GRAB_ON) return "PINCH";
  if (n === 4) return "PALM";
  if (f[0] && n === 1) return "ONE_FINGER";
  if (f[0] && f[1] && n === 2) return "TWO_FINGERS";
  if (f[0] && f[1] && f[2] && n === 3) return "THREE_FINGERS";
  return null;
}

// The camera is TRANSPORT-ONLY (the wand owns conducting, modes, and aim —
// a finger count must never flip det/ai mid-performance). And transport needs
// COMMITMENT: hold the pose ~1.2s before it fires, so a passing open hand
// can't stop the show.
const TRANSPORT_HOLD_MS = 1200;
let holdSince = 0, holdFired = false;

function commitGesture(g, xm) {
  cvGesture = g;
  holdSince = performance.now();
  holdFired = false;
  if (g === "PALM" && !playing) flashCmd("✋ hold to play…");
  else if (g === "FIST" && playing) flashCmd("✊ hold to pause…");
  sendCvState();
}

function tickTransportHold(now) {
  if (holdFired || !cvGesture || now - holdSince < TRANSPORT_HOLD_MS) return;
  if (cvGesture === "PALM" && !playing) {
    playing = true; holdFired = true;
    send({ t: P.ADMIN_CMD, cmd: "start" }); flashCmd("✋ play");
  } else if (cvGesture === "FIST" && playing) {
    playing = false; holdFired = true;
    send({ t: P.ADMIN_CMD, cmd: "stop" }); flashCmd("✊ pause");
  }
}

function setMode(m) {
  if (cvMode === m) return;
  // A direct mode flip mid-pinch (AI<->DET) must not leave a grab open under
  // the old mode's meaning — close it now; next frame's hysteresis check
  // reopens a fresh one under the new mode if the hand is still pinched.
  if (grabbed) endGrab(performance.now());
  cvMode = m;
  el("mode").textContent = { SELECT: "☝ SELECT", DETERMINISTIC: "✌ DET", AI: "🤟 AI" }[m];
  if (m === "DETERMINISTIC") send({ t: P.WAND_MODE, mode: "det" });
  if (m === "AI") send({ t: P.WAND_MODE, mode: "ai" });
  flashCmd({ SELECT: "☝ select — point at an instrument",
             DETERMINISTIC: "✌ deterministic — pinch: drag ↕ = volume, swipe ↔ = tempo",
             AI: "🤟 AI mode — pinch + wave to conduct" }[m]);
}

// SELECT mode: the index finger sweeps the connected phones left -> right;
// pointing targets the one under your finger for AI/DET edits (nothing is
// muted — every instrument keeps playing; the console card just glows).
function aimAt(xm, now) {
  if (!roster.length || now - lastAimMs < 350) return;
  const idx = Math.max(0, Math.min(roster.length - 1, Math.floor(xm * roster.length)));
  const s = roster[idx];
  if (s.id === lastAimId) return;
  lastAimId = s.id; lastAimMs = now;
  send({ t: P.ADMIN_CMD, cmd: "aim", args: { section_id: s.id } });
  flashCmd(`🎯 ${s.instrument || s.id}`);
}

// A fast back-and-forth sweep (direction reversals in the fingertip's x)
// reads as a shake rather than a point — used to explicitly target "all".
function detectShake(xm, now) {
  shakeBuf.push({ t: now, xm });
  const cutoff = now - SHAKE_WINDOW_MS;
  while (shakeBuf.length && shakeBuf[0].t < cutoff) shakeBuf.shift();
  if (shakeBuf.length < 4) return false;
  let reversals = 0, prevSign = 0;
  for (let i = 1; i < shakeBuf.length; i++) {
    const d = shakeBuf[i].xm - shakeBuf[i - 1].xm;
    if (Math.abs(d) < SHAKE_MIN_DELTA) continue;
    const sign = d > 0 ? 1 : -1;
    if (prevSign && sign !== prevSign) reversals++;
    prevSign = sign;
  }
  if (reversals >= SHAKE_MIN_REVERSALS && now - lastShakeMs > SELECT_ALL_LATCH_MS) {
    lastShakeMs = now;
    shakeBuf = [];
    return true;
  }
  return false;
}

function selectAll(now) {
  lastAimId = "all"; lastAimMs = now;
  selectAllUntil = now + SELECT_ALL_LATCH_MS;   // let go before pointing resumes aiming
  send({ t: P.ADMIN_CMD, cmd: "aim", args: { section_id: "all" } });
  flashCmd("🤚 select all");
}

function sendCvState() {
  send({ t: P.CV_STATE, gesture: cvGesture, mode: cvMode, confidence: 0.9 });
}

function flashCmd(text) {
  const d = document.createElement("div");
  d.textContent = text;
  d.style.cssText = "position:fixed;left:50%;top:18%;transform:translateX(-50%);z-index:50;" +
    "color:#e7c583;background:rgba(20,12,8,.9);padding:10px 20px;border:1px solid #a8712a;" +
    "border-radius:8px;font-size:22px;transition:opacity .5s;pointer-events:none";
  document.body.appendChild(d);
  setTimeout(() => { d.style.opacity = "0"; setTimeout(() => d.remove(), 600); }, 900);
}

function flushPose() {
  if (!poseBuf.length) return;
  send({ t: P.WAND_POSE, seq: seq++, frames: poseBuf });
  poseBuf = [];
}

function startGrab(now) {
  grabbed = true;
  send({ t: P.WAND_GRAB, state: "start", tw: Math.round(now) });
}
function endGrab(now) {
  grabbed = false;
  flushPose();
  send({ t: P.WAND_GRAB, state: "end", tw: Math.round(now) });
}

// --- drawing ---
function draw(landmarks) {
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  // mirrored video
  ctx.save(); ctx.translate(w, 0); ctx.scale(-1, 1);
  ctx.drawImage(video, 0, 0, w, h);
  ctx.restore();

  // overlays in mirrored-normalized coords
  const MX = (p) => (1 - p.x) * w, MY = (p) => p.y * h;

  // fingertip trail (glows when grabbing)
  if (trail.length > 1) {
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    for (let i = 1; i < trail.length; i++) {
      const a = trail[i - 1], b = trail[i];
      const on = b.grabbed;
      ctx.strokeStyle = on ? "rgba(70,209,122,0.9)" : "rgba(231,197,131,0.35)";
      ctx.lineWidth = on ? 10 * (i / trail.length) : 3;
      ctx.beginPath(); ctx.moveTo(a.xm * w, a.y * h); ctx.lineTo(b.xm * w, b.y * h); ctx.stroke();
    }
  }

  if (landmarks) {
    ctx.fillStyle = "#9fb4d8";
    for (const p of landmarks) { ctx.beginPath(); ctx.arc(MX(p), MY(p), 3, 0, 6.3); ctx.fill(); }
    // fingertip marker + grab ring
    const tip = landmarks[INDEX_TIP];
    ctx.beginPath(); ctx.arc(MX(tip), MY(tip), grabbed ? 16 : 8, 0, 6.3);
    ctx.fillStyle = grabbed ? "rgba(70,209,122,0.85)" : "rgba(231,197,131,0.85)"; ctx.fill();
  }
}

// (No on-screen controls: the hand is the whole interface. Thumbs/recal remain
// protocol messages the hardware wand can still send; nothing here needs them.)
