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

## Theory-device SFT v3 — judge-approved, on ear trial (2026-07-18)

| Model | Run id |
|---|---|
| Bar-line v3 (theory devices: +passing/arpeggio/echo styles), Qwen3.5-4B SFT | `flash-1784407398-edf55491` |

Eval (server/tools/eval_barmodel.py, 10 rows/style, retry-on-timeout):
harmonize **0.990** / passing **0.990** / arpeggio **0.988** / dense 0.990 /
calm 0.977 — every style matches the deterministic teacher, 0 format failures,
and harmonize BEATS the keeper's 0.963. The live demo now serves v3 with
`WM_BARMODEL_STYLES=harmonize,dense,calm,counter,echo,free,passing,arpeggio`;
the keeper remains deployed and is the instant rollback (swap
WM_BARMODEL_NAME back and drop the styles var). v3 becomes the new keeper only
after it survives the ear test. Next: GRPO chained from this adapter
(`init_from_adapter` in freesolo/barline/configs/rl.toml).

## Fast serving — Fireworks dedicated (2026-07-18)

v3 with the LoRA MERGED into the base (scratchpad/merge_lora.py, spot-verified
bit-exact) serves on a Fireworks dedicated deployment: **453ms median / 746ms
max** per composed bar (vs ~4.6s on the shared pool), judge mean 0.968, live
styles 0.95-1.00. Details the next person needs:

- Model + deployment live on Caellum's Fireworks org; HF mirror of the raw
  adapter: `Caecae2k/wand-barline-v3`. Deployments MUST use the validated
  shape `accounts/fireworks/deploymentShapes/qwen3p5-4b-minimal` (H200/FP8) —
  a generic H100/bf16 deployment fails with an internal error.
- Fireworks' qwen3_5 chat template returns the answer in `reasoning_content`
  with `content` empty; both model clients fall back to it (ml/barmodel.py,
  ml/policy.py).
- Env (values NOT in the repo — ask Caellum): WM_BARMODEL_URL points at the
  Fireworks OpenAI base, WM_BARMODEL_NAME is `model#deployment`,
  WM_BARMODEL_PREFETCH=1 (one-bar horizon), WM_BARMODEL_TIMEOUT_MS=4000.
- ~$7/hr while replicas are up; scale-to-zero after 30 idle min. Freesolo
  serving of the un-merged v3 stays deployed as the free instant fallback.

Ear verdicts on the ship-now devices (2026-07-18, Zora's Domain renders):
harmonize + hush strongest, arpeggio + passing good, **echo CUT** (sounded
out of place even after the underlap fix — remains in the trained styles but
is never invoked live).
