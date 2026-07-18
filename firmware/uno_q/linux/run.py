"""run.py — board-side entrypoint (Arduino App Lab launches this on Linux).

Wires the shared state, AI-mode scaffold, phone-select helper, and the WiFi
link, then runs forever.
"""
from __future__ import annotations

import asyncio
import logging

import config
from ai_mode import AiMode
from phone_select import PhoneSelect
from state import WandState
from wand_link import WandLink


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    state = WandState()
    ai = AiMode(config.MODEL_PATH)
    link = WandLink(state, ai_mode=ai)
    link.phone_select = PhoneSelect(link.send)   # helper sends via the link's ws
    await link.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
