"""Integration test of the non-model backend: the Backboard announcer, the
hash-chained show ledger, and the hardware wand surface (touch pads, ToF
tension, aiming). Spawns its own server process + a mock Backboard endpoint.
No browser, no internet, no AI models.

Covers:
  [1] show start -> commentator line (via mock Backboard) reaches the stage
  [2] MPR121 pad down/up forces/releases a candidate (roster reflects it)
  [3] ToF distance -> fx.tension broadcast with the right value
  [4] IMU yaw -> wand.state diagnostics; malformed rows are filtered; a new
      hardware wand resets the stream counters
  [5] show stop -> manifest written; the event hash chain verifies

Run:  python server/tools/show_test.py     (from repo root)
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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # server/ on path

from websockets.asyncio.client import connect

import showlog

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
PORT = int(os.environ.get("WM_TEST_PORT", "8098"))
ANNOUNCE_TEXT = "Ladies and gentlemen, the phones are alive!"


class _Backboard(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"assistant_id": "a1", "thread_id": "t1",
                           "content": ANNOUNCE_TEXT}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def ws(role: str):
    conn = await connect(f"ws://127.0.0.1:{PORT}/ws")
    await conn.send(json.dumps({"t": "hello", "v": 1, "role": role,
                                "session": "lol1", "client_id": None}))
    welcome = json.loads(await asyncio.wait_for(conn.recv(), 5))
    assert welcome["t"] == "welcome", welcome
    return conn, welcome


async def recv_until(conn, want: str, timeout: float = 6.0, pred=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = max(0.05, deadline - time.monotonic())
        try:
            msg = json.loads(await asyncio.wait_for(conn.recv(), left))
        except asyncio.TimeoutError:
            break
        if msg.get("t") == want and (pred is None or pred(msg)):
            return msg
    return None


def wait_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


async def run(shows_dir: str) -> int:
    stage, _ = await ws("stage")
    admin, _ = await ws("admin")
    section, w = await ws("section")
    await section.send(json.dumps({"t": "section.ready"}))
    sid = w["config"]["section_id"]

    print("[1] show start -> commentator line reaches the stage")
    await admin.send(json.dumps({"t": "admin.cmd", "cmd": "start"}))
    ann = await recv_until(stage, "announce")
    assert ann and ann["text"] == ANNOUNCE_TEXT, f"got {ann}"
    print(f"    announce: {ann['text']!r}")

    print("[2] MPR121 pad forces / releases a candidate")
    wand, _ = await ws("wand")
    await wand.send(json.dumps({"t": "wand.touch", "pad": 4, "state": "down"}))
    r = await recv_until(stage, "roster", pred=lambda m: m["engine"]["forced"] == "rhythmic_dense")
    assert r, "pad 4 did not force rhythmic_dense"
    await wand.send(json.dumps({"t": "wand.touch", "pad": 4, "state": "up"}))
    r = await recv_until(stage, "roster", pred=lambda m: m["engine"]["forced"] == "auto")
    assert r, "pad release did not return to auto"
    print("    pad 4 -> rhythmic_dense -> auto ✓")

    print("[3] ToF distance -> fx.tension")
    await wand.send(json.dumps({"t": "wand.range", "mm": 150}))
    fx = await recv_until(stage, "fx.tension")
    assert fx and abs(fx["value"] - 0.9) < 0.01, f"got {fx}"
    print(f"    150mm -> tension {fx['value']}")

    print("[4] IMU yaw -> wand.state with an aimed section")
    frames = [[i * 20.0, 0, 0, 0, 0, 0, 0] for i in range(5)]
    await wand.send(json.dumps({"t": "wand.imu", "seq": 1, "frames": frames}))
    st = await recv_until(stage, "wand.state", pred=lambda m: m.get("aim_section") == sid)
    assert st, "no wand.state with the aimed section"
    imu = st.get("imu", {})
    assert imu.get("seq") == 1, f"wand.state missing IMU sequence telemetry: {imu}"
    assert imu.get("batches") == 1 and imu.get("frames") == 5, \
        f"wand.state has wrong IMU counters: {imu}"
    assert imu.get("invalid_frames") == 0 and imu.get("seq_gaps") == 0, \
        f"wand.state reports an unhealthy IMU stream: {imu}"
    print(f"    aiming at {st['aim_section']} (yaw {st['yaw_deg']}°)")
    print(f"    stream telemetry: {imu['batches']} batch / {imu['frames']} frames, no errors")

    # A malformed row with an extreme would-be gyro value must be counted but
    # never reach WandAimer.
    await asyncio.sleep(0.2)
    await wand.send(json.dumps({
        "t": "wand.imu", "seq": 2,
        "frames": [[1000, 0, 0, 9.81, 0, 100000]],  # six fields, not seven
    }))
    bad = await recv_until(
        stage, "wand.state",
        pred=lambda m: (m.get("imu") or {}).get("invalid_frames") == 1,
    )
    assert bad, "malformed IMU diagnostics were not broadcast"
    assert bad["yaw_deg"] == st["yaw_deg"], "malformed IMU row reached WandAimer"
    assert bad["imu"]["frames"] == 5 and bad["imu"]["batches"] == 2, bad["imu"]

    # The newest hardware wand owns the slot. Its diagnostics start clean, and
    # its first arbitrary sequence value is a baseline rather than a gap.
    replacement, _ = await ws("wand")
    reset_frames = [[200 + i * 20.0, 0, 0, 9.81, 0, 0, 0] for i in range(5)]
    await replacement.send(json.dumps({"t": "wand.imu", "seq": 100,
                                       "frames": reset_frames}))
    reset = await recv_until(
        stage, "wand.state",
        pred=lambda m: (m.get("imu") or {}).get("seq") == 100,
    )
    assert reset, "replacement hardware wand produced no diagnostics"
    reset_imu = reset["imu"]
    assert (reset_imu["batches"], reset_imu["frames"], reset_imu["invalid_frames"],
            reset_imu["seq_gaps"]) == (1, 5, 0, 0), reset_imu
    print("    malformed row filtered; replacement wand counters reset ✓")

    print("[5] show stop -> manifest written, hash chain verifies")
    await admin.send(json.dumps({"t": "admin.cmd", "cmd": "stop"}))
    await asyncio.sleep(1.0)
    logs = sorted(pathlib.Path(shows_dir).glob("*.jsonl"))
    manifests = sorted(pathlib.Path(shows_dir).glob("*.manifest.json"))
    assert logs and manifests, f"ledger files missing in {shows_dir}"
    events = [json.loads(line) for line in logs[-1].read_text().splitlines()]
    assert showlog.verify(events), "hash chain does not verify"
    manifest = json.loads(manifests[-1].read_text())
    assert manifest["head_hash"] == events[-1]["hash"]
    kinds = {e["kind"] for e in events}
    for want in ("show.start", "section.join", "wand.connect", "wand.touch",
                 "wand.tension", "wand.aim", "show.stop"):
        assert want in kinds, f"missing ledger kind {want} (have {sorted(kinds)})"
    print(f"    {len(events)} events, head {manifest['head_hash'][:16]}…, chain verifies ✓")

    for c in (stage, admin, section, wand, replacement):
        await c.close()
    print("\nALL SHOW CHECKS PASSED ✓")
    return 0


def main() -> int:
    mock = ThreadingHTTPServer(("127.0.0.1", 0), _Backboard)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    shows_dir = tempfile.mkdtemp(prefix="wm-shows-")

    env = dict(os.environ)
    env.update({
        "WM_HTTP_PORT": str(PORT),
        "WM_DECISION_LOG": "0",
        "WM_SHOWS_DIR": shows_dir,
        "WM_SESSION_FILE": str(pathlib.Path(shows_dir) / "session.json"),
        "WM_BACKBOARD_URL": f"http://127.0.0.1:{mock.server_address[1]}/api",
        "WM_BACKBOARD_KEY": "test-key",
    })
    server = subprocess.Popen([sys.executable, str(REPO / "server" / "main.py")], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_port(PORT), "server did not come up"
        return asyncio.run(run(shows_dir))
    finally:
        server.terminate()
        mock.shutdown()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nSHOW TEST FAILED: {e}")
        sys.exit(1)
