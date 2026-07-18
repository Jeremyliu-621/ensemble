# Phoneharmonic CV Hand Movements

A standalone browser prototype for controlling MIDI playback and switching
Phoneharmonic demo modes with hand gestures. MediaPipe reads the user's physical
left hand from the webcam while the interface renders only the tracked hand
skeleton over a black background.

This directory is intentionally self-contained and does not connect to the wand,
phone assignment, or Phoneharmonic server code.

## Gesture mapping

| Physical-left-hand gesture | Result |
|---|---|
| Open palm | Play |
| Closed fist | Pause |
| Pinch and move horizontally | Scrub backward or forward through the MIDI song |
| One finger (index) | Toggle Select mode |
| Two fingers (index + middle) | Toggle Deterministic Edit mode |
| Three fingers (index + middle + ring) | Toggle AI Edit mode |

The three modes are mutually exclusive and latched. Releasing the triggering pose
does not clear the mode. The mode remains marked as **toggled** until another mode
gesture replaces it.

Deterministic Edit and AI Edit currently represent state only. They do not change
the audio yet.

## Requirements

- A modern browser with webcam and WebAssembly support
- Webcam permission for `127.0.0.1` or `localhost`
- Internet access when first loading the page, because MediaPipe, Tone.js, and the
  MIDI parser are loaded from CDNs
- Python 3 or another local static-file server

## Run locally

From the repository root:

```sh
python3 -m http.server 8765 --bind 127.0.0.1 --directory cv_hand_movements
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/) and select **Tap to start
camera**. The browser still uses the webcam for recognition, but the live camera
image is hidden.

Upload a `.mid` or `.midi` file with the upload control, drag one onto the timeline,
or use `sample.mid` from this directory.

## Recognition behavior

- Only MediaPipe results labeled as the physical `Left` hand are classified,
  smoothed, routed, and drawn.
- The physical right hand is ignored even when both hands are visible.
- Pinch classification has priority over all other gestures.
- Exact one-, two-, and three-finger poses have priority over MediaPipe's built-in
  `Pointing_Up` and `Victory` labels.
- Gesture enter/exit events are stabilized across multiple frames to reduce
  accidental commands and UI flicker.
- Transport gestures remain available while any mode is toggled.

## Tests

From the repository root:

```sh
node --test cv_hand_movements/tests/*.test.mjs
```

The tests cover mode finger patterns, physical-left-hand selection, play/pause
routing, mode no-op behavior, and continuous pinch scrubbing.

## Project structure

```text
cv_hand_movements/
|-- cv/
|   |-- gestures.js       Gesture classification and pinch hysteresis
|   |-- handedness.js     Physical-left-hand filtering
|   |-- recognizer.js     Camera and MediaPipe setup
|   `-- stabilize.js      Landmark smoothing and gesture debouncing
|-- midi/
|   |-- commands.js       Gesture-to-transport routing
|   |-- player.js         Tone.js MIDI playback
|   `-- timeline.js       Piano roll and scrub position mapping
|-- tests/
|   `-- cv.test.mjs       Pure JavaScript behavior tests
|-- ARCHITECTURE.md       Detailed pipeline and design boundaries
|-- index.html            Prototype interface and styles
|-- main.js               Application state and CV/UI wiring
`-- sample.mid            Sample MIDI file
```

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the detailed processing pipeline and
extension boundaries.
