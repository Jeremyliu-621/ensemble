# Hardware wand — the exact firmware contract

Everything the physical wand (UNO Q or ESP32) must speak. Two transport
options, same JSON messages either way:

- **WebSocket** (WiFi): connect to `ws://<server-lan-ip>:8080/ws`, JSON text
  frames. `web/wandsim/wandsim.js` is the byte-for-byte reference.
- **Bluetooth Serial** (the new plan): pair with the laptop and print ONE
  JSON message PER LINE over serial — no hello, no WiFi stack, nothing else.
  The laptop runs the bridge, which handles the handshake and forwarding:
  `python server/tools/wand_bridge.py --port /dev/tty.<device> --baud 115200`.

## Handshake

First frame on connect (and on every reconnect):

```json
{"t": "hello", "v": 1, "role": "wand", "session": "lol1", "client_id": null}
```

Server replies `{"t": "welcome", "client_id": "...", ...}`. Reuse the returned
`client_id` on reconnects (store it in flash/RAM) so the server treats you as
the same wand. Latest wand to connect owns the single wand slot.

## Messages the wand sends

| Message | Payload | Rate | Effect |
|---|---|---|---|
| `wand.imu` | `{"seq": n, "frames": [[tw, ax, ay, az, gx, gy, gz], ...]}` | batches of ~5 at 50-60 Hz sampling | Gestures (during a grab) + aiming (always) |
| `wand.grab` | `{"state": "start"\|"end", "tw": ms}` | on MPR121 grab-pad edge | Frames between start/end become one gesture window |
| `wand.touch` | `{"pad": 0-11, "state": "down"\|"up"}` | on pad edge | Pads 0-5 force a candidate while held (0 lower_imitation, 1 contrary_motion, 2 sustained, 3 delayed, 4 rhythmic_dense, 5 rest); `up` returns to auto. Pads 6+ are yours for firmware-side modes |
| `wand.range` | `{"mm": 234.0}` | ~10 Hz while valid | Proximity tension: 600mm+ = open, 100mm = full wash-out (fx.tension to every phone) |
| `wand.recal` | `{"tw": ms}` | on a button press | Zeroes the aiming yaw ("this way is forward") |
| `wand.mode` | `{"mode": "ai"\|"det", "param": "pitch"\|"volume"\|"filter"?}` | on the physical toggle (cycle: ai → det:pitch → det:volume → det:filter → ai) | **ai**: grabs become gesture windows the AI composes from. **det**: pure coordinate control — the wand's tilt (no motion needed) streams the selected parameter to the aimed phone: pitch = scale-locked degrees, volume = gain sweep, filter = the room tension filter. Grabs are ignored in det |
| `wand.feedback` | `{"value": 1\|-1}` | on thumbs pads | Weights the last musical decision in the training data |
| `wand.gesture` | `{"label": "sharp_up"\|"sharp_down"\|"swish"\|"twist"\|"still"\|"flick", "strength"?: 0.2-1.5}` | OPTIONAL: if you run TinyML on the MCU | A pre-classified motion; the server maps it to the same intent features the raw path extracts. Use this *instead of* grab+imu gestures if on-wand classification is your thing — but keep streaming `wand.imu` regardless (aiming and det-mode need it) |

Units and axis convention (MUST match — the server interprets, the wand only
reports): `tw` = wand-local monotonic ms; `ax, ay, az` = accelerometer m/s²
**including gravity** (Modulino Movement `getX/Y/Z()` returns g — multiply by
9.81); `gx, gy, gz` = gyro deg/s (`getRoll/Pitch/Yaw()` on the Modulino are
angular velocities — send them raw, do NOT integrate or fuse); `ay` is the
"lift" axis (vertical when the wand is held level); `gz` is the yaw axis used
for aiming. Send `wand.imu` continuously (not just during grabs) — that's
what drives aiming; phones can't, hardware can.

## What aiming does

The server integrates `gz` into a pointing direction and locks onto the
section whose stage azimuth is within 40°. The aimed phone carries the
accompaniment line solo, and the stage glows that performer. Sections are
auto-spread across -60°..+60° if never explicitly placed (`stage.place` from
the editor overrides). `wand.recal` re-zeroes drift — put it on a pad.

## UNO Q specifics (gotchas already verified)

- Qwiic connector = the MCU's **`Wire1`**, not `Wire`. Chain: Modulino
  Movement (0x6A) + Modulino Distance (0x29) + Adafruit MPR121 STEMMA QT
  (0x5A) — no address conflicts. `mpr121.begin(0x5A, &Wire1)`.
- MCU sketch polls sensors, `Bridge.notify("imu", ...)` to the Linux side;
  Python (`arduino.app_utils`, `Bridge.provide`) batches into the JSON above
  and holds the WebSocket over WiFi. If Bridge fights you, fall back to CSV
  over serial + a `pyserial` reader — same JSON out.
- Power bank: draw is so low (0.5-3.5W) that many banks auto-shut-off; test
  yours or use one with a trickle mode. Cheap cables cause brownouts.
- ESP32 fallback: MPU6050 raw accel/gyro into the same frames; ArduinoWebsockets
  + `WiFi.setSleep(false)`; grab/touch from any GPIO buttons.

## Bench test without a server

`python server/tools/smoke_test.py` exercises the full path; or run the
server and watch its log — every gesture prints its feature vector, every
`wand.touch`/`wand.range`/aim change is logged and appears on the stage.
