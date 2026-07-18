"""main.py — board-side entrypoint (Arduino App Lab launches python/main.py).

Wires shared state, the AI-mode scaffold, the phone-select helper, and the WiFi
link, then hands control to the Arduino App runtime.

Launch model mirrors the TESTED stream_probe app: register the MCU Bridge
providers on the main thread, run the WebSocket link in a daemon thread, and
call App.run() on the main thread so the Arduino Bridge runtime services the
imu/range callbacks coming off the MCU.
"""
from __future__ import annotations

import asyncio
import logging
import threading

import config
from ai_mode import AiMode
from phone_select import PhoneSelect
from state import WandState
from wand_link import WandLink

try:
    from arduino.app_utils import App   # type: ignore
except Exception:  # noqa: BLE001 - keep importable off-board for laptop tests
    App = None

log = logging.getLogger("wand.main")


def main() -> None:
    if App is None:
        raise RuntimeError("arduino.app_utils is required on the UNO Q")

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    state = WandState()
    ai = AiMode(config.MODEL_PATH)
    link = WandLink(state, ai_mode=ai)
    link.phone_select = PhoneSelect(link.send)   # helper sends via the link's ws

    link.register_bridge()   # provide imu/range on the main thread, before App.run()
    threading.Thread(target=lambda: asyncio.run(link.run()),
                     name="phoneharmonic-wand-ws", daemon=True).start()
    log.info("Bridge providers ready; starting Arduino App runtime")
    App.run()


if __name__ == "__main__":
    main()
