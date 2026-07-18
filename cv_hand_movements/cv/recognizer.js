// Camera + MediaPipe GestureRecognizer setup and the per-frame driver.
//
// We use GestureRecognizer (not HandLandmarker) because it returns 21 landmarks,
// world landmarks, handedness AND built-in gesture labels in a single pass — so
// Open_Palm / Closed_Fist / etc. come for free and we only hand-roll pinch.
//
// Runs on the laptop: getUserMedia needs a secure context, and localhost qualifies.

import { GestureRecognizer, FilesetResolver }
  from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs";

const MP_VER = "0.10.14";
const WASM = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MP_VER}/wasm`;
const MODEL =
  "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task";

// Reshape the recognizer's parallel-array result into an array of per-hand objects.
function normalize(result) {
  const hands = [];
  const n = result.landmarks?.length ?? 0;
  for (let i = 0; i < n; i++) {
    hands.push({
      landmarks: result.landmarks[i],           // {x,y,z} normalized image coords
      world: result.worldLandmarks?.[i] ?? null, // {x,y,z} metric, hand-centered
      handedness: result.handednesses?.[i]?.[0] ?? null, // {categoryName, score}
      builtin: result.gestures?.[i]?.[0] ?? null,        // {categoryName, score}
    });
  }
  return hands;
}

// Create the recognizer. Resolves to { recognizer, delegate } where `delegate` is
// what actually loaded ("GPU" can silently fall back to CPU on some browsers).
export async function createRecognizer() {
  const fileset = await FilesetResolver.forVisionTasks(WASM);
  let delegate = "GPU";
  let recognizer;
  try {
    recognizer = await GestureRecognizer.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: MODEL, delegate: "GPU" },
      runningMode: "VIDEO",
      numHands: 2,                       // find the left hand even if both are visible
      minHandDetectionConfidence: 0.5,
      minHandPresenceConfidence: 0.5,
      minTrackingConfidence: 0.5,
    });
  } catch (e) {
    console.warn("[cv] GPU delegate failed, retrying on CPU", e);
    delegate = "CPU";
    recognizer = await GestureRecognizer.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: MODEL, delegate: "CPU" },
      runningMode: "VIDEO",
      numHands: 2,
    });
  }
  return { recognizer, delegate };
}

// Open the webcam into `video` and wait until it has real frame dimensions.
export async function startCamera(video) {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
    audio: false,
  });
  video.srcObject = stream;
  await video.play();
  if (!video.videoWidth) {
    await new Promise((r) => video.addEventListener("loadeddata", r, { once: true }));
  }
  return { width: video.videoWidth || 640, height: video.videoHeight || 480 };
}

// Drive one inference per *new* camera frame via requestVideoFrameCallback (falls
// back to rAF where unsupported). `onFrame(hands, dtMs)` gets normalized hands.
export function runLoop(video, recognizer, onFrame) {
  let lastTs = -1;
  let lastWall = performance.now();
  let stopped = false;

  const tick = () => {
    if (stopped) return;
    const now = performance.now();
    // recognizeForVideo requires monotonically increasing timestamps.
    const ts = Math.max(now, lastTs + 1);
    lastTs = ts;
    let hands = [];
    try {
      hands = normalize(recognizer.recognizeForVideo(video, ts));
    } catch (e) {
      console.error("[cv] recognizeForVideo failed", e);
    }
    const dt = now - lastWall;
    lastWall = now;
    onFrame(hands, dt);
    schedule();
  };

  const schedule = () => {
    if (typeof video.requestVideoFrameCallback === "function") {
      video.requestVideoFrameCallback(() => tick());
    } else {
      requestAnimationFrame(() => tick());
    }
  };

  schedule();
  return () => { stopped = true; };
}
