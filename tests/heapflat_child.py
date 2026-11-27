"""Child process of tests/test_heap_flat.py: run one conversion case N times in this process and
print the referee's live bytes after each. Run with the counting shim preloaded and PYTHONMALLOC=malloc
(so the interpreter's own memory is counted too). Not a test itself.

usage: heapflat_child.py CASE ISO OUTDIR N [AUDIO_START AUDIO_END]
CASE: ok | truncated | badout | cancel | corrupt | dstbad | leak | leak256 (the last two are positive controls)
cancel: the progress callback raises KeyboardInterrupt, the Python API's cancel hook (_native.run_area
cancels the engine when the progress callback or the waiting thread is interrupted).
corrupt: random bytes (fixed seed) over the middle third of the audio area, AUDIO_START..AUDIO_END
(sectors of the synthetic ISO, from tests/sacdgen.py's layout).
"""
import contextlib
import ctypes
import gc
import io
import os
import random
import shutil
import sys
import tracemalloc
from pathlib import Path

from pysacd import convert

case, iso, outdir, n = sys.argv[1], sys.argv[2], Path(sys.argv[3]), int(sys.argv[4])
AUDIO = (int(sys.argv[5]), int(sys.argv[6])) if len(sys.argv) > 6 else None
lib = ctypes.CDLL(None)
lib.cm_live.restype = ctypes.c_longlong
lib.cm_count.restype = ctypes.c_longlong
libc_malloc = lib.malloc
libc_malloc.restype = ctypes.c_void_p
libc_malloc.argtypes = [ctypes.c_size_t]
leaked: list[int] = []
outcomes: dict[str, int] = {}


def one() -> None:
    target = iso
    opts = convert.Options(output_dir=str(outdir), musicbrainz=False, cover=False, overwrite=True, jobs=2)
    if case == "truncated":
        target = str(outdir / "cut.iso")
        Path(target).write_bytes(Path(iso).read_bytes()[: 2048 * 600])
    elif case == "corrupt":
        assert AUDIO, "corrupt needs AUDIO_START AUDIO_END"
        target = str(outdir / "bad.iso")
        raw = bytearray(Path(iso).read_bytes())
        lo = (AUDIO[0] + (AUDIO[1] - AUDIO[0]) // 3) * 2048
        hi = (AUDIO[0] + 2 * (AUDIO[1] - AUDIO[0]) // 3) * 2048
        rng = random.Random(61)
        raw[lo:hi] = bytes(rng.getrandbits(8) for _ in range(hi - lo))
        Path(target).write_bytes(bytes(raw))
    elif case == "cancel":
        def cancelling(label):
            def show(p):
                raise KeyboardInterrupt
            return show
        convert._progress_printer = cancelling
    elif case == "badout":
        opts.output_dir = str(outdir / "no" / "such" / "dir" / "\0")
    outcome = "ok"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = convert.convert(target, opts)
        if case in ("ok", "leak", "leak256"):
            assert any(outdir.rglob("*.dsf")), "the conversion wrote nothing"
        if rc:
            outcome = f"exit {rc}"
    except (SystemExit, OSError, ValueError, RuntimeError, KeyboardInterrupt) as e:
        if case in ("ok", "leak", "leak256"):
            raise                                # the success case must succeed
        outcome = type(e).__name__
    outcomes[outcome] = outcomes.get(outcome, 0) + 1     # a few fixed keys: adds nothing after the first run
    if case == "leak":
        leaked.append(libc_malloc(4096))      # 4 KiB per conversion, never freed (positive control)
    elif case == "leak256":
        leaked.append(libc_malloc(256))       # 256 B per conversion: a control near the detection threshold
    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(exist_ok=True)


def level() -> int:
    gc.collect()
    return int(lib.cm_live())


TRACE = os.environ.get('HEAPFLAT_TRACEMALLOC') == '1'   # tracemalloc's own tables would sit in the shim's count
if TRACE:
    tracemalloc.start()
for _ in range(int(os.environ.get('HEAPFLAT_WARMUP', '1'))):
    one()                                       # warm-up: takes the one-time allocations
# the record lives in C memory, preallocated, so that keeping it adds nothing the referee counts
levels = (ctypes.c_longlong * n)()
blocks = (ctypes.c_longlong * n)()
traced = (ctypes.c_longlong * n)()
if os.environ.get('HEAPFLAT_SITES'):
    lib.cm_site_start()
for i in range(n):
    one()
    levels[i] = level()
    blocks[i] = lib.cm_count()
    traced[i] = tracemalloc.get_traced_memory()[0] if TRACE else 0
if os.environ.get("HEAPFLAT_SITES"):
    lib.cm_site_dump(os.environ["HEAPFLAT_SITES"].encode())
print("OUTCOMES", " ".join(f"{k.replace(chr(32), chr(95))}={v}" for k, v in sorted(outcomes.items())))
print("LEVELS", *list(levels))
print("BLOCKS", *list(blocks))
print("TRACED", *list(traced))
