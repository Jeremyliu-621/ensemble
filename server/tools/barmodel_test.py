"""Headless test of the bar-line (music editing) model path (localhost only).

Covers:
  [1] sanitize_line clamps the grid, snaps to key, folds register, rejects junk
  [2] style_for maps gestures onto the style vocabulary
  [3] a fake endpoint's line becomes the "generated" candidate and plays when forced
  [4] dead endpoint -> engine unaffected, "generated" simply absent
  [5] the mock server drives BOTH models end-to-end (decision + bar-line)
  [6] build_bar_dataset synthetic rows are playable Freesolo rows

Run:  python server/tools/barmodel_test.py     (from repo root)
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import random
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["WM_DECISION_LOG"] = "0"          # tests must not pollute the harvest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # server/ on path

import config
from engine.candidates import REG_HI, REG_LO
from engine.conductor import Conductor
from gesture_test import imu_window
from gestures.features import GestureFeatures
from ml.barmodel import sanitize_line, style_for

REPLY: dict = {"content": ""}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"choices": [{"message": {"content": REPLY["content"]}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def use_barmodel(url: str) -> None:
    config.MODEL_URL = config.MODEL_NAME = ""          # decision model off unless a test wants it
    config.BARMODEL_URL = url
    config.BARMODEL_NAME = "test-run"
    config.BARMODEL_KEY = "test-key"
    config.BARMODEL_TIMEOUT_MS = 1000.0


async def pull_bar(c: Conductor):
    s = c._next_bar_start
    events = c.get_events(s, s)
    return c._last_choice, events


async def run() -> int:
    print("[1] sanitize_line: clamp grid, snap key, fold register, reject junk")
    line = sanitize_line({"notes": [[0, 40, 100, 2.0], [14.9, 1, 30, 0.01], ["x"], [1, 2, 3]]}, 0)
    assert line and len(line) == 2, f"got {line}"
    for (on, dur, midi, vel) in line:
        assert 0 <= on <= 15 and 1 <= dur <= 16 - on
        assert REG_LO <= midi <= REG_HI and midi % 12 in {0, 2, 4, 5, 7, 9, 11}
        assert 0.1 <= vel <= 1.0
    assert sanitize_line({"notes": "nope"}, 0) is None
    assert sanitize_line({"notes": []}, 0) is None
    print(f"    ok ({line})")

    print("[2] style_for maps gestures onto the style vocabulary")
    assert style_for(None) == "calm"
    assert style_for(GestureFeatures(rotation=0.8)) == "counter"
    assert style_for(GestureFeatures(energy=0.9, size=0.8, duration=1.0)) == "dense"
    assert style_for(GestureFeatures(energy=0.5, size=0.3, duration=0.4)) == "echo"
    assert style_for(GestureFeatures(energy=0.4, size=0.4, duration=1.2)) == "free"
    print("    ok")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    live = f"http://127.0.0.1:{server.server_address[1]}/v1"

    print("[3] fake endpoint's line becomes the forced 'generated' candidate")
    use_barmodel(live)
    REPLY["content"] = '{"notes": [[0, 8, 100, 2.0], [8, 8, 41, 0.5]]}'
    c = Conductor()
    c.on_transport("start", 0.0)
    await pull_bar(c)                       # bar 0 plays; prefetch for bar 2 fires
    await asyncio.sleep(0.4)
    await pull_bar(c)                       # bar 1
    c.set_forced("generated")
    choice, ev = await pull_bar(c)          # bar 2 — the prefetched line lands here
    assert choice == "generated", f"got {choice}"
    line_ev = [e for e in ev if e.vel in (1.0, 0.5)]     # the sanitized line (melody is 0.9)
    assert len(line_ev) == 2, f"line events: {[(e.note, e.vel) for e in ev]}"
    print(f"    plays {[(e.note, e.vel) for e in line_ev]}")

    print("[4] dead endpoint -> 'generated' absent, engine unaffected")
    use_barmodel(f"http://127.0.0.1:{free_port()}/v1")
    config.BARMODEL_TIMEOUT_MS = 300.0
    c = Conductor()
    c.on_transport("start", 0.0)
    c.set_forced("generated")
    await pull_bar(c)
    await asyncio.sleep(0.5)
    choice, ev = await pull_bar(c)
    assert choice != "generated" and ev, f"got {choice}, {len(ev)} events"
    print(f"    fell through to {choice}")

    print("[5] mock server drives BOTH models end-to-end")
    from mock_model import serve_in_thread
    mock = serve_in_thread()
    base = f"http://127.0.0.1:{mock.server_address[1]}/v1"
    use_barmodel(base)
    config.MODEL_URL, config.MODEL_NAME, config.MODEL_KEY = base, "mock", "k"
    config.MODEL_TIMEOUT_MS = 1000.0
    c = Conductor()
    c.on_transport("start", 0.0)
    c.on_gesture(imu_window(accel_mag=12.0))             # asks the mock decision model
    await asyncio.sleep(0.5)
    choice, _ = await pull_bar(c)                        # also prefetches bar 2's line
    assert c._last_source == "model", f"source {c._last_source}"
    await asyncio.sleep(0.5)
    await pull_bar(c)                                    # bar 1
    c.set_forced("generated")
    choice, ev = await pull_bar(c)                       # bar 2
    assert choice == "generated" and ev, "mock bar line did not arrive"
    print(f"    decision source=model, bar 1 forced -> {choice} ({len(ev)} events)")
    mock.shutdown()

    print("[6] build_bar_dataset synthetic rows are playable Freesolo rows")
    import build_bar_dataset as bbd
    rows = bbd.to_freesolo(bbd.synth_rows(60, random.Random(2)))
    assert len(rows) == 60
    for r in rows:
        assert set(r) == {"input", "output"} and "Context: " in r["input"]
        assert sanitize_line(json.loads(r["output"]), 0) is not None
    print(f"    {len(rows)} rows validated")

    server.shutdown()
    print("\nALL BAR-MODEL CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(asyncio.run(run()))
    except AssertionError as e:
        print(f"\nBAR-MODEL TEST FAILED: {e}")
        sys.exit(1)
