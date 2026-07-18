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

// Real sampled instruments (FluidR3_GM renders in /assets/sf/, fetched by
// server/tools/fetch_samples.sh). Samples exist every 3rd semitone; the player
// detunes the nearest one. Instruments without samples fall back to oscillators.
const SAMPLE_MAP = {
  piano: "acoustic_grand_piano", violin: "violin", viola: "viola", cello: "cello",
  flute: "flute", clarinet: "clarinet", trumpet: "trumpet", bass: "acoustic_bass",
  harp: "orchestral_harp", bell: "music_box",
};
const FLATS = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"];

// A seat on the stage per instrument (stereo pan): decongests the mix the way
// a real ensemble does, instead of everything stacked dead-center.
const PAN = { piano: 0, violin: 0.35, viola: 0.2, cello: -0.3, flute: 0.45,
              clarinet: -0.4, trumpet: 0.3, bass: -0.15, harp: -0.35, bell: 0.45 };

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
    this.timbreName = name;
  }

  // Nearest sampled note for this instrument, or null (loading/missing —
  // caller falls back to an oscillator, so audio never waits on a fetch).
  _sample(inst, midi) {
    const folder = SAMPLE_MAP[inst];
    if (!folder) return null;
    if (!this._samples) this._samples = {};
    let sm = 36 + Math.round((midi - 36) / 3) * 3;
    sm = Math.max(36, Math.min(96, sm));
    const key = `${folder}/${FLATS[sm % 12]}${Math.floor(sm / 12) - 1}`;
    const entry = this._samples[key];
    if (entry === undefined) {
      this._samples[key] = null;               // loading
      fetch(`/assets/sf/${key}.mp3`)
        .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error(String(r.status)))))
        .then((ab) => this.ctx.decodeAudioData(ab))
        .then((buf) => { this._samples[key] = { buffer: buf }; })
        .catch(() => { this._samples[key] = { buffer: false }; });
      return null;
    }
    if (!entry || !entry.buffer) return null;
    return { buffer: entry.buffer, cents: (midi - sm) * 100 };
  }

  // Must be called inside a user gesture (autoplay policy).
  async unlock() {
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    await this.ctx.resume();
    this.master = this.ctx.createGain();
    this.master.gain.value = 0.3;           // headroom; the limiter catches peaks
    // Master tension filter: the wand's ToF proximity sweeps this closed for
    // build-ups (fx.tension). Wide open (18kHz) = inaudible by default.
    this.fx = this.ctx.createBiquadFilter();
    this.fx.type = "lowpass";
    this.fx.frequency.value = 18000;
    // Ear-safety limiter: dense material (busy MIDIs, transcriptions) can stack
    // dozens of oscillators — the compressor stops that becoming a blare.
    this.limiter = this.ctx.createDynamicsCompressor();
    this.limiter.threshold.value = -9;      // safety net, not a sound: gentle, peaks only
    this.limiter.knee.value = 12;
    this.limiter.ratio.value = 4;
    this.limiter.attack.value = 0.004;
    this.limiter.release.value = 0.18;
    this.master.connect(this.fx);
    this.fx.connect(this.limiter);
    this.limiter.connect(this.ctx.destination);
    // Silent one-sample buffer so iOS marks the context "running".
    const buf = this.ctx.createBuffer(1, 1, this.ctx.sampleRate);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    src.start(0);
    // Preload every sampled note now (~5MB, local) so no note ever falls back
    // to an oscillator mid-song while its sample streams in.
    for (const inst of Object.keys(SAMPLE_MAP)) {
      for (let m = 36; m <= 96; m += 3) this._sample(inst, m);
    }
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
    // Crowd control: soften only genuinely extreme stacks (ringing tails are
    // normal now), and never below ~half volume — the limiter handles the rest.
    const crowd = Math.max(0.55, Math.min(1, 48 / (this.scheduled.length + 1)));
    const peak = Math.max(0.05, (ev.vel || 0.7) * crowd);

    if (ev.art === "drum") {                // percussion voice (GM-mapped by pitch)
      this._drum(noteToMidi(ev.note), t, peak);
      if (this.onPlay) {
        const delay = ev.at - this.clock.serverNow();
        setTimeout(() => this.onPlay(ev, peak), Math.max(0, delay));
      }
      return;
    }

    const durSec = Math.max(0.08, (ev.dur || 200) / 1000);
    const sustain = ev.art === "sustain";

    // Sampled path: the event names its instrument (a loaded MIDI part), or
    // this device has one configured. Real recorded notes beat oscillators.
    const inst = ev.inst || this.timbreName;
    const smp = inst ? this._sample(inst, noteToMidi(ev.note)) : null;
    if (smp) {
      const src = this.ctx.createBufferSource();
      src.buffer = smp.buffer;
      src.detune.value = smp.cents + (this.exprSemis || 0) * 100;
      // Never outlive the sample: our one-shots run ~2s — holding longer means
      // dead air or a hard cutoff mid-note. Cap and release gracefully.
      const holdSec = Math.min(durSec, Math.max(0.1, smp.buffer.duration - 0.35));
      const g = this.ctx.createGain();
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(peak, t + 0.012);
      // Plucked notes ring naturally past their notated length, like a pedal.
      const tail = sustain ? 0.25 : 0.4;
      g.gain.setValueAtTime(peak, t + Math.max(0.012, holdSec - 0.05));
      g.gain.exponentialRampToValueAtTime(0.0001, t + holdSec + tail);
      const pan = this.ctx.createStereoPanner ? this.ctx.createStereoPanner() : null;
      if (pan) {
        pan.pan.value = PAN[inst] || 0;
        src.connect(g).connect(pan).connect(this.master);
      } else {
        src.connect(g).connect(this.master);
      }
      src.start(t);
      src.stop(t + holdSec + tail + 0.05);
      this.scheduled.push(src);
      src.onended = () => {
        const i = this.scheduled.indexOf(src);
        if (i >= 0) this.scheduled.splice(i, 1);
      };
      if (this.onPlay) {
        const delay = ev.at - this.clock.serverNow();
        setTimeout(() => this.onPlay(ev, peak), Math.max(0, delay));
      }
      return;
    }

    const osc = this.ctx.createOscillator();
    const g = this.ctx.createGain();
    osc.type = this.timbre ? this.timbre.wave : (sustain ? "sine" : "triangle");
    osc.frequency.value = noteToFreq(ev.note);
    if (this.exprSemis) osc.detune.value = this.exprSemis * 100;
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

  _noise() {
    if (!this._noiseBuf) {
      const n = this.ctx.sampleRate;        // 1s of white noise, built once
      this._noiseBuf = this.ctx.createBuffer(1, n, n);
      const d = this._noiseBuf.getChannelData(0);
      for (let i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
    }
    const src = this.ctx.createBufferSource();
    src.buffer = this._noiseBuf;
    return src;
  }

  // Synthesized kit: kick <= 36, snare 37-40, everything else hat/percussion.
  _drum(midi, t, vel) {
    const g = this.ctx.createGain();
    g.connect(this.master);
    if (midi <= 36) {                       // kick: pitch-swept sine + thump
      const osc = this.ctx.createOscillator();
      osc.frequency.setValueAtTime(150, t);
      osc.frequency.exponentialRampToValueAtTime(45, t + 0.12);
      g.gain.setValueAtTime(vel, t);
      g.gain.exponentialRampToValueAtTime(0.001, t + 0.25);
      osc.connect(g);
      osc.start(t); osc.stop(t + 0.3);
      this.scheduled.push(osc);
    } else if (midi <= 40) {                // snare: bandpassed noise + body tone
      const noise = this._noise();
      const bp = this.ctx.createBiquadFilter();
      bp.type = "bandpass"; bp.frequency.value = 1800; bp.Q.value = 0.8;
      g.gain.setValueAtTime(vel * 0.8, t);
      g.gain.exponentialRampToValueAtTime(0.001, t + 0.18);
      noise.connect(bp).connect(g);
      noise.start(t); noise.stop(t + 0.2);
      const body = this.ctx.createOscillator();
      const bg = this.ctx.createGain();
      body.type = "triangle"; body.frequency.value = 185;
      bg.gain.setValueAtTime(vel * 0.4, t);
      bg.gain.exponentialRampToValueAtTime(0.001, t + 0.1);
      body.connect(bg).connect(this.master);
      body.start(t); body.stop(t + 0.12);
      this.scheduled.push(noise, body);
    } else if (midi >= 49) {                // crash: long bright wash (the sting)
      const noise = this._noise();
      const hp = this.ctx.createBiquadFilter();
      hp.type = "highpass"; hp.frequency.value = 4500;
      g.gain.setValueAtTime(vel * 0.6, t);
      g.gain.exponentialRampToValueAtTime(0.001, t + 0.9);
      noise.connect(hp).connect(g);
      noise.start(t); noise.stop(t + 0.95);
      this.scheduled.push(noise);
    } else {                                // hats / other percussion: bright tick
      const noise = this._noise();
      const hp = this.ctx.createBiquadFilter();
      hp.type = "highpass"; hp.frequency.value = 7000;
      const open = midi === 46;             // open hat rings a little longer
      g.gain.setValueAtTime(vel * 0.5, t);
      g.gain.exponentialRampToValueAtTime(0.001, t + (open ? 0.25 : 0.06));
      noise.connect(hp).connect(g);
      noise.start(t); noise.stop(t + (open ? 0.3 : 0.08));
      this.scheduled.push(noise);
    }
  }

  // Deterministic-mode expression (fx.expr): scale-locked pitch offset + volume
  // swell. Warps live voices (detune ramp) and everything scheduled after.
  setExpression(semis, gain) {
    this.exprSemis = semis || 0;
    if (!this.ctx) return;
    if (this.master) this.master.gain.setTargetAtTime(0.22 * (gain || 1), this.ctx.currentTime, 0.08);
    for (const src of this.scheduled) {
      if (src.detune) src.detune.setTargetAtTime(this.exprSemis * 100, this.ctx.currentTime, 0.06);
    }
  }

  // Proximity build-up: 0 = open, 1 = fully "squished" (wand ToF -> fx.tension).
  // Near the floor the master low-pass sweeps closed AND the limiter clamps down,
  // so the room goes muffled + compressed ("underwater"), not just darker.
  setTension(v) {
    if (!this.fx) return;
    const t = Math.max(0, Math.min(1, v || 0));
    const now = this.ctx.currentTime;
    const f = 250 + 17750 * Math.pow(1 - t, 2);
    this.fx.frequency.setTargetAtTime(f, now, 0.08);
    if (this.limiter) {
      // Squish the dynamics as t rises, easing back to the ear-safety defaults
      // (-9 dB / 4:1) when open. Doesn't touch master.gain, so fx.expr is free.
      this.limiter.threshold.setTargetAtTime(-9 - 26 * t, now, 0.08);   // -> -35 dB
      this.limiter.ratio.setTargetAtTime(4 + 8 * t, now, 0.08);         // -> 12:1
    }
  }

  panic() {
    while (this.scheduled.length) {
      try { this.scheduled.pop().stop(); } catch {}
    }
  }
}
