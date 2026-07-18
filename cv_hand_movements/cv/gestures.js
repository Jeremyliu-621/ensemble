// Gesture classification on a single hand.
//
// Open palm and closed fist come from MediaPipe's GestureRecognizer. Pinch and the
// exact one/two/three-finger mode poses are classified from landmarks here.

// 21-landmark indices.
export const L = {
  WRIST: 0,
  THUMB_MCP: 2, THUMB_TIP: 4,
  INDEX_MCP: 5, INDEX_PIP: 6, INDEX_TIP: 8,
  MIDDLE_MCP: 9, MIDDLE_PIP: 10, MIDDLE_TIP: 12,
  RING_MCP: 13, RING_PIP: 14, RING_TIP: 16,
  PINKY_MCP: 17, PINKY_PIP: 18, PINKY_TIP: 20,
};

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

// Hand span = wrist → middle-finger MCP. Used to normalize distances so thresholds
// are invariant to how close the hand is to the camera.
export function handSpan(lm) {
  return Math.max(dist(lm[L.WRIST], lm[L.MIDDLE_MCP]), 1e-4);
}

// A finger is "extended" when its tip is farther from the wrist than its PIP joint.
function extended(lm, tip, pip) {
  return dist(lm[L.WRIST], lm[tip]) > dist(lm[L.WRIST], lm[pip]) * 1.1;
}

// Booleans + count of extended fingers (thumb handled separately, less reliable).
export function fingerState(lm) {
  const index = extended(lm, L.INDEX_TIP, L.INDEX_PIP);
  const middle = extended(lm, L.MIDDLE_TIP, L.MIDDLE_PIP);
  const ring = extended(lm, L.RING_TIP, L.RING_PIP);
  const pinky = extended(lm, L.PINKY_TIP, L.PINKY_PIP);
  // Thumb: distance of tip from the pinky MCP relative to span.
  const thumb = dist(lm[L.THUMB_TIP], lm[L.PINKY_MCP]) / handSpan(lm) > 0.6;
  const count = [index, middle, ring, pinky].filter(Boolean).length;
  return { thumb, index, middle, ring, pinky, count };
}

// Only the canonical adjacent-finger poses switch modes. The thumb is ignored so
// its less reliable extension estimate cannot interrupt a deliberate mode change.
export function modeGestureFromFingerState({ index, middle, ring, pinky }) {
  if (index && !middle && !ring && !pinky) return "ONE_FINGER";
  if (index && middle && !ring && !pinky) return "TWO_FINGERS";
  if (index && middle && ring && !pinky) return "THREE_FINGERS";
  return null;
}

// Normalized thumb–index gap. Small = pinching.
export function pinchRatio(lm) {
  return dist(lm[L.THUMB_TIP], lm[L.INDEX_TIP]) / handSpan(lm);
}

// Per-hand pinch tracker with hysteresis. Returns true while pinched.
export function makePinchTracker({ on = 0.35, off = 0.5 } = {}) {
  const state = new Map(); // key -> bool
  return (key, lm) => {
    const r = pinchRatio(lm);
    const was = state.get(key) ?? false;
    const now = was ? r < off : r < on;
    state.set(key, now);
    return { pinched: now, ratio: r };
  };
}

// Map built-in MediaPipe labels to our display names.
const BUILTIN = {
  Open_Palm: "PALM",
  Closed_Fist: "FIST",
};

// Priority is PINCH, then exact mode poses, then supported MediaPipe gestures.
// This ensures Pointing_Up and Victory cannot steal the one/two-finger mode poses.
// Returns { gesture, score } with gesture possibly null.
export function classifyGesture(hand, pinchTracker, key) {
  const lm = hand.landmarks;
  if (!lm) return { gesture: null, score: 0 };

  const { pinched, ratio } = pinchTracker(key, lm);
  if (pinched) {
    // Confidence: how far below the "on" threshold we are (clamped 0..1).
    return { gesture: "PINCH", score: Math.min(1, Math.max(0.5, 1 - ratio)) };
  }

  const modeGesture = modeGestureFromFingerState(fingerState(lm));
  if (modeGesture) {
    return { gesture: modeGesture, score: hand.builtin?.score ?? 1 };
  }

  const b = hand.builtin;
  if (b && b.categoryName !== "None" && BUILTIN[b.categoryName]) {
    return { gesture: BUILTIN[b.categoryName], score: b.score };
  }
  return { gesture: null, score: 0 };
}
