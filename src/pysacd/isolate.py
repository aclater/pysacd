"""Run the native engine in a sandboxed child process (opt-in: run_area(isolate=True), --isolate).

The default is in-process, with no fork and no cost. With isolation, the parent starts
`python -E -s -m pysacd._isolated` and sends it the job. The child loads the engine, then
applies rlimits, no_new_privs, Landlock (read the image, write the output directories) and
seccomp (no exec, sockets, ptrace, mounts or namespaces) before it reads a single sector.

Trust runs one way. The job goes to the child as a pickle, because the parent wrote it. What
comes back is untrusted, since the child has parsed a hostile image: newline-delimited JSON,
each line bounded in size, with every field type- and range-checked here. Sector sources
(`source=`, e.g. a PS3) stay in the parent and answer the child's read requests over its
stdin. See docs/security.md, "Isolated engine".
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import select
import signal
import subprocess
import sys
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from ._native import OutputSpec, SectorSource
    from .toc import Area, SACDImage

log = logging.getLogger("pysacd.isolate")
engine_log = logging.getLogger("pysacd.engine")

MAX_LINE = 1 << 20              # largest message from the child (a DST index is the biggest)
MAX_READ = 4096                 # most sectors the child may request at once
GRACE = 2.0                     # seconds between SIGTERM and SIGKILL when cancelling
DSD_FRAME = 4704                # bytes per channel per 1/75 s frame of DSD64
SLACK = 1 << 20                 # headers, ID3 tags, DSDIFF chunks, FLAC framing


def _fsize_limit(specs: list[OutputSpec], channels: int, pcm_rate: int, iso_bytes: int) -> int:
    """RLIMIT_FSIZE: the largest file any single output can legitimately need."""
    biggest = iso_bytes
    for s in specs:
        frames = max(0, s.tc_end - s.tc_start) + 1
        dsd = frames * DSD_FRAME * channels
        pcm = frames * (pcm_rate // 75 + 1) * channels * 3          # 24-bit WAV; FLAC is smaller
        biggest = max(biggest, max(dsd, pcm) + len(s.header) + len(s.footer) + s.flac_padding)
    return biggest + SLACK


def _int(v: object, lo: int = 0, hi: int = 1 << 62) -> int:
    if type(v) is not int or not lo <= v <= hi:
        raise ValueError(f"bad integer {v!r}")
    return v


def run_area(image: SACDImage, iso_path: str | os.PathLike, area: Area | None, specs: list[OutputSpec],
             pcm_rate: int, gain_db: float, threads: int, iso_out: str | os.PathLike | None,
             progress, interval: float, source: SectorSource | None) -> None:
    import dataclasses

    from ._native import NativeError, Progress

    iso_path = os.path.realpath(iso_path)
    # The child runs in "/", so every path it gets is absolute; results are copied back onto
    # the caller's specs.
    caller_specs = specs
    specs = [dataclasses.replace(s, path=os.path.abspath(s.path)) for s in specs]
    if iso_out is not None:
        iso_out = os.path.abspath(iso_out)
    outdirs = sorted({os.path.realpath(Path(s.path).parent) for s in specs} |
                     ({os.path.realpath(Path(iso_out).parent)} if iso_out is not None else set()))
    iso_bytes = image.total_sectors * 2048 if iso_out is not None else 0
    job = {
        "iso_path": iso_path,
        "sector_size": image.sector_size, "data_offset": image.data_offset,
        "total_sectors": image.total_sectors,
        "area": area, "specs": specs, "pcm_rate": pcm_rate, "gain_db": gain_db,
        "threads": threads or os.cpu_count() or 1,
        "iso_out": os.fspath(iso_out) if iso_out is not None else None,
        "interval": interval, "source": source is not None,
        "read_only": [] if source is not None else [iso_path], "writable": outdirs,
        "fsize": _fsize_limit(specs, area.channel_count if area else 2, pcm_rate, iso_bytes),
        "nofile": 64 + len(specs),
        "log_level": engine_log.getEffectiveLevel(),
    }
    log.debug("isolated engine: read %s, write %s, RLIMIT_FSIZE %d", job["read_only"], outdirs, job["fsize"])
    child = spawn()
    assert child.stdin is not None and child.stdout is not None
    try:
        pickle.dump(job, child.stdin, protocol=pickle.HIGHEST_PROTOCOL)
        child.stdin.flush()
        done = _serve(child, child.stdout, child.stdin, progress, interval, source, specs, Progress)
    except BaseException:
        _kill(child)
        raise
    finally:
        for f in (child.stdin, child.stdout):
            try:
                f.close()
            except OSError:
                pass
    for mine, theirs in zip(caller_specs, specs):
        mine.frames_written, mine.frames_missing, mine.dst_errors = theirs.frames_written, theirs.frames_missing, theirs.dst_errors
        mine.samples, mine.data_bytes, mine.dst_index = theirs.samples, theirs.data_bytes, theirs.dst_index
    rc = child.wait()
    if done is None:
        raise NativeError(f"isolated engine exited without a result (status {_status(rc)})")
    if done:
        raise NativeError(done)


def spawn() -> subprocess.Popen:
    """Start the child. -E -s -P: no PYTHON* variables, user site-packages or current
    directory (an output directory can contain a disc-named "pysacd" folder) on its path; the
    package's own location is passed explicitly, so source-tree and installed runs both work.
    A new session keeps the terminal's Ctrl-C away from it: the parent cancels it instead."""
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    return subprocess.Popen(
        [sys.executable, "-E", "-s", "-P", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); from pysacd._isolated import main; sys.exit(main())",
         pkg_parent],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True, cwd="/", close_fds=True)


def _status(rc: int) -> str:
    if rc < 0:
        try:
            return f"killed by {signal.Signals(-rc).name}"
        except ValueError:
            return f"killed by signal {-rc}"
    return str(rc)


def _kill(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(GRACE)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def _serve(child, out: IO[bytes], inp: IO[bytes], progress, interval: float, source, specs, Progress) -> str | None:
    """Handle the child's messages until it reports a result. Returns "" on success, the
    error text on failure, or None if the child died without reporting."""
    buf = b""
    fd = out.fileno()
    while True:
        ready, _, _ = select.select([fd], [], [], interval)
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                return None
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                result = _message(line, inp, progress, source, specs, Progress)
                if result is not None:
                    return result
            if len(buf) > MAX_LINE:
                raise _native_error("isolated engine sent an oversized message")
        elif child.poll() is not None:
            return None


def _native_error(msg: str):
    from ._native import NativeError
    log.error("%s", msg)
    return NativeError(msg)


def _message(line: bytes, inp: IO[bytes], progress, source, specs, Progress) -> str | None:
    try:
        m = json.loads(line)
        if not isinstance(m, dict):
            raise TypeError("not an object")
        t = m.get("t")
        if t == "progress":
            if progress:
                progress(Progress(*(_int(m[k]) for k in ("fd", "ft", "sd", "st"))))
        elif t == "log":
            level, msg = _int(m["level"], 0, 50), m["msg"]
            if not isinstance(msg, str):
                raise ValueError("log text")
            engine_log.log(level, "%s", msg[:4096])
        elif t == "sandbox":
            log.info("isolated engine: landlock ABI %s, seccomp %s, no_new_privs %s",
                     _int(m["landlock"], -4096, 100), bool(m["seccomp"]), bool(m["nnp"]))
            if not m["landlock"] or m["landlock"] < 0:
                log.warning("isolated engine: no Landlock filesystem restriction on this kernel "
                            "(seccomp and no_new_privs still apply)")
        elif t == "read":
            if source is None:
                raise ValueError("read request without a sector source")
            lsn, count = _int(m["lsn"], 0, 1 << 32), _int(m["count"], 1, MAX_READ)
            try:
                data = source(lsn, count)
                ok = len(data) == count * 2048
            except Exception:
                log.exception("sector source failed at LSN %d (+%d)", lsn, count)
                data, ok = b"", False
            inp.write(b"\x01" + data if ok else b"\x00")
            inp.flush()
        elif t == "done":
            outs = m["outputs"]
            if not isinstance(outs, list) or len(outs) != len(specs):
                raise ValueError("output count")
            for s, o in zip(specs, outs):
                s.frames_written, s.frames_missing = _int(o["frames_written"]), _int(o["frames_missing"])
                s.dst_errors, s.samples, s.data_bytes = _int(o["dst_errors"]), _int(o["samples"]), _int(o["data_bytes"])
                idx = o["dst_index"]
                if not isinstance(idx, list) or len(idx) > max(0, s.tc_end - s.tc_start):
                    raise ValueError("DST index")
                s.dst_index = [(_int(a), _int(b, 0, 1 << 32)) for a, b in idx]
            err = m["error"]
            if not isinstance(err, str):
                raise ValueError("error text")
            return err[:256]
        else:
            raise ValueError(f"unknown message type {t!r}")
    except (ValueError, KeyError, TypeError) as e:
        raise _native_error(f"isolated engine sent a malformed message ({e})") from None
    return None
