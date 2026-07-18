// Editable canvas piano-roll — the heart of the Control Room.
//
// A self-contained, framework-free multi-track piano roll built on the patterns
// from mature open-source editors (signal's mode-based tool dispatch, LMMS's
// modifier idioms, BeepBox's audition-on-draw), with the layered-canvas render
// approach: a `main` canvas holds the static-ish grid + notes (redrawn only when
// the model, scroll, or zoom changes) and an `overlay` canvas holds the moving
// playhead + marquee (redrawn every rAF, so a moving playhead never repaints the
// notes). Notes are stored authoritatively in 16th-note ticks + MIDI number and
// pixels are derived at draw time.
//
// Model (owned here, mutated via edits, serialised out for the engine):
//   track = { id, name, instrument, isDrum, isMelody, color, muted, solo,
//             notes: [ { id, start, dur, pitch, vel } ] }   // start/dur in 16ths
//
// Callbacks: onChange() after any edit · onAudition(pitch, vel) to preview a note.
// The caller supplies a playhead source via setPlayheadSource(() => pos16|null).

const GUTTER_W = 54;   // left piano-key column (px)
const RULER_H = 26;    // top bar:beat ruler (px)
const VEL_H = 66;      // bottom velocity lane (px)
const RESIZE_PX = 7;   // right-edge grab zone for resizing
const PITCH_HI = 108, PITCH_LO = 21;   // full editable range (piano)
const BLACK = new Set([1, 3, 6, 8, 10]);   // black-key pitch classes
const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

let _uid = 1;
const uid = () => "e" + _uid++;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const freqOf = (m) => 440 * Math.pow(2, (m - 69) / 12);

export class PianoRoll {
  constructor(container) {
    this.el = container;
    this.el.style.position = "relative";
    this.el.style.touchAction = "none";
    this.el.tabIndex = 0;                    // focusable for key handling

    this.main = this._mkCanvas(1);
    this.overlay = this._mkCanvas(2);
    this.overlay.style.pointerEvents = "none";

    // model
    this.tracks = [];
    this.activeId = null;
    this.sel = new Set();                    // selected note ids (active track)

    // view
    this.pxPer16 = 22;                       // horizontal zoom (px per 16th)
    this.rowH = 12;                          // vertical zoom (px per semitone)
    this.scrollX = 0;
    this.scrollY = 0;
    this.snap = 1;                           // grid division in 16ths (1=1/16,2=1/8,4=1/4,16=bar)
    this.tool = "pencil";
    this.follow = true;                      // playhead auto-scroll while playing
    this.lastLen = 4;                        // remembered note length (16ths) = a beat

    // interaction state
    this._drag = null;                       // active gesture descriptor
    this._hoverCursor = "default";
    this._marquee = null;                    // {x0,y0,x1,y1} in client px
    this._undo = [];
    this._redo = [];
    this._playheadSrc = () => null;
    this._audio = null;

    // callbacks
    this.onChange = () => {};
    this.onAudition = null;                  // if null, uses the built-in blip

    this._need = true;
    this._bind();
    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(this.el);
    this._resize();
    requestAnimationFrame(() => this._frame());
  }

  _mkCanvas(z) {
    const c = document.createElement("canvas");
    Object.assign(c.style, { position: "absolute", inset: "0", width: "100%", height: "100%", zIndex: z });
    this.el.appendChild(c);
    return c;
  }

  // ---- public API ----
  load({ tracks, activeId } = {}) {
    this.tracks = (tracks || []).map((t) => ({
      id: t.id || uid(), name: t.name || t.instrument || "track",
      instrument: t.instrument || "synth", isDrum: !!t.isDrum, isMelody: !!t.isMelody,
      color: t.color, muted: !!t.muted, solo: !!t.solo,
      notes: (t.notes || []).map((n) => ({ id: n.id || uid(), start: n.start, dur: n.dur, pitch: n.pitch, vel: n.vel ?? 0.8 })),
    }));
    this.activeId = activeId || (this.tracks[0] && this.tracks[0].id) || null;
    this.sel.clear();
    this._undo = []; this._redo = [];
    // center the view on the notes' pitch range
    const ns = this.tracks.flatMap((t) => t.notes);
    if (ns.length) {
      const mid = ns.reduce((s, n) => s + n.pitch, 0) / ns.length;
      this.scrollY = clamp((PITCH_HI - mid) * this.rowH - this._rollH() / 2, 0, this._maxScrollY());
    }
    this._need = true;
  }

  serialize() {
    // Muted/solo resolved here: a muted track (or a non-soloed track while any
    // track is soloed) is sent with no notes, so it goes silent without losing
    // its notes in the editor.
    const anySolo = this.tracks.some((t) => t.solo);
    return {
      tracks: this.tracks.map((t) => {
        const silent = t.muted || (anySolo && !t.solo);
        return {
          instrument: t.instrument, is_drum: t.isDrum, is_melody: t.isMelody, name: t.name,
          notes: silent ? [] : t.notes.map((n) => {
            const bar = Math.floor(n.start / 16), on = n.start % 16;
            return [bar, on, Math.min(n.dur, 16 - on), n.pitch, +n.vel.toFixed(2)];
          }),
        };
      }),
    };
  }

  redraw() { this._need = true; }
  setActive(id) { this.activeId = id; this.sel.clear(); this._need = true; }
  setTool(t) { this.tool = t; this._need = true; }
  setSnap(s) { this.snap = s; this._need = true; }
  setFollow(v) { this.follow = v; }
  setPlayheadSource(fn) { this._playheadSrc = fn || (() => null); }
  active() { return this.tracks.find((t) => t.id === this.activeId) || null; }
  addTrack(t) { const nt = { id: uid(), notes: [], muted: false, solo: false, ...t }; this.tracks.push(nt); this.activeId = nt.id; this._need = true; return nt; }
  removeTrack(id) {
    this.tracks = this.tracks.filter((t) => t.id !== id);
    if (this.activeId === id) this.activeId = this.tracks[0]?.id || null;
    this._commit();
  }
  zoom(dx, dy) { this.pxPer16 = clamp(this.pxPer16 * dx, 8, 64); this.rowH = clamp(this.rowH * dy, 7, 24); this._clampScroll(); this._need = true; }

  // ---- geometry ----
  _rollW() { return this.main.clientWidth - GUTTER_W; }
  _rollH() { return this.main.clientHeight - RULER_H - VEL_H; }
  _songLen16() {
    let end = 16 * 4;                          // always show at least 4 bars
    for (const t of this.tracks) for (const n of t.notes) end = Math.max(end, n.start + n.dur);
    return Math.ceil(end / 16) * 16 + 16 * 2;  // + a couple of empty bars to extend into
  }
  _nBars() {                                    // the looped length (last used bar + 1)
    let end = 16;
    for (const t of this.tracks) for (const n of t.notes) end = Math.max(end, n.start + n.dur);
    return Math.max(1, Math.ceil(end / 16));
  }
  _maxScrollX() { return Math.max(0, this._songLen16() * this.pxPer16 - this._rollW()); }
  _maxScrollY() { return Math.max(0, (PITCH_HI - PITCH_LO + 1) * this.rowH - this._rollH()); }
  _clampScroll() { this.scrollX = clamp(this.scrollX, 0, this._maxScrollX()); this.scrollY = clamp(this.scrollY, 0, this._maxScrollY()); }
  _xOf(tick) { return GUTTER_W + tick * this.pxPer16 - this.scrollX; }
  _yOf(pitch) { return RULER_H + (PITCH_HI - pitch) * this.rowH - this.scrollY; }
  _tickAt(px) { return (px - GUTTER_W + this.scrollX) / this.pxPer16; }
  _pitchAt(py) { return PITCH_HI - Math.floor((py - RULER_H + this.scrollY) / this.rowH); }
  _snapTick(tick, free) { const s = free ? 1 : this.snap; return Math.round(tick / s) * s; }
  _snapFloor(tick, free) { const s = free ? 1 : this.snap; return Math.floor(tick / s) * s; }

  // ---- hit testing (active track only) ----
  _hit(px, py) {
    const t = this.active(); if (!t) return null;
    const pitch = this._pitchAt(py), tick = this._tickAt(px);
    // iterate topmost-last so later (drawn on top) wins
    for (let i = t.notes.length - 1; i >= 0; i--) {
      const n = t.notes[i];
      if (n.pitch !== pitch) continue;
      if (tick >= n.start && tick <= n.start + n.dur) {
        const edge = px >= this._xOf(n.start + n.dur) - RESIZE_PX;
        return { note: n, edge };
      }
    }
    return null;
  }

  // ---- undo / redo ----
  _snapshot() { return this.tracks.map((t) => ({ ...t, notes: t.notes.map((n) => ({ ...n })) })); }
  _pushUndo() { this._undo.push(this._snapshot()); if (this._undo.length > 60) this._undo.shift(); this._redo = []; }
  _restore(snap) { this.tracks = snap.map((t) => ({ ...t, notes: t.notes.map((n) => ({ ...n })) })); this._need = true; }
  undo() { if (!this._undo.length) return; this._redo.push(this._snapshot()); this._restore(this._undo.pop()); this._changed(); }
  redo() { if (!this._redo.length) return; this._undo.push(this._snapshot()); this._restore(this._redo.pop()); this._changed(); }
  _commit() { this._pushUndo(); this._changed(); }
  _changed() { this._need = true; this.onChange(); }

  // ---- audition ----
  _blip(pitch, vel = 0.7) {
    if (this.onAudition) return this.onAudition(pitch, vel);
    try {
      if (!this._audio) this._audio = new (window.AudioContext || window.webkitAudioContext)();
      const ctx = this._audio; if (ctx.state === "suspended") ctx.resume();
      const t = ctx.currentTime, o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "triangle"; o.frequency.value = freqOf(pitch);
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.18 * vel + 0.02, t + 0.006);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
      o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + 0.24);
    } catch { /* audition is best-effort */ }
  }

  // ---- events ----
  _bind() {
    this.el.addEventListener("mousedown", (e) => this._down(e));
    window.addEventListener("mousemove", (e) => this._move(e));
    window.addEventListener("mouseup", (e) => this._up(e));
    this.el.addEventListener("contextmenu", (e) => e.preventDefault());
    this.el.addEventListener("wheel", (e) => this._wheel(e), { passive: false });
    this.el.addEventListener("keydown", (e) => this._key(e));
    this.el.addEventListener("mousemove", (e) => this._hover(e));
  }

  _rel(e) { const r = this.el.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
  _inRoll(p) { return p.x >= GUTTER_W && p.y >= RULER_H && p.y <= this.main.clientHeight - VEL_H; }
  _inVel(p) { return p.x >= GUTTER_W && p.y > this.main.clientHeight - VEL_H; }
  _inRuler(p) { return p.y < RULER_H && p.x >= GUTTER_W; }
  _inKeys(p) { return p.x < GUTTER_W && p.y >= RULER_H && p.y <= this.main.clientHeight - VEL_H; }

  _hover(e) {
    const p = this._rel(e);
    let cur = "default";
    if (this._inRoll(p)) {
      const h = this._hit(p.x, p.y);
      cur = h ? (h.edge ? "ew-resize" : "move") : (this.tool === "pencil" ? "crosshair" : "cell");
    } else if (this._inVel(p)) cur = "ns-resize";
    else if (this._inRuler(p)) cur = "pointer";
    else if (this._inKeys(p)) cur = "pointer";
    if (cur !== this._hoverCursor) { this._hoverCursor = cur; this.el.style.cursor = cur; }
  }

  _down(e) {
    this.el.focus();
    const p = this._rel(e);
    const free = e.altKey;

    if (this._inKeys(p)) { this._blip(clamp(this._pitchAt(p.y), PITCH_LO, PITCH_HI)); return; }
    if (this._inRuler(p)) { this._drag = { kind: "pan-none" }; return; }   // (seek is engine-driven; ignore)
    if (this._inVel(p)) { this._pushUndo(); this._drag = { kind: "vel" }; this._paintVel(p); return; }
    if (!this._inRoll(p)) return;

    const t = this.active(); if (!t) return;
    const hit = this._hit(p.x, p.y);
    const selectMode = this.tool === "select" || e.ctrlKey || e.metaKey;

    // right-click deletes
    if (e.button === 2) { if (hit) { this._pushUndo(); t.notes = t.notes.filter((n) => n !== hit.note); this.sel.delete(hit.note.id); this._changed(); } return; }
    if (e.button !== 0) return;

    if (hit) {
      if (!this.sel.has(hit.note.id) && !e.shiftKey) this.sel = new Set([hit.note.id]);
      else if (e.shiftKey) this.sel.has(hit.note.id) ? this.sel.delete(hit.note.id) : this.sel.add(hit.note.id);
      this._pushUndo();
      const ids = [...this.sel];
      const base = ids.map((id) => { const n = t.notes.find((x) => x.id === id); return { id, start: n.start, pitch: n.pitch, dur: n.dur }; });
      this._drag = { kind: hit.edge ? "resize" : "move", anchor: hit.note, base, startTick: this._tickAt(p.x), startPitch: this._pitchAt(p.y), free, moved: false };
      this._need = true;
      return;
    }

    if (selectMode) { this.sel = e.shiftKey ? this.sel : new Set(); this._marquee = { x0: p.x, y0: p.y, x1: p.x, y1: p.y, add: e.shiftKey, before: new Set(this.sel) }; return; }

    // pencil on empty grid: create a note and drag its right edge to size it
    const start = clamp(this._snapFloor(this._tickAt(p.x), free), 0, 100000);
    const pitch = clamp(this._pitchAt(p.y), PITCH_LO, PITCH_HI);
    this._pushUndo();
    const note = { id: uid(), start, dur: this.lastLen, pitch, vel: 0.8 };
    t.notes.push(note);
    this.sel = new Set([note.id]);
    this._blip(pitch, note.vel);
    this._drag = { kind: "resize", anchor: note, base: [{ id: note.id, start, pitch, dur: this.lastLen }], startTick: this._tickAt(p.x), startPitch: pitch, free, created: true, moved: false };
    this._changed();
  }

  _move(e) {
    if (!this._drag && !this._marquee) return;
    const p = this._rel(e);

    if (this._marquee) { this._marquee.x1 = p.x; this._marquee.y1 = p.y; this._applyMarquee(); return; }
    const d = this._drag;
    if (d.kind === "vel") { this._paintVel(p); return; }
    if (d.kind === "pan-none") return;
    const t = this.active(); if (!t) return;

    if (d.kind === "move") {
      const dt = this._snapTick(this._tickAt(p.x) - d.startTick, d.free);
      const dp = Math.round((this._pitchAt(p.y) - d.startPitch));   // pitch already integral
      if (dt || dp) d.moved = true;
      let minStart = Infinity, changedPitch = null;
      for (const b of d.base) {
        const n = t.notes.find((x) => x.id === b.id); if (!n) continue;
        n.start = Math.max(0, b.start + dt);
        const np = clamp(b.pitch + dp, PITCH_LO, PITCH_HI);
        if (np !== n.pitch) changedPitch = np;
        n.pitch = np;
        minStart = Math.min(minStart, n.start);
      }
      if (changedPitch != null && d._lastAudP !== changedPitch) { d._lastAudP = changedPitch; this._blip(changedPitch, 0.6); }
      this._need = true;
    } else if (d.kind === "resize") {
      const b = d.base[0];
      const raw = this._tickAt(p.x);
      let end = this._snapTick(raw, d.free);
      if (end <= b.start) end = b.start + (d.free ? 1 : this.snap);
      const n = t.notes.find((x) => x.id === b.id); if (n) { n.dur = Math.max(1, end - b.start); this.lastLen = n.dur; d.moved = true; }
      this._need = true;
    }
  }

  _up(e) {
    if (this._marquee) { this._marquee = null; this._need = true; return; }
    const d = this._drag; this._drag = null;
    if (!d) return;
    if (d.kind === "pan-none") return;
    if (d.kind === "vel") { this._changed(); return; }
    // a pure click that created a note, or any real edit -> commit + push
    if (d.moved || d.created) this._changed();
    else { this._undo.pop(); this._need = true; }   // no-op move: discard the snapshot
  }

  _applyMarquee() {
    const m = this._marquee, t = this.active(); if (!t) return;
    const x0 = Math.min(m.x0, m.x1), x1 = Math.max(m.x0, m.x1), y0 = Math.min(m.y0, m.y1), y1 = Math.max(m.y0, m.y1);
    const sel = new Set(m.before);
    for (const n of t.notes) {
      const nx0 = this._xOf(n.start), nx1 = this._xOf(n.start + n.dur), ny0 = this._yOf(n.pitch), ny1 = ny0 + this.rowH;
      if (nx1 >= x0 && nx0 <= x1 && ny1 >= y0 && ny0 <= y1) sel.add(n.id);
    }
    this.sel = sel; this._need = true;
  }

  _paintVel(p) {
    const t = this.active(); if (!t) return;
    const tick = this._tickAt(p.x);
    const laneTop = this.main.clientHeight - VEL_H + 8, laneBot = this.main.clientHeight - 8;
    const v = clamp((laneBot - p.y) / (laneBot - laneTop), 0.05, 1);
    // affect the selection if the note under x is selected, else the nearest note at x
    let best = null, bestD = 1e9;
    for (const n of t.notes) {
      if (tick >= n.start && tick <= n.start + n.dur) { const d = Math.abs(this._xOf(n.start) - p.x); if (d < bestD) { bestD = d; best = n; } }
    }
    if (!best) return;
    const targets = this.sel.has(best.id) ? t.notes.filter((n) => this.sel.has(n.id)) : [best];
    for (const n of targets) n.vel = v;
    this._need = true;
  }

  _wheel(e) {
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) {                 // zoom toward cursor
      const p = this._rel(e);
      const tickAtCursor = this._tickAt(p.x);
      const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      this.pxPer16 = clamp(this.pxPer16 * f, 8, 64);
      this.scrollX = clamp((tickAtCursor * this.pxPer16) - (p.x - GUTTER_W), 0, this._maxScrollX());
      this._need = true;
    } else if (e.shiftKey) { this.scrollX = clamp(this.scrollX + e.deltaY, 0, this._maxScrollX()); this._need = true; }
    else { this.scrollY = clamp(this.scrollY + e.deltaY, 0, this._maxScrollY()); this._need = true; }
  }

  _key(e) {
    const t = this.active(); if (!t) return;
    const mod = e.ctrlKey || e.metaKey;
    const selNotes = () => t.notes.filter((n) => this.sel.has(n.id));
    if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? this.redo() : this.undo(); return; }
    if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); this.redo(); return; }
    if (mod && e.key.toLowerCase() === "a") { e.preventDefault(); this.sel = new Set(t.notes.map((n) => n.id)); this._need = true; return; }
    if (mod && e.key.toLowerCase() === "d") { e.preventDefault(); this._duplicate(); return; }
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); if (this.sel.size) { this._pushUndo(); t.notes = t.notes.filter((n) => !this.sel.has(n.id)); this.sel.clear(); this._changed(); } return; }
    if (e.key === "1") { this.setTool("pencil"); return; }
    if (e.key === "2") { this.setTool("select"); return; }
    if (e.key.toLowerCase() === "q") { if (this.sel.size) { this._pushUndo(); for (const n of selNotes()) n.start = Math.round(n.start / this.snap) * this.snap; this._changed(); } return; }
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) {
      e.preventDefault(); if (!this.sel.size) return; this._pushUndo();
      const ns = selNotes();
      if (e.key === "ArrowLeft") for (const n of ns) n.start = Math.max(0, n.start - this.snap);
      if (e.key === "ArrowRight") for (const n of ns) n.start += this.snap;
      if (e.key === "ArrowUp") { const s = mod ? 12 : 1; for (const n of ns) n.pitch = clamp(n.pitch + s, PITCH_LO, PITCH_HI); this._blip(ns[0].pitch, 0.6); }
      if (e.key === "ArrowDown") { const s = mod ? 12 : 1; for (const n of ns) n.pitch = clamp(n.pitch - s, PITCH_LO, PITCH_HI); this._blip(ns[0].pitch, 0.6); }
      this._changed();
    }
  }

  _duplicate() {
    const t = this.active(); if (!t || !this.sel.size) return;
    this._pushUndo();
    const ns = t.notes.filter((n) => this.sel.has(n.id));
    const span = Math.max(...ns.map((n) => n.start + n.dur)) - Math.min(...ns.map((n) => n.start));
    const shift = Math.max(this.snap, Math.ceil(span / this.snap) * this.snap);
    const copies = ns.map((n) => ({ id: uid(), start: n.start + shift, dur: n.dur, pitch: n.pitch, vel: n.vel }));
    t.notes.push(...copies);
    this.sel = new Set(copies.map((n) => n.id));
    this._changed();
  }

  // ---- rendering ----
  _resize() {
    const dpr = window.devicePixelRatio || 1;
    for (const c of [this.main, this.overlay]) {
      const w = this.el.clientWidth, h = this.el.clientHeight;
      c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
      c.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    this._clampScroll();
    this._need = true;
  }

  _frame() {
    if (this._need) { this._drawMain(); this._need = false; }
    this._drawOverlay();
    requestAnimationFrame(() => this._frame());
  }

  _drawMain() {
    const ctx = this.main.getContext("2d");
    const W = this.main.clientWidth, H = this.main.clientHeight;
    const rollBot = H - VEL_H;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#faf4e6"; ctx.fillRect(0, 0, W, H);

    // --- roll body (clipped) ---
    ctx.save();
    ctx.beginPath(); ctx.rect(GUTTER_W, RULER_H, W - GUTTER_W, rollBot - RULER_H); ctx.clip();

    // key rows
    const pTop = PITCH_HI - Math.floor(this.scrollY / this.rowH);
    for (let p = pTop; this._yOf(p) < rollBot; p--) {
      if (p < PITCH_LO) break;
      const y = this._yOf(p);
      ctx.fillStyle = BLACK.has(((p % 12) + 12) % 12) ? "#eadfc6" : "#f6efdd";
      ctx.fillRect(GUTTER_W, y, W - GUTTER_W, this.rowH);
      if (p % 12 === 0) { ctx.strokeStyle = "rgba(54,38,25,.18)"; ctx.beginPath(); ctx.moveTo(GUTTER_W, Math.round(y) + 0.5); ctx.lineTo(W, Math.round(y) + 0.5); ctx.stroke(); }
    }

    // vertical grid: 16th, beat, bar
    const len = this._songLen16();
    for (let tick = Math.floor(this.scrollX / this.pxPer16); tick <= len; tick++) {
      const x = this._xOf(tick); if (x > W) break; if (x < GUTTER_W) continue;
      const isBar = tick % 16 === 0, isBeat = tick % 4 === 0;
      if (!isBeat && this.pxPer16 < 12) continue;   // hide 16th lines when zoomed out
      ctx.strokeStyle = isBar ? "rgba(54,38,25,.35)" : isBeat ? "rgba(54,38,25,.16)" : "rgba(54,38,25,.07)";
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, RULER_H); ctx.lineTo(Math.round(x) + 0.5, rollBot); ctx.stroke();
    }
    // loop-end marker
    const lx = this._xOf(this._nBars() * 16);
    if (lx > GUTTER_W && lx < W) { ctx.strokeStyle = "rgba(217,83,74,.6)"; ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(lx, RULER_H); ctx.lineTo(lx, rollBot); ctx.stroke(); ctx.setLineDash([]); }

    // notes: ghost inactive tracks first, then the active track
    for (const t of this.tracks) if (t.id !== this.activeId) this._drawNotes(ctx, t, 0.26, rollBot);
    const act = this.active(); if (act) this._drawNotes(ctx, act, 1, rollBot);
    ctx.restore();

    this._drawRuler(ctx, W, len);
    this._drawKeys(ctx, rollBot);
    this._drawVelLane(ctx, W, H);
  }

  _drawNotes(ctx, t, alpha, rollBot) {
    const minTick = this.scrollX / this.pxPer16 - 8, maxTick = (this.scrollX + this._rollW()) / this.pxPer16 + 1;
    const col = t.color || "#8a6d4f";
    for (const n of t.notes) {
      if (n.start + n.dur < minTick || n.start > maxTick) continue;   // cull
      const x = this._xOf(n.start), y = this._yOf(n.pitch), w = Math.max(2, n.dur * this.pxPer16 - 1), h = this.rowH - 1;
      if (y + h < RULER_H || y > rollBot) continue;
      const selected = alpha === 1 && this.sel.has(n.id);
      ctx.globalAlpha = alpha * (0.35 + 0.65 * clamp(n.vel, 0, 1));
      ctx.fillStyle = col;
      this._roundRect(ctx, x, y, w, h, 2); ctx.fill();
      ctx.globalAlpha = alpha;
      if (selected) { ctx.strokeStyle = "#362619"; ctx.lineWidth = 2; this._roundRect(ctx, x + 0.5, y + 0.5, w - 1, h - 1, 2); ctx.stroke(); }
      else { ctx.strokeStyle = "rgba(54,38,25,.55)"; ctx.lineWidth = 1; ctx.strokeRect(Math.round(x) + 0.5, Math.round(y) + 0.5, w, h); }
    }
    ctx.globalAlpha = 1;
  }

  _drawRuler(ctx, W, len) {
    ctx.fillStyle = "#e7dcc4"; ctx.fillRect(GUTTER_W, 0, W - GUTTER_W, RULER_H);
    ctx.fillStyle = "#7a6650"; ctx.font = "600 11px ui-monospace, monospace"; ctx.textBaseline = "middle";
    for (let bar = Math.floor(this.scrollX / this.pxPer16 / 16); bar * 16 <= len; bar++) {
      const x = this._xOf(bar * 16); if (x > W) break; if (x < GUTTER_W - 20) continue;
      ctx.strokeStyle = "rgba(54,38,25,.45)"; ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, RULER_H - 8); ctx.lineTo(Math.round(x) + 0.5, RULER_H); ctx.stroke();
      if (x >= GUTTER_W) ctx.fillText(String(bar + 1), x + 4, RULER_H / 2);
    }
    ctx.strokeStyle = "rgba(54,38,25,.3)"; ctx.beginPath(); ctx.moveTo(GUTTER_W, RULER_H + 0.5); ctx.lineTo(W, RULER_H + 0.5); ctx.stroke();
  }

  _drawKeys(ctx, rollBot) {
    ctx.fillStyle = "#e7dcc4"; ctx.fillRect(0, RULER_H, GUTTER_W, rollBot - RULER_H);
    ctx.save(); ctx.beginPath(); ctx.rect(0, RULER_H, GUTTER_W, rollBot - RULER_H); ctx.clip();
    const pTop = PITCH_HI - Math.floor(this.scrollY / this.rowH);
    ctx.font = "600 9px ui-monospace, monospace"; ctx.textBaseline = "middle";
    for (let p = pTop; this._yOf(p) < rollBot; p--) {
      if (p < PITCH_LO) break;
      const y = this._yOf(p), black = BLACK.has(((p % 12) + 12) % 12);
      ctx.fillStyle = black ? "#453425" : "#fdfaf2"; ctx.fillRect(0, y, GUTTER_W - 1, this.rowH - 0.5);
      if (p % 12 === 0) { ctx.fillStyle = "#7a6650"; ctx.fillText(`C${p / 12 - 1}`, 6, y + this.rowH / 2); }
    }
    ctx.restore();
    ctx.strokeStyle = "rgba(54,38,25,.45)"; ctx.beginPath(); ctx.moveTo(GUTTER_W + 0.5, RULER_H); ctx.lineTo(GUTTER_W + 0.5, rollBot); ctx.stroke();
  }

  _drawVelLane(ctx, W, H) {
    const top = H - VEL_H, laneTop = top + 8, laneBot = H - 8;
    ctx.fillStyle = "#e7dcc4"; ctx.fillRect(0, top, W, VEL_H);
    ctx.strokeStyle = "rgba(54,38,25,.45)"; ctx.beginPath(); ctx.moveTo(0, top + 0.5); ctx.lineTo(W, top + 0.5); ctx.stroke();
    ctx.fillStyle = "#7a6650"; ctx.font = "600 9px ui-monospace, monospace"; ctx.textBaseline = "top"; ctx.fillText("VELOCITY", 6, top + 5);
    const t = this.active(); if (!t) return;
    ctx.save(); ctx.beginPath(); ctx.rect(GUTTER_W, top, W - GUTTER_W, VEL_H); ctx.clip();
    for (const n of t.notes) {
      const x = this._xOf(n.start); if (x < GUTTER_W - 4 || x > W) continue;
      const h = (laneBot - laneTop) * clamp(n.vel, 0, 1);
      ctx.globalAlpha = this.sel.has(n.id) ? 1 : 0.7;
      ctx.fillStyle = this.sel.has(n.id) ? "#362619" : (t.color || "#8a6d4f");
      ctx.fillRect(Math.round(x), laneBot - h, 3, h);
    }
    ctx.globalAlpha = 1; ctx.restore();
  }

  _drawOverlay() {
    const ctx = this.overlay.getContext("2d");
    const W = this.overlay.clientWidth, H = this.overlay.clientHeight, rollBot = H - VEL_H;
    ctx.clearRect(0, 0, W, H);

    // marquee
    if (this._marquee) {
      const m = this._marquee, x = Math.min(m.x0, m.x1), y = Math.min(m.y0, m.y1), w = Math.abs(m.x1 - m.x0), h = Math.abs(m.y1 - m.y0);
      ctx.fillStyle = "rgba(108,79,216,.10)"; ctx.fillRect(x, y, w, h);
      ctx.strokeStyle = "rgba(108,79,216,.8)"; ctx.setLineDash([4, 2]); ctx.strokeRect(x + 0.5, y + 0.5, w, h); ctx.setLineDash([]);
    }

    // playhead (own layer so it never repaints the notes)
    const pos = this._playheadSrc();
    if (pos != null) {
      if (this.follow) this._followPlayhead(pos, W);
      const x = this._xOf(pos);
      if (x >= GUTTER_W - 1 && x <= W) {
        ctx.strokeStyle = "rgba(54,38,25,.85)"; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, RULER_H - 6); ctx.lineTo(Math.round(x) + 0.5, rollBot); ctx.stroke();
        ctx.fillStyle = "#362619"; ctx.beginPath(); ctx.moveTo(x - 4, RULER_H - 6); ctx.lineTo(x + 4, RULER_H - 6); ctx.lineTo(x, RULER_H); ctx.fill();
      }
    }
  }

  _followPlayhead(pos, W) {
    const x = this._xOf(pos), right = W - 60;
    if (x > right) { this.scrollX = clamp(pos * this.pxPer16 - (right - GUTTER_W), 0, this._maxScrollX()); this._need = true; }
    else if (x < GUTTER_W) { this.scrollX = clamp(pos * this.pxPer16 - this._rollW() * 0.2, 0, this._maxScrollX()); this._need = true; }
  }

  _roundRect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
}

export const NOTE_LABEL = (m) => `${NOTE_NAMES[((m % 12) + 12) % 12]}${Math.floor(m / 12) - 1}`;
