"""Freesolo environment for the BAR-LINE model (context -> a bar of notes).

The task: given key/chord/melody + a style directive (prompt built by
server/ml/schema.bar_prompt_for), write one bar of accompaniment as
{"notes": [[onset, dur, midi, velocity], ...]} on the 16th grid.

Reward (0..1) — sync by hand with server/ml/barmodel.sanitize_line:
  0.25  format: exactly {"notes": [...]}, rows are 4-number lists
  0.20  grid: onset 0-15, dur >= 1, onset+dur <= 16, <= 16 notes
  0.20  in key (major scale of the context key)
  0.10  register (E3..B4 accompaniment window, MIDI 52-71)
  0.15  style match (dense/calm/counter/echo/free heuristics)
  0.10  melody clearance (no semitone/tritone clash on overlapping notes)

Dataset sidecar: dataset/{train,eval}.jsonl from server/tools/build_bar_dataset.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from freesolo.datasets import TaskExample
from freesolo.environments import EnvironmentSingleTurn, RewardResult

DEFAULT_DATASET_PATH = Path(__file__).parent / "dataset" / "train.jsonl"

MAJOR = [0, 2, 4, 5, 7, 9, 11]
REG_LO, REG_HI = 52, 71
CLASH = {1, 6, 11}


def load_jsonl(path):
    with Path(path).open() as f:
        return [json.loads(line) for line in f if line.strip()]


def _pcs(root: int) -> set[int]:
    return {(root + i) % 12 for i in MAJOR}


def _context_from(prompt: str) -> dict | None:
    marker = "Context: "
    i = prompt.rfind(marker)
    if i < 0:
        return None
    try:
        obj = json.loads(prompt[i + len(marker):].strip())
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _rows_of(obj) -> list | None:
    if not isinstance(obj, dict) or set(obj) != {"notes"} or not isinstance(obj["notes"], list):
        return None
    rows = []
    for r in obj["notes"]:
        if (isinstance(r, list) and len(r) == 4
                and all(isinstance(v, (int, float)) for v in r)):
            rows.append(r)
    return rows


def _grid_ok(r: list) -> bool:
    on, dur = r[0], r[1]
    return 0 <= on <= 15 and dur >= 1 and on + dur <= 16


def _style_score(rows: list, context: dict) -> float:
    style = context.get("style", "free")
    n = len(rows)
    mean_dur = sum(r[1] for r in rows) / n
    if style == "harmonize":                 # THE style: chord tones, held, few
        ch = context.get("chord") or {}
        root = int(ch.get("root", 0))
        pcs = {(root + o) % 12 for o in ((0, 3, 7) if ch.get("minor") else (0, 4, 7))}
        in_chord = sum(1 for r in rows if int(r[2]) % 12 in pcs) / n
        long_frac = sum(1 for r in rows if r[1] >= 8) / n
        if in_chord >= 0.75 and long_frac >= 0.6 and n <= 5:
            return 1.0
        return 0.4 if in_chord >= 0.5 else 0.1
    if style == "dense":
        return 1.0 if (n >= 5 and mean_dur <= 3) else 0.3
    if style == "calm":
        return 1.0 if (n <= 4 and mean_dur >= 6) else 0.3
    if style == "counter":
        melody = context.get("melody") or []
        if len(melody) < 2 or n < 2:
            return 0.5
        return 1.0 if (melody[-1][2] - melody[0][2]) * (rows[-1][2] - rows[0][2]) < 0 else 0.3
    if style == "echo":
        prev = context.get("prev_melody") or []
        if not prev:
            return 0.5
        prev_pcs = {int(p[2]) % 12 for p in prev}
        return 1.0 if sum(1 for r in rows if int(r[2]) % 12 in prev_pcs) >= n / 2 else 0.3
    return 1.0


def _clearance(rows: list, context: dict) -> float:
    melody = context.get("melody") or []
    if not melody:
        return 1.0
    clashes = 0
    for r in rows:
        for m in melody:
            if r[0] < m[0] + m[1] and m[0] < r[0] + r[1] and int(abs(r[2] - m[2])) % 12 in CLASH:
                clashes += 1
                break
    return 1.0 - clashes / len(rows)


def reward(prompt: str, response: str) -> float:
    try:
        obj = json.loads(response)
    except (TypeError, ValueError):
        return 0.0
    rows = _rows_of(obj)
    if not rows:
        return 0.0
    context = _context_from(prompt) or {}
    key_pcs = _pcs(int(context.get("key", 0)))
    r = 0.25 * (len(rows) / max(1, len(obj["notes"])))
    r += 0.20 * (sum(1 for x in rows if _grid_ok(x)) / len(rows))
    r += 0.20 * (sum(1 for x in rows if int(x[2]) % 12 in key_pcs) / len(rows))
    r += 0.10 * (sum(1 for x in rows if REG_LO <= x[2] <= REG_HI) / len(rows))
    r += 0.15 * _style_score(rows, context)
    r += 0.10 * _clearance(rows, context)
    return min(1.0, r)


class BarlineEnv(EnvironmentSingleTurn):
    dataset = load_jsonl(DEFAULT_DATASET_PATH)

    def build_prompt_messages(self, example: TaskExample, prompt_text: str):
        return [{"role": "user", "content": example.input}]

    def score_response(self, example: TaskExample, response_text: str) -> RewardResult:
        return RewardResult(score=reward(example.input, response_text), threshold=0.7)


def load_environment(dataset_path: str | None = None, **kwargs) -> BarlineEnv:
    env = BarlineEnv()
    if dataset_path:
        env.dataset = load_jsonl(dataset_path)
    return env
