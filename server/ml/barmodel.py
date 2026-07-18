"""Client for the bar-line ("music editing") model: a trained generator that
writes one fresh accompaniment line per bar as JSON notes.

Prefetched a bar ahead (the request rides the previous bar's ~2.4s), consumed
by the conductor as an extra candidate named "generated" — so the ranker, the
decision model, or a forced override can pick a line no rule-based generator
could have written. Every reply is sanitized (snap to key, fold into the
accompaniment register, clamp to the grid): the model supplies contour and
rhythm, the engine guarantees playability. A slow/absent/off-format reply
just means the rule-based candidates compete on their own that bar.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

import config
from engine.candidates import REG_HI, REG_LO
from engine.theory import snap_to_scale
from gestures.features import GestureFeatures
from ml.schema import BAR_SCHEMA, bar_prompt_for

log = logging.getLogger("barmodel")


def style_for(gesture: GestureFeatures | None) -> str:
    """The style directive sent with a prefetch — same intent vocabulary the
    dataset builder labels with (see ml/schema.STYLES)."""
    if gesture is None:
        return "calm"
    if gesture.rotation > 0.5:
        return "counter"
    if gesture.duration and gesture.duration < 0.6 and gesture.energy > 0.4:
        return "echo"
    e = 0.6 * gesture.energy + 0.4 * gesture.size
    if e > 0.6:
        return "dense"
    if e < 0.25:
        return "calm"
    return "free"


# Style-appropriate register folds: passing tones live near the melody,
# arpeggios low, pads in the accompaniment band. Style-blind folding would
# silently relocate a device out of its musical register.
STYLE_REG = {"passing": (52, 88), "echo": (48, 84), "arpeggio": (40, 72)}


def sanitize_line(obj, key_root: int, style: str = "harmonize") -> list | None:
    """Make any {"notes": [...]} reply playable: clamp to the grid, snap to the
    key, fold into the style's register. None if nothing usable remains."""
    if not isinstance(obj, dict) or not isinstance(obj.get("notes"), list):
        return None
    lo, hi = STYLE_REG.get(style, (REG_LO, REG_HI))
    out = []
    for row in obj["notes"][:16]:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            continue
        try:
            on, dur, midi, vel = (float(v) for v in row)
        except (TypeError, ValueError):
            continue
        on = int(max(0, min(15, on)))
        dur = int(max(1, min(16 - on, dur)))
        midi = snap_to_scale(int(midi), key_root)
        while midi > hi:
            midi -= 12
        while midi < lo:
            midi += 12
        out.append((on, dur, midi, round(max(0.1, min(1.0, vel)), 3)))
    return out or None


class RemoteBarModel:
    """Fire-and-forget prefetch client for an OpenAI-compatible endpoint."""

    def __init__(self) -> None:
        self.url = config.BARMODEL_URL.rstrip("/") + "/chat/completions" if config.BARMODEL_URL else ""
        self._cache: tuple[int, list] | None = None   # (bar_idx, sanitized notes)
        self._inflight: set[int] = set()
        if self.configured:
            log.info("bar-line model: %s @ %s (timeout %.0fms)",
                     config.BARMODEL_NAME, self.url, config.BARMODEL_TIMEOUT_MS)

    @property
    def configured(self) -> bool:
        return bool(self.url and config.BARMODEL_NAME)

    def take(self, bar_idx: int) -> tuple[list, str] | None:
        """(notes, style) written for exactly this bar, consumed once."""
        if self._cache and self._cache[0] == bar_idx:
            _idx, notes, style = self._cache
            self._cache = None
            return notes, style
        return None

    def prefetch(self, bar_idx: int, context: dict, key_root: int) -> None:
        if not self.configured or bar_idx in self._inflight:
            return
        if self._cache and self._cache[0] == bar_idx:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._inflight.add(bar_idx)
        loop.create_task(self._ask(loop, bar_idx, context, key_root))

    async def _ask(self, loop: asyncio.AbstractEventLoop, bar_idx: int,
                   context: dict, key_root: int) -> None:
        body = json.dumps({
            "model": config.BARMODEL_NAME,
            "messages": [{"role": "user", "content": bar_prompt_for(context)}],
            "max_tokens": 200,   # a 16-note bar is ~190 tokens; smaller cap = faster reply
            "temperature": 0.7,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "bar", "schema": BAR_SCHEMA}},
        }).encode()
        try:
            text = await asyncio.wait_for(
                loop.run_in_executor(None, self._post, body),
                timeout=config.BARMODEL_TIMEOUT_MS / 1000.0 + 0.2)
        except Exception as e:  # noqa: BLE001 - a missed bar is just a bar without this candidate
            log.warning("bar ask failed (%s) — rule-based candidates only", type(e).__name__)
            return
        finally:
            self._inflight.discard(bar_idx)
        try:
            notes = sanitize_line(json.loads(text), key_root,
                                  str(context.get("style") or "harmonize"))
        except (TypeError, ValueError):
            notes = None
        if notes is None:
            log.warning("bar reply unusable: %.120s", text)
            return
        self._cache = (bar_idx, notes, str(context.get("style") or "harmonize"))
        log.info("bar %d: generated line, %d notes (%s)", bar_idx, len(notes), context.get("style"))

    def _post(self, body: bytes) -> str:
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.BARMODEL_KEY}",
        })
        with urllib.request.urlopen(req, timeout=config.BARMODEL_TIMEOUT_MS / 1000.0) as resp:
            payload = json.loads(resp.read().decode())
        return payload["choices"][0]["message"]["content"]
