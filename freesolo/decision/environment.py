"""Freesolo environment for the DECISION model (gesture -> accompaniment pick).

The task: given musical context + the conductor's gesture (the prompt built by
server/ml/schema.prompt_for), emit exactly {"candidate": ..., "octave_shift": ...}.

Reward (0..1), a port of the server's heuristic prior (server/ml/heuristic.py —
keep in sync by hand):
  0.35  perfectly formatted decision JSON (exactly the two schema keys)
  0.35  candidate consistency with the gesture, scaled between the worst and
        best candidate the heuristic would score for that context
  0.15  octave_shift matches the gesture's vertical intent
  0.15  keeps the music evolving (doesn't repeat the previous candidate)

Dataset sidecar: dataset/{train,eval}.jsonl from server/tools/build_dataset.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from freesolo.datasets import TaskExample
from freesolo.environments import EnvironmentSingleTurn, RewardResult

DEFAULT_DATASET_PATH = Path(__file__).parent / "dataset" / "train.jsonl"

CANDIDATES = ["lower_imitation", "contrary_motion", "sustained", "delayed",
              "rhythmic_dense", "rest", "generated"]


def load_jsonl(path):
    with Path(path).open() as f:
        return [json.loads(line) for line in f if line.strip()]


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


def _candidate_scores(context: dict) -> dict[str, float]:
    g = context.get("gesture")
    e = g.get("energy", 0.0) if g else 0.0
    size = g.get("size", 0.0) if g else 0.0
    vert = g.get("vertical", 0.0) if g else 0.0
    rot = g.get("rotation", 0.0) if g else 0.0
    dur = g.get("duration", 0.0) if g else 0.0
    energy_intent = 0.6 * e + 0.4 * size
    flick = 1.0 if (dur and dur < 0.6) else 0.35
    return {
        "rest": 0.65 * (1 - energy_intent) - 0.25,
        "sustained": 0.45 * (1 - energy_intent) + 0.45 * max(0.0, vert)
                     + (0.35 if g is None else 0.0),
        "lower_imitation": 0.45 + 0.25 * (1 - abs(energy_intent - 0.5) * 2),
        "contrary_motion": 0.30 + 1.20 * rot + 0.25 * max(0.0, -vert),
        "delayed": 0.20 + 0.80 * energy_intent * flick,
        "rhythmic_dense": 0.10 + 1.30 * energy_intent,
        "generated": 0.55 + 0.35 * energy_intent,
    }


def _consistency(context: dict, candidate: str) -> float:
    scores = _candidate_scores(context)
    best, worst = max(scores.values()), min(scores.values())
    if best <= worst:
        return 1.0
    return (scores[candidate] - worst) / (best - worst)


def _shift_consistency(context: dict, shift: int) -> float:
    g = context.get("gesture")
    vert = g.get("vertical", 0.0) if g else 0.0
    want = 1 if vert > 0.6 else -1 if vert < -0.6 else 0
    return 1.0 if shift == want else 0.0


def reward(prompt: str, response: str) -> float:
    try:
        obj = json.loads(response)
    except (TypeError, ValueError):
        return 0.0
    if (not isinstance(obj, dict) or set(obj) != {"candidate", "octave_shift"}
            or obj["candidate"] not in CANDIDATES or obj["octave_shift"] not in (-1, 0, 1)):
        return 0.0
    context = _context_from(prompt)
    if context is None:
        return 0.65  # formatted, but nothing to judge musically
    r = 0.35
    r += 0.35 * _consistency(context, obj["candidate"])
    r += 0.15 * _shift_consistency(context, obj["octave_shift"])
    r += 0.15 * (0.0 if obj["candidate"] == context.get("prev") else 1.0)
    return min(1.0, r)


class DecisionEnv(EnvironmentSingleTurn):
    dataset = load_jsonl(DEFAULT_DATASET_PATH)

    def build_prompt_messages(self, example: TaskExample, prompt_text: str):
        return [{"role": "user", "content": example.input}]

    def score_response(self, example: TaskExample, response_text: str) -> RewardResult:
        return RewardResult(score=reward(example.input, response_text), threshold=0.7)


def load_environment(dataset_path: str | None = None, **kwargs) -> DecisionEnv:
    env = DecisionEnv()
    if dataset_path:
        env.dataset = load_jsonl(dataset_path)
    return env
