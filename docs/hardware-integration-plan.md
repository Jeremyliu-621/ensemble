# Hardware integration — what's left to build

Everything the *server* needs from the wand is already fully specified and
implemented (`docs/hardware-wand.md` = the wire contract, `server/wandio.py`
= gesture windowing + aiming, `server/main.py` = mode/grab/aim dispatch,
`server/tools/wand_bridge.py` = the serial→WebSocket bridge). **None of that
needs to change.** This doc is the checklist for the parts that don't exist
yet: the firmware itself, the link between the UNO Q and the laptop, phone
routing/placement, and the CV gesture state machine that now owns mode
switching *and* grab signaling instead of physical buttons.

Current hardware on hand: **Arduino UNO Q + Modulino Movement (IMU) only** —
no MPR121, no ToF, no wired buttons in the primary path.

**Core design decision:** CV is the primary state machine. The physical wand
does exactly one job — stream raw IMU continuously and let itself be pointed
at things. Every discrete state change (transport, mode, gesture-window
start/end) comes from the laptop's webcam watching the DJ's off-hand, not
from anything wired to the board. No GPIO/GND button wiring is required for
the primary build. Physical buttons are kept as an optional **backup only**,
for if CV recognition proves unreliable live (bad lighting, camera angle) —
see §5.4.

---

## Architecture at a glance

Updated from the original concept sketch to match what this repo actually
does: the wand never classifies anything on-device (no on-board TinyML —
raw IMU only, classification/decision-making is server-side, per
`RESEARCH.md`'s DTW recommendation), and the transport is **Wi-Fi**, not
Bluetooth — see §3 for why.

```
┌────────────────────────────────────────────────────────────────────────┐
│                           THE WAND HARDWARE                            │
│                                                                        │
│  [ Arduino UNO Q + Modulino Movement ]                                 │
│  • Role: streams RAW accel/gyro continuously. No on-device             │
│    classification — the board never decides what a gesture means,     │
│    the server does (heuristic today, DTW/trained model later).        │
│  • Network: Wi-Fi client on the board's own Linux side (recommended,   │
│    §3 Path B1). USB serial for bench bring-up only (§3 Path A).        │
└─────────────────────────────────┬──────────────────────────────────────┘
                                  │
                                  │ COMMUNICATION: Wi-Fi (ws://) — primary
                                  │              USB serial — bring-up only
                                  │ PAYLOAD: {"t":"wand.imu","frames":
                                  │           [[tw,ax,ay,az,gx,gy,gz],...]}}
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                       THE LAPTOP (CENTRAL HUB)                         │
│                                                                        │
│  [ WebCam / CV — off-hand ]          [ Conductor Engine ]              │
│  • Role: fist(hold) = mode toggle,   • Role: resolves aim (yaw→        │
│    pinch(edge) = grab-window           azimuth), buffers gesture       │
│    signal, open-palm = global          windows between grab start/end, │
│    transport — a SEPARATE ws          picks/generates the next        │
│    connection from the wand's          accompaniment line              │
│                                                                        │
│  [ Networking ]                                                        │
│  • Role: broadcasts the local Wi-Fi hotspot & hosts the WebSocket      │
│    server (:8080 / :8443) that every phone, the CV tab, and the wand   │
│    all connect to                                                     │
└─────────────────────────────────┬──────────────────────────────────────┘
                                  │
                                  │ COMMUNICATION: local Wi-Fi (WebSockets)
                                  │ PAYLOAD: {"t":"sched.notes",...} /
                                  │          {"t":"fx.expr",...} (targeted)
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        THE ORCHESTRA HARDWARE                          │
│                                                                        │
│  [ Smartphones (BYOD) ]                                                │
│  • Network: connect directly to the laptop's Wi-Fi hotspot, join by    │
│    scanning a QR code shown on the stage page                         │
│  • Routing: LLM arranger groups the loaded MIDI's tracks across        │
│    connected phones → each gets an instrument via section.config       │
│    (§4)                                                                │
│  • Action: Web Audio API plays clock-synced scheduled notes; the       │
│    aimed/soloed phone additionally receives fx.expr (deterministic     │
│    mode) or the newly composed line (AI mode)                         │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Bill of materials

- [x] Arduino UNO Q
- [x] Modulino Movement (Qwiic/STEMMA QT → `Wire1`, addr `0x6A`)
- [ ] USB cable (data-capable) for the laptop↔UNO Q bring-up path
- [ ] Power bank, once untethered — `docs/hardware-wand.md` already flags the
      auto-shutoff-on-low-draw and cheap-cable-brownout gotchas
- [ ] *(optional, backup-only)* one or two momentary pushbuttons — not
      wired or coded as part of the primary build; see §5.4

**Out of scope:** MPR121 pads, Modulino Distance (ToF/`fx.tension`), any
GPIO+GND button wiring in the primary path.

---

## 2. Firmware — MCU sketch (does not exist yet, needs to be written)

Target: `firmware/uno_q/wand_mcu/wand_mcu.ino` (directory scaffolded, file
not yet written).

This is deliberately thin — the board has no state-machine logic at all,
it's a pure sensor streamer:

- [ ] Init Modulino Movement over `Wire1`
- [ ] Sample IMU at 50–60 Hz; convert accel g → m/s² (× 9.81, **including**
      gravity per the contract); gyro (`getRoll/Pitch/Yaw`) is already
      angular velocity in deg/s — send raw, do **not** integrate on-device
- [ ] Batch ~5 frames per line → one `wand.imu` JSON object, streamed
      continuously from boot (this is what drives both aiming and the
      deterministic-mode expression warp — see §6)
- [ ] No grab detection, no mode logic, no buttons on the primary path —
      all of that lives in the CV client (§4)
- [ ] Dependency: Arduino `Modulino` library. The exact method names
      (`getX/Y/Z`, `getRoll/Pitch/Yaw`, `.begin()`, `.available()`,
      `.update()`) are asserted as "verified" in `docs/hardware-wand.md` —
      re-confirm against the actual installed library version once the
      sketch is on real hardware.

---

## 3. UNO Q ↔ laptop communication — Wi-Fi vs Bluetooth, researched

**Recommendation: Wi-Fi, not Bluetooth, for the live/final path.** Bench-test
over USB serial first regardless.

### Why Wi-Fi wins here

- **Architectural consistency, zero new server code.** Every other device in
  the room (phones, the CV tab) already talks to the laptop over the exact
  same `ws://` protocol. A Wi-Fi-connected wand is just one more client on
  infrastructure that's already built, tested, and proven — it needs no
  bridge process at all (unlike Bluetooth, which needs `wand_bridge.py` or
  an on-board pairing dance).
- **Unconfirmed Bluetooth Classic (SPP) support.** The UNO Q's onboard
  module is the WCBN3536A (Qualcomm WCN3980 chipset), which does Wi-Fi 5
  (802.11a/b/g/n/ac dual-band) and Bluetooth 5.1. `docs.arduino.cc` and the
  datasheet confirm the module and Bluetooth 5.1, but **do not explicitly
  document classic SPP/RFCOMM support** — BT 5.1 chips are frequently
  BLE-focused, and `wand_bridge.py`'s `pyserial` approach specifically needs
  classic SPP, not BLE. This is a real unresolved risk, not a settled fact.
- **Latency is good enough either way, and Wi-Fi is competitive.** Public
  measurements put BLE sensor-event latency around ~100ms for small payloads
  (e.g. a 24-byte tap event, Arduino Nano BLE → server), with connection-
  interval tuning needed to do better. Classic BT SPP is generally lower
  latency than BLE for steady streams (comparable to what game controllers
  use) but still adds a pairing/profile layer. Local Wi-Fi on the same LAN
  the phones already use typically runs single-digit-to-low-double-digit ms
  for small JSON frames — and this system already tolerates far more slack
  than that by design (150–600ms scheduling lookahead, ~2.4s bar
  quantization for musical decisions per the README).
- **Power is a non-issue here.** BLE's real advantage over both Wi-Fi and
  classic BT is power efficiency — but the wand is already power-bank-fed
  and expected to stay near the laptop/room, so that advantage doesn't
  matter for this build.

### What this changes from the original plan

- [ ] Treat §3 Path B1 (Wi-Fi direct from the UNO Q's own Linux side) as the
      **default target**, not just an "option"
- [ ] Treat Bluetooth Serial (Path B2) as a **fallback only**, worth
      revisiting if Wi-Fi proves flaky in the venue (contention from a room
      full of phones on the same hotspot is the realistic failure mode to
      watch for, not Bluetooth's own limitations)

### Path A — USB Serial (bring-up, do this first regardless)

- [ ] Flash the sketch, confirm the board enumerates as a serial device
- [ ] Run the **existing, unmodified** bridge:
      `python server/tools/wand_bridge.py --port /dev/tty.<device> --baud 115200`
- [ ] Confirm the hello/welcome handshake completes and the server log shows
      `wand connected (variant=hw)`
- [ ] Bench-test: watch the server log for continuous `wand.imu` frames while
      moving the board

### Path B1 — Wi-Fi direct from the UNO Q's Linux side (recommended default)

The UNO Q is a dual-brain board: a real-time MCU plus an onboard Qualcomm
Linux SoC. Write a small Python script that runs *on the board's Linux
side* (`arduino.app_utils`, `Bridge.provide` — per the "gotchas" section of
`docs/hardware-wand.md`) that receives the MCU's `Bridge.notify()` sensor
events, batches them into the same JSON, and holds a WebSocket client
directly to `ws://<lan-ip>:8080/ws`. No laptop-side bridge script needed —
the wand joins over Wi-Fi exactly like a phone section or the CV tab does.

- [ ] Confirm the UNO Q's Linux side can join the laptop's hotspot/LAN
- [ ] Write the glue script (batching + WS client + hello handshake, mirrors
      `wand_bridge.py`'s forwarding logic but runs on-board instead of on
      the laptop)
- [ ] Swap over from Path A once proven

### Path B2 — Bluetooth Serial (fallback only, not the default)

`docs/hardware-wand.md` calls this "the new plan," but per the research
above it's now the fallback. Requires confirming the WCBN3536A exposes
classic SPP/RFCOMM via BlueZ (`bluetoothctl`, `rfcomm bind`) — **test this
as a go/no-go check** before investing time in it. If it's BLE-only,
`wand_bridge.py`'s `pyserial` approach won't work unmodified and would need
a BLE-UART bridge instead.

---

## 4. Phone routing & spatial placement (already implemented — setup steps only)

No new code needed here; this is what the operator does with BYOD phones so
the wand's targeting behavior lines up with reality.

- **Join:** each phone scans the QR on the stage page → connects to the
  laptop's Wi-Fi hotspot → opens a `role: "section"` WebSocket connection →
  server binds it to a section slot (reused across reconnects by
  `client_id`)
- **Instrument assignment:** on song load, the server extracts MIDI tracks
  and sends them + the roster of connected phones to the LLM arranger
  (`server/arranger.py`), which groups tracks by musical role/frequency
  range and replies with a strict `{section_id: [track indices]}` mapping;
  falls back to simple round-robin if the arranger is unreachable or
  unconfigured. Each phone gets its instrument pushed via
  `{"t":"section.config",...}`.
- **Spatial placement (the part that matters for the wand):** sections are
  auto-spread across -60°..+60° azimuth by join order unless explicitly
  placed. For the wand's pointing gesture to feel physically correct, **the
  phones' actual positions in the room should roughly match their assigned
  azimuth order** — either arrange them left-to-right in join order, or use
  the editor's `stage.place` control to assign azimuths matching wherever
  you've actually put them.
- **Targeting/isolation:** the wand's integrated yaw locks onto whichever
  section's azimuth is within 40° → that phone's line goes solo, others
  mute/cover — this is what makes "point the wand at a phone" work.

This is the one place a **physical setup step** (arranging phones in the
room) is required for a *software* feature to behave correctly — worth a
line in the run-of-show checklist, not just a code note.

---

## 5. CV gesture state machine — the primary control layer

File: `web/cvwand/cvwand.js`, extended. Runs in a laptop browser tab as its
**own wand-role WebSocket connection** (`role: "wand-cv"`), separate from
and simultaneous with the hardware wand's connection (`role: "wand"`) — both
are accepted concurrently by the server (dispatch is gated by
"role is a wand role," not by a single-owner slot; the "wand connected"
stage display is cosmetic only). The off-hand tracked by the webcam gets
**three distinct, non-overlapping shapes**, each mapped to one message type:

| Off-hand shape | Trigger style | Message sent | Meaning |
|---|---|---|---|
| **Open palm** | hold ~0.6s = toggle; swipe = edge | `admin.cmd` (`start`/`stop`/`rewind`/`forward`) | global transport — already implemented |
| **Fist** | hold ~0.6s | `wand.mode` (`"ai"` ⇄ `"det"`) | mode toggle — net new |
| **Pinch (thumb+index)** | edge-triggered, no hold | `wand.grab` (`"start"`/`"end"`) | bounds an AI-mode gesture window — net new, signal-only |

### 5.1 Mode toggle (fist)

- [ ] Add a fist detector: all four fingertips closer to the wrist than
      their PIP joints (mirror image of the existing open-palm condition)
- [ ] Reuse the existing ~600ms hold-timer pattern (same shape as the
      palm-hold start/stop logic)
- [ ] On toggle, send `{t: "wand.mode", mode: "ai"|"det"}`
- [ ] No server changes needed — confirmed the server accepts `wand.mode`
      from any wand-role client, resets a stranded grab, and releases the
      expression warp on leaving `det`

### 5.2 Grab signal (pinch) — reuses existing detector, changes what it sends

- [ ] The pinch hysteresis detector already exists (`GRAB_ON`/`GRAB_OFF`,
      well-tested) — reuse it verbatim for edge detection
- [ ] **Important divergence from the current file:** when a hardware wand
      is connected, the pinch must send `wand.grab` **only** — it must NOT
      also stream `wand.pose` frames the way it does today in "CV-as-wand"
      mode. Reason: `WandRouter` buffers whatever frames arrive during a
      grab window keyed only by *modality* (imu vs pose), not by which
      connection sent them. If the CV tab keeps streaming its own pose
      frames while the real wand streams imu frames, they race for the same
      window and one gets discarded. Gating pose-streaming off when acting
      as a CV-assist (vs. a full CV-as-wand substitute with no hardware
      present) resolves this cleanly.
- [ ] The existing no-hardware "webcam is the whole wand" mode (pinch +
      pose streaming + grab, for demos with zero hardware) stays intact as
      a fallback path — it's a different operating mode of the same file,
      not something to delete

### 5.3 Transport (open palm) — already built, no changes

Existing `handlePalm()` logic in `cvwand.js` stays as-is.

### 5.4 Physical backup buttons (optional, deferred)

If live CV recognition proves unreliable (lighting, camera angle, hand
occlusion by the wand itself), fall back to physical buttons wired to the
UNO Q sending the *same* messages (`wand.grab`, `wand.mode`) as edge
triggers — same server contract either way, so nothing downstream needs to
know which source fired. Not part of the primary build; only worth wiring
if bench-testing shows CV is flaky. If added: momentary pushbutton(s),
GPIO + GND, `INPUT_PULLUP`, debounced.

---

## 6. Semantic walkthrough — what fires when, mapped to the live user flow

**Step 1 — Network & hardware init**
- UNO Q powers on → firmware boots and immediately starts streaming
  `wand.imu` continuously, with no gesture or connect step required (§2, §3)
- Phones join the laptop's Wi-Fi/hotspot, scan the QR, become sections (§4)
- The laptop opens the CV tab, which starts tracking the DJ's off-hand and
  opens its own `wand-cv` connection alongside the hardware wand's `wand`
  connection (§5)

**Step 2 — Song load & AI orchestration** *(already built, no hardware
dependency)*
- DJ drops a MIDI file on the laptop → server extracts tracks → LLM
  arranger groups them across connected phones → each phone gets its part
  via `section.config` (§4)

**Step 3 — Live performance**
- **Global transport:** DJ raises the off-hand, open palm → CV tab detects
  it → sends `admin.cmd` → server applies start/stop/rewind/forward to the
  whole room (§5.3, unchanged)
- **Targeting & isolation:** DJ physically points the wand at a phone →
  firmware's continuous `wand.imu` feeds the server's `WandAimer` → yaw
  integration locks aim within 40° of a section's placed azimuth → that
  phone's line goes solo, others mute/cover (already built —
  `server/wandio.py`, `server/engine/conductor.py`; depends on phone
  placement matching azimuth order, §4)
- **Mode selection:** DJ makes a fist with the off-hand, holds ~0.6s → CV
  tab sends `wand.mode` → server toggles `session.wand.mode` (§5.1)
  - **Path A — Deterministic (continuous control):** no grab needed at
    all. The physical wand's already-continuous `wand.imu` stream feeds
    the server's `_expression()`: lifting the wand raises the lift-axis
    reading (`ay`), which gets quantized to scale-locked pitch degrees and
    swells the gain, streamed as `fx.expr` to the aimed phone only. The
    aimed phone's synth warps pitch/volume live. (Already built —
    `server/main.py:_expression`.)
  - **Path B — AI composition (discrete gestures):** DJ pinches with the
    off-hand → CV tab sends `wand.grab start` (signal only, §5.2) → server's
    `WandRouter` starts buffering the *hardware wand's* incoming `wand.imu`
    frames (buffering is modality-keyed, source-agnostic, so this just
    works) → DJ performs the physical swish on the wand → DJ releases the
    pinch → CV tab sends `wand.grab end` → server closes the window,
    extracts gesture features, and the engine's decision logic (heuristic /
    trained model, per `docs/ai-training.md`) picks or generates the next
    accompaniment, streamed to the aimed phone.
- **Backup path:** if CV gesture recognition is unreliable in venue
  conditions, physical buttons (§5.4) substitute for the pinch/fist edges —
  same messages, same server handling, no code downstream needs to change.

---

## 7. End-to-end bring-up sequence

1. Bench-test the firmware alone (serial monitor) — confirm the `wand.imu`
   JSON lines are well-formed and arriving at 50–60 Hz before touching the
   bridge
2. `wand_bridge.py` (Path A) + server — confirm the hardware wand shows
   connected, and aiming works (walk it past a joined, correctly-placed
   phone section, confirm aim locks within 40°)
3. Switch to Wi-Fi direct (Path B1) once A is proven; keep Bluetooth (B2)
   only if a bench test confirms classic SPP actually works
4. CV tab: confirm the fist/palm/pinch detectors fire cleanly against real
   lighting, *before* wiring any backup buttons
5. Mode toggle: fist-hold flips `session.wand.mode`; in `det` mode, confirm
   the expression warp reaches the aimed phone as the wand is raised/lowered
6. Grab/AI mode: pinch-bound gesture window on the off-hand, real swish on
   the physical wand → confirm a gesture window shows up in the server log
   and the accompaniment changes
7. Full run-through of the README's existing P1 mic-sync test, now with real
   hardware and the CV tab both in the loop

---

## 8. Open risks / assumptions to verify on real hardware

- Modulino library exact API — asserted as verified in-repo, re-confirm
  against your installed library version
- Classic Bluetooth SPP support on the WCBN3536A module — genuinely
  unconfirmed by public docs (§3); the reason Wi-Fi is now the recommended
  default rather than an equal option
- Wi-Fi contention: a room full of phones plus the wand plus the CV tab all
  on one hotspot is the realistic risk to watch for on Path B1, not
  Bluetooth's own limitations — worth a load test with several phones
  joined before trusting it live
- CV detector separation in practice: fist vs. a "loose" open hand vs. pinch
  need enough visual margin that MediaPipe doesn't flicker between them —
  worth tuning thresholds live before the demo, same way `GRAB_ON`/`GRAB_OFF`
  already use hysteresis to avoid flicker
- The off-hand needs to stay in frame while the DJ is also manipulating the
  physical wand with the other hand — camera framing/position matters more
  now that CV is load-bearing for mode and grab, not just a demo fallback

---

## Sources (Bluetooth vs Wi-Fi research, §3)

- [UNO Q | Arduino Documentation](https://docs.arduino.cc/hardware/uno-q/)
- [Arduino® UNO Q datasheet (ABX00162)](https://docs.arduino.cc/resources/datasheets/ABX00162-datasheet.pdf)
- [UNO Q User Manual](https://docs.arduino.cc/tutorials/uno-q/user-manual/)
- [Qualcomm acquires Arduino, introduces Arduino UNO Q "dual-brain" SBC — CNX Software](https://www.cnx-software.com/2025/10/07/qualcomm-acquires-arduino-introduces-arduino-uno-q-dual-brain-sbc/)
- [Does Arduino Uno Q (WCBN3536A) support 4-address Wi-Fi mode? — Arduino Forum](https://forum.arduino.cc/t/does-arduino-uno-q-wcbn3536a-support-4-address-wi-fi-mode-iw-list/1409128)
- [Serial Port Profile — Bluetooth SIG spec](https://www.bluetooth.com/specifications/specs/serial-port-profile-1-1/)
- [BLE vs Bluetooth Classic: Data Rate, Range & Power Guide — Zbotic](https://zbotic.in/bluetooth-ble-vs-classic-data-rate-range-power-guide/)
- [LPMS-B2 wireless IMU (Bluetooth Classic + BLE throughput reference)](https://zenshin-tech.com/product/lpms-b2/)
