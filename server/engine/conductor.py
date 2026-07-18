"""Conductor: the real music engine (implements the MusicEngine Protocol).

Per bar it generates the candidate accompaniments, asks the ranker which one the
latest gesture wants, and schedules that line across the sections plus the melody
on top — so there's always music, and gestures reshape the accompaniment. Pulled
by the scheduler on a lookahead, exactly like the metronome stub it replaces.
"""
from __future__ import annotations

import itertools
import logging
import secrets

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
        # Clients dedupe sched.notes by event id (lookahead windows overlap). Ids
        # must therefore stay unique ACROSS server restarts, or a tab that lives
        # through a restart silently drops every "already seen" n1, n2, … again.
        self._id_boot = secrets.token_hex(3)
        self._tracks: list[dict] = []                    # parts of a loaded MIDI (for the editor)
        self._reanchor = False                           # re-align the bar cursor on the next pull
        self._anchor_ms = 0.0                            # server-ms of bar 0 (drives the editor playhead)
        self._gesture_shift = 0                          # global octave shift (non-aimed gestures)
        self._aimed: str | None = None                   # section the wand/editor is conducting (None = all)
        self._part: dict[str, str] = {}                  # section_id -> pinned candidate (set by aiming)
        self._part_shift: dict[str, int] = {}            # section_id -> pinned octave shift

    def load_song(self, song, tracks: list[dict] | None = None) -> None:
        """Replace the song and restart it cleanly from bar 0 (a freshly dropped MIDI)."""
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
        if set_tempo:
            self.set_tempo(song.bpm)
        if reanchor:
            self._reanchor = True

    # --- editor controls ---
    def set_tempo(self, bpm: float) -> None:
        self.bpm = max(40.0, min(220.0, bpm))
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
            "candidates": list(GENERATORS),
            "gesture": self._gesture.as_dict() if self._gesture else None,
            "song": self.song.name,
            "key_root": self.song.key_root,
            "bars": len(self.song.bars),
            "tracks": tracks,
            "aimed": self._aimed,
            # Lets the editor draw a smooth playhead from its own synced clock:
            # pos16 = ((clock.serverNow() - anchor) / s16_ms) mod (n_bars*16).
            "transport": {"playing": self._playing, "anchor": self._anchor_ms,
                          "bar_ms": self.bar_ms, "s16_ms": self.s16_ms,
                          "n_bars": len(self.song.bars)},
        }

    # --- transport ---
    def on_transport(self, cmd: str, t0_ms: float | None) -> None:
        if cmd in ("start", "clicktest"):
            self._playing = True
            self._next_bar_idx = 0
            self._next_bar_start = t0_ms or 0.0
            self._anchor_ms = self._next_bar_start
            self._reanchor = False   # a fresh start supersedes a pending load re-anchor
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
        choice = heuristic.choose(heuristic.rank(self._gesture, list(GENERATORS)), self._last_choice)
        shift = heuristic.octave_shift(self._gesture)
        if self._aimed:                                  # shape only the aimed instrument
            self._part[self._aimed] = choice
            self._part_shift[self._aimed] = shift
            log.info("gesture -> %s on %s (aimed)", choice, self._aimed)
        else:                                            # shape the whole accompaniment
            self._last_choice = choice
            self._gesture_shift = shift
            log.info("gesture -> %s (all)", choice)

    def on_grab(self, kind: str, server_ms: float) -> None:
        pass  # grab edges could cut sustains; not needed for the slice

    def on_aim(self, section_id: str | None) -> None:
        self._aimed = section_id or None
        log.info("aim -> %s", self._aimed or "all")

    def on_feedback(self, value: int) -> None:
        log.info("feedback %+d (ranker training wired in P5)", value)

    # --- event pull ---
    def get_events(self, now_ms: float, until_ms: float) -> list[NoteEvent]:
        if not self._playing:
            return []
        if self._reanchor:                       # a freshly loaded song starts here
            self._next_bar_start = now_ms + 100.0
            self._next_bar_idx = 0
            self._anchor_ms = self._next_bar_start
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
        events: list[NoteEvent] = []

        # Global accompaniment choice: editor force > last non-aimed gesture > pad.
        gchoice = self._forced if (self._forced and self._forced in cands) else (self._last_choice or "sustained")
        if gchoice not in cands:
            gchoice = "sustained"
        self._last_choice = self._last_choice or gchoice
        melody_notes = [(on, dur, midi, 0.9) for (on, dur, midi) in bar.melody]

        sections = self._sections
        if not sections:
            # Laptop-only: melody + global accompaniment on the shared stream.
            self._emit(events, None, cands[gchoice], bar_start, ART.get(gchoice, "pluck"), self._gesture_shift)
            self._emit(events, None, melody_notes, bar_start, "pluck", 0)
            return events

        # Each instrument plays its own part: a pinned candidate if it was aimed +
        # shaped, else the lead plays the melody and everyone else the global
        # accompaniment. Per-section volume/mute applied in _emit.
        for i, s in enumerate(sections):
            pinned = self._part.get(s.section_id)
            if pinned is not None and pinned in cands:
                notes, art, shift = cands[pinned], ART.get(pinned, "pluck"), self._part_shift.get(s.section_id, 0)
            elif i == 0:
                notes, art, shift = melody_notes, "pluck", 0
            else:
                notes, art, shift = cands[gchoice], ART.get(gchoice, "pluck"), self._gesture_shift
            self._emit(events, s, notes, bar_start, art, shift)

        log.info("bar %d: %d sections, global=%s, pinned=%s", idx, len(sections), gchoice, self._part or "-")
        return events

    def _emit(self, events, sinfo, notes, bar_start, art, shift):
        """Emit `notes` for a section (None -> SECTION_ALL / laptop), applying its
        volume and mute."""
        if sinfo is not None and (sinfo.muted or sinfo.volume <= 0.001):
            return
        section = SECTION_ALL if sinfo is None else sinfo.section_id
        vol = 1.0 if sinfo is None else sinfo.volume
        for (on, dur, midi, vel) in notes:
            events.append(self._note(section, bar_start + on * self.s16_ms, dur * self.s16_ms,
                                     _clampmidi(midi + shift), max(0.05, vel * vol), art))

    def _part_targets(self, i: int, part) -> list[SectionInfo | None]:
        """All sections that should sound part i: every phone assigned this part's
        instrument (doubled phones play in unison), else index round-robin as a
        fallback so nothing is silent. No phones -> [None] = the laptop plays it."""
        if not self._sections:
            return [None]
        matched = [s for s in self._sections if s.instrument == part.instrument]
        return matched or [self._sections[i % len(self._sections)]]

    def _arrangement_events(self, idx: int, bar_start: float) -> list[NoteEvent]:
        """Play a loaded MIDI's parts across sections by instrument assignment
        (a part sounds on EVERY phone assigned to it — that's how two phones
        double one instrument; laptop plays all via SECTION_ALL if no phones),
        plus the gesture layer riding on the lead."""
        events: list[NoteEvent] = []
        n = len(self._sections)
        melody_info = None
        for i, part in enumerate(self.song.parts):
            targets = self._part_targets(i, part)
            if part.is_melody:
                melody_info = targets[0]
            notes = part.bars[idx % len(part.bars)]
            for sinfo in targets:
                if sinfo is not None and (sinfo.muted or sinfo.volume <= 0.001):
                    continue
                sec = SECTION_ALL if sinfo is None else sinfo.section_id
                vol = 1.0 if sinfo is None else sinfo.volume
                for (on, dur, midi, vel) in notes:
                    # Drum notes carry art="drum": the synth plays them as percussion
                    # by MIDI drum-map pitch, independent of the section's timbre.
                    art = "drum" if part.is_drum else ("sustain" if dur >= 8 else "pluck")
                    midi_out = midi if part.is_drum else _clampmidi(midi)
                    events.append(self._note(sec, bar_start + on * self.s16_ms,
                                             dur * self.s16_ms, midi_out, max(0.08, vel * vol), art))

        # Gesture/editor accompaniment layer, riding on the lead instrument.
        bar, prev = self.song.bar(idx), self.song.bar(idx - 1)
        cands = generate(bar, prev, self.song.key_root)
        gchoice = self._forced if (self._forced and self._forced in cands) else (self._last_choice or "sustained")
        if gchoice not in cands:
            gchoice = "sustained"
        self._last_choice = self._last_choice or gchoice
        if not (melody_info is not None and (melody_info.muted or melody_info.volume <= 0.001)):
            msec = SECTION_ALL if melody_info is None else melody_info.section_id
            mvol = 1.0 if melody_info is None else melody_info.volume
            for (on, dur, midi, vel) in cands[gchoice]:
                events.append(self._note(msec, bar_start + on * self.s16_ms, dur * self.s16_ms,
                                         _clampmidi(midi + self._gesture_shift), vel * 0.7 * mvol,
                                         ART.get(gchoice, "pluck")))
        log.info("bar %d arrangement: %d parts -> %d sections, overlay=%s", idx, len(self.song.parts), n, gchoice)
        return events

    def _note(self, section: str, at: float, dur: float, midi: int, vel: float, art: str) -> NoteEvent:
        return NoteEvent(id=f"n{self._id_boot}-{next(self._ids)}", section=section, at=at, dur=dur,
                         note=midi_to_name(midi), vel=round(vel, 3), art=art)


def _clampmidi(m: int) -> int:
    return max(36, min(84, m))
