"""Harmony remix: the whole song, exactly as written, plus a chord layer —
nothing else. The focused test of 'add harmony without ruining it'.

The craft points:
  - The pad re-strikes ONLY when the harmony changes (holds through repeated
    chords) — no droning against the song's movement.
  - Voice leading: each chord takes the inversion nearest the previous one,
    so the pad glides between chords instead of jumping.
  - A soft cello root underneath moves with it.

Run:  python server/tools/harmony_remix.py songs/zelda-fairy.mid
Out:  songs/<stem>-harmony.wav (+ <stem>-neutral.wav for A/B)
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


def voice_lead(prev_voices: list[int] | None, pcs: tuple[int, ...]) -> list[int]:
    """Each voice moves to the NEAREST pitch of the new chord (classic voice
    leading). First chord: close position around G3."""
    if prev_voices is None:
        base = 55
        return sorted(base + ((pc - base) % 12) for pc in pcs)
    voices = []
    for v, pc in zip(prev_voices, sorted(pcs)):
        candidates = [pc + 12 * o for o in range(3, 7)]
        voices.append(min(candidates, key=lambda c: abs(c - v)))
    return sorted(voices)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("midi", nargs="?", default=str(REPO / "songs" / "zelda-fairy.mid"))
    args = ap.parse_args()
    path = pathlib.Path(args.midi)
    song, _parts = load_midi_bytes(path.read_bytes(), path.name)
    stem = path.stem
    bar_ms = 60_000.0 / song.bpm * 4
    s16 = bar_ms / 16
    chords = bar_chords(song)
    n = len(song.bars)

    def render_pass(with_harmony: bool, out_name: str) -> None:
        events: list[NoteEvent] = []
        nid = 0

        def emit(at, dur, midi, vel, inst):
            nonlocal nid
            nid += 1
            events.append(NoteEvent(f"h{nid}", "all", at, dur,
                                    midi_to_name(max(24, min(96, midi))),
                                    round(min(1.0, vel), 3), "pluck", inst))

        # The song itself, verbatim.
        for bar_i in range(n):
            t = bar_i * bar_ms
            for part in song.parts:
                if part.is_drum:
                    continue
                for (on, dur, midi, vel) in part.bars[bar_i % len(part.bars)]:
                    emit(t + on * s16, dur * s16, midi, vel, part.instrument)

        if with_harmony:
            # Group consecutive bars sharing a chord -> one held, voice-led pad.
            voices = None
            i = 0
            while i < n:
                j = i
                while j + 1 < n and chords[j + 1] == chords[i]:
                    j += 1
                root, minor = chords[i]
                voices = voice_lead(voices, triad(root, minor))
                start, dur = i * bar_ms, (j - i + 1) * bar_ms * 0.98
                for v in voices:
                    emit(start, dur, v, 0.24, "viola")
                emit(start, dur, 36 + root, 0.3, "cello")   # the root moves with it
                i = j + 1

        render(events, n * bar_ms, REPO / "songs" / out_name)

    render_pass(False, f"{stem}-neutral.wav")
    render_pass(True, f"{stem}-harmony.wav")
    print("chords:", " ".join(f"{'m' if m else ''}{r}" for r, m in chords))
    return 0


if __name__ == "__main__":
    sys.exit(main())
