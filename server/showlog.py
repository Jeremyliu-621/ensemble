"""Tamper-evident show ledger: an append-only, hash-chained record of the
performance's structural events (transport, joins, drops, songs, wand moments).

Each event's SHA-256 covers the previous event's hash, so the final head hash
commits to the entire set — that's what tools/mint_cnft.py puts on-chain as
the verifiable fingerprint of an unrepeatable performance. Events mirror to
server/data/shows/<name>.jsonl as they happen (lazy-open, crash-safe), and
write_manifest() drops a stats+head summary when the show ends.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

import config

log = logging.getLogger("showlog")

_CHAIN_KEYS = ("i", "ts", "kind", "data", "prev")


def event_hash(ev: dict) -> str:
    payload = json.dumps({k: ev[k] for k in _CHAIN_KEYS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def verify(events: list[dict]) -> bool:
    """Recompute the whole chain; True iff untampered and correctly linked."""
    prev = "genesis"
    for ev in events:
        if ev.get("prev") != prev or event_hash(ev) != ev.get("hash"):
            return False
        prev = ev["hash"]
    return True


class ShowLog:
    def __init__(self, session: str) -> None:
        self.session = session
        self.events: list[dict] = []
        self.head = "genesis"
        self._file = None
        self._path = None
        self._opened = False
        self._started: float | None = None

    def record(self, kind: str, **data) -> dict:
        ev = {"i": len(self.events), "ts": round(time.time(), 3),
              "kind": kind, "data": data, "prev": self.head}
        ev["hash"] = event_hash(ev)
        self.head = ev["hash"]
        self.events.append(ev)
        if kind == "show.start" and self._started is None:
            self._started = ev["ts"]
        self._persist(ev)
        return ev

    def manifest(self) -> dict:
        kinds: dict[str, int] = {}
        for ev in self.events:
            kinds[ev["kind"]] = kinds.get(ev["kind"], 0) + 1
        return {
            "session": self.session,
            "started": self._started,
            "ended": round(time.time(), 3),
            "events": len(self.events),
            "head_hash": self.head,
            "kinds": kinds,
        }

    def write_manifest(self):
        """Write <show>.manifest.json next to the event log; path or None."""
        if self._path is None:
            return None
        path = self._path.with_suffix(".manifest.json")
        try:
            path.write_text(json.dumps(self.manifest(), indent=2), encoding="utf-8")
            log.info("show manifest: %s (head %s)", path, self.head[:16])
            return path
        except OSError as e:
            log.warning("manifest write failed: %s", e)
            return None

    def _persist(self, ev: dict) -> None:
        if not self._opened:
            self._opened = True
            try:
                config.SHOWS_DIR.mkdir(parents=True, exist_ok=True)
                self._path = config.SHOWS_DIR / time.strftime(f"show-{self.session}-%Y%m%d-%H%M%S.jsonl")
                self._file = open(self._path, "a", encoding="utf-8")
                log.info("show ledger: %s", self._path)
            except OSError as e:
                log.warning("show ledger disabled: %s", e)
        if self._file is None:
            return
        try:
            self._file.write(json.dumps(ev, separators=(",", ":")) + "\n")
            self._file.flush()
        except OSError as e:
            log.warning("show ledger write failed: %s", e)
            self._file = None
