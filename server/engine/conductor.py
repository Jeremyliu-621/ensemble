"""Conductor: the real music engine (implements the MusicEngine Protocol).

Per bar it generates the candidate accompaniments, asks the ranker which one the
latest gesture wants, and schedules that line across the sections plus the melody
on top — so there's always music, and gestures reshape the accompaniment. Pulled
by the scheduler on a lookahead, exactly like the metronome stub it replaces.
"""
from __future__ import annotations

import itertools
import logging
import math
import secrets

import config
from config import MIN_LEAD_MS
from engine.candidates import ART, GENERATORS, generate
from engine.harmony import (PAD_VEL, ROOT_VEL, approach_run, arpeggiate, bar_chords,
                            chord_span, passing_infill, thin_grid, voice_lead)
from engine.song import builtin_song
from engine.theory import midi_to_name, triad, voice_triad
from engine_api import CancelSpec, GestureWindow, NoteEvent, SectionInfo
from gestures.features import GestureFeatures, extract_features
from ml import heuristic
from ml.barmodel import RemoteBarModel, style_for
from ml.datalog import DecisionLog
from ml.policy import RemoteModel, heuristic_decision
from ml.schema import Decision, build_bar_context, build_context
from protocol import SECTION_ALL

log = logging.getLogger("engine")


class Conductor:
    def __init__(self) -> None:
        self.song = builtin_song()
        self.bpm = self.song.bpm
        self.bar_ms = 60_000.0 / self.bpm * 4           # 4/4
        self.s16_ms = self.bar_ms / 16
        self._playing = False
        self._next_bar_idx = 0
        self._next_bar_start = 0.0
        self._gesture: GestureFeatures | None = None
        self._last_choice: str | None = None
        self._forced: str | None = None                 # editor override; None = let the ranker choose
        self._aim: str | None = None                    # wand-aimed section (spatial/solo mode)
        self._part_map: dict[int, str] | None = None    # LLM arranger: part idx -> section
        self._pickup: list | None = None                # instant gesture answer, pending emission
        self._arc = 0                                   # bars left in a build arc (0 = none)
        self._arc_total = 4
        self._arc_now = (1.0, 0.0, False)               # this bar's (vel_mult, density_floor, climax)
        # The CONDUCTING ENVELOPE: a continuous intensity (0 hushed .. 1 full,
        # 0.5 = the song exactly as written). Gestures push the target; the
        # envelope chases it and both relax toward neutral over ~6-8 bars — so
        # a wave swells the orchestra and it breathes back down, instead of an
        # inserted "altered section". Drives density, dynamics, AND tempo.
        self._intensity = 0.5
        self._intensity_target = 0.5
        self.base_bpm = self.bpm                        # the song's own tempo (rubato pivots here)
        self._chords = bar_chords(self.song)
        self._pad_voices: list[int] | None = None
        self._pad_until = -1
        self._model = RemoteModel()                     # trained policy (WM_MODEL_URL); optional
        self._barmodel = RemoteBarModel()               # trained line writer (WM_BARMODEL_URL)
        self._decision: Decision | None = None          # the policy's active answer, until the next gesture
        self._last_source = "heuristic"
        self._datalog = DecisionLog()
        self._sections: list[SectionInfo] = []
        self._cancels: list[CancelSpec] = []
        self._ids = itertools.count(1)
        # Clients dedupe sched.notes by event id (lookahead windows overlap). Ids
        # must therefore stay unique ACROSS server restarts, or a tab that lives
        # through a restart silently drops every "already seen" n1, n2, … again.
        self._id_boot = secrets.token_hex(3)
        self._tracks: list[dict] = []                    # parts of a loaded MIDI (for the editor)
        self._reanchor = False                           # re-align the bar cursor on the next pull
        self._anchor_ms = 0.0                            # server-ms of bar 0 (drives the editor playhead)
        self._device = "verbatim"                        # what the arrangement layer did last bar

    def load_song(self, song, tracks: list[dict] | None = None) -> None:
        """Replace the song and restart it cleanly from bar 0 (a freshly dropped MIDI)."""
        self._part_map = None                   # a new song gets a fresh arrangement
        self.update_song(song, tracks, reanchor=True, set_tempo=True)
        self._last_choice = None
        log.info("loaded song '%s': %d bars, key=%d, %d parts",
                 song.name, len(song.bars), song.key_root, len(self._tracks))

    def update_song(self, song, tracks: list[dict] | None = None, *,
                    reanchor: bool = False, set_tempo: bool = False) -> None:
        """Swap in a new song. With reanchor=False the bar cursor keeps running, so
        a live edit takes effect on the next bar rather than restarting playback."""
        self.song = song
        self._tracks = tracks or []
        self._chords = bar_chords(song)         # harmony follows every song change
        self._pad_voices = None                 # pad layer re-voices fresh
        self._pad_until = -1
        if set_tempo:
            self.set_tempo(song.bpm)
        if reanchor:
            self._reanchor = True

    # --- editor controls ---
    def set_tempo(self, bpm: float) -> None:
        self.base_bpm = max(40.0, min(220.0, bpm))
        self.bpm = self.base_bpm
        self.bar_ms = 60_000.0 / self.bpm * 4
        self.s16_ms = self.bar_ms / 16

    def set_forced(self, candidate: str | None) -> None:
        self._forced = candidate if candidate and candidate != "auto" else None

    def part_instruments(self) -> list[str]:
        """Ordered non-drum instruments of the current song's parts (empty for
        the built-in loop). Joining phones are dealt from THIS list so every
        phone always matches a track the song actually contains."""
        return [t["instrument"] for t in self._tracks if not t.get("is_drum")]

    def status(self) -> dict:
        tracks = self._tracks
        if not tracks:   # built-in song: expose its melody as an editable track
            roll = [[b, on, dur, pitch, 0.9]
                    for b, bar in enumerate(self.song.bars) for (on, dur, pitch) in bar.melody]
            tracks = [{"name": "melody", "instrument": "synth", "is_drum": False,
                       "is_melody": True, "note_count": len(roll), "roll": roll}]
        return {
            "playing": self._playing,
            "bpm": round(self.bpm),
            "forced": self._forced or "auto",
            "last_choice": self._last_choice,
            "decision_source": self._last_source,
            "device": self._device,
            "intensity": round(self._intensity, 3),
            "candidates": list(GENERATORS) + (["generated"] if self._barmodel.configured else []),
            "training_rows": self._datalog.rows,
            "gesture": self._gesture.as_dict() if self._gesture else None,
            "song": self.song.name,
            "key_root": self.song.key_root,
            "bars": len(self.song.bars),
            "tracks": tracks,
            "aimed": self._aim,
            # Lets the editor draw a smooth playhead from its own synced clock:
            # pos16 = ((clock.serverNow() - anchor) / s16_ms) mod (n_bars*16).
            "transport": {"playing": self._playing, "anchor": self._anchor_ms,
                          "bar_ms": self.bar_ms, "s16_ms": self.s16_ms,
                          "n_bars": len(self.song.bars)},
        }

    # --- transport ---
    def on_transport(self, cmd: str, t0_ms: float | None) -> None:
        if cmd in ("start", "clicktest"):
            if self._playing:                    # restart: silence the old timeline first
                self._cancels.append(CancelSpec(allnotesoff=True))
            self._playing = True
            self._reanchor = False               # start's clean anchor wins over a pending reanchor
            self._next_bar_idx = 0
            self._next_bar_start = t0_ms or 0.0
            self._anchor_ms = self._next_bar_start
            self._reanchor = False   # a fresh start supersedes a pending load re-anchor
            log.info("transport start @%.0f  bar=%.0fms (%.0f BPM)",
                     self._next_bar_start, self.bar_ms, self.song.bpm)
        elif cmd in ("rewind", "forward"):       # palm-swipe time jump, beat-locked
            self._next_bar_idx = max(0, self._next_bar_idx + (-4 if cmd == "rewind" else 4))
            log.info("timeline %s -> bar %d", cmd, self._next_bar_idx)
        elif cmd in ("stop", "allnotesoff"):
            self._playing = False
            self._cancels.append(CancelSpec(allnotesoff=True))

    # --- inputs ---
    def on_sections_changed(self, sections: list[SectionInfo]) -> None:
        self._sections = [s for s in sections if s.ready]

    # Preset feature vectors for on-wand TinyML labels: firmware that classifies
    # locally can skip streaming a window and just name the motion. The label
    # becomes the same 5-feature intent the raw path extracts, so both firmware
    # styles drive the identical decision pipeline.
    _CLASSIFIED = {
        "sharp_up":   GestureFeatures(energy=0.9, size=0.7, vertical=0.9, duration=0.3),
        "sharp_down": GestureFeatures(energy=0.9, size=0.7, vertical=-0.9, duration=0.3),
        "swish":      GestureFeatures(energy=0.7, size=0.7, duration=0.8),
        "twist":      GestureFeatures(energy=0.5, size=0.4, rotation=0.9, duration=0.7),
        "still":      GestureFeatures(energy=0.05, size=0.05, duration=1.0),
        "flick":      GestureFeatures(energy=0.75, size=0.3, duration=0.3),
    }

    def on_gesture(self, window: GestureWindow) -> None:
        self._gesture_in(extract_features(window), window.t_end_server_ms)

    def on_classified(self, label: str, strength: float, server_ms: float) -> None:
        """A TinyML-classified gesture from the wand itself (wand.gesture)."""
        preset = self._CLASSIFIED.get(label)
        if preset is None:
            log.info("unknown classified gesture %r ignored", label)
            return
        s = max(0.2, min(1.5, strength or 1.0))
        self._gesture_in(GestureFeatures(
            energy=min(1.0, preset.energy * s), size=min(1.0, preset.size * s),
            vertical=preset.vertical, rotation=min(1.0, preset.rotation * s),
            duration=preset.duration), server_ms)

    @staticmethod
    def _is_stab(g: GestureFeatures | None) -> bool:
        """Sharp AND short (<0.4s) = a stab. A half-second vigorous wave is a
        push; every real flick (demo button, wand presets) is ~0.3s."""
        return g is not None and g.energy > 0.65 and 0.0 < g.duration < 0.4

    def _gesture_in(self, features: GestureFeatures, t_end_ms: float) -> None:
        self._gesture = features
        # Push the conducting envelope: the gesture's vigor becomes the target
        # intensity the orchestra chases (and then relaxes from). Two shapes are
        # special: a stab is an ACCENT — the sting fires but the envelope stays
        # put, the arrangement shouldn't lurch. A twist carries its push in the
        # wrist, not the arm — rotation lifts the target so the arpeggio it
        # selects is actually audible (accel alone reads near-zero).
        if not self._is_stab(features):
            target = 0.6 * features.energy + 0.4 * features.size
            if features.rotation > 0.5:
                target = max(target, 0.5 + 0.35 * features.rotation)
            self._intensity_target = max(0.0, min(1.0, target))
        log.info("gesture -> %s (intensity target %.2f)",
                 {k: round(v, 2) for k, v in features.as_dict().items()}, self._intensity_target)
        # A SWELL (slow, sustained lift) arms a planned multi-bar arc: the next
        # bars ramp density + velocity and land on a climax crash. One decision,
        # deterministic execution — latency only gates when it starts, not how
        # long it lasts.
        if features.vertical > 0.6 and features.energy > 0.45 and features.duration > 0.8:
            self._arc = self._arc_total
            log.info("build arc armed: %d bars to climax", self._arc)
        self._decision = None                    # new intent — the model must re-decide
        self._model.request(self._context())
        self._queue_pickup(t_end_ms)

    def _arc_step(self) -> tuple[float, float, bool]:
        """Advance the build arc one bar: (vel_mult, density_floor, climax)."""
        if self._arc <= 0:
            return (1.0, 0.0, False)
        p = 1.0 - (self._arc - 1) / self._arc_total   # 0.25 -> 0.5 -> 0.75 -> 1.0
        self._arc -= 1
        return (0.8 + 0.5 * p, min(1.0, 0.35 + 0.75 * p), p >= 1.0)

    def on_grab(self, kind: str, server_ms: float) -> None:
        pass  # grab edges could cut sustains; not needed for the slice

    def _queue_pickup(self, t_end_ms: float) -> None:
        """The room answers a gesture right away, on the next 8th-note. A sharp
        DRAMATIC motion (fast + short = a stab) gets a fortissimo two-octave
        chord sting with a crash; anything gentler gets a quick low-velocity
        flourish. The full musical response still lands at the bar line."""
        if not self._playing or not config.PICKUP:
            return
        g = self._gesture
        sting = self._is_stab(g)
        eighth = self.bar_ms / 8
        bar_start = self._next_bar_start - self.bar_ms
        at = bar_start + math.ceil((t_end_ms + 60.0 - bar_start) / eighth) * eighth
        chord = self.song.bar(max(0, self._next_bar_idx - 1)).chord_pcs
        if sting:
            stab = voice_triad(chord, base=52) + voice_triad(chord, base=64)
            self._pickup = [(at, eighth, m, 0.95, "pluck") for m in stab]
            self._pickup.append((at, eighth, 49, 0.9, "drum"))             # crash
        else:                                                              # the flourish
            vel = round(0.25 + 0.5 * (g.energy if g else 0.3), 3)
            self._pickup = [(at + i * eighth / 4, eighth / 4, m, vel, "pluck")
                            for i, m in enumerate(voice_triad(chord, base=64))]

    def on_aim(self, section_id: str | None) -> None:
        # Engaging solo cuts every OTHER phone's in-flight notes immediately —
        # isolation is heard in ~one scheduler tick, not at the next bar line.
        # Releasing solo lets the room come back on the beat (no cancels).
        if (section_id and section_id != self._aim and self._playing
                and any(s.section_id == section_id for s in self._sections)):
            for s in self._sections:
                if s.section_id != section_id:
                    self._cancels.append(CancelSpec(section=s.section_id))
        self._aim = section_id

    def on_feedback(self, value: int) -> None:
        self._datalog.feedback(value)
        log.info("feedback %+d (logged as a training signal)", value)

    # --- event pull ---
    def get_events(self, now_ms: float, until_ms: float) -> list[NoteEvent]:
        if not self._playing:
            return []
        if self._reanchor:                       # a freshly loaded song starts here
            # Must clear MIN_LEAD_MS or the new song's downbeat gets dropped.
            self._next_bar_start = now_ms + max(2 * MIN_LEAD_MS, 400.0)
            self._next_bar_idx = 0
            self._anchor_ms = self._next_bar_start
            self._reanchor = False
        events: list[NoteEvent] = []
        if self._pickup:                         # instant gesture answer, once
            for (at, dur, midi, vel, art) in self._pickup:
                if at >= now_ms + MIN_LEAD_MS:
                    events.append(self._note(SECTION_ALL, at, dur, midi, vel, art))
            self._pickup = None
        while self._next_bar_start <= until_ms:
            if self._next_bar_start >= now_ms - self.bar_ms:
                events.extend(self._bar_events(self._next_bar_idx, self._next_bar_start))
            self._next_bar_start += self.bar_ms
            self._next_bar_idx += 1
        return events

    def get_cancels(self) -> list[CancelSpec]:
        out, self._cancels = self._cancels, []
        return out

    # --- decision policy ---
    def _context(self, idx: int | None = None) -> dict:
        bar = self.song.bar(self._next_bar_idx if idx is None else idx)
        return build_context(key_root=self.song.key_root, bpm=self.bpm,
                             chord_root=bar.chord_root, chord_minor=bar.chord_minor,
                             last_choice=self._last_choice, gesture=self._gesture)

    def _decide(self, idx: int, cands: dict) -> Decision:
        """Editor override > active model answer > heuristic — always instantly
        playable, so the network can only add intelligence, never stall a bar.
        Every (context, decision) pair becomes a logged training row."""
        ctx = self._context(idx)
        if self._forced and self._forced in cands:
            decision = Decision(candidate=self._forced,
                                octave_shift=heuristic.octave_shift(self._gesture) // 12,
                                source="forced")
        else:
            fresh = self._model.take()
            if fresh is not None:
                self._decision = fresh
            if self._decision is not None and self._decision.candidate in cands:
                decision = self._decision
            else:
                decision = heuristic_decision(self._gesture, self._last_choice, list(cands))
        self._last_choice = decision.candidate
        self._last_source = decision.source
        self._datalog.decision(bar=idx, song=self.song.name, context=ctx, decision=decision)
        return decision

    def _arr_style(self) -> str:
        """The ear-approved device vocabulary, ranked by the conductor's own
        listening tests: harmonize and hush strongest, then arpeggio and
        passing. (Echo was cut — sounded out of place.) Any device that ADDS
        notes needs the envelope above neutral, so all three bands live in the
        lift-reachable zone: twist or the biggest wave = ENERGIZE (arpeggio),
        a firm push = GROUND (harmonize), the lightest push that still lifts =
        EMBELLISH (passing)."""
        g = self._gesture
        # A build arc blooms chords by definition — the climax needs its
        # harmonic bed, not sparse ornaments, whatever gesture armed it.
        if g is None or self._arc_now[1] > 0:
            return "harmonize"
        if g.rotation > 0.5:
            return "arpeggio"
        e = 0.6 * g.energy + 0.4 * g.size
        if e > 0.88:
            return "arpeggio"
        if e > 0.76:
            return "harmonize"
        return "passing"

    def _take_generated(self, idx: int, cands: dict) -> None:
        """Add the bar model's prefetched line (if one landed for this bar) and
        request TWO bars ahead — measured serving latency for a composed bar is
        ~4.6s, so the request needs two bars of playing time (~4.8s at 100 BPM)
        to land. A faster serving host can drop this back to one."""
        taken = self._barmodel.take(idx)
        if taken:
            cands["generated"] = taken[0]
            self._gen_style = taken[1]
        if self._barmodel.configured:
            tgt, prev = self.song.bar(idx + 2), self.song.bar(idx + 1)
            style = self._arr_style() if self.song.parts else style_for(self._gesture)
            # Never ask the deployed adapter for a style it wasn't trained on —
            # it would improvise something OFF-style and we'd play it under the
            # style's name. Out-of-list styles are deterministic (harmony.py).
            if style not in config.BARMODEL_STYLES:
                style = "harmonize"
            self._barmodel.prefetch(idx + 2, build_bar_context(
                key_root=self.song.key_root, bpm=self.bpm,
                chord_root=tgt.chord_root, chord_minor=tgt.chord_minor,
                style=style,
                melody=tgt.melody, prev_melody=prev.melody), self.song.key_root)

    # --- bar generation ---
    def _bar_events(self, idx: int, bar_start: float) -> list[NoteEvent]:
        self._arc_now = self._arc_step()
        # Advance the conducting envelope one bar: chase the gesture's target,
        # while the target itself relaxes toward neutral. ~2 bars to arrive,
        # ~6-8 bars to breathe back — phrasing, not a step function.
        self._intensity += (self._intensity_target - self._intensity) * 0.5
        self._intensity_target += (0.5 - self._intensity_target) * 0.18
        if abs(self._intensity - 0.5) < 0.03 and self._gesture is not None:
            self._gesture = None                 # fully relaxed: the cue is over
            self._decision = None
        # Rubato: the tempo leans with the conductor (±6% around the song's own).
        self.bpm = self.base_bpm * (1 + (self._intensity - 0.5) * 0.12)
        self.bar_ms = 60_000.0 / self.bpm * 4
        self.s16_ms = self.bar_ms / 16
        if self.song.parts:                      # a loaded MIDI: play its arrangement
            return self._arrangement_events(idx, bar_start)
        bar = self.song.bar(idx)
        prev = self.song.bar(idx - 1)
        cands = generate(bar, prev, self.song.key_root)
        self._take_generated(idx, cands)

        decision = self._decide(idx, cands)
        choice, shift = decision.candidate, decision.semitones()

        responder = cands[choice]
        art = ART.get(choice, "pluck")
        events: list[NoteEvent] = []

        # Distribute parts so multiple phones are genuinely different instruments:
        #   2+ sections -> section[0] plays melody, the rest play the accompaniment
        #                  (wand aim overrides: the aimed phone carries the line solo)
        #   1 section   -> it plays both
        #   0 sections  -> laptop (stage) plays everything via SECTION_ALL
        n = len(self._sections)
        aim = self._aim if any(s.section_id == self._aim for s in self._sections) else None
        if n >= 2 and aim:
            melody_sec = next(s.section_id for s in self._sections if s.section_id != aim)
            responder_secs = [aim]
        elif n >= 2:
            melody_sec = self._sections[0].section_id
            responder_secs = [s.section_id for s in self._sections[1:]]
        elif n == 1:
            melody_sec = self._sections[0].section_id
            responder_secs = [melody_sec]
        else:
            melody_sec = SECTION_ALL
            responder_secs = [SECTION_ALL]

        vel_mult, _floor, climax = self._arc_now
        for (on, dur, midi, vel) in responder:
            at, d, note = bar_start + on * self.s16_ms, dur * self.s16_ms, _clampmidi(midi + shift)
            for sec in responder_secs:
                events.append(self._note(sec, at, d, note, min(1.0, vel * vel_mult), art))

        for (on, dur, midi) in bar.melody:
            events.append(self._note(melody_sec, bar_start + on * self.s16_ms,
                                     dur * self.s16_ms, midi, 0.9, "pluck"))
        if climax:                               # the arc lands: a crash on the downbeat
            events.append(self._note(SECTION_ALL, bar_start, self.s16_ms * 2, 49, 0.95, "drum"))

        # Free-play reports its device too, so the camera flash always has the
        # truth to show — including a REAL octave shift when one was applied.
        self._device = choice if abs(shift) < 12 else \
            f"{choice} · octave {'up' if shift > 0 else 'down'}"
        log.info("bar %d -> %s [%s] (%d notes, shift %+d, %d sections)",
                 idx, choice, decision.source, len(responder), shift, n)
        return events

    def _shape(self, notes: list, keep: float, vel_scale: float, shift: int,
               is_drum: bool) -> list:
        """Bend one part's bar to the current envelope: `keep` is the fraction
        of notes that survive (the structurally strongest: long, loud,
        on-beat), `vel_scale` the dynamics, `shift` the register (never for
        drums). The build arc's floor/multiplier ride on top."""
        arc_mult, arc_floor, _climax = self._arc_now
        keep = max(keep, arc_floor)
        vel_scale *= arc_mult
        if keep <= 0.0 or not notes:
            return []
        if keep >= 1.0:
            kept = list(notes)
        else:
            ranked = sorted(notes, key=lambda nt: nt[1] * 2 + nt[3] * 4 + (2 if nt[0] % 4 == 0 else 0),
                            reverse=True)
            kept = sorted(ranked[:max(1, round(len(notes) * keep))])
        if is_drum:
            shift = 0
        return [(on, dur, midi + shift, min(1.0, vel * vel_scale))
                for (on, dur, midi, vel) in kept]

    def set_part_assignment(self, mapping: dict[str, list[int]] | None) -> None:
        """LLM-arranger routing: section_id -> part indices. None = round-robin."""
        self._part_map = {}
        for sid, idxs in (mapping or {}).items():
            for i in idxs:
                self._part_map[int(i)] = sid
        if not self._part_map:
            self._part_map = None
        log.info("part assignment: %s", mapping or "round-robin")

    def _arrangement_events(self, idx: int, bar_start: float) -> list[NoteEvent]:
        """The approved conducting vocabulary, and nothing else:
        neutral = the file verbatim; calm = HUSH (beat-grid simplification,
        softer); lift = HARMONY (voice-led chord pads + cello root, re-struck
        only on chord changes — the model's generated layer when one arrived,
        the deterministic theory otherwise). Tempo rubato rides the envelope.
        Part map routes parts; aiming SOLOS a phone; drums never transpose."""
        i9 = self._intensity
        calm = max(0.0, 0.5 - i9) * 2            # 0..1 below neutral
        lift = max(0.0, i9 - 0.5) * 2            # 0..1 above neutral
        arc_mult, arc_floor, _climax = self._arc_now
        lift = max(lift, arc_floor)              # a build arc is a sustained push
        neutral = calm < 0.06 and lift < 0.06 and not self._forced
        thin_level = 0 if calm < 0.2 else (1 if calm < 0.6 else 2)
        vel_scale = (1.0 - 0.45 * calm) * (1.0 + 0.35 * lift) * arc_mult

        gen_line = None
        if neutral:
            self._last_choice = None
        else:
            bar, prev = self.song.bar(idx), self.song.bar(idx - 1)
            cands = generate(bar, prev, self.song.key_root)
            self._take_generated(idx, cands)     # prefetches "harmonize" for idx+2
            gen_line = cands.get("generated")
            self._decide(idx, cands)             # the trained policy + training rows

        events: list[NoteEvent] = []
        n = len(self._sections)
        by_id = {s.section_id: s for s in self._sections}
        solo = self._aim if self._aim in by_id else None
        for i, part in enumerate(self.song.parts):
            # Routing priority: explicit LLM part map > instrument match (every
            # phone assigned this part's instrument plays it — that's how two
            # phones DOUBLE one section, in unison) > index round-robin so
            # nothing is silent > laptop via SECTION_ALL when no phones.
            if self._part_map and i in self._part_map and self._part_map[i] in by_id:
                secs = [self._part_map[i]]
            elif n == 0:
                secs = [SECTION_ALL]
            else:
                matched = [s.section_id for s in self._sections if s.instrument == part.instrument]
                secs = matched or [self._sections[i % n].section_id]
            if solo:
                secs = [s for s in secs if s == solo]
                if not secs:
                    continue                     # isolation: only the aimed phone sounds
            raw = part.bars[idx % len(part.bars)]
            if neutral:
                notes = raw
            elif part.is_melody:
                notes = [(on, dur, m, min(1.0, v * max(0.9, vel_scale)))
                         for (on, dur, m, v) in raw]
            else:
                # Drums hush one level harder — a calm room drops toward the kick.
                lvl = min(2, thin_level + 1) if (part.is_drum and thin_level) else thin_level
                notes = [(on, dur, m, min(1.0, v * vel_scale))
                         for (on, dur, m, v) in thin_grid(raw, lvl)]
            for sec in secs:
                sinfo = by_id.get(sec)
                if sinfo is not None and (sinfo.muted or sinfo.volume <= 0.001):
                    continue                     # per-phone mute/volume (console target panel)
                svol = 1.0 if sinfo is None else sinfo.volume
                for (on, dur, midi, vel) in notes:
                    art = "drum" if part.is_drum else ("sustain" if dur >= 8 else "pluck")
                    # Drum-map pitches are identifiers, not pitches - never clamp/fold them.
                    midi_out = midi if part.is_drum else _clampmidi(midi)
                    events.append(self._note(sec, bar_start + on * self.s16_ms,
                                             dur * self.s16_ms, midi_out, max(0.12, vel * svol), art,
                                             inst=part.instrument))

        # HARMONY on a push: the ear-ranked device for the gesture. The line is
        # the trained model's when one landed FOR THIS STYLE, else the exact
        # deterministic generator the model is trained on (engine/harmony.py) —
        # so every device sounds NOW, model in or out, and sounds the same.
        device = None
        if lift > 0.2:
            chord = self._chords[idx % len(self._chords)]
            style = self._arr_style()
            line, src = None, None
            if gen_line and getattr(self, "_gen_style", None) == style:
                line, src = gen_line, "model"
            elif style == "arpeggio":
                line, src = arpeggiate(self.song.bar(idx), self.song.bar(idx - 1),
                                       self.song.key_root), "theory"
            elif style == "passing":
                # Gap infill where the melody leaps, plus the approach run into
                # each chord change — so a light touch answers on EVERY song,
                # including ones whose melody never leaves stepwise motion.
                b = self.song.bar(idx)
                line = (passing_infill(b, self.song.bar(idx - 1), self.song.key_root)
                        + approach_run(b, self.song.bar(idx + 1), self.song.key_root))
                src = "theory"
            if line and style != "harmonize":
                mel_inst = next((p.instrument for p in self.song.parts if p.is_melody), "violin")
                solo_piece = len([p for p in self.song.parts if not p.is_drum]) <= 1
                # Ornaments ride the melody's own instrument; on a solo piece
                # EVERY device speaks it so nothing "comes out of nowhere".
                inst = mel_inst if (solo_piece or style == "passing") else "harp"
                for (on, dur, midi, vel) in line[:16]:
                    art = "sustain" if dur >= 8 else "pluck"
                    events.append(self._note(SECTION_ALL, bar_start + on * self.s16_ms,
                                             dur * self.s16_ms, midi,
                                             min(vel, 0.35 + 0.3 * lift), art, inst=inst))
                self._pad_until = idx
                device = f"{style} · {src}"
            elif line and src == "model":
                # Harmonize from the keeper model — the ear-approved live path,
                # unchanged: its held voicing re-lands each bar.
                for (on, dur, midi, vel) in line[:5]:
                    art = "sustain" if dur >= 8 else "pluck"
                    events.append(self._note(SECTION_ALL, bar_start + on * self.s16_ms,
                                             dur * self.s16_ms, midi,
                                             min(vel, 0.35 + 0.3 * lift), art, inst="viola"))
                self._pad_until = idx
                device = "harmonize · model"
            else:
                # Deterministic pads (harmonize with no model line, or an
                # ornament with nothing to say this bar): held, voice-led
                # chords, re-struck only when the harmony moves.
                if idx > self._pad_until or self._chords[(idx - 1) % len(self._chords)] != chord:
                    span = chord_span(self._chords, idx % len(self._chords))
                    self._pad_voices = voice_lead(self._pad_voices, triad(*chord))
                    dur_ms = span * self.bar_ms * 0.98
                    pad_vel = PAD_VEL * (0.5 + 0.5 * lift)
                    for v in self._pad_voices:
                        events.append(self._note(SECTION_ALL, bar_start, dur_ms, v,
                                                 pad_vel, "sustain", inst="viola"))
                    events.append(self._note(SECTION_ALL, bar_start, dur_ms, 36 + chord[0],
                                             ROOT_VEL * (0.5 + 0.5 * lift), "sustain", inst="cello"))
                    self._pad_until = idx + span - 1
                device = ("harmonize · pad" if style == "harmonize"
                          else f"{style} · tacet (pad)")
        else:
            self._pad_until = -1                 # released: next push re-voices fresh

        # The device readout the demo shows: what this bar ACTUALLY did.
        if neutral:
            self._device = "verbatim"
        elif calm >= lift:
            self._device = "hush"
        elif device:
            self._device = device
        else:
            self._device = "swelling"            # pushed, device engages next bar
        log.info("bar %d arrangement: i=%.2f %s (%d parts -> %d sections)%s",
                 idx, i9, self._device,
                 len(self.song.parts), n, f", solo={solo}" if solo else "")
        return events

    def _note(self, section: str, at: float, dur: float, midi: int, vel: float, art: str,
              inst: str | None = None) -> NoteEvent:
        return NoteEvent(id=f"n{self._id_boot}-{next(self._ids)}", section=section, at=at, dur=dur,
                         note=midi_to_name(midi), vel=round(vel, 3), art=art, inst=inst)


def _clampmidi(m: int) -> int:
    return max(36, min(84, m))
