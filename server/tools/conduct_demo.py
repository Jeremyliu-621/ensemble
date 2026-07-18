"""Render ONE audio file that walks a song through conductor instructions —
the make-or-break test: do the transformations sound like interpretation,
not corruption?

Treatments follow what real conductors actually change (dynamics, density,
tempo, articulation — never foreign notes):
  - HUSH: accompaniment simplifies on the beat-grid (every 2nd/4th note, the
    way a player would simplify — no random holes), softer, tempo eases back.
  - SWELL: full density, dynamics open, tempo leans forward.
  - BUILD: a 4-bar ramp of all three, landing on a climax bar with the melody
    doubled an octave up and a full downbeat chord.
  - RELEASE: back to the file, exactly as written.

Run:  python server/tools/conduct_demo.py songs/zelda-fairy.mid
Out:  songs/conducted-demo.wav (+ a printed section map with timestamps)
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

os.environ.setdefault("WM_DECISION_LOG", "0")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # server/ on path

from engine.midi_load import load_midi_bytes
from engine.theory import midi_to_name
from engine_api import NoteEvent
from render_preview import REPO, render

# Per-bar instruction: (label, thin_level, vel_mult, tempo_mult, double_melody, chord_hit)
SCORE = (
    [("neutral", 0, 1.00, 1.00, False, False)] * 8
    + [("hush",    1, 0.60, 0.94, False, False)] * 2
    + [("hush",    2, 0.50, 0.92, False, False)] * 2
    + [("swell",   0, 1.25, 1.05, False, False)] * 4
    + [("build",   1, 0.90, 0.97, False, False)]
    + [("build",   0, 1.05, 1.00, False, False)]
    + [("build",   0, 1.20, 1.03, False, False)]
    + [("CLIMAX",  0, 1.45, 1.06, True,  True)]
    + [("release", 0, 1.00, 1.00, False, False)] * 4
)


def thin(notes: list, level: int) -> list:
    """Simplify like a player would: keep the beat-grid, drop the in-betweens."""
    if level <= 0 or not notes:
        return notes
    step = 2 if level == 1 else 4
    kept = [n for n in sorted(notes) if (n[0] % step) < 0.26]
    return kept or sorted(notes)[:1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("midi", nargs="?", default=str(REPO / "songs" / "zelda-fairy.mid"))
    args = ap.parse_args()
    song, _parts = load_midi_bytes(pathlib.Path(args.midi).read_bytes(),
                                   pathlib.Path(args.midi).name)
    base_bar_ms = 60_000.0 / song.bpm * 4

    events: list[NoteEvent] = []
    t = 0.0
    nid = 0
    print("section map:")
    for bar_i, (label, level, vmul, tmul, double, chord) in enumerate(SCORE):
        bar_ms = base_bar_ms / tmul
        s16 = bar_ms / 16
        if bar_i == 0 or SCORE[bar_i - 1][0] != label:
            print(f"  {t/1000:5.1f}s  {label}")
        for part in song.parts:
            if part.is_drum:
                continue
            raw = part.bars[bar_i % len(part.bars)]
            notes = raw if part.is_melody else thin(raw, level)
            for (on, dur, midi, vel) in notes:
                nid += 1
                v = min(1.0, vel * (vmul if not part.is_melody else max(0.8, vmul)))
                events.append(NoteEvent(f"c{nid}", "all", t + on * s16, dur * s16,
                                        midi_to_name(midi), round(v, 3), "pluck",
                                        part.instrument))
                if double and part.is_melody:
                    nid += 1
                    events.append(NoteEvent(f"c{nid}", "all", t + on * s16, dur * s16,
                                            midi_to_name(min(96, midi + 12)),
                                            round(v * 0.8, 3), "pluck", "violin"))
        if chord:   # climax downbeat: the whole ensemble hits together
            bar = song.bar(bar_i % len(song.bars))
            for j, pc in enumerate(bar.chord_pcs):
                nid += 1
                events.append(NoteEvent(f"c{nid}", "all", t, s16 * 8,
                                        midi_to_name(48 + ((pc - 48) % 12) + (0 if j else -12)),
                                        0.9, "pluck", "cello" if j == 0 else "viola"))
        t += bar_ms

    out = REPO / "songs" / "conducted-demo.wav"
    render(events, t, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
