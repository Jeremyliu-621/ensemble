"""Smoke-test the editor round-trip: grid notes -> Song -> conductor playback.

Mirrors what the browser editor + main.py do, without a browser: builds a Song
from editor-authored grid parts, swaps it into a Conductor without restarting
(update_song), starts transport, and pulls a lookahead window of events. Verifies
that edits are reflected in what the engine schedules, and that status().transport
is well-formed for the editor's playhead.

Run:  venv/Scripts/python.exe server/tools/edit_test.py
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.conductor import Conductor
from engine.midi_load import build_song_from_grid


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def main() -> None:
    fails = []

    # Two tracks: a melody and a bass line, on the 16th grid.
    parts = [
        {"instrument": "violin", "is_drum": False, "is_melody": True,
         "notes": [[0, 0, 4, 72, 0.9], [0, 4, 4, 76, 0.8], [1, 0, 8, 79, 0.85]]},
        {"instrument": "bass", "is_drum": False, "is_melody": False,
         "notes": [[0, 0, 8, 48, 0.7], [1, 0, 8, 43, 0.7]]},
    ]
    song, tracks = build_song_from_grid(parts, bpm=120, name="unit-song")

    # --- structure checks ---
    if len(song.bars) != 2:
        fails.append(f"expected 2 bars, got {len(song.bars)}")
    if len(song.parts) != 2:
        fails.append(f"expected 2 parts, got {len(song.parts)}")
    if not any(p.is_melody for p in song.parts):
        fails.append("no melody part flagged")
    if song.parts[0].bars[0] == []:
        fails.append("melody bar 0 empty (notes lost)")
    # roll must carry velocity now (5-tuples)
    if tracks and tracks[0]["roll"] and len(tracks[0]["roll"][0]) != 5:
        fails.append(f"roll rows should be 5-long, got {tracks[0]['roll'][0]}")
    # key estimate should land on C (0) for these C-major-ish notes
    if song.key_root != 0:
        print(f"  note: key estimated as {song.key_root} (expected 0=C; heuristic, non-fatal)")

    # --- conductor swap without restart ---
    eng = Conductor()
    eng.update_song(song, tracks, reanchor=False, set_tempo=True)
    if not approx(eng.bar_ms, 60000 / 120 * 4):
        fails.append(f"tempo not applied: bar_ms={eng.bar_ms}")

    # start transport at t=1000 and pull the first two bars
    eng.on_transport("start", 1000.0)
    st = eng.status()
    tr = st.get("transport") or {}
    for k in ("playing", "anchor", "bar_ms", "s16_ms", "n_bars"):
        if k not in tr:
            fails.append(f"transport missing '{k}'")
    if tr.get("anchor") != 1000.0:
        fails.append(f"anchor should be 1000, got {tr.get('anchor')}")
    if tr.get("n_bars") != 2:
        fails.append(f"transport n_bars should be 2, got {tr.get('n_bars')}")

    events = eng.get_events(1000.0, 1000.0 + eng.bar_ms * 2 + 10)
    if not events:
        fails.append("no events scheduled after start")
    # the edited melody's first note (C5=72) should appear near the anchor
    c5 = [e for e in events if e.note == "C5"]
    if not c5:
        fails.append("edited melody note C5 not scheduled")
    else:
        if not approx(min(e.at for e in c5), 1000.0, tol=eng.s16_ms + 1):
            print(f"  note: earliest C5 at {min(e.at for e in c5):.0f} (anchor 1000)")

    # --- live edit: delete the bass, add a high note, swap in mid-play ---
    parts2 = [
        {"instrument": "violin", "is_drum": False, "is_melody": True,
         "notes": [[0, 0, 4, 84, 1.0]]},   # single high C6
    ]
    song2, tracks2 = build_song_from_grid(parts2, bpm=120, name="unit-song")
    eng.update_song(song2, tracks2, reanchor=False)
    ev2 = eng.get_events(1000.0 + eng.bar_ms * 2, 1000.0 + eng.bar_ms * 4)
    if not any(e.note == "C6" for e in ev2):
        fails.append("edited-in C6 not scheduled after live update_song")
    if any(e.note in ("C3", "G2") for e in ev2):
        fails.append("deleted bass still sounding after edit")

    # --- empty song must not crash ---
    song3, tracks3 = build_song_from_grid([], bpm=100, name="empty")
    eng.update_song(song3, tracks3, reanchor=False)
    eng.get_events(9000.0, 9000.0 + eng.bar_ms)   # should be safe

    if fails:
        print("FAIL:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print(f"PASS — edit round-trip OK ({len(events)} events first pull, "
          f"key={song.key_root}, {len(song.parts)} parts, playhead transport well-formed)")


if __name__ == "__main__":
    main()
