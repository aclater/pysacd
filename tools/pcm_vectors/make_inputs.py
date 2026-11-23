#!/usr/bin/env python3
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Write input frame files for pcm_vectors into a directory (default ./inputs).

Files are named {kind}_{channels}ch.dsd, kind in silence, ff, music, 20 frames each.
Frames are channel-interleaved: FRAME_BYTES_PER_CH bytes of channel 0, then channel 1, ...

The "music" input is SYNTHETIC: a 1 kHz plus a 7 kHz sine (amplitude 0.25 each)
through a plain first-order 1-bit delta-sigma modulator at DSD64 (2822400 Hz).
Channel c is the same signal with its phase advanced by c * 0.3 rad. It is not
a recording and not a real SACD noise shaper; it only gives the filter a
deterministic, music-like bit stream.
"""
import math
import os
import sys

FRAME_BYTES_PER_CH = 4704
DSD_SILENCE = 0x69
FRAMES = 20
FS = 2822400.0


def modulate(nbits, phase):
    out = bytearray(nbits // 8)
    integ = 0.0
    prev = 1.0
    w1 = 2 * math.pi * 1000.0 / FS
    w2 = 2 * math.pi * 7000.0 / FS
    for i in range(nbits):
        x = 0.25 * math.sin(w1 * i + phase) + 0.25 * math.sin(w2 * i + phase)
        integ += x - prev
        bit = 1 if integ >= 0.0 else 0
        prev = 1.0 if bit else -1.0
        out[i >> 3] |= bit << (7 - (i & 7))  # MSB first
    return bytes(out)


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "inputs"
    os.makedirs(outdir, exist_ok=True)
    for ch in (2, 6):
        n = FRAMES * FRAME_BYTES_PER_CH
        chans = [modulate(n * 8, 0.3 * c) for c in range(ch)]
        data = {
            "silence": bytes([DSD_SILENCE]) * (n * ch),
            "ff": b"\xff" * (n * ch),
        }
        buf = bytearray()
        for f in range(FRAMES):
            for c in range(ch):
                buf += chans[c][f * FRAME_BYTES_PER_CH:(f + 1) * FRAME_BYTES_PER_CH]
        data["music"] = bytes(buf)
        for kind, b in data.items():
            with open(os.path.join(outdir, "%s_%dch.dsd" % (kind, ch)), "wb") as fh:
                fh.write(b)


if __name__ == "__main__":
    main()
