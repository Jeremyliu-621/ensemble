"""Validation and diagnostics for incoming ``wand.imu`` batches."""
from __future__ import annotations

import math


def _finite_number(value: object) -> bool:
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value)))


class ImuTelemetry:
    """Validate IMU input and retain a compact stream-health snapshot."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.batches = 0
        self.frames = 0
        self.invalid_frames = 0
        self.seq_gaps = 0
        self.last_seq: int | None = None
        self.last_frame: list[float] | None = None
        self.last_rx_server_ms: float | None = None

    def ingest(self, seq: object, frames: object, server_ms: float) -> list[list[float]]:
        """Count one batch and return only its valid, normalized frames."""
        self.batches += 1

        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            self.invalid_frames += len(frames) if isinstance(frames, list) else 1
            return []

        if self.last_seq is None:
            self.last_seq = seq
        elif seq > self.last_seq:
            self.seq_gaps += max(0, seq - self.last_seq - 1)
            self.last_seq = seq

        if not isinstance(frames, list):
            self.invalid_frames += 1
            return []

        valid: list[list[float]] = []
        for frame in frames:
            if (not isinstance(frame, list) or len(frame) != 7
                    or not all(_finite_number(value) for value in frame)):
                self.invalid_frames += 1
                continue
            valid.append([float(value) for value in frame])

        if valid:
            self.frames += len(valid)
            self.last_frame = valid[-1]
            self.last_rx_server_ms = float(server_ms)
        return valid

    def snapshot(self) -> dict:
        return {
            "seq": self.last_seq,
            "batches": self.batches,
            "frames": self.frames,
            "invalid_frames": self.invalid_frames,
            "seq_gaps": self.seq_gaps,
            "last_frame": self.last_frame,
            "last_rx_server_ms": self.last_rx_server_ms,
        }
