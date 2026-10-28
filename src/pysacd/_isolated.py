"""Child side of the isolated engine (see pysacd.isolate). Not a public entry point.

Protocol: the parent pickles a job onto stdin. The child answers with newline-delimited JSON
on its original stdout. With a sector source, it reads replies on stdin: b"\\x01" followed by
the sectors, or b"\\x00" for a failed read.
"""

from __future__ import annotations

import ctypes
import errno
import json
import logging
import os
import pickle
import resource
import signal
import socket  # imported up front: after Landlock the child can't read the stdlib
import sys
import threading

INDEX_CHUNK = 1024              # DST index entries per message (~25 KiB of JSON)


class _Image:
    """What run_area needs to know about the image; the child never parses the TOC."""

    def __init__(self, sector_size: int, data_offset: int, total_sectors: int):
        self.sector_size, self.data_offset, self.total_sectors = sector_size, data_offset, total_sectors


def _paths(paths: list[str]):
    arr = (ctypes.c_char_p * max(1, len(paths)))(*[os.fsencode(p) for p in paths])
    return arr, len(paths)


def _sandbox(lib, job: dict) -> dict:
    lib.sacdconv_landlock.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_char_p), ctypes.c_int]
    for lim, value in ((resource.RLIMIT_FSIZE, job["fsize"]), (resource.RLIMIT_CORE, 0),
                       (resource.RLIMIT_NOFILE, job["nofile"])):
        _soft, hard = resource.getrlimit(lim)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(lim, (value, value))
    nnp = lib.sacdconv_no_new_privs() == 0
    ro, nro = _paths(job["read_only"])
    rw, nrw = _paths(job["writable"])
    landlock = lib.sacdconv_landlock(ro, nro, rw, nrw)
    seccomp = lib.sacdconv_seccomp() == 0
    return {"landlock": landlock, "seccomp": seccomp, "nnp": nnp}


def _probe(job: dict) -> dict:
    """Test hook (tests/test_isolate.py): what the sandbox lets through. Each entry is the
    errno name, or "ok"."""

    def attempt(fn) -> str:
        try:
            fn()
            return "ok"
        except OSError as e:
            return errno.errorcode.get(e.errno or 0, str(e.errno))

    def write_inside():
        with open(os.path.join(job["writable"][0], "probe.tmp"), "wb") as f:
            f.write(b"x")
        os.unlink(os.path.join(job["writable"][0], "probe.tmp"))

    def oversize():
        with open(os.path.join(job["writable"][0], "big.tmp"), "wb") as f:
            try:
                f.write(b"\0" * (job["fsize"] + 1))
            finally:
                os.unlink(os.path.join(job["writable"][0], "big.tmp"))

    return {
        "exec": attempt(lambda: os.execv("/bin/true", ["/bin/true"])),
        "socket": attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()),
        "unix_socket": attempt(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()),
        "read_outside": attempt(lambda: open(job["probe_outside"], "rb").close()),
        "write_outside": attempt(lambda: open(job["probe_outside"] + ".new", "wb").close()),
        "read_inside": attempt(lambda: open(job["read_only"][0], "rb").close()),
        "write_inside": attempt(write_inside),
        "oversize": attempt(oversize),
        "thread": attempt(lambda: threading.Thread(target=lambda: None).start()),
    }


def main() -> int:
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)       # exceeding RLIMIT_FSIZE is EFBIG, not death
    proto = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)               # anything printed by accident goes to stderr, never into the protocol
    stdin = sys.stdin.buffer
    job = pickle.load(stdin)
    lock = threading.Lock()

    def send(**m) -> None:
        line = json.dumps(m).encode() + b"\n"
        with lock:
            proto.write(line)

    class ToParent(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            send(t="log", level=record.levelno, msg=record.getMessage())

    pl = logging.getLogger("pysacd")
    pl.handlers[:] = [ToParent()]
    pl.propagate = False
    pl.setLevel(job["log_level"])

    from pysacd import _native
    lib = _native._load()       # load the engine (and libFLAC) before the filesystem is restricted
    send(t="sandbox", **_sandbox(lib, job))
    if job.get("probe"):
        send(t="probe", results=_probe(job))
        return 0

    source = None
    if job["source"]:
        rlock = threading.Lock()

        def source(lsn: int, count: int) -> bytes:
            with rlock:
                send(t="read", lsn=lsn, count=count)
                if stdin.read(1) != b"\x01":
                    raise OSError(f"the parent's sector source failed at LSN {lsn}")
                data = stdin.read(count * 2048)
                if len(data) != count * 2048:
                    raise OSError("short sector reply from the parent")
                return data

    specs = job["specs"]
    error = ""
    try:
        _native.run_area(_Image(job["sector_size"], job["data_offset"], job["total_sectors"]),  # type: ignore[arg-type]
                         job["iso_path"], job["area"], specs, job["pcm_rate"], job["gain_db"], job["threads"],
                         iso_out=job["iso_out"], interval=job["interval"], source=source,
                         progress=lambda p: send(t="progress", fd=p.frames_done, ft=p.frames_total,
                                                 sd=p.sectors_done, st=p.sectors_total))
    except _native.NativeError as e:
        error = str(e) or "engine failed"
    # A DSDIFF DST index has one entry per frame (hundreds of thousands for a long area), so it
    # goes in bounded chunks before "done" instead of on one unbounded line.
    for i, s in enumerate(specs):
        for k in range(0, len(s.dst_index), INDEX_CHUNK):
            send(t="index", o=i, v=s.dst_index[k:k + INDEX_CHUNK])
    send(t="done", error=error, outputs=[
        {"frames_written": s.frames_written, "frames_missing": s.frames_missing, "dst_errors": s.dst_errors,
         "samples": s.samples, "data_bytes": s.data_bytes} for s in specs])
    return 0


if __name__ == "__main__":
    sys.exit(main())
