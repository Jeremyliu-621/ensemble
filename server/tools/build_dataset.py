"""Build a Freesolo SFT dataset for the accompaniment decision model.

Two sources, merged:
  1. Harvested play sessions — server/data/decisions/*.jsonl, written live by
     the conductor: the rows a human actually conducted. Thumbs-down rows are
     dropped, thumbs-up rows repeated 3x.
  2. Synthetic coverage — a seeded sweep of the gesture-feature space labeled
     by the heuristic ranker, so the model sees the whole input range.

TASTE_RULES then overrides labels where your musical judgment disagrees with
the heuristic — that's where the trained model becomes yours instead of a copy
of hand-written rules. Edit them freely; they win over both sources.

Output: freesolo/dataset/train.jsonl + eval.jsonl in Freesolo's required
{"input": ..., "output": ...} row format.

Run:  python server/tools/build_dataset.py [--n 3000] [--seed 7]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # server/ on path

import config
from gestures.features import GestureFeatures
from ml.policy import heuristic_decision
from ml.schema import CANDIDATES, parse_decision, prompt_for

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

# --- Your musical taste, as label overrides ---------------------------------
# Each rule: (condition on the context, label override); rules run in order and
# the last match wins over both the heuristic label and harvested rows.
# Conditions see the context dict (ml/schema.build_context): key, bpm,
# chord{root,minor}, prev, gesture{energy,size,vertical,rotation,duration}.
# Examples to start from (disabled):
#   (lambda c: c["gesture"] and c["gesture"]["rotation"] > 0.8 and c["chord"]["minor"],
#    {"candidate": "delayed", "octave_shift": -1}),      # violent minor twist -> echo, low
#   (lambda c: c["gesture"] and c["gesture"]["energy"] < 0.05 and c["bpm"] > 140,
#    {"candidate": "rest", "octave_shift": 0}),          # dead-still at speed -> thin out
TASTE_RULES: list = []


def apply_taste(context: dict, label: dict) -> dict:
    for cond, override in TASTE_RULES:
        try:
            if cond(context):
                label = dict(label, **override)
        except (KeyError, TypeError):
            pass
    return label


def synth_rows(n: int, rng: random.Random) -> list[dict]:
    """Seeded sweep of the input space, labeled by the heuristic ranker."""
    rows = []
    for _ in range(n):
        gesture = None if rng.random() < 0.08 else GestureFeatures(
            energy=round(rng.random(), 2),
            size=round(rng.random(), 2),
            vertical=round(rng.uniform(-1, 1), 2),
            rotation=round(rng.random(), 2),
            duration=round(rng.uniform(0.1, 2.0), 2),
        )
        prev = rng.choice(CANDIDATES + [None])
        context = {
            "key": rng.randrange(12),
            "bpm": rng.choice([80, 90, 100, 110, 120, 140, 160]),
            "chord": {"root": rng.randrange(12), "minor": rng.random() < 0.4},
            "prev": prev,
            "gesture": gesture.as_dict() if gesture else None,
        }
        d = heuristic_decision(gesture, prev)
        label = {"candidate": d.candidate, "octave_shift": d.octave_shift}
        rows.append({"context": context, "label": apply_taste(context, label)})
    return rows


def harvest_rows() -> list[dict]:
    """Rows a human actually conducted, weighted by wand feedback."""
    rows: list[dict] = []
    if not config.DECISIONS_DIR.exists():
        return rows
    for path in sorted(config.DECISIONS_DIR.glob("*.jsonl")):
        decisions: dict[int, dict] = {}
        weight: dict[int, int] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if "candidate" in row:
                decisions[row["id"]] = row
            elif "feedback" in row and row.get("for_id") in decisions:
                weight[row["for_id"]] = 3 if row["feedback"] > 0 else 0
        for rid, row in decisions.items():
            label = apply_taste(row["context"], {"candidate": row["candidate"],
                                                 "octave_shift": row["octave_shift"]})
            rows.extend([{"context": row["context"], "label": label}] * weight.get(rid, 1))
    return rows


def to_freesolo(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        output = json.dumps(r["label"], separators=(",", ":"))
        assert parse_decision(output) is not None, f"invalid label: {output}"
        out.append({"input": prompt_for(r["context"]), "output": output})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=3000, help="synthetic rows to generate")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(REPO / "freesolo" / "dataset"))
    args = ap.parse_args()

    rng = random.Random(args.seed)
    harvested = harvest_rows()
    synthetic = synth_rows(args.n, rng)
    rows = to_freesolo(harvested + synthetic)
    rng.shuffle(rows)
    cut = max(1, len(rows) // 20)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, part in (("eval.jsonl", rows[:cut]), ("train.jsonl", rows[cut:])):
        with open(out / name, "w", encoding="utf-8") as f:
            for row in part:
                f.write(json.dumps(row) + "\n")

    print(f"harvested {len(harvested)} rows, synthesized {len(synthetic)} "
          f"({len(TASTE_RULES)} taste rules)")
    print(f"train: {len(rows) - cut} rows -> {out / 'train.jsonl'}")
    print(f"eval:  {cut} rows -> {out / 'eval.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
