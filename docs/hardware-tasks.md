# Hardware build tasks

Companion checklist to `docs/hardware-integration-plan.md` (full detail,
diagram, research live there — this is just the punch list).

## Executive summary

The wand doesn't think — it just streams motion data the whole time. Your
other hand, watched by the webcam, is what's actually driving things: a
fist flips AI/manual mode, a pinch marks the start/end of a gesture, an open
palm is play/pause/skip. Point the wand at a phone and that phone goes solo.
In manual mode, moving the wand bends pitch/volume live; in AI mode, a
pinch-bounded swish becomes a gesture the server turns into music. Phones
join by QR and get an instrument handed to them automatically.

---

## Tasks

### 1. Wand streams to the laptop at all
Step zero. Before we can do anything clever, we just need the board talking
to the laptop reliably over a wire — no wireless, no gestures, just "is data
showing up."
- [ ] MCU sketch reads Modulino Movement, converts units per contract
- [ ] `wand.imu` JSON lines print over USB serial at 50–60 Hz
- [ ] `wand_bridge.py` forwards them; server log shows `wand connected`

### 2. Accurately select a phone by pointing the wand
Pointing at a phone and having *that* phone respond is basically the whole
magic trick of this project, so it needs to feel tight, not "close enough."
The math already exists — we just haven't stress-tested it with an actual
hand holding an actual wand yet.
- [ ] Confirm the yaw tracking doesn't drift over a multi-minute set
- [ ] `wand.recal` reliably re-zeroes "forward"
- [ ] Phones physically arranged in the room to match their aim order — if
      they're not, pointing at phone 2 might light up phone 4
- [ ] Lock angle tuned live — tight enough you don't grab the wrong phone,
      loose enough to actually hit from a few meters back

### 3. One-hand gestures classify reliably in AI/edit mode
This is the fun one — swish the wand around and have AI mode actually catch
what you meant. Right now the gesture recognition is pretty basic, so this
task is really "make a handful of gestures feel consistently different from
each other," not "build a perfect classifier."
- [ ] Off-hand pinch cleanly bounds the gesture window around the real
      wand-hand swish
- [ ] Tune recognition on real recorded gestures, not synthetic test data
- [ ] A handful of distinct moves (sharp-up, sustained, twist) each
      reliably produce a distinct musical result you can demo back-to-back

### 4. Deterministic mode responds smoothly to wand lift
The manual mode is the simple one — lift the wand, pitch goes up, that's
it. The logic's already written; this task is just making sure it feels
good with a real hand instead of a script faking the motion.
- [ ] Real IMU lift data drives the pitch/gain warp on the aimed phone
- [ ] It feels continuous, not steppy, across the full range of motion
- [ ] Leaving `det` mode cleanly resets the warp everywhere

### 5. CV off-hand state machine works without flicker
The webcam watching your other hand is doing a lot of work — fist, palm,
and pinch all need to be told apart cleanly, or you'll get random mode
switches you didn't ask for. Lighting and camera angle are the enemy here.
- [ ] Fist / open-palm / pinch detectors tuned against real venue lighting
- [ ] No false triggers between the three shapes
- [ ] Off-hand stays trackable while the other hand is busy holding the wand

### 6. Wand/Modulino reflects state back to the DJ
Right now the wand has no idea what mode it's in — it just sends data and
the laptop decides what to do with it. It'd be nice if it could light up or
buzz to say "hey, you're in AI mode now" without needing to glance at a
screen. Nothing like this exists yet — it's a new idea, not a missing
feature we forgot.
- [ ] Define a minimal message the laptop can send back to the wand
- [ ] Firmware reacts to it — some kind of light or indicator on the board
- [ ] Nice-to-have, not required for the core loop to work — don't let this
      block the demo

### 7. Phones route to correct instruments and sit where the wand expects them
This one's basically already built — the AI groups the song's tracks and
hands them to phones on its own. The real task here is physical: put the
phones in the room in the order the wand expects to point at them, or
aiming will feel wrong even though the code is fine.
- [ ] Spot-check the AI's grouping against a real multi-track song, make
      sure it's musically sane, not just "it didn't crash"
- [ ] Confirm the simple round-robin fallback still works if the AI call fails
- [ ] Phone placement becomes a checklist item for setup, not just a note
      in the code

### 8. Wand goes wireless
Once everything works over a USB cable, the next step is cutting the cord.
We're leaning Wi-Fi over Bluetooth — see the other doc for why — mostly
because it's less of an unknown.
- [ ] Get Path A (USB) fully working first, don't skip ahead
- [ ] Wi-Fi direct from the board itself, no laptop-side bridge needed
- [ ] Test it with a room full of phones hogging the same hotspot before
      trusting it live

### 9. Backup buttons work if CV fails
Just a safety net. If the webcam gesture detection turns out to be flaky
live, wire up a couple of literal buttons that do the same job.
- [ ] Don't build this unless CV actually lets us down in testing
- [ ] Same messages either way, so nothing downstream needs to change

### 10. Full rehearsal / demo dry run
The final boss — everything running together at once, no shortcuts or
fallback modes, exactly like it'll go on stage.
- [ ] Hardware wand + CV tab + phones + Wi-Fi, all live, no USB tether
- [ ] Run the README's mic-sync test with real hardware in the loop
