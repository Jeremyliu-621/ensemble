# CV Gesture Prototype - Architecture

This standalone prototype reads gestures from the user's **physical left hand** and
turns them into persistent application modes or MIDI transport commands. It uses
vanilla JavaScript, ES modules, and MediaPipe from a CDN, with no build step.

Serve this folder over `localhost` so `getUserMedia` can access the webcam.

## Supported gestures

| Physical-left-hand gesture | Result |
|---|---|
| Open palm | Play |
| Closed fist | Pause |
| Pinch and move horizontally | Scrub the MIDI timeline |
| One finger (index) | Enter Select mode |
| Two fingers (index + middle) | Enter Deterministic Edit mode |
| Three fingers (index + middle + ring) | Enter AI Edit mode |

Modes are mutually exclusive toggles. Releasing the pose does not leave the current
mode, and the UI continues to label it as toggled until another mode pose replaces
it. Select, Deterministic Edit, and AI Edit currently carry state only and do not
modify MIDI or invoke wand behavior.

## Pipeline

```text
camera frame
   |
   v
GestureRecognizer.recognizeForVideo          cv/recognizer.js
   |  landmarks + handedness + built-in gesture for up to two hands
   v
keep only handedness == "Left"                cv/handedness.js
   |  physical right hand is ignored
   v
smooth left-hand landmarks                    cv/stabilize.js
   v
classify gesture                              cv/gestures.js
   |  PINCH -> exact finger pose -> PALM/FIST
   v
confirm/release gesture edges                 cv/stabilize.js
   |
   +--> sticky mode state + status modal      main.js
   |
   +--> play/pause/pinch scrub                midi/commands.js
```

## Gesture priority

`PINCH` has highest priority because MediaPipe may otherwise describe a pinch as a
fist or no gesture. Exact finger poses run next, before MediaPipe's `Pointing_Up`
and `Victory` results, so one and two fingers reliably switch modes. Only canonical
adjacent poses are accepted: index; index + middle; or index + middle + ring. Thumb
position is ignored for mode counting.

Open palm and closed fist use MediaPipe's built-in labels. Other built-in gestures
are unsupported and produce no command.

## Modules

| File | Responsibility |
|---|---|
| `cv/recognizer.js` | Camera setup, MediaPipe GestureRecognizer initialization, and frame inference. It detects up to two hands so the left hand remains discoverable when both are visible. |
| `cv/handedness.js` | Selects the strongest physical-left-hand result and rejects all physical-right-hand results. |
| `cv/gestures.js` | Finger-extension helpers, pinch hysteresis, exact mode poses, and supported built-in mappings. |
| `cv/stabilize.js` | Landmark smoothing and debounced gesture enter/exit events. |
| `main.js` | Owns persistent mode state, filters/draws the left hand, and updates the gesture and mode UI. |
| `midi/commands.js` | Maps palm, fist, and held pinch to MIDI transport behavior. Mode gestures are ignored here. |
| `index.html` | Webcam stage, non-blocking mode modal, diagnostics, and MIDI timeline. |

## State boundaries

- CV owns `NONE`, `SELECT`, `DETERMINISTIC`, and `AI` mode state.
- Transport gestures remain available in every mode.
- Deterministic and AI modes have no edit behavior yet.
- Instrument/phone selection, wand input, phone mapping, and server messages are out
  of scope for this prototype.

## Verification

Run the pure JavaScript checks from the repository root:

```sh
node --test cv_hand_movements/tests/*.test.mjs
```

For camera verification, serve `cv_hand_movements`, start the camera, and confirm
that the physical right hand never changes gestures or modes. With the physical
left hand, verify all six supported gestures, mode persistence after pose release,
and pinch scrubbing in both directions.
