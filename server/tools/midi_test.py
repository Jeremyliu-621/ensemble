"""Test the MIDI drop path: build a MIDI, load it, drive the conductor, and
assert it plays the loaded song (not the hardcoded one). No network/hardware.

Run:  python server/tools/midi_test.py    (from repo root)
"""
from __future__ import annotations

import io
import os
import pathlib
import sys

os.environ["WM_DECISION_LOG"] = "0"          # test decisions must not pollute the harvest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import mido
from mido import Message, MetaMessage, MidiFile, MidiTrack

from engine.conductor import Conductor
from engine.midi_load import load_midi_bytes
from engine_api import SectionInfo


def make_test_midi() -> bytes:
    mid = MidiFile(ticks_per_beat=480)
    mel = MidiTrack(); mid.tracks.append(mel)
    mel.append(MetaMessage("track_name", name="Lead", time=0))
    mel.append(MetaMessage("set_tempo", tempo=mido.bpm2tempo(96), time=0))
    mel.append(Message("program_change", channel=0, program=73, time=0))  # flute
    for p in [72, 76, 79, 76, 74, 77, 81, 77]:
        mel.append(Message("note_on", channel=0, note=p, velocity=90, time=0))
        mel.append(Message("note_off", channel=0, note=p, velocity=0, time=480))
    ch = MidiTrack(); mid.tracks.append(ch)
    ch.append(MetaMessage("track_name", name="Strings", time=0))
    ch.append(Message("program_change", channel=1, program=48, time=0))
    for chord in [[48, 52, 55], [43, 47, 50]]:
        for p in chord:
            ch.append(Message("note_on", channel=1, note=p, velocity=60, time=0))
        for j, p in enumerate(chord):
            ch.append(Message("note_off", channel=1, note=p, velocity=0, time=1920 if j == 0 else 0))
    dr = MidiTrack(); mid.tracks.append(dr)
    dr.append(MetaMessage("track_name", name="Drums", time=0))
    for _ in range(8):
        dr.append(Message("note_on", channel=9, note=36, velocity=100, time=0))
        dr.append(Message("note_off", channel=9, note=36, velocity=0, time=240))
    buf = io.BytesIO(); mid.save(file=buf)
    return buf.getvalue()


def main() -> int:
    print("[1] parse a 3-part MIDI (flute lead, strings, drums)")
    data = make_test_midi()
    song, parts = load_midi_bytes(data, "test.mid")
    assert song.key_root == 0, f"expected C major, got {song.key_root}"
    assert len(parts) == 3, parts
    melody_parts = [p for p in parts if p["is_melody"]]
    assert len(melody_parts) == 1 and melody_parts[0]["instrument"] == "flute", melody_parts
    print(f"    {len(parts)} parts, key=C, bpm={song.bpm:.0f}, melody={melody_parts[0]['name']}")

    print("[2] melody matches the MIDI (C E G E ...)")
    assert song.bars[0].melody == [(0, 4, 72), (4, 4, 76), (8, 4, 79), (12, 4, 76)], song.bars[0].melody
    assert song.bars[0].chord_pcs == (0, 4, 7), song.bars[0].chord_pcs   # C major from the strings
    print(f"    bar0 melody {song.bars[0].melody}")

    print("[3] load into conductor -> it plays the LOADED song")
    c = Conductor()
    assert c.song.name == "loop-CGAmF"     # starts on the hardcoded song
    c.load_song(song, parts)
    assert c.status()["song"] == "test.mid" and len(c.status()["tracks"]) == 3
    c.on_sections_changed([SectionInfo("s1", "flute", 0.0, True)])
    c.on_transport("start", 0.0)
    s = c._next_bar_start
    ev = c.get_events(s, s)               # first pull re-anchors, so pull again for a real bar
    s = c._next_bar_start
    ev = c.get_events(s, s)
    notes = sorted(e.note for e in ev)
    assert ev, "loaded song produced no events"
    # Neutral (no gesture) must be VERBATIM: exactly the file's notes for this
    # bar (bar 1 here), no overlay, no shaping.
    expected = sum(len(p.bars[1]) for p in song.parts)
    assert len(ev) == expected, f"neutral must be verbatim: {len(ev)} != {expected}"
    print(f"    conductor status: song={c.status()['song']}, bars={c.status()['bars']}, playing")
    print(f"    a bar of events emitted: {len(ev)} notes")

    print("[4] the conductor SHAPES the arrangement (calm thins it, energy opens it)")
    from gesture_test import imu_window

    def bar0_events(gesture):
        c2 = Conductor()
        c2.load_song(song, parts)
        c2.on_transport("start", 0.0)
        c2.on_gesture(gesture)
        return c2.get_events(0.0, 0.0)

    calm = bar0_events(imu_window(accel_mag=0.3, dur_s=0.25, n=10))
    busy = bar0_events(imu_window(accel_mag=12.0, dur_s=0.5))
    calm_drums = sum(1 for e in calm if e.art == "drum")
    busy_drums = sum(1 for e in busy if e.art == "drum")
    assert len(busy) > len(calm), f"energy should open the arrangement: {len(busy)} vs {len(calm)}"
    assert 1 <= calm_drums < busy_drums, f"drums should thin when calm ({calm_drums} vs {busy_drums})"
    print(f"    busy bar {len(busy)} events ({busy_drums} drum hits) > calm bar {len(calm)} ({calm_drums} hits)")

    print("\nALL MIDI CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nMIDI TEST FAILED: {e}")
        sys.exit(1)
