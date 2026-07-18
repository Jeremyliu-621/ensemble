// Tone.js-backed MIDI transport. Owns the AudioContext clock (Tone.Transport), a
// PolySynth per track, and the scheduled notes. Everything the gesture layer needs
// is a plain method here so commands.js never touches Tone directly.

import * as Tone from "https://cdn.jsdelivr.net/npm/tone@15.1.22/+esm";
import { Midi } from "https://cdn.jsdelivr.net/npm/@tonejs/midi@2.0.28/+esm";

const SECTION_COUNT = 8;

export class MidiPlayer {
  constructor({ onChange } = {}) {
    this.onChange = onChange || (() => {});
    this.midi = null;
    this.parts = [];
    this.synths = [];
    this.duration = 0;
    this.sections = [];       // [{ start, end }]
    this.selected = 0;
    this.loop = false;
    this.name = "";
  }

  get playing() {
    return Tone.getTransport().state === "started";
  }

  // Live playhead in seconds.
  get position() {
    return Math.min(Tone.getTransport().seconds, this.duration || 0);
  }

  // Must be called from a user gesture to unlock audio.
  static async unlockAudio() {
    await Tone.start();
  }

  // Parse + schedule an uploaded MIDI file (ArrayBuffer).
  async load(arrayBuffer, name = "clip.mid") {
    this._teardown();
    this.midi = new Midi(arrayBuffer);
    this.name = name;
    this.duration = this.midi.duration || 0;

    const transport = Tone.getTransport();
    transport.stop();
    transport.seconds = 0;
    transport.loop = false;

    // One PolySynth per non-empty track, scheduled via a Part.
    this.midi.tracks.forEach((track) => {
      if (!track.notes.length) return;
      const synth = new Tone.PolySynth(Tone.Synth, {
        envelope: { attack: 0.01, decay: 0.2, sustain: 0.3, release: 0.6 },
      }).toDestination();
      synth.volume.value = -8;
      const part = new Tone.Part((time, note) => {
        synth.triggerAttackRelease(note.name, note.duration, time, note.velocity);
      }, track.notes.map((n) => ({ time: n.time, name: n.name, duration: n.duration, velocity: n.velocity })));
      part.start(0);
      this.synths.push(synth);
      this.parts.push(part);
    });

    // Split into equal sections for navigation.
    this.sections = [];
    const span = this.duration / SECTION_COUNT;
    for (let i = 0; i < SECTION_COUNT; i++) {
      this.sections.push({ start: i * span, end: (i + 1) * span });
    }
    this.selected = 0;
    this._applyLoop();
    this.onChange();
    return this.midi;
  }

  // Flattened notes for the timeline: [{ midi, time, duration, track }].
  notes() {
    if (!this.midi) return [];
    const out = [];
    this.midi.tracks.forEach((t, ti) => {
      for (const n of t.notes) out.push({ midi: n.midi, time: n.time, duration: n.duration, track: ti });
    });
    return out;
  }

  play() { if (this.midi) { Tone.getTransport().start(); this.onChange(); } }
  pause() { Tone.getTransport().pause(); this.onChange(); }
  toggle() { this.playing ? this.pause() : this.play(); }

  // Seek to an absolute time (clamped). Used by scrubbing.
  seek(seconds) {
    const t = Math.max(0, Math.min(seconds, this.duration || 0));
    Tone.getTransport().seconds = t;
    this.onChange();
  }

  // Rewind to the start of the current section (or track start if already there).
  rewind() {
    const s = this.sections[this.selected];
    const start = s ? s.start : 0;
    this.seek(this.position - start < 0.05 ? 0 : start);
  }

  selectSection(i) {
    if (!this.sections.length) return;
    this.selected = (i + this.sections.length) % this.sections.length;
    this.seek(this.sections[this.selected].start);
    this._applyLoop();
    this.onChange();
  }
  nextSection() { this.selectSection(this.selected + 1); }
  prevSection() { this.selectSection(this.selected - 1); }

  toggleLoop() {
    this.loop = !this.loop;
    this._applyLoop();
    this.onChange();
  }

  _applyLoop() {
    const transport = Tone.getTransport();
    const s = this.sections[this.selected];
    if (this.loop && s) {
      transport.loop = true;
      transport.loopStart = s.start;
      transport.loopEnd = s.end;
    } else {
      transport.loop = false;
    }
  }

  _teardown() {
    this.parts.forEach((p) => p.dispose());
    this.synths.forEach((s) => s.dispose());
    this.parts = [];
    this.synths = [];
  }
}
