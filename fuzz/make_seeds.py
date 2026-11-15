#!/usr/bin/env python3
"""Write the engine fuzzer's seed corpus: synthetic disc images (#36, #85).

    PYTHONPATH=src:tests python3 fuzz/make_seeds.py build/fuzz-seeds

Seeds for fuzz/fuzz_engine.c, in OUT/engine: pysacd's hostile corpus (tests/sacdgen.py:
malformed TOCs, audio sectors and layouts around a small valid disc with a stereo DST area and
a 6-channel DSD area) and, when the test-only DST encoder is built (make test-tools), a small
disc of coded DST frames with every optional DST feature. These are plain images.

The cfg-*.iso seeds are configured runs (#157): the harness's CFG_MAGIC and run configuration,
then an image (the valid disc, a longer one, or the DST discs). Each reaches one of the engine's
paths that plain images don't: ranges past frame 0 in every format, DSF chains with a head
frame, the ISO copy, cancellation, 176.4 kHz PCM, FLAC, 1 and 20 threads, the filler limit, a
provisional start. No real disc data is used, so the corpus can be shared freely.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import sacdgen

# fuzz/fuzz_engine.c's configured runs: keep in step with its CFG_* offsets and bits
MAGIC = b"PSFZcfg1"
CFG_SIZE = 24
OUTS, FLAGS, THREADS, CANCEL, FLAC, LEAD, HEAD, GAP, START, LEN, SHIFT, SPLIT, CHECK, GAIN, MISUSE = \
    0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 19, 20, 22, 23
DSF, FLAC_OUT, WAV, DFF, DST, CHAIN, WAV2 = 1, 2, 4, 8, 16, 32, 64
ALL = DSF | FLAC_OUT | WAV | DFF | DST | WAV2
F176K, FILLER, COPY, SPECULATIVE, SOURCE, LOG, CHECK_FRAME, DATA_OFFSET = 1, 2, 4, 8, 16, 32, 64, 128
CANCEL_LOG, CANCEL_THREAD, CANCEL_BEFORE = 1 << 6, 2 << 6, 3 << 6
EMPTY, FORMAT, RATE, CHANNELS, TOC = 1, 2, 4, 8, 16


def config(outs: int = 0, flags: int = 0, threads: int = 2, cancel: int = 0, flac: int = 0, lead: int = 0,
           head: int = 0xFF, gap: int = 0xFF, start: int = 0, length: int = 1, shifts: tuple = (),
           split: int = 0, check: int = 0, gain: int = 0, misuse: int = 0) -> bytes:
    """A run configuration for fuzz/fuzz_engine.c: values as the harness reads them (threads and
    length as such; the harness stores threads - 1 and length - 1)."""
    c = bytearray(CFG_SIZE)
    c[OUTS], c[FLAGS], c[THREADS], c[CANCEL], c[FLAC] = outs, flags, threads - 1, cancel, flac
    c[LEAD], c[HEAD], c[GAP], c[SPLIT], c[GAIN], c[MISUSE] = lead, head, gap, split, gain & 0xFF, misuse
    c[START:START + 2] = start.to_bytes(2, "little")
    c[LEN:LEN + 2] = (length - 1).to_bytes(2, "little")
    c[CHECK:CHECK + 2] = check.to_bytes(2, "little")
    for i, v in enumerate(shifts):
        c[SHIFT + i] = v
    return MAGIC + bytes(c)


def long_image() -> bytes:
    """A valid disc with room for several batches: a stereo DST area of 100 frames (4 tracks)
    and a 6-channel DSD area of 60 frames (3 tracks)."""
    areas = [sacdgen.AreaSpec(2, [sacdgen._dst_frame(2, i) for i in range(100)], [25] * 4),
             sacdgen.AreaSpec(6, [sacdgen._dsd_frame(6, i) for i in range(60)], [20] * 3, dst=False)]
    img, _ = sacdgen.build_image(areas, sacdgen.DiscInfo(title="pysacd fuzz seed"))
    return bytes(img)


def configured(valid: bytes) -> dict[str, bytes]:
    """The cfg-*.iso seeds: name, configuration + image."""
    long = long_image()
    every = (0, 5, 9, 13, 17, 0, 30)    # each output's own start
    return {
        "cfg-formats-offset": config(ALL, start=21, length=60, shifts=every, threads=3) + long,
        "cfg-formats-offset-valid": config(ALL, start=3, length=6, shifts=(0, 1, 2, 3, 4, 0, 5)) + valid,
        "cfg-chain-head": config(CHAIN, start=30, length=50, lead=7, head=0, split=20, flags=LOG) + long,
        "cfg-chain-head-older": config(CHAIN | WAV, start=40, length=30, lead=3, head=3, split=7) + long,
        "cfg-chain-from-0": config(CHAIN | DSF, start=0, length=40, split=10) + long,
        "cfg-chain-lead-no-head": config(CHAIN, start=20, length=30, lead=5, split=4) + long,
        "cfg-copy": config(DFF, flags=COPY, start=10, length=20) + long,
        "cfg-copy-source": config(DST | WAV, flags=COPY | SOURCE, start=10, length=20) + long,
        "cfg-copy-only": config(0, flags=COPY | LOG) + valid,
        "cfg-cancel-log": config(ALL, flags=LOG, cancel=CANCEL_LOG | 3, length=150, threads=4) + long,
        "cfg-cancel-log-reading": config(DST | DFF, cancel=CANCEL_LOG | 12, length=150, threads=2) + long,
        "cfg-cancel-log-late": config(ALL, cancel=CANCEL_LOG | 40, length=200, threads=3) + long,
        "cfg-cancel-log-copy": config(ALL, flags=COPY, cancel=CANCEL_LOG | 0, length=50) + long,
        "cfg-cancel-thread": config(ALL | CHAIN, cancel=CANCEL_THREAD | 10, length=150, threads=4, split=40) + long,
        "cfg-cancel-before": config(ALL, flags=COPY, cancel=CANCEL_BEFORE, length=50) + long,
        "cfg-176k": config(WAV | FLAC_OUT | WAV2, flags=F176K, flac=5 | 16, start=12, length=70,
                           shifts=every, gain=-24) + long,
        "cfg-176k-valid": config(WAV | FLAC_OUT, flags=F176K | LOG, start=1, length=10) + valid,
        "cfg-threads-1": config(ALL, threads=1, start=5, length=90, shifts=every) + long,
        "cfg-threads-20": config(ALL | CHAIN, threads=20, length=200, split=99, flags=LOG) + long,
        "cfg-filler-refused": config(ALL | CHAIN, start=120, length=200, split=50, threads=3) + long,
        "cfg-filler-allowed": config(ALL | CHAIN, flags=FILLER, start=120, length=200, split=50, threads=3) + long,
        "cfg-filler-tail": config(ALL, flags=FILLER, start=40, length=200, threads=3) + long,
        "cfg-speculative": config(DFF | WAV | DSF, flags=SPECULATIVE | LOG, gap=0, start=50, length=40) + long,
        "cfg-speculative-source": config(DST | WAV, flags=SPECULATIVE | SOURCE | LOG, gap=0, start=50,
                                         length=40) + long,
        "cfg-check-frame": config(DFF | DST, flags=CHECK_FRAME | LOG, check=58, start=50, length=20) + long,
        "cfg-data-offset-source": config(DFF, flags=DATA_OFFSET | SOURCE, length=10) + long,
        "cfg-dst-only": config(DST, start=10, length=60, flags=LOG) + long,
        "cfg-misuse-empty": config(DFF | WAV, misuse=EMPTY, length=10) + valid,
        "cfg-misuse-format": config(DFF, misuse=FORMAT, length=10) + valid,
        "cfg-misuse-rate": config(WAV, misuse=RATE, length=10) + valid,
        "cfg-misuse-channels": config(DFF, misuse=CHANNELS, length=10, flags=LOG) + valid,
        "cfg-misuse-toc": config(DFF, misuse=TOC, length=10, flags=LOG) + valid,
    }


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/fuzz-seeds") / "engine"
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    valid = b""
    for h in sacdgen.hostile_corpus():
        (out / f"{h.name}.iso").write_bytes(h.image)
        n += 1
        if h.name == "valid":
            valid = h.image
    for name, data in configured(valid).items():
        (out / f"{name}.iso").write_bytes(data)
        n += 1
    try:
        import test_dst_full as dst
        if dst.HAVE_ENCODER:
            for ch in (2, 6):
                frames = [f for _, _, f in dst.encoded(ch)]
                with tempfile.TemporaryDirectory() as tmp:
                    iso = Path(tmp) / "d.iso"
                    sacdgen.build_iso(iso, [sacdgen.AreaSpec(ch, frames, [len(frames)], dst=True)])
                    image = iso.read_bytes()
                (out / f"dst-features-{ch}ch.iso").write_bytes(image)
                (out / f"cfg-dst-features-{ch}ch.iso").write_bytes(
                    config(ALL | CHAIN, flags=F176K | LOG, start=1, length=len(frames), split=2) + image)
                n += 2
    except ImportError:
        pass
    print(f"wrote {n} seeds to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
