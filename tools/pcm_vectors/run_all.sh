#!/bin/sh
# SPDX-License-Identifier: MIT OR Apache-2.0
# Generate vectors for every input, channel count and rate, for both variants.
# Usage: run_all.sh [outdir]   (gain 0 dB, tc0 0)
set -eu
cd "$(dirname "$0")"
out=${1:-vectors}
./build.sh
python3 make_inputs.py inputs
for v in ref wheel; do
  mkdir -p "$out/$v"
  for ch in 2 6; do
    for rate in 88200 176400; do
      for kind in silence ff music; do
        n=${kind}_${ch}ch_${rate}
        build/pcm_vectors_$v inputs/${kind}_${ch}ch.dsd $ch $rate 0 0 \
          "$out/$v/$n.f32" "$out/$v/$n.i32"
      done
    done
  done
done
