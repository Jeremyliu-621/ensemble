"""Session state: the orchestra roster, wand slot, and transport flag.

Kept deliberately small for P1 (sections + playing flag). Instrument
assignment, placement azimuths, and JSON persistence arrive with P2.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from engine_api import SectionInfo

# The conductor stands just below the map's front edge, looking "up" into the
# stage. Placing this slightly off the bottom edge keeps every phone in front,
# so azimuths stay within a sane ~±75° fan instead of wrapping past ±90°.
_CONDUCTOR_Y = -0.25


def azimuth_from_xy(px: float, py: float) -> float:
    """Angle (deg) from the conductor's forward axis to a phone at (px, py).
    0 = straight ahead, negative = to the conductor's left, positive = right."""
    return math.degrees(math.atan2(px, py - _CONDUCTOR_Y))

# Distinct instruments handed out as phones join, so no two sound identical.
INSTRUMENT_ROTATION = ["violin", "cello", "flute", "clarinet", "viola", "piano", "bass", "bell"]


@dataclass
class Section:
    section_id: str
    client_id: str
    instrument: str = "synth"
    azimuth_deg: float = 0.0      # angle from the conductor's forward axis (- left, + right)
    px: float = 0.0              # top-down map position, normalized: -1 (far left) .. +1 (far right)
    py: float = 0.5             #   0 = next to the conductor .. 1 = back of the stage
    placed: bool = False         # True once the user has dragged it onto the seating map
    connected: bool = True
    ready: bool = False
    volume: float = 1.0
    muted: bool = False


@dataclass
class WandSlot:
    connected: bool = False
    variant: str = "none"     # "sim" | "hw" | "none"
    aim_mode: str = "cycle"   # "cycle" (tap to select) | "yaw" (pointing)


@dataclass
class SessionState:
    name: str
    playing: bool = False
    sections: dict[str, Section] = field(default_factory=dict)  # keyed by section_id
    wand: WandSlot = field(default_factory=WandSlot)
    _next_section_num: int = 1

    def new_section_id(self) -> str:
        sid = f"s{self._next_section_num}"
        self._next_section_num += 1
        return sid

    def next_instrument(self) -> str:
        """First instrument in the rotation not already in use (else wrap)."""
        used = {s.instrument for s in self.sections.values()}
        for inst in INSTRUMENT_ROTATION:
            if inst not in used:
                return inst
        return INSTRUMENT_ROTATION[len(self.sections) % len(INSTRUMENT_ROTATION)]

    def place_section(self, section_id: str, px: float, py: float) -> Section | None:
        """Pin a section to a spot on the top-down seating map (manual placement),
        recomputing the azimuth the wand will point along."""
        sec = self.sections.get(section_id)
        if not sec:
            return None
        sec.px = max(-1.0, min(1.0, float(px)))
        sec.py = max(0.0, min(1.0, float(py)))
        sec.azimuth_deg = round(azimuth_from_xy(sec.px, sec.py), 1)
        sec.placed = True
        return sec

    def nearest_to_yaw(self, yaw_deg: float, max_gap_deg: float = 35.0) -> str | None:
        """Which placed section is the wand pointing at? Nearest azimuth within
        max_gap_deg (else None = pointing at empty space). This is the bridge from
        manual placement to real yaw pointing when the wand arrives."""
        placed = [s for s in self.sections.values() if s.connected and s.placed]
        if not placed:
            return None
        best = min(placed, key=lambda s: abs(s.azimuth_deg - yaw_deg))
        return best.section_id if abs(best.azimuth_deg - yaw_deg) <= max_gap_deg else None

    def engine_sections(self) -> list[SectionInfo]:
        return [
            SectionInfo(s.section_id, s.instrument, s.azimuth_deg, s.ready, s.volume, s.muted)
            for s in self.sections.values()
            if s.connected
        ]

    def roster_payload(self) -> dict:
        return {
            "playing": self.playing,
            "sections": [
                {
                    "id": s.section_id,
                    "instrument": s.instrument,
                    "azimuth_deg": s.azimuth_deg,
                    "px": s.px,
                    "py": s.py,
                    "placed": s.placed,
                    "connected": s.connected,
                    "ready": s.ready,
                    "volume": s.volume,
                    "muted": s.muted,
                }
                for s in self.sections.values()
            ],
            "wand": {
                "connected": self.wand.connected,
                "variant": self.wand.variant,
                "aim_mode": self.wand.aim_mode,
            },
        }
