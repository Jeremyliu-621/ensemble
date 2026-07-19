"""Wand input router. Buffers raw IMU/pose frames during a grab and hands the
engine one complete GestureWindow on release — the segmentation the MPR121 (or
its pinch/touch stand-ins) provides for free. Frames arriving outside a grab are
ignored, so a gesture is exactly what happened between grab-start and grab-end.
"""
from __future__ import annotations

import logging
import math
import os

from engine_api import GestureWindow, MusicEngine

# On-site aiming calibration, NO reflash needed. The board's mounting decides
# which gyro channel is the pointing axis and its sign: sweep the wand slowly
# left->right while watching tools/wand_watch.py — yaw should climb positive.
#   mirrored?     WM_YAW_SIGN=-1
#   barely moves? WM_YAW_AXIS=4 or 5 (frame index: 4=gx, 5=gy, 6=gz)
YAW_AXIS = int(os.environ.get("WM_YAW_AXIS", "6"))
YAW_SIGN = float(os.environ.get("WM_YAW_SIGN", "1"))

log = logging.getLogger("wand")

MIN_FRAMES = 3       # windows shorter than this are dropped as noise
MAX_FRAMES = 20_000  # a grab left open by a dropped wand stops growing here (~5 min @ 60Hz)


class WandRouter:
    def __init__(self, engine: MusicEngine, recorder=None) -> None:
        self._engine = engine
        self._recorder = recorder
        self._grabbing = False
        self._modality: str | None = None
        self._frames: list[list[float]] = []
        self._t_start: float = 0.0

    @property
    def grabbing(self) -> bool:
        return self._grabbing

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
                if self._recorder:
                    self._recorder.record(window)
                self._engine.on_gesture(window)
            self._frames = []
        # Let the engine react to the raw grab edges too (e.g., cut sustains).
        self._engine.on_grab(kind, server_ms)


def _wrap_deg(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


class WandAimer:
    """Integrates gyro yaw (gz, deg/s) into a pointing direction and resolves
    it against the sections' placed azimuths. The hardware wand streams IMU
    continuously so it aims freely; the phone wand streams only during grabs,
    so it aims while grabbed. wand.recal zeroes the direction."""

    LOCK_DEG = 40.0   # aim locks to a section within this of its azimuth

    def __init__(self) -> None:
        self.yaw = 0.0
        self._last_tw: float | None = None
        self._bias = 0.0                # learned at-rest gyro bias (deg/s) — the
                                        # real board reads ~1 deg/s while still,
                                        # ~55 deg/min of phantom beam rotation

    def on_frames(self, frames: list[list[float]]) -> None:
        for f in frames:
            if len(f) < 7:
                continue
            try:
                tw, gz = float(f[0]), float(f[YAW_AXIS]) * YAW_SIGN
                gmag = math.sqrt(f[4] * f[4] + f[5] * f[5] + f[6] * f[6])
            except (TypeError, ValueError):
                continue
            if gmag < 3.0:              # still enough that the reading IS bias
                self._bias += (gz - self._bias) * 0.02
            if self._last_tw is not None:
                dt = (tw - self._last_tw) / 1000.0
                if 0.0 < dt < 0.5:
                    self.yaw = _wrap_deg(self.yaw + (gz - self._bias) * dt)
            self._last_tw = tw

    def recal(self) -> None:
        self.yaw = 0.0
        self._last_tw = None

    def resolve(self, placements: dict[str, float]) -> str | None:
        """The section whose azimuth is nearest the current yaw, or None."""
        best_sid, best_d = None, self.LOCK_DEG + 1
        for sid, az in placements.items():
            d = abs(_wrap_deg(az - self.yaw))
            if d < best_d:
                best_sid, best_d = sid, d
        return best_sid if best_d <= self.LOCK_DEG else None


class ShakeDetector:
    """A deliberate SHAKE — fast, repeated high-g impulses — read off raw
    accelerometer magnitude, distinct from the slow tilt/point motions that
    drive aiming and DET-mode expression. |accel| pulses well above gravity
    at each jerk of a shake regardless of which way it's swung (the vector
    norm can't tell direction), so this counts rising-edge crossings above a
    hard threshold rather than looking for alternating sign. Used as "select
    all": since the hardware wand aims continuously from yaw (there's no
    separate SELECT mode to gate it), a shake is the one gesture that's
    unmistakably not a point, so App latches aim to None for a short window
    after one fires."""

    WINDOW_MS = 600.0        # how far back "recent" impulses are counted
    HARD_MS2 = 14.0          # |accel| - gravity that counts as a forceful jerk
    MIN_PEAKS = 4            # jerks within WINDOW_MS to call it a shake
    COOLDOWN_MS = 1500.0     # refractory period so one shake doesn't retrigger

    def __init__(self) -> None:
        self._samples: list[tuple[float, float]] = []   # (tw, |accel|)
        self._last_fire = float("-inf")

    def on_frames(self, frames: list[list[float]]) -> bool:
        """Feed the same IMU batch aiming saw; True the frame a shake fires."""
        for f in frames:
            if len(f) < 4:
                continue
            try:
                tw, ax, ay, az = float(f[0]), float(f[1]), float(f[2]), float(f[3])
            except (TypeError, ValueError):
                continue
            self._samples.append((tw, math.sqrt(ax * ax + ay * ay + az * az)))
        if not self._samples:
            return False
        now = self._samples[-1][0]
        cutoff = now - self.WINDOW_MS
        self._samples = [s for s in self._samples if s[0] >= cutoff]
        peaks, above = 0, False
        for _, mag in self._samples:
            hard = abs(mag - 9.8) > self.HARD_MS2
            if hard and not above:
                peaks += 1
            above = hard
        if peaks >= self.MIN_PEAKS and now - self._last_fire > self.COOLDOWN_MS:
            self._last_fire = now
            self._samples.clear()
            return True
        return False
