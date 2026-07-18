"""The contract between the realtime layer and the music engine.

The scheduler and wand router call ONLY the methods on `MusicEngine`. Anything
implementing this Protocol (the P1 metronome stub, the real symbolic engine
later) drops in with zero changes to the realtime layer. All calls are
synchronous and must return quickly — a slow engine can only dull musical
responsiveness, never break clock sync, because events are pulled on a lookahead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


# --- Data crossing the boundary ---

@dataclass
class SectionInfo:
    """A joined orchestra section, as the engine needs to see it."""
    section_id: str
    instrument: str
    azimuth_deg: float = 0.0
    ready: bool = False
    volume: float = 1.0      # 0..1, scales this section's note velocities
    muted: bool = False


@dataclass
class ImuFrame:
    """One raw IMU sample. accel m/s^2 incl. gravity; gyro deg/s; tw = wand-local ms."""
    tw: float
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float


@dataclass
class GestureWindow:
    """A complete grab->release gesture. The realtime layer buffers frames during
    the grab and hands the engine one window on release. `modality` is "imu"
    (phone/ESP32, frames [tw,ax,ay,az,gx,gy,gz]) or "pose" (webcam, frames
    [tw,x,y,z,roll_deg]); `frames` are the raw on-the-wire rows."""
    modality: str
    frames: list[list[float]]
    t_start_server_ms: float
    t_end_server_ms: float


@dataclass
class NoteEvent:
    """A scheduled note. `at` is server-time ms and MUST be >= now + MIN_LEAD_MS
    when returned from get_events. `section` may be SECTION_ALL to hit every section.
    `note` is a scientific-pitch string (Tone/WebAudio-native, e.g. "C4").
    `inst` names the sampled instrument for this note (a loaded MIDI part's
    voice); None lets the playing device use its own configured instrument."""
    id: str
    section: str
    at: float
    dur: float
    note: str
    vel: float = 0.8
    art: str = "pluck"
    inst: str | None = None


@dataclass
class CancelSpec:
    """Cancel already-broadcast events. Either explicit ids, or everything for a
    section at/after a time, or a global panic (allnotesoff)."""
    ids: list[str] = field(default_factory=list)
    section: str | None = None
    after: float | None = None
    allnotesoff: bool = False


# --- The interface ---

@runtime_checkable
class MusicEngine(Protocol):
    def on_sections_changed(self, sections: list[SectionInfo]) -> None: ...

    def on_transport(self, cmd: str, t0_ms: float | None) -> None:
        """cmd in {"start","stop","clicktest","resync","allnotesoff"}. `start`
        anchors the musical timeline at t0_ms (server clock)."""
        ...

    def on_gesture(self, window: GestureWindow) -> None: ...

    def on_grab(self, kind: str, server_ms: float) -> None:
        """kind in {"start","end"}. `end` may cause sustained notes to be cut."""
        ...

    def on_aim(self, section_id: str | None) -> None: ...

    def on_feedback(self, value: int) -> None: ...

    def get_events(self, now_ms: float, until_ms: float) -> list[NoteEvent]:
        """Return NOT-yet-emitted events with `at` in roughly (now, until]. Each
        event is returned exactly once (the engine tracks its own cursor)."""
        ...

    def get_cancels(self) -> list[CancelSpec]:
        """Return pending cancellations since the last call, then clear them."""
        ...
