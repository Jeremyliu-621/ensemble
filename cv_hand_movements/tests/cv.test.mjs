import assert from "node:assert/strict";
import test from "node:test";

import { modeGestureFromFingerState } from "../cv/gestures.js";
import { pickLeftHand } from "../cv/handedness.js";
import { GestureRouter } from "../midi/commands.js";

const fingers = (overrides = {}) => ({
  thumb: false,
  index: false,
  middle: false,
  ring: false,
  pinky: false,
  count: 0,
  ...overrides,
});

test("canonical finger poses map to the three persistent modes", () => {
  assert.equal(modeGestureFromFingerState(fingers({ index: true })), "ONE_FINGER");
  assert.equal(
    modeGestureFromFingerState(fingers({ index: true, middle: true })),
    "TWO_FINGERS",
  );
  assert.equal(
    modeGestureFromFingerState(fingers({ index: true, middle: true, ring: true })),
    "THREE_FINGERS",
  );
});

test("non-canonical finger poses do not switch modes", () => {
  assert.equal(modeGestureFromFingerState(fingers()), null);
  assert.equal(modeGestureFromFingerState(fingers({ middle: true })), null);
  assert.equal(
    modeGestureFromFingerState(fingers({ index: true, middle: true, pinky: true })),
    null,
  );
  assert.equal(
    modeGestureFromFingerState(fingers({ index: true, middle: true, ring: true, pinky: true })),
    null,
  );
});

test("thumb position does not affect mode finger counting", () => {
  assert.equal(
    modeGestureFromFingerState(fingers({ thumb: true, index: true, middle: true })),
    "TWO_FINGERS",
  );
});

test("only the strongest physical-left-hand result is selected", () => {
  const right = { handedness: { categoryName: "Right", score: 0.99 }, landmarks: [{}] };
  const weakerLeft = { handedness: { categoryName: "Left", score: 0.7 }, landmarks: [{}] };
  const strongerLeft = { handedness: { categoryName: "Left", score: 0.9 }, landmarks: [{}] };

  const result = pickLeftHand([right, weakerLeft, strongerLeft]);
  assert.equal(result.hand, strongerLeft);
  assert.equal(result.reason, "physical-left");
  assert.equal(pickLeftHand([right]), null);
});

function makeRouterHarness() {
  const calls = [];
  const player = {
    midi: {},
    playing: false,
    play() { calls.push("play"); this.playing = true; },
    pause() { calls.push("pause"); this.playing = false; },
    seek(time) { calls.push(["seek", time]); },
  };
  const timeline = {
    scrubbing: false,
    xToTime(x) { return x * 100; },
  };
  const router = new GestureRouter(player, timeline, {
    onAction(action) { calls.push(["action", action]); },
  });
  return { calls, player, router, timeline };
}

test("palm always plays and fist always pauses", () => {
  const { calls, router } = makeRouterHarness();
  router.update({ event: { phase: "enter", gesture: "PALM" }, active: "PALM", handX: 0.5 });
  router.update({ event: { phase: "enter", gesture: "FIST" }, active: "FIST", handX: 0.5 });
  assert.deepEqual(calls, ["play", ["action", "play"], "pause", ["action", "pause"]]);
});

test("mode gestures have no MIDI side effects", () => {
  const { calls, router } = makeRouterHarness();
  for (const gesture of ["ONE_FINGER", "TWO_FINGERS", "THREE_FINGERS"]) {
    router.update({ event: { phase: "enter", gesture }, active: gesture, handX: 0.5 });
  }
  assert.deepEqual(calls, []);
});

test("pinch keeps continuous scrubbing and resumes prior playback", () => {
  const { calls, player, router, timeline } = makeRouterHarness();
  player.playing = true;

  router.update({ event: { phase: "enter", gesture: "PINCH" }, active: "PINCH", handX: 0.25 });
  assert.equal(timeline.scrubbing, true);
  router.update({ event: null, active: "PINCH", handX: 0.75 });
  router.update({ event: { phase: "exit", gesture: "PINCH" }, active: null, handX: 0.75 });

  assert.equal(timeline.scrubbing, false);
  assert.deepEqual(calls, [
    "pause",
    ["action", "scrub start"],
    ["seek", 25],
    ["seek", 75],
    "play",
    ["action", "scrub end"],
  ]);
});
