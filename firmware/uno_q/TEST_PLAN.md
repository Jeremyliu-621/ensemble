# Wand Bring-Up — Semantic Test Plan

Each test proves one behavior end-to-end. Run them in order: a failure localizes
the break to the layer that test covers. Tests T0.* need no board; T1+ need the
UNO Q.

**Prereqs on the laptop:**
```bash
pip install "websockets>=16,<17" mido      # server deps
python server/main.py                       # note the LAN IP it prints on startup
```
Keep `server/tools/wand_monitor.py --ip <laptop-lan-ip>` open in a second
terminal for every hardware test — it's the observation window for both directions.

---

## Layer 0 — Automated (no board) — regression before you touch hardware

### T0.1 — Server emits `wand.cmd` on state change
**Proves:** the laptop→wand downlink fires on connect, transport, and mode.
**How:** run the server-downlink e2e (spins up the real server + a fake wand/admin in-process).
**Pass:** 4/4 PASS — snapshot on connect, `start`→`playing:true`, `stop`→`playing:false`, `det`→`mode:'det'`.

### T0.2 — Board encoding round-trips
**Proves:** the CSV contracts wiring MCU↔Linux↔server (IMU-CSV parse, `wand.cmd`→state→MCU-CSV).
**How:** the board-logic unit test (`_parse_imu_csv`, `WandState`).
**Pass:** IMU CSV → 7 floats; `wand.cmd{playing,mode,aim}` → `"1,det,s2"`; bad input → dropped.

### T0.3 — Monitor observes uplink
**Proves:** an admin observer sees the wand connect and yaw move — the readout you'll rely on for T3.
**How:** the monitor observe-path e2e (fake wand streams rotating gz).
**Pass:** roster shows `wand connected variant=hw`; `wand.state` yaw values change.

> These three are already green. Re-run them after any server/protocol edit.

---

## Layer 1 — MCU sensor (board)

### T1.1 — IMU sampling & units are correct
**Proves:** the Modulino reads, and accel is m/s² **with gravity**, gyro is raw deg/s — the #1 thing that silently breaks everything downstream.
**How:** temporarily enable the serial debug print in `sketch/sketch.ino` (or read the values via T3). Hold the board flat and still.
**Pass:** at rest, the gravity axis reads **≈9.8** (not ≈1.0 → you forgot ×9.81; not ≈0 → you subtracted gravity), the other two accel axes ≈0, all three gyro axes **≈0**. Rotating the board makes the matching gyro axis spike to tens/hundreds deg/s and return to ~0 when still.

---

## Layer 2 — Uplink: board → server

### T2.1 — Wand connects over WiFi
**Proves:** handshake works; the board owns the wand slot.
**How:** power the board, start `python/main.py`, watch the server log and the monitor.
**Pass:** server logs `wand connected (variant=hw)`; monitor prints `roster: wand connected=True variant=hw`.

### T2.2 — Accelerometer/IMU data is streaming to the server
**Proves:** the full MCU→Bridge→Linux→WebSocket→server path carries live IMU.
**How:** wave/rotate the board while watching the monitor.
**Pass:** `wand.state` lines stream and **`yaw` changes** as you rotate about the vertical axis (yaw integrates gz). Holding still → yaw stops moving. (No board activity → no yaw change → break is uplink.)

### T2.3 — Stream is continuous and at rate
**Proves:** the board streams from boot at ~10–12 msg/s, not just in bursts.
**How:** hold the board still and watch the monitor / server for steady `wand.state` updates (throttled to ~150 ms) and no long gaps.
**Pass:** updates arrive continuously without multi-second stalls; reconnect isn't happening in a loop.

### T2.4 — Aiming / phone selection
**Proves:** pointing the wand selects a phone (server integrates gz→azimuth, ±40° lock).
**How:** join ≥2 phones (`section` role) arranged left→right, mark them ready; point the wand at each in turn.
**Pass:** monitor `wand.state.aim_section` switches to the phone you're pointing at and holds; sweeping past a phone locks it within 40°. Press `d` (det) and lift the wand at a phone → only that phone's expression warps (fx.expr).

---

## Layer 3 — Downlink: laptop → board  (the new `wand.cmd` channel)

### T3.1 — Transport state reaches the board ("pause music")
**Proves:** laptop transport → `wand.cmd{playing}` → board LED.
**How:** in the monitor press `s` (start) then `x` (stop).
**Pass:** on `s` the board's **play LED goes solid**; on `x` it **blinks** (~2 Hz). Monitor echoes `-> {cmd:start/stop}` and the roster flips `playing`.

### T3.2 — Mode state reaches the board (ai ↔ det)
**Proves:** `wand.cmd{mode}` → board mode LED.
**How:** in the monitor press `d` (det) then `a` (ai).
**Pass:** board **mode LED on** for det, **off** for ai. (This is the same signal the CV fist-gesture will drive.)

### T3.3 — Selected phone reaches the board
**Proves:** `wand.cmd{aim}` → board aim feedback.
**How:** point the wand at different phones (needs T2.4 working).
**Pass:** the board's aim buzzer/LED blips/updates each time the selected phone changes.

### T3.4 — Board syncs state on connect (initial snapshot)
**Proves:** a board joining mid-show immediately learns the current state.
**How:** press `s` (show playing), then power-cycle or restart `python/main.py` on the board.
**Pass:** within a second of reconnecting, the board's play LED shows **solid** (playing) without you pressing anything — the server pushed a `wand.cmd` snapshot right after `welcome`.

### T3.5 — Reconnect resilience
**Proves:** a WiFi blip self-heals and re-syncs, reusing the same wand identity.
**How:** briefly drop the board's WiFi (or kill/restart the server), then restore.
**Pass:** the board reconnects on its own (echoes the cached `client_id`), the monitor shows the wand reconnect, and board LEDs re-reflect current state. No manual restart needed.

---

## Layer 4 — Full loop (with the CV gesture path)

### T4.1 — CV palm-pause moves the board
**Proves:** the real intended flow: laptop CV gesture → server → board.
**How:** open `web/cvwand/` on the laptop, hold an open palm ~0.6 s to toggle the show.
**Pass:** the show pauses/resumes AND the board's play LED reacts (solid↔blink) — no monitor keypress involved. This is "CV gesture from the laptop sets the state of the Arduino" working end-to-end.

> Note: the CV wand joins as role `wand-cv` and the hardware wand as `wand` — they
> contend for the single wand slot (latest wins). For T4.1, connect the CV wand
> for the gesture and keep the hardware wand connected as the *observer of state*;
> if aiming/IMU fights the CV grab, test T4.1 with the CV wand as the only active
> input and the board reacting to `wand.cmd` only.

---

## Quick failure map
| Symptom | Likely layer |
|---|---|
| No `wand connected` in server log | T2.1 — WiFi/handshake/LAN IP wrong |
| Connects, but yaw never moves | T2.2 — MCU not sampling / Bridge "imu" not flowing |
| accel ≈1.0 not 9.8, or expression 10× off | T1.1 — forgot ×9.81 |
| aim never resolves | T2.4 — no ready phones / not placed L-R |
| Keys echo `->` but board LED dead | T3.x — Bridge "cmd" downlink to MCU, or LED wiring |
| Board LED works from monitor but not CV | T4.1 — wand-slot contention (see note) |
