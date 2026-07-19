"""Gemini musical cleanup: messy transcribed MIDI -> clean phone-format song.

basic-pitch transcription of a YouTube mix is phantom-note soup. This sends
the raw note list to Gemini with the grid schema the server already speaks
(build_song_from_grid) and asks for a CLEAN arrangement: the melody identified
as one singable line, bass/harmony separated, artifacts dropped, everything
quantized to the 16th grid. The result is validated through the real loader
and written back as a standard MIDI file, so every existing path (song
buttons, editor, conductor) just works.

Run:  python server/tools/gemini_clean.py [songs/yt-song.mid]
      (backs up the original next to it as *.orig.mid)
Env:  WM_GEMINI_KEY (from .env), WM_GEMINI_MODEL (default gemini-2.5-flash)
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # server/ on path

import config
from engine.midi_load import build_song_from_grid, load_midi_bytes

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

INSTRUMENTS = ("piano", "violin", "cello", "viola", "harp", "flute",
               "clarinet", "trumpet", "bass", "bell", "synth", "drums")
GM_PROGRAM = {"piano": 0, "bell": 9, "synth": 81, "bass": 33, "violin": 40,
              "cello": 42, "viola": 41, "harp": 46, "trumpet": 56,
              "clarinet": 71, "flute": 73}

PROMPT = """You are a music arranger. Below is a RAW automatic transcription of a song
(notes as [absolute_16th_position, duration_in_16ths, midi_pitch, velocity_0_to_1], at {bpm} BPM,
4/4 time, 16 sixteenth-notes per bar). It is noisy: phantom notes, split notes, no part separation.

Rewrite it as a CLEAN, playable arrangement:
- Identify THE melody: one singable monophonic line. Mark that part "is_melody": true.
- Add 1-3 accompaniment parts (bass line, chords/harmony) drawn from the source material.
- DROP transcription artifacts: isolated blips, impossible clusters, sub-40 or over-96 pitches
  unless clearly intentional bass/sparkle.
- Quantize musically. Keep at most {max_bars} bars (loop the strongest section if the song is longer).
- Instruments must be chosen from: {instruments}.

Answer with ONLY this JSON (no prose, no code fences):
{{"name": "<short song name>", "bpm": <number>,
  "parts": [{{"instrument": "<from list>", "is_drum": false, "is_melody": <bool>,
             "notes": [[bar, onset16, dur16, midi_pitch, velocity], ...]}}, ...]}}

bar is 0-based; onset16 0-15; dur16 1-16; velocity 0.1-1.0.

RAW TRANSCRIPTION:
{notes}"""


def ask_gemini(prompt: str) -> str:
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent",
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                         "generationConfig": {"temperature": 0.3,
                                              "maxOutputTokens": 32768}}).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_KEY})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                payload = json.load(r)
            return payload["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:  # noqa: BLE001 - free tier throws transient 404/429
            last = e
            print(f"  attempt {attempt + 1} failed ({e}); retrying…")
            time.sleep(8 * (attempt + 1))
    raise SystemExit(f"Gemini unreachable after retries: {last}")


def parse_grid(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.startswith("json") else t
    start, end = t.find("{"), t.rfind("}")
    return json.loads(t[start:end + 1])


def grid_to_midi(grid: dict) -> bytes:
    import io

    import mido
    mid = mido.MidiFile(ticks_per_beat=480)
    six = 120
    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("set_tempo",
                                 tempo=int(60_000_000 / max(40, min(220, float(grid.get("bpm", 100))))),
                                 time=0))
    mid.tracks.append(meta)
    for i, p in enumerate(grid.get("parts", [])):
        tr = mido.MidiTrack()
        ch = 9 if p.get("is_drum") else min(8, i)
        inst = str(p.get("instrument", "piano"))
        if not p.get("is_drum"):
            tr.append(mido.Message("program_change", channel=ch,
                                   program=GM_PROGRAM.get(inst, 0), time=0))
        events = []
        for n in p.get("notes", []):
            if len(n) < 4:
                continue
            bar, on, dur, pitch = int(n[0]), int(n[1]), int(n[2]), int(n[3])
            vel = float(n[4]) if len(n) > 4 else 0.7
            t0 = (bar * 16 + max(0, min(15, on))) * six
            t1 = t0 + max(1, min(16, dur)) * six
            v = max(20, min(127, int(vel * 127)))
            events.append((t0, "note_on", pitch, v))
            events.append((t1, "note_off", pitch, 0))
        events.sort(key=lambda e: (e[0], e[1] == "note_on"))
        last_t = 0
        for (t, kind, pitch, v) in events:
            tr.append(mido.Message(kind, channel=ch, note=max(0, min(127, pitch)),
                                   velocity=v, time=t - last_t))
            last_t = t
        mid.tracks.append(tr)
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def clean(path: pathlib.Path, max_bars: int = 24) -> pathlib.Path:
    if not config.GEMINI_KEY:
        raise SystemExit("set WM_GEMINI_KEY in .env")
    song, parts = load_midi_bytes(path.read_bytes(), path.name)
    notes = []
    for part in song.parts:
        for bar_i, bar_notes in enumerate(part.bars):
            for (on, dur, pitch, vel) in bar_notes:
                notes.append([round(bar_i * 16 + on, 1), round(dur, 1), int(pitch), vel])
    notes.sort()
    if len(notes) > 900:                     # keep the prompt sane; front-load the song
        notes = notes[:900]
    print(f"{path.name}: {len(notes)} raw notes, {len(song.bars)} bars -> Gemini ({config.GEMINI_MODEL})")
    text = ask_gemini(PROMPT.format(bpm=round(song.bpm), max_bars=max_bars,
                                    instruments=", ".join(INSTRUMENTS),
                                    notes=json.dumps(notes, separators=(",", ":"))))
    grid = parse_grid(text)
    # Validate through the REAL loader before touching any file.
    for p in grid.get("parts", []):
        p.setdefault("is_drum", False)
        p.setdefault("is_melody", False)
    vsong, vparts = build_song_from_grid(grid["parts"], float(grid.get("bpm", song.bpm)),
                                         str(grid.get("name", path.stem)))
    n_notes = sum(len(p.get("notes", [])) for p in grid["parts"])
    assert vsong.bars and n_notes >= 24, f"suspiciously thin result: {n_notes} notes"
    midi_bytes = grid_to_midi(grid)
    load_midi_bytes(midi_bytes, "roundtrip-check")      # must load cleanly
    backup = path.with_suffix(".orig.mid")
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    path.write_bytes(midi_bytes)
    print(f"cleaned: '{grid.get('name')}' {len(grid['parts'])} parts, {n_notes} notes, "
          f"{len(vsong.bars)} bars (original backed up to {backup.name})")
    return path


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else REPO / "songs" / "yt-song.mid")
    clean(target)
