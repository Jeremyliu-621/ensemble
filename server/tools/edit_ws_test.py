"""End-to-end WebSocket test of the editor edit path against a running server.

Connects as an editor (role stage), starts transport, pushes a song.edit, and
verifies the server rebuilds the song (roster echoes the edited tracks) and emits
a well-formed transport for the playhead — with no bad_edit error.

Start the server first, then:  venv/Scripts/python.exe server/tools/edit_ws_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets

URL = f"ws://localhost:{os.environ.get('WM_HTTP_PORT', '8080')}/ws"
EDIT = {
    "t": "song.edit",
    "song": {"name": "ws-edit", "bpm": 110, "parts": [
        {"instrument": "violin", "is_drum": False, "is_melody": True,
         "notes": [[0, 0, 4, 72, 0.9], [0, 8, 4, 76, 0.8]]},
        {"instrument": "cello", "is_drum": False, "is_melody": False,
         "notes": [[0, 0, 8, 48, 0.7]]},
    ]},
}


async def run() -> int:
    async with websockets.connect(URL) as ws:
        await ws.send(json.dumps({"t": "hello", "v": 1, "role": "stage", "session": "lol1", "client_id": None}))
        welcome = json.loads(await ws.recv())
        assert welcome.get("t") == "welcome", f"no welcome: {welcome}"

        await ws.send(json.dumps({"t": "admin.cmd", "cmd": "start"}))
        await ws.send(json.dumps(EDIT))

        saw_edit_tracks = False
        saw_transport = False
        err = None
        # read messages for a short window
        for _ in range(40):
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            except asyncio.TimeoutError:
                break
            t = msg.get("t")
            if t == "err":
                err = msg
            if t == "roster":
                eng = msg.get("engine") or {}
                names = [tr.get("instrument") for tr in (eng.get("tracks") or [])]
                if eng.get("song") == "ws-edit" and "violin" in names and "cello" in names:
                    saw_edit_tracks = True
                tr = eng.get("transport") or {}
                if tr.get("playing") and tr.get("s16_ms") and tr.get("n_bars"):
                    saw_transport = True
            if t == "engine.state" and (msg.get("transport") or {}).get("playing"):
                saw_transport = True
            if saw_edit_tracks and saw_transport:
                break

        # leave the engine stopped so the test is idempotent
        await ws.send(json.dumps({"t": "admin.cmd", "cmd": "stop"}))
        await asyncio.sleep(0.2)

    if err:
        print(f"FAIL: server returned error {err}")
        return 1
    if not saw_edit_tracks:
        print("FAIL: roster never reflected the edited song (violin+cello, name 'ws-edit')")
        return 1
    if not saw_transport:
        print("FAIL: no well-formed transport seen for the playhead")
        return 1
    print("PASS — song.edit applied over ws; roster reflects edit; transport OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except (ConnectionRefusedError, OSError) as e:
        print(f"FAIL: could not connect to {URL} — is the server running? ({e})")
        sys.exit(2)
