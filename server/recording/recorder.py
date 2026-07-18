"""Gesture recorder — the data-collection half of the gesture-ML pipeline.

When recording is on, every completed grab->release gesture window is appended
to data/gestures/<session>.jsonl with its label, ready to train a classifier
(the research recommends DTW/Jackknife on 1-2 templates per gesture per user —
see docs/audio-sync-research.md and RESEARCH.md). One JSON object per line:
  {"label","modality","t_start","t_end","frames":[[...], ...]}
"""
from __future__ import annotations

import json
import logging

from config import SERVER_DIR
from engine_api import GestureWindow

log = logging.getLogger("record")


class GestureRecorder:
    def __init__(self, session: str) -> None:
        self._dir = SERVER_DIR / "data" / "gestures"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{session}.jsonl"
        self.enabled = False
        self.label = ""
        self.count = 0

    def start(self, label: str) -> None:
        self.enabled = True
        self.label = (label or "unlabeled").strip()
        log.info("recording gestures as %r -> %s", self.label, self._path.name)

    def stop(self) -> None:
        self.enabled = False
        log.info("recording stopped (%d saved this session)", self.count)

    def record(self, window: GestureWindow) -> None:
        if not self.enabled:
            return
        rec = {
            "label": self.label,
            "modality": window.modality,
            "t_start": window.t_start_server_ms,
            "t_end": window.t_end_server_ms,
            "frames": window.frames,
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self.count += 1

    def status(self) -> dict:
        return {"recording": self.enabled, "label": self.label, "count": self.count}
