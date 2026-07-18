"""Chord theory the conductor uses live — the EXACT treatments approved in
the offline listening tests (harmony_remix.py):

  - bar_chords: the song's harmony per bar, read from its accompaniment or
    inferred diatonically from a bare melody's downbeats.
  - voice_lead: each chord takes the inversion nearest the previous voicing,
    so pads glide between chords instead of jumping.
  - chord_span: how long the current chord holds — pads re-strike ONLY when
    the harmony changes, never drone against movement.
  - thin_grid: hush by simplifying onto the beat grid (every 2nd/4th note),
    the way a player would simplify — no random holes.

The live engine AND the training-data generators import from here, so what
the model learns is byte-identical to what the fallback plays.
"""
from __future__ import annotations

from engine.theory import MAJOR, scale_pcs, snap_to_scale, triad

PAD_VEL = 0.24
ROOT_VEL = 0.3

# Diatonic triad qualities on each major-scale degree: I ii iii IV V vi.
_DEGREES = [(0, False), (2, True), (4, True), (5, False), (7, False), (9, True)]

# How much better a NEW chord must fit before we move off the current one —
# harmony has inertia; melodies arpeggiate over held chords.
_INERTIA = 1.35


def _fit_chord(bar, key: int, prev: tuple[int, bool]) -> tuple[int, bool]:
    """Best diatonic triad for this bar's melody, weighted by duration, with
    a bias toward keeping the previous chord (catches infrequent changes
    instead of churning on every downbeat)."""
    weights: dict[int, float] = {}
    for (_on, dur, m) in bar.melody:
        pc = snap_to_scale(m, key) % 12
        weights[pc] = weights.get(pc, 0.0) + dur
    if not weights:
        return prev
    best, best_score = prev, -1.0
    for off, minor in _DEGREES:
        root = (key + off) % 12
        pcs = set(triad(root, minor))
        score = sum(w for pc, w in weights.items() if pc in pcs)
        if (root, minor) == prev:
            score *= _INERTIA
        if score > best_score:
            best, best_score = (root, minor), score
    return best


def bar_chords(song) -> list[tuple[int, bool]]:
    """(root_pc, minor) per bar. Real harmony parts win; a bare melody is
    harmonized by best-fit diatonic triads with inertia."""
    has_harmony = any(not p.is_melody and not p.is_drum for p in song.parts)
    out = []
    prev = (song.key_root, (song.key_root + 4) % 12 not in scale_pcs(song.key_root))
    for bar in song.bars:
        if has_harmony:
            prev = (bar.chord_root, bar.chord_minor)
        elif bar.melody:
            prev = _fit_chord(bar, song.key_root, prev)
        out.append(prev)
    return out


def voice_lead(prev_voices: list[int] | None, pcs: tuple[int, ...]) -> list[int]:
    """Nearest-inversion voicing (classic voice leading), ~G3 region to start."""
    if prev_voices is None:
        base = 55
        return sorted(base + ((pc - base) % 12) for pc in pcs)
    voices = []
    for v, pc in zip(prev_voices, sorted(pcs)):
        candidates = [pc + 12 * o for o in range(3, 7)]
        voices.append(min(candidates, key=lambda c: abs(c - v)))
    return sorted(voices)


def chord_span(chords: list[tuple[int, bool]], idx: int, cap: int = 4) -> int:
    """Bars (up to cap) the chord at idx holds, including idx itself."""
    j = idx
    while j + 1 < len(chords) and j - idx + 1 < cap and chords[j + 1] == chords[idx]:
        j += 1
    return j - idx + 1


def thin_grid(notes: list, level: int) -> list:
    """Hush: keep the beat grid (every 2nd note at level 1, every 4th at 2)."""
    if level <= 0 or not notes:
        return notes
    step = 2 if level == 1 else 4
    kept = [n for n in sorted(notes) if (n[0] % step) < 0.26]
    return kept or sorted(notes)[:1]
