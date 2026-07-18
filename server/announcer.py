"""Backboard.io commentator: one stateful assistant follows the set and
returns short hype lines, broadcast to the stage as `announce` toasts.

Backboard is a stateful assistant API (X-API-Key auth, POST /threads/messages,
assistant/thread auto-created on first call, `memory: "Auto"` for cross-thread
recall) — the memory is the point: the assistant remembers the whole set, and
with WM_BACKBOARD_ASSISTANT pinned it remembers *previous* sets too. Requests
are fire-and-forget with per-event-kind throttles; no key configured means the
whole thing is inert; any failure is one log line, never a stalled show.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request

import config

log = logging.getLogger("announcer")

PROMPT = (
    "You are the live on-stage announcer for 'Wand Maestro', a show where a "
    "conductor's wand drives an orchestra of audience phones. React to the "
    "event below with exactly ONE energetic spoken line, at most 18 words, "
    "plain text, no emojis, no quotation marks. You remember this whole set - "
    "call back to earlier moments when it lands.\nEvent: "
)

# Minimum seconds since the previous announcement, per event kind. Transport
# and song moments always speak; churn (joins/drops) and vibe checks wait.
MIN_GAP_S = {"show.start": 0, "show.stop": 0, "song.load": 10,
             "wand.connect": 30, "section.join": 30, "section.drop": 30, "vibe": 75}


class Announcer:
    def __init__(self, on_line) -> None:
        self._on_line = on_line          # async callback(text) -> broadcast to the stage
        self._last = 0.0
        self._assistant = config.BACKBOARD_ASSISTANT or None
        self._thread = None              # one thread per server run = one set
        if self.configured:
            log.info("announcer: backboard %s (%s/%s)",
                     config.BACKBOARD_URL, config.BACKBOARD_PROVIDER, config.BACKBOARD_MODEL)

    @property
    def configured(self) -> bool:
        return bool(config.BACKBOARD_KEY)

    def poke(self, kind: str, digest: str) -> None:
        """Maybe announce this event. Safe to call from anywhere, any rate."""
        if not self.configured:
            return
        now = time.monotonic()
        if now - self._last < MIN_GAP_S.get(kind, 60):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._last = now
        loop.create_task(self._ask(loop, kind, digest))

    async def _ask(self, loop: asyncio.AbstractEventLoop, kind: str, digest: str) -> None:
        body: dict = {
            "content": PROMPT + digest,
            "llm_provider": config.BACKBOARD_PROVIDER,
            "model_name": config.BACKBOARD_MODEL,
            "memory": "Auto",
            "stream": False,
        }
        if self._assistant:
            body["assistant_id"] = self._assistant
        if self._thread:
            body["thread_id"] = self._thread
        try:
            resp = await asyncio.wait_for(
                loop.run_in_executor(None, self._post, json.dumps(body).encode()),
                timeout=10.0)
        except Exception as e:  # noqa: BLE001 - a silent announcer, never a stalled show
            log.warning("announce failed (%s: %s)", kind, type(e).__name__)
            return
        self._assistant = resp.get("assistant_id") or self._assistant
        self._thread = resp.get("thread_id") or self._thread
        text = _extract_text(resp)
        if text:
            log.info("announce (%s): %s", kind, text)
            await self._on_line(text)

    def _post(self, body: bytes) -> dict:
        req = urllib.request.Request(
            config.BACKBOARD_URL.rstrip("/") + "/threads/messages", data=body,
            headers={"Content-Type": "application/json", "X-API-Key": config.BACKBOARD_KEY})
        with urllib.request.urlopen(req, timeout=9.0) as resp:
            return json.loads(resp.read().decode())


def _extract_text(resp: dict) -> str | None:
    """Backboard reply shapes drift; try the plausible fields defensively."""
    for probe in (resp.get("content"), (resp.get("message") or {}).get("content") if isinstance(resp.get("message"), dict) else None,
                  resp.get("response"), resp.get("text")):
        if isinstance(probe, str) and probe.strip():
            return probe.strip().strip('"')[:200]
    return None
