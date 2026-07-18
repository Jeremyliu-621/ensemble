# Training the AI (two models on Freesolo)

The orchestra has **two trained brains**, both served as OpenAI-compatible
endpoints, both with instant rule-based fallbacks — the music can never stall
on a network call:

1. **Decision model** ("input → action"): given the musical context and the
   conductor's gesture, pick the accompaniment and register —
   `{"candidate": "rhythmic_dense", "octave_shift": 0}`. Replaces the
   hand-written ranker (`server/ml/heuristic.py`).
2. **Bar-line model** ("music editing"): given key/chord/melody and a style
   directive, *write a new accompaniment line note-by-note* —
   `{"notes": [[onset, dur, midi, vel], ...]}`. It appears in the engine as
   the extra candidate **"generated"**, prefetched one bar ahead, and every
   reply is sanitized (snapped to key, folded into register, clamped to the
   grid) so the model supplies contour and rhythm while the engine guarantees
   playability.

Both contracts live in `server/ml/schema.py` — the single source of truth for
the server parser, the dataset builders, and the GRPO `structured_outputs`.

**Verified facts** (checked 2026-07-18): Freesolo is at **freesolo.co** (not
.ai). Runs are pre-quoted fixed-price (`--cost`); small LoRA runs finish in
minutes-to-hours for single-digit dollars; `flash deploy` gives an
OpenAI-compatible endpoint. Critical rule: **`structured_outputs` is only
valid for GRPO/OPD — an SFT config containing it is rejected at submit.**

## The pipeline

```
                 DECISION MODEL                      BAR-LINE MODEL
play sessions -> server/data/decisions/*.jsonl   folder of .mid files (optional)
                        |                                 |
        tools/build_dataset.py                tools/build_bar_dataset.py
        (harvest + synthetic + TASTE_RULES)   (theory pairs + real arrangements)
                        |                                 |
        freesolo/decision/dataset/            freesolo/barline/dataset/
                        |                                 |
        flash train decision/configs/{sft,rl}  flash train barline/configs/{sft,rl}
                        |                                 |
        flash deploy -> WM_MODEL_URL/NAME/KEY  flash deploy -> WM_BARMODEL_URL/NAME/KEY
                        \_________________________________/
                                python server/main.py
                    (heuristic + rule-generators cover all failures)
```

## Division of labor

**On this machine (no account needed) — all built and tested:**
- Harvest data: run the server, conduct; every bar logs a training row, wand
  thumbs up/down weight them (`WM_DECISION_LOG=0` disables).
- Build datasets: `python server/tools/build_dataset.py` and
  `python server/tools/build_bar_dataset.py [--midi-dir songs/]`.
- Encode taste: edit `TASTE_RULES` in build_dataset.py (label overrides where
  your judgment disagrees with the heuristic) and feed the bar model MIDIs in
  the style the orchestra should speak.
- Rehearse without any deploy: `python server/tools/mock_model.py`, then start
  the server with the printed env vars — the full AI-enabled system runs
  against local stand-ins. This is also the on-stage fallback if the venue
  loses internet.
- Verify: `python server/tools/policy_test.py` and `barmodel_test.py`.

**On Freesolo (account + credits), per model:**
```bash
uv tool install freesolo-flash
flash login --api-key                 # dashboard key
flash env setup                       # scaffold; reconcile freesolo/<model>/ with it
flash env push                        # uploads environment + dataset/

flash train freesolo/decision/configs/sft.toml --cost   # fixed quote first, always
flash train freesolo/decision/configs/sft.toml
flash train freesolo/decision/configs/rl.toml --cost
flash train freesolo/decision/configs/rl.toml           # GRPO vs environment.py
flash deploy <run-id>
```
Then export the env vars and restart the server:
```bash
export WM_MODEL_URL=https://<host>/v1    WM_MODEL_NAME=<decision-run> WM_MODEL_KEY=<key>
export WM_BARMODEL_URL=https://<host>/v1 WM_BARMODEL_NAME=<barline-run> WM_BARMODEL_KEY=<key>
python server/main.py
```
Sanity-check any deploy with the plain OpenAI SDK/curl before wiring it in.

## Recommended training order (hackathon clock)

1. **Decision SFT** first — smallest model (0.8b/2b), minutes to train, and
   the demo story ("the model that picks the music was trained today on my
   conducting") lands immediately. Ship it, keep playing.
2. **Bar-line SFT** next (4b if the quote allows) — the flashier capability:
   the orchestra plays lines no rule wrote. `--midi-dir` data makes or breaks
   the phrasing; even 20-50 MIDIs help.
3. **GRPO both** once SFT adapters exist and you've heard their failure
   modes — the rewards (`freesolo/*/environment.py`) are already written:
   decision = format + gesture-consistency + shift-intent + don't-repeat;
   bar-line = format + grid + in-key + register + style match + melody
   clearance. Chain each from its SFT run.
4. **Re-harvest and retrain** — every rehearsal logs more rows; a second SFT
   pass the night before the demo is cheap and real.

Reward/heuristic sync warning: the GRPO environments inline ports of
`heuristic.rank` and `barmodel.sanitize_line` — if you tune those, re-port.

## What the server does at run time

- **Decision**: asked async on every completed gesture, ~800ms budget
  (`WM_MODEL_TIMEOUT_MS`); its answer holds until the next gesture; editor
  override > model > heuristic; a new gesture clears any stale answer. The
  roster's `engine.decision_source` shows which brain made the last call.
- **Bar-line**: prefetched for bar N+1 while bar N plays (~2.4s of headroom,
  `WM_BARMODEL_TIMEOUT_MS`); arrives as candidate "generated", which the
  ranker favors for flowing mid-energy gestures, the decision model can pick
  by name, and the editor can force ("AI-written line"). A missed bar just
  means the six rule-based candidates compete alone.
- Every decision, from either brain or the fallback, is logged back into the
  harvest — playing the instrument improves the next training run.

## Non-model backend (built — see show_test.py)

- **Set-memory commentator** (`server/announcer.py`): a Backboard.io
  assistant (MLH partner, $5 free credits) with `memory: "Auto"` follows the
  set — joins, drops, songs, vibe checks — and its one-line reactions toast
  on the stage (`announce`). Set `WM_BACKBOARD_KEY`; pin
  `WM_BACKBOARD_ASSISTANT` to keep memory across shows. Unset = inert.
  Voice: Backboard's `voice.tts` (ElevenLabs) can be added to the same call;
  the stage already plays `audio_b64` if present.
- **Show ledger** (`server/showlog.py`): every structural moment is
  hash-chained; `admin stop` writes `server/data/shows/*.manifest.json` whose
  head hash commits to the whole set. `tools/mint_cnft.py` mints it as a
  Solana cNFT via Crossmint staging (devnet, dry-runs without a key).
- **Hardware wand surface**: aiming (gyro yaw → the aimed phone carries the
  line, stage glows it), MPR121 pads → forced candidates, ToF distance →
  `fx.tension` filter sweeps on every phone. Firmware contract:
  [`hardware-wand.md`](hardware-wand.md).
