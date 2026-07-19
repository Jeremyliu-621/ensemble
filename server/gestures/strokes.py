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
# ── pose zones: the whole vocabulary, RELATIVE to the calibrated neutral ─────
# Wherever the wand points at recal time (or its first calm hold) is NEUTRAL —
# no zone fires there. Zones are departures FROM that pose, in degrees.
PITCH_ZONE_DEG = 30.0  # raise/lower the tip this far from neutral
ROLL_DEG = 55.0        # wrist roll (about the wand's long axis) from neutral
ROLL_SIGN = float(os.environ.get("WM_ROLL_SIGN", "1"))   # -1 if rolls read mirrored
YAW_ZONE_DEG = 35.0    # pointed left/right of neutral (integrated yaw)
BASELINE_CALM_MS = 350.0  # first hold this calm = the auto-captured neutral
TILT_HOLD_MS = 600.0   # zone held that long (calmly) -> commits
TILT_REFIRE_MS = 1400.0  # ...and re-commits while held, so a hush stays down
TILT_CALM_RMS = 2.5    # poses read only while the wand is otherwise quiet
SHAKE_REVERSALS = 4   # sign flips of the dominant linear axis
SHAKE_RMS = 3.0       # ...with at least this much vigor

_GRAV_ALPHA = 0.08    # gravity EMA
_NOISE_ACC = 1.5      # ignore reversals below this (m/s^2)

# Motion detection (swipes/circle/stab/motion-raise) was cut wholesale: it
# needed an unrealistically steady hand and false-fired constantly on real
# hardware. The vocabulary is WHERE THE WAND POINTS, held ~0.6s:
#   UP = swell · DOWN = hush · RIGHT = harmony · LEFT = runs · ROLL = arpeggio
# SHAKE is the one motion survivor (rapid reversals — robust, and select-all
# needs it). LEFT/RIGHT use the same integrated yaw as the aiming beam, so
# the console's recalibrate button re-zeroes "forward" for both.
STROKES = ("POINT_LEFT", "POINT_RIGHT", "RAISE", "LOWER",
           "ROLL_LEFT", "ROLL_RIGHT", "SHAKE", "STILL")


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
        self._yaw = 0.0                        # integrated yaw for LEFT/RIGHT zones
        self._pitch0: float | None = None      # calibrated neutral pitch (deg)
        self._roll0 = 0.0                      # calibrated neutral roll (deg)
        self._base_calm_since: float | None = None

    def _angles(self) -> tuple[float, float]:
        """(pitch, roll) of the wand in degrees, from the gravity EMA."""
        g = self._g or [0.0, 0.0, 9.8]
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, g[1] / 9.8))))
        roll = math.degrees(math.atan2(g[0], g[2])) * ROLL_SIGN
        return pitch, roll

    def recal(self) -> None:
        """The pose the wand is in RIGHT NOW becomes neutral — no zone fires
        there; zones are departures from it. The console's 🎯 button calls
        this alongside the aimer's recal: one shared neutral for beam and
        zones. Before any frames arrive, the first calm hold auto-captures."""
        self._yaw = 0.0
        if self._g is not None:
            self._pitch0, self._roll0 = self._angles()
        else:
            self._pitch0 = None
        self._base_calm_since = None

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

        # Pose zones ARE the vocabulary — and they're RELATIVE to the
        # calibrated neutral: wherever the wand pointed at recal (or its first
        # calm hold) fires nothing; zones are departures from that pose, held
        # calmly ~0.6s. A held zone RE-commits, so "pointed at the floor"
        # keeps the room hushed. Switching zones restarts the hold clock.
        self._yaw += yaw_rate * dt
        pitch, roll = self._angles()
        if self._pitch0 is None:                 # auto-baseline: first calm hold
            if la_mag < TILT_CALM_RMS:
                if self._base_calm_since is None:
                    self._base_calm_since = tw
                elif tw - self._base_calm_since >= BASELINE_CALM_MS:
                    self._pitch0, self._roll0 = pitch, roll
            else:
                self._base_calm_since = None
        zone: str | None = None
        if self._pitch0 is not None and la_mag < TILT_CALM_RMS:
            dpitch = pitch - self._pitch0
            droll = ((roll - self._roll0 + 180.0) % 360.0) - 180.0
            if dpitch > PITCH_ZONE_DEG:
                zone = "RAISE"                  # raised from neutral: the swell
            elif dpitch < -PITCH_ZONE_DEG:
                zone = "LOWER"                  # dropped from neutral: hush
            elif droll > ROLL_DEG:
                zone = "ROLL_RIGHT"             # wrist rolled: arpeggio
            elif droll < -ROLL_DEG:
                zone = "ROLL_LEFT"
            elif self._yaw > YAW_ZONE_DEG:
                zone = "POINT_RIGHT"            # right of neutral: harmony
            elif self._yaw < -YAW_ZONE_DEG:
                zone = "POINT_LEFT"             # left of neutral: runs
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
        # SHAKE is the only surviving MOTION stroke (rapid reversals — robust
        # without a steady hand, and select-all needs it). Everything else is
        # pose zones, handled in _ingest.
        cand: str | None = None
        if ft["reversals"] >= SHAKE_REVERSALS and ft["la_rms"] > SHAKE_RMS:
            cand = "SHAKE"
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
