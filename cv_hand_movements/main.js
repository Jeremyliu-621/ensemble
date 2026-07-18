// Wires the left-hand CV pipeline (recognizer -> filter -> smooth -> classify ->
// debounce) and renders the overlay, mode state, and gesture panel.

import { createRecognizer, startCamera, runLoop } from "./cv/recognizer.js";
import { makeSmoother, GestureStabilizer } from "./cv/stabilize.js";
import { makePinchTracker, classifyGesture, L } from "./cv/gestures.js";
import { pickLeftHand } from "./cv/handedness.js";
import { MidiPlayer } from "./midi/player.js";
import { Timeline } from "./midi/timeline.js";
import { GestureRouter } from "./midi/commands.js";

const el = (id) => document.getElementById(id);
const video = el("video");
const overlay = el("overlay");
const ctx = overlay.getContext("2d");

// Display metadata for each gesture label.
const GESTURE_META = {
  PALM:         { glyph: "✋", color: "#8b7bff", name: "OPEN PALM" },
  PINCH:        { glyph: "🤏", color: "#46d17a", name: "PINCH" },
  FIST:         { glyph: "✊", color: "#ff9f45", name: "CLOSED FIST" },
  ONE_FINGER:   { glyph: "1", color: "#4fc3f7", name: "SELECT MODE" },
  TWO_FINGERS:  { glyph: "2", color: "#ffb74d", name: "DETERMINISTIC MODE" },
  THREE_FINGERS: { glyph: "3", color: "#f06292", name: "AI MODE" },
};

const MODE_BY_GESTURE = {
  ONE_FINGER: "SELECT",
  TWO_FINGERS: "DETERMINISTIC",
  THREE_FINGERS: "AI",
};

const MODE_META = {
  NONE: { name: "No mode toggled", instruction: "Show 1, 2, or 3 fingers" },
  SELECT: { name: "Select mode", instruction: "Toggled with one finger" },
  DETERMINISTIC: { name: "Deterministic edit mode", instruction: "Toggled with two fingers" },
  AI: { name: "AI edit mode", instruction: "Toggled with three fingers" },
};

// --- pipeline state ---
const smooth = makeSmoother(0.5);
const pinchTracker = makePinchTracker({ on: 0.35, off: 0.5 });
const stabilizer = new GestureStabilizer({ confirmFrames: 3, releaseFrames: 4, refractoryMs: 250 });
let currentMode = "NONE";
let currentModeGesture = null;
let fpsEMA = 0;

// --- MIDI editor ---
const player = new MidiPlayer({ onChange: refreshTransport });
const timeline = new Timeline(el("roll"), player);
const router = new GestureRouter(player, timeline, {
  onAction: (msg) => logLine(`🎛 <b>${msg}</b>`),
});

// Hand-skeleton connections for the overlay.
const BONES = [
  [0,1],[1,2],[2,3],[3,4],            // thumb
  [0,5],[5,6],[6,7],[7,8],            // index
  [5,9],[9,10],[10,11],[11,12],      // middle
  [9,13],[13,14],[14,15],[15,16],    // ring
  [13,17],[17,18],[18,19],[19,20],   // pinky
  [0,17],                             // palm base
];

// ---------------- start ----------------
el("start").addEventListener("click", async () => {
  el("start").style.display = "none";
  el("loading").style.display = "block";
  try {
    await MidiPlayer.unlockAudio();   // resume AudioContext inside the user gesture
    const dims = await startCamera(video);
    overlay.width = dims.width;
    overlay.height = dims.height;

    el("loading").textContent = "loading gesture model…";
    const { recognizer, delegate } = await createRecognizer();
    el("delegate").textContent = delegate;
    el("loading").style.display = "none";

    runLoop(video, recognizer, onFrame);
  } catch (e) {
    console.error("[cv] init failed", e);
    el("loading").textContent = "camera/model failed — check permissions & console";
    el("loading").style.display = "block";
  }
});

// ---------------- per-frame ----------------
function onFrame(hands, dtMs) {
  // FPS (smoothed)
  if (dtMs > 0) {
    const inst = 1000 / dtMs;
    fpsEMA = fpsEMA ? fpsEMA + 0.15 * (inst - fpsEMA) : inst;
    el("fps").textContent = fpsEMA.toFixed(0);
  }
  el("handCount").textContent = hands.length;

  // Ignore the physical right hand completely. Recognition stays configured for
  // two hands so the physical left can still be found when both are in frame.
  const control = pickLeftHand(hands);
  if (control) {
    control.hand.landmarks = smooth(control.key, control.hand.landmarks);
  }

  // Classify + debounce.
  let label = null, score = 0;
  if (control) {
    const res = classifyGesture(control.hand, pinchTracker, control.key);
    label = res.gesture;
    score = res.score;
  }
  const event = stabilizer.update(label);
  if (event) handleEvent(event, control);

  // Route to the MIDI editor: discrete edges + continuous pinch-scrub.
  // Un-mirror X (video/overlay are CSS-mirrored) so hand-right = playhead-right.
  const handX = control ? 1 - control.hand.landmarks[L.WRIST].x : null;
  router.update({ event, active: stabilizer.active, handX });

  updatePanel(control, label, score);
  updateTrackingStatus(control);
  draw(control);
}

// Rising/falling edge events update sticky mode state and the gesture HUD.
function handleEvent(event, control) {
  const hand = control?.hand?.handedness?.categoryName ?? "?";
  if (event.phase === "enter") {
    const mode = MODE_BY_GESTURE[event.gesture];
    if (mode) {
      setMode(mode, event.gesture);
      showModeBadge();
    } else {
      showBadge(event.gesture);
    }
    logLine(`<b>${event.gesture}</b> · ${hand} hand`);
  } else if (!MODE_BY_GESTURE[event.gesture]) {
    hideBadge();
    showModeBadge();
  }
}

// ---------------- UI ----------------
function updatePanel(control, label, score) {
  const handStatus = el("ctrlHand");
  handStatus.textContent = control ? "Detected" : "Not detected";
  handStatus.classList.toggle("free", Boolean(control));
  el("ctrlReason").textContent = control
    ? `${Math.round((control.hand.handedness?.score ?? 0) * 100)}% confidence`
    : "Physical left only";

  const meta = label ? GESTURE_META[label] : null;
  el("nowGlyph").textContent = meta ? meta.glyph : "·";
  const name = el("nowName");
  name.textContent = meta?.name || "no gesture";
  name.classList.toggle("idle", !label);
  el("confBar").style.width = `${Math.round((label ? score : 0) * 100)}%`;
  if (meta) el("confBar").style.background = meta.color;
}

function showBadge(label) {
  const meta = GESTURE_META[label] || { glyph: "✷", name: label };
  el("badgeGlyph").textContent = meta.glyph;
  el("badgeLabel").textContent = meta.name;
  el("badge").classList.add("show");
}
function hideBadge() { el("badge").classList.remove("show"); }

function showModeBadge() {
  if (currentMode === "NONE" || !currentModeGesture) return;
  const mode = MODE_META[currentMode];
  const gesture = GESTURE_META[currentModeGesture];
  el("badgeGlyph").textContent = gesture.glyph;
  el("badgeLabel").textContent = `${mode.name} toggled`;
  el("badge").classList.add("show");
}

function setMode(mode, gesture) {
  if (!MODE_META[mode]) return;
  currentMode = mode;
  currentModeGesture = gesture;
  const meta = MODE_META[mode];
  el("modeModal").dataset.mode = mode;
  el("modeName").textContent = meta.name;
  el("modeInstruction").textContent = meta.instruction;
}

function updateTrackingStatus(control) {
  const status = el("leftStatus");
  status.textContent = control ? "Left hand detected" : "Left hand not detected";
  status.classList.toggle("ok", Boolean(control));
}

function logLine(html) {
  const div = document.createElement("div");
  div.innerHTML = `${new Date().toLocaleTimeString([], { hour12: false })} — ${html}`;
  const log = el("log");
  log.prepend(div);
  while (log.children.length > 40) log.removeChild(log.lastChild);
}

// ---------------- overlay drawing ----------------
// The canvas is CSS-mirrored to match the video, so we draw in raw (unmirrored)
// normalized coords scaled to canvas pixels.
function draw(control) {
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  if (!control) return;
  const W = overlay.width, H = overlay.height;
  const lm = control.hand.landmarks;
  if (!lm) return;

  ctx.lineWidth = 4;
  ctx.strokeStyle = "#8b7bff";
  ctx.beginPath();
  for (const [a, b] of BONES) {
    ctx.moveTo(lm[a].x * W, lm[a].y * H);
    ctx.lineTo(lm[b].x * W, lm[b].y * H);
  }
  ctx.stroke();

  for (const p of lm) {
    ctx.beginPath();
    ctx.arc(p.x * W, p.y * H, 5, 0, Math.PI * 2);
    ctx.fillStyle = "#c9c0ff";
    ctx.fill();
  }

  ctx.strokeStyle = "#46d17a";
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.moveTo(lm[L.THUMB_TIP].x * W, lm[L.THUMB_TIP].y * H);
  ctx.lineTo(lm[L.INDEX_TIP].x * W, lm[L.INDEX_TIP].y * H);
  ctx.stroke();
}

// ---------------- MIDI editor: buttons, upload, render ----------------
// On-screen controls keep the MIDI transport testable without a camera.
el("btnPlay").addEventListener("click", () => player.toggle());
el("btnRewind").addEventListener("click", () => player.rewind());
el("btnPrev").addEventListener("click", () => player.prevSection());
el("btnNext").addEventListener("click", () => player.nextSection());
el("btnLoop").addEventListener("click", () => player.toggleLoop());

// Click/drag the roll to scrub with a mouse (parallels pinch-scrub).
const roll = el("roll");
let mouseScrub = false;
const rollSeek = (e) => {
  const r = roll.getBoundingClientRect();
  player.seek(timeline.xToTime((e.clientX - r.left) / r.width));
};
roll.addEventListener("pointerdown", (e) => { mouseScrub = true; roll.setPointerCapture(e.pointerId); rollSeek(e); });
roll.addEventListener("pointermove", (e) => { if (mouseScrub) rollSeek(e); });
roll.addEventListener("pointerup", () => { mouseScrub = false; });

// Upload (button) + drag-and-drop onto the roll.
el("file").addEventListener("change", (e) => { if (e.target.files[0]) loadFile(e.target.files[0]); });
roll.addEventListener("dragover", (e) => { e.preventDefault(); roll.classList.add("drag"); });
roll.addEventListener("dragleave", () => roll.classList.remove("drag"));
roll.addEventListener("drop", (e) => {
  e.preventDefault(); roll.classList.remove("drag");
  const f = e.dataTransfer.files[0];
  if (f) loadFile(f);
});

async function loadFile(file) {
  try {
    await MidiPlayer.unlockAudio();          // in case a MIDI is dropped pre-camera
    const buf = await file.arrayBuffer();
    await player.load(buf, file.name);
    logLine(`🎵 loaded <b>${file.name}</b> · ${player.duration.toFixed(1)}s`);
  } catch (err) {
    console.error("[midi] load failed", err);
    logLine(`⚠ failed to load ${file.name}`);
  }
}

const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

function refreshTransport() {
  el("btnPlay").innerHTML = player.playing
    ? '⏸ Pause' : '▶︎ Play <small>✋</small>';
  el("btnLoop").classList.toggle("on", player.loop);
  el("clip").textContent = player.midi
    ? `${player.name} · §${player.selected + 1}/${player.sections.length}` : "no clip";
}

// Timeline render loop (independent of the camera cadence so the playhead is smooth).
function renderTimeline() {
  el("pos").textContent = `${fmt(player.position)} / ${fmt(player.duration)}`;
  timeline.draw();
  requestAnimationFrame(renderTimeline);
}
requestAnimationFrame(renderTimeline);
refreshTransport();
