# Training the decision model (Freesolo)

The conductor's musical brain is a **swappable decision policy**: given the
musical context and the latest gesture, emit one tiny JSON decision —

```json
{"candidate": "rhythmic_dense", "octave_shift": 0}
```

Out of the box the hand-written heuristic (`server/ml/heuristic.py`) is the
policy. This doc is the full path to replacing it with a model you post-train
on Freesolo — and how the server keeps playing flawlessly when that model is
slow, down, or wrong.

**Verified facts** (checked 2026-07-18): Freesolo lives at **freesolo.co**
(not .ai — that domain is parked). Runs are pre-quoted fixed-price
(`--cost`), small LoRA runs finish in minutes-to-hours and cost single-digit
dollars, and a deploy gives an **OpenAI-compatible** endpoint. Critical rule:
**`structured_outputs` is only valid for GRPO/OPD — an SFT config containing
it is rejected at submit.**

## How the pieces fit

```
play sessions ──> server/data/decisions/*.jsonl      (logged live, every bar)
                          │
                          v
        python server/tools/build_dataset.py         (+ synthetic sweep + TASTE_RULES)
                          │
                          v
              freesolo/dataset/{train,eval}.jsonl    ({"input","output"} rows)
                          │
        flash train freesolo/configs/sft.toml        (imitation)
        flash train freesolo/configs/rl.toml         (GRPO vs environment.py reward)
                          │
        flash deploy <run-id>                        (OpenAI-compatible endpoint)
                          │
                          v
   WM_MODEL_URL/WM_MODEL_NAME/WM_MODEL_KEY -> server (heuristic fallback built in)
```

## Step 1 — harvest data by playing

Run the server and conduct. Every bar the conductor appends a JSONL row
(context, decision, source) to `server/data/decisions/session-*.jsonl`; a
wand thumbs-up/down (`wand.feedback`) attaches to the decision it judged —
thumbs-down rows are dropped from the dataset, thumbs-up rows weighted 3x.
An hour of deliberate conducting (vary energy, twists, lifts, stillness) is
a few thousand rows. `WM_DECISION_LOG=0` disables logging.

## Step 2 — build the dataset

```bash
python server/tools/build_dataset.py          # -> freesolo/dataset/{train,eval}.jsonl
```

Merges your harvested rows with a seeded synthetic sweep of the feature space
labeled by the heuristic. **Edit `TASTE_RULES` in build_dataset.py** — label
overrides where your musical judgment disagrees with the heuristic. That's
the point of training at all: the model becomes yours, not a lossy copy of
`heuristic.py`. Output rows are validated against the decision schema
(`server/ml/schema.py DECISION_SCHEMA`) before writing.

## Step 3 — train on Freesolo

```bash
uv tool install freesolo-flash
flash login --api-key            # key from the freesolo.co dashboard; prepay a small balance
flash env setup                  # scaffolds the env — reconcile freesolo/ with what it generates
flash env push                   # uploads the environment + dataset/

flash train freesolo/configs/sft.toml --cost    # fixed quote, no submit
flash train freesolo/configs/sft.toml           # stage 1: imitation
flash train freesolo/configs/rl.toml --cost
flash train freesolo/configs/rl.toml            # stage 2: GRPO vs score_response
```

- `freesolo/environment.py:score_response` is the GRPO reward: 0.35 format +
  0.35 gesture-consistency + 0.15 octave-shift intent + 0.15 don't-repeat.
  It inlines a port of `heuristic.rank` — if you tune the heuristic, re-port.
- `rl.toml` embeds the decision schema as `structured_outputs`, so the
  GRPO'd adapter physically cannot emit off-format tokens (and it becomes
  the serving default). Do **not** add that key to `sft.toml`.
- Budget guide: an SFT on a ~2B model quotes in the ~$10 range, a 300-step
  GRPO on a small model in cents-to-dollars. Model ids: see the catalog at
  freesolo.co/docs/reference/models; key names may drift — trust the
  `flash env setup` scaffold over these files where they disagree.

## Step 4 — deploy and point the server at it

```bash
flash deploy <run-id>
export WM_MODEL_URL=https://<serving-host>/v1   # the deploy's OpenAI-compatible base
export WM_MODEL_NAME=<run-id>
export WM_MODEL_KEY=<key>
python server/main.py
```

Sanity-check the endpoint first with the standard OpenAI SDK
(`base_url=$WM_MODEL_URL, model=$WM_MODEL_NAME`) or curl.

## What the server does with it (and without it)

- On every completed gesture the conductor fires an **async** ask
  (`server/ml/policy.py RemoteModel`) with an ~800ms budget
  (`WM_MODEL_TIMEOUT_MS`). The bar is never delayed: decisions are consumed
  at the next bar boundary, and there is a full bar (~2.4s at 100 BPM) of
  headroom.
- Priority per bar: **editor override > model answer > heuristic**. A model
  answer stays active until the next gesture; a new gesture clears it so a
  stale answer can never outlive the intent that asked for it.
- Any failure — timeout, HTTP error, off-format reply — logs one warning and
  the heuristic covers, silently. Pull the network cable mid-set and the
  music does not stop. The editor/stage roster shows which brain made the
  last call (`engine.decision_source`).
- Every decision (model or heuristic) is logged back into the harvest, so
  each rehearsal makes the next training run better.

## Verify headless

```bash
python server/tools/policy_test.py    # fake endpoint: model path, fallback, dataset validity
python server/tools/gesture_test.py   # heuristic path unchanged
```
