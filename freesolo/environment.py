"""Freesolo GRPO environment for the accompaniment decision model.

score_response() is the reward. It runs on Freesolo's workers, not in this
repo, so everything it needs is inlined (a faithful port of the server's
heuristic scoring in server/ml/heuristic.py — keep them in sync by hand).

Reward shape (0..1):
  0.35  perfectly formatted decision JSON (exactly the two schema keys)
  0.35  candidate consistency with the gesture, scaled between the worst and
        best candidate the heuristic would score for that context
  0.15  octave_shift matches the gesture's vertical intent
  0.15  keeps the music evolving (doesn't repeat the previous candidate)

Reconcile with the scaffold `flash env setup` generates for your account:
keep its load_environment() return shape and wire score_response in as the
reward; the dataset sidecar is freesolo/dataset/ (build_dataset.py output).
"""
from __future__ import annotations

import json

CANDIDATES = ["lower_imitation", "contrary_motion", "sustained", "delayed",
              "rhythmic_dense", "rest"]


def _context_from(prompt: str) -> dict | None:
    """The prompt ends with 'Context: {...}' (see server/ml/schema.prompt_for)."""
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
    """Port of server/ml/heuristic.rank — the musical prior the model is
    rewarded for respecting (and free to bend where SFT taught it taste)."""
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


def score_response(prompt: str, response: str) -> float:
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
    reward = 0.35
    reward += 0.35 * _consistency(context, obj["candidate"])
    reward += 0.15 * _shift_consistency(context, obj["octave_shift"])
    reward += 0.15 * (0.0 if obj["candidate"] == context.get("prev") else 1.0)
    return min(1.0, reward)


def load_environment():
    """Keep the return shape of the load_environment() that `flash env setup`
    scaffolds for your account version; this dict form is a placeholder."""
    return {"reward": score_response, "dataset": "dataset"}
