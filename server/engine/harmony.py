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

from engine.theory import scale_pcs, snap_to_scale

PAD_VEL = 0.24
ROOT_VEL = 0.3


def bar_chords(song) -> list[tuple[int, bool]]:
    """(root_pc, minor) per bar."""
    has_harmony = any(not p.is_melody and not p.is_drum for p in song.parts)
    out = []
    prev = (song.key_root, (song.key_root + 4) % 12 not in scale_pcs(song.key_root))
    for bar in song.bars:
        if has_harmony:
            prev = (bar.chord_root, bar.chord_minor)
        elif bar.melody:
            root = snap_to_scale(bar.melody[0][2], song.key_root) % 12
            prev = (root, (root + 4) % 12 not in scale_pcs(song.key_root))
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
