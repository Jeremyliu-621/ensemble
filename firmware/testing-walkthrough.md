# Testing walkthrough — CV app ⇄ server ⇄ Arduino wand

This guide walks the whole communication flow from the outside in, in the order
you should test it. Each phase is independently verifiable, so when something
breaks you know exactly which hop failed.

```
cv_hand_movements (webcam, role admin)  ──admin.cmd / wand.mode──┐
                                                                 ▼
                                                        server/main.py  (ws://<laptop>:8080/ws)
                                                                 ▲
Arduino UNO Q + Modulino (role wand) ──────wand.imu──────────────┘
                                                                 │
                                              section.config / sched.notes / fx.expr
                                                                 ▼
                                                         phones (role section)
```

**Golden rule:** the CV app joins as `admin`, the Arduino joins as `wand`. They
must never both be a `wand*` role — wand roles share one slot (latest wins) and
would clobber each other.

---

## Phase 0 — One-time setup

From the repo root (`Phoneharmonic/`):

```sh
# Python server deps (uses the repo venv)
.venv/bin/python -c "import websockets, mido; print('deps ok')"
# If that fails:  .venv/bin/python -m pip install -r server/requirements.txt

# Node is only needed to run the CV unit tests
node --version
```

**Checkpoint:** `deps ok` prints, Node ≥ 18.

---

## Phase 1 — Automated tests (no hardware, no network)

Fast confidence check that the protocol logic and CV gesture logic are intact.

```sh
# Server-side: IMU parsing, telemetry, monitor thresholds, launcher arg checks
.venv/bin/python server/tools/stream_probe_test.py

# CV-side: finger poses -> modes, handedness, transport routing, pinch scrub
node --test cv_hand_movements/tests/cv.test.mjs
```

**Checkpoint:** both suites report all-pass (server suite: `OK`; CV suite:
`pass 7  fail 0`).

---

## Phase 2 — CV app → server, laptop-only (no board yet)

This proves the biggest new piece — the CV gestures reach the server — without
any hardware. **Do this before touching the Arduino.**

**Terminal 1 — server:**
```sh
.venv/bin/python server/main.py
```
Watch for `HTTP/ws  listening on :8080`. (A warning that `:8443` is disabled
without certs is fine — we only need plain `ws` for this.)

**Terminal 2 — serve the CV page on the laptop:**
```sh
python3 -m http.server 8765 --bind 127.0.0.1 --directory cv_hand_movements
```

**Browser:**
1. Open <http://127.0.0.1:8765/> and click **Tap to start camera** (allow webcam).
2. In the right-hand **System** card, the **Server** row should flip to
   **Connected** (green). If it says *Offline*, see Troubleshooting.
3. Raise your **physical left hand** and try each gesture. In the **Event Log**
   you should see the outgoing messages, each prefixed `↗`:

   | Gesture (left hand) | Event-log line | Server console line |
   |---|---|---|
   | ✋ Open palm | `↗ admin.cmd start` | `admin start` / roster update |
   | ✊ Closed fist | `↗ admin.cmd stop` | `admin stop` |
   | 🤏 Pinch, move L then release | `↗ admin.cmd rewind` | timeline jump |
   | 🤏 Pinch, move R then release | `↗ admin.cmd forward` | timeline jump |
   | ✌️ Two fingers | `↗ wand.mode det` | `wand mode -> det` |
   | 🤟 Three fingers | `↗ wand.mode ai` | `wand mode -> ai` |
   | ☝️ One finger (Select) | *(no `↗` line — local only)* | *(nothing — selection is the wand's aim)* |

**Optional — watch the server from a dashboard instead of the console:**
open the stage page at <http://localhost:8080/stage/?admin=1>. It shows the
roster (you'll see the CV app listed as `admin`) and the live mode.

**Checkpoint:** Server row = Connected, and 2-finger / 3-finger / palm / fist
each produce the matching `↗` line **and** a matching server console line. If
this works, the entire CV→server path is proven.

The server also logs the debounced recognizer state whenever it changes, for
example: `cv state client=… gesture=TWO_FINGERS mode=DETERMINISTIC confidence=96%`.
When the gesture is released it logs `gesture=NONE` while preserving the sticky
current mode.

> Tip: to point the CV page at a server on another machine (e.g. the laptop from
> a second computer), add `?ws=`:
> `http://127.0.0.1:8765/?ws=ws://172.20.10.3:8080/ws`

---

## Phase 3 — Network for the real demo (phone hotspot)

The Arduino talks to the server over Wi-Fi, so laptop + board + phones must share
one LAN. We use your **phone hotspot**.

1. Turn on your phone's Personal Hotspot.
2. Join the **laptop** to it.
3. Join the **UNO Q** to the same hotspot (board-side Wi-Fi provisioning).
4. Get the laptop's hotspot IP — this is your `--server-ip`:
   ```sh
   ipconfig getifaddr en0        # iPhone hotspot -> a 172.20.10.x address
   ```
   If that prints nothing, the laptop hasn't joined the hotspot yet (this exact
   symptom is what blocked the stream probe before).
5. Confirm the board is reachable:
   ```sh
   ssh arduino@<board-name>.local        # e.g. arduino@ArduinoUnoQ.local
   ```
   If `.local` fails on the hotspot, use the board's `172.20.10.x` IP instead.
6. Allow inbound connections on port 8080 (macOS: System Settings → Network →
   Firewall — allow `python`, or turn the firewall off for the demo).

**Checkpoint:** `ipconfig getifaddr en0` returns `172.20.10.x`, and
`ssh arduino@<board>.local` connects.

---

## Phase 4 — Arduino IMU uplink (the stream probe)

With the LAN up, run the one-command physical test. `--keep-running` leaves the
streamer alive afterward so it stays connected as the real wand.

```sh
./firmware/uno_q/stream_probe/run_probe.sh \
  --board arduino@<board>.local \
  --server-ip 172.20.10.x \
  --keep-running
```

It SSHes the app to the board, compiles/flashes the MCU sketch, starts the Linux
streamer, (re)starts the server, and runs a guided 30-second physical test:

1. **0–8 s:** hold the board flat and still.
2. **8–20 s:** rotate it clearly around its vertical (yaw) axis.
3. **20–30 s:** hold still again.

**Checkpoint:** every result row is `PASS`, ending with `hardware stream PASS`.
The wand now appears in the roster as `variant=hw`.

If it fails, match the symptom (README table in
`firmware/uno_q/stream_probe/README.md`):

| Symptom | Likely cause |
|---|---|
| No wand in roster | Wi-Fi / wrong `--server-ip` / SSH / handshake |
| Connects, zero frames | Modulino init or MCU↔Linux Bridge |
| Gravity ≈ 1 not 9.81 | Missing g→m/s² conversion |
| Yaw never moves | Gyro axis mapping |

Board logs:
```sh
ssh arduino@<board>.local \
  "arduino-app-cli app logs /home/arduino/ArduinoApps/phoneharmonic-stream-probe --all"
```

---

## Phase 5 — Full three-way integration

Now run the CV control and the physical wand **at the same time** and confirm
they coexist (the whole point of the `admin` vs `wand` role split).

1. Keep the server running (Phase 4 started it; or `.venv/bin/python server/main.py`).
2. Leave the wand streaming (`--keep-running` from Phase 4).
3. On the laptop, serve and open the CV page (Phase 2), pointing it at the
   hotspot IP if the server isn't same-origin:
   `http://127.0.0.1:8765/?ws=ws://172.20.10.x:8080/ws`
4. Open the stage dashboard: <http://localhost:8080/stage/?admin=1>. You should
   see **both** clients in the roster: the CV app as `admin` and the wand as
   `wand (hw)`.
5. (Optional) Join a phone as an orchestra section by opening
   `http://172.20.10.x:8080/section/?s=lol1` on a phone on the hotspot.

**Run the demo flow (from `docs/demo_flow.md`):**

| Step | Do this | Expect |
|---|---|---|
| Transport | ✋ palm / ✊ fist (left hand) | show plays / pauses |
| Aim | point the **wand** at a section | `wand.state.aim_section` tracks it on the stage page |
| Select | ☝️ one finger, point the wand | the aimed instrument highlights |
| Deterministic | ✌️ two fingers, tilt the wand | aimed section warps (`fx.expr`) |
| AI | 🤟 three fingers, swish the wand | AI-mode window opens |

**Checkpoint:** the CV app (`admin`) and the wand (`wand hw`) are both in the
roster simultaneously and neither drops the other. Mode changes come from the
webcam; motion/aim comes from the physical wand.

---

## Quick reference

| Thing | Command / URL |
|---|---|
| Start server | `.venv/bin/python server/main.py` |
| Serve CV page | `python3 -m http.server 8765 --bind 127.0.0.1 --directory cv_hand_movements` |
| CV page | <http://127.0.0.1:8765/> (add `?ws=ws://IP:8080/ws` to retarget) |
| Stage dashboard | <http://localhost:8080/stage/?admin=1> |
| Phone section join | `http://<laptop-ip>:8080/section/?s=lol1` |
| Laptop IP (hotspot) | `ipconfig getifaddr en0` |
| Run wand probe | `./firmware/uno_q/stream_probe/run_probe.sh --board arduino@<board>.local --server-ip <ip> --keep-running` |
| Interactive wand monitor | `.venv/bin/python server/tools/wand_monitor.py` (keys: `s` start, `x` stop, `d` det, `a` ai) |
| Server unit tests | `.venv/bin/python server/tools/stream_probe_test.py` |
| CV unit tests | `node --test cv_hand_movements/tests/cv.test.mjs` |

## Troubleshooting the CV → server link

| Symptom | Fix |
|---|---|
| **Server** row stays *Offline* | Is `server/main.py` running? Is the `?ws=` host/port right? Same machine → default `ws://localhost:8080/ws` works. |
| Connected, but no `↗` lines | Use your **physical left hand** (right hand is ignored). Watch the **Gesture Recognition** card to confirm the pose is detected. |
| `↗` lines appear but server does nothing | Confirm the app joined as `admin` (check the roster on the stage page), not a wand role. |
| Mode flips constantly | Modes are latched; a brief mis-detection can retrigger. Hold the pose clearly; the stabilizer needs a few stable frames. |
| Works offline, not on the hotspot | Firewall blocking :8080, or `?ws=` pointing at the wrong IP. Re-check `ipconfig getifaddr en0`. |
