"""AI-mode scaffold — future on-device gesture inference for the UNO Q.

TODAY THIS IS INERT. The real decision + bar-line models run on Freesolo
endpoints hit by the laptop server (see docs/ai-training.md, server/ml/). This
module is the placeholder for eventually classifying gesture windows *on the
board's Linux side* to cut the Freesolo round-trip.

Target output contract (mirror of server/ml/schema.py DECISION_SCHEMA):
    {"candidate": <generator name>, "octave_shift": -1 | 0 | 1}
"""
from __future__ import annotations

import logging

log = logging.getLogger("wand.ai")


class AiMode:
    def __init__(self, model_path: str | None = None):
        self.model = None
        self.enabled = False            # tracks whether the wand is in "ai" mode
        if model_path:
            self.load(model_path)

    def load(self, path: str) -> None:
        """Load a local model artifact. TODO: wire to whatever runtime the
        trained on-device model ships as (e.g. ONNX Runtime / TFLite)."""
        log.info("ai_mode: model load requested (%s) — scaffold, not implemented", path)
        # self.model = <load path>

    def on_state(self, st) -> None:
        """Track show state pushed from the laptop; only relevant in ai mode."""
        self.enabled = (st.mode == "ai")

    def infer(self, window) -> dict | None:
        """Classify one gesture window -> decision dict, or None to defer to the
        server. Returns None today so the server's model stays authoritative."""
        if not self.enabled or self.model is None:
            return None
        # TODO: features = extract(window); return self.model.predict(features)
        return None
