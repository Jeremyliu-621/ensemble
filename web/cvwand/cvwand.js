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
import * as P from "../shared/protocol.js";

const MP_VER = "0.10.14";
const WASM = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MP_VER}/wasm`;
const MODEL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task";

// MediaPipe hand landmark indices.
const WRIST = 0, THUMB_TIP = 4, INDEX_MCP = 5, INDEX_TIP = 8, MIDDLE_MCP = 9, PINKY_MCP = 17;

// Pinch hysteresis: grab when the thumb–index gap (relative to hand width) drops
// below GRAB_ON, release when it rises above GRAB_OFF. Two thresholds prevent flicker.
const GRAB_ON = 0.55, GRAB_OFF = 0.80;

const POSE_BATCH = 4;         // frames per wand.pose packet
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
let seq = 0;
let poseBuf = [];
const trail = [];             // {xm, y, grabbed}
let lastFrameT = 0, fps = 0;
let aimSection = 0;

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

// --- start camera + tracker (must be inside a user gesture) ---
el("start").addEventListener("click", async () => {
  el("start").style.display = "none";
  el("loading").style.display = "block";
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } }, audio: false,
    });
    video.srcObject = stream;
    await video.play();
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;

    el("loading").textContent = "loading hand model…";
    const fileset = await FilesetResolver.forVisionTasks(WASM);
    landmarker = await HandLandmarker.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: MODEL, delegate: "GPU" },
      runningMode: "VIDEO",
      numHands: 1,
    });
    el("loading").style.display = "none";

    connect();
    running = true;
    requestAnimationFrame(loop);
  } catch (e) {
    console.error("[cvwand] init failed", e);
    el("loading").style.display = "block";
    el("loading").textContent = "⚠ " + (e && e.message ? e.message : "camera/model failed — check webcam permission and internet");
  }
});

// --- WebSocket ---
function connect() {
  conn = new Conn({ role: "wand-cv", session });
  conn.onOpen(() => { el("dot").classList.add("ok"); });
  conn.onClose(() => { el("dot").classList.remove("ok"); });
  conn.connect();
}

function send(obj) { if (conn) conn.send(obj); }

// --- per-frame loop ---
function loop(now) {
  if (!running) return;
  if (lastFrameT) {
    const dt = now - lastFrameT;
    fps = fps ? fps * 0.9 + (1000 / dt) * 0.1 : 1000 / dt;
  }
  lastFrameT = now;

  let landmarks = null;
  try {
    const res = landmarker.detectForVideo(video, now);
    if (res.landmarks && res.landmarks.length) landmarks = res.landmarks[0];
  } catch (e) { /* transient frames before video is ready */ }

  if (landmarks) processHand(landmarks, now);
  else if (grabbed) endGrab(now);   // hand left frame while grabbing -> release

  draw(landmarks);
  updateHud();
  requestAnimationFrame(loop);
}

function processHand(lm, now) {
  const tip = lm[INDEX_TIP];
  const handW = dist(lm[INDEX_MCP], lm[PINKY_MCP]) || 0.001;
  const pinchRatio = dist(lm[THUMB_TIP], lm[INDEX_TIP]) / handW;
  el("pinch").textContent = pinchRatio.toFixed(2);

  // Grab state machine (hysteresis).
  if (!grabbed && pinchRatio < GRAB_ON) startGrab(now);
  else if (grabbed && pinchRatio > GRAB_OFF) endGrab(now);

  // Pose frame: mirror x for a selfie-natural feel; roll from wrist->middle-MCP.
  const xm = 1 - tip.x;
  const roll = Math.atan2(lm[MIDDLE_MCP].y - lm[WRIST].y, lm[MIDDLE_MCP].x - lm[WRIST].x) * 180 / Math.PI;
  poseBuf.push([Math.round(now), +xm.toFixed(4), +tip.y.toFixed(4), +tip.z.toFixed(4), +roll.toFixed(1)]);
  if (poseBuf.length >= POSE_BATCH) flushPose();

  trail.push({ xm, y: tip.y, grabbed });
  if (trail.length > TRAIL_LEN) trail.shift();
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

function updateHud() {
  el("fps").textContent = fps ? fps.toFixed(0) : "—";
  const s = el("grabstate");
  s.textContent = grabbed ? "GRABBED" : "open";
  s.className = grabbed ? "grabbed" : "";
}

// --- controls (thumbup/down/recal are real protocol messages; no-op server-side
//     until the ranker/aiming phases wire them) ---
el("thumbup").addEventListener("click", () => send({ t: P.WAND_FEEDBACK, value: 1 }));
el("thumbdown").addEventListener("click", () => send({ t: P.WAND_FEEDBACK, value: -1 }));
el("recal").addEventListener("click", () => send({ t: P.WAND_RECAL, tw: Math.round(performance.now()) }));
el("cycle").addEventListener("click", () => {
  aimSection++;
  console.log("[cvwand] cycle section ->", aimSection, "(aim routing wired in P3/P5)");
});
