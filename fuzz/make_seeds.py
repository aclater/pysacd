#!/usr/bin/env python3
"""Write the fuzzers' seed corpora: synthetic DST frames and audio sectors (#36).

    PYTHONPATH=src:tests python3 fuzz/make_seeds.py build/fuzz-seeds

Every seed comes from the test-only DST encoder (make test-tools): each
optional DST feature at 2, 5 and 6 channels, the hand-made invalid frames,
and those frames muxed into sectors, DST and plain. No real disc data is
used, so the corpora can be shared freely.
"""

from __future__ import annotations

import sys
from pathlib import Path

import sacdgen
import test_dst_full as dst

CHANNELS = {2: 0, 5: 1, 6: 2}       # first input byte -> channel count, as the harnesses read it


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/fuzz-seeds")
    frames_dir, demux_dir = out / "dst_frame", out / "demux"
    frames_dir.mkdir(parents=True, exist_ok=True)
    demux_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for ch, sel in CHANNELS.items():
        enc = dst.encoded(ch)
        for name, _, frame in enc:
            (frames_dir / f"{ch}ch-{name}").write_bytes(bytes([sel]) + frame)
            n += 1
        for name, frame in dst.invalid_frames(ch).items():
            (frames_dir / f"{ch}ch-invalid-{name}").write_bytes(bytes([sel]) + frame)
            n += 1
        dst_frames = [f for _, _, f in enc]
        sectors, _ = sacdgen.mux(dst_frames[:8], dst=True)
        (demux_dir / f"{ch}ch-dst").write_bytes(bytes([sel]) + b"".join(sectors[:64]))
        plain = [d for _, d, _ in enc[:3]]
        sectors, _ = sacdgen.mux(plain, dst=False)
        (demux_dir / f"{ch}ch-plain").write_bytes(bytes([sel]) + b"".join(sectors[:64]))
        n += 2
    print(f"wrote {n} seeds to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
