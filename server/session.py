"""Session state: the orchestra roster, wand slot, and transport flag.

Kept deliberately small for P1 (sections + playing flag). Instrument
assignment, placement azimuths, and JSON persistence arrive with P2.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from engine_api import SectionInfo

# The laptop (webcam + conductor) is the HUB in the middle of the room map, so
# phones can sit anywhere around it and azimuths span the full circle:
# 0 = straight ahead (top of the map), +90 = right, ±180 = behind, -90 = left.
_HUB_X, _HUB_Y = 0.0, 0.5


def azimuth_from_xy(px: float, py: float) -> float:
    """Angle (deg) from the hub's forward axis to a phone at (px, py)."""
    return math.degrees(math.atan2(px - _HUB_X, py - _HUB_Y))


def ang_dist(a: float, b: float) -> float:
    """Circular distance between two angles in degrees (0..180)."""
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d

# Distinct instruments handed out as phones join, so no two sound identical.
INSTRUMENT_ROTATION = ["violin", "cello", "flute", "clarinet", "viola", "piano", "bass", "bell"]


@dataclass
class Section:
    section_id: str
    client_id: str
    instrument: str = "synth"
    azimuth_deg: float = 0.0      # angle from the conductor's forward axis (- left, + right)
    px: float = 0.0              # top-down map position, normalized: -1 (far left) .. +1 (far right)
    py: float = 0.9             #   0 = front edge .. 1 = back of the room (hub sits at 0.5)
    placed: bool = False         # True once the user has dragged it onto the room map
    connected: bool = True
    ready: bool = False
    volume: float = 1.0
    muted: bool = False
    dropped_at: float | None = None   # epoch s of disconnect (grace-period pruning)


@dataclass
class WandSlot:
    connected: bool = False
    variant: str = "none"     # "sim" | "hw" | "none"
    aim_mode: str = "cycle"   # "cycle" (tap to select) | "yaw" (pointing)
    mode: str = "ai"          # "ai" (gestures compose) | "det" (continuous control)
    det_param: str = "pitch"  # what det-mode height controls: "pitch" | "volume" | "filter"


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
        """Fallback for when NO song is loaded: first rotation instrument not
        already in use (else wrap). With a song, use deal_instrument instead."""
        used = {s.instrument for s in self.sections.values()}
        for inst in INSTRUMENT_ROTATION:
            if inst not in used:
                return inst
        return INSTRUMENT_ROTATION[len(self.sections) % len(INSTRUMENT_ROTATION)]

    # ── instrument assignment: ONE authority for every path ─────────────────
    # (join, rejoin, MIDI drop, live edit). Policy: a phone must always be an
    # instrument the current song actually contains, transport running or not;
    # extra phones double the least-covered part; existing valid assignments
    # are never shuffled without cause (minimal moves).

    def _live(self) -> list[Section]:
        """Connected sections in join order (dicts preserve insertion order)."""
        return [s for s in self.sections.values() if s.connected]

    def deal_instrument(self, song_instruments: list[str] | None = None) -> str:
        """Instrument for a JOINING phone: the song's least-covered part
        (ties broken by part order), so it always matches a real track.
        No loaded song -> the generic rotation."""
        insts = list(dict.fromkeys(song_instruments or []))
        if not insts:
            return self.next_instrument()
        counts = Counter(s.instrument for s in self._live())
        return min(insts, key=lambda i: counts.get(i, 0))

    def reconcile_instruments(self, song_instruments: list[str]) -> list[Section]:
        """Re-align connected phones after the song changed, with minimal moves:
        phones already on one of the song's instruments keep it; stale phones
        (instrument not in the song) are re-dealt; then surplus doublings are
        spread onto still-uncovered parts, newest phone first. Returns the
        sections that changed so the caller can notify them. No song -> no-op."""
        insts = list(dict.fromkeys(song_instruments))
        if not insts:
            return []
        live = self._live()
        changed: list[Section] = []

        counts = lambda: Counter(s.instrument for s in live)  # noqa: E731

        for s in live:                          # stale phones get a real part
            if s.instrument not in insts:
                c = counts()
                s.instrument = min(insts, key=lambda i: c.get(i, 0))
                changed.append(s)
        while True:                             # spread doublings onto uncovered parts
            c = counts()
            uncovered = [i for i in insts if c.get(i, 0) == 0]
            surplus = [s for s in reversed(live) if c.get(s.instrument, 0) >= 2]
            if not uncovered or not surplus:
                break
            mover = surplus[0]
            mover.instrument = uncovered[0]
            if mover not in changed:
                changed.append(mover)
        return changed

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
        """Which placed section is the wand pointing at? Nearest azimuth (circular,
        so ±180 wraps) within max_gap_deg, else None = pointing at empty space.
        This is the bridge from manual placement to real yaw pointing."""
        placed = [s for s in self.sections.values() if s.connected and s.placed]
        if not placed:
            return None
        best = min(placed, key=lambda s: ang_dist(s.azimuth_deg, yaw_deg))
        return best.section_id if ang_dist(best.azimuth_deg, yaw_deg) <= max_gap_deg else None

    # --- persistence: instruments/placement survive a server restart ---
    def to_dict(self) -> dict:
        return {"next": self._next_section_num,
                "sections": [{"section_id": s.section_id, "client_id": s.client_id,
                              "instrument": s.instrument, "azimuth_deg": s.azimuth_deg,
                              "px": s.px, "py": s.py, "placed": s.placed,
                              "volume": s.volume, "muted": s.muted}
                             for s in self.sections.values()]}

    def restore(self, data: dict) -> None:
        """Rebuild the roster from a saved snapshot: every slot starts dropped;
        a phone rejoining with its stored client_id rebinds its old seat."""
        import time
        self._next_section_num = int(data.get("next", 1))
        for row in data.get("sections", []):
            s = Section(section_id=row["section_id"], client_id=row["client_id"],
                        instrument=row.get("instrument", "synth"),
                        azimuth_deg=float(row.get("azimuth_deg", 0.0)),
                        px=float(row.get("px", 0.0)), py=float(row.get("py", 0.9)),
                        placed=bool(row.get("placed", False)),
                        volume=float(row.get("volume", 1.0)), muted=bool(row.get("muted", False)),
                        connected=False, ready=False, dropped_at=time.time())
            self.sections[s.section_id] = s

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
                "mode": self.wand.mode,
                "det_param": self.wand.det_param,
            },
        }
