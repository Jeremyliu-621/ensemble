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


def test_right_swipe():
    tr = StrokeTracker()
    run(tr, frames(rest(0.3)))
    got, _ = run(tr, frames(gyro_pulse(6, 120.0, 0.45), t0=2000))
    assert got == ["RIGHT_SWIPE"], got


def test_left_swipe():
    tr = StrokeTracker()
    run(tr, frames(rest(0.3)))
    got, _ = run(tr, frames(gyro_pulse(6, -120.0, 0.45), t0=2000))
    assert got == ["LEFT_SWIPE"], got


def test_raise_lower():
    tr = StrokeTracker()
    run(tr, frames(rest(0.3)))
    got, _ = run(tr, frames(gyro_pulse(4, 100.0, 0.45), t0=2000))
    assert got == ["RAISE"], got
    tr2 = StrokeTracker()
    run(tr2, frames(rest(0.3)))
    got2, _ = run(tr2, frames(gyro_pulse(4, -100.0, 0.45), t0=2000))
    assert got2 == ["LOWER"], got2


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
        ((0.0, tilt45, G * math.cos(math.radians(42.0))), "HALF_RAISE"),
        ((0.0, G, 0.0), "RAISE"),
        ((G * 0.95, 0.0, G * 0.31), "ROLL_RIGHT"),    # rolled ~72 deg right
        ((-G * 0.95, 0.0, G * 0.31), "ROLL_LEFT"),
    ]
    for accel, want in cases:
        tr = StrokeTracker()
        run(tr, frames(rest(0.4)))
        hold = lambda i, t: accel + (0.0, 0.0, 0.0) if i < 90 else None  # noqa: E731
        got, _ = run(tr, frames(hold, t0=2000))
        assert want in got, f"{want}: {got}"


def test_stab():
    tr = StrokeTracker()
    run(tr, frames(rest(0.4)))

    def spec(i, t):
        if i >= 20:
            return None
        spike = 14.0 if 5 <= i < 9 else 0.0    # short hard jab, no rotation
        return (spike, 0.0, G, 0.0, 0.0, 0.0)
    got, _ = run(tr, frames(spec, t0=2000))
    assert got == ["STAB"], got


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
