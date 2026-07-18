// The CV controls are intentionally restricted to the user's physical left hand.
// MediaPipe's selfie-camera handedness label remains "Left" when the preview is
// displayed mirrored, as it is in this prototype.

// A stable per-hand key we can use for smoothing / pinch state.
export function handKey(hand, idx) {
  return hand.handedness?.categoryName ?? `hand${idx}`;
}

// Pick the strongest physical-left-hand result and ignore every right-hand result.
// Keeping recognition at two hands lets MediaPipe find the left hand even when the
// right hand is also visible.
export function pickLeftHand(hands) {
  if (!hands || hands.length === 0) return null;
  const candidates = hands
    .map((hand, idx) => ({ hand, idx }))
    .filter(({ hand }) => hand.handedness?.categoryName === "Left" && hand.landmarks)
    .sort((a, b) => (b.hand.handedness?.score ?? 0) - (a.hand.handedness?.score ?? 0));
  if (!candidates.length) return null;

  const { hand, idx } = candidates[0];
  return {
    hand,
    idx,
    key: handKey(hand, idx),
    reason: "physical-left",
  };
}
