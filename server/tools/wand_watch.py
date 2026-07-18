"""Stand-in wand monitor — watch a real hardware wand stream, no extra deps.

Connects to a running server as a 'stage' observer and prints:
  - when the wand connects / disconnects (from the roster), and
  - live yaw_deg as you rotate the wand (from wand.state).

Every 'wand:' line means the board's wand.imu frames actually reached the
server, so this is enough to prove the physical path
    Movement -> MCU -> Bridge -> onboard Linux -> WiFi -> server.
Reading it:
  * no "WAND CONNECTED"      -> deploy / WiFi / server-URL / handshake
  * connected but no 'wand:' -> Modulino init or MCU<->Linux Bridge (no frames)
  * 'wand:' yaw never moves  -> gyro axis mapping
It does NOT score fps/gravity like the (unpushed) wand_monitor.py.

  python server/tools/wand_watch.py
  python server/tools/wand_watch.py --url ws://192.168.137.1:8080/ws
"""
from __future__ import annotations

import argparse
import asyncio
import json

from websockets.asyncio.client import connect


async def watch(url: str, session: str) -> None:
    async with connect(url) as ws:
        await ws.send(json.dumps({"t": "hello", "v": 1, "role": "stage",
                                  "session": session, "client_id": None}))
        await ws.recv()   # welcome
        print(f"watching {url} as stage — rotate/tilt the wand; Ctrl-C to stop\n")
        wand_up: bool | None = None
        n = 0
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            t = msg.get("t")
            if t == "roster":
                w = msg.get("wand") or {}
                up = bool(w.get("connected"))
                if up != wand_up:
                    wand_up = up
                    print(f">>> WAND {'CONNECTED (' + str(w.get('variant')) + ')' if up else 'disconnected'}")
            elif t == "wand.state":
                n += 1
                yaw = msg.get("yaw_deg")
                yaw_s = f"{yaw:+7.1f}" if isinstance(yaw, (int, float)) else "   ?  "
                print(f"wand: yaw={yaw_s}\N{DEGREE SIGN}   aim={msg.get('aim_section')}   "
                      f"grabbed={msg.get('grabbed')}   (frame batch #{n})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    ap.add_argument("--session", default="lol1")
    a = ap.parse_args()
    try:
        asyncio.run(watch(a.url, a.session))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
