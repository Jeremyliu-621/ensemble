"""Headless end-to-end smoke test for the P0/P1 server.

Boots nothing itself — assumes `python server/main.py` is already running on
:8080. Exercises: static file serving, hello/welcome, clock ping/pong, section
join + ready, admin start, and receipt of scheduled metronome events. This
validates the entire realtime path except browser audio (which needs a device).

Run:  python server/tools/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import urllib.request

# Windows consoles default to cp1252; force UTF-8 so status glyphs don't crash.
sys.stdout.reconfigure(encoding="utf-8")

import websockets

import os

PORT = os.environ.get("WM_HTTP_PORT", "8080")
HTTP = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}/ws"
V = 1


def check_static() -> None:
    for path in ("/console/", "/editor/", "/section/", "/shared/protocol.js"):
        with urllib.request.urlopen(HTTP + path, timeout=3) as r:
            body = r.read()
            assert r.status == 200 and body, f"static {path} -> {r.status}"
            print(f"  static {path:24s} {r.status} {len(body)}B  {r.headers['Content-Type']}")


async def hello(ws, role, session="lol1"):
    await ws.send(json.dumps({"t": "hello", "v": V, "role": role, "session": session, "client_id": None}))
    welcome = json.loads(await ws.recv())
    assert welcome["t"] == "welcome", welcome
    return welcome


async def collect_notes(ws, seconds: float) -> list:
    got = []
    end = time.time() + seconds
    while time.time() < end:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5))
        except asyncio.TimeoutError:
            continue
        if m["t"] == "sched.notes":
            got.extend(m["events"])
    return got


async def clock_roundtrip(ws) -> float:
    t0 = time.perf_counter() * 1000
    await ws.send(json.dumps({"t": "clock.ping", "id": 1, "t0": t0}))
    while True:
        m = json.loads(await ws.recv())
        if m["t"] == "clock.pong":
            assert m["id"] == 1 and m["t0"] == t0 and isinstance(m["ts"], (int, float)), m
            rtt = time.perf_counter() * 1000 - t0
            return rtt


async def main() -> int:
    print("[1] static files")
    check_static()

    print("[2] section join + clock + ready")
    async with websockets.connect(WS) as sec:
        w = await hello(sec, "section")
        sid = w["config"]["section_id"]
        print(f"  welcome: section_id={sid} instrument={w['config']['instrument']} server_time={w['config'] and w['server_time']:.0f}ms")
        rtt = await clock_roundtrip(sec)
        print(f"  clock.pong ok (loopback rtt {rtt:.2f}ms)")
        await sec.send(json.dumps({"t": "section.ready"}))

        print("[3] admin start -> expect musical sched.notes (melody + accompaniment)")
        async with websockets.connect(WS) as adm:
            await hello(adm, "admin")
            await adm.send(json.dumps({"t": "admin.cmd", "cmd": "start"}))

            got = await collect_notes(sec, 3.0)
            assert got, "no sched.notes received after start!"
            note_re = re.compile(r"^[A-G]#?-?\d$")
            for k in ("id", "section", "at", "dur", "note", "vel"):
                assert k in got[0], f"event missing {k}: {got[0]}"
            assert all(note_re.match(e["note"]) for e in got), "non-musical note name emitted"
            hit = {e["section"] for e in got}
            print(f"  {len(got)} events routed to sections {sorted(hit)}")
            # one section joined -> events route to it (SECTION_ALL only when laptop-only)
            assert sid in hit or "all" in hit, f"events not routed to the section: {hit}"

            print("[4] live wand gesture -> engine keeps emitting (path works)")
            async with websockets.connect(WS) as wand:
                await hello(wand, "wand-sim")
                await wand.send(json.dumps({"t": "wand.grab", "state": "start", "tw": 0}))
                frames = [[i * 16.0, (20 if i % 2 else -20), 0.0, 0.0, 0.0, 0.0, 0.0] for i in range(30)]
                await wand.send(json.dumps({"t": "wand.imu", "seq": 0, "frames": frames}))
                await wand.send(json.dumps({"t": "wand.grab", "state": "end", "tw": 480}))
                after = await collect_notes(sec, 3.0)
                assert after, "events stopped flowing after a gesture!"
                print(f"  gesture accepted; {len(after)} more events flowed")

            await adm.send(json.dumps({"t": "admin.cmd", "cmd": "stop"}))

    print("\nALL CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as e:  # noqa: BLE001
        print(f"\nSMOKE TEST FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
