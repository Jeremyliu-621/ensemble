"""Render a song two ways: verbatim, and conducted with CLASSICAL, harmony-
first treatments — the chord movement is the star.

Sections (sized to the song):
  NEUTRAL     the file as written
  HARMONIZE   the AI's chord pads enter under the melody (progression audible)
  HUSH        beat-grid simplification, soft, eased tempo (pads stay)
  SWELL       pads + a walking bass-root line + octave shimmer
  BUILD       layers arrive bar by bar, breath before...
  CLIMAX      ...melody + octave lead over a rolled chord; texture simplifies
  RELEASE     soft re-entry, then verbatim

Chords come from the song's own harmony parts; if the file is a bare melody
(like a solo classical line), each bar's chord is inferred FROM the melody,
diatonic in the estimated key — the same harmonization the hum pipeline uses.

Run:  python server/tools/conduct_demo.py songs/<file>.mid
Out:  songs/<stem>-neutral.wav + songs/<stem>-conducted.wav
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

os.environ.setdefault("WM_DECISION_LOG", "0")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # server/ on path

from engine.midi_load import load_midi_bytes
from engine.theory import midi_to_name, scale_pcs, snap_to_scale, triad
from engine_api import NoteEvent
from render_preview import REPO, render


def bar_chords(song) -> list[tuple[int, bool]]:
    """(root_pc, minor) per bar. Real harmony parts win; a bare melody gets
    harmonized diatonically from its own downbeats."""
    has_harmony = any(not p.is_melody and not p.is_drum for p in song.parts)
    out = []
    prev = (song.key_root, (song.key_root + 4) % 12 not in scale_pcs(song.key_root))
    for bar in song.bars:
        if has_harmony:
            prev = (bar.chord_root, bar.chord_minor)
        elif bar.melody:
            root = snap_to_scale(bar.melody[0][2], song.key_root) % 12
            prev = (root, (root + 4) % 12 not in scale_pcs(song.key_root))
        out.append(prev)
    return out


def build_score(n: int) -> list[tuple]:
    """(label, thin, vel, tempo, pad, bassline, acc8, dbl, roll, luft) x n"""
    neutral = ("neutral", 0, 1.00, 1.00, 0, 0, 0, 0, 0, 0)
    score = [neutral] * max(4, n // 5)
    score += [("harmonize", 0, 1.00, 1.00, 1, 0, 0, 0, 0, 0)] * max(4, n // 5)
    score += [("hush", 1, 0.62, 0.94, 1, 0, 0, 0, 0, 0)] * max(3, n // 8)
    score += [("swell", 0, 1.10, 1.02, 1, 1, 1, 0, 0, 0)] * max(4, n // 6)
    score += [("build", 0, 0.95, 0.98, 1, 1, 0, 0, 0, 0),
              ("build", 0, 1.08, 1.01, 1, 1, 1, 0, 0, 0),
              ("build", 0, 1.18, 1.03, 1, 1, 1, 0, 0, 1),
              ("CLIMAX", 1, 1.10, 1.04, 1, 1, 0, 1, 1, 0),
              ("release", 0, 0.85, 0.97, 1, 0, 0, 0, 0, 0)]
    while len(score) < n:
        score.append(neutral)
    return score[:n]


def thin(notes: list, level: int) -> list:
    if level <= 0 or not notes:
        return notes
    step = 2 if level == 1 else 4
    kept = [x for x in sorted(notes) if (x[0] % step) < 0.26]
    return kept or sorted(notes)[:1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("midi", nargs="?", default=str(REPO / "songs" / "zelda-fairy.mid"))
    args = ap.parse_args()
    path = pathlib.Path(args.midi)
    song, _parts = load_midi_bytes(path.read_bytes(), path.name)
    stem = path.stem
    base_bar_ms = 60_000.0 / song.bpm * 4
    chords = bar_chords(song)
    n_windows = len(song.bars)
    score = build_score(n_windows)

    def render_pass(conducted: bool, out_name: str) -> None:
        events: list[NoteEvent] = []
        t = 0.0
        nid = 0

        def emit(at, dur, midi, vel, inst):
            nonlocal nid
            nid += 1
            events.append(NoteEvent(f"c{nid}", "all", at, dur,
                                    midi_to_name(max(24, min(96, midi))),
                                    round(min(1.0, vel), 3), "pluck", inst))

        if conducted:
            print(f"section map ({out_name}):")
        for bar_i in range(n_windows):
            (label, level, vmul, tmul, pad, bassline, acc8, dbl, roll, luft) = (
                score[bar_i] if conducted else ("neutral", 0, 1.0, 1.0, 0, 0, 0, 0, 0, 0))
            bar_ms = base_bar_ms / tmul
            s16 = bar_ms / 16
            if conducted and (bar_i == 0 or score[bar_i - 1][0] != label):
                print(f"  {t/1000:5.1f}s  {label}")
            root, minor = chords[bar_i]
            for part in song.parts:
                if part.is_drum:
                    continue
                raw = part.bars[bar_i % len(part.bars)]
                notes = raw if part.is_melody else thin(raw, level)
                for (on, dur, midi, vel) in notes:
                    if luft and on >= 14:
                        continue
                    acc_mul = min(vmul, 0.75) if dbl else vmul
                    v = vel * (max(0.9, vmul) if part.is_melody else acc_mul)
                    emit(t + on * s16, dur * s16, midi, v, part.instrument)
                    if part.is_melody and dbl:
                        emit(t + on * s16, dur * s16, midi + 12, v * 0.7, "violin")
                    if not part.is_melody and acc8 and midi >= 55:
                        emit(t + on * s16, dur * s16, midi + 12, v * 0.45, part.instrument)
            if pad:      # the star: the chord progression, voiced as soft strings
                for j, pc in enumerate(sorted(triad(root, minor))):
                    emit(t, bar_ms * 0.96, 55 + ((pc - 7) % 12) + (12 if j == 2 else 0),
                         0.27, "viola")
            if bassline:  # classical root-and-fifth movement in the cello
                emit(t, bar_ms * 0.5, 36 + root, 0.5, "cello")
                emit(t + bar_ms * 0.5, bar_ms * 0.5, 36 + (root + 7) % 12 + (12 if root > 7 else 0),
                     0.42, "cello")
            if roll:
                for j, (off, inst, v) in enumerate([(-24, "cello", 0.8), (-12, "viola", 0.7),
                                                    (0, "harp", 0.65), (12, "harp", 0.55)]):
                    emit(t + j * 35.0, bar_ms * 0.9, 60 + root + off, v, inst)
            t += bar_ms
        render(events, t, REPO / "songs" / out_name)

    render_pass(False, f"{stem}-neutral.wav")
    render_pass(True, f"{stem}-conducted.wav")
    return 0


if __name__ == "__main__":
    sys.exit(main())
