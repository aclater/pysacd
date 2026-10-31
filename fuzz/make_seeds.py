#!/usr/bin/env python3
"""Write the engine fuzzer's seed corpus: synthetic disc images (#36, #85).

    PYTHONPATH=src:tests python3 fuzz/make_seeds.py build/fuzz-seeds

Seeds for fuzz/fuzz_engine.c, in OUT/engine: pysacd's hostile corpus (tests/sacdgen.py:
malformed TOCs, audio sectors and layouts around a small valid disc with a stereo DST area and
a 6-channel DSD area) and, when the test-only DST encoder is built (make test-tools), a small
disc of coded DST frames with every optional DST feature. No real disc data is used, so the
corpus can be shared freely.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import sacdgen


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/fuzz-seeds") / "engine"
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for h in sacdgen.hostile_corpus():
        (out / f"{h.name}.iso").write_bytes(h.image)
        n += 1
    try:
        import test_dst_full as dst
        if dst.HAVE_ENCODER:
            for ch in (2, 6):
                frames = [f for _, _, f in dst.encoded(ch)]
                with tempfile.TemporaryDirectory() as tmp:
                    iso = Path(tmp) / "d.iso"
                    sacdgen.build_iso(iso, [sacdgen.AreaSpec(ch, frames, [len(frames)], dst=True)])
                    (out / f"dst-features-{ch}ch.iso").write_bytes(iso.read_bytes())
                n += 1
    except ImportError:
        pass
    print(f"wrote {n} seeds to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
