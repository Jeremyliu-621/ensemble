"""YouTube (or any audio file) -> MIDI, ready to drop into the editor.

Pipeline: yt-dlp downloads the audio, Spotify's basic-pitch transcribes it to
MIDI. Full-mix transcriptions are inherently messy (one polyphonic track, no
stems) — best results with simple/sparse songs, and the engine treats the
result like any MIDI: melody detected, key/chords estimated, gestures shape it.
For well-known songs, a hand-made MIDI from an archive will always beat a
transcription — try that first.

The raw transcription is a single polyphonic track, which the engine would
treat as an untouchable melody — so it gets split into Treble/Mid/Bass parts,
giving the conductor an arrangement it can actually shape.

Requires the transcription env (built once; ffmpeg via brew):
  venv/bin/uv venv ~/.wm-transcribe --python 3.12
  venv/bin/uv pip install --python ~/.wm-transcribe/bin/python \
      yt-dlp basic-pitch onnxruntime "setuptools<81" "scipy<1.13"

Run:  venv/bin/python server/tools/song_from_audio.py <url-or-file> [--name song]
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tempfile

import mido

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
PYBIN = pathlib.Path.home() / ".wm-transcribe" / "bin"


def split_registers(src_path: pathlib.Path, dest: pathlib.Path) -> None:
    """Split a one-track transcription into treble/mid/bass parts AND prune it:
    per beat, per band, keep only the few longest-and-loudest notes. Raw
    full-mix transcriptions are note spam; this keeps the musical skeleton so
    the engine sees an arrangement it can bend, not a wall of noise."""
    src = mido.MidiFile(str(src_path))
    merged = mido.merge_tracks(src.tracks)
    tpb = src.ticks_per_beat

    notes, open_, tempo_msgs, t = [], {}, [], 0
    for msg in merged:
        t += msg.time
        if msg.type == "set_tempo":
            tempo_msgs.append((t, msg.copy(time=0)))
        elif msg.type == "note_on" and msg.velocity > 0:
            open_.setdefault(msg.note, []).append((t, msg.velocity))
        elif msg.type in ("note_off", "note_on"):
            if open_.get(msg.note):
                s, v = open_[msg.note].pop(0)
                if t > s:
                    notes.append((s, t, msg.note, v))

    bands = [("Treble", 68, 127, 0, 3), ("Mid", 50, 67, 1, 2), ("Bass", 0, 49, 2, 2)]
    kept = []
    for (name, lo, hi, ch, cap) in bands:
        by_beat: dict[int, list] = {}
        for n in notes:
            if lo <= n[2] <= hi:
                by_beat.setdefault(n[0] // tpb, []).append(n)
        for group in by_beat.values():
            group.sort(key=lambda n: (n[1] - n[0]) * n[3], reverse=True)
            kept.extend((ch,) + n for n in group[:cap])

    out = mido.MidiFile(ticks_per_beat=tpb)
    programs = {0: 40, 1: 48, 2: 33}   # treble=violin, mid=strings, bass=bass
    trks = {}
    for (name, _lo, _hi, ch, _cap) in bands:
        tr = mido.MidiTrack()
        tr.append(mido.MetaMessage("track_name", name=name, time=0))
        tr.append(mido.Message("program_change", channel=ch, program=programs[ch], time=0))
        out.tracks.append(tr)
        trks[ch] = {"tr": tr, "last": 0}
    events = [(abs_t, 0, m) for (abs_t, m) in tempo_msgs]
    for (ch, s, e, p, v) in kept:
        events.append((s, ch, mido.Message("note_on", channel=ch, note=p, velocity=v, time=0)))
        events.append((e, ch, mido.Message("note_off", channel=ch, note=p, velocity=0, time=0)))
    events.sort(key=lambda x: x[0])
    for (abs_t, ch, m) in events:
        st = trks[ch]
        st["tr"].append(m.copy(time=abs_t - st["last"]))
        st["last"] = abs_t
    out.save(str(dest))
    print(f"pruned {len(notes)} raw notes -> {len(kept)} kept")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", help="YouTube URL or local audio file")
    ap.add_argument("--name", default="transcribed")
    args = ap.parse_args()

    out_dir = REPO / "songs"
    out_dir.mkdir(exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix="wm-audio-"))

    if args.source.startswith("http"):
        print("downloading audio…")
        subprocess.run([str(PYBIN / "yt-dlp"), "-x", "--audio-format", "wav",
                        "-o", str(work / "audio.%(ext)s"), args.source], check=True)
        audio = next(work.glob("audio.*"))
    else:
        audio = pathlib.Path(args.source)

    print("transcribing (basic-pitch)…")
    onnx = (PYBIN.parent / "lib" / "python3.12" / "site-packages" / "basic_pitch"
            / "saved_models" / "icassp_2022" / "nmp.onnx")
    # Strict thresholds: a full mix transcribed loosely becomes note spam
    # (drums/reverb/vocal texture all read as phantom notes) — a wall of noise.
    subprocess.run([str(PYBIN / "basic-pitch"), "--model-path", str(onnx),
                    "--onset-threshold", "0.7", "--frame-threshold", "0.5",
                    "--minimum-note-length", "120",
                    "--minimum-frequency", "65", "--maximum-frequency", "1500",
                    str(work), str(audio)], check=True)
    mid = next(work.glob("*.mid"))
    dest = out_dir / f"{args.name}.mid"
    split_registers(mid, dest)
    print(f"wrote {dest} (register-split into Treble/Mid/Bass)")

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from engine.midi_load import load_midi_bytes
    song, parts = load_midi_bytes(dest.read_bytes(), dest.name)
    print(f"loader sees: key={song.key_root} bpm={song.bpm:.0f} bars={len(song.bars)} "
          f"parts={len(parts)} — drop it on the editor, or it's already in songs/ "
          f"for build_bar_dataset --midi-dir")
    return 0


if __name__ == "__main__":
    sys.exit(main())
