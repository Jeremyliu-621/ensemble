// Shared WebAudio synth. Turns scheduled NoteEvents into oscillator voices,
// timed against the synced clock so every device plays in agreement. Used by
// the section pages and by the stage (which plays the orchestra when it's the
// only audio device).

const SEMI = { C: 0, "C#": 1, D: 2, "D#": 3, E: 4, F: 5, "F#": 6, G: 7, "G#": 8, A: 9, "A#": 10, B: 11 };

function noteToMidi(note) {
  const m = /^([A-G]#?)(-?\d+)$/.exec(note);
  return m ? (parseInt(m[2], 10) + 1) * 12 + SEMI[m[1]] : 69;
}
function noteToFreq(note) {
  return 440 * Math.pow(2, (noteToMidi(note) - 69) / 12);
}

// Percussion by General-MIDI drum-map pitch: kick = pitched thump, snare = noisy
// body, hats/cymbals = short high noise.
function drumHit(ctx, out, t, note, vel) {
  const midi = noteToMidi(note);
  const g = ctx.createGain();
  g.connect(out);
  if (midi <= 37) {                       // 35/36 kick
    const o = ctx.createOscillator();
    o.type = "sine";
    o.frequency.setValueAtTime(150, t);
    o.frequency.exponentialRampToValueAtTime(45, t + 0.11);
    g.gain.setValueAtTime(Math.min(1, vel), t);
    g.gain.exponentialRampToValueAtTime(0.001, t + 0.16);
    o.connect(g); o.start(t); o.stop(t + 0.18);
    return o;
  }
  const hat = midi >= 42;                  // 42/44/46 hats, 49/51 cymbals
  const dur = hat ? 0.05 : 0.13;
  const buf = ctx.createBuffer(1, Math.ceil(ctx.sampleRate * dur), ctx.sampleRate);
  const d = buf.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  const src = ctx.createBufferSource();
  src.buffer = buf;
  const f = ctx.createBiquadFilter();
  f.type = hat ? "highpass" : "bandpass";
  f.frequency.value = hat ? 8000 : 1900;
  g.gain.setValueAtTime(vel * 0.7, t);
  g.gain.exponentialRampToValueAtTime(0.001, t + dur);
  src.connect(f).connect(g); src.start(t); src.stop(t + dur);
  return src;
}

// Rough instrument timbres: {oscillator wave, lowpass cutoff Hz}. Enough to make
// a violin, cello, flute etc. read as distinct without samples.
const TIMBRES = {
  violin: { wave: "sawtooth", cutoff: 3200 },
  cello:  { wave: "sawtooth", cutoff: 1400 },
  viola:  { wave: "sawtooth", cutoff: 2200 },
  flute:  { wave: "sine",     cutoff: 6000 },
  clarinet:{ wave: "square",  cutoff: 2000 },
  piano:  { wave: "triangle", cutoff: 5000 },
  bass:   { wave: "triangle", cutoff: 600 },
  synth:  { wave: "triangle", cutoff: 4000 },
  bell:   { wave: "sine",     cutoff: 8000 },
};

export class Synth {
  // clock: a Clock instance; onPlay(ev, peak): optional visual callback fired at play time.
  constructor(clock, onPlay = null) {
    this.clock = clock;
    this.onPlay = onPlay;
    this.ctx = null;
    this.master = null;
    this.scheduled = [];
    this.timbre = null;   // set via setInstrument; null => waveform from articulation
  }

  setInstrument(name) {
    this.timbre = TIMBRES[name] || null;
  }

  // Must be called inside a user gesture (autoplay policy).
  async unlock() {
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    await this.ctx.resume();
    this.master = this.ctx.createGain();
    this.master.gain.value = 0.22;          // headroom so stacked notes don't clip
    this.master.connect(this.ctx.destination);
    // Silent one-sample buffer so iOS marks the context "running".
    const buf = this.ctx.createBuffer(1, 1, this.ctx.sampleRate);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    src.start(0);
    return this.ctx;
  }

  // ev = {at (server ms), dur (ms), note, vel, art}
  schedule(ev) {
    if (!this.ctx) return;
    const when = this.clock.serverToAudioTime(ev.at);
    if (when === null) return;
    const now = this.ctx.currentTime;
    if (when < now - 0.05) return;          // hopelessly late — drop
    const t = Math.max(when, now + 0.001);
    const peak = Math.max(0.05, ev.vel || 0.7);

    if (ev.art === "drum") {               // percussion, independent of the section instrument
      const src = drumHit(this.ctx, this.master, t, ev.note, peak);
      this.scheduled.push(src);
      src.onended = () => { const i = this.scheduled.indexOf(src); if (i >= 0) this.scheduled.splice(i, 1); };
      if (this.onPlay) {
        const delay = ev.at - this.clock.serverNow();
        setTimeout(() => this.onPlay(ev, peak), Math.max(0, delay));
      }
      return;
    }

    const durSec = Math.max(0.08, (ev.dur || 200) / 1000);
    const sustain = ev.art === "sustain";
    const osc = this.ctx.createOscillator();
    const g = this.ctx.createGain();
    osc.type = this.timbre ? this.timbre.wave : (sustain ? "sine" : "triangle");
    osc.frequency.value = noteToFreq(ev.note);
    if (this.timbre) {
      const lp = this.ctx.createBiquadFilter();
      lp.type = "lowpass";
      lp.frequency.value = this.timbre.cutoff;
      osc.connect(lp).connect(g).connect(this.master);
    } else {
      osc.connect(g).connect(this.master);
    }

    const atk = sustain ? 0.03 : 0.005;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(peak, t + atk);
    if (sustain) {
      g.gain.setValueAtTime(peak, t + Math.max(atk, durSec - 0.15));
    }
    g.gain.exponentialRampToValueAtTime(0.0001, t + durSec);
    osc.start(t);
    osc.stop(t + durSec + 0.03);
    this.scheduled.push(osc);
    osc.onended = () => {
      const i = this.scheduled.indexOf(osc);
      if (i >= 0) this.scheduled.splice(i, 1);
    };

    if (this.onPlay) {
      const delay = ev.at - this.clock.serverNow();
      setTimeout(() => this.onPlay(ev, peak), Math.max(0, delay));
    }
  }

  panic() {
    while (this.scheduled.length) {
      try { this.scheduled.pop().stop(); } catch {}
    }
  }
}
