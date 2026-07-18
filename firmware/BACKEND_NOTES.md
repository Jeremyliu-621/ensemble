# Firmware ↔ Backend: what the server needs from the wand

Hey — I went through the whole `server/` side to figure out exactly what the
firmware has to produce and how it talks to the backend. Here's the rundown so
you don't have to reverse-engineer the Python yourself.

## TL;DR

The board has **one real job**: stream raw IMU continuously as JSON over a
WebSocket. That single stream drives *everything* the server does with the
wand — aiming, gesture capture, and continuous expression control. If you get
`wand.imu` right, you're basically done. Everything else is optional and mostly
depends on hardware we don't currently have (MPR121 touch pads, ToF distance).

Our current hardware: **Arduino UNO Q + Modulino Movement (the IMU) only.**

---

## Current state of the backend (what already exists)

All of this is written and working — none of it needs to change for us to bring
up the wand:

- **`server/main.py`** — the WebSocket server + message dispatch. It listens on
  `:8080` (plain ws) and `:8443` (wss). Lines 325–360 are the wand message
  router.
- **`server/wandio.py`** — the two pieces that consume our IMU stream:
  - `WandRouter` — buffers IMU frames between a grab-start and grab-end and
    hands the engine one complete "gesture window."
  - `WandAimer` — integrates the gyro yaw axis into a pointing direction and
    figures out which phone/section we're aiming at.
- **`server/protocol.py`** — the source of truth for every message type and its
  payload shape. If you want the canonical field list, read this file.
- **`server/tools/wand_bridge.py`** — a ready-to-run laptop-side bridge that
  reads JSON-lines off a serial port and forwards them to the server. We use
  this for bench bring-up so the firmware doesn't need a WiFi stack on day one.
- **`web/wandsim/wandsim.js`** — a browser "fake wand" that speaks the exact
  same wire protocol. This is the **byte-for-byte reference implementation** —
  when in doubt about a message shape, copy what this file sends.

So the backend is done. What's missing is entirely on our side: the firmware,
and the link from the board to the laptop.

---

## The one message that matters: `wand.imu`

Three different server features all read the *same* IMU frame:

1. **Aiming** — `WandAimer` (wandio.py:92) integrates `gz` (yaw rate) over time
   into a heading, then locks onto whichever section's stage azimuth is within
   40°. That's how pointing the wand selects a phone.
2. **Gestures** — while a "grab" is active, `WandRouter` (wandio.py:37) collects
   these frames into a window and feeds them to the music engine, which decides
   the next accompaniment line.
3. **Deterministic-mode expression** — `main.py:490` reads `ay` (the lift axis)
   and maps how high you raise the wand to a scale-locked pitch + volume swell
   on the aimed phone.

All three read one frame type. Get it right and all three light up.

### Exact shape

```json
{"t": "wand.imu", "seq": 12, "frames": [[tw, ax, ay, az, gx, gy, gz], ...]}
```

- `seq` — a monotonically increasing batch counter (int). Nice for debugging
  dropped frames.
- `frames` — a batch of ~5 sensor rows. Each row is 7 numbers.

### Units and axes — this is where it's easy to mess up

The server *interprets* these numbers; the wand only reports. So the conventions
below are non-negotiable:

| Field | Meaning | How to produce it from Modulino Movement |
|---|---|---|
| `tw`  | wand-local **monotonic ms** | `millis()`. Must be a real clock, not a frame counter — the aimer computes `dt` between frames and only trusts `0 < dt < 0.5s` (wandio.py:101). |
| `ax`,`ay`,`az` | accelerometer, **m/s², gravity INCLUDED** | `getX/Y/Z()` returns **g** — multiply by **9.81**. Don't subtract gravity. `ay` is the vertical/lift axis when held level. |
| `gx`,`gy`,`gz` | gyro, **deg/s, RAW** | `getRoll/Pitch/Yaw()` are already angular velocities in deg/s. **Send them raw — do NOT integrate or sensor-fuse on the board.** The server integrates `gz` itself. |

Two gotchas worth repeating because they'll silently break things:

- **Accel must include gravity and be in m/s².** Det-mode does `row[2] / 9.8` to
  get a tilt in the -1..1 range (main.py:499). If you send raw g, expression
  will be ~10x off.
- **Do not integrate the gyro.** People instinctively want to turn gyro into an
  angle on-device. Don't — the server does that. Send the raw rate.

### Timing

- Sample the IMU at **50–60 Hz**.
- Batch **~5 frames per message** (keeps the message rate ~10–12 Hz).
- **Stream continuously from boot** — not just during grabs. Aiming needs the
  stream at all times. (Phone wands can only stream during a grab; our hardware
  can do better, so it should.)

---

## The handshake (do this once before streaming)

First frame on every connect *and* every reconnect:

```json
{"t": "hello", "v": 1, "role": "wand", "session": "lol1", "client_id": null}
```

- `role: "wand"` registers us as the hardware wand (variant `hw` — protocol.py:48).
- The server replies with a `welcome` message containing a `client_id`.
  **Store that id and send it back in `client_id` on reconnect** so the server
  treats us as the same wand instead of a new one.
- Only one wand slot exists; the most recent wand to connect owns it.

Note: if we go through the serial bridge for bring-up (below), the bridge does
this handshake for us — the MCU just prints IMU lines.

---

## Best practices: how to get data from the UNO Q to the server

Short answer: **WiFi is the recommended live transport, not Bluetooth.** But you
start on **USB serial** for bring-up because it's the least code and lets you
prove the sensor math first. Bluetooth is a fallback we may not even be able to
use. Here's the full picture and what each one looks like in code.

### The key fact about the UNO Q: it's two computers in one

The UNO Q is a **dual-brain board** — a real-time microcontroller (MCU, runs
your `.ino` sketch) *plus* a full Qualcomm Linux SoC on the same board. That
matters a lot for how we send data:

- The **MCU** is what talks to the Modulino over I²C. It's great at sampling a
  sensor at a steady 60 Hz, but it can't run a WiFi/WebSocket stack.
- The **Linux side** can run Python, hold a WebSocket, join WiFi — everything a
  phone can do. It talks to the MCU over an on-board channel called **Bridge**.

So the good architecture is: **MCU samples the sensor → hands frames to Linux
over Bridge → Linux batches them into JSON and holds the WebSocket to the
laptop.** No external bridge process, no Bluetooth pairing — the wand just joins
the same WiFi as every phone in the room.

### Why WiFi beats Bluetooth here (the verdict)

1. **Zero new server code / architectural consistency.** Every other device in
   the room — phones, the CV tab — already talks to the laptop over `ws://`.
   A WiFi wand is just one more WebSocket client on infrastructure that's
   already built and tested. Bluetooth needs a separate bridge process
   (`wand_bridge.py`) or an on-board pairing dance.
2. **Bluetooth Classic (SPP) support is unconfirmed on this board.** The UNO Q's
   radio (WCBN3536A / Qualcomm WCN3980) does WiFi 5 + Bluetooth 5.1, but the
   docs *don't* confirm classic SPP/RFCOMM — and `wand_bridge.py`'s pyserial
   approach specifically needs classic SPP, not BLE. BT 5.1 chips are often
   BLE-only. This is a real go/no-go risk, not a settled fact. **Test it before
   trusting it.**
3. **Latency is fine on the LAN.** Small JSON frames over local WiFi run
   single-digit-to-low-double-digit ms, and this system already tolerates way
   more slack than that (150–600 ms scheduling lookahead by design).
4. **Power doesn't matter here.** BLE's one real edge is power efficiency, but
   the wand is power-bank-fed and sits near the laptop — so that advantage is
   moot.

The one WiFi risk to watch: **contention** — a room full of phones + the wand +
the CV tab all on one hotspot. Load-test with several phones joined before
trusting it live. (Full write-up: `docs/hardware-integration-plan.md` §3.)

---

### Option 1 — USB serial + the existing bridge  → **do this FIRST (bring-up)**

The simplest possible thing. The MCU sketch prints **one JSON object per line**
over USB serial. No WiFi, no handshake, no WebSocket on the board at all. The
laptop runs the bridge we already have:

```
python server/tools/wand_bridge.py --port /dev/tty.<device> --baud 115200
```

That bridge (`server/tools/wand_bridge.py`) opens the serial port, does the
`hello`/`welcome` handshake as role `wand`, validates each line is an allowed
message type, and forwards it verbatim to `ws://127.0.0.1:8080/ws`. It
reconnects forever and drops malformed lines with a note.

**MCU sketch shape for this path** (pseudo-Arduino):

```cpp
#include <Arduino_Modulino.h>
ModulinoMovement imu;

void setup() {
  Serial.begin(115200);
  Wire1.begin();              // Qwiic connector is Wire1 on the UNO Q, not Wire
  Modulino.begin(Wire1);
  imu.begin();               // Modulino Movement @ 0x6A
}

unsigned long lastSeq = 0;

void loop() {
  // sample at ~60 Hz, batch 5 frames, then print one JSON line
  static float batch[5][7];
  for (int i = 0; i < 5; i++) {
    imu.update();
    unsigned long tw = millis();
    batch[i][0] = tw;
    batch[i][1] = imu.getX() * 9.81;   // g -> m/s², KEEP gravity
    batch[i][2] = imu.getY() * 9.81;
    batch[i][3] = imu.getZ() * 9.81;
    batch[i][4] = imu.getRoll();       // deg/s, RAW — do not integrate
    batch[i][5] = imu.getPitch();
    batch[i][6] = imu.getYaw();
    delay(16);                          // ~60 Hz
  }
  // print: {"t":"wand.imu","seq":N,"frames":[[tw,ax,ay,az,gx,gy,gz],...]}
  Serial.print("{\"t\":\"wand.imu\",\"seq\":");
  Serial.print(++lastSeq);
  Serial.print(",\"frames\":[");
  for (int i = 0; i < 5; i++) {
    Serial.print("[");
    for (int j = 0; j < 7; j++) { Serial.print(batch[i][j]); if (j < 6) Serial.print(","); }
    Serial.print(i < 4 ? "]," : "]");
  }
  Serial.println("]}");
}
```

Success looks like: server log prints `wand connected (variant=hw)` and a stream
of `wand.imu` frames as you wave the board around. **Prove the sensor math here
before touching WiFi** — it's much easier to debug over a serial monitor.

---

### Option 2 — WiFi from the board's Linux side  → **the live/untethered build**

Once serial works, move the networking onto the board. Split the work across the
two brains:

- **MCU sketch:** same sampling loop as above, but instead of `Serial.print`,
  push each frame to Linux with `Bridge.notify("imu", ...)`.
- **Linux-side Python script** (runs on the UNO Q itself, via Arduino App Lab /
  `arduino.app_utils`): receives the MCU's Bridge events with `Bridge.provide`,
  batches ~5 into the `wand.imu` JSON, and holds a WebSocket client straight to
  the laptop. This is essentially `wand_bridge.py`'s forwarding logic, just
  running on-board instead of on the laptop — copy its handshake verbatim.

**Linux-side glue shape** (pseudo-Python, mirrors `wand_bridge.py:37-67`):

```python
import asyncio, json
from arduino.app_utils import Bridge
from websockets.asyncio.client import connect

URL = "ws://<laptop-lan-ip>:8080/ws"   # same address the phones use

async def run():
    client_id = None
    buf = []
    async with connect(URL) as ws:
        # handshake — identical to wand_bridge.py
        await ws.send(json.dumps({"t": "hello", "v": 1, "role": "wand",
                                  "session": "lol1", "client_id": client_id}))
        welcome = json.loads(await ws.recv())
        client_id = welcome.get("client_id", client_id)

        seq = 0
        # MCU pushes one [tw,ax,ay,az,gx,gy,gz] row per Bridge.notify("imu", row)
        def on_imu(row):
            buf.append(row)
        Bridge.provide("imu", on_imu)

        while True:
            if len(buf) >= 5:
                frames, buf[:] = buf[:5], buf[5:]
                seq += 1
                await ws.send(json.dumps({"t": "wand.imu", "seq": seq, "frames": frames}))
            await asyncio.sleep(0.005)
```

(If the `Bridge` API fights you, the documented fallback is CSV-over-serial
between the MCU and Linux + a `pyserial` reader on Linux — same JSON out the
WebSocket either way.)

Now the wand joins over WiFi exactly like a phone: **no laptop-side bridge
process at all.** Swap over from Option 1 once it's proven.

---

### Option 3 — Bluetooth Serial  → **fallback ONLY, and gated on a test**

`docs/hardware-wand.md` originally called this "the new plan," but per the radio
research it's now the fallback. If you go this way, the MCU pairs with the laptop
over Bluetooth Serial and prints the same JSON-per-line as Option 1 — and the
laptop runs `wand_bridge.py` pointed at the Bluetooth serial device instead of
USB. Firmware never needs a WiFi stack.

**Before spending any time here, run the go/no-go test:** confirm the WCBN3536A
actually exposes classic SPP/RFCOMM via BlueZ (`bluetoothctl`, `rfcomm bind`).
If it's BLE-only, `wand_bridge.py`'s pyserial approach won't work unmodified and
you'd need a BLE-UART bridge instead. Only revisit this if WiFi proves flaky in
the venue.

---

### Which to use, in one line each

- **Bench / bring-up today:** Option 1 (serial + `wand_bridge.py`). Least code,
  easiest to debug.
- **The actual demo:** Option 2 (WiFi from the Linux side). Untethered, no bridge
  process, joins like a phone.
- **Only if WiFi dies in the venue:** Option 3 (Bluetooth) — and only after the
  SPP go/no-go test passes.

---

## What we DON'T need to build (given our current hardware)

Our BOM is UNO Q + Modulino Movement only. These messages exist in the protocol
but we can skip them:

| Message | Why we skip it |
|---|---|
| `wand.grab` (start/end) | Grab windowing now comes from the **CV client** (webcam watching the off-hand), not a wired touch pad. And in det-mode the server ignores grabs entirely (main.py:334). Not the board's job anymore. |
| `wand.mode` (ai/det)     | Also moved to the CV client. Optional from firmware. |
| `wand.touch` (pads 0–11) | Needs the MPR121 touch board — we don't have it. If we ever add buttons, pads 6+ are free for firmware-defined modes. |
| `wand.range` (mm)        | Needs the ToF distance sensor — we don't have it. This is what would drive the proximity "tension" wash-out effect. |
| `wand.recal`             | Zeroes the aiming yaw drift ("this way is forward"). Nice-to-have — wire it to a button if we add one; a restart also resets it. |
| `wand.feedback` (±1)     | Thumbs-up/down to weight the last musical decision in training data. Optional, skip for the minimal build. |

---

## So, concretely, what to build

1. **MCU sketch** (`firmware/uno_q/wand/sketch/sketch.ino`):
   - Init Modulino Movement on **`Wire1`** (the Qwiic connector is Wire1 on the
     UNO Q, not Wire), address `0x6A`.
   - Sample at ~60 Hz. Convert accel g → m/s² (×9.81, keep gravity). Read gyro
     as raw deg/s.
   - Batch ~5 frames and either **print the `wand.imu` JSON line over serial**
     (Option 1) or `Bridge.notify` them to Linux (Option 2).
   - No state machine, no grab logic, no buttons. Pure sensor streamer.
   - Depends on the Arduino `Modulino` library — double-check the method names
     (`getX/Y/Z`, `getRoll/Pitch/Yaw`, `.begin()`, `.update()`) against the
     installed version once it's on real hardware.

2. **(Option 2 only) On-board Linux glue script** — batches Bridge events into
   the `wand.imu` JSON, does the `hello` handshake, holds the WebSocket to the
   laptop.

3. **Verify** using `server/tools/smoke_test.py` or just run the server and
   watch its log — every gesture prints its feature vector and every aim change
   is logged.

Start with Path 1. A board that only streams correct IMU over serial is a fully
functional wand.
