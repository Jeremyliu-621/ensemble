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
    """Split a one-track transcription into treble/mid/bass parts so the engine
    sees an arrangement it can bend (a lone part would play verbatim)."""
    src = mido.MidiFile(str(src_path))
    merged = mido.merge_tracks(src.tracks)
    bands = [("Treble", 68, 127, 0), ("Mid", 50, 67, 1), ("Bass", 0, 49, 2)]
    out = mido.MidiFile(ticks_per_beat=src.ticks_per_beat)
    tracks = []
    for (name, _lo, _hi, _ch) in bands:
        tr = mido.MidiTrack()
        tr.append(mido.MetaMessage("track_name", name=name, time=0))
        out.tracks.append(tr)
        tracks.append({"tr": tr, "last": 0})
    t = 0
    for msg in merged:
        t += msg.time
        if msg.type in ("note_on", "note_off"):
            for (name, lo, hi, ch), st in zip(bands, tracks):
                if lo <= msg.note <= hi:
                    st["tr"].append(msg.copy(channel=ch, time=t - st["last"]))
                    st["last"] = t
                    break
        elif msg.type == "set_tempo":
            tracks[0]["tr"].append(msg.copy(time=t - tracks[0]["last"]))
            tracks[0]["last"] = t
    out.save(str(dest))


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
    subprocess.run([str(PYBIN / "basic-pitch"), "--model-path", str(onnx),
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
