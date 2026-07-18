"""Append-only JSONL log of every musical decision — the training-data harvest.

One file per server run under server/data/decisions/ (gitignored), opened
lazily on the first row. Each bar the conductor logs (context, decision,
source); a wand thumbs-up/down is attached to the decision it judged.
tools/build_dataset.py turns these logs into Freesolo training rows, so an
hour of real conducting becomes model data. WM_DECISION_LOG=0 disables it.
"""
from __future__ import annotations

import json
import logging
import time

import config
from ml.schema import Decision

log = logging.getLogger("datalog")


class DecisionLog:
    def __init__(self) -> None:
        self._file = None
        self._opened = False
        self._n = 0

    def decision(self, *, bar: int, song: str, context: dict, decision: Decision) -> None:
        self._n += 1
        self._write({"id": self._n, "ts": round(time.time(), 3), "bar": bar, "song": song,
                     "context": context, "candidate": decision.candidate,
                     "octave_shift": decision.octave_shift, "source": decision.source})

    def feedback(self, value: int) -> None:
        if self._n:
            self._write({"ts": round(time.time(), 3), "feedback": value, "for_id": self._n})

    def _write(self, row: dict) -> None:
        if not self._opened:
            self._opened = True
            if config.DECISION_LOG:
                try:
                    config.DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
                    path = config.DECISIONS_DIR / time.strftime("session-%Y%m%d-%H%M%S.jsonl")
                    self._file = open(path, "a", encoding="utf-8")
                    log.info("decision log: %s", path)
                except OSError as e:
                    log.warning("decision log disabled: %s", e)
        if self._file is None:
            return
        try:
            self._file.write(json.dumps(row, separators=(",", ":")) + "\n")
            self._file.flush()
        except OSError as e:
            log.warning("decision log write failed: %s", e)
            self._file = None
