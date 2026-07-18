"""Shared wand state — the single source of truth the link, MCU reflector,
phone-select helper, and AI-mode scaffold all read.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WandState:
    client_id: str | None = None    # server-assigned; echoed on reconnect
    playing: bool = False           # transport (pause/play) from the laptop
    mode: str = "ai"                # "ai" (gestures compose) | "det" (continuous)
    aim: str | None = None          # selected phone's section id, or None

    def update_from_cmd(self, msg: dict) -> None:
        """Apply a server->wand `wand.cmd` payload."""
        if "playing" in msg:
            self.playing = bool(msg["playing"])
        if "mode" in msg:
            self.mode = "det" if msg["mode"] == "det" else "ai"
        if "aim" in msg:
            self.aim = msg["aim"]

    def to_mcu_csv(self) -> str:
        """Serialize for the MCU Bridge topic "cmd": "playing,mode,aim"."""
        return f"{1 if self.playing else 0},{self.mode},{self.aim or ''}"
