"""wand_demo.py — a fake UNO Q for demos and laptop testing.

Connects to the server as role "wand" and behaves like a person holding the
hardware wand: slowly sweeps the pointing beam left <-> right across the room,
and every few seconds "squishes" (grab) while waving energetically, releasing
after ~1s — so the console shows the live beam, cards glow as it passes, the
beam turns green during grabs, and the camera hub flashes what each gesture did.

Usage (server running):
    python server/tools/wand_demo.py            # ws://127.0.0.1:8080/ws
    WM_HTTP_PORT=8098 python server/tools/wand_demo.py
    python server/tools/wand_demo.py --seconds 120
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import websockets

PORT = os.environ.get("WM_HTTP_PORT", "8080")
WS = os.environ.get("WM_WS_URL", f"ws://127.0.0.1:{PORT}/ws")

RATE_HZ = 50          # IMU sample rate
BATCH = 5             # frames per wand.imu (like the board)
SWEEP_PERIOD_S = 9.0  # one full left->right->left sweep
SWEEP_DEG = 70.0      # sweep amplitude: ±70° covers the whole room
GRAB_EVERY_S = 7.0    # squish cadence
GRAB_LEN_S = 1.1


async def main(seconds: float) -> None:
    async with websockets.connect(WS) as ws:
        await ws.send(json.dumps({"t": "hello", "v": 1, "role": "wand", "session": "lol1"}))
        welcome = json.loads(await ws.recv())
        print(f"fake wand connected as {welcome.get('client_id', '?')[:8]} -> {WS}")
        print("watch the console: the beam sweeps the room; green = grabbing")

        async def drain() -> None:
            # the server pushes roster/engine.state/wand.cmd at us; if nobody
            # reads them the receive queue fills and the socket chokes on
            # keepalive — exactly what the real board's _downlink task is for
            async for _ in ws:
                pass
        drain_task = asyncio.create_task(drain())

        t0 = time.perf_counter()
        seq, frames = 0, []
        grabbed, next_grab, grab_until = False, GRAB_EVERY_S, 0.0
        # target yaw follows a sine; we emit the RATE (deg/s) the IMU would see
        prev_yaw = 0.0

        while (t := time.perf_counter() - t0) < seconds:
            yaw = SWEEP_DEG * math.sin(2 * math.pi * t / SWEEP_PERIOD_S)
            gz = (yaw - prev_yaw) * RATE_HZ          # deg/s to move to the new yaw
            prev_yaw = yaw
            wave = 25.0 * math.sin(2 * math.pi * t * 2.2) if grabbed else 0.0
            tw = round(t * 1000)
            frames.append([tw, wave * 0.3, 9.81, wave * 0.2, wave, wave * 0.6, gz])
            if len(frames) >= BATCH:
                seq += 1
                await ws.send(json.dumps({"t": "wand.imu", "seq": seq, "frames": frames}))
                frames = []

            if not grabbed and t >= next_grab:
                grabbed, grab_until = True, t + GRAB_LEN_S
                await ws.send(json.dumps({"t": "wand.grab", "state": "start", "tw": tw}))
                print(f"  squish  (t={t:5.1f}s, yaw {yaw:+.0f}°)")
            elif grabbed and t >= grab_until:
                grabbed, next_grab = False, t + GRAB_EVERY_S
                await ws.send(json.dumps({"t": "wand.grab", "state": "end", "tw": tw}))
                print(f"  release (t={t:5.1f}s)")

            await asyncio.sleep(1.0 / RATE_HZ)
        drain_task.cancel()
        print("demo done")


async def strokes_mode(loops: int) -> None:
    """Perform the whole stroke vocabulary, announced, so the console panel can
    be watched end-to-end with zero hardware (and the e2e test can assert)."""
    import math as _m

    def gyro(name, gx=0.0, gz=0.0, dur=0.45):
        return (name, lambda t: (0.0, 0.0, 9.81, gx, 0.0, gz), dur)

    def pose(name, ax=0.0, ay=0.0, az=9.81, dur=1.6):
        """A held orientation: gravity sits on the given axes, no motion."""
        return (name, lambda t: (ax, ay, az, 0.0, 0.0, 0.0), dur)

    def turn_hold(name, gz, dur=1.8):
        """Yaw-turn for 0.5s, then hold still: enters a POINT_LEFT/RIGHT zone.
        Yaw INTEGRATES across segments, so the script must return to zero."""
        return (name, lambda t: (0.0, 0.0, 9.81, 0.0, 0.0, gz if t < 0.5 else 0.0), dur)

    def shake(t):
        return (9.0 * _m.sin(2 * _m.pi * 7 * t), 0.0, 9.81, 0.0, 0.0, 0.0)

    SCRIPT = [
        turn_hold("ARPEGGIO (right pole)", 168.0),     # yaw 0 -> +84
        turn_hold("RUNS (left pole)", -336.0),         # +84 -> -84
        turn_hold("recenter", 168.0),                  # -84 -> 0 (commits nothing)
        pose("HARMONY (up pole)", ay=9.81, az=0.0),
        pose("HUSH (down pole)", ay=-9.81, az=0.0),
        ("SHAKE", shake, 0.7),
    ]

    async with websockets.connect(WS) as ws:
        await ws.send(json.dumps({"t": "hello", "v": 1, "role": "wand", "session": "lol1"}))
        await ws.recv()
        print(f"fake wand (stroke mode) -> {WS}")

        async def drain() -> None:
            async for _ in ws:
                pass
        drain_task = asyncio.create_task(drain())

        seq, tw = 0, 1000.0
        for loop_i in range(loops):
            for (name, fn, dur) in SCRIPT:
                print(f"  performing {name}")
                t, frames = 0.0, []
                while t < dur:
                    ax, ay, az, gx, gy, gz = fn(t)
                    frames.append([round(tw), ax, ay, az, gx, gy, gz])
                    if len(frames) >= BATCH:
                        seq += 1
                        await ws.send(json.dumps({"t": "wand.imu", "seq": seq, "frames": frames}))
                        frames = []
                        await asyncio.sleep(BATCH / RATE_HZ)
                    t += 1.0 / RATE_HZ
                    tw += 1000.0 / RATE_HZ
                # settle: stillness between strokes (lets the latch expire)
                for _ in range(int(1.6 * RATE_HZ / BATCH)):
                    seq += 1
                    batch = [[round(tw + i * 20), 0.0, 0.0, 9.81, 0.0, 0.0, 0.0] for i in range(BATCH)]
                    tw += BATCH * 20
                    await ws.send(json.dumps({"t": "wand.imu", "seq": seq, "frames": batch}))
                    await asyncio.sleep(BATCH / RATE_HZ)
        drain_task.cancel()
        print("stroke demo done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--strokes", action="store_true",
                    help="perform the stroke vocabulary instead of sweeping")
    ap.add_argument("--loops", type=int, default=3)
    args = ap.parse_args()
    asyncio.run(strokes_mode(args.loops) if args.strokes else main(args.seconds))
