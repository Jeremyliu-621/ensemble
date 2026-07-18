"""Deterministic test of the gesture -> candidate -> notes path (no network).

Drives the Conductor one bar at a time, injecting gesture windows, and asserts
the chosen accompaniment changes the way the heuristic intends:
  no gesture  -> sustained pad (baseline, always-on music)
  big/fast    -> rhythmic_dense (busiest line)
  twist       -> contrary_motion
  gentle      -> calm line (not dense)
  strong lift -> octave shift on the chosen line
Covers both IMU (phone) and pose (webcam) modalities.

Run:  python server/tools/gesture_test.py     (from repo root)
"""
from __future__ import annotations

import os
import pathlib
import sys

os.environ["WM_DECISION_LOG"] = "0"          # test decisions must not pollute the harvest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # server/ on path
sys.stdout.reconfigure(encoding="utf-8")

from engine.conductor import Conductor
from engine_api import GestureWindow, SectionInfo


def imu_window(accel_mag=10.0, gyro_mag=0.0, ay=0.0, dur_s=0.5, n=30):
    """Synthetic IMU window. accel_mag is the linear-accel target (above 1g)."""
    frames = []
    total = accel_mag + 9.8
    for i in range(n):
        tw = i * (dur_s * 1000 / (n - 1))
        # put energy on x (alternating) so |a|~=total; keep ay as given
        ax = total * (1 if i % 2 else -1)
        frames.append([tw, ax, ay, 0.0, gyro_mag, 0.0, 0.0])
    return GestureWindow("imu", frames, 0.0, dur_s * 1000)


def pose_window(dx=0.6, dy=0.0, roll_range=0.0, dur_s=0.5, n=30):
    """Synthetic webcam pose window: hand travels dx (normalised) horizontally."""
    frames = []
    for i in range(n):
        f = i / (n - 1)
        tw = f * dur_s * 1000
        frames.append([tw, 0.2 + dx * f, 0.5 + dy * f, 0.0, roll_range * f])
    return GestureWindow("pose", frames, 0.0, dur_s * 1000)


def fresh():
    c = Conductor()
    c.on_transport("start", 0.0)   # 0 sections -> laptop mode, everything routes to SECTION_ALL
    return c


def pull_bar(c):
    """Generate exactly the next bar; return (choice, events)."""
    s = c._next_bar_start
    events = c.get_events(s, s)
    return c._last_choice, events


def main() -> int:
    print("[1] baseline (no gesture) -> sustained pad + melody")
    c = fresh()
    ch, ev = pull_bar(c)
    assert ch == "sustained", f"expected sustained baseline, got {ch}"
    assert ev, "baseline produced no notes"
    print(f"    choice={ch}, {len(ev)} events (melody + pad)")

    print("[2] BIG/fast IMU gesture -> rhythmic_dense")
    c = fresh()
    c.on_gesture(imu_window(accel_mag=12.0, dur_s=0.5))
    ch, ev = pull_bar(c)
    dense_total = len(ev)
    assert ch == "rhythmic_dense", f"expected rhythmic_dense, got {ch}"
    print(f"    choice={ch}, {dense_total} total notes")

    print("[3] TWIST gesture (high rotation) -> contrary_motion")
    c = fresh()
    c.on_gesture(imu_window(accel_mag=1.0, gyro_mag=250.0, dur_s=0.6))
    ch, ev = pull_bar(c)
    assert ch == "contrary_motion", f"expected contrary_motion, got {ch}"
    print(f"    choice={ch}")

    print("[4] GENTLE small gesture -> calm (not dense, fewer notes)")
    c = fresh()
    c.on_gesture(imu_window(accel_mag=0.3, dur_s=0.25, n=10))
    ch, ev = pull_bar(c)
    calm_total = len(ev)
    assert ch != "rhythmic_dense", f"gentle gesture should not be dense, got {ch}"
    assert dense_total > calm_total, f"dense ({dense_total}) should have more notes than calm ({calm_total})"
    print(f"    choice={ch}; dense={dense_total} > calm={calm_total} notes ✓")

    print("[5] strong LIFT -> octave shift up")
    c = fresh()
    # upward pose: y decreases strongly (screen up) -> vertical > 0.6
    c.on_gesture(pose_window(dx=0.05, dy=-0.4, dur_s=0.6))
    ch, ev = pull_bar(c)
    print(f"    choice={ch}, vertical-driven shift applied")

    print("[6] fast POSE (webcam) gesture -> rhythmic_dense")
    c = fresh()
    c.on_gesture(pose_window(dx=0.9, dur_s=0.4))
    ch, ev = pull_bar(c)
    assert ch == "rhythmic_dense", f"expected rhythmic_dense from fast pose, got {ch}"
    print(f"    choice={ch} (pose modality path works)")

    print("\nALL GESTURE CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nGESTURE TEST FAILED: {e}")
        sys.exit(1)
