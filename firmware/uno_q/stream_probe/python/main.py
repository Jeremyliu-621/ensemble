"""UNO Q Linux side of the isolated Phoneharmonic IMU stream probe.

Bridge callbacks receive CSV samples from the MCU. A background asyncio thread
batches them into the production ``wand.imu`` WebSocket contract while the main
thread stays in ``App.run()`` to service Arduino Bridge callbacks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import websockets
from websockets.asyncio.client import connect

try:
    from arduino.app_utils import App, Bridge  # type: ignore
except ImportError:  # Keep pure logic importable for laptop-side tests.
    App = None
    Bridge = None

PROTOCOL_VERSION = 1
BATCH_SIZE = 5
QUEUE_SIZE = 600
RECONNECT_SECONDS = 1.0
HEALTH_SECONDS = 5.0
CONFIG_PATH = Path(__file__).with_name("probe_config.json")

log = logging.getLogger("imu_probe")


def parse_imu_csv(payload: object) -> list[float] | None:
    """Return one finite seven-number IMU row, or ``None`` when malformed."""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str):
        return None
    parts = payload.split(",")
    if len(parts) != 7:
        return None
    try:
        row = [float(part) for part in parts]
    except ValueError:
        return None
    return row if all(math.isfinite(value) for value in row) else None


@dataclass(frozen=True)
class ProbeConfig:
    ws_url: str
    session: str


def load_config(path: Path = CONFIG_PATH) -> ProbeConfig:
    """Load and validate the deployment-generated board configuration."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing generated config: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid generated config {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError("probe_config.json must contain a JSON object")
    ws_url = raw.get("ws_url")
    session = raw.get("session")
    if not isinstance(ws_url, str) or urlparse(ws_url).scheme not in ("ws", "wss"):
        raise RuntimeError("probe_config.json ws_url must use ws:// or wss://")
    if not isinstance(session, str) or not session.strip():
        raise RuntimeError("probe_config.json session must be a non-empty string")
    return ProbeConfig(ws_url=ws_url, session=session)


class SampleBuffer:
    """Bounded producer/consumer queue plus thread-safe health counters."""

    def __init__(self, maxsize: int = QUEUE_SIZE) -> None:
        self._queue: queue.Queue[list[float]] = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self.accepted = 0
        self.rejected = 0
        self.dropped = 0

    def accept_payload(self, payload: object) -> bool:
        row = parse_imu_csv(payload)
        if row is None:
            with self._lock:
                self.rejected += 1
            return False
        self.put(row)
        return True

    def put(self, row: list[float]) -> None:
        while True:
            try:
                self._queue.put_nowait(row)
                with self._lock:
                    self.accepted += 1
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    continue
                with self._lock:
                    self.dropped += 1

    def take_batch(self, size: int = BATCH_SIZE) -> list[list[float]] | None:
        if self._queue.qsize() < size:
            return None
        rows: list[list[float]] = []
        for _ in range(size):
            try:
                rows.append(self._queue.get_nowait())
            except queue.Empty:  # Only the producer competes, so this is defensive.
                for row in rows:
                    self.put(row)
                return None
        return rows

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "accepted": self.accepted,
                "rejected": self.rejected,
                "dropped": self.dropped,
                "queued": self._queue.qsize(),
            }


class StreamClient:
    def __init__(self, config: ProbeConfig, samples: SampleBuffer) -> None:
        self.config = config
        self.samples = samples
        self.client_id: str | None = None
        self.seq = 0
        self.batches_sent = 0

    def next_message(self) -> dict | None:
        frames = self.samples.take_batch()
        if frames is None:
            return None
        self.seq += 1
        return {"t": "wand.imu", "seq": self.seq, "frames": frames}

    async def _handshake(self, ws) -> None:
        await ws.send(json.dumps({
            "t": "hello",
            "v": PROTOCOL_VERSION,
            "role": "wand",
            "session": self.config.session,
            "client_id": self.client_id,
        }))
        try:
            welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        except (asyncio.TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("server did not return a valid welcome") from exc
        if (welcome.get("t") != "welcome"
                or welcome.get("v") != PROTOCOL_VERSION
                or welcome.get("role") != "wand"):
            raise RuntimeError(f"unexpected handshake response: {welcome!r}")
        assigned = welcome.get("client_id")
        if not isinstance(assigned, str) or not assigned:
            raise RuntimeError("welcome did not contain a client_id")
        self.client_id = assigned

    async def _connected(self, ws) -> None:
        last_health = time.monotonic()
        while True:
            message = self.next_message()
            if message is not None:
                await ws.send(json.dumps(message, separators=(",", ":")))
                self.batches_sent += 1
            else:
                await asyncio.sleep(0.005)

            now = time.monotonic()
            if now - last_health >= HEALTH_SECONDS:
                last_health = now
                health = self.samples.snapshot()
                log.info(
                    "health accepted=%d rejected=%d dropped=%d queued=%d batches=%d seq=%d",
                    health["accepted"], health["rejected"], health["dropped"],
                    health["queued"], self.batches_sent, self.seq,
                )

    async def run(self) -> None:
        while True:
            try:
                async with connect(self.config.ws_url) as ws:
                    await self._handshake(ws)
                    log.info("connected to %s as %s", self.config.ws_url, self.client_id)
                    await self._connected(ws)
            except (OSError, websockets.ConnectionClosed, RuntimeError) as exc:
                log.warning("stream disconnected (%s); retrying in %.1fs", exc, RECONNECT_SECONDS)
            except Exception:  # noqa: BLE001 - an on-board daemon must self-heal.
                log.exception("unexpected stream failure; retrying")
            await asyncio.sleep(RECONNECT_SECONDS)


def run_app() -> None:
    if App is None or Bridge is None:
        raise RuntimeError("arduino.app_utils is required on the UNO Q")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    config = load_config()
    samples = SampleBuffer()
    client = StreamClient(config, samples)

    Bridge.provide("imu_sample", samples.accept_payload)
    threading.Thread(
        target=lambda: asyncio.run(client.run()),
        name="phoneharmonic-websocket",
        daemon=True,
    ).start()
    log.info("Bridge provider ready; starting Arduino App runtime")
    App.run()


if __name__ == "__main__":
    run_app()
