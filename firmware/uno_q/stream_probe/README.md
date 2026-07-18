# Phoneharmonic UNO Q IMU Stream Probe

This isolated Arduino App proves the complete physical path from a Modulino
Movement to the real Phoneharmonic server:

```text
Movement -> UNO Q MCU -> Arduino Bridge -> UNO Q Linux -> WiFi -> server /ws
```

It deliberately excludes gesture classification, LEDs, buzzers, AI mode, and
phone selection. The only output is the production `wand.imu` stream.

## Hardware and prerequisites

- Connect the Modulino Movement to the UNO Q Qwiic connector.
- Provision the UNO Q and laptop onto the same WiFi network.
- Confirm `ssh arduino@<board-name>.local` works.
- Use a current UNO Q Linux image containing `arduino-app-cli`.
- Install the laptop server dependencies:

  ```bash
  python3 -m pip install -r server/requirements.txt
  ```

Find the laptop's WiFi IPv4 address with `ipconfig getifaddr en0` on macOS,
`hostname -I` on Linux, or `ipconfig` on Windows. Do not use `127.0.0.1`: that
would refer to the UNO Q itself when used by the board.

For an IPv6 macOS hotspot, first confirm that the board is reachable over IPv6:

```bash
ssh -6 arduino@ArduinoUnoQ.local
exit
```

Then list the Mac's assigned non-loopback, non-link-local IPv6 addresses and
their interfaces:

```bash
ifconfig | awk '
  /^[a-z0-9]/ { interface=$1; sub(/:$/, "", interface) }
  /inet6 / {
    address=tolower($2)
    if (address != "::1" && address !~ /^fe80:/) print interface, $2
  }
'
```

Choose the global or unique-local address on the interface shared with the UNO
Q. The hotspot interface may be `en0`, `en1`, or a bridge interface depending
on the Mac and Internet Sharing configuration. Do not use a `fe80::` address:
link-local addresses require receiver-specific interface scoping and are not
suitable for the generated board URL.

Close browser, simulator, and CV wand clients before testing. Phoneharmonic has
one active wand slot, and the most recently connected wand owns it.

## Run the test

From any working directory, run:

```bash
./firmware/uno_q/stream_probe/run_probe.sh \
  --board arduino@uno-q.local \
  --server-ip 192.168.1.42
```

For IPv6, pass the bare address in quotes:

```bash
LAPTOP_IPV6='2605:8d80:440:7d4c::10'
./firmware/uno_q/stream_probe/run_probe.sh \
  --board arduino@ArduinoUnoQ.local \
  --server-ip "$LAPTOP_IPV6"
```

Do not add square brackets to `--server-ip`. The launcher validates and
canonicalizes the address, then adds the RFC-required brackets when it creates
the URL: `ws://[2605:8d80:440:7d4c::10]:8080/ws`.

The launcher copies only this isolated App to
`/home/arduino/ArduinoApps/phoneharmonic-stream-probe`, compiles and flashes its
MCU sketch, starts its Linux process, starts or reuses the real server, and runs
the guided monitor. It never stores WiFi credentials or a generated IP address
in the repository.

Use `--dry-run` to validate arguments without contacting the board. Use
`--keep-running` to leave the minimal streamer running after a successful test
for integration with the rest of the application. Without that flag, the
launcher stops the probe App when it exits.

## Physical phases

The default test lasts 30 seconds:

1. **0–8 seconds:** hold the board flat and completely still.
2. **8–20 seconds:** rotate it clearly around its vertical/yaw axis.
3. **20–30 seconds:** stop and hold it still again.

PASS requires a connected hardware wand, 45–70 sensor frames/s, 8–15 batches/s,
no invalid frames or sequence gaps, no receive pause over one second, gravity
near 9.81 m/s², low gyro activity while still, and obvious yaw movement during
the middle phase.

## Expected successful run

The launcher prints these milestones in order. Advancing from the access check
to deployment confirms SSH and `arduino-app-cli`; an SSH error stops the run.

```text
[probe] checking UNO Q access: arduino@ArduinoUnoQ.local
[probe] deploying isolated app to arduino@ArduinoUnoQ.local:...
[probe] compiling, flashing, and starting the UNO Q app
```

It then either reuses a compatible server or starts one. For IPv6, every shown
WebSocket URL should contain brackets:

```text
[probe] starting Phoneharmonic server at ws://[2605:8d80:440:7d4c::10]:8080/ws
[probe] starting guided 30s physical test
[probe] connecting to ws://[2605:8d80:440:7d4c::10]:8080/ws
[probe] admin connected as 1a2b3c4d
```

The first `HOLD STILL` prompt appears only after the server roster reports a
connected `variant=hw` wand. Seeing all three prompts confirms that deployment,
the board WebSocket handshake, and the physical capture are in progress:

```text
[probe   0.0s] HOLD STILL: place the board flat and do not move it
[probe   8.0s] MOVE: rotate the board clearly around its vertical/yaw axis
[probe  20.0s] HOLD STILL AGAIN: stop moving the board
```

A successful physical test ends with every result row marked `PASS`, followed
by:

```text
[probe] PASS
[probe] hardware stream PASS
```

Without `--keep-running`, the final cleanup message confirms that the isolated
board App was stopped. If the server cannot accept the IPv6 connection, verify
that the Mac firewall permits inbound TCP on the selected port (default 8080)
and that the chosen address belongs to the hotspot interface.

## Troubleshooting

| Symptom | Likely boundary |
|---|---|
| No hardware wand in the roster | Deployment, WiFi, server URL, or handshake |
| Wand connects but receives zero frames | Modulino initialization or MCU/Linux Bridge |
| Gravity is near `1` instead of `9.81` | Missing g-to-m/s² conversion |
| Invalid frames | CSV parsing, non-finite sensor output, or serialization |
| Low rate or long pauses | MCU scheduling, Bridge queue pressure, or WiFi |
| Frames arrive but yaw never moves | Gyro axis mapping or physical sensor reading |
| Server reports sequence gaps | Linux batching, reconnects, or WiFi loss |

Board-side logs are available after deployment with:

```bash
ssh arduino@uno-q.local \
  "arduino-app-cli app logs /home/arduino/ArduinoApps/phoneharmonic-stream-probe --all"
```

## Useful options

```text
--server-port PORT   default 8080
--session NAME       default lol1
--duration SECONDS   default 30
--keep-running       preserve the running board App after PASS
--dry-run            show resolved settings without changing anything
```

The Arduino App structure and dependencies are declared in `app.yaml` and
`sketch/sketch.yaml`; no App Lab GUI action is required.
