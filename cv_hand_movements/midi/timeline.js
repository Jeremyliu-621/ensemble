// Piano-roll timeline: draws notes, section bands, and the live playhead onto a
// canvas, and maps screen-X ↔ time so the gesture layer can scrub.

const TRACK_COLORS = ["#8b7bff", "#4fc3f7", "#46d17a", "#ff9f45", "#f06292", "#ffd54f"];

export class Timeline {
  constructor(canvas, player) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.player = player;
    this.scrubbing = false;
    this._resize();
    window.addEventListener("resize", () => this._resize());
  }

  _resize() {
    const dpr = window.devicePixelRatio || 1;
    this.w = this.canvas.clientWidth || 800;
    this.h = this.canvas.clientHeight || 160;
    this.canvas.width = this.w * dpr;
    this.canvas.height = this.h * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // Normalized screen X in [0,1] → time in seconds.
  xToTime(nx) {
    return Math.max(0, Math.min(1, nx)) * (this.player.duration || 0);
  }
  timeToX(t) {
    return this.player.duration ? (t / this.player.duration) * this.w : 0;
  }

  draw() {
    const { ctx, w, h, player } = this;
    ctx.clearRect(0, 0, w, h);

    // background
    ctx.fillStyle = "#0e0f18";
    ctx.fillRect(0, 0, w, h);

    if (!player.midi) {
      ctx.fillStyle = "#565b73";
      ctx.font = "13px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.fillText("drop a .mid here or use Upload", w / 2, h / 2);
      ctx.textAlign = "left";
      return;
    }

    // section bands (alternating tint, selected highlighted)
    player.sections.forEach((s, i) => {
      const x0 = this.timeToX(s.start), x1 = this.timeToX(s.end);
      if (i === player.selected) {
        ctx.fillStyle = player.loop ? "#46d17a22" : "#8b7bff22";
        ctx.fillRect(x0, 0, x1 - x0, h);
        ctx.strokeStyle = player.loop ? "#46d17a" : "#8b7bff";
        ctx.lineWidth = 1.5;
        ctx.strokeRect(x0 + 0.5, 0.5, x1 - x0 - 1, h - 1);
      } else if (i % 2 === 0) {
        ctx.fillStyle = "#ffffff06";
        ctx.fillRect(x0, 0, x1 - x0, h);
      }
    });

    // notes, scaled to the pitch range present
    const notes = player.notes();
    let lo = 127, hi = 0;
    for (const n of notes) { lo = Math.min(lo, n.midi); hi = Math.max(hi, n.midi); }
    const pad = 6, range = Math.max(1, hi - lo);
    const rowH = (h - pad * 2) / (range + 1);
    for (const n of notes) {
      const x = this.timeToX(n.time);
      const wNote = Math.max(2, this.timeToX(n.time + n.duration) - x);
      const y = pad + (hi - n.midi) * rowH;
      ctx.fillStyle = TRACK_COLORS[n.track % TRACK_COLORS.length];
      ctx.fillRect(x, y, wNote, Math.max(2, rowH - 1));
    }

    // playhead
    const px = this.timeToX(player.position);
    ctx.strokeStyle = this.scrubbing ? "#46d17a" : "#fff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(px, 0);
    ctx.lineTo(px, h);
    ctx.stroke();
  }
}
