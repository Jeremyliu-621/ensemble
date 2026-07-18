# Trained model registry — the weights and where they live

Every adapter is stored permanently by Freesolo (run ids never expire) AND
mirrored to a Hugging Face repo at train time. To re-serve any of them:
`flash deploy <run-id>`; to copy weights to our own HF account:
`flash export --adapter-id <run-id> --repository <org/name>`.

## KEEPER — the approved baseline (do not lose)

| Model | Run id | Serving alias | HF repo |
|---|---|---|---|
| Decision (gesture → intent), Qwen3.5-2B SFT | `flash-1784391958-0aa455cc` | same | `Freesolo-Co/flashrun-neuroscore-wand-decision-01ce43dce9ed1809` |
| **Bar-line HARMONIZE v2** (chord theory), Qwen3.5-4B SFT | `flash-1784401136-51c64a15` | same | `Freesolo-Co/flashrun-neuroscore-wand-barline-a2894d1a851e6f5c` |
| Bar-line v1 (pre-harmonize, superseded) | `flash-1784391961-091caa0e` | undeploy when v2 is proven | same repo (earlier revision) |

Eval (2026-07-18): decision — 30/30 valid JSON, 97% label agreement.
Harmonize v2 — mean reward **0.963** on held-out harmonize prompts (equal to
the deterministic theory layer), min 0.900, 0 format failures, ~3.7s median
serving latency. User-approved by ear on Great Fairy Fountain + Zora's Domain.

## Serving env (what the live server runs)

```
WM_MODEL_URL=https://clado-ai--freesolo-lora-serving.modal.run/v1
WM_MODEL_NAME=flash-1784391958-0aa455cc
WM_BARMODEL_URL=https://clado-ai--freesolo-lora-serving.modal.run/v1
WM_BARMODEL_NAME=flash-1784401136-51c64a15
WM_MODEL_KEY / WM_BARMODEL_KEY = the org's Freesolo API key
```

Fallback chain if serving is ever down: `tools/mock_model.py` locally, and the
deterministic theory in `engine/harmony.py` under everything.

## Next planned run (do NOT replace the keeper — new runs get NEW ids)

Theory-device model: the deep-researched catalog of diatonic composer devices
(approach runs/glissandi, suspensions, passing tones, pedal points, ...) as
new styles with generators + rewards. The keeper stays deployed until the new
one beats it by ear AND by judge.

Ear verdicts on the ship-now devices (2026-07-18, Zora's Domain renders):
harmonize + hush strongest, arpeggio + passing good, **echo CUT** (sounded
out of place even after the underlap fix — remains in the trained styles but
is never invoked live).
