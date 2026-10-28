"""ctypes binding for the native engine (_sacdconv.so/.dylib/.dll, built by `make`)."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .toc import Area, SACDImage

FORMATS = {"dsf": 0, "flac": 1, "wav": 2, "dff": 3, "dff-dst": 4}
SCAN_SLACK = 128    # sectors read around the needed range (frame context + boundary frames)


class _Output(ctypes.Structure):
    # Must match sc_output_t in csrc/sacdconv.c
    _fields_ = [
        ("format", ctypes.c_int),
        ("path", ctypes.c_char_p),
        ("tc_start", ctypes.c_uint32),
        ("tc_end", ctypes.c_uint32),
        ("head_frame", ctypes.c_int32),
        ("head_bytes", ctypes.c_uint32),
        ("tail_bytes", ctypes.c_uint32),
        ("header", ctypes.c_char_p),
        ("header_len", ctypes.c_uint32),
        ("footer", ctypes.c_char_p),
        ("footer_len", ctypes.c_uint32),
        ("dst_offsets", ctypes.POINTER(ctypes.c_uint64)),
        ("dst_lengths", ctypes.POINTER(ctypes.c_uint32)),
        ("dst_cap", ctypes.c_uint32),
        ("flac_level", ctypes.c_int),
        ("flac_padding", ctypes.c_uint32),
        ("frames_written", ctypes.c_uint32),
        ("frames_missing", ctypes.c_uint32),
        ("dst_errors", ctypes.c_uint32),
        ("dst_count", ctypes.c_uint32),
        ("samples", ctypes.c_uint64),
        ("data_bytes", ctypes.c_uint64),
        ("error", ctypes.c_char * 256),
    ]


# int read_fn(void *ctx, uint32_t lsn, uint32_t count, uint8_t *buf)
_READ_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.POINTER(ctypes.c_uint8))
# void log_fn(void *ctx, int level, const char *msg)
_LOG_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p)


class _Area(ctypes.Structure):
    # Must match sc_area_t in csrc/sacdconv.c
    _fields_ = [
        ("iso_path", ctypes.c_char_p),
        ("sector_size", ctypes.c_int),
        ("data_offset", ctypes.c_int),
        ("scan_lsn_start", ctypes.c_uint32),
        ("scan_lsn_end", ctypes.c_uint32),
        ("channels", ctypes.c_int),
        ("pcm_rate", ctypes.c_int),
        ("gain_db", ctypes.c_double),
        ("threads", ctypes.c_int),
        ("iso_out_path", ctypes.c_char_p),
        ("iso_sectors", ctypes.c_uint32),
        ("cancel", ctypes.c_int),
        ("frames_done", ctypes.c_uint64),
        ("frames_total", ctypes.c_uint64),
        ("sectors_done", ctypes.c_uint64),
        ("sectors_total", ctypes.c_uint64),
        ("error", ctypes.c_char * 256),
        ("read_fn", _READ_FN),
        ("read_ctx", ctypes.c_void_p),
        ("log_fn", _LOG_FN),
        ("log_ctx", ctypes.c_void_p),
        ("log_level", ctypes.c_int),
    ]


class NativeError(RuntimeError):
    pass


log = logging.getLogger("pysacd.engine")

# Reads 2048-byte sectors (lsn, count) -> bytes, e.g. from a disc on a network server
SectorSource = Callable[[int, int], bytes]


_lib = None
LIBRARY = {"win32": "_sacdconv.dll", "darwin": "_sacdconv.dylib"}.get(sys.platform, "_sacdconv.so")


def _load():
    global _lib
    if _lib is None:
        path = Path(__file__).with_name(LIBRARY)
        if not path.exists():
            raise NativeError(f"{path} not built; run `make` in the pysacd source tree")
        lib = ctypes.CDLL(str(path))
        lib.sacdconv_area_run.argtypes = [ctypes.POINTER(_Area), ctypes.POINTER(_Output), ctypes.c_int]
        lib.sacdconv_area_run.restype = ctypes.c_int
        lib.sacdconv_has_flac.restype = ctypes.c_int
        lib.sacdconv_build_info.restype = ctypes.c_char_p
        _check_abi(lib, path)
        _lib = lib
    return _lib


ABI = 1    # SACDCONV_ABI in csrc/sacdconv.c


def _layout(struct) -> list[int]:
    return [ctypes.sizeof(struct)] + [getattr(struct, name).offset for name, _ in struct._fields_]


def _check_abi(lib, path) -> None:
    """Refuse an engine built from different struct definitions than the ctypes mirrors here:
    a mismatch would make the engine read and write the wrong fields."""
    try:
        abi = lib.sacdconv_abi()
    except AttributeError:
        abi = 0
    if abi != ABI:
        raise NativeError(f"{path} has engine ABI {abi}, this pysacd needs {ABI}; rebuild it (make clean all)")
    lib.sacdconv_layout.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
    for which, struct in ((0, _Area), (1, _Output)):
        want = _layout(struct)
        buf = (ctypes.c_uint32 * 64)()
        n = lib.sacdconv_layout(which, buf, 64)
        got = list(buf[:min(n, 64)])
        if got != want:
            raise NativeError(f"{path}: {struct.__name__} layout differs from the engine's "
                              f"(engine {got}, pysacd {want}); rebuild it (make clean all)")


def has_flac() -> bool:
    return bool(_load().sacdconv_has_flac())


def library_path() -> str:
    return str(Path(__file__).with_name(LIBRARY))


def build_info() -> str:
    """Compiler and features the engine was built with, for diagnostic logs."""
    return _load().sacdconv_build_info().decode()


@dataclass
class OutputSpec:
    """One file to produce from an area: frames [tc_start, tc_end)."""
    format: str
    path: str | os.PathLike
    tc_start: int
    tc_end: int
    head_frame: int = -1          # DSF nopad: carry-in from this frame's tail
    head_bytes: int = 0
    tail_bytes: int = 0           # DSF nopad: bytes/channel left for the next file
    header: bytes = b""           # DSDIFF header
    footer: bytes = b""           # DSF ID3 tag / DSDIFF trailing chunks
    flac_level: int = 5
    flac_padding: int = 0
    # results
    frames_written: int = 0
    frames_missing: int = 0
    dst_errors: int = 0
    samples: int = 0
    data_bytes: int = 0
    dst_index: list[tuple[int, int]] = field(default_factory=list)   # DSDIFF DST: (offset, length)


@dataclass
class Progress:
    frames_done: int
    frames_total: int
    sectors_done: int
    sectors_total: int


def _scan_range(area: Area, specs: list[OutputSpec]) -> tuple[int, int]:
    """LSN range covering every frame the outputs need."""
    tracks = area.tracks
    lo_tc = min(min(s.tc_start, s.head_frame if s.head_frame >= 0 else s.tc_start) for s in specs)
    hi_tc = max(s.tc_end for s in specs)
    lo = area.track_start
    hi = area.track_end
    for i, t in enumerate(tracks):
        if t.start.total_frames <= lo_tc:
            lo = t.start_lsn if i else area.track_start
        nxt = tracks[i + 1] if i + 1 < len(tracks) else None
        if nxt and nxt.start.total_frames >= hi_tc and hi == area.track_end:
            hi = nxt.start_lsn
    return max(area.track_start, lo - SCAN_SLACK), min(area.track_end, hi + SCAN_SLACK)


def run_area(image: SACDImage, iso_path: str | os.PathLike, area: Area | None, specs: list[OutputSpec],
             pcm_rate: int = 88200, gain_db: float = 0.0, threads: int = 0,
             iso_out: str | os.PathLike | None = None,
             progress: Callable[[Progress], None] | None = None, interval: float = 0.5,
             source: SectorSource | None = None) -> None:
    """Produce all outputs from one area in a single pass. Blocking, multithreaded.

    `progress` is called from this thread every `interval` seconds while the
    engine runs. Raises NativeError on failure; results are stored on the specs.
    With `source`, sectors come from that callable instead of the file at
    iso_path (which is then only a name for messages). Engine diagnostics go to
    the "pysacd.engine" logger.
    """
    lib = _load()
    outs = (_Output * max(1, len(specs)))()
    keep = []   # keep ctypes buffers alive
    for o, s in zip(outs, specs):
        o.format = FORMATS[s.format]
        o.path = os.fsencode(s.path)
        o.tc_start, o.tc_end = s.tc_start, s.tc_end
        o.head_frame, o.head_bytes, o.tail_bytes = s.head_frame, s.head_bytes, s.tail_bytes
        o.header, o.header_len = s.header, len(s.header)
        o.footer, o.footer_len = s.footer, len(s.footer)
        o.flac_level, o.flac_padding = s.flac_level, s.flac_padding
        if s.format == "dff-dst":
            cap = s.tc_end - s.tc_start
            offs, lens = (ctypes.c_uint64 * cap)(), (ctypes.c_uint32 * cap)()
            keep += [offs, lens]
            o.dst_offsets = ctypes.cast(offs, ctypes.POINTER(ctypes.c_uint64))
            o.dst_lengths = ctypes.cast(lens, ctypes.POINTER(ctypes.c_uint32))
            o.dst_cap = cap

    a = _Area()
    a.iso_path = os.fsencode(iso_path)
    a.sector_size, a.data_offset = image.sector_size, image.data_offset
    if specs:
        assert area is not None, "outputs need an area"
        a.scan_lsn_start, a.scan_lsn_end = _scan_range(area, specs)
        # The range comes from the TOC, which may be hostile: a sector source must only ever be
        # asked for sectors that exist (#11). A file needs no check: pread stops at its end.
        if source is not None and not 0 <= a.scan_lsn_start <= a.scan_lsn_end < image.total_sectors:
            raise NativeError(f"area LSN {a.scan_lsn_start}-{a.scan_lsn_end} lies outside the image "
                              f"({image.total_sectors} sectors)")
        a.channels = area.channel_count
    else:
        a.channels = 2
    a.pcm_rate = pcm_rate
    a.gain_db = gain_db
    a.threads = threads
    if iso_out is not None:
        a.iso_out_path = os.fsencode(iso_out)
        a.iso_sectors = image.total_sectors

    # Callbacks run on engine threads; they must never raise into C.
    def on_log(_ctx, level, msg):
        log.log(level, msg.decode(errors="replace"))

    def on_read(_ctx, lsn, count, buf):
        try:
            data = source(lsn, count)
            if len(data) != count * 2048:
                raise OSError(f"short read: {len(data)} of {count * 2048} bytes")
            ctypes.memmove(buf, data, len(data))
            return 0
        except Exception as e:
            log.exception("sector source failed at LSN %d (+%d)", lsn, count)
            a.error = str(e).encode(errors="replace")[:255]
            return -1

    callbacks = [_LOG_FN(on_log)]
    a.log_fn = callbacks[0]
    a.log_level = log.getEffectiveLevel()
    if source is not None:
        if image.sector_size != 2048:
            raise NativeError("a sector source must deliver plain 2048-byte sectors")
        callbacks.append(_READ_FN(on_read))
        a.read_fn = callbacks[1]
    for s in specs:
        log.debug("output %s %s: frames %d-%d%s", s.format, s.path, s.tc_start, s.tc_end,
                  f", nopad head {s.head_bytes}@{s.head_frame} tail {s.tail_bytes}" if s.head_frame >= 0 or s.tail_bytes
                  else "")

    result = []
    t = threading.Thread(target=lambda: result.append(lib.sacdconv_area_run(ctypes.byref(a), outs, len(specs))),
                         name="engine")
    t.start()
    try:
        while t.is_alive():
            t.join(interval)
            if progress:
                progress(Progress(a.frames_done, a.frames_total, a.sectors_done, a.sectors_total))
    except BaseException:
        a.cancel = 1
        t.join()
        raise

    for o, s in zip(outs, specs):
        s.frames_written, s.frames_missing, s.dst_errors = o.frames_written, o.frames_missing, o.dst_errors
        s.samples, s.data_bytes = o.samples, o.data_bytes
        if s.format == "dff-dst":
            n = min(o.dst_count, o.dst_cap)
            s.dst_index = [(o.dst_offsets[i], o.dst_lengths[i]) for i in range(n)]
    if result and result[0] != 0:
        raise NativeError(a.error.decode(errors="replace"))
