#!/bin/sh
# SPDX-License-Identifier: MIT OR Apache-2.0
# Build the reference and wheel-flavoured variants into ./build/.
set -eu
cd "$(dirname "$0")"
mkdir -p build
CC=${CC:-gcc}
# ref: plain, no FMA contraction, no SIMD dispatch, no -march.
$CC -O2 -ffp-contract=off -std=gnu17 -Wall -I../../csrc -o build/pcm_vectors_ref pcm_vectors.c -lm
# wheel: the flags the wheels are built with, with the CPU dispatch.
$CC -O3 -ffast-math -fno-finite-math-only -DSC_DISPATCH -std=gnu17 -Wall -I../../csrc -o build/pcm_vectors_wheel pcm_vectors.c -lm
