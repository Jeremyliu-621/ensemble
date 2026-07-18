"""Connection registry + broadcast. Tracks who is connected, by client_id and
role, and fans messages out without letting one slow/dead client stall others.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from websockets.asyncio.server import ServerConnection

log = logging.getLogger("hub")

_SEND_TIMEOUT_S = 1.0  # a client that can't accept a frame in this long is dropped


@dataclass
class ClientConn:
    client_id: str
    role: str
    ws: ServerConnection
    name: str = ""
    section_id: str | None = None
    # Latest self-reported clock estimate, for the admin health readout.
    theta: float | None = None
    rtt: float | None = None
    extra: dict = field(default_factory=dict)


async def send_json(ws: ServerConnection, msg: dict) -> None:
    await ws.send(json.dumps(msg))


class Hub:
    def __init__(self) -> None:
        self._by_id: dict[str, ClientConn] = {}

    def register(self, conn: ClientConn) -> None:
        old = self._by_id.get(conn.client_id)
        self._by_id[conn.client_id] = conn
        if old is not None and old.ws is not conn.ws:
            # A reconnect superseded an open socket (wifi blip): close the old
            # one now so its handler exits instead of lingering to ping-timeout.
            asyncio.ensure_future(self._close_quietly(old.ws))
        log.info("register %s role=%s (total=%d)", conn.client_id[:8], conn.role, len(self._by_id))

    @staticmethod
    async def _close_quietly(ws: ServerConnection) -> None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    def unregister(self, client_id: str, conn: ClientConn | None = None) -> None:
        """Remove a registration. Pass `conn` to make it identity-safe: if a
        reconnect already replaced this client's entry with a NEWER connection,
        the old socket's late disconnect must NOT evict the new one."""
        current = self._by_id.get(client_id)
        if current is None or (conn is not None and current is not conn):
            return
        self._by_id.pop(client_id, None)
        log.info("unregister %s role=%s (total=%d)", client_id[:8], current.role, len(self._by_id))

    def get(self, client_id: str) -> ClientConn | None:
        return self._by_id.get(client_id)

    def by_role(self, *roles: str) -> list[ClientConn]:
        return [c for c in self._by_id.values() if c.role in roles]

    def all(self) -> list[ClientConn]:
        return list(self._by_id.values())

    async def send_to(self, client_id: str, msg: dict) -> None:
        conn = self._by_id.get(client_id)
        if conn is not None:
            await self._guarded_send(conn, msg)

    async def broadcast(self, msg: dict, roles: tuple[str, ...] | None = None) -> None:
        """Send `msg` to every connection (optionally filtered by role),
        concurrently, dropping any client that times out."""
        targets = self._by_id.values() if roles is None else [c for c in self._by_id.values() if c.role in roles]
        payload = json.dumps(msg)
        await asyncio.gather(
            *(self._guarded_send_raw(c, payload) for c in list(targets)),
            return_exceptions=True,
        )

    async def _guarded_send(self, conn: ClientConn, msg: dict) -> None:
        await self._guarded_send_raw(conn, json.dumps(msg))

    async def _guarded_send_raw(self, conn: ClientConn, payload: str) -> None:
        try:
            await asyncio.wait_for(conn.ws.send(payload), timeout=_SEND_TIMEOUT_S)
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - drop-on-any-failure is intentional
            log.warning("send failed to %s (%s); closing", conn.client_id[:8], type(e).__name__)
            try:
                await conn.ws.close()
            except Exception:
                pass
