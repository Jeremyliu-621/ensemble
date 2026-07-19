"""Pluggable decision policy: who picks the accompaniment after each gesture.

The heuristic ranker is always available and always instant. When WM_MODEL_URL
is set, a trained model (any OpenAI-compatible endpoint — e.g. a Freesolo
deploy) is asked asynchronously the moment a gesture lands; its answer shapes
the following bars if it arrives in time, and the heuristic silently covers
when the model is slow, down, or off-format. The scheduler path never blocks
on the network — a dead endpoint can only cost intelligence, never music.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

import config
from gestures.features import GestureFeatures
from ml import heuristic
from ml.schema import CANDIDATES, DECISION_SCHEMA, Decision, parse_decision, prompt_for

log = logging.getLogger("policy")


def heuristic_decision(gesture: GestureFeatures | None, last_choice: str | None,
                       candidates: list[str] | None = None) -> Decision:
    """Rank over `candidates` when given (the bar's actually-available lines —
    "generated" only exists on bars where the bar model delivered), else the
    full vocabulary (dataset labeling)."""
    scores = heuristic.rank(gesture, candidates if candidates is not None else CANDIDATES)
    choice = heuristic.choose(scores, last_choice)
    return Decision(candidate=choice, octave_shift=heuristic.octave_shift(gesture) // 12,
                    source="heuristic")


class RemoteModel:
    """Fire-and-forget client for an OpenAI-compatible /chat/completions."""

    def __init__(self) -> None:
        self.url = config.MODEL_URL.rstrip("/") + "/chat/completions" if config.MODEL_URL else ""
        self._latest: Decision | None = None
        if self.configured:
            log.info("decision model: %s @ %s (timeout %.0fms)",
                     config.MODEL_NAME, self.url, config.MODEL_TIMEOUT_MS)

    @property
    def configured(self) -> bool:
        return bool(self.url and config.MODEL_NAME)

    def take(self) -> Decision | None:
        """The freshest model answer, consumed once."""
        d, self._latest = self._latest, None
        return d

    def request(self, context: dict) -> None:
        """Kick off an async ask. No-op without configuration or a running
        loop (the headless engine tests drive the Conductor loop-less)."""
        if not self.configured:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._ask(loop, context))

    async def _ask(self, loop: asyncio.AbstractEventLoop, context: dict) -> None:
        body = json.dumps({
            "model": config.MODEL_NAME,
            "messages": [{"role": "user", "content": prompt_for(context)}],
            "max_tokens": 60,
            "temperature": 0,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "decision", "schema": DECISION_SCHEMA}},
        }).encode()
        try:
            text = await asyncio.wait_for(
                loop.run_in_executor(None, self._post, body),
                timeout=config.MODEL_TIMEOUT_MS / 1000.0 + 0.2)
        except Exception as e:  # noqa: BLE001 - timeout/network/HTTP all mean "heuristic covers"
            log.warning("model ask failed (%s) — heuristic covers", type(e).__name__)
            return
        decision = parse_decision(text)
        if decision is None:
            log.warning("model reply off-format — heuristic covers: %.120s", text)
            return
        self._latest = decision
        log.info("model decision: %s shift %+d", decision.candidate, decision.octave_shift)

    def _post(self, body: bytes) -> str:
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.MODEL_KEY}",
        })
        with urllib.request.urlopen(req, timeout=config.MODEL_TIMEOUT_MS / 1000.0) as resp:
            payload = json.loads(resp.read().decode())
        msg = payload["choices"][0]["message"]
        # Hybrid-thinking serving (Fireworks qwen3_5 template) can route the
        # whole answer into reasoning_content and leave content empty.
        return msg.get("content") or msg.get("reasoning_content") or ""
