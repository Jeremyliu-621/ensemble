"""wand_link.py — the WiFi WebSocket link between the UNO Q and the laptop.

Runs on the board's Linux side. Bidirectional:
  UPLINK   Bridge topic "imu" (CSV from the MCU) -> batch ~5 -> wand.imu JSON.
  DOWNLINK server wand.cmd -> WandState -> Bridge topic "cmd" (CSV to the MCU),
           plus phone-select + ai-mode hooks.

Mirrors the handshake/forward logic of server/tools/wand_bridge.py, but lives on
the board and is two-way. Reconnects forever, echoing the cached client_id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque

import websockets
from websockets.asyncio.client import connect

import config
from state import WandState

log = logging.getLogger("wand.link")

# Bridge (MCU<->Linux). Optional import so this module stays testable off-board.
try:
    from arduino.app_utils import Bridge   # type: ignore
except Exception:  # noqa: BLE001
    Bridge = None
    log.warning("arduino.app_utils.Bridge unavailable — running off-board (no MCU I/O)")


class WandLink:
    def __init__(self, state: WandState, ai_mode=None, phone_select=None):
        self.state = state
        self.ai_mode = ai_mode
        self.phone_select = phone_select
        self._buf: deque[list[float]] = deque()
        self._lock = threading.Lock()
        self._seq = 0
        self._range: float | None = None    # latest ToF mm from the MCU, if new

    # --- MCU uplink ingress (Bridge callback; may fire off-thread) ---
    def _on_imu(self, payload) -> None:
        row = _parse_imu_csv(payload)
        if row is not None:
            with self._lock:
                self._buf.append(row)

    def _drain(self, n: int) -> list[list[float]] | None:
        with self._lock:
            if len(self._buf) < n:
                return None
            return [self._buf.popleft() for _ in range(n)]

    # --- MCU range ingress (ToF) ---
    def _on_range(self, payload) -> None:
        mm = _parse_range(payload)
        if mm is not None:
            with self._lock:
                self._range = mm

    def _take_range(self) -> float | None:
        with self._lock:
            mm, self._range = self._range, None
            return mm

    # --- MCU downlink egress ---
    def _push_to_mcu(self) -> None:
        if Bridge is not None:
            Bridge.notify("cmd", self.state.to_mcu_csv())

    async def send(self, obj: dict) -> None:
        """Used by helpers (e.g. phone_select.recal) to reach the server."""
        if self._ws is not None:
            await self._ws.send(json.dumps(obj))

    def register_bridge(self) -> None:
        """Register the MCU uplink providers. Call once on the main thread before
        App.run() — the Arduino Bridge runtime services these callbacks."""
        if Bridge is not None:
            Bridge.provide("imu", self._on_imu)
            Bridge.provide("range", self._on_range)

    # --- main loop: reconnect forever ---
    async def run(self) -> None:
        self._ws = None
        while True:
            try:
                async with connect(config.WS_URL) as ws:
                    self._ws = ws
                    await self._handshake(ws)
                    log.info("wand link up -> %s (client_id=%s)",
                             config.WS_URL, self.state.client_id)
                    await asyncio.gather(self._uplink(ws), self._downlink(ws))
            except (OSError, websockets.ConnectionClosed) as e:
                log.warning("link down (%s); reconnecting", type(e).__name__)
            except Exception:  # noqa: BLE001
                log.exception("link error; reconnecting")
            finally:
                self._ws = None
            await asyncio.sleep(config.RECONNECT_BACKOFF_S)

    async def _handshake(self, ws) -> None:
        await ws.send(json.dumps({
            "t": "hello", "v": config.PROTOCOL_VERSION, "role": "wand",
            "session": config.SESSION, "client_id": self.state.client_id,
        }))
        welcome = json.loads(await ws.recv())
        self.state.client_id = welcome.get("client_id", self.state.client_id)

    async def _uplink(self, ws) -> None:
        while True:
            sent = False
            frames = self._drain(config.BATCH)
            if frames is not None:
                self._seq += 1
                await ws.send(json.dumps({"t": "wand.imu", "seq": self._seq, "frames": frames}))
                sent = True
            mm = self._take_range()
            if mm is not None:
                await ws.send(json.dumps({"t": "wand.range", "mm": mm}))
                sent = True
            if not sent:
                await asyncio.sleep(0.005)

    async def _downlink(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            t = msg.get("t")
            if t == "wand.cmd":
                self.state.update_from_cmd(msg)
                self._push_to_mcu()
                if self.phone_select is not None:
                    self.phone_select.on_state(self.state)
                if self.ai_mode is not None:
                    self.ai_mode.on_state(self.state)
            elif t == "err":
                log.warning("server err: %s", msg.get("msg"))
            # welcome / clock.pong: ignored


def _parse_imu_csv(payload) -> list[float] | None:
    """Parse the MCU's "tw,ax,ay,az,gx,gy,gz" into 7 floats."""
    try:
        parts = payload.split(",") if isinstance(payload, str) else list(payload)
        if len(parts) != 7:
            return None
        return [float(x) for x in parts]
    except (ValueError, TypeError, AttributeError):
        return None


def _parse_range(payload) -> float | None:
    """Parse the MCU's distance payload ("mm") into a float, dropping NaN."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    try:
        mm = float(payload)
    except (ValueError, TypeError):
        return None
    return mm if mm == mm else None
