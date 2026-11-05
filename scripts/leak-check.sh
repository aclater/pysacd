#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Check the C engine for leaks (#122), without Python, whose own allocations hide them:
# build/leak_engine is the engine and the scarletbook library driven by the fuzz harness
# (open, read the TOC, write DSF, DSDIFF, WAV and FLAC, close), run on synthetic discs.
#
#   scripts/leak-check.sh lsan      LeakSanitizer, on every seed, twice (about 15 s)
#   scripts/leak-check.sh memcheck  valgrind memcheck, on a few seeds: any definite leak or
#                                   memory error fails (under a minute)
#
# Needs make test-tools's compiler, libFLAC's development files, the test requirements (numpy),
# and for each mode gcc's LeakSanitizer runtime or valgrind. A failure exits non-zero.
set -euo pipefail
cd "$(dirname "$0")/.."

# valid discs (stereo DST, 6-channel DSD; coded DST at 2 and 6 channels) and three error paths
MEMCHECK_SEEDS=(valid dst-features-2ch dst-features-6ch dst-garbage-frames truncated-in-audio track-lsn-past-end)

seeds=build/leak-seeds/engine
make -s test-tools
PYTHONPATH=src:tests "${PYTHON:-python3}" fuzz/make_seeds.py build/leak-seeds
count=$(find "$seeds" -type f | wc -l)
if [ "$count" -lt 90 ]; then
    echo "only $count seeds: is something missing?" >&2
    exit 1
fi

rm -f build/leak_engine
case "${1:-}" in
lsan)
    make -s LEAK_FLAGS="-O1 -g -fno-omit-frame-pointer -fsanitize=leak" leak-driver
    # twice: an allocation that a second run overwrites shows only then
    LSAN_OPTIONS=exitcode=23 build/leak_engine "$seeds"/* "$seeds"/*
    echo "LeakSanitizer: no leaks in $count seeds, twice"
    ;;
memcheck)
    make -s LEAK_FLAGS="-O1 -g -fno-omit-frame-pointer" leak-driver
    files=()
    for seed in "${MEMCHECK_SEEDS[@]}"; do
        files+=("$seeds/$seed.iso")
    done
    valgrind -q --leak-check=full --errors-for-leak-kinds=definite --show-leak-kinds=definite \
        --error-exitcode=99 build/leak_engine "${files[@]}"
    echo "memcheck: no definite leaks or memory errors in ${#files[@]} seeds"
    ;;
*)
    echo "usage: $0 lsan|memcheck" >&2
    exit 2
    ;;
esac
