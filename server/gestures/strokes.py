"""Live stroke-intent classifier for the hardware wand.

Fed every validated wand.imu batch (frames [tw_ms, ax,ay,az, gx,gy,gz] —
accel m/s^2 WITH gravity, gyro deg/s), keeps a rolling ~0.7s window and
answers two questions continuously:

  * meters — how is the wand moving right now? {energy, size, lift, swirl},
    normalized like gestures/features.py so the console bars feel consistent.
  * stroke — what did the hand just DO? One of LEFT_SWIPE, RIGHT_SWIPE,
    RAISE, LOWER, CIRCLE, STAB, SHAKE — or STILL when quiet, None while
    moving-but-ambiguous. A committed stroke latches ~1s so it's readable.

Pure math, no I/O — unit-tested in server/tests/test_strokes.py. Display-only
for now: nothing here feeds the music engine.

Axis calibration mirrors the aiming story (wandio.py): the yaw channel comes
from WM_YAW_AXIS/WM_YAW_SIGN so "left swipe" agrees with where the beam moves;
the pitch channel has its own WM_PITCH_AXIS/WM_PITCH_SIGN (default gx).
"""
from __future__ import annotations

import math
import os
from collections import deque

from wandio import YAW_AXIS, YAW_SIGN

PITCH_AXIS = int(os.environ.get("WM_PITCH_AXIS", "4"))
PITCH_SIGN = float(os.environ.get("WM_PITCH_SIGN", "1"))

WINDOW_MS = 700.0     # rolling analysis window
STEP_MS = 100.0       # classify at most this often
LATCH_MS = 1000.0     # a committed stroke stays on screen this long
STILL_MS = 500.0      # this long below the motion floor -> STILL

SWIPE_DEG = 35.0      # net yaw travel for a swipe
RAISE_DEG = 30.0      # net pitch travel for raise/lower
CIRCLE_ROT_DEG = 260.0  # cumulative rotation with little net travel -> circle
STAB_ACC = 12.0       # linear-accel spike (m/s^2) — high: real swipes peak past 8
STAB_TRAVEL = 20.0    # a stab goes nowhere: net yaw+pitch travel must stay tiny
# ── pose zones (gravity-only: drift-free, no motion dynamics) ────────────────
# Where the wand POINTS is the primary vocabulary; movement strokes are extras.
TILT_HIGH = 0.80      # pointed ~90% up
TILT_HALF = 0.50      # pointed halfway up
TILT_DOWN = -0.50     # pointed down
ROLL_DEG = 55.0       # wrist roll (about the wand's long axis) to count as rolled
ROLL_SIGN = float(os.environ.get("WM_ROLL_SIGN", "1"))   # -1 if rolls read mirrored
TILT_HOLD_MS = 600.0  # zone held that long (calmly) -> commits
TILT_REFIRE_MS = 1400.0  # ...and re-commits while held, so a hush stays down
TILT_CALM_RMS = 2.5   # poses read only while the wand is otherwise quiet
SHAKE_REVERSALS = 4   # sign flips of the dominant linear axis
SHAKE_RMS = 3.0       # ...with at least this much vigor

_GRAV_ALPHA = 0.08    # gravity EMA
_NOISE_ACC = 1.5      # ignore reversals below this (m/s^2)

# CIRCLE (gyro-loop detection) was cut: it false-fired on ordinary waving.
# Arpeggio now comes from the ROLL poses (wrist-roll + hold) or SHAKE.
STROKES = ("LEFT_SWIPE", "RIGHT_SWIPE", "RAISE", "HALF_RAISE", "LOWER",
           "ROLL_LEFT", "ROLL_RIGHT", "STAB", "SHAKE", "STILL")


class StrokeTracker:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        # window rows: (tw, dt, la(3), la_mag, yaw_rate, pitch_rate, gyro_mag)
        self._win: deque = deque()
        self._g: list[float] | None = None    # gravity EMA (sensor frame)
        self._last_tw: float | None = None
        self._last_step = 0.0
        self._last_active = 0.0
        self._latched: str | None = None
        self._latch_until = 0.0
        self._pose_zone: str | None = None     # which pose zone the wand is held in
        self._tilt_since: float | None = None  # when the current pose-hold began
        self._tilt_fired = 0.0                 # last pose commit (for re-fire)

    def push(self, frames: list[list[float]]) -> tuple[str | None, dict, bool]:
        """Feed one batch. Returns (stroke, meters, committed_new).
        stroke: latched stroke name, "STILL", or None (moving, unclassified).
        committed_new: True the moment a stroke latches (caller may bypass
        its broadcast throttle so the panel reacts instantly)."""
        committed = False
        for f in frames:
            if len(f) < 7:
                continue
            committed = self._ingest(f) or committed
        return self._current(), self._meters(), committed

    # ── per-frame ingest ─────────────────────────────────────────────────────
    def _ingest(self, f: list[float]) -> bool:
        tw = float(f[0])
        if self._last_tw is not None and tw < self._last_tw - 5000:
            self.reset()                        # board rebooted: its clock restarted
        dt = 0.0 if self._last_tw is None else max(0.0, min(0.1, (tw - self._last_tw) / 1000.0))
        self._last_tw = tw

        a = [float(f[1]), float(f[2]), float(f[3])]
        if self._g is None:
            self._g = list(a)
        else:
            self._g = [g * (1 - _GRAV_ALPHA) + x * _GRAV_ALPHA for g, x in zip(self._g, a)]
        la = [x - g for x, g in zip(a, self._g)]
        la_mag = math.sqrt(sum(x * x for x in la))

        yaw_rate = float(f[YAW_AXIS]) * YAW_SIGN
        pitch_rate = float(f[PITCH_AXIS]) * PITCH_SIGN
        gyro_mag = math.sqrt(f[4] * f[4] + f[5] * f[5] + f[6] * f[6])

        self._win.append((tw, dt, la, la_mag, yaw_rate, pitch_rate, gyro_mag))
        while self._win and tw - self._win[0][0] > WINDOW_MS:
            self._win.popleft()

        if la_mag > 1.0 or gyro_mag > 40.0:
            self._last_active = tw

        # Pose zones: WHERE the wand points (and how it's rolled), held calmly,
        # is the primary vocabulary — pure gravity reads, drift-free, no
        # motion-dynamics thresholds. A zone held TILT_HOLD_MS commits and
        # RE-commits while held, so "pointed at the floor" keeps the room
        # hushed. Switching zones restarts the hold clock.
        tilt = self._g[1] / 9.8
        roll = math.degrees(math.atan2(self._g[0], self._g[2])) * ROLL_SIGN
        zone: str | None = None
        if la_mag < TILT_CALM_RMS:
            if tilt > TILT_HIGH:
                zone = "RAISE"                  # ~90% up: the swell
            elif tilt > TILT_HALF:
                zone = "HALF_RAISE"             # halfway up: harmony
            elif tilt < TILT_DOWN:
                zone = "LOWER"                  # down: hush
            elif abs(tilt) < 0.45 and roll > ROLL_DEG:
                zone = "ROLL_RIGHT"             # wrist rolled right: arpeggio
            elif abs(tilt) < 0.45 and roll < -ROLL_DEG:
                zone = "ROLL_LEFT"              # wrist rolled left: passing
        if zone != self._pose_zone:
            self._pose_zone = zone
            self._tilt_since = tw if zone else None
        elif zone is not None and self._tilt_since is not None:
            if (tw - self._tilt_since >= TILT_HOLD_MS
                    and tw - self._tilt_fired >= TILT_REFIRE_MS):
                self._tilt_fired = tw
                self._last_active = tw
                self._latched = zone
                self._latch_until = tw + LATCH_MS
                return True

        if tw - self._last_step >= STEP_MS:
            self._last_step = tw
            return self._classify(tw)
        return False

    # ── windowed features ────────────────────────────────────────────────────
    def _features(self) -> dict:
        dyaw = dpitch = rot = lift = dir_rot = 0.0
        ang_prev: float | None = None
        mags: list[float] = []
        sums = [0.0, 0.0, 0.0]
        for (_tw, dt, la, la_mag, yr, pr, gm) in self._win:
            dyaw += yr * dt
            dpitch += pr * dt
            rot += gm * dt
            mags.append(la_mag)
            for i in range(3):
                sums[i] += abs(la[i])
            if self._g is not None:
                gmag = math.sqrt(sum(g * g for g in self._g)) or 1.0
                lift += sum(x * g for x, g in zip(la, self._g)) / gmag * dt
            # circle detector: in a circular motion the (yaw, pitch) rotation
            # vector itself rotates; accumulate its direction change.
            if math.hypot(yr, pr) > 30.0:
                ang = math.atan2(pr, yr)
                if ang_prev is not None:
                    d = ang - ang_prev
                    while d > math.pi:
                        d -= 2 * math.pi
                    while d < -math.pi:
                        d += 2 * math.pi
                    dir_rot += d
                ang_prev = ang
        la_rms = math.sqrt(sum(m * m for m in mags) / len(mags)) if mags else 0.0
        peak = max(mags) if mags else 0.0
        # sign reversals of the dominant linear axis (shake detector)
        dom = max(range(3), key=lambda i: sums[i])
        reversals, prev = 0, 0.0
        for (_tw, _dt, la, *_rest) in self._win:
            v = la[dom]
            if abs(v) < _NOISE_ACC:
                continue
            if prev and (v > 0) != (prev > 0):
                reversals += 1
            prev = v
        return {"dyaw": dyaw, "dpitch": dpitch, "rot": rot, "lift": lift,
                "dir_rot": dir_rot, "la_rms": la_rms, "peak": peak,
                "reversals": reversals}

    def _classify(self, tw: float) -> bool:
        if len(self._win) < 5:
            return False
        ft = self._features()
        cand: str | None = None
        # straight strokes must be DOMINANTLY one axis AND straight (a swipe's
        # gyro direction is constant; a circle's rotates) — otherwise a
        # circle's quarters read as swipes before the loop closes
        straight = abs(ft["dir_rot"]) < math.radians(60.0)
        yaw_dom = abs(ft["dyaw"]) > 0.65 * ft["rot"] and straight
        pitch_dom = abs(ft["dpitch"]) > 0.65 * ft["rot"] and straight
        if ft["reversals"] >= SHAKE_REVERSALS and ft["la_rms"] > SHAKE_RMS:
            cand = "SHAKE"
        elif (ft["peak"] > STAB_ACC and ft["rot"] < 60.0
              and abs(ft["dyaw"]) < STAB_TRAVEL and abs(ft["dpitch"]) < STAB_TRAVEL):
            cand = "STAB"           # a spike that TRAVELS is a swipe, not a stab
        elif abs(ft["dyaw"]) > SWIPE_DEG and yaw_dom:
            cand = "RIGHT_SWIPE" if ft["dyaw"] > 0 else "LEFT_SWIPE"
        elif abs(ft["dpitch"]) > RAISE_DEG and pitch_dom:
            cand = "RAISE" if ft["dpitch"] > 0 else "LOWER"
        if cand is None:
            return False
        if cand == self._latched and tw < self._latch_until:
            self._latch_until = tw + LATCH_MS     # same stroke sustained: extend
            return False
        self._latched = cand
        self._latch_until = tw + LATCH_MS
        self._win.clear()                         # one motion = one stroke
        return True

    # ── outputs ──────────────────────────────────────────────────────────────
    def _current(self) -> str | None:
        tw = self._last_tw or 0.0
        if self._latched and tw < self._latch_until:
            return self._latched
        if tw - self._last_active > STILL_MS:
            return "STILL"
        return None

    def _meters(self) -> dict:
        if not self._win:
            return {"energy": 0.0, "size": 0.0, "lift": 0.0, "swirl": 0.0}
        ft = self._features()
        dur = max(0.05, (self._win[-1][0] - self._win[0][0]) / 1000.0)
        return {
            # same normalization spirit as gestures/features.py (_extract_imu)
            "energy": round(min(1.0, ft["la_rms"] / 8.0), 2),
            "size": round(min(1.0, 0.5 * ft["rot"] / 400.0 + 0.5 * ft["la_rms"] / 8.0), 2),
            "lift": round(max(-1.0, min(1.0, ft["lift"] / 2.0)), 2),
            "swirl": round(min(1.0, (ft["rot"] / dur) / 200.0), 2),
        }
