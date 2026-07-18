// The gesture vocabulary lives here: turn debounced gesture edges + the live
// held-gesture/hand-X into MidiPlayer operations. Nothing else knows the mapping.

// Discrete ops fire once on the gesture's "enter" edge.
const DISCRETE = {
  PALM: (p) => p.play(),
  FIST: (p) => p.pause(),
};

const TRANSPORT_GESTURES = new Set(["PALM", "FIST", "PINCH"]);

export class GestureRouter {
  constructor(player, timeline, { onAction } = {}) {
    this.player = player;
    this.timeline = timeline;
    this.onAction = onAction || (() => {});
    this._wasScrubbing = false;
    this._resumeAfterScrub = false;
  }

  // Called every frame from main.js.
  //   event: {phase:"enter"|"exit", gesture} | null   (debounced edges)
  //   active: current held gesture string | null      (stabilizer.active)
  //   handX:  free-hand on-screen X in [0,1] | null    (already un-mirrored)
  update({ event, active, handX }) {
    // Mode gestures are owned by main.js and intentionally have no MIDI effect.
    if (event?.phase === "enter" && !TRANSPORT_GESTURES.has(event.gesture)) return;

    if (!this.player.midi) {
      // Still surface edges so the HUD reads, but there's nothing to control yet.
      if (event?.phase === "enter") this.onAction(`${event.gesture} (load a MIDI first)`);
      return;
    }

    // --- continuous: pinch-to-scrub ---
    const scrubbing = active === "PINCH" && handX != null;
    if (scrubbing) {
      if (!this._wasScrubbing) {
        // Pause on grab so the scrub is clean; remember to resume on release.
        this._resumeAfterScrub = this.player.playing;
        if (this.player.playing) this.player.pause();
        this.timeline.scrubbing = true;
        this.onAction("scrub start");
      }
      this.player.seek(this.timeline.xToTime(handX));
    } else if (this._wasScrubbing) {
      this.timeline.scrubbing = false;
      if (this._resumeAfterScrub) this.player.play();
      this.onAction("scrub end");
    }
    this._wasScrubbing = scrubbing;

    // --- discrete edges (ignore PINCH; it's the scrub verb) ---
    if (event?.phase === "enter" && event.gesture !== "PINCH") {
      const op = DISCRETE[event.gesture];
      if (op) {
        op(this.player);
        this.onAction(this._describe(event.gesture));
      }
    }
  }

  _describe(g) {
    const p = this.player;
    switch (g) {
      case "PALM": return "play";
      case "FIST": return "pause";
      default: return g;
    }
  }
}
