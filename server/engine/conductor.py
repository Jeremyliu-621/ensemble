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

from config import MIN_LEAD_MS
from engine.candidates import ART, GENERATORS, generate
from engine.song import builtin_song
from engine.theory import midi_to_name, voice_triad
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
        self._model = RemoteModel()                     # trained policy (WM_MODEL_URL); optional
        self._barmodel = RemoteBarModel()               # trained line writer (WM_BARMODEL_URL)
        self._decision: Decision | None = None          # the policy's active answer, until the next gesture
        self._last_source = "heuristic"
        self._datalog = DecisionLog()
        self._sections: list[SectionInfo] = []
        self._cancels: list[CancelSpec] = []
        self._ids = itertools.count(1)
        self._tracks: list[dict] = []                    # parts of a loaded MIDI (for the editor)
        self._reanchor = False                           # re-align the bar cursor on the next pull

    def load_song(self, song, tracks: list[dict] | None = None) -> None:
        self.song = song
        self._tracks = tracks or []
        self._part_map = None                   # a new song gets a fresh arrangement
        self.set_tempo(song.bpm)
        self._last_choice = None
        self._reanchor = True   # start the new song cleanly at the next scheduler tick
        log.info("loaded song '%s': %d bars, key=%d, %d parts",
                 song.name, len(song.bars), song.key_root, len(self._tracks))

    # --- editor controls ---
    def set_tempo(self, bpm: float) -> None:
        self.bpm = max(40.0, min(220.0, bpm))
        self.bar_ms = 60_000.0 / self.bpm * 4
        self.s16_ms = self.bar_ms / 16

    def set_forced(self, candidate: str | None) -> None:
        self._forced = candidate if candidate and candidate != "auto" else None

    def status(self) -> dict:
        tracks = self._tracks
        if not tracks:   # built-in song: expose its melody as a viewable track
            roll = [[b, on, dur, pitch]
                    for b, bar in enumerate(self.song.bars) for (on, dur, pitch) in bar.melody]
            tracks = [{"name": "melody", "instrument": "synth", "is_drum": False,
                       "is_melody": True, "note_count": len(roll), "roll": roll}]
        return {
            "playing": self._playing,
            "bpm": round(self.bpm),
            "forced": self._forced or "auto",
            "last_choice": self._last_choice,
            "decision_source": self._last_source,
            "candidates": list(GENERATORS) + (["generated"] if self._barmodel.configured else []),
            "training_rows": self._datalog.rows,
            "gesture": self._gesture.as_dict() if self._gesture else None,
            "song": self.song.name,
            "key_root": self.song.key_root,
            "bars": len(self.song.bars),
            "tracks": tracks,
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
        "flick":      GestureFeatures(energy=0.6, size=0.3, duration=0.3),
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

    def _gesture_in(self, features: GestureFeatures, t_end_ms: float) -> None:
        self._gesture = features
        log.info("gesture -> %s", {k: round(v, 2) for k, v in features.as_dict().items()})
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
        if not self._playing:
            return
        eighth = self.bar_ms / 8
        bar_start = self._next_bar_start - self.bar_ms
        at = bar_start + math.ceil((t_end_ms + 60.0 - bar_start) / eighth) * eighth
        chord = self.song.bar(max(0, self._next_bar_idx - 1)).chord_pcs
        g = self._gesture
        if g is not None and g.energy > 0.65 and 0.0 < g.duration < 0.6:   # the sting
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

    def _take_generated(self, idx: int, cands: dict) -> None:
        """Add the bar model's prefetched line (if one landed for this bar) and
        request TWO bars ahead — measured serving latency for a composed bar is
        ~4.6s, so the request needs two bars of playing time (~4.8s at 100 BPM)
        to land. A faster serving host can drop this back to one."""
        line = self._barmodel.take(idx)
        if line:
            cands["generated"] = line
        if self._barmodel.configured:
            tgt, prev = self.song.bar(idx + 2), self.song.bar(idx + 1)
            self._barmodel.prefetch(idx + 2, build_bar_context(
                key_root=self.song.key_root, bpm=self.bpm,
                chord_root=tgt.chord_root, chord_minor=tgt.chord_minor,
                style=style_for(self._gesture),
                melody=tgt.melody, prev_melody=prev.melody), self.song.key_root)

    # --- bar generation ---
    def _bar_events(self, idx: int, bar_start: float) -> list[NoteEvent]:
        self._arc_now = self._arc_step()
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

        log.info("bar %d -> %s [%s] (%d notes, shift %+d, %d sections)",
                 idx, choice, decision.source, len(responder), shift, n)
        return events

    # How much of each loaded-MIDI part survives, per chosen candidate class.
    # This is how the conductor bends the ACTUAL arrangement, not just an overlay:
    # rest thins to melody-only, sustained is a sparse pad, rhythmic_dense opens
    # everything up.
    _DENSITY = {"rest": 0.0, "sustained": 0.4, "delayed": 0.6, "lower_imitation": 0.8,
                "contrary_motion": 0.8, "generated": 0.8, "rhythmic_dense": 1.0}

    def _shape(self, notes: list, decision: Decision, is_drum: bool) -> list:
        """Bend one part's bar to the conductor: candidate class sets density,
        gesture energy drives velocity, the decision's octave shift moves the
        register. Drums thin too (a calm room drops toward the kick) but never
        transpose. Kept notes are the structurally strongest: long, loud, on-beat."""
        keep = self._DENSITY.get(decision.candidate, 0.8)
        arc_mult, arc_floor, _climax = self._arc_now
        if arc_floor > 0.0:                      # a build arc overrides thinning upward
            keep = max(keep, arc_floor)
        if keep <= 0.0 or not notes:
            return []
        if keep >= 1.0:
            kept = list(notes)
        else:
            ranked = sorted(notes, key=lambda nt: nt[1] * 2 + nt[3] * 4 + (2 if nt[0] % 4 == 0 else 0),
                            reverse=True)
            kept = sorted(ranked[:max(1, round(len(notes) * keep))])
        shift = 0 if is_drum else decision.semitones()
        vel_scale = (0.75 + 0.45 * (self._gesture.energy if self._gesture else 0.0)) * arc_mult
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
        """Play a loaded MIDI's parts distributed across sections — bent to the
        conductor via _shape (the melody part is the song's identity and plays
        verbatim), plus the gesture layer riding on the lead. The LLM arranger's
        part map routes parts when present; aiming the wand at a phone SOLOS it
        (every other phone mutes for the bar). Drum parts play through the
        synth's percussion voice (art="drum")."""
        bar, prev = self.song.bar(idx), self.song.bar(idx - 1)
        cands = generate(bar, prev, self.song.key_root)
        self._take_generated(idx, cands)
        decision = self._decide(idx, cands)
        choice, shift = decision.candidate, decision.semitones()

        events: list[NoteEvent] = []
        n = len(self._sections)
        solo = self._aim if any(s.section_id == self._aim for s in self._sections) else None
        melody_sec = solo or SECTION_ALL
        for i, part in enumerate(self.song.parts):
            if self._part_map and i in self._part_map and any(
                    s.section_id == self._part_map[i] for s in self._sections):
                sec = self._part_map[i]
            else:
                sec = SECTION_ALL if n == 0 else self._sections[i % n].section_id
            if part.is_melody and not solo:
                melody_sec = sec
            if solo and sec != solo:
                continue                         # isolation: only the aimed phone sounds
            raw = part.bars[idx % len(part.bars)]
            notes = raw if part.is_melody else self._shape(raw, decision, part.is_drum)
            for (on, dur, midi, vel) in notes:
                art = "drum" if part.is_drum else ("sustain" if dur >= 8 else "pluck")
                # Drum-map pitches are identifiers, not pitches - never clamp/fold them.
                midi_out = midi if part.is_drum else _clampmidi(midi)
                events.append(self._note(sec, bar_start + on * self.s16_ms,
                                         dur * self.s16_ms, midi_out, max(0.12, vel), art))

        for (on, dur, midi, vel) in cands[choice]:
            events.append(self._note(melody_sec, bar_start + on * self.s16_ms,
                                     dur * self.s16_ms, _clampmidi(midi + shift), vel * 0.7,
                                     ART.get(choice, "pluck")))
        if self._arc_now[2]:                     # the arc lands: a crash on the downbeat
            events.append(self._note(SECTION_ALL, bar_start, self.s16_ms * 2, 49, 0.95, "drum"))
        log.info("bar %d arrangement: %d parts -> %d sections, shape=%s%s",
                 idx, len(self.song.parts), n, choice, f", solo={solo}" if solo else "")
        return events

    def _note(self, section: str, at: float, dur: float, midi: int, vel: float, art: str) -> NoteEvent:
        return NoteEvent(id=f"n{next(self._ids)}", section=section, at=at, dur=dur,
                         note=midi_to_name(midi), vel=round(vel, 3), art=art)


def _clampmidi(m: int) -> int:
    return max(36, min(84, m))
