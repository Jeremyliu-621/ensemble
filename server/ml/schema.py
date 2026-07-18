"""The decision contract between the music engine and any trained policy model.

A decision is the tiny JSON object a policy emits per gesture:

    {"candidate": "rhythmic_dense", "octave_shift": 0}

`candidate` picks which accompaniment line the coming bars play; `octave_shift`
moves that line by whole octaves. DECISION_SCHEMA is the single source of
truth: the server parses replies against it, the dataset builder validates
training rows with it, and the GRPO config embeds it as `structured_outputs`
so the trained model cannot emit off-format tokens.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from engine.candidates import GENERATORS

# "generated" is the bar-line model's freshly written line (ml/barmodel.py);
# it competes with the rule-based candidates whenever one has arrived.
CANDIDATES = list(GENERATORS) + ["generated"]

DECISION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "candidate": {"type": "string", "enum": CANDIDATES},
        "octave_shift": {"type": "integer", "enum": [-1, 0, 1]},
    },
    "required": ["candidate", "octave_shift"],
    "additionalProperties": False,
}

INSTRUCTION = (
    "You are the accompaniment brain of a gesture-conducted orchestra. Given the "
    "musical context and the conductor's gesture, reply with ONLY a JSON object "
    'like {"candidate": "sustained", "octave_shift": 0}. candidate is one of: '
    + ", ".join(CANDIDATES) + ". octave_shift is -1, 0 or 1 (whole octaves)."
)


@dataclass
class Decision:
    candidate: str
    octave_shift: int          # whole octaves, -1..1
    source: str = "model"      # "heuristic" | "model" | "forced"

    def semitones(self) -> int:
        return self.octave_shift * 12


def build_context(*, key_root: int, bpm: float, chord_root: int, chord_minor: bool,
                  last_choice: str | None, gesture) -> dict:
    """The compact model input. Floats are rounded so live prompts tokenise the
    same way the logged/training ones do."""
    g = {k: round(v, 2) for k, v in gesture.as_dict().items()} if gesture else None
    return {
        "key": key_root,
        "bpm": round(bpm),
        "chord": {"root": chord_root, "minor": chord_minor},
        "prev": last_choice,
        "gesture": g,
    }


def prompt_for(context: dict) -> str:
    return INSTRUCTION + "\nContext: " + json.dumps(context, separators=(",", ":"))


def parse_decision(text: str, source: str = "model") -> Decision | None:
    """Parse a policy reply. Lenient on extra keys (an SFT-only model may chat),
    strict on values — any deviation returns None and the heuristic covers."""
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    cand, shift = obj.get("candidate"), obj.get("octave_shift")
    if cand not in CANDIDATES or shift not in (-1, 0, 1):
        return None
    return Decision(candidate=cand, octave_shift=int(shift), source=source)


# --- The bar-line ("music editing") model contract ---------------------------
# One bar of accompaniment on the 16th grid: {"notes": [[onset, dur, midi, vel], ...]}.
# The server sanitizes every reply (snap to key, fold into register, clamp to
# the grid) — the model supplies contour and rhythm, the engine guarantees
# playability.

STYLES = ["dense", "calm", "counter", "echo", "free", "harmonize", "passing", "arpeggio"]

BAR_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "array", "minItems": 4, "maxItems": 4,
                      "items": {"type": "number"}},
        },
    },
    "required": ["notes"],
    "additionalProperties": False,
}

BAR_INSTRUCTION = (
    "You write one bar of accompaniment for a gesture-conducted orchestra. Given "
    'the musical context, reply with ONLY a JSON object {"notes": [[onset, dur, '
    "midi, velocity], ...]}: onset 0-15 on the 16th-note grid, dur 1-16 "
    "sixteenths, midi pitch 40-84 in the given key, velocity 0.1-1.0. Match the "
    "requested style: dense=busy subdivision, calm=long low chord tones, "
    "counter=moves against the melody, echo=answer the previous bar's melody "
    "tail in this bar's silent slots an octave lower and soft, free=your "
    "choice, harmonize=voice the bar's chord as a held pad (few long chord "
    "tones, smooth voice leading), passing=fill the melody's 3rd/4th leaps "
    "with the in-between scale tone (short, soft, stepwise), arpeggio=the "
    "chord as an even low-high-mid-high 8th-note figure below the melody. "
    "Stay out of the melody's way."
)


def build_bar_context(*, key_root: int, bpm: float, chord_root: int, chord_minor: bool,
                      style: str, melody, prev_melody) -> dict:
    return {
        "key": key_root,
        "bpm": round(bpm),
        "chord": {"root": chord_root, "minor": chord_minor},
        "style": style,
        "melody": [[int(a), int(b), int(c)] for (a, b, c) in melody],
        "prev_melody": [[int(a), int(b), int(c)] for (a, b, c) in prev_melody],
    }


def bar_prompt_for(context: dict) -> str:
    return BAR_INSTRUCTION + "\nContext: " + json.dumps(context, separators=(",", ":"))
