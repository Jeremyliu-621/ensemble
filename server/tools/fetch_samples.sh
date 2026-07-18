#!/bin/bash
# Fetch real sampled instrument notes (FluidR3_GM soundfont renders, MIT-licensed,
# from gleitz/midi-js-soundfonts) into web/assets/sf/ so the synth plays actual
# instruments instead of oscillators. Every 3rd semitone (C2..C7 lattice); the
# player pitch-shifts the nearest sample.  Run once: bash server/tools/fetch_samples.sh
set -e
cd "$(dirname "$0")/../.."
BASE="https://gleitz.github.io/midi-js-soundfonts/FluidR3_GM"
INSTS="acoustic_grand_piano violin viola cello flute clarinet trumpet acoustic_bass orchestral_harp music_box"
FLATS=(C Db D Eb E F Gb G Ab A Bb B)
jobs=()
for inst in $INSTS; do
  mkdir -p "web/assets/sf/$inst"
  for midi in $(seq 36 3 96); do
    pc=$((midi % 12)); oct=$((midi / 12 - 1))
    note="${FLATS[$pc]}$oct"
    out="web/assets/sf/$inst/$note.mp3"
    [ -f "$out" ] || jobs+=("$BASE/$inst-mp3/$note.mp3 $out")
  done
done
printf "%s\n" "${jobs[@]}" | xargs -P 8 -n 2 sh -c 'curl -sf -o "$1" "$0" || echo "miss: $0"'
echo "fetched $(find web/assets/sf -name '*.mp3' | wc -l | tr -d ' ') samples"
