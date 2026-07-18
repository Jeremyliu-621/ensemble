"""Headless test of the decision-policy layer (localhost only, no browser).

Covers:
  [1] schema parse: valid / lenient-extras / invalid replies
  [2] a fake OpenAI-compatible endpoint drives the bar choice (source=model)
  [3] the model's octave_shift is audible in the scheduled notes
  [4] dead endpoint    -> heuristic covers, music never stalls
  [5] off-format reply -> heuristic covers
  [6] a new gesture clears the previous model answer
  [7] build_dataset synthetic rows are schema-valid Freesolo rows

Run:  python server/tools/policy_test.py     (from repo root)
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
from engine.conductor import Conductor
from gesture_test import imu_window
from ml.schema import parse_decision

REPLY: dict = {"content": ""}                # what the fake endpoint answers next


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"choices": [{"message": {"content": REPLY["content"]}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):             # keep test output clean
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def use_model(url: str) -> None:
    config.MODEL_URL = url
    config.MODEL_NAME = "test-run"
    config.MODEL_KEY = "test-key"
    config.MODEL_TIMEOUT_MS = 1000.0


async def conducted_bar(c: Conductor, window) -> tuple[str, str, list]:
    """Gesture -> let the async ask land -> pull the next bar."""
    c.on_gesture(window)
    await asyncio.sleep(0.4)
    s = c._next_bar_start
    events = c.get_events(s, s)
    return c._last_choice, c._last_source, events


def octaves(events, art: str) -> list[int]:
    return [int(e.note[-1]) for e in events if e.art == art]


async def run() -> int:
    print("[1] parse_decision accepts valid + extra keys, rejects bad values")
    assert parse_decision('{"candidate":"rest","octave_shift":-1}').candidate == "rest"
    assert parse_decision('{"candidate":"rest","octave_shift":0,"why":"calm"}') is not None
    assert parse_decision('{"candidate":"banana","octave_shift":0}') is None
    assert parse_decision('{"candidate":"rest","octave_shift":2}') is None
    assert parse_decision("not json") is None
    print("    ok")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    live = f"http://127.0.0.1:{server.server_address[1]}/v1"

    print("[2] model endpoint drives the choice (heuristic would say rhythmic_dense)")
    use_model(live)
    REPLY["content"] = '{"candidate":"contrary_motion","octave_shift":0}'
    c = Conductor()
    c.on_transport("start", 0.0)
    choice, source, _ = await conducted_bar(c, imu_window(accel_mag=12.0))
    assert (choice, source) == ("contrary_motion", "model"), f"got {choice}/{source}"
    print(f"    choice={choice} source={source}")

    print("[3] model octave_shift is audible in the pad notes")
    REPLY["content"] = '{"candidate":"sustained","octave_shift":0}'
    c = Conductor()
    c.on_transport("start", 0.0)
    _, _, ev0 = await conducted_bar(c, imu_window(accel_mag=2.0))
    REPLY["content"] = '{"candidate":"sustained","octave_shift":1}'
    c = Conductor()
    c.on_transport("start", 0.0)
    _, _, ev1 = await conducted_bar(c, imu_window(accel_mag=2.0))
    o0, o1 = octaves(ev0, "sustain"), octaves(ev1, "sustain")
    assert o0 and o1 and [o + 1 for o in o0] == o1, f"shift not applied: {o0} vs {o1}"
    print(f"    pad octaves {o0} -> {o1}")

    print("[4] dead endpoint -> heuristic covers")
    use_model(f"http://127.0.0.1:{free_port()}/v1")
    config.MODEL_TIMEOUT_MS = 300.0
    c = Conductor()
    c.on_transport("start", 0.0)
    choice, source, ev = await conducted_bar(c, imu_window(accel_mag=12.0))
    assert (choice, source) == ("rhythmic_dense", "heuristic"), f"got {choice}/{source}"
    assert ev, "fallback produced no notes"
    print(f"    choice={choice} source={source} ({len(ev)} events)")

    print("[5] off-format reply -> heuristic covers")
    use_model(live)
    REPLY["content"] = "a lovely bar of music, maestro"
    c = Conductor()
    c.on_transport("start", 0.0)
    choice, source, _ = await conducted_bar(c, imu_window(accel_mag=12.0))
    assert (choice, source) == ("rhythmic_dense", "heuristic"), f"got {choice}/{source}"
    print(f"    choice={choice} source={source}")

    print("[6] a new gesture clears the previous model answer")
    REPLY["content"] = '{"candidate":"contrary_motion","octave_shift":0}'
    c = Conductor()
    c.on_transport("start", 0.0)
    choice, source, _ = await conducted_bar(c, imu_window(accel_mag=12.0))
    assert source == "model"
    REPLY["content"] = "no longer valid"       # next ask fails -> nothing to reuse
    choice, source, _ = await conducted_bar(c, imu_window(accel_mag=12.0))
    assert source == "heuristic", f"stale model decision survived: {choice}/{source}"
    print("    stale answer dropped, heuristic covers")

    print("[7] build_dataset synthetic rows are valid Freesolo rows")
    from build_dataset import synth_rows, to_freesolo
    rows = to_freesolo(synth_rows(200, random.Random(1)))
    assert len(rows) == 200
    for r in rows:
        assert set(r) == {"input", "output"} and "Context: " in r["input"]
        assert parse_decision(r["output"]) is not None
    print(f"    {len(rows)} rows validated")

    server.shutdown()
    print("\nALL POLICY CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(asyncio.run(run()))
    except AssertionError as e:
        print(f"\nPOLICY TEST FAILED: {e}")
        sys.exit(1)
