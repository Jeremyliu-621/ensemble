# Wand Maestro

A custom wand conducts a distributed orchestra: phones scan a QR, join as
orchestra sections, and each plays its own **clock-synced** audio. A symbolic
engine proposes accompaniment candidates per bar and a ranker picks one from the
wand's gesture. See `../.claude/plans/virtual-dancing-adleman.md` for the full
plan and `RESEARCH.md` for the verified gesture-recognition research.

**Build status:** P0 (scaffold), P1 server (clock-synced playback), three wand
inputs (webcam hand / phone / physical UNO Q), a music-engine slice where
**gestures audibly reshape the accompaniment**, **per-section instruments with
distinct timbres**, a visual **[how-it-works guide](web/guide/)**, and a manual
**[editor / control room](web/editor/)** (transport, tempo, force-a-candidate,
assign instruments, **drop a MIDI file** to replace the song), and **MIDI song
loading** (`mido` parses a dropped `.mid` into tracks/instruments, picks the
melody, estimates key + per-bar chords), and **two trainable models** (a
decision policy behind `WM_MODEL_URL` picking the accompaniment from the
gesture, and a bar-line generator behind `WM_BARMODEL_URL` writing fresh
lines as the "generated" candidate — both with instant rule-based fallbacks,
a local mock endpoint (`server/tools/mock_model.py`), and dataset builders —
see [`docs/ai-training.md`](docs/ai-training.md)) — all built and headless-tested
(`server/tools/`: `smoke_test.py`, `gesture_test.py`, `midi_test.py`). Open gate: the two-phone mic skew test
(§ P1 verification). Research on how others sync audience audio (and the "Waterloo"
reference) is in [`docs/audio-sync-research.md`](docs/audio-sync-research.md) — TL;DR
we match IRCAM's Soundworks pattern; the one gap is clock-drift compensation.

**Pages:** `/` → `/home/` (landing menu), `/stagepix/` (pixel-art orchestra +
QR), `/editor/` (control room — live stage in the centre + transport, tempo,
MIDI drop, piano-roll, instrument assignment, gesture recorder), `/guide/` (how
it works), `/cvwand/` (webcam wand), `/wandsim/` (phone wand, HTTPS),
`/section/?s=lol1` (a phone as an instrument). All pages share one
crimson+gold+serif design language. MIDI songs play their full arrangement
across phones **including drums** (percussion synth); gestures can be recorded to
`data/gestures/` for training a DTW/Jackknife classifier.

## Known-good setup: laptop + UNO Q wand + phones

Follow this order whenever the network changes. The server puts the laptop
address it detects at startup into the QR code and into the UNO Q connection
information. Starting the server before joining the final network is the most
common cause of a stale, unreachable QR code.

### 0. One-time laptop setup

From the repository root on macOS/Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
```

The commands below use `.venv/bin/python` directly. Activating the virtual
environment is optional.

Connect the Modulino Movement to the UNO Q Qwiic connector. Connect the
Modulino Distance too when using the full production wand; the isolated stream
probe only tests the Movement/IMU path.

### 1. Provision the UNO Q in Arduino App Lab

Arduino App Lab is required for the UNO Q's first setup and remains useful for
viewing board logs. Arduino's official [UNO Q setup documentation](https://docs.arduino.cc/resources/datasheets/ABX00162-ABX00173-datasheet.pdf)
describes the same first-time sequence.

1. Install and open [Arduino App Lab](https://docs.arduino.cc/software/app-lab/).
2. Connect the UNO Q to the laptop with a **USB-C data cable**. USB is required
   for first-time PC-hosted setup; it may remain connected for power afterward.
3. Install any prompted UNO Q updates, then restart App Lab if requested.
4. Give the board a device name and password. The name becomes its network
   hostname, for example `jerm.local`; the SSH login is normally
   `arduino@jerm.local`.
5. In App Lab's network setup, enter the credentials for the **same final Wi-Fi
   or phone hotspot** that the laptop and phones will use.
6. Wait for the board to appear as a Network target. Verify it from Terminal:

   ```bash
   ssh arduino@<board-name>.local
   ```

   For an IPv6-only hotspot, use:

   ```bash
   ssh -6 arduino@<board-name>.local
   ```

7. Type `exit` to close SSH. If `.local` lookup fails, use the board's numeric
   address shown by App Lab instead.

To run the full production app through the GUI on an ordinary IPv4 Wi-Fi, open
`firmware/uno_q/wand/` as an App and click **Run** in the upper-right corner.
App Lab builds the Linux component, flashes the MCU sketch, starts the app, and
shows both sides' logs. For repeatable demos, prefer the deployment command in
the IPv4 instructions below: it performs the same deployment while also writing
the laptop's current address into `wand_config.json`.

### 2A. Ordinary router/Wi-Fi (IPv4; full production wand)

1. Connect the laptop, UNO Q, and every phone to the same Wi-Fi.
2. Find the laptop's Wi-Fi address:

   ```bash
   ipconfig getifaddr en0
   ```

   If that is blank, find the active interface with:

   ```bash
   route -n get default | grep interface
   ```

   Then replace `en0` with that interface. A normal result looks like
   `192.168.x.x`, `10.x.x.x`, or `172.16-31.x.x`. Never use `127.0.0.1` as the
   board address.
3. Stop a server left over from another network, then start a fresh one with the
   detected address. Keep this terminal open:

   ```bash
   pkill -f 'server/main.py' 2>/dev/null || true
   LAPTOP_IP=$(ipconfig getifaddr en0)
   WM_LAN_IP="$LAPTOP_IP" .venv/bin/python server/main.py
   ```

4. In a second terminal, deploy the full wand. This rewrites
   `wand_config.json`, so do it again after every network/address change. Stop
   an old stream-probe app first so two board apps do not compete for the one
   wand slot:

   ```bash
   LAPTOP_IP=$(ipconfig getifaddr en0)
   BOARD=arduino@<board-name>.local
   ssh "$BOARD" "arduino-app-cli app stop /home/arduino/ArduinoApps/phoneharmonic-stream-probe" 2>/dev/null || true
   firmware/uno_q/deploy_wand.sh "$BOARD" "$LAPTOP_IP"
   ```

5. Open `http://localhost:8080/console/` on the laptop. The physical wand should
   appear as connected and moving it should animate the pointing beam and meters.

### 2B. Phone hotspot (including IPv6-only iPhone hotspots)

Use this exact connection order:

1. Enable the phone's hotspot. On iPhone 12 or newer, enable **Settings →
   Personal Hotspot → Maximize Compatibility** if the UNO Q has difficulty
   joining.
2. Connect the **laptop to the hotspot through Wi-Fi**, not through iPhone USB
   tethering. Unplug the iPhone from the laptop while diagnosing. The UNO Q may
   remain connected to USB for power/App Lab; its application traffic must still
   use the hotspot Wi-Fi.
3. In Arduino App Lab, change the UNO Q network to this hotspot and wait for the
   Network target to reconnect.
4. Connect any additional instrument phones to the hotspot. Do not assume that
   the phone providing the hotspot can also reach services hosted by a tethered
   client; test it, but keep a second phone joined to the hotspot for the demo.
5. Verify the active Mac interface and addresses:

   ```bash
   route -n get default | grep interface
   ipconfig getifaddr en0
   ifconfig en0 | grep 'inet6 '
   ```

#### If `ipconfig getifaddr en0` returns normal IPv4

Use the ordinary IPv4 instructions above. If it returns `192.0.0.2` with a
`255.255.255.255`/`/32` mask, do **not** use it: that is an isolated USB/CLAT
address, not a board-reachable laptop address.

#### If IPv4 is blank or `192.0.0.2`: use the relay-enabled IPv6 path

Current UNO Q App containers have IPv4-only Docker networking even when the
board host and laptop can communicate over IPv6. The stream-probe launcher
handles this by installing a small relay on the UNO Q host. This is the exact
path that has passed the real hotspot hardware test.

1. Capture a non-loopback, non-link-local IPv6 address from the hotspot
   interface. Keep the assignment on one physical shell line:

   ```bash
   LAPTOP_IPV6=$(ifconfig en0 | awk '/inet6 / && $2 !~ /^fe80:/ && $2 != "::1" {print $2;exit}')
   echo "$LAPTOP_IPV6"
   ```

   It should print an address such as `2605:...`. Do not use an address beginning
   with `fe80:`.
2. Stop any stale server **before** deploying the board, then start a new server
   with this address. Keep this terminal open:

   ```bash
   pkill -f 'server/main.py' 2>/dev/null || true
   WM_LAN_IP="$LAPTOP_IPV6" .venv/bin/python server/main.py
   ```

   Confirm the startup output prints a bracketed section URL such as
   `http://[2605:...]:8080/section/?s=lol1`.
3. In a second terminal, use short variables to avoid accidentally splitting a
   long command. Replace only the board hostname:

   ```bash
   LAPTOP_IPV6=$(ifconfig en0 | awk '/inet6 / && $2 !~ /^fe80:/ && $2 != "::1" {print $2;exit}')
   PROBE=./firmware/uno_q/stream_probe/run_probe.sh
   BOARD=arduino@<board-name>.local
   SERVER_IP="$LAPTOP_IPV6"
   ssh -6 "$BOARD" "arduino-app-cli app stop /home/arduino/ArduinoApps/phoneharmonic-wand" 2>/dev/null || true
   "$PROBE" --board "$BOARD" --server-ip "$SERVER_IP" --keep-running
   ```

   `--server-ip` takes the **bare** IPv6 address; do not add square brackets.
   The launcher adds brackets when building the WebSocket URL. If using shell
   continuation backslashes instead, `\` must be the final character on the
   line—no spaces may follow it.
4. Follow the 30-second prompts: still, rotate clearly around yaw, still again.
   Every result row must say `PASS`. `--keep-running` leaves both the IMU app and
   IPv6 relay running for the live frontend.

The relay-enabled stream probe provides production-format IMU data for aiming,
live meters, stroke/pose recognition, and the motion-controlled demo. It does
not send the production Distance/ToF squeeze events or receive LED/buzzer
feedback. The full production wand currently requires ordinary reachable IPv4;
porting the relay into `firmware/uno_q/wand/` remains required for the complete
ToF/downlink feature set on an IPv6-only hotspot.

### 3. Open the live app and join phones

1. On the laptop, open **`http://localhost:8080/console/`**. Using `localhost`
   keeps camera access in a browser secure context and avoids a stale LAN-IP TLS
   certificate.
2. Hard-refresh the console after every server/network restart. The QR is built
   from the address captured when the server started; an already-open page may
   still show the old QR until it reconnects or refreshes.
3. Point the physical wand at the laptop and click **Recalibrate**. Move it and
   confirm the beam/meters respond.
4. Select a song and click **▶ Start** once. This user click is required by the
   browser to unlock audio.
5. Scan the refreshed QR with each instrument phone. Each section URL uses plain
   `http://...:8080` and does not require a certificate. Tap the phone's Join
   button to unlock its audio.
6. If scanning fails, type the exact **section join** URL printed by the server
   into the phone browser. IPv6 browser URLs require square brackets around the
   address. If the URL works on a second phone but not on the hotspot-hosting
   phone, use the second phone—the hotspot host is blocking/rejecting its route
   to a tethered client.
7. Keep Bluetooth audio off. Bluetooth output latency is far too high for the
   synchronized-phone test.

### 4. Required verification checkpoints

- `ssh` or `ssh -6` reaches the UNO Q on the final network.
- Server startup advertises the **current** network address, not the previous
  router/hotspot address.
- The wand test reports `variant=hw`, 45–70 frames/s, no invalid frames or
  sequence gaps, and visible physical yaw movement.
- The console beam and live meters react to the wand.
- Every joined phone appears in the console roster and shows clock RTT/offset.
- Every phone plays after its local Join/audio-unlock tap.
- Lock/unlock a phone and confirm it reconnects; leave the system running for at
  least five minutes before a demo.

### Hotspot/network troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ipconfig getifaddr en0` is blank | The hotspot is IPv6-only, or `en0` is not the active interface. Inspect `route -n get default` and use the IPv6 relay path. |
| Address is `192.0.0.2/32` | USB tethering or CLAT isolation. Join the hotspot through Wi-Fi and use its global IPv6 address. |
| `--server-ip must be ...` | The variable is empty or contains brackets. Run `echo "$LAPTOP_IPV6"`; pass the bare numeric address. |
| `zsh: command not found: --server-ip` | The command was split after the board hostname without a final `\`. Use the short `PROBE`/`BOARD`/`SERVER_IP` variables above. |
| Wand test passes but QR opens an old address | A server from the previous network is still running. Stop it, restart with `WM_LAN_IP`, then refresh the console. |
| Correct section URL works on laptop but not a phone | Allow inbound Python/TCP 8080 in the Mac firewall. If only the hotspot-hosting phone fails, use another phone joined to the hotspot. |
| QR page opens but there is no sound | Tap Join on that phone and Start on the console; browser audio must be unlocked separately on every device. |
| Wand disappears after another wand page opens | Only one client owns the wand slot. Close `/wandsim/`, extra CV wand pages, and simulators during the hardware test. |

For the probe's full diagnostics, logs, stop commands, and expected metrics, see
[`firmware/uno_q/stream_probe/README.md`](firmware/uno_q/stream_probe/README.md).

---

## Quick start (laptop dev)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
.venv/bin/python server/main.py
```

The server prints its LAN IP and the two URLs you need:

- **Laptop console:** `http://localhost:8080/console/`
- **Section join:**  `http://<lan-ip>:8080/section/?s=lol1`

Open the console on your laptop; open the section URL on each phone (same Wi-Fi).

### Headless smoke test (no browser needed)

With the server running:

```bash
.venv/bin/python server/tools/smoke_test.py
```

Validates static serving, the hello/welcome handshake, clock ping/pong, section
join, and metronome scheduling end-to-end. Expect `ALL CHECKS PASSED`.

---

## Architecture (one process, two ports)

- `:8080` — plain HTTP + `ws://`. Section pages (any phone, no cert hassle) and
  the UNO Q wand.
- `:8443` — HTTPS + `wss://` via mkcert. The wand-**sim** page only (DeviceMotion
  needs a secure context). Disabled until you add certs — the metronome test
  doesn't need it.

The wand streams **raw IMU frames**; the server does all interpretation. Server
never says "play now" — it broadcasts "play note X at server-time T" ~150–600 ms
ahead, and each device converts T to its own audio clock. That lookahead + the
clock sync is what keeps devices together.

Key files: `server/main.py` (entry + connection handler), `server/scheduler.py`
(lookahead broadcast), `server/clocksync.py` (server clock),
`web/shared/clock.js` (**the #1 risk — NTP sync + audio-time mapping**),
`web/section/section.js` (join + scheduling), `server/engine_api.py` (the engine
contract), `server/engine_stub.py` (P1 metronome).

---

## Wands — three interchangeable input options

The conductor's input is swappable; all three stream to the server and the
server does all interpretation, so you can demo with no hardware today and drop
in the real wand later with zero server changes.

| Option | URL | Needs | "Grab" gesture |
|---|---|---|---|
| 🖐️ **Hand (webcam)** | `http://localhost:8080/cvwand/` (laptop) | just a webcam | pinch thumb+index, hold 5s |
| 📱 **Phone** | `https://<lan-ip>:8443/wandsim/` (phone) | HTTPS cert (below) | hold finger on screen |
| 🪄 **UNO Q wand** | `firmware/uno_q/wand/` | UNO Q + Movement + Distance | cover/release the ToF sensor |

The hand and phone options both mirror the hardware's "grab to bound a gesture"
semantics. The phone streams `wand.imu` in the exact format the firmware will —
it's the hardware stand-in. The webcam streams `wand.pose` (position, not IMU);
the P3 gesture extractor consumes either through a common trajectory model.

## HTTPS for the phone wand and LAN camera console

Phones only expose motion sensors on a secure origin, and browsers apply the
same rule to the camera when the console is opened through a LAN IP. A local
certificate in `certs/` makes the server serve `:8443` automatically. Opening
`http://<lan-ip>:8080/console/` then redirects to the secure console. Trust the
development certificate on any device that needs camera or motion access (on
macOS, import `certs/cert.pem` into Keychain Access and set it to **Always
Trust**). On a phone, open `https://<lan-ip>:8443/wandsim/`, accept or trust the
development certificate, then "Enable motion".

**If your LAN IP changed** (different network), regenerate the cert:

```bash
LANIP=$(python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0])")
openssl req -x509 -newkey rsa:2048 -nodes -keyout certs/key.pem -out certs/cert.pem \
  -days 365 -subj "//CN=wandmaestro" -addext "subjectAltName=IP:${LANIP},DNS:localhost,IP:127.0.0.1"
```

**To lose the warning entirely** (nicer for a real demo), use
[mkcert](https://github.com/FiloSottile/mkcert) instead: `mkcert -install` then
`mkcert -cert-file certs/cert.pem -key-file certs/key.pem <lan-ip> localhost`,
and install the mkcert root CA on each team phone once. Stranger phones running
only section pages on `:8080` never need any of this.

---

## P1 verification — the synced-metronome mic test (THE risk gate)

This is the one measurement that proves the core premise. **Do not build P2
until it passes.**

1. Start the server. Open the **stage admin** page on the laptop.
2. Join **two phones** via the section URL — tap **TAP TO JOIN** on each (this
   unlocks audio; you'll see the HUD show a θ offset and rtt within ~2 s).
3. Confirm on the stage page that the **clock spread** readout is small
   (green, ≤30 ms) — a quick sanity check before the mic.
4. Physically pile both phones next to the **laptop's microphone**. Open Audacity
   (or any recorder) on the laptop and hit record.
5. On the stage admin page, click **▶ Start metronome** (120 BPM = one click
   every 500 ms). Let it run ~60 s. For the stress case, midway through start a
   video streaming on another device on the same WiFi.
6. Stop. In the recording, zoom into each click: you'll see one cluster of clicks
   per beat (one per device). **Measure the width of each cluster.**
   - **Pass:** cluster width ≤ 30 ms sustained (target ≤ 15 ms).
   - If wide: the usual culprit is per-device **audio output latency**, not the
     network. Use the **trim** slider on the lagging phone's HUD to nudge it into
     the cluster; persist the value. Never use Bluetooth speakers (150 ms+).
7. **Rejoin test:** lock one phone for ~20 s mid-run, then unlock. It should
   re-sync and its clicks should fall back into the cluster; the other phone
   never stops.

Secondary check (no mic): each click also flashes the phone screen at the
scheduled instant — film both phones at 240 fps and compare flash timing (±4 ms).

---

## Repo layout

```
server/   Python asyncio server (realtime + engine + ml as phases land)
web/      no-build vanilla JS clients (stage, section; wandsim/mousesim later)
firmware/ UNO Q MCU sketch + onboard Linux/Python apps
certs/    mkcert output (gitignored)
```
