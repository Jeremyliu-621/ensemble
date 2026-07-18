"""Load a dropped MIDI file into the engine's Song.

A MIDI file is separated instruments + note events (pitch, start, duration,
velocity) — the "piano roll." We group notes by channel into parts, pick a
melody part, quantise to the 16th grid, estimate per-bar chords and the key, and
build the same `Song`/`BarData` the hardcoded song used, so the conductor plays a
dropped song with zero other changes. Other parts are reported for the editor to
show and (later) distribute across instruments.
"""
from __future__ import annotations

import io
import math
from collections import defaultdict

import mido

from engine.song import BarData, Note, Song, SongPart
from engine.theory import MAJOR, triad

MAX_BARS = 64    # cap loop length so a long MIDI doesn't produce an enormous song
ROLL_CAP = 2000  # cap notes sent per track to the editor (our loops are far shorter)

# General-MIDI program -> one of our sprite/timbre instruments (best-effort).
def gm_instrument(program: int, is_drum: bool) -> str:
    if is_drum:
        return "drums"
    table = [
        (0, 8, "piano"), (8, 16, "bell"), (16, 24, "synth"), (24, 32, "piano"),
        (32, 40, "bass"), (40, 42, "violin"), (42, 44, "cello"), (44, 45, "viola"),
        (45, 46, "harp"),   # pizzicato strings: plucked — the harp samples, not a bowed viola
        (46, 47, "harp"), (47, 56, "violin"), (56, 64, "trumpet"), (64, 72, "clarinet"),
        (72, 80, "flute"),
    ]
    for lo, hi, name in table:
        if lo <= program < hi:
            return name
    return "synth"


def _collect_notes(mid: mido.MidiFile):
    """Return {channel: {"program":int,"name":str,"notes":[(start,dur,pitch,vel)]}}."""
    parts: dict[int, dict] = defaultdict(lambda: {"program": 0, "name": "", "notes": []})
    for track in mid.tracks:
        t = 0
        track_name = ""
        open_notes: dict[tuple[int, int], tuple[int, int]] = {}
        for msg in track:
            t += msg.time
            if msg.type == "track_name":
                track_name = msg.name
            elif msg.type == "program_change":
                parts[msg.channel]["program"] = msg.program
            elif msg.type == "note_on" and msg.velocity > 0:
                open_notes[(msg.channel, msg.note)] = (t, msg.velocity)
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                key = (msg.channel, msg.note)
                if key in open_notes:
                    start, vel = open_notes.pop(key)
                    parts[msg.channel]["notes"].append((start, max(1, t - start), msg.note, vel))
        for ch in parts:
            if not parts[ch]["name"] and track_name:
                # attribute the track name to whichever channel it carried notes on
                if any(n for n in parts[ch]["notes"]):
                    parts[ch]["name"] = track_name
    return parts


def _tempo_bpm(mid: mido.MidiFile) -> float:
    for track in mid.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                return 60_000_000 / msg.tempo
    return 120.0


def _estimate_key(weighted_pc: dict[int, float]) -> int:
    """Pick the major-key root whose scale best covers the (duration-weighted) notes."""
    best_root, best_score = 0, -1.0
    for root in range(12):
        scale = {(root + i) % 12 for i in MAJOR}
        score = sum(w for pc, w in weighted_pc.items() if pc in scale)
        if score > best_score:
            best_root, best_score = root, score
    return best_root


def load_midi_bytes(data: bytes, name: str = "uploaded") -> tuple[Song, list[dict]]:
    mid = mido.MidiFile(file=io.BytesIO(data))
    tpb = mid.ticks_per_beat or 480
    six = max(1, tpb // 4)          # ticks per sixteenth
    bar_ticks = tpb * 4             # 4/4
    bpm = _tempo_bpm(mid)

    parts = _collect_notes(mid)
    if not parts or all(not p["notes"] for p in parts.values()):
        raise ValueError("no notes found in MIDI")

    # Part metadata + melody selection (non-drum, highest mean pitch, enough notes).
    part_info: list[dict] = []
    melody_ch, melody_score = None, -1.0
    weighted_pc: dict[int, float] = defaultdict(float)
    for ch, p in sorted(parts.items()):
        notes = p["notes"]
        if not notes:
            continue
        is_drum = ch == 9
        mean_pitch = sum(n[2] for n in notes) / len(notes)
        instrument = gm_instrument(p["program"], is_drum)
        nm = p["name"] or instrument
        part_info.append({"channel": ch, "name": nm, "program": p["program"],
                          "instrument": instrument, "is_drum": is_drum,
                          "note_count": len(notes), "mean_pitch": round(mean_pitch, 1),
                          "is_melody": False})
        if not is_drum:
            for (_s, d, pitch, _v) in notes:
                weighted_pc[pitch % 12] += d
            # prefer a part named like a lead; else highest mean pitch with >= 4 notes
            named = any(k in nm.lower() for k in ("melody", "lead", "vocal", "soprano"))
            score = mean_pitch + (1000 if named else 0) + (0 if len(notes) >= 4 else -500)
            if score > melody_score:
                melody_ch, melody_score = ch, score

    key_root = _estimate_key(weighted_pc)
    for pi in part_info:
        pi["is_melody"] = pi["channel"] == melody_ch

    # Build bars.
    all_notes = [(s, d, pitch, vel, ch) for ch, p in parts.items() for (s, d, pitch, vel) in p["notes"]]
    max_tick = max((s + d) for (s, d, *_r) in all_notes)
    n_bars = min(MAX_BARS, max(1, math.ceil(max_tick / bar_ticks)))

    bars: list[BarData] = []
    prev_chord = (key_root, False)
    for b in range(n_bars):
        b0, b1 = b * bar_ticks, (b + 1) * bar_ticks
        melody: list[Note] = []
        harmony_pcs: list[tuple[int, int]] = []   # (pitch, pitch_class) for chord estimate
        for (s, d, pitch, vel, ch) in all_notes:
            if not (b0 <= s < b1):
                continue
            if ch == melody_ch:
                onset16 = round((s - b0) / six)
                if 0 <= onset16 < 16:
                    dur16 = max(1, min(16 - onset16, round(d / six)))
                    melody.append((onset16, dur16, pitch))
            elif ch != 9:
                harmony_pcs.append((pitch, pitch % 12))

        # chord estimate: root = lowest harmony note's pc; minor if the b3 is present
        if harmony_pcs:
            root = min(harmony_pcs, key=lambda x: x[0])[1]
            pcs = {pc for _p, pc in harmony_pcs}
            minor = ((root + 3) % 12 in pcs) and ((root + 4) % 12 not in pcs)
            prev_chord = (root, minor)
        root, minor = prev_chord
        melody.sort()
        bars.append(BarData(root, minor, triad(root, minor), melody))

    # Full arrangement: each part's actual notes binned per bar (for distribution).
    song_parts: list[SongPart] = []
    for pi in part_info:
        part_bars: list[list] = [[] for _ in range(n_bars)]
        for (s, d, pitch, vel) in parts[pi["channel"]]["notes"]:
            b = s // bar_ticks
            if b >= n_bars:
                continue
            # 64th-note resolution (quarter-16th floats): fast runs and arpeggios
            # keep their flow instead of clumping onto a coarse grid, and notes
            # may ring past the barline (up to 4 bars) like a real sequencer.
            onset16 = round((s - b * bar_ticks) / six * 4) / 4
            if 0 <= onset16 < 16:
                dur16 = max(0.25, min(32 - onset16, round(d / six * 4) / 4))
                part_bars[b].append((onset16, dur16, pitch, round(vel / 127, 2)))
        for pb in part_bars:
            pb.sort()
        song_parts.append(SongPart(pi["instrument"], pi["is_drum"], pi["is_melody"], part_bars))
        # editable piano-roll for the editor: [[bar, onset16, dur16, pitch, vel], ...]
        roll = []
        for bi, notes in enumerate(part_bars):
            for (on, dur, pitch, v) in notes:
                roll.append([bi, on, dur, pitch, v])
        pi["roll"] = roll[:ROLL_CAP]

    song = Song(name=name, bpm=round(bpm, 1), key_root=key_root, bars=bars, parts=song_parts)
    # Make the fitted harmony canonical: bare-melody files get their inertia-fit
    # progression written into the bars, so arpeggios/candidates/pads all agree.
    from engine.harmony import apply_chords
    apply_chords(song)
    return song, part_info


def build_song_from_grid(parts: list[dict], bpm: float, name: str = "edited") -> tuple[Song, list[dict]]:
    """Turn editor-authored grid notes into the same Song/part_info the MIDI loader
    produces, so a hand-edited song plays through the identical conductor path.

    `parts`: [{"instrument","is_drum","is_melody","name"?,
               "notes":[[bar, onset16, dur16, pitch, vel], ...]}, ...]
    Velocities are 0..1. Bars/onsets/durations are clamped to the 16-slot grid.
    Key + per-bar chords are estimated exactly like load_midi_bytes so the gesture
    overlay stays musical.
    """
    def clamp_note(bar, on, dur, pitch, vel):
        bar = max(0, int(bar))
        on = max(0, min(15, int(on)))
        dur = max(1, min(16 - on, int(dur)))
        return bar, on, dur, max(0, min(127, int(pitch))), round(max(0.0, min(1.0, float(vel))), 2)

    # normalise + find the loop length
    clean: list[list] = []
    n_bars = 1
    for p in parts:
        rows = [clamp_note(*(n + [0.7] * (5 - len(n)))[:5]) for n in p.get("notes", []) if len(n) >= 4]
        clean.append(rows)
        for (bar, *_r) in rows:
            n_bars = max(n_bars, bar + 1)
    n_bars = min(MAX_BARS, n_bars)

    # duration-weighted key estimate over non-drum notes
    weighted_pc: dict[int, float] = defaultdict(float)
    for p, rows in zip(parts, clean):
        if p.get("is_drum"):
            continue
        for (_b, _on, dur, pitch, _v) in rows:
            weighted_pc[pitch % 12] += dur
    key_root = _estimate_key(weighted_pc) if weighted_pc else 0

    # melody part: the flagged one, else the non-drum part with the highest mean pitch
    melody_idx = next((i for i, p in enumerate(parts) if p.get("is_melody")), None)
    if melody_idx is None:
        best_mean = -1.0
        for i, (p, rows) in enumerate(zip(parts, clean)):
            if p.get("is_drum") or not rows:
                continue
            mean = sum(n[3] for n in rows) / len(rows)
            if mean > best_mean:
                best_mean, melody_idx = mean, i

    # bars: chord estimate from harmony notes; melody from the melody part
    bars: list[BarData] = []
    prev_chord = (key_root, False)
    for b in range(n_bars):
        melody: list[Note] = []
        harmony_pcs: list[tuple[int, int]] = []
        for i, (p, rows) in enumerate(zip(parts, clean)):
            if p.get("is_drum"):
                continue
            for (bar, on, dur, pitch, _v) in rows:
                if bar != b:
                    continue
                if i == melody_idx:
                    melody.append((on, dur, pitch))
                else:
                    harmony_pcs.append((pitch, pitch % 12))
        if harmony_pcs:
            root = min(harmony_pcs, key=lambda x: x[0])[1]
            pcs = {pc for _p, pc in harmony_pcs}
            minor = ((root + 3) % 12 in pcs) and ((root + 4) % 12 not in pcs)
            prev_chord = (root, minor)
        elif melody:
            prev_chord = (melody[0][2] % 12, False)
        root, minor = prev_chord
        melody.sort()
        bars.append(BarData(root, minor, triad(root, minor), melody))

    # arrangement parts + editor roll
    song_parts: list[SongPart] = []
    part_info: list[dict] = []
    for i, (p, rows) in enumerate(zip(parts, clean)):
        part_bars: list[list] = [[] for _ in range(n_bars)]
        for (bar, on, dur, pitch, vel) in rows:
            if bar < n_bars:
                part_bars[bar].append((on, dur, pitch, vel))
        for pb in part_bars:
            pb.sort()
        is_melody = (i == melody_idx)
        song_parts.append(SongPart(p["instrument"], bool(p.get("is_drum")), is_melody, part_bars))
        roll = [[b, on, dur, pitch, v] for b, notes in enumerate(part_bars) for (on, dur, pitch, v) in notes]
        part_info.append({"channel": i, "name": p.get("name") or p["instrument"], "program": 0,
                          "instrument": p["instrument"], "is_drum": bool(p.get("is_drum")),
                          "is_melody": is_melody, "note_count": len(rows),
                          "mean_pitch": 0.0, "roll": roll[:ROLL_CAP]})

    song = Song(name=name, bpm=round(float(bpm), 1), key_root=key_root, bars=bars, parts=song_parts)
    return song, part_info
