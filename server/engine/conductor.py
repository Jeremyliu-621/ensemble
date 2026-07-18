"""Conductor: the real music engine (implements the MusicEngine Protocol).

Per bar it generates the candidate accompaniments, asks the ranker which one the
latest gesture wants, and schedules that line across the sections plus the melody
on top — so there's always music, and gestures reshape the accompaniment. Pulled
by the scheduler on a lookahead, exactly like the metronome stub it replaces.
"""
from __future__ import annotations

import itertools
import logging

from engine.candidates import ART, GENERATORS, generate
from engine.song import builtin_song
from engine.theory import midi_to_name
from engine_api import CancelSpec, GestureWindow, NoteEvent, SectionInfo
from gestures.features import GestureFeatures, extract_features
from ml import heuristic
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
        self._sections: list[SectionInfo] = []
        self._cancels: list[CancelSpec] = []
        self._ids = itertools.count(1)
        self._tracks: list[dict] = []                    # parts of a loaded MIDI (for the editor)
        self._reanchor = False                           # re-align the bar cursor on the next pull

    def load_song(self, song, tracks: list[dict] | None = None) -> None:
        self.song = song
        self._tracks = tracks or []
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
            "candidates": list(GENERATORS),
            "gesture": self._gesture.as_dict() if self._gesture else None,
            "song": self.song.name,
            "key_root": self.song.key_root,
            "bars": len(self.song.bars),
            "tracks": tracks,
        }

    # --- transport ---
    def on_transport(self, cmd: str, t0_ms: float | None) -> None:
        if cmd in ("start", "clicktest"):
            self._playing = True
            self._next_bar_idx = 0
            self._next_bar_start = t0_ms or 0.0
            log.info("transport start @%.0f  bar=%.0fms (%.0f BPM)",
                     self._next_bar_start, self.bar_ms, self.song.bpm)
        elif cmd in ("stop", "allnotesoff"):
            self._playing = False
            self._cancels.append(CancelSpec(allnotesoff=True))

    # --- inputs ---
    def on_sections_changed(self, sections: list[SectionInfo]) -> None:
        self._sections = [s for s in sections if s.ready]

    def on_gesture(self, window: GestureWindow) -> None:
        self._gesture = extract_features(window)
        log.info("gesture -> %s", {k: round(v, 2) for k, v in self._gesture.as_dict().items()})

    def on_grab(self, kind: str, server_ms: float) -> None:
        pass  # grab edges could cut sustains; not needed for the slice

    def on_aim(self, section_id: str | None) -> None:
        pass

    def on_feedback(self, value: int) -> None:
        log.info("feedback %+d (ranker training wired in P5)", value)

    # --- event pull ---
    def get_events(self, now_ms: float, until_ms: float) -> list[NoteEvent]:
        if not self._playing:
            return []
        if self._reanchor:                       # a freshly loaded song starts here
            self._next_bar_start = now_ms + 100.0
            self._next_bar_idx = 0
            self._reanchor = False
        events: list[NoteEvent] = []
        while self._next_bar_start <= until_ms:
            if self._next_bar_start >= now_ms - self.bar_ms:
                events.extend(self._bar_events(self._next_bar_idx, self._next_bar_start))
            self._next_bar_start += self.bar_ms
            self._next_bar_idx += 1
        return events

    def get_cancels(self) -> list[CancelSpec]:
        out, self._cancels = self._cancels, []
        return out

    # --- bar generation ---
    def _bar_events(self, idx: int, bar_start: float) -> list[NoteEvent]:
        if self.song.parts:                      # a loaded MIDI: play its arrangement
            return self._arrangement_events(idx, bar_start)
        bar = self.song.bar(idx)
        prev = self.song.bar(idx - 1)
        cands = generate(bar, prev, self.song.key_root)

        # Editor override wins; otherwise the ranker picks from the gesture.
        if self._forced and self._forced in cands:
            choice = self._forced
        else:
            scores = heuristic.rank(self._gesture, list(cands.keys()))
            choice = heuristic.choose(scores, self._last_choice)
        self._last_choice = choice
        shift = heuristic.octave_shift(self._gesture)

        responder = cands[choice]
        art = ART.get(choice, "pluck")
        events: list[NoteEvent] = []

        # Distribute parts so multiple phones are genuinely different instruments:
        #   2+ sections -> section[0] plays melody, the rest play the accompaniment
        #   1 section   -> it plays both
        #   0 sections  -> laptop (stage) plays everything via SECTION_ALL
        n = len(self._sections)
        if n >= 2:
            melody_sec = self._sections[0].section_id
            responder_secs = [s.section_id for s in self._sections[1:]]
        elif n == 1:
            melody_sec = self._sections[0].section_id
            responder_secs = [melody_sec]
        else:
            melody_sec = SECTION_ALL
            responder_secs = [SECTION_ALL]

        for (on, dur, midi, vel) in responder:
            at, d, note = bar_start + on * self.s16_ms, dur * self.s16_ms, _clampmidi(midi + shift)
            for sec in responder_secs:
                events.append(self._note(sec, at, d, note, vel, art))

        for (on, dur, midi) in bar.melody:
            events.append(self._note(melody_sec, bar_start + on * self.s16_ms,
                                     dur * self.s16_ms, midi, 0.9, "pluck"))

        log.info("bar %d -> %s (%d notes, shift %+d, %d sections)", idx, choice, len(responder), shift, n)
        return events

    def _arrangement_events(self, idx: int, bar_start: float) -> list[NoteEvent]:
        """Play a loaded MIDI's parts distributed across sections (round-robin;
        laptop plays all via SECTION_ALL if no phones), plus the gesture layer
        riding on the lead. Drums are skipped until we have a percussion voice."""
        events: list[NoteEvent] = []
        n = len(self._sections)
        melody_sec = SECTION_ALL
        for i, part in enumerate(self.song.parts):
            sec = SECTION_ALL if n == 0 else self._sections[i % n].section_id
            if part.is_melody:
                melody_sec = sec
            for (on, dur, midi, vel) in part.bars[idx % len(part.bars)]:
                # Drum notes carry art="drum": the synth plays them as percussion by
                # MIDI drum-map pitch, independent of the section's instrument timbre.
                art = "drum" if part.is_drum else ("sustain" if dur >= 8 else "pluck")
                midi_out = midi if part.is_drum else _clampmidi(midi)
                events.append(self._note(sec, bar_start + on * self.s16_ms,
                                         dur * self.s16_ms, midi_out, max(0.12, vel), art))

        # Gesture/editor layer: a candidate built from the lead, riding on top.
        bar, prev = self.song.bar(idx), self.song.bar(idx - 1)
        cands = generate(bar, prev, self.song.key_root)
        if self._forced and self._forced in cands:
            choice = self._forced
        else:
            choice = heuristic.choose(heuristic.rank(self._gesture, list(cands.keys())), self._last_choice)
        self._last_choice = choice
        shift = heuristic.octave_shift(self._gesture)
        for (on, dur, midi, vel) in cands[choice]:
            events.append(self._note(melody_sec, bar_start + on * self.s16_ms,
                                     dur * self.s16_ms, _clampmidi(midi + shift), vel * 0.7,
                                     ART.get(choice, "pluck")))
        log.info("bar %d arrangement: %d parts -> %d sections, overlay=%s", idx, len(self.song.parts), n, choice)
        return events

    def _note(self, section: str, at: float, dur: float, midi: int, vel: float, art: str) -> NoteEvent:
        return NoteEvent(id=f"n{next(self._ids)}", section=section, at=at, dur=dur,
                         note=midi_to_name(midi), vel=round(vel, 3), art=art)


def _clampmidi(m: int) -> int:
    return max(36, min(84, m))
