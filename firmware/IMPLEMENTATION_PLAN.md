# Firmware Implementation Plan — UNO Q Wand (WiFi build)

> Companion to `firmware/BACKEND_NOTES.md`. That file describes the *minimal*
> pure-streamer wand. This plan implements the **full bidirectional wand** you
> asked for: it streams Modulino movement data up, **and** receives laptop state
> (pause/play, ai↔det mode, selected phone) back down over a new `wand.cmd`
> message, plus an on-board AI-mode scaffold and phone-selection helper.

---

## Context

The wand is an **Arduino UNO Q + Modulino Movement (IMU)**. The UNO Q is a
dual-brain board: a real-time **MCU** (runs the `.ino`, talks I²C to the
Modulino) and a **Qualcomm Linux SoC** (runs Python, holds WiFi/WebSocket). They
talk over the on-board **Bridge** channel.

Today the server (`server/main.py`, `:8080` ws) treats the wand as a **pure
uplink**: it consumes `wand.imu` for aiming / gestures / expression, but it never
sends the wand anything except `welcome` / `clock.pong` / `err` (confirmed:
`server/hub.py:74-82` filters broadcasts to `section`/`stage`/`admin` only — wands
receive no broadcasts). So there is **no way for laptop state to reach the board**.
`docs/hardware-tasks.md` Task 6 flags this as net-new.

This plan closes that gap with a dedicated **`wand.cmd`** server→wand message, so
CV gestures on the laptop (palm = pause/play → `admin.cmd`; fist = `wand.mode`;
point = aim) end up reflected on the physical wand (LED / buzz / internal state),
and gives the board a place to eventually run its own AI-mode model.

### Locked decisions (from planning Q&A)
1. **Downlink = new `wand.cmd` message.** Edit `server/protocol.py` +
   `server/main.py` (+ mirror `web/shared/protocol.js`). This is the best-practice
   channel rather than a second observer socket.
2. **Transport = WiFi-only, on-board.** MCU `Bridge.notify` → on-board Python →
   `ws://<laptop-lan-ip>:8080/ws`. No USB-serial bring-up, no `wand_bridge.py`.
3. **AI-mode stub = scaffold for future on-device inference** (`ai_mode.py`:
   `load()` / `infer(window)` stubs, currently returns `None` so the server's
   Freesolo models stay authoritative until the on-device model is trained).

---

## Architecture

```
  ┌──────────────── UNO Q ────────────────┐
  │  MCU (.ino)              Linux (Python)│
  │  Modulino @0x6A ─I²C─┐                 │            LAPTOP
  │  sample ~60Hz        │  wand_link.py   │        server/main.py
  │  g→m/s², raw gyro    ├─Bridge.notify──►│  ws role=wand           :8080
  │                      │   "imu"[7]      │  ── wand.imu ──────────► aiming/
  │                      │                 │                          gesture/
  │  LED / buzzer  ◄─────┤◄─Bridge.notify──┤  ◄── wand.cmd ─────────  det-expr
  │  reflect state       │   "cmd"{state}  │      {playing,mode,aim}
  └──────────────────────┴─────────────────┘
                                   Linux also runs:
                                     ai_mode.py     (scaffold, mode=="ai")
                                     phone_select.py(tracks aim, recal)
```

- **Uplink** (unchanged contract): MCU samples → `Bridge.notify("imu", row)` →
  `wand_link.py` batches ~5 rows → `wand.imu` JSON → WebSocket.
- **Downlink** (new): server sends `wand.cmd` on the same WebSocket →
  `wand_link.py` fans it to `Bridge.notify("cmd", {...})` (→ MCU reflects it) and
  to `phone_select` / `ai_mode`.

---

## Part A — Server: the `wand.cmd` downlink (Python + JS)

Minimal, additive, non-breaking. Only wand-role connections ever receive it.

**A1. `server/protocol.py`** — add one constant + payload doc in the Server→Client
block (near `WAND_STATE = "wand.state"`, L37):
```python
WAND_CMD = "wand.cmd"   # server -> wand: reflect laptop state on the board.
#   {playing: bool, mode: "ai"|"det", aim: section_id|null, seq: int}
```

**A2. `web/shared/protocol.js`** — mirror the same constant in the Server→Client
section (keep the two files in sync, as the header comment there requires).

**A3. `server/main.py`** — add a sender + emit at the three state-change sites.
The wand connection is already tracked as `self._wand_client` (claimed in
`_on_hello`, ~L209-216). Add a throttled helper:
```python
def _notify_wand(self):
    conn = self._wand_client
    if conn is None: return
    self._wand_seq += 1
    payload = {"t": P.WAND_CMD,
               "playing": self.session.playing,
               "mode": self.session.wand.mode,
               "aim": self._last_aim,          # section id or None
               "seq": self._wand_seq}
    await conn.send(payload)   # fire-and-forget; guard/try like other sends
```
Emit `_notify_wand()` at:
- **Transport change** — end of `_admin` (L382-422), after `session.playing`
  flips on `start`/`stop`/`rewind`/`forward`. (This is the "pause music" path:
  CV palm → `admin.cmd` → `_admin` → `wand.cmd{playing:false}` → board LED.)
- **Mode change** — in the `wand.mode` handler (L337-348), after
  `session.wand.mode` is set.
- **Aim change** — in `_update_aim` (L512-528), alongside the existing
  `WAND_STATE` broadcast; reuse the same 150 ms throttle and the resolved `aim`.
- **Initial snapshot** — right after sending `welcome` in `_on_hello`
  (L229-236), so a freshly-connected board syncs current state immediately.

Reuse existing plumbing: `self.session.playing`, `self.session.wand.mode`, and
the aim already computed in `_update_aim`. No new state machine on the server.

**Guardrails:** wrap the send so a dead wand socket can't break `_admin`
broadcasts; skip entirely when `self._wand_client is None`. This keeps the change
invisible to phones/stage.

---

## Part B — MCU sketch: `firmware/uno_q/wand/sketch/sketch.ino`

Pure sensor loop **plus** a downlink reflector. No classification, no yaw
integration on-board (the server integrates `gz`).

- **Init:** `Wire1.begin()` (Qwiic is `Wire1` on the UNO Q, *not* `Wire`),
  `Modulino.begin(Wire1)`, `ModulinoMovement imu; imu.begin()` (@0x6A).
- **Sample loop ~60 Hz:** `imu.update()`, build one row
  `[tw=millis(), getX()*9.81, getY()*9.81, getZ()*9.81, getRoll(), getPitch(), getYaw()]`
  — accel g→m/s² **keeping gravity**; gyro **raw deg/s**. Push each row up:
  `Bridge.notify("imu", row)`.
- **Downlink reflect:** `Bridge.provide("cmd", onCmd)` where `onCmd({playing,
  mode, aim})` drives physical feedback — e.g. onboard LED solid when `playing`,
  blink when paused; a second LED / color for `mode` (ai vs det); optional short
  buzz on `aim` change. Keep the mapping in one small `applyState()` function so
  it's easy to retune. If no LED/buzzer is wired yet, `onCmd` just stores the
  latest state (still useful for debugging over the Linux log).
- **No** grab/mode/touch/range logic on the MCU — those come from the CV client.

> Verify method names (`getX/Y/Z`, `getRoll/Pitch/Yaw`, `.begin()`, `.update()`)
> and the `Bridge` API (`notify`/`provide`) against the installed Arduino
> `Modulino` + App Lab libs on real hardware — BACKEND_NOTES calls this out.
> Documented fallback if `Bridge` fights us: CSV-over-serial MCU↔Linux.

---

## Part C — Linux uplink + downlink: `firmware/uno_q/wand/python/wand_link.py`

The heart of the WiFi build. Mirrors `server/tools/wand_bridge.py:37-67`'s
handshake/forward logic, but runs **on the board** and is bidirectional. Uses
`arduino.app_utils.Bridge` + `websockets.asyncio.client`.

- **Handshake** (copy `wand_bridge.py` verbatim): connect
  `ws://<laptop-lan-ip>:8080/ws`, send
  `{"t":"hello","v":1,"role":"wand","session":"lol1","client_id":<cached|null>}`,
  read `welcome`, cache `client_id`, echo it on every reconnect. Reconnect forever
  (2 s serial-style backoff → here it's WS backoff).
- **Uplink task:** `Bridge.provide("imu", on_imu)` appends rows to a buffer; when
  `len(buf) >= 5`, pop 5, `seq += 1`, send
  `{"t":"wand.imu","seq":seq,"frames":frames}`. ~10-12 Hz message rate.
- **Downlink task:** `async for msg in ws:` — on `msg["t"] == "wand.cmd"`, update
  the shared `WandState` and fan out:
  - `Bridge.notify("cmd", {playing, mode, aim})` → MCU reflects it.
  - `phone_select.on_cmd(state)` and `ai_mode.on_state(state)`.
  Ignore `welcome`/`clock.pong`/`err` (log `err`).
- **Shared state:** a tiny `WandState` dataclass (`playing`, `mode`, `aim`,
  `client_id`) in `firmware/uno_q/wand/python/state.py`, passed to the helpers.

---

## Part D — AI-mode scaffold: `firmware/uno_q/wand/python/ai_mode.py`

Placeholder framed for **future on-device inference** (not wired to affect audio
yet — the server's Freesolo decision/bar models stay authoritative).
```python
class AiMode:
    """SCAFFOLD: eventually classify gesture windows on the UNO Q's Linux side
    instead of round-tripping to Freesolo. Today it is inert."""
    def __init__(self): self.enabled = False; self.model = None
    def load(self, path): ...          # TODO: load a local model artifact
    def on_state(self, st): self.enabled = (st.mode == "ai")
    def infer(self, window): return None   # TODO: -> {"candidate":..., "octave_shift":...}
```
Wire `on_state` into `wand_link`'s downlink so it tracks mode. `infer()` is called
nowhere in the minimal build; the docstring + `server/ml/schema.py`
(`DECISION_SCHEMA`) note documents the target output contract for whoever trains
it. This satisfies "placeholder for the model that runs in ai mode" without
changing current behavior.

---

## Part E — Phone selection: `firmware/uno_q/wand/python/phone_select.py`

Selection itself is **server-side** (yaw→azimuth lock within 40°,
`server/wandio.py:80-117`). The board doesn't decide *which* phone — it **tracks
and reflects** the current selection and can trigger a re-zero.
```python
class PhoneSelect:
    def __init__(self, send): self.send = send; self.aim = None
    def on_cmd(self, st): self.aim = st.aim          # from wand.cmd downlink
    async def recal(self):                            # "this way is forward"
        await self.send({"t": "wand.recal", "tw": now_ms()})
```
- `on_cmd` keeps the selected `section_id` current (fed to the MCU LED via
  `wand_link`).
- `recal()` sends the existing `wand.recal` message (`main.py:358-360` zeroes
  `WandAimer` yaw). Trigger it from a future button, or expose it as a small
  function callable at startup / from a debug prompt.
- Optional stretch: map a physical button (pads 6+ are reserved for firmware) to
  cycle a *forced* aim — but the clean path is physical pointing, so this stays a
  stub note, not built now.

---

## Part F — Config + entrypoint

- **`firmware/uno_q/wand/python/config.py`** — `LAPTOP_IP` (LAN IP the phones use;
  server logs it via `detect_lan_ip`, `main.py:83-105`), `SESSION = "lol1"`,
  `WS_PORT = 8080`, `BATCH = 5`, `MODEL_PATH = None`.
- **`firmware/uno_q/wand/python/main.py`** — asyncio entrypoint: build `WandState`,
  `AiMode`, `PhoneSelect`, start `wand_link` (which owns the WebSocket + Bridge
  wiring), run forever. This is what Arduino App Lab launches on the Linux side.

---

## Files to create / modify

```
firmware/uno_q/wand/sketch/sketch.ino          NEW  (Part B)
firmware/uno_q/wand/python/main.py                    NEW  (Part F entrypoint)
firmware/uno_q/wand/python/wand_link.py              NEW  (Part C — WS + Bridge)
firmware/uno_q/wand/python/ai_mode.py                NEW  (Part D — scaffold)
firmware/uno_q/wand/python/phone_select.py           NEW  (Part E)
firmware/uno_q/wand/python/state.py                  NEW  (WandState dataclass)
firmware/uno_q/wand/python/config.py                 NEW  (Part F config)
firmware/uno_q/wand/python/requirements.txt          NEW  (websockets)
server/protocol.py                             EDIT (A1: WAND_CMD)
web/shared/protocol.js                         EDIT (A2: mirror)
server/main.py                                 EDIT (A3: _notify_wand + 4 sites)
```

---

## Verification (end-to-end)

1. **Server change in isolation** — start `python server/main.py`. Open the CV
   wand (`web/cvwand/`) or `web/wandsim/`, connect as a wand, and confirm the
   server log still shows `wand connected (variant=hw/…)`. Palm-pause via CV and
   confirm no regressions in `_admin` broadcasts (phones still mute/resume).
2. **`wand.cmd` emission** — temporarily log outgoing `wand.cmd` in `_notify_wand`
   (or watch with a scratch WS client joined as role `wand`). Trigger: palm
   pause → expect `{playing:false}`; fist toggle → `{mode:"det"}`; point the
   wand → `{aim:"s2"}`. Confirm one snapshot arrives right after `welcome`.
3. **Board uplink** — flash the MCU sketch, run `python/main.py` on the UNO Q Linux side
   pointed at `LAPTOP_IP`. Wave the wand; server log should print `wand.imu`
   frames and aim changes. Validate the **sensor math** first: accel ≈ 9.8 on the
   down axis at rest (gravity included), gyro ≈ 0 at rest.
4. **Board downlink** — with the board connected, pause from the CV client and
   confirm `wand_link` receives `wand.cmd` and the MCU `onCmd` fires (LED changes
   / logged). Point at different phones → aim LED tracks.
5. **Reconnect** — kill WiFi briefly; confirm `wand_link` reconnects, re-sends
   `hello` with the cached `client_id`, and the server re-issues a `wand.cmd`
   snapshot so the board re-syncs state.

## Risks / notes
- **Bridge API names** (`notify`/`provide`) and **Modulino method names** are
  unverified until on real hardware — CSV-over-serial MCU↔Linux is the documented
  fallback (BACKEND_NOTES §Option 2). Confirm early.
- **Single wand slot, latest wins** — if both the CV wand and the hardware wand
  connect as wand roles, they contend for the one slot. During bring-up, run one
  wand client at a time (the CV client can stay for gesture/transport if it joins
  as `wand-cv` — that's a separate concern, but be aware of slot ownership).
- **WiFi contention** — load-test with several phones joined before trusting the
  venue (`docs/hardware-integration-plan.md` §3).
- `ai_mode.infer()` is intentionally inert; wiring it into the audio path is a
  follow-up that must not regress the Freesolo model precedence
  (`server/engine/conductor.py`).
