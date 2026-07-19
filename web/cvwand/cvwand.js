// CV camera: webcam hand-tracking as the conductor's LEFT HAND.
//
// It does NOT conduct — the wand owns conducting, modes, aim and select-all, and
// the server drops wand.* from this role outright (a stray finger count flipping
// det/ai mid-performance was a real bug). The camera owns two things instead:
//   TRANSPORT — open palm plays, fist pauses, each held ~1.2s so a passing hand
//               can't stop the show.
//   MIXER     — pinch and drag: ↕ position rides volume, ↔ speed rides tempo,
//               across every instrument, streamed on cv.expr. The pinch must be
//               held ~5s to engage, and releasing it exits immediately. A held
//               pinch locks transport out, so riding a fade can never pause the show.
//
// Runs on the laptop (getUserMedia needs a secure context — localhost qualifies).

// GestureRecognizer, not HandLandmarker (ported from cv_hand_movements/): one
// pass returns landmarks + HANDEDNESS + trained gesture labels — Open_Palm and
// Closed_Fist come from MediaPipe's model instead of hand-rolled finger
// geometry, and handedness lets us track ONLY the physical left hand, so the
// wand-waving right hand can never fire transport.
import { GestureRecognizer, FilesetResolver }
  from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs";
import { Conn } from "../shared/ws.js";
import { effectLabel } from "../shared/vocab.js";
import * as P from "../shared/protocol.js";

const MP_VER = "0.10.14";
const WASM = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MP_VER}/wasm`;
const MODEL = "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task";

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

// The mixer takes COMMITMENT, like transport does: hold the pinch this long
// before it engages, so a hand that happens to close on its way somewhere else
// can't grab the faders. Leaving is the opposite — release drops it instantly.
const PINCH_ARM_MS = 5000;

const POSE_BATCH = 2;         // frames per cv.expr packet — small, so the beat
                              // follows the hand instead of trailing it by a batch
const TRAIL_LEN = 48;


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
let pinchSince = null;        // when the current pinch closed; null = not pinching
let seq = 0;
let poseBuf = [];
const trail = [];             // {xm, y, grabbed}
let playing = true;

// ── the camera's vocabulary (docs/demo_flow.md) ──────────────────────────────
//   ✋ PALM = play (hold)      ✊ FIST = pause (hold)
//   🤏 PINCH = mixer (hold ~5s to engage): drag ↕ volume, swipe ↔ tempo, all
//              instruments. Release exits at once. While held, transport is
//              locked out.
// Finger-count poses are NOT ours — modes/aim/select belong to the wand.
// Discrete-gesture reliability: classify off smoothed landmarks (raw ones are
// too jittery for finger-count geometry) and debounce with asymmetric
// confirm/release windows, so a single noisy frame can neither restart a hold
// nor prematurely drop an already-active gesture. The mixer stays instant —
// it reads raw landmarks below, untouched by any of this.
const SMOOTH_ALPHA = 0.5;           // EMA weight for classification-only landmarks
const CONFIRM_FRAMES = 3;           // consecutive frames a new gesture must hold to commit
const RELEASE_FRAMES = 4;           // consecutive misses before an active gesture drops
const COMMIT_COOLDOWN_MS = 250;     // minimum gap between two gesture commits
const cvMode = "NONE";              // no modes here any more; cv.state still reports one
let cvGesture = null;
let smoothLm = null;                // EMA-smoothed landmarks, classification only
let candGesture = null, candCount = 0, missCount = 0, lastCommitMs = -Infinity;

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
    const OPTS = {
      baseOptions: { modelAssetPath: MODEL, delegate: "GPU" },
      runningMode: "VIDEO",
      numHands: 2,      // find the LEFT hand even when the wand hand is in frame
      minHandDetectionConfidence: 0.5,
      minHandPresenceConfidence: 0.5,
      minTrackingConfidence: 0.5,
    };
    try {
      landmarker = await GestureRecognizer.createFromOptions(fileset, OPTS);
    } catch (err) {
      console.warn("[cvwand] GPU delegate failed, retrying on CPU", err);
      OPTS.baseOptions.delegate = "CPU";
      landmarker = await GestureRecognizer.createFromOptions(fileset, OPTS);
    }
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
  // The server echoes back what the mixer actually applied. Showing its numbers
  // rather than recomputing them locally means the readout can never drift from
  // the mapping that's really driving the show.
  conn.on(P.CV_EXPR, (m) => {
    const pct = Math.round(((m.gain ?? 1) - 0.3) / 0.9 * 100);
    el("mode").textContent = `🔊 ${pct}%   ♩ ${Math.round(m.bpm ?? 0)}`;
  });
  conn.connect();
}

function send(obj) { if (conn) conn.send(obj); }

// --- per-frame loop ---
// One inference per NEW camera frame (requestVideoFrameCallback where the
// browser supports it), timestamps kept monotonic for recognizeForVideo, and
// ONLY the physical left hand is tracked — MediaPipe's handedness stays
// "Left" for the physical left hand in a mirrored selfie preview (verified in
// the cv_hand_movements prototype), so the wand hand is invisible to us.
let lastTs = -1;
let builtinGesture = null;      // MediaPipe's trained label for the left hand
function loop(now) {
  if (!running) return;
  let landmarks = null;
  builtinGesture = null;
  try {
    const ts = Math.max(now, lastTs + 1);
    lastTs = ts;
    const res = landmarker.recognizeForVideo(video, ts);
    let best = -1;
    for (let i = 0; i < (res.landmarks?.length ?? 0); i++) {
      const h = res.handednesses?.[i]?.[0];
      if (h && h.categoryName === "Left" && h.score > best) {
        best = h.score;
        landmarks = res.landmarks[i];
        builtinGesture = res.gestures?.[i]?.[0] ?? null;
      }
    }
  } catch (e) { /* transient frames before video is ready */ }

  if (landmarks) processHand(landmarks, now);
  else {
    if (grabbed) endPinch(now);     // hand left frame mid-pinch -> release the mixer
    pinchSince = null;              // ...and a hand that left mid-arm starts over
    smoothLm = null;                // don't ease in from a stale hand position on re-entry
    candGesture = null; candCount = 0; missCount = 0;
    if (cvGesture !== null) { cvGesture = null; sendCvState(); }
  }

  draw(landmarks);
  if (typeof video.requestVideoFrameCallback === "function") {
    video.requestVideoFrameCallback((t) => loop(t));
  } else {
    requestAnimationFrame(loop);
  }
}

function processHand(lm, now) {
  const tip = lm[INDEX_TIP];
  const handW = dist(lm[INDEX_MCP], lm[PINKY_MCP]) || 0.001;
  const pinchRatio = dist(lm[THUMB_TIP], lm[INDEX_TIP]) / handW;
  const xm = 1 - tip.x;   // mirror x for a selfie-natural feel

  // ── discrete gesture layer: smoothed + debounced, drives transport/cv.state ──
  // PINCH IS MODAL: from the instant a pinch starts arming until the fingers
  // clearly open (GRAB_OFF hysteresis), the classifier looks for NOTHING else.
  // Mid-pinch frames read as fists/signs and were breaking the pinch — once
  // you're pinching, the only question is "still pinching?".
  const pinchOwnsHand = grabbed || pinchSince !== null;
  if (pinchOwnsHand) {
    if (cvGesture !== "PINCH") { cvGesture = "PINCH"; sendCvState(); }
    candGesture = null; candCount = 0; missCount = 0;
  } else {
    const slm = smoothLandmarks(lm);
    const sHandW = dist(slm[INDEX_MCP], slm[PINKY_MCP]) || 0.001;
    const sPinchRatio = dist(slm[THUMB_TIP], slm[INDEX_TIP]) / sHandW;
    updateGesture(classify(slm, sPinchRatio), xm, now);
  }

  // Conducting, mode poses, select/aim and the wand.pose stream stay CUT: the
  // camera never conducts (the server drops wand.* from the cv role). What it
  // DOES own is the mixer — pinch and drag to ride volume/tempo. Read off raw
  // landmarks, instant, so the fader doesn't lag the hand.
  // Hysteresis still decides what counts as "pinching" (GRAB_ON to close,
  // GRAB_OFF to open), but closing no longer engages the mixer on its own — it
  // starts the arming clock, and only PINCH_ARM_MS of unbroken hold engages it.
  // Release is immediate at every stage: it either cancels the arming or, once
  // engaged, exits the mixer on the spot.
  const pinching = grabbed || pinchSince !== null
    ? pinchRatio <= GRAB_OFF
    : pinchRatio < GRAB_ON;

  if (!pinching) {
    if (grabbed) endPinch(now);
    pinchSince = null;
  } else if (grabbed) {
    pushPinch(now, xm, tip.y);
  } else if (pinchSince === null) {
    pinchSince = now;
    flashCmd("🤏 hold to mix…");
  } else if (now - pinchSince >= PINCH_ARM_MS) {
    startPinch(now, xm, tip.y);
  }

  // AFTER the mixer, never before: a hand closing into a pinch passes through
  // shapes that read as a fist, so on the very frame the pinch latches this
  // must already know the mixer owns the hand. Running it first fired a pause
  // at the exact moment you started every pinch.
  tickTransportHold(now);

  trail.push({ xm, y: tip.y, grabbed });
  if (trail.length > TRAIL_LEN) trail.shift();
}

// One discrete gesture per frame, from the closed cv.state vocabulary.
function classify(lm, pinchRatio) {
  // MediaPipe's TRAINED gesture model decides PALM/FIST (ported from
  // cv_hand_movements/): far more robust than finger-count geometry across
  // lighting, angles, and hand shapes. PINCH keeps priority via the tight
  // threshold — a deliberate thumb-index touch beats a fist's incidental
  // thumb proximity, which is close but rarely that tight.
  if (pinchRatio < GRAB_ON_TIGHT) return "PINCH";
  const b = builtinGesture;
  if (b && b.score >= 0.5) {
    if (b.categoryName === "Closed_Fist") return "FIST";
    if (b.categoryName === "Open_Palm") return "PALM";
    // Demo hand-signs for the four devices — trained labels only, so they
    // hold up under stage lighting: 👍 harmony · 👎 hush · ✌️ arpeggio · ☝️ runs
    if (b.categoryName === "Thumb_Up") return "THUMB_UP";
    if (b.categoryName === "Thumb_Down") return "THUMB_DOWN";
    if (b.categoryName === "Victory") return "VICTORY";
  }
  if (pinchRatio < GRAB_ON) return "PINCH";
  return null;
}

// EMA landmark smoothing, used only for discrete-gesture classification — the
// pinch mixer reads raw landmarks so the fader never lags the hand.
function smoothLandmarks(lm) {
  if (!smoothLm || smoothLm.length !== lm.length) {
    smoothLm = lm.map((p) => ({ x: p.x, y: p.y, z: p.z }));
    return smoothLm;
  }
  for (let i = 0; i < lm.length; i++) {
    smoothLm[i].x += SMOOTH_ALPHA * (lm[i].x - smoothLm[i].x);
    smoothLm[i].y += SMOOTH_ALPHA * (lm[i].y - smoothLm[i].y);
    smoothLm[i].z += SMOOTH_ALPHA * (lm[i].z - smoothLm[i].z);
  }
  return smoothLm;
}

// Confirm a new gesture only after CONFIRM_FRAMES consecutive frames, and only
// drop an active one after RELEASE_FRAMES consecutive misses — a single noisy
// classification can neither restart a hold nor prematurely end one.
function updateGesture(g, xm, now) {
  if (cvGesture !== null) {
    if (g === cvGesture) { missCount = 0; return; }
    missCount++;
    if (missCount < RELEASE_FRAMES) return;
    missCount = 0;
    candGesture = g; candCount = g ? 1 : 0;
    commitGesture(null, xm);          // release: drop to neutral
    return;
  }
  if (!g) { candGesture = null; candCount = 0; return; }
  if (g === candGesture) candCount++;
  else { candGesture = g; candCount = 1; }
  if (candCount >= CONFIRM_FRAMES && now - lastCommitMs >= COMMIT_COOLDOWN_MS) {
    lastCommitMs = now;
    candGesture = null; candCount = 0;
    commitGesture(g, xm);
  }
}

// The camera is TRANSPORT + MIXER (the wand owns conducting, modes, and aim —
// a finger count must never flip det/ai mid-performance). And transport needs
// COMMITMENT: hold the pose ~1.2s before it fires, so a passing open hand
// can't stop the show.
const TRANSPORT_HOLD_MS = 1200;
const DEVICE_HOLD_MS = 900;   // device signs commit a touch faster than transport
// Demo hand-signs -> the four musical devices (fires admin.cmd device, the
// camera's ONE sanctioned musical channel; wand.* stays blocked for this role).
const SIGN_DEVICE = {
  THUMB_UP: ["HARMONY", "👍 harmony"],
  THUMB_DOWN: ["HUSH", "👎 hush"],
  VICTORY: ["ARPEGGIO", "✌️ arpeggio"],
  // POINT_UP was cut: MediaPipe confuses it with Victory. Runs stay on the
  // wand pose / pads / console button.
};
let holdSince = 0, holdFired = false;

function commitGesture(g, xm) {
  cvGesture = g;
  holdSince = performance.now();
  holdFired = false;
  if (g === "PALM" && !playing) flashCmd("✋ hold to play…");
  else if (g === "FIST" && playing) flashCmd("✊ hold to pause…");
  else if (SIGN_DEVICE[g]) flashCmd(`${SIGN_DEVICE[g][1]} — hold…`);
  sendCvState();
}

function tickTransportHold(now) {
  // A held pinch OWNS the hand: riding the volume fader curls the fingers in
  // ways that read as a fist, and pausing the show mid-fade is never what you
  // meant. Park the clock at "now" for as long as the mixer holds the hand, so
  // the wait also can't mature DURING a pinch and fire the instant you let go.
  // The arming hold counts as owning it too — otherwise the 5s wait for the
  // mixer sits there long enough for the 1.2s fist hold to pause the show
  // underneath it, every single time.
  if (grabbed || pinchSince !== null) { holdSince = now; return; }
  if (holdFired || !cvGesture) return;
  const held = now - holdSince;
  if (cvGesture === "PALM" && !playing && held >= TRANSPORT_HOLD_MS) {
    playing = true; holdFired = true;
    send({ t: P.ADMIN_CMD, cmd: "start" }); flashCmd("✋ play");
  } else if (cvGesture === "FIST" && playing && held >= TRANSPORT_HOLD_MS) {
    playing = false; holdFired = true;
    send({ t: P.ADMIN_CMD, cmd: "stop" }); flashCmd("✊ pause");
  } else if (SIGN_DEVICE[cvGesture] && held >= DEVICE_HOLD_MS) {
    const [device, label] = SIGN_DEVICE[cvGesture];
    holdFired = true;
    send({ t: P.ADMIN_CMD, cmd: "device", args: { name: device } });
    flashCmd(label);
  }
}

// setMode/aimAt/detectShake/selectAll are GONE: modes, aiming and select-all
// are wand-only now, and the server drops wand.mode plus admin aim verbs from
// the cv role — keeping them here would just have been dead code that lies
// about what the camera can do.

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

// ── the left-hand mixer ──────────────────────────────────────────────────────
// Pinch and drag: ↕ position rides volume, ↔ speed rides tempo, both across
// every instrument. Frames batch exactly like the old pose stream did, but on
// cv.expr — the camera is barred from wand.* and this is explicitly not
// conducting, it's the mixing desk.
function flushPinch(state) {
  if (state === "move" && !poseBuf.length) return;
  send({ t: P.CV_EXPR, state, seq: seq++, frames: poseBuf });
  poseBuf = [];
}

function startPinch(now, xm, y) {
  grabbed = true;
  holdFired = true;             // kill any transport hold the approach matured
  poseBuf = [[Math.round(now), +xm.toFixed(4), +y.toFixed(4)]];
  flushPinch("start");
  flashCmd("🤏 mixer — drag ↕ volume, swipe ↔ tempo");
}

function pushPinch(now, xm, y) {
  poseBuf.push([Math.round(now), +xm.toFixed(4), +y.toFixed(4)]);
  if (poseBuf.length >= POSE_BATCH) flushPinch("move");
}

function endPinch(now) {
  grabbed = false;
  pinchSince = null;                  // a new pinch arms from scratch
  flushPinch("end");                  // whatever you dialled in stays put
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
