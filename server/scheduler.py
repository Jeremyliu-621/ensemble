"""The lookahead scheduler loop.

Every SCHED_TICK_MS it pulls freshly-due events from the engine for the window
(now, now + LOOKAHEAD_MS], drops any that violate the MIN_LEAD_MS safety margin,
and broadcasts them to sections + stage. It NEVER says "play now" — every event
carries an absolute server-time `at`, which each client converts to its own
audio clock. This is what keeps devices in sync.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging

from clocksync import server_time_ms
from config import LOOKAHEAD_MS, MIN_LEAD_MS, SCHED_TICK_MS
from engine_api import MusicEngine
from hub import Hub
from protocol import ENGINE_STATE, SCHED_CANCEL, SCHED_NOTES

log = logging.getLogger("sched")


class Scheduler:
    def __init__(self, engine: MusicEngine, hub: Hub) -> None:
        self._engine = engine
        self._hub = hub
        self._task: asyncio.Task | None = None
        self._seen_choice: str | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        tick = SCHED_TICK_MS / 1000.0
        while True:
            try:
                await self._pump()
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("scheduler tick failed")
            await asyncio.sleep(tick)

    async def _pump(self) -> None:
        now = server_time_ms()

        # 1) Cancellations first, so a cut lands before any note we're about to send.
        for spec in self._engine.get_cancels():
            if spec.allnotesoff:
                await self._hub.broadcast({"t": SCHED_CANCEL, "allnotesoff": True},
                                          roles=("section", "stage"))
            else:
                msg = {"t": SCHED_CANCEL}
                if spec.ids:
                    msg["ids"] = spec.ids
                if spec.section is not None:
                    msg["section"] = spec.section
                if spec.after is not None:
                    msg["after"] = spec.after
                await self._hub.broadcast(msg, roles=("section", "stage"))

        # 2) New events for the lookahead window.
        events = self._engine.get_events(now, now + LOOKAHEAD_MS)
        if not events:
            return

        safe = []
        for e in events:
            if e.at < now + MIN_LEAD_MS:
                log.warning("dropping late event %s (lead %.0fms < %.0fms)",
                            e.id, e.at - now, MIN_LEAD_MS)
                continue
            safe.append(dataclasses.asdict(e))

        if safe:
            await self._hub.broadcast({"t": SCHED_NOTES, "events": safe},
                                      roles=("section", "stage"))

        # 3) When the chosen accompaniment changes, push a light live update to the
        # stage/editor (drives the "now playing" + change indicator, even laptop-only).
        status = getattr(self._engine, "status", None)
        if status:
            st = status()
            # Fire on decision changes AND on envelope movement (~0.05 steps),
            # so the stage can animate the conducting intensity live.
            key = (st.get("last_choice"), st.get("decision_source"),
                   round(st.get("intensity", 0.5) * 20))
            if key != self._seen_choice:
                self._seen_choice = key
                await self._hub.broadcast({
                    "t": ENGINE_STATE, "last_choice": st["last_choice"], "gesture": st["gesture"],
                    "decision_source": st.get("decision_source"),
                    "intensity": st.get("intensity"),
                    "playing": st["playing"], "bpm": st["bpm"], "song": st["song"],
                }, roles=("stage", "admin"))
