"""Phone selection helper.

The *decision* of which phone is selected is made server-side: the server
integrates the wand's yaw (gz) into a heading and locks onto the section whose
stage azimuth is within 40 deg (server/wandio.py WandAimer). The board does not
choose — it TRACKS the current selection (from the wand.cmd downlink) and can ask
the server to re-zero the heading ("this way is forward") via wand.recal.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("wand.select")


class PhoneSelect:
    def __init__(self, send):
        # send: async callable that puts a dict on the wand WebSocket.
        self._send = send
        self.aim: str | None = None

    def on_state(self, st) -> None:
        """Called on every wand.cmd; keep the selected section id current."""
        if st.aim != self.aim:
            self.aim = st.aim
            log.info("phone selection -> %s", self.aim or "(none)")

    async def recal(self) -> None:
        """Zero the aiming yaw drift — wire this to a physical button if one is
        added. A server restart also resets it. (server/main.py handles wand.recal.)"""
        await self._send({"t": "wand.recal", "tw": int(time.monotonic() * 1000)})
        log.info("sent wand.recal (re-zeroed heading)")
