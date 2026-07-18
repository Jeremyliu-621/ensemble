"""Tests for the reworked flow: solo isolation on aim, LLM-arranger part
routing, palm transport (rewind/forward + wand-role permission), deterministic
mode expression, and the mode toggle's ledger trail.

Units run in-process; the integration half spawns a real server.

Run:  python server/tools/rework_test.py     (from repo root)
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

os.environ["WM_DECISION_LOG"] = "0"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # server/ on path

from websockets.asyncio.client import connect

import arranger
from engine.conductor import Conductor
from gestures.features import GestureFeatures
from engine.midi_load import load_midi_bytes
from engine_api import SectionInfo
from midi_test import make_test_midi

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
PORT = int(os.environ.get("WM_TEST_PORT", "8096"))


def loaded_conductor() -> Conductor:
    song, parts = load_midi_bytes(make_test_midi(), "test.mid")
    c = Conductor()
    c.load_song(song, parts)
    c.on_sections_changed([SectionInfo("s1", "flute", 0.0, True),
                           SectionInfo("s2", "cello", 0.0, True)])
    c.on_transport("start", 0.0)
    return c


def units() -> None:
    print("[1] aiming SOLOS the targeted phone (everyone else mutes, instantly)")
    c = loaded_conductor()
    c.on_aim("s2")
    cancels = c.get_cancels()
    assert [sp.section for sp in cancels] == ["s1"], cancels   # in-flight cut, no bar wait
    ev = c.get_events(0.0, 0.0)
    assert ev and {e.section for e in ev} == {"s2"}, {e.section for e in ev}
    c.on_aim(None)
    s = c._next_bar_start
    ev = c.get_events(s, s)
    assert {e.section for e in ev} == {"s1", "s2"}, {e.section for e in ev}
    print("    solo -> only s2; release -> both phones again")

    print("[2] arranger part map routes parts (round-robin overridden)")
    c = loaded_conductor()
    c.set_part_assignment({"s2": [0, 1, 2]})
    ev = c.get_events(0.0, 0.0)
    assert {e.section for e in ev} == {"s2"}, {e.section for e in ev}
    print("    all three parts seated on s2")

    print("[3] rewind/forward jump the timeline, beat-locked, floored at 0")
    c = loaded_conductor()
    for _ in range(6):
        s = c._next_bar_start
        c.get_events(s, s)
    assert c._next_bar_idx == 6
    c.on_transport("rewind", None)
    assert c._next_bar_idx == 2
    c.on_transport("rewind", None)
    assert c._next_bar_idx == 0
    c.on_transport("forward", None)
    assert c._next_bar_idx == 4
    print("    6 -> 2 -> 0 -> 4")

    print("[3b] on-wand TinyML labels drive the same pipeline as raw windows")
    c = loaded_conductor()
    c.on_classified("sharp_up", 1.0, 400.0)
    assert c._gesture is not None and c._gesture.vertical == 0.9
    assert c._pickup and c._pickup[0][3] == 0.95, "sharp_up should queue the sting"
    c.on_classified("nonsense", 1.0, 500.0)      # unknown label: ignored, no crash
    print("    sharp_up -> features + sting queued; unknown label ignored")

    print("[3c] a swell gesture builds: crescendo + the harmony layer joins")
    c = loaded_conductor()
    c._gesture_in(GestureFeatures(energy=0.6, size=0.5, vertical=0.8, duration=1.2), 400.0)
    assert c._arc == 4, c._arc
    vels, pads = [], False
    for _ in range(4):
        s = c._next_bar_start
        ev = c.get_events(s, s)
        body = [e.vel for e in ev if e.dur > 100 and e.art != "drum"]
        vels.append(sum(body) / max(1, len(body)))
        pads = pads or any(e.inst == "viola" and e.art == "sustain" for e in ev)
    assert vels[-1] > vels[0], f"no crescendo: {vels}"
    assert pads, "harmony pad layer missing during the build"
    assert c._arc == 0
    print(f"    mean velocities {[round(v, 2) for v in vels]} + voice-led pads ✓")

    print("[4] arranger JSON parsing: fences, prose, dupes, leftovers")
    resp = {"content": 'Sure!\n```json\n{"s1": [0, 2], "s2": [1, 1], "sX": [9]}\n```'}
    m = arranger._parse(resp, ["s1", "s2"], 4)
    assert m == {"s1": [0, 2, 3], "s2": [1]}, m           # dupe dropped, 3 round-robined, sX ignored
    assert arranger._parse({"content": "no json here"}, ["s1"], 2) is None
    assert arranger._parse({"content": '{"s1": []}'}, ["s1"], 2) is None
    print(f"    {m}")


async def ws(role: str):
    conn = await connect(f"ws://127.0.0.1:{PORT}/ws")
    await conn.send(json.dumps({"t": "hello", "v": 1, "role": role,
                                "session": "lol1", "client_id": None}))
    welcome = json.loads(await asyncio.wait_for(conn.recv(), 5))
    return conn, welcome


async def recv_until(conn, want: str, timeout: float = 5.0, pred=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = json.loads(await asyncio.wait_for(conn.recv(), max(0.05, deadline - time.monotonic())))
        except asyncio.TimeoutError:
            break
        if msg.get("t") == want and (pred is None or pred(msg)):
            return msg
    return None


async def integration(shows_dir: str) -> None:
    stage, _ = await ws("stage")
    section, w = await ws("section")
    await section.send(json.dumps({"t": "section.ready"}))
    sid = w["config"]["section_id"]
    wand, _ = await ws("wand")

    print("[5] deterministic mode: lifting the wand streams scale-locked expression")
    await wand.send(json.dumps({"t": "wand.mode", "mode": "det"}))
    r = await recv_until(stage, "roster", pred=lambda m: m["wand"].get("mode") == "det")
    assert r, "mode toggle not reflected in roster"
    frames = [[i * 20.0, 0.0, 9.8, 0.0, 0.0, 0.0, 0.0] for i in range(5)]   # full lift
    await wand.send(json.dumps({"t": "wand.imu", "seq": 1, "frames": frames}))
    fx = await recv_until(stage, "fx.expr")
    assert fx and fx["semis"] == 12 and fx["section"] == sid, fx
    print(f"    lift -> +{fx['semis']} semitones on {fx['section']} (gain {fx['gain']})")

    print("[5b] det sub-modes: the button cycles WHAT height controls")
    await wand.send(json.dumps({"t": "wand.mode", "mode": "det", "param": "volume"}))
    await recv_until(stage, "fx.expr")                      # the neutral reset
    await asyncio.sleep(0.15)                               # clear the 100ms expr throttle
    await wand.send(json.dumps({"t": "wand.imu", "seq": 2, "frames": frames}))
    fx = await recv_until(stage, "fx.expr", pred=lambda m: m.get("gain", 0) > 1.0)
    assert fx and fx["semis"] == 0 and abs(fx["gain"] - 1.2) < 0.01, fx
    await wand.send(json.dumps({"t": "wand.mode", "mode": "det", "param": "filter"}))
    await asyncio.sleep(0.15)
    lowered = [[i * 20.0, 0.0, -9.8, 0.0, 0.0, 0.0, 0.0] for i in range(5)]
    await wand.send(json.dumps({"t": "wand.imu", "seq": 3, "frames": lowered}))
    ten = await recv_until(stage, "fx.tension", pred=lambda m: m["value"] == 1.0)
    assert ten is not None, "filter param did not drive fx.tension"
    print(f"    volume: gain {fx['gain']} semis 0; filter: lowered wand -> tension 1.0 (washed)")

    print("[6] wand/palm may drive transport verbs, nothing else")
    await wand.send(json.dumps({"t": "admin.cmd", "cmd": "rewind"}))
    err = await recv_until(wand, "err", timeout=1.0)
    assert err is None, f"transport verb rejected: {err}"
    await wand.send(json.dumps({"t": "admin.cmd", "cmd": "tempo", "args": {"bpm": 200}}))
    err = await recv_until(wand, "err", timeout=2.0)
    assert err and err["code"] == "forbidden", "non-transport verb was NOT rejected"
    print("    rewind allowed, tempo forbidden")

    print("[7] the mode toggle lands in the show ledger")
    await asyncio.sleep(0.5)
    logs = sorted(pathlib.Path(shows_dir).glob("*.jsonl"))
    assert logs, "no ledger written"
    kinds = {json.loads(line)["kind"] for line in logs[-1].read_text().splitlines()}
    assert "wand.mode" in kinds, kinds
    print(f"    ledger kinds: {sorted(kinds)}")

    for c in (stage, section, wand):
        await c.close()


def wait_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main() -> int:
    units()
    shows_dir = tempfile.mkdtemp(prefix="wm-rework-")
    env = dict(os.environ)
    env.update({"WM_HTTP_PORT": str(PORT), "WM_DECISION_LOG": "0",
                "WM_SHOWS_DIR": shows_dir,
                "WM_SESSION_FILE": str(pathlib.Path(shows_dir) / "session.json")})
    server = subprocess.Popen([sys.executable, str(REPO / "server" / "main.py")], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_port(PORT), "server did not come up"
        asyncio.run(integration(shows_dir))
    finally:
        server.terminate()
    print("\nALL REWORK CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nREWORK TEST FAILED: {e}")
        sys.exit(1)
