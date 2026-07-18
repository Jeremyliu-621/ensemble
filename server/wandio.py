"""Wand input router. Buffers raw IMU/pose frames during a grab and hands the
engine one complete GestureWindow on release — the segmentation the MPR121 (or
its pinch/touch stand-ins) provides for free. Frames arriving outside a grab are
ignored, so a gesture is exactly what happened between grab-start and grab-end.
"""
from __future__ import annotations

import logging

from engine_api import GestureWindow, MusicEngine

log = logging.getLogger("wand")

MIN_FRAMES = 3       # windows shorter than this are dropped as noise
MAX_FRAMES = 20_000  # a grab left open by a dropped wand stops growing here (~5 min @ 60Hz)


class WandRouter:
    def __init__(self, engine: MusicEngine) -> None:
        self._engine = engine
        self._grabbing = False
        self._modality: str | None = None
        self._frames: list[list[float]] = []
        self._t_start: float = 0.0

    def on_imu(self, frames: list[list[float]]) -> None:
        self._collect("imu", frames)

    def on_pose(self, frames: list[list[float]]) -> None:
        self._collect("pose", frames)

    def _collect(self, modality: str, frames: list[list[float]]) -> None:
        if not self._grabbing:
            return
        if self._modality is None:
            self._modality = modality
        if modality == self._modality and len(self._frames) < MAX_FRAMES:
            self._frames.extend(frames)

    def reset(self) -> None:
        """Forget any in-progress grab (the wand disconnected or was replaced)."""
        self._grabbing = False
        self._modality = None
        self._frames = []

    def on_grab(self, kind: str, server_ms: float) -> None:
        if kind == "start":
            self._grabbing = True
            self._modality = None
            self._frames = []
            self._t_start = server_ms
        elif kind == "end":
            self._grabbing = False
            if self._modality and len(self._frames) >= MIN_FRAMES:
                window = GestureWindow(
                    modality=self._modality,
                    frames=self._frames,
                    t_start_server_ms=self._t_start,
                    t_end_server_ms=server_ms,
                )
                log.info("gesture window: %s, %d frames, %.0fms",
                         self._modality, len(self._frames), server_ms - self._t_start)
                self._engine.on_gesture(window)
            self._frames = []
        # Let the engine react to the raw grab edges too (e.g., cut sustains).
        self._engine.on_grab(kind, server_ms)
