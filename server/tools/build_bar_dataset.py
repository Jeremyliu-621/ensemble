"""Build a Freesolo SFT dataset for the bar-line (music editing) model.

Sources, merged:
  1. Synthetic theory pairs — random keys, diatonic chords, and generated
     melodies run through the six rule-based generators, labeled by style
     (dense/calm/counter/echo/free). Teaches the output format and baseline
     musicality across the whole input space.
  2. Real arrangements (--midi-dir DIR of .mid files) — every non-drum,
     non-melody part of every file becomes (context, that part's actual bar)
     pairs via the same loader the server uses. Teaches phrasing and voicing
     no rule can. Drop a folder of MIDIs in the style you want the orchestra
     to speak.

Output: freesolo/barline/dataset/{train,eval}.jsonl
Run:  python server/tools/build_bar_dataset.py [--n 800] [--midi-dir songs/]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # server/ on path

from engine import candidates as C
from engine.harmony import PAD_VEL, ROOT_VEL, voice_lead
from engine.song import BarData
from engine.theory import scale_notes, triad
from ml.barmodel import sanitize_line
from ml.schema import bar_prompt_for, build_bar_context

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def harmonize(bar: BarData, prev: BarData, key: int) -> list:
    """The approved treatment: the bar's chord as a held, voice-led pad + root.
    THE style the model must master — chord theory, from the listening tests."""
    notes = [(0, 16, v, PAD_VEL) for v in voice_lead(None, bar.chord_pcs)]
    notes.append((0, 16, 48 + bar.chord_root, ROOT_VEL))
    return notes


# style -> the rule-based generator whose output teaches it (also used by
# tools/mock_model.py to impersonate the trained model)
STYLE_GEN = {
    "harmonize": harmonize,
    "dense": C.rhythmic_dense,
    "calm": C.sustained,
    "counter": C.contrary_motion,
    "echo": C.delayed,
    "free": C.lower_imitation,
}

DEGREES = [(0, False), (2, True), (4, True), (5, False), (7, False), (9, True)]  # I ii iii IV V vi

RHYTHMS = [
    [(0, 4), (4, 4), (8, 4), (12, 4)],                                        # quarters
    [(0, 2), (2, 2), (4, 2), (6, 2), (8, 2), (10, 2), (12, 2), (14, 2)],      # eighths
    [(0, 4), (6, 2), (8, 4), (14, 2)],                                        # syncopated
    [(0, 8), (8, 4), (12, 4)],                                                # half + quarters
    [(0, 6), (6, 6), (12, 4)],                                                # dotted
]


def synth_melody(rng: random.Random, key: int) -> list:
    """A random-walk melody over the key's scale, on a sampled rhythm."""
    notes = scale_notes(key, 60, 84)
    i = min(range(len(notes)), key=lambda j: abs(notes[j] - 72))
    out = []
    for (on, dur) in rng.choice(RHYTHMS):
        i = max(0, min(len(notes) - 1, i + rng.randint(-2, 2)))
        out.append((on, dur, notes[i]))
    return out


def synth_bar(rng: random.Random, key: int) -> BarData:
    off, minor = rng.choice(DEGREES)
    root = (key + off) % 12
    return BarData(root, minor, triad(root, minor), synth_melody(rng, key))


def synth_rows(n: int, rng: random.Random) -> list[dict]:
    rows: list[dict] = []
    while len(rows) < n:
        key = rng.randrange(12)
        prev, bar = synth_bar(rng, key), synth_bar(rng, key)
        bpm = rng.choice([80, 90, 100, 110, 120, 140, 160])
        for style, gen in STYLE_GEN.items():
            notes = gen(bar, prev, key)
            if not notes:
                continue
            ctx = build_bar_context(key_root=key, bpm=bpm, chord_root=bar.chord_root,
                                    chord_minor=bar.chord_minor, style=style,
                                    melody=bar.melody, prev_melody=prev.melody)
            rows.append({"context": ctx,
                         "notes": [[o, d, m, round(v, 2)] for (o, d, m, v) in notes]})
    return rows[:n]


def _classify(notes: list) -> str:
    n = len(notes)
    durs = [d for (_, d, _, _) in notes]
    if n >= 6:
        return "dense"
    if n <= 3 and max(durs) >= 8:
        return "calm"
    return "free"


def _clean(notes: list) -> list:
    """Grid/range clamp for real-MIDI labels. Deliberately does NOT snap to the
    estimated key — real phrasing may be chromatic; the server snaps at runtime."""
    out = []
    for (on, dur, midi, vel) in notes[:16]:
        on = int(max(0, min(15, on)))
        out.append([on, int(max(1, min(16 - on, dur))), int(max(30, min(90, midi))),
                    round(max(0.1, min(1.0, vel)), 2)])
    return out


def midi_rows(midi_dir: str) -> list[dict]:
    from engine.midi_load import load_midi_bytes
    rows: list[dict] = []
    for path in sorted(pathlib.Path(midi_dir).glob("**/*.mid")):
        try:
            song, _ = load_midi_bytes(path.read_bytes(), path.name)
        except Exception as e:  # noqa: BLE001 - a bad file just gets skipped
            print(f"  skip {path.name}: {e}")
            continue
        for part in song.parts:
            if part.is_drum or part.is_melody:
                continue
            for idx, bar_notes in enumerate(part.bars):
                if not bar_notes or len(bar_notes) > 16:
                    continue
                bar, prev = song.bar(idx), song.bar(idx - 1)
                ctx = build_bar_context(key_root=song.key_root, bpm=song.bpm,
                                        chord_root=bar.chord_root, chord_minor=bar.chord_minor,
                                        style=_classify(bar_notes), melody=bar.melody,
                                        prev_melody=prev.melody)
                rows.append({"context": ctx, "notes": _clean(bar_notes)})
    return rows


def to_freesolo(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        label = {"notes": r["notes"]}
        assert sanitize_line(label, r["context"]["key"]) is not None, f"unplayable label: {label}"
        out.append({"input": bar_prompt_for(r["context"]), "output": json.dumps(label)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=800, help="synthetic (key,chord,melody) seeds; x5 styles")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--midi-dir", default=None, help="folder of .mid files to harvest")
    ap.add_argument("--out", default=str(REPO / "freesolo" / "barline" / "dataset"))
    args = ap.parse_args()

    rng = random.Random(args.seed)
    harvested = midi_rows(args.midi_dir) if args.midi_dir else []
    synthetic = synth_rows(args.n * len(STYLE_GEN), rng)
    rows = to_freesolo(harvested + synthetic)
    rng.shuffle(rows)
    cut = max(1, len(rows) // 20)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, part in (("eval.jsonl", rows[:cut]), ("train.jsonl", rows[cut:])):
        with open(out / name, "w", encoding="utf-8") as f:
            for row in part:
                f.write(json.dumps(row) + "\n")

    print(f"midi: {len(harvested)} rows, synthetic: {len(synthetic)} rows")
    print(f"train: {len(rows) - cut} rows -> {out / 'train.jsonl'}")
    print(f"eval:  {cut} rows -> {out / 'eval.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
