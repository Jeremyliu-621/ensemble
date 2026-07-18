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

from engine.theory import MAJOR, scale_notes, scale_pcs, snap_to_scale, triad

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
    """Nearest-inversion voicing (classic voice leading), ~G3 region to start.
    Guards against parallel perfects (research checker's FuxCP Rule 7 fix): if
    a voice pair holds a P5/P8 while both voices move the same direction, the
    upper voice takes its next-nearest chord tone instead."""
    if prev_voices is None:
        base = 55
        return sorted(base + ((pc - base) % 12) for pc in pcs)
    voices = []
    for v, pc in zip(prev_voices, sorted(pcs)):
        candidates = sorted((pc + 12 * o for o in range(3, 7)), key=lambda c: abs(c - v))
        voices.append(candidates[0])
    # Parallel-perfect check against the previous voicing, pairwise.
    for i in range(len(voices)):
        for j in range(i + 1, len(voices)):
            if i < len(prev_voices) and j < len(prev_voices):
                before = (prev_voices[j] - prev_voices[i]) % 12
                after = (voices[j] - voices[i]) % 12
                di, dj = voices[i] - prev_voices[i], voices[j] - prev_voices[j]
                if before == after and after in (0, 7) and di != 0 and (di > 0) == (dj > 0):
                    pc_j = voices[j] % 12
                    alts = sorted((pc_j + 12 * o for o in range(3, 7)),
                                  key=lambda c: abs(c - prev_voices[j]))
                    for alt in alts[1:]:
                        if (alt - voices[i]) % 12 not in (0, 7):
                            voices[j] = alt
                            break
    return sorted(voices)


def passing_infill(bar, prev, key: int) -> list:
    """EMBELLISH: fill melodic 3rds/4ths with the intervening scale tone(s) —
    motion added without changing the tune. (Open Music Theory recipe.)"""
    sc = scale_notes(key, 36, 96)
    out = []
    mel = sorted(bar.melody)
    for (a, b) in zip(mel, mel[1:]):
        if b[0] < a[0] + a[1]:                   # overlapping notes: no gap to fill
            continue
        lo, hi = min(a[2], b[2]), max(a[2], b[2])
        mids = [m for m in sc if lo < m < hi]
        iv = abs(b[2] - a[2])
        s = min(a[1] // 2, 2)
        if s < 1:
            continue
        if iv in (3, 4) and mids:
            out.append((a[0] + a[1] - s, s, mids[len(mids) // 2], 0.5))
        elif iv == 5 and len(mids) >= 2:
            first, second = (mids[0], mids[-1]) if b[2] > a[2] else (mids[-1], mids[0])
            out.append((a[0] + a[1] - s, max(1, s // 2), first, 0.5))
            out.append((a[0] + a[1] - s + max(1, s // 2), max(1, s // 2), second, 0.5))
    return out[:6]   # ≤6 passing tones a bar — embellishment, not a second melody


def approach_run(bar, nxt, key: int) -> list:
    """EMBELLISH, at a chord change: three diatonic 16ths climbing the last
    beat into the next bar's chord — the 'glissando into the new chord' the
    listening tests singled out. Silent while the harmony holds, so it reads
    as intention, not noodling."""
    if (nxt.chord_root, nxt.chord_minor) == (bar.chord_root, bar.chord_minor):
        return []
    center = (sum(m for (_o, _d, m) in bar.melody) // len(bar.melody)) if bar.melody else 67
    target = min((nxt.chord_root + 12 * o for o in range(3, 8)),
                 key=lambda m: abs(m - center))
    below = [m for m in scale_notes(key, 40, 96) if m < target][-3:]
    return [(13 + i, 1, m, 0.45) for i, m in enumerate(below)]


def arpeggiate(bar, prev, key: int) -> list:
    """ENERGIZE: the bar's chord as an Alberti-pattern 8th figure (low-high-
    mid-high) in the register below the melody."""
    pcs = sorted(triad(bar.chord_root, bar.chord_minor))
    lo = 48 + ((pcs[0] - 48) % 12)
    third = lo + ((pcs[1] - pcs[0]) % 12)
    fifth = lo + ((pcs[2] - pcs[0]) % 12)
    pattern = [lo, fifth, third, fifth]
    return [(i * 2, 2, pattern[i % 4], 0.42) for i in range(8)]


def echo(bar, prev, key: int) -> list:
    """ANSWER: replay the previous bar's melody tail — in THIS bar's silent
    slots when there are any, otherwise an octave below the melody where it
    can't collide. Soft either way: call and response, not competition."""
    if not prev.melody:
        return []
    occupied = set()
    for (on, dur, _m) in bar.melody:
        for t in range(int(on), min(16, int(on + dur))):
            occupied.add(t)
    bar_min = min((m for (_o, _d, m) in bar.melody), default=127)
    frag = sorted(prev.melody)[-3:]
    base = frag[0][0]
    out = []
    for (on, dur, m) in frag:
        at = min(14, 8 + (on - base) // 2)
        d = max(1, min(int(dur), 4))
        in_gap = all(t not in occupied for t in range(int(at), min(16, int(at + d))))
        deep = (m - 12) <= bar_min - 7          # well under the melody: safe underlap
        if in_gap or deep:
            out.append((at, d, m - 12, 0.35))
    return out


def apply_chords(song) -> list[tuple[int, bool]]:
    """Write the fitted progression INTO the song's bars so every consumer
    (pads, arpeggios, candidates, model context) sees the same harmony. On
    files with real harmony parts this is an identity operation; on bare
    melodies it replaces the loader's stale defaults with the inertia fit —
    without this, bar-local generators arpeggiate one frozen chord forever."""
    fitted = bar_chords(song)
    for bar, (root, minor) in zip(song.bars, fitted):
        bar.chord_root = root
        bar.chord_minor = minor
        bar.chord_pcs = triad(root, minor)
    return fitted


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
