"""StrokeTracker unit tests: synthetic 50 Hz IMU windows must classify to the
right stroke, and noise/stillness must never commit one.

Run:  python server/tests/test_strokes.py   (or pytest)
"""
from __future__ import annotations

import math
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gestures.strokes import StrokeTracker  # noqa: E402

HZ, DT_MS = 50, 20
G = 9.81


def frames(spec, t0=1000.0):
    """spec: callable(i, t_s) -> (ax, ay, az, gx, gy, gz); returns 1 batch/frame."""
    out = []
    i = 0
    t = t0
    while True:
        row = spec(i, (t - t0) / 1000.0)
        if row is None:
            break
        ax, ay, az, gx, gy, gz = row
        out.append([t, ax, ay, az, gx, gy, gz])
        i += 1
        t += DT_MS
    return out


def run(tracker, rows):
    """Feed frames in batches of 5 (like the board); collect committed strokes."""
    committed = []
    last = (None, {}, False)
    for k in range(0, len(rows), 5):
        last = tracker.push(rows[k:k + 5])
        if last[2]:
            committed.append(last[0])
    return committed, last


def rest(duration_s):
    n = int(duration_s * HZ)
    return lambda i, t: (0.0, 0.0, G, 0.0, 0.0, 0.0) if i < n else None


def gyro_pulse(axis, dps, duration_s):
    """Rotation on one gyro axis (4=gx,5=gy,6=gz) for duration_s."""
    n = int(duration_s * HZ)

    def spec(i, t):
        if i >= n:
            return None
        g = [0.0, 0.0, 0.0]
        g[axis - 4] = dps
        return (0.0, 0.0, G, *g)
    return spec


def test_point_right_left_yaw_zones():
    """Turn past ±35° of the calibrated forward and dwell -> POINT_RIGHT /
    POINT_LEFT. recal() re-zeroes forward."""
    tr = StrokeTracker()
    run(tr, frames(rest(0.3)))
    turn = frames(gyro_pulse(6, 120.0, 0.5), t0=2000)     # +60 deg
    dwell = frames(rest(1.0), t0=2600)
    got, _ = run(tr, turn + dwell)
    assert "POINT_RIGHT" in got, got
    tr.recal()                                            # here = new forward
    got2, _ = run(tr, frames(rest(1.6), t0=4000))
    assert "POINT_RIGHT" not in got2, got2                 # recal cleared the zone
    turn_l = frames(gyro_pulse(6, -120.0, 0.5), t0=6000)  # -60 deg from new zero
    dwell_l = frames(rest(1.0), t0=6600)
    got3, _ = run(tr, turn_l + dwell_l)
    assert "POINT_LEFT" in got3, got3


def test_motion_pitch_pulses_commit_nothing():
    """Motion detection is gone: a pitch-rate pulse with level gravity is not
    a RAISE — only actually POINTING up (gravity) commits."""
    tr = StrokeTracker()
    run(tr, frames(rest(0.3)))
    got, _ = run(tr, frames(gyro_pulse(4, 100.0, 0.45), t0=2000))
    assert "RAISE" not in got and "LOWER" not in got, got


def test_circle_motion_no_longer_commits():
    """CIRCLE detection was cut (false-fired on ordinary waving): loop motion
    must commit NOTHING — arpeggio comes from the ROLL poses now."""
    tr = StrokeTracker()

    def spec(i, t):
        if t >= 1.2:
            return None
        w = 2 * math.pi / 0.6                 # two full loops in 1.2s
        return (0.0, 0.0, G, 260.0 * math.sin(w * t), 0.0, 260.0 * math.cos(w * t))
    got, _ = run(tr, frames(spec))
    assert "CIRCLE" not in got, got


def test_pose_zones():
    """Held poses commit from gravity alone: half-up = HALF_RAISE, full-up =
    RAISE, wrist rolls = ROLL_RIGHT/ROLL_LEFT."""
    tilt45 = G * math.sin(math.radians(42.0))     # ~0.67g on the lift axis
    cases = [
        ((0.0, tilt45, G * math.cos(math.radians(42.0))), "RAISE"),   # 42 deg counts as up
        ((0.0, G, 0.0), "RAISE"),
        ((0.0, -G, 0.0), "LOWER"),
        ((G * 0.95, 0.0, G * 0.31), "ROLL_RIGHT"),    # rolled ~72 deg right
        ((-G * 0.95, 0.0, G * 0.31), "ROLL_LEFT"),
    ]
    for accel, want in cases:
        tr = StrokeTracker()
        run(tr, frames(rest(0.4)))
        hold = lambda i, t: accel + (0.0, 0.0, 0.0) if i < 90 else None  # noqa: E731
        got, _ = run(tr, frames(hold, t0=2000))
        assert want in got, f"{want}: {got}"


def test_stab_no_longer_commits():
    """STAB was cut with the rest of motion detection — an accel spike must
    commit nothing (it constantly false-fired on pose transitions)."""
    tr = StrokeTracker()
    run(tr, frames(rest(0.4)))

    def spec(i, t):
        if i >= 20:
            return None
        spike = 14.0 if 5 <= i < 9 else 0.0    # short hard jab, no rotation
        return (spike, 0.0, G, 0.0, 0.0, 0.0)
    got, _ = run(tr, frames(spec, t0=2000))
    assert "STAB" not in got, got


def test_shake():
    tr = StrokeTracker()

    def spec(i, t):
        if t >= 0.7:
            return None
        return (9.0 * math.sin(2 * math.pi * 7 * t), 0.0, G, 0.0, 0.0, 0.0)
    got, _ = run(tr, frames(spec))
    assert "SHAKE" in got, got


def test_tilt_hold_commits_raise_lower():
    """Pointing the wand clearly up/down and holding calmly = RAISE/LOWER —
    a pure gravity read (the robust path for real hardware)."""
    tr = StrokeTracker()
    run(tr, frames(rest(0.4)))
    up = lambda i, t: (0.0, G, 0.0, 0.0, 0.0, 0.0) if i < 90 else None  # noqa: E731
    got, _ = run(tr, frames(up, t0=2000))
    assert "RAISE" in got, got
    tr2 = StrokeTracker()
    run(tr2, frames(rest(0.4)))
    down = lambda i, t: (0.0, -G, 0.0, 0.0, 0.0, 0.0) if i < 90 else None  # noqa: E731
    got2, _ = run(tr2, frames(down, t0=2000))
    assert "LOWER" in got2, got2


def test_recal_pose_is_neutral():
    """Recalibrating in ANY pose makes that pose the silent neutral — zones
    fire only on departure from it, and returning goes quiet again."""
    tilt40 = (0.0, G * math.sin(math.radians(40.0)), G * math.cos(math.radians(40.0)))
    tr = StrokeTracker()
    hold40 = lambda i, t: tilt40 + (0.0, 0.0, 0.0) if i < 60 else None  # noqa: E731
    run(tr, frames(hold40))              # wand held raised ~40 deg from the start
    tr.recal()                           # <- this raised pose becomes neutral
    got, _ = run(tr, frames(hold40, t0=3000))
    assert got == [], f"neutral pose fired {got}"
    up90 = lambda i, t: (0.0, G, 0.0, 0.0, 0.0, 0.0) if i < 90 else None  # noqa: E731
    got2, _ = run(tr, frames(up90, t0=6000))
    assert "RAISE" in got2, got2         # 50 deg further up: now it's a raise
    got3, _ = run(tr, frames(hold40, t0=9000))
    assert "RAISE" not in got3, got3     # back to the recal'd pose: quiet


def test_still_and_noise_never_commit():
    tr = StrokeTracker()
    _, last = run(tr, frames(rest(1.2)))
    assert last[0] == "STILL", last

    tr2 = StrokeTracker()
    import random
    rnd = random.Random(7)

    def noisy(i, t):
        if t >= 1.5:
            return None
        return (rnd.uniform(-0.5, 0.5), rnd.uniform(-0.5, 0.5), G + rnd.uniform(-0.5, 0.5),
                rnd.uniform(-10, 10), rnd.uniform(-10, 10), rnd.uniform(-10, 10))
    got, _ = run(tr2, frames(noisy))
    assert got == [], f"noise committed a stroke: {got}"


def test_meters_move():
    # feed an energetic burst and check the meters respond
    tr2 = StrokeTracker()
    _, last = run(tr2, frames(gyro_pulse(6, 200.0, 0.4)))
    assert last[1]["swirl"] > 0.3, last[1]
    tr3 = StrokeTracker()
    _, last3 = run(tr3, frames(rest(1.0)))
    assert last3[1]["energy"] < 0.1, last3[1]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  {name} ✓")
    print("\nSTROKE UNIT TESTS PASSED ✓")
