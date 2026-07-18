# CV Hand-Gesture Flow — Research & Recommended Architecture

_Target: detect which hand is free (not holding anything), use that hand for
gesture input, and flag discrete gestures (open palm, pinch, …) that drive
state changes. Browser-based, low latency, high accuracy._

---

## TL;DR / Recommendation

- **Use `@mediapipe/tasks-vision` → `GestureRecognizer`** (the current Tasks API),
  not the legacy `@mediapipe/hands`. It gives you, per detected hand, in one pass:
  **21 landmarks (2D + world/3D)**, **handedness (Left/Right + confidence)**, and
  **built-in gesture categories**. This is everything the flow needs from one model.
- Run it in **`runningMode: "VIDEO"`** so it uses inter-frame tracking (skips the
  expensive palm-detection stage most frames) → much lower latency.
- **"Which hand is free" is NOT something MediaPipe tells you directly** — there is
  no "holding an object" signal. Infer it with a lightweight heuristic (see
  §4). Do **not** reach for a hand-object interaction model — those are research-grade,
  heavy, and overkill for a hackathon-latency budget.
- Built-in gestures cover Open_Palm and Closed_Fist. **Pinch is NOT built in** —
  compute it yourself from landmark distance (thumb tip ↔ index tip). Same technique
  extends to any custom gesture.
- **Debounce every gesture** with an N-frame confirmation + hysteresis before it
  fires a state change, or the UI will flicker.

---

## 1. Library choice

### MediaPipe Tasks Vision (recommended)
- NPM: `@mediapipe/tasks-vision` (the actively maintained package).
- `GestureRecognizer` is a superset of `HandLandmarker`: same landmarks + handedness,
  **plus** gesture classification, for ~no extra cost. Prefer it so you don't have to
  hand-roll open-palm / fist detection.
- Legacy `@mediapipe/hands` was deprecated in March 2023 — avoid for new work.

### Output shape (per frame, per hand)
`GestureRecognizerResult` gives parallel arrays, one element per detected hand:
- `landmarks` — 21 points, normalized image coords `{x, y, z}` (x,y in [0,1]).
- `worldLandmarks` — 21 points in metric 3D relative to hand center (good for
  scale-invariant distance math like pinch).
- `handedness` — `[{ categoryName: "Left"|"Right", score }]`.
- `gestures` — `[{ categoryName, score }]` from the built-in classifier.

### Built-in gesture labels
`None`, `Closed_Fist`, `Open_Palm`, `Pointing_Up`, `Thumb_Up`, `Thumb_Down`,
`Victory`, `ILoveYou`. **Pinch, OK-sign, etc. are not included** → custom (§5).

### 21-landmark index cheat-sheet
- Wrist: `0`
- Thumb: `1,2,3` + tip `4`
- Index: `5,6,7` + tip `8`
- Middle: `9,10,11` + tip `12`
- Ring: `13,14,15` + tip `16`
- Pinky: `17,18,19` + tip `20`

---

## 2. Config for low latency + accuracy

```js
import { FilesetResolver, GestureRecognizer } from "@mediapipe/tasks-vision";

const vision = await FilesetResolver.forVisionTasks(
  // served WASM bundle; can self-host to avoid CDN latency/CSP issues
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision/wasm"
);

const recognizer = await GestureRecognizer.createFromOptions(vision, {
  baseOptions: {
    modelAssetPath: "gesture_recognizer.task",
    delegate: "GPU",          // fastest; falls back to CPU/XNNPACK if unsupported
  },
  runningMode: "VIDEO",       // enables tracking → skips palm detection most frames
  numHands: 2,                // need both to decide which is free
  minHandDetectionConfidence: 0.5,
  minHandPresenceConfidence: 0.5,
  minTrackingConfidence: 0.5,
});
```

Then per video frame:
```js
const nowMs = performance.now();
const result = recognizer.recognizeForVideo(video, nowMs);
```

### Latency levers (biggest → smallest impact)
1. **`runningMode: "VIDEO"`** — tracking avoids re-running palm detection every frame.
   Single biggest latency win vs IMAGE mode.
2. **`delegate: "GPU"`** — fastest path. Caveat: on some browsers it silently falls
   back to XNNPACK/CPU ("Created XNNPACK delegate for CPU" in console). Detect and
   surface this; CPU still runs but slower.
3. **Drive the loop with `requestVideoFrameCallback`** (not `requestAnimationFrame`)
   so you process exactly one inference per *new* camera frame — no wasted or
   duplicated work.
4. **Optionally run inference in a Web Worker** with `OffscreenCanvas` so the model
   never blocks the render/UI thread. MediaPipe's own samples do this. Adds
   complexity — only do it if the main thread janks. For a first cut, main-thread
   VIDEO+GPU is usually smooth (≈30–60 FPS on a laptop).
5. **Downscale the camera feed** (e.g. 640×480). Landmark accuracy holds up well and
   inference gets cheaper.
6. **Self-host the WASM + `.task` model** to kill CDN round-trips and cold starts,
   and to stay CSP-friendly.

Rule of thumb from the MediaPipe team's "dos and don'ts": don't create/destroy the
recognizer repeatedly, don't run in IMAGE mode on a stream, and keep timestamps
monotonic when calling `recognizeForVideo`.

---

## 3. Handedness — read the mirror carefully

- MediaPipe assumes a **mirrored (selfie) input**. A reported `"Left"` corresponds to
  the user's **actual left hand** when the webcam is displayed mirrored (the usual
  case). If you flip the video with CSS `transform: scaleX(-1)` for display, the labels
  still refer to the physical hand — just make sure your on-screen labels match what
  the user perceives, and test with a known hand.
- Handedness has a confidence `score`; when both hands are present and one is partly
  occluded (e.g. gripping a device), its handedness score can drop — useful signal (§4).

---

## 4. "Which hand is free?" — the hard part (no direct signal)

MediaPipe gives you hands, not "is this hand holding something." True hand–object
interaction models (HOIST-Former, egocentric HOI detectors, RGB-D manipulation
trackers) exist but are **research-grade, heavy, and wrong for a low-latency web app**.
Instead, infer the free hand with cheap heuristics. Pick per your setup:

### Option A — Gesture-activity heuristic (recommended, robust, zero extra models)
The hand gripping a phone/instrument is stuck in a **static, fist-like, partially
occluded pose**; the free hand is the one **actively forming recognizable gestures**.
So: **the "control" hand = the hand currently producing a non-`None`, high-confidence
gesture (Open_Palm / Pinch / etc.).**
- Concretely: each frame, for each hand compute a gesture. The hand whose gesture is
  recognized (score above threshold) and is *changing/deliberate* wins control.
- Bonus signals that correlate with "holding something":
  - Lower `handedness.score` / `minHandPresence` (occlusion by the object).
  - Landmarks bunched into a small bounding box (fingers wrapped around a device).
  - Low landmark motion variance over a short window (a gripping hand is still).
- This needs **no object detector** and degrades gracefully.

### Option B — Explicit user assignment (simplest, most reliable)
Let the user declare which hand holds the device once (or infer from a one-time
"hold your phone up" calibration), then treat the *other* handedness label as the
control hand for the session. Trivial, deterministic, great for a demo. Combine with
Option A as a fallback if the declared free hand disappears.

### Option C — Object detection + overlap (heaviest, most literal)
Run a small object detector (e.g. MediaPipe `ObjectDetector` / an SSD-MobileNet /
COCO "cell phone" class) and mark the hand whose bounding box **overlaps the detected
object** as the holding hand; the other is free.
- Pros: literally answers "which hand holds the object."
- Cons: second model = more latency + memory; detector must know the object class;
  overlap logic is fiddly; more failure modes. **Only if A/B aren't good enough.**

**Recommendation:** ship **Option B for reliability + Option A as the live fallback**.
Skip C unless the object is arbitrary and you truly must localize it.

---

## 5. Custom gestures (pinch and friends)

Built-in labels don't include pinch, so compute it from landmarks.

### Pinch = thumb tip ↔ index tip distance
```js
// Use worldLandmarks for scale-invariance (distance in meters, not screen %),
// so the threshold doesn't drift as the hand moves toward/away from the camera.
function pinchDistance(worldLandmarks) {
  const t = worldLandmarks[4];  // thumb tip
  const i = worldLandmarks[8];  // index tip
  return Math.hypot(t.x - i.x, t.y - i.y, t.z - i.z);
}
const isPinch = pinchDistance(hand.worldLandmarks) < PINCH_THRESHOLD;
```
- Typical thresholds people report on **normalized 2D** coords: ~`0.05` (tight) to
  `0.08–0.11` (comfortable). If you use **world** landmarks the number is in meters
  (~0.03 m) — calibrate empirically and expose it as a tunable.
- Normalize by hand size for robustness: divide the pinch distance by a reference span
  (e.g. wrist `0` → middle-finger MCP `9`) so it's invariant to distance from camera
  even in 2D. `ratio = pinchDist / handSpan; isPinch = ratio < 0.35` (tune).

### Pattern for any custom gesture
Most static poses are expressible as **finger-extended / finger-curled** booleans:
a finger is "extended" when its tip is farther from the wrist than its PIP joint (or
the tip is above the PIP in y for an upright hand). Combine:
- Open palm = all five extended (or just trust built-in `Open_Palm`).
- Fist = all curled (or built-in `Closed_Fist`).
- Point = index extended, others curled.
- Pinch = thumb–index distance small (above).
- OK sign, "gun", etc. = analogous distance/extension rules.

For anything you can't express as a rule, MediaPipe Model Maker lets you **train a
custom gesture classifier** on your own labeled samples and drop the resulting `.task`
into the same `GestureRecognizer` — but a hackathon rarely needs this; rules are
faster to build and debug.

---

## 6. Stability — debounce, hysteresis, smoothing

Raw per-frame classification flickers. Never fire a state change off a single frame.

- **N-of-M confirmation:** only accept a new gesture once it's been the top result for
  e.g. 3–5 consecutive frames (or ~100 ms).
- **Hysteresis on thresholds:** use different enter/exit thresholds for pinch (enter at
  ratio < 0.30, exit at > 0.40) so a hand hovering at the boundary doesn't chatter.
- **Landmark smoothing:** apply a One-Euro filter (or a short EMA) to landmark
  positions before computing distances — kills jitter without adding much lag. One-Euro
  is the standard choice because it smooths when slow and stays responsive when fast.
- **Edge-triggered state:** distinguish "gesture is held" from "gesture just started."
  Fire state transitions on the *rising edge* (None→Pinch) so one pinch = one action,
  not one-per-frame. A short refractory period after firing prevents double-triggers.

Suggested per-hand state machine:
```
IDLE ──(gesture G confirmed N frames)──▶ ACTIVE(G) ──emit onEnter(G)──
ACTIVE(G) ──(gesture changes / lost M frames)──▶ IDLE ──emit onExit(G)──
```

---

## 7. Suggested module layout for web2

```
web2/
  cv/
    recognizer.ts     // create + configure GestureRecognizer, VIDEO loop
    handedness.ts     // free-hand inference (Option B assignment + A fallback)
    gestures.ts       // pinch + custom rule detectors on landmarks
    stabilize.ts      // one-euro smoothing, N-of-M debounce, hysteresis, edge events
    state.ts          // maps confirmed gestures → app state changes
  ui/
    CameraView        // <video> + mirrored overlay, draws landmarks + gesture label
    Badge             // shows "PALM" / "PINCH" label per your spec
```

Data flow: `camera frame → recognizeForVideo → smooth landmarks → per-hand gesture →
choose control hand → debounce → edge event → state change → UI label`.

### Minimal end-to-end loop
```js
function loop() {
  const t = performance.now();
  const res = recognizer.recognizeForVideo(video, t);

  const hands = res.landmarks.map((lm, i) => ({
    landmarks: lm,
    world: res.worldLandmarks[i],
    handedness: res.handedness[i][0],        // {categoryName, score}
    builtin: res.gestures[i]?.[0] ?? null,   // {categoryName, score}
  }));

  const control = pickControlHand(hands);    // §4: assignment + activity fallback
  if (control) {
    const g = classifyGesture(control);      // built-in OR custom pinch (§5)
    const event = stabilizer.update(g);      // §6: debounce + edge
    if (event?.type === "enter") setLabel(event.gesture);  // "Open Palm" | "Pinch"
    if (event?.type === "exit")  clearLabel();
  }
  video.requestVideoFrameCallback(loop);     // one inference per camera frame
}
video.requestVideoFrameCallback(loop);
```

---

## 8. Gotchas checklist

- [ ] `runningMode` must be `"VIDEO"` for the stream — IMAGE mode re-runs palm
      detection every frame and is much slower.
- [ ] Keep timestamps passed to `recognizeForVideo` **monotonically increasing**.
- [ ] GPU delegate may silently fall back to CPU — log which delegate you actually got.
- [ ] Mirror handling: decide once whether the feed is mirrored and make labels match
      the user's real hand.
- [ ] `numHands: 2` or you can't compare hands to find the free one.
- [ ] Don't recreate the recognizer per frame; create once, reuse.
- [ ] Self-host WASM + model if a CDN is slow, blocked, or CSP-restricted.
- [ ] Getty camera permission + `playsInline` for the `<video>`; wait for
      `loadeddata` before the first inference.

---

## Sources

- [Gesture recognition guide for Web — MediaPipe / Google AI Edge](https://ai.google.dev/edge/mediapipe/solutions/vision/gesture_recognizer/web_js)
- [Gesture recognizer task guide](https://ai.google.dev/edge/mediapipe/solutions/vision/gesture_recognizer)
- [Hand landmarks detection guide (landmarks, handedness, world coords, VIDEO tracking)](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker)
- [@mediapipe/tasks-vision on npm](https://www.npmjs.com/package/@mediapipe/tasks-vision)
- [7 dos and don'ts of using ML on the web with MediaPipe — Google Developers Blog](https://developers.googleblog.com/7-dos-and-donts-of-using-ml-on-the-web-with-mediapipe/)
- [Motion Controls In The Browser — Smashing Magazine (pinch distance + debounce)](https://www.smashingmagazine.com/2022/10/motion-controls-browser/)
- [Practical gesture detection with MediaPipe in your browser — Damien Contreras](https://medium.com/@c-damien/practical-gesture-detection-with-mediapipe-in-your-browser-283c7c1f09f0)
- [MediaPipe Hand Tracking & Face Detection in JavaScript — Sander de Snaijer](https://www.sanderdesnaijer.com/blog/mediapipe-hand-face-tracking)
- [MediaPipe samples web — WebWorker + OffscreenCanvas pattern](https://github.com/google-ai-edge/mediapipe-samples-web)
- [The real-time hand and object recognition for virtual interaction (PMC)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11232578/)
- [HOIST-Former: Hand-held Objects Identification/Segmentation/Tracking (why HOI is heavy)](https://arxiv.org/pdf/2404.13819)
