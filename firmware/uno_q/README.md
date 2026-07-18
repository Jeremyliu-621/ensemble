# UNO Q Wand Firmware

This is the firmware for the physical wand — an **Arduino UNO Q + Modulino
Movement (IMU)**. The wand is the conductor's instrument: you wave it and point
it, and the laptop turns that into music across the room's phones.

The important thing to know up front: the UNO Q is **two computers on one board**.
There's a small real-time microcontroller *and* a full little Linux computer with
WiFi, sitting side by side and talking over an on-board channel. Our code is split
to match that hardware, which is why there are two folders here.

```
         wand_mcu/  (microcontroller)        linux/  (on-board Linux + WiFi)
        ┌──────────────────────────┐       ┌──────────────────────────────┐
 IMU ──►│ read the sensor, drive    │──────►│ package readings, hold the    │──► laptop
        │ the LED/buzzer            │◄──────│ WiFi connection to the laptop │◄── server
        └──────────────────────────┘       └──────────────────────────────┘
```

## `wand_mcu/` — the microcontroller half

This is the "classic Arduino" part: a single sketch (`wand_mcu.ino`) running on
the real-time chip. It's great at doing one small thing very steadily, and that's
all we ask of it. Two responsibilities:

- **Read the motion sensor** many times a second and convert the raw numbers into
  the units the laptop expects, then hand each reading off to the Linux half.
- **Reflect show state back to the operator** — when the laptop says "the music is
  paused" or "you're now in AI mode," the sketch lights an LED or gives a little
  buzz so the person holding the wand gets feedback.

What it deliberately does *not* do: it makes no decisions. It doesn't figure out
what a gesture "means," doesn't decide which phone you're pointing at, doesn't
know anything about the music. It's a sensor-and-lights device. All the smarts
live on the laptop. Keeping the microcontroller dumb is intentional — it's the
part that has to stay rock-solid at a steady sample rate.

## `linux/` — the on-board Linux half (the networking brain)

The microcontroller can't do WiFi, so this is the piece that actually talks to
the laptop. It's a small Python program running on the UNO Q's built-in Linux
computer. Think of it as the wand's networking layer. Its job is the two-way
bridge between the microcontroller and the laptop server:

- **Uplink:** collect the sensor readings coming off the microcontroller, bundle
  them up, and stream them to the laptop over WiFi — the same way a phone in the
  room connects. This stream is what drives everything: aiming, gestures, and the
  continuous "lift to swell the volume" control.
- **Downlink:** listen for state updates the laptop sends back (paused/playing,
  which mode we're in, which phone is currently selected) and pass them down to
  the microcontroller so it can light the right LED.

The folder is a handful of small files, each with a clear role: one owns the WiFi
connection and the message plumbing, one holds the current "what's the wand's
state" snapshot, one tracks which phone is selected, one is a placeholder for a
future on-device AI feature we haven't built yet, and one config file with things
like the laptop's address. There's a single entry point (`run.py`) that wires them
together and is what gets launched on the board.

## How they fit together

- The **microcontroller** is the hands: it feels the motion and shows the lights.
- The **Linux half** is the voice: it carries the wand's data to the laptop and
  brings the laptop's answers back.
- The **laptop server** is the brain: it decides what everything means and what
  the music does.

Because the wand rides the same WiFi as the phones, from the laptop's point of
view it's just one more client in the room — no special cabling, no separate
bridge process. Plug it in, it joins, and it starts streaming.

## Related docs

- `firmware/BACKEND_NOTES.md` — what the laptop server expects from the wand.
- `firmware/IMPLEMENTATION_PLAN.md` — the full design and the reasoning behind it.
- `firmware/uno_q/TEST_PLAN.md` — step-by-step checks to confirm it all works.
