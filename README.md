# Wand Maestro

A custom wand conducts a distributed orchestra: phones scan a QR, join as
orchestra sections, and each plays its own **clock-synced** audio. A symbolic
engine proposes accompaniment candidates per bar and a ranker picks one from the
wand's gesture. See `../.claude/plans/virtual-dancing-adleman.md` for the full
plan and `RESEARCH.md` for the verified gesture-recognition research.

**Build status:** P0 (scaffold), P1 server (clock-synced playback), three wand
inputs (webcam hand / phone / [ESP32 later]), a music-engine slice where
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

### Run it — the flow

1. `python server/main.py`
2. **On the computer**, open the orchestra: **`http://localhost:8080/`** → **Tap to
   start**. The laptop *is* the orchestra — you'll hear a looping melody + soft
   chord pad, and a **QR code** appears.
3. **Conduct it**, either way:
   - **Phone wand** — scan the stage's QR with a phone on the same Wi-Fi (tap
     through the one-time cert warning). Wave/tilt to conduct, hold the screen to
     "grab". The stage shows "wand connected" when it's live.
   - **Webcam** (no phone) — click *conduct with your webcam* on the stage →
     allow the camera, **pinch** thumb+index and move your hand.
4. Big/fast motion → busy line; gentle → calm pad; a twist → a counter-melody;
   raise/lower → octave shift. Changes land on the **next bar (~2.4 s)**, so
   conduct slightly ahead of the beat.

Extra phones can still join as **instrument sections** at
`http://<lan-ip>:8080/section/?s=lol1` — once one does, the laptop hands the audio
off to the phones and becomes the visual stage.

---

## Quick start (laptop dev)

```bash
python -m venv venv
venv/Scripts/python -m pip install -r server/requirements.txt   # Windows
# source venv/bin/activate && pip install -r server/requirements.txt   # mac/linux
python server/main.py
```

The server prints its LAN IP and the two URLs you need:

- **Stage / admin:** `http://<lan-ip>:8080/stage/?admin=1`
- **Section join:**  `http://<lan-ip>:8080/section/?s=lol1`

Open the stage on your laptop; open the section URL on each phone (same WiFi).

### Headless smoke test (no browser needed)

With the server running:

```bash
python server/tools/smoke_test.py
```

Validates static serving, the hello/welcome handshake, clock ping/pong, section
join, and metronome scheduling end-to-end. Expect `ALL CHECKS PASSED`.

---

## Architecture (one process, two ports)

- `:8080` — plain HTTP + `ws://`. Section pages (any phone, no cert hassle) and
  the ESP32 wand.
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
| 🖐️ **Hand (webcam)** | `http://localhost:8080/cvwand/` (laptop) | just a webcam | pinch thumb+index |
| 📱 **Phone** | `https://<lan-ip>:8443/wandsim/` (phone) | HTTPS cert (below) | hold finger on screen |
| 🪄 **ESP32 wand** | (firmware, later) | the hardware | squeeze the MPR121 pad |

The hand and phone options both mirror the hardware's "grab to bound a gesture"
semantics. The phone streams `wand.imu` in the exact format the firmware will —
it's the hardware stand-in. The webcam streams `wand.pose` (position, not IMU);
the P3 gesture extractor consumes either through a common trajectory model.

## HTTPS for the phone wand

Phones only expose motion sensors on a secure origin. A **self-signed cert is
already generated** in `certs/` for LAN IP `192.168.18.8` — the server serves
`:8443` automatically when it's present. On the phone, open
`https://<lan-ip>:8443/wandsim/`, tap through the one-time "not private"
warning (expected for self-signed), then "Enable motion".

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
firmware/ ESP32 wand (Arduino C++) — added when hardware arrives
certs/    mkcert output (gitignored)
```
