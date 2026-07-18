// Jitter + flicker control.
//
//   smoothLandmarks    — per-point EMA so pinch distances don't chatter.
//   GestureStabilizer  — N-of-M confirmation + refractory period, emitting
//                        rising/falling EDGE events so one gesture = one action.
//
// Kept deliberately simple (EMA, not One-Euro) — plenty for discrete labels and
// trivial to reason about at a hackathon.

// Exponential moving average over a hand's 21 landmarks, keyed by hand identity
// (we key on handedness label so left/right don't cross-contaminate).
export function makeSmoother(alpha = 0.5) {
  const state = new Map(); // key -> [{x,y,z}, ...]
  return (key, landmarks) => {
    if (!landmarks) return landmarks;
    const prev = state.get(key);
    if (!prev || prev.length !== landmarks.length) {
      const copy = landmarks.map((p) => ({ x: p.x, y: p.y, z: p.z ?? 0 }));
      state.set(key, copy);
      return copy;
    }
    const out = landmarks.map((p, i) => ({
      x: prev[i].x + alpha * (p.x - prev[i].x),
      y: prev[i].y + alpha * (p.y - prev[i].y),
      z: prev[i].z + alpha * ((p.z ?? 0) - prev[i].z),
    }));
    state.set(key, out);
    return out;
  };
}

// Confirms a gesture only after it has been the top result for `confirmFrames`
// consecutive frames, then emits { phase:"enter", gesture } once. Emits
// { phase:"exit" } when the gesture is lost for `releaseFrames`. A refractory
// window after an enter prevents rapid double-fires.
export class GestureStabilizer {
  constructor({ confirmFrames = 3, releaseFrames = 4, refractoryMs = 250 } = {}) {
    this.confirmFrames = confirmFrames;
    this.releaseFrames = releaseFrames;
    this.refractoryMs = refractoryMs;
    this.active = null;      // currently emitted gesture (or null)
    this.candidate = null;   // gesture accumulating confirmation
    this.candidateCount = 0;
    this.missCount = 0;
    this.lastEnterAt = 0;
  }

  // raw: gesture label string (e.g. "PINCH") or null. Returns an event or null.
  update(raw, now = performance.now()) {
    // --- release path: is the active gesture still present? ---
    if (this.active) {
      if (raw === this.active) {
        this.missCount = 0;
      } else {
        this.missCount++;
        if (this.missCount >= this.releaseFrames) {
          const gone = this.active;
          this.active = null;
          this.candidate = raw;
          this.candidateCount = raw ? 1 : 0;
          this.missCount = 0;
          return { phase: "exit", gesture: gone };
        }
      }
      return null;
    }

    // --- acquire path: accumulate confirmation for a new gesture ---
    if (!raw) { this.candidate = null; this.candidateCount = 0; return null; }
    if (raw === this.candidate) {
      this.candidateCount++;
    } else {
      this.candidate = raw;
      this.candidateCount = 1;
    }
    if (
      this.candidateCount >= this.confirmFrames &&
      now - this.lastEnterAt >= this.refractoryMs
    ) {
      this.active = raw;
      this.lastEnterAt = now;
      this.candidate = null;
      this.candidateCount = 0;
      this.missCount = 0;
      return { phase: "enter", gesture: raw };
    }
    return null;
  }
}
