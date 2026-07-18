"""Terminal monitor and guided physical probe for the hardware wand.

Interactive mode observes aiming and drives downlink state with single-letter
commands. ``--probe`` runs a timed stationary/moving/stationary test and exits
with an objective PASS/FAIL status. ``--check-server`` performs only a protocol
handshake and is used by the deployment launcher.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import threading
import time
from dataclasses import dataclass

import websockets
from websockets.asyncio.client import connect

from network_address import websocket_url


@dataclass(frozen=True)
class ProbeSnapshot:
    elapsed: float
    received_at: float
    yaw_deg: float
    imu: dict


@dataclass(frozen=True)
class ProbeCheck:
    name: str
    passed: bool
    detail: str
    boundary: str


def _stdin_thread(loop, queue: asyncio.Queue) -> None:
    """Read single-line commands off stdin without blocking the event loop."""
    for line in sys.stdin:
        loop.call_soon_threadsafe(queue.put_nowait, line.strip().lower())
    loop.call_soon_threadsafe(queue.put_nowait, "q")


def _describe(msg: dict) -> str | None:
    """Render an inbound server message as a human line, or None to skip."""
    t = msg.get("t")
    if t == "welcome":
        return f"connected as admin (client_id={msg.get('client_id', '?')[:8]})"
    if t == "roster":
        wand = msg.get("wand", {})
        sections = msg.get("sections", [])
        ready = sum(1 for section in sections if section.get("ready"))
        return (f"roster: wand connected={wand.get('connected')} variant={wand.get('variant')} "
                f"mode={wand.get('mode')} | sections={len(sections)} ({ready} ready) "
                f"| playing={msg.get('playing')}")
    if t == "wand.state":
        imu = msg.get("imu") or {}
        health = (f" frames={imu.get('frames')} batches={imu.get('batches')} "
                  f"invalid={imu.get('invalid_frames')} gaps={imu.get('seq_gaps')}")
        return (f"wand.state: aim={msg.get('aim_section')} yaw={msg.get('yaw_deg')}° "
                f"grabbed={msg.get('grabbed')}{health}")
    if t == "engine.state":
        return f"engine.state: playing={msg.get('playing')} last_choice={msg.get('last_choice')}"
    if t == "err":
        return f"ERR {msg.get('code')}: {msg.get('msg')}"
    return None


def _finite(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _vectors(snapshots: list[ProbeSnapshot], start: float, end: float) -> list[list[float]]:
    rows: list[list[float]] = []
    for snapshot in snapshots:
        if not start <= snapshot.elapsed <= end:
            continue
        frame = snapshot.imu.get("last_frame")
        if not isinstance(frame, list) or len(frame) != 7:
            continue
        values = [_finite(value) for value in frame]
        if all(value is not None for value in values):
            rows.append([float(value) for value in values])
    return rows


def _median_magnitude(rows: list[list[float]], indexes: tuple[int, int, int]) -> float | None:
    if not rows:
        return None
    magnitudes = [math.sqrt(sum(row[index] ** 2 for index in indexes)) for row in rows]
    return statistics.median(magnitudes)


def _counter(snapshot: ProbeSnapshot, key: str) -> int:
    value = snapshot.imu.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def evaluate_probe(
    snapshots: list[ProbeSnapshot],
    duration: float,
    hardware_connected: bool = True,
) -> list[ProbeCheck]:
    """Evaluate captured diagnostics independently of WebSocket I/O."""
    checks: list[ProbeCheck] = []

    def add(name: str, passed: bool, detail: str, boundary: str) -> None:
        checks.append(ProbeCheck(name, passed, detail, boundary))

    add(
        "hardware wand",
        hardware_connected,
        "server roster reports connected variant=hw" if hardware_connected
        else "hardware wand did not connect before timeout",
        "deployment / WiFi / URL / handshake",
    )

    if len(snapshots) < 2:
        detail = f"only {len(snapshots)} diagnostic update(s) received"
        for name in ("sample rate", "batch rate", "valid frames", "sequence continuity",
                     "receive continuity", "gravity units", "initial stillness",
                     "final stillness", "physical yaw movement"):
            add(name, False, detail, "MCU sensor / Bridge callback / WiFi stream")
        return checks

    first, last = snapshots[0], snapshots[-1]
    span = max(0.001, last.received_at - first.received_at)
    frame_rate = (_counter(last, "frames") - _counter(first, "frames")) / span
    batch_rate = (_counter(last, "batches") - _counter(first, "batches")) / span
    add("sample rate", 45.0 <= frame_rate <= 70.0, f"{frame_rate:.1f} frames/s (need 45–70)",
        "MCU timing / Bridge delivery / queue pressure / WiFi")
    add("batch rate", 8.0 <= batch_rate <= 15.0, f"{batch_rate:.1f} batches/s (need 8–15)",
        "Linux batching / queue pressure / WiFi")

    invalid = _counter(last, "invalid_frames")
    gaps = _counter(last, "seq_gaps")
    add("valid frames", invalid == 0, f"{invalid} invalid frame(s)",
        "serialization / numeric validation / sensor output")
    add("sequence continuity", gaps == 0, f"{gaps} missing batch sequence(s)",
        "Linux batching / reconnect / WiFi")

    intervals = [b.received_at - a.received_at for a, b in zip(snapshots, snapshots[1:])]
    # Once diagnostics have started, silence through the end of the requested
    # capture is also a stream outage even though there is no later snapshot to
    # form a pair with.
    intervals.append(max(0.0, duration - last.elapsed))
    max_silence = max(intervals, default=float("inf"))
    add("receive continuity", max_silence <= 1.0, f"longest server-state gap {max_silence:.3f}s",
        "MCU timing / Bridge delivery / queue pressure / WiFi")

    first_end = duration * (8.0 / 30.0)
    movement_end = duration * (20.0 / 30.0)
    initial_rows = _vectors(snapshots, 0.0, first_end)
    moving_rows = _vectors(snapshots, first_end, movement_end)
    final_rows = _vectors(snapshots, movement_end, duration + 0.5)

    gravity = _median_magnitude(initial_rows, (1, 2, 3))
    gravity_ok = gravity is not None and 8.0 <= gravity <= 11.5
    add("gravity units", gravity_ok,
        "no valid stationary samples" if gravity is None else f"median |accel| {gravity:.2f} m/s²",
        "sensor read / g-to-m/s² conversion")

    initial_gyro = _median_magnitude(initial_rows, (4, 5, 6))
    final_gyro = _median_magnitude(final_rows, (4, 5, 6))
    add("initial stillness", initial_gyro is not None and initial_gyro < 5.0,
        "no valid initial samples" if initial_gyro is None else f"median |gyro| {initial_gyro:.2f} deg/s",
        "sensor stability / board was not held still")
    add("final stillness", final_gyro is not None and final_gyro < 5.0,
        "no valid final samples" if final_gyro is None else f"median |gyro| {final_gyro:.2f} deg/s",
        "sensor stability / board was not held still")

    moving_yaws = [snapshot.yaw_deg for snapshot in snapshots
                   if first_end <= snapshot.elapsed <= movement_end]
    yaw_span = max(moving_yaws) - min(moving_yaws) if moving_yaws else 0.0
    max_yaw_rate = max((abs(row[6]) for row in moving_rows), default=0.0)
    moved = yaw_span >= 20.0 or max_yaw_rate >= 20.0
    add("physical yaw movement", moved,
        f"yaw span {yaw_span:.1f}°, peak |gz| {max_yaw_rate:.1f} deg/s",
        "gyro axes / physical sensor reading")
    return checks


def _print_checks(checks: list[ProbeCheck]) -> bool:
    print("\n[probe] results")
    print(f"  {'RESULT':7s}  {'CHECK':22s}  DETAIL")
    print(f"  {'-' * 7}  {'-' * 22}  {'-' * 42}")
    for check in checks:
        result = "PASS" if check.passed else "FAIL"
        print(f"  {result:7s}  {check.name:22s}  {check.detail}")
        if not check.passed:
            print(f"           likely boundary: {check.boundary}")
    passed = all(check.passed for check in checks)
    print(f"\n[probe] {'PASS' if passed else 'FAIL'}")
    return passed


async def _send_hello(ws, session: str) -> dict:
    await ws.send(json.dumps({
        "t": "hello", "v": 1, "role": "admin", "session": session, "client_id": None,
    }))
    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    welcome = json.loads(raw)
    if welcome.get("t") != "welcome" or welcome.get("v") != 1 or welcome.get("role") != "admin":
        raise RuntimeError(f"incompatible server response: {welcome!r}")
    return welcome


async def check_server(url: str, session: str) -> int:
    """Return 0 for Phoneharmonic, 2 for unreachable, 3 for incompatible."""
    try:
        async with connect(url, open_timeout=3.0) as ws:
            await _send_hello(ws, session)
        return 0
    except (OSError, asyncio.TimeoutError, websockets.ConnectionClosed):
        return 2
    except (RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return 3


async def run_probe(url: str, session: str, duration: float, startup_timeout: float) -> int:
    print(f"[probe] connecting to {url}")
    try:
        async with connect(url) as ws:
            welcome = await _send_hello(ws, session)
            print(f"[probe] admin connected as {welcome['client_id'][:8]}")
            print("[probe] close any browser/CV wand; only one wand may own the active slot")

            connected = False
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline and not connected:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(0.5, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                if msg.get("t") == "roster":
                    wand = msg.get("wand") or {}
                    connected = bool(wand.get("connected") and wand.get("variant") == "hw")

            if not connected:
                return 0 if _print_checks(evaluate_probe([], duration, hardware_connected=False)) else 1

            first_end = duration * (8.0 / 30.0)
            movement_end = duration * (20.0 / 30.0)
            started = time.monotonic()
            transitions = [
                (0.0, "HOLD STILL: place the board flat and do not move it"),
                (first_end, "MOVE: rotate the board clearly around its vertical/yaw axis"),
                (movement_end, "HOLD STILL AGAIN: stop moving the board"),
            ]
            transition_index = 0
            snapshots: list[ProbeSnapshot] = []

            while True:
                now = time.monotonic()
                elapsed = now - started
                while (transition_index < len(transitions)
                       and elapsed >= transitions[transition_index][0]):
                    print(f"[probe {elapsed:5.1f}s] {transitions[transition_index][1]}")
                    transition_index += 1
                if elapsed >= duration:
                    break

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(0.25, duration - elapsed))
                except asyncio.TimeoutError:
                    continue
                received = time.monotonic()
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if msg.get("t") != "wand.state" or not isinstance(msg.get("imu"), dict):
                    continue
                yaw = _finite(msg.get("yaw_deg"))
                if yaw is None:
                    continue
                snapshots.append(ProbeSnapshot(
                    elapsed=received - started,
                    received_at=received,
                    yaw_deg=yaw,
                    imu=msg["imu"],
                ))

            return 0 if _print_checks(evaluate_probe(snapshots, duration)) else 1
    except (OSError, asyncio.TimeoutError, websockets.ConnectionClosed,
            RuntimeError, ValueError, TypeError) as exc:
        print(f"[probe] connection failed: {exc}")
        _print_checks(evaluate_probe([], duration, hardware_connected=False))
        return 1


async def run_interactive(url: str, session: str) -> None:
    key_to_msg = {
        "s": {"t": "admin.cmd", "cmd": "start"},
        "x": {"t": "admin.cmd", "cmd": "stop"},
        "d": {"t": "wand.mode", "mode": "det"},
        "a": {"t": "wand.mode", "mode": "ai"},
    }
    loop = asyncio.get_running_loop()
    command_queue: asyncio.Queue = asyncio.Queue()
    threading.Thread(target=_stdin_thread, args=(loop, command_queue), daemon=True).start()

    print(f"[monitor] connecting to {url} …  keys: s=start x=stop d=det a=ai q=quit")
    async with connect(url) as ws:
        welcome = await _send_hello(ws, session)
        print("  <-", _describe(welcome))

        async def rx() -> None:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                line = _describe(msg)
                if line:
                    print("  <-", line)

        async def tx() -> None:
            while True:
                key = await command_queue.get()
                if key == "q":
                    return
                out = key_to_msg.get(key)
                if out:
                    await ws.send(json.dumps(out))
                    print("  ->", out)
                elif key:
                    print(f"  (unknown key {key!r})")

        _, pending = await asyncio.wait(
            {asyncio.create_task(rx()), asyncio.create_task(tx())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", default="127.0.0.1", help="laptop LAN IP running the server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--session", default="lol1")
    parser.add_argument("--probe", action="store_true", help="run the guided physical PASS/FAIL test")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--check-server", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.duration <= 0 or args.startup_timeout <= 0:
        parser.error("--duration and --startup-timeout must be positive")

    try:
        url = websocket_url(args.ip, args.port)
    except ValueError as exc:
        parser.error(str(exc))
    if args.check_server:
        return asyncio.run(check_server(url, args.session))
    if args.probe:
        return asyncio.run(run_probe(url, args.session, args.duration, args.startup_timeout))
    try:
        asyncio.run(run_interactive(url, args.session))
        return 0
    except (KeyboardInterrupt, websockets.ConnectionClosed):
        return 0


if __name__ == "__main__":
    sys.exit(main())
